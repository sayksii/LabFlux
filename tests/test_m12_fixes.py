"""M12 fixes before the demo: a decision for every model stop, other users' failure reasons, a
scheduling loop that survives a failed pass, and GPU-aware CPU/RAM blockers."""

import logging
import time
from contextlib import ExitStack

import pytest
from conftest import heartbeat, register, submit, user_headers, worker_headers
from fastapi.testclient import TestClient
from test_model_services import FAKE, model_jobs, report

from labflux.api import create_app
from labflux.config import ModelConfig, Settings
from labflux.explain import blocker
from labflux.views import public_reason


@pytest.fixture
def make_client(tmp_path, clock):
    with ExitStack() as stack:

        def build(**settings):
            values = Settings(
                data_dir=tmp_path / "data",
                users={"alice": "alice-token", "bob": "bob-token"},
                **settings,
            )
            app = create_app(values, run_loop=False, clock=lambda: clock[0])
            return stack.enter_context(TestClient(app))

        yield build


def stopping(service):
    return [
        d for d in service.queue_view("bob")["recent_decisions"] if d["kind"] == "model_stopping"
    ]


# 1. model_stopping -----------------------------------------------------------------------------


def test_reclaiming_an_idle_model_is_a_decision_naming_its_node_and_reason(make_client, clock):
    client = make_client(models={"fake": FAKE})
    service = client.app.state.scheduler
    instance = register(client, "beta", cpu=1, datasets=["fake-model"])
    job_id = service.ensure_model("fake")["job_id"]
    report(client, service, job_id, instance)
    submit(client, cpu=1)  # Waits for the model's CPU, so reclaim_idle_s applies.
    clock[0] += 61
    heartbeat(client, "beta", instance)
    service.tick()
    assert service.list_models()[0]["state"] == "stopping"
    reason = "idle for 61s while other jobs wait for resources"
    [decision] = stopping(service)
    assert decision["job_id"] == job_id and decision["node_id"] == "beta"
    assert decision["detail"] == {"service": "fake", "reason": reason}
    assert decision["text"] == f"model fake: {reason}; stopping it"
    service.tick()  # Already stopping: not recorded again.
    assert len(stopping(service)) == 1


def test_a_configuration_change_records_the_stop_too(make_client, clock):
    persistent = {**FAKE, "mode": "persistent"}
    client = make_client(models={"fake": persistent})
    service = client.app.state.scheduler
    instance = register(client, "beta", cpu=1, datasets=["fake-model"])
    service.tick()
    [job] = model_jobs(service)
    report(client, service, job["id"], instance)
    service.settings.models = {"fake": ModelConfig(**persistent, port=9000)}
    service.tick()
    [decision] = stopping(service)
    assert (decision["job_id"], decision["node_id"]) == (job["id"], "beta")
    assert decision["detail"]["reason"] == "model configuration changed"


# 2. Other users' reasons -----------------------------------------------------------------------


def end(client, job_id, instance, outcome, reason, user="alice"):
    """The Worker reports the job's current attempt as ended."""
    attempt = client.get(f"/api/jobs/{job_id}", headers=user_headers(user)).json()["attempts"][-1]
    response = client.post(
        f"/internal/workers/{attempt['node_id']}/attempts/{attempt['id']}/events",
        headers=worker_headers(),
        json={
            "event_id": f"end-{attempt['id']}",
            "instance_id": instance,
            "kind": "FINISHED",
            "outcome": outcome,
            "exit_code": None,
            "termination_confirmed": True,
            "reason": reason,
        },
    )
    assert response.status_code == 200, response.text


def test_others_see_a_failure_that_could_quote_the_image_only_as_failed(make_client):
    client = make_client()
    instance = register(client, "beta", cpu=4)
    leaky = submit(client, image="registry.invalid/alice/private-proj:1")
    oom = submit(client)
    error = "404 Client Error: pull access denied for registry.invalid/alice/private-proj"
    end(client, leaky["id"], instance, "FAILED", error)
    end(client, oom["id"], instance, "FAILED", "memory limit exceeded (OOM)")

    def reasons(user):
        response = client.get("/api/jobs", headers=user_headers(user), params={"scope": "all"})
        assert response.status_code == 200, response.text
        return {item["id"]: item["reason"] for item in response.json()}

    assert reasons("bob") == {leaky["id"]: "failed", oom["id"]: "memory limit exceeded (OOM)"}
    assert reasons("alice")[leaky["id"]] == error  # The owner still sees everything.
    assert "private-proj" not in str(client.app.state.scheduler.queue_view("bob"))


@pytest.mark.parametrize(
    "reason",
    [
        "waiting for scheduling",
        "assigned to beta",
        "executing",
        "waiting for matching CPU/RAM/GPU capacity or datasets",
        "cancelled while queued",
        "cancelled while queued: no request is waiting for it",
        "completed",
        "completed; log limit reached",
        "cancelled",
        "time limit exceeded",
        "memory limit exceeded (OOM)",
        "worker heartbeat expired; resources remain reserved",
        "waiting for confirmed container termination",
        "preempted by job 0123abcd; waiting for confirmed container termination",
        "preempted by job 0123abcd; queued to run again (preemption 1 of 3)",
        "container missing after worker restart; queued for retry 1 of 2",
        "session lease expired; waiting for confirmed container termination",
        "idle for 61s while other jobs wait for resources; waiting for confirmed container "
        "termination",
    ],
)
def test_known_scheduler_and_worker_reasons_stay_visible(reason):
    assert public_reason(reason, "FAILED") == reason


@pytest.mark.parametrize(
    ("reason", "state", "shown"),
    [
        ('OCI runtime create failed: exec: "train_secret.py": not found', "FAILED", "failed"),
        ("output collection failed: /workspace/secret.bin", "FAILED", "failed"),
        ("executing; rm -rf /data", "RUNNING", "running"),
        ("docker error; waiting for confirmed container termination", "STOPPING", "stopping"),
    ],
)
def test_other_reasons_become_the_state(reason, state, shown):
    assert public_reason(reason, state) == shown


# 3. The scheduling loop ------------------------------------------------------------------------


def test_a_failed_scheduling_pass_is_logged_and_the_loop_goes_on(tmp_path, caplog):
    settings = Settings(
        data_dir=tmp_path, users={"alice": "alice-token"}, scheduler_interval_s=0.01
    )
    app = create_app(settings)
    calls = []

    def tick():
        calls.append(time.monotonic())
        if len(calls) == 1:
            raise RuntimeError("database is locked")

    app.state.scheduler.tick = tick
    with caplog.at_level(logging.ERROR, logger="labflux.api"), TestClient(app):
        deadline = time.monotonic() + 5
        while len(calls) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
    assert len(calls) >= 3
    [record] = [r for r in caplog.records if r.name == "labflux.api"]
    assert record.getMessage() == "scheduling pass failed"
    assert "database is locked" in str(record.exc_info[1])


# 4. GPU-aware CPU and RAM blockers -------------------------------------------------------------

A40S = [
    {
        "uuid": f"GPU-0000000{index}-0000-0000-0000-000000000000",
        "index": index,
        "name": "NVIDIA A40",
        "memory_total_mib": 46068,
    }
    for index in (3, 4)
]


def node(name, cpu, memory_mib, gpus=()):
    return {
        "id": name,
        "capacity": {"cpu": cpu, "memory_mib": memory_mib, "datasets": [], "gpus": list(gpus)},
        "observed": {"gpus": [{"uuid": gpu["uuid"], "external_use": False} for gpu in gpus]},
    }


def two_a40s(cpu=16, memory_mib=8192):
    return {"cpu": cpu, "memory_mib": memory_mib, "gpu": 2, "gpu_model": "A40", "min_vram_mib": 0}


def explain(request, beta, gamma):
    """beta has no GPU; gamma has both A40s free."""
    idle = {"cpu": 0, "memory_mib": 0, "jobs": 0}
    return blocker(request, [], [beta, gamma], {"beta": idle, "gamma": idle}, set(), 8)


@pytest.mark.parametrize("beta_cpu", [12, 20])
def test_a_gpu_jobs_cpu_blocker_names_only_a_node_with_the_cards(beta_cpu):
    # beta has more free cores than gamma, whether or not they would be enough.
    found = explain(two_a40s(), node("beta", beta_cpu, 65536), node("gamma", 10, 65536, A40S))
    assert found == {"kind": "cpu", "need": 16, "best_free": 10, "best_node": "gamma"}


def test_a_gpu_jobs_memory_blocker_names_only_a_node_with_the_cards():
    request = two_a40s(cpu=4, memory_mib=32768)
    found = explain(request, node("beta", 32, 65536), node("gamma", 32, 16384, A40S))
    assert found == {
        "kind": "memory",
        "need_mib": 32768,
        "best_free_mib": 16384,
        "best_node": "gamma",
    }


def test_a_job_without_gpus_still_compares_every_node():
    request = {"cpu": 16, "memory_mib": 1024, "gpu": 0, "gpu_model": None, "min_vram_mib": None}
    found = explain(request, node("beta", 12, 65536), node("gamma", 10, 65536, A40S))
    assert found == {"kind": "cpu", "need": 16, "best_free": 12, "best_node": "beta"}
