"""M12 views for every logged-in user: node allocations, all-users job summaries, the queue."""

from contextlib import ExitStack

import pytest
from conftest import heartbeat, register, submit, user_headers, worker_headers
from fastapi.testclient import TestClient
from sqlalchemy import select

from labflux import views
from labflux.api import create_app
from labflux.config import Settings
from labflux.database import attempts, jobs
from labflux.models import MODEL_OWNER

RTX3090 = {
    "uuid": "GPU-00000001-0000-0000-0000-000000000000",
    "index": 0,
    "name": "NVIDIA GeForce RTX 3090",
    "memory_total_mib": 24576,
}
A5000 = {
    "uuid": "GPU-00000002-0000-0000-0000-000000000000",
    "index": 1,
    "name": "NVIDIA RTX A5000",
    "memory_total_mib": 24564,
}
FAKE = {
    "image": "python:3.12-slim",
    "argv": ["python", "server.py"],
    "resources": {"cpu": 1, "memory_mib": 512},
}
SUMMARY_FIELDS = {
    "id",
    "owner",
    "mine",
    "kind",
    "service",
    "state",
    "reason",
    "schedule_note",
    "priority",
    "resources",
    "time_limit_s",
    "estimated_runtime_s",
    "safe_to_rerun",
    "preemptible",
    "project",
    "datasets",
    "created_at",
    "updated_at",
    "submission_seq",
    "current_attempt_id",
    "node_id",
    "lease_expires_at",
    "cancel_requested",
}


@pytest.fixture
def make_client(tmp_path, clock):
    """Build a Scheduler API client running the named policy; all share the test clock."""
    with ExitStack() as stack:

        def build(policy="fcfs", **settings):
            values = Settings(
                data_dir=tmp_path / policy,
                users={"alice": "alice-token", "bob": "bob-token"},
                scheduling_policy=policy,
                **settings,
            )
            app = create_app(values, run_loop=False, clock=lambda: clock[0])
            return stack.enter_context(TestClient(app))

        yield build


def get(client, path, user="alice", **params):
    response = client.get(path, headers=user_headers(user), params=params)
    assert response.status_code == 200, response.text
    return response.json()


def started(client, service, job_id, instance):
    """The Worker reports the job's current attempt as started."""
    with service.db.transaction() as connection:
        attempt_id, node_id = connection.execute(
            select(attempts.c.id, attempts.c.node_id)
            .select_from(attempts.join(jobs, jobs.c.current_attempt_id == attempts.c.id))
            .where(jobs.c.id == job_id)
        ).one()
    response = client.post(
        f"/internal/workers/{node_id}/attempts/{attempt_id}/events",
        headers=worker_headers(),
        json={"event_id": f"start-{attempt_id}", "instance_id": instance, "kind": "STARTED"},
    )
    assert response.status_code == 200, response.text


def advance(client, clock, seconds, nodes):
    """Move the clock and keep every node online (heartbeats expire after 10 seconds)."""
    clock[0] += seconds
    for name, (instance, gpus) in nodes.items():
        heartbeat(client, name, instance, gpus)


def allocations_by_job(client, user):
    return {
        node["id"]: {item["job_id"]: item for item in node["allocations"]}
        for node in get(client, "/api/nodes", user)
    }


def test_allocations_show_every_users_work_with_kind_placement_and_end(make_client, clock):
    client = make_client("backfill", models={"fake": FAKE})
    service = client.app.state.scheduler
    beta = register(client, "beta", cpu=4, memory=4096)
    delta = register(client, "delta", cpu=8, memory=8192, gpus=[RTX3090, A5000])
    nodes = {"beta": (beta, []), "delta": (delta, [RTX3090, A5000])}
    heartbeat(client, "delta", delta, [RTX3090, A5000])  # The cards are observed free.
    gpu_job = submit(client, resources={"cpu": 2, "memory_mib": 256, "gpu": 1}, time_limit_s=3600)
    advance(client, clock, 5, nodes)
    started(client, service, gpu_job["id"], delta)  # t=1005
    response = client.post("/api/sessions", headers=user_headers("bob"), json={})
    assert response.status_code == 200, response.text
    session_id = response.json()["id"]
    advance(client, clock, 5, nodes)
    started(client, service, session_id, beta)  # t=1010: its lease runs from now.
    model_id = service.ensure_model("fake")["job_id"]
    # The whole of delta, so it waits for the GPU job; the short job is backfilled.
    head = submit(client, cpu=8, user="bob", time_limit_s=3600)
    short = submit(client, cpu=1, time_limit_s=60)

    alice = allocations_by_job(client, "alice")
    assert set(alice) == {"beta", "delta"}
    assert set(alice["delta"]) == {gpu_job["id"]}
    assert set(alice["beta"]) == {session_id, model_id, short["id"]}
    assert all(head["id"] not in items for items in alice.values())
    batch = alice["delta"][gpu_job["id"]]
    assert batch == {
        "job_id": gpu_job["id"],
        "attempt_id": gpu_job["current_attempt_id"],
        "owner": "alice",
        "mine": True,
        "kind": "BATCH",
        "service": None,
        "state": "RUNNING",
        "attempt_state": "RUNNING",
        "cpu": 2,
        "memory_mib": 256,
        "gpu_uuids": [A5000["uuid"]],  # The smaller matching card.
        "created_at": 1000.0,
        "started_at": 1005.0,
        "ends_at": 1005.0 + 3600,  # Its time limit from the start.
        "lease_expires_at": None,
        "placement": "started",
        "preempted_by": None,
    }
    session = alice["beta"][session_id]
    assert (session["owner"], session["mine"], session["kind"]) == ("bob", False, "SESSION")
    assert session["state"] == "RUNNING" and session["gpu_uuids"] == []
    lease = 1010.0 + service.settings.session_lease_s
    assert session["lease_expires_at"] == session["ends_at"] == lease
    model = alice["beta"][model_id]
    assert (model["owner"], model["mine"], model["kind"]) == (MODEL_OWNER, False, "MODEL")
    assert model["service"] == "fake" and model["state"] == "STARTING"
    assert model["started_at"] is None and model["lease_expires_at"] is None
    assert model["ends_at"] == 1010.0 + 300  # Not started: its startup timeout from dispatch.
    backfilled = alice["beta"][short["id"]]
    assert backfilled["placement"] == "backfilled" and backfilled["mine"]
    assert backfilled["ends_at"] == 1010.0 + 60
    assert {item["placement"] for item in (batch, session, model)} == {"started"}

    bob = allocations_by_job(client, "bob")
    assert bob["beta"][session_id]["mine"] and not bob["delta"][gpu_job["id"]]["mine"]
    assert not bob["beta"][short["id"]]["mine"] and not bob["beta"][model_id]["mine"]
    # The existing totals stay, and agree with the allocations.
    by_id = {node["id"]: node for node in get(client, "/api/nodes", "bob")}
    assert by_id["delta"]["allocated"]["gpu_uuids"] == [A5000["uuid"]]
    assert by_id["beta"]["allocated"]["cpu"] == 3 and by_id["beta"]["allocated"]["jobs"] == 3
    assert "instance_id" not in by_id["beta"]


def test_all_users_summaries_never_show_argv_env_image_or_source(make_client):
    client = make_client()
    upload = client.post(
        "/api/artifacts/sources",
        headers={**user_headers(), "Content-Type": "application/gzip"},
        content=b"source archive bytes",
    )
    assert upload.status_code == 201, upload.text
    source_id = upload.json()["id"]
    secret = submit(
        client,
        image="registry.invalid/private-image:1",
        argv=["python", "train.py", "--token=argv-secret"],
        env={"API_KEY": "env-secret"},
        source_id=source_id,
        priority=7,
        datasets=["corpus"],
        project="thesis",
    )
    theirs = submit(client, user="bob")
    response = client.get("/api/jobs", headers=user_headers("bob"), params={"scope": "all"})
    assert response.status_code == 200, response.text
    for leaked in ("private-image", "argv-secret", "env-secret", "API_KEY", source_id):
        assert leaked not in response.text
    summaries = {item["id"]: item for item in response.json()}
    assert list(summaries) == [secret["id"], theirs["id"]]  # Submission order.
    assert all(set(item) == SUMMARY_FIELDS for item in summaries.values())
    mine = summaries[secret["id"]]
    assert mine["owner"] == "alice" and not mine["mine"] and summaries[theirs["id"]]["mine"]
    assert mine["state"] == "QUEUED" and mine["node_id"] is None
    assert (mine["priority"], mine["project"], mine["datasets"]) == (7, "thesis", ["corpus"])
    assert mine["resources"]["cpu"] == 1 and mine["time_limit_s"] == 3600
    # Without scope (or scope=mine) the caller's own full rows, unchanged.
    own = get(client, "/api/jobs", "bob")
    assert [job["id"] for job in own] == [theirs["id"]] and "spec" in own[0]
    assert get(client, "/api/jobs", "bob", scope="mine") == own


def test_all_users_summaries_name_the_node_of_running_jobs(make_client):
    client = make_client()
    register(client, "beta")
    job = submit(client)
    [summary] = get(client, "/api/jobs", "bob", scope="all")
    assert summary["state"] == "STARTING" and summary["node_id"] == "beta"
    assert summary["current_attempt_id"] == job["current_attempt_id"]


def test_all_users_summaries_keep_ended_jobs_for_24_hours(make_client, clock, monkeypatch):
    client = make_client()

    def cancelled(user="alice"):
        value = submit(client, user=user)  # No node: it stays queued until cancelled.
        response = client.post(f"/api/jobs/{value['id']}/cancel", headers=user_headers(user))
        assert response.json()["state"] == "CANCELLED"
        return value["id"]

    def listed():
        return [item["id"] for item in get(client, "/api/jobs", scope="all")]

    old = cancelled()
    waiting = submit(client)["id"]
    clock[0] += 24 * 3600 - 10
    recent = [cancelled("bob"), cancelled(), cancelled("bob")]
    assert listed() == [old, waiting, *recent]  # old ended 10 s short of 24 h ago.
    clock[0] += 11
    assert listed() == [waiting, *recent]
    # At most SUMMARY_TERMINAL_LIMIT ended jobs: the newest; unfinished ones always.
    monkeypatch.setattr(views, "SUMMARY_TERMINAL_LIMIT", 2)
    assert listed() == [waiting, *recent[1:]]


def test_unknown_scope_is_rejected(client):
    response = client.get("/api/jobs", headers=user_headers(), params={"scope": "everyone"})
    assert response.status_code == 422
    assert client.get("/api/jobs", params={"scope": "all"}).status_code == 401


def test_queue_endpoint_serves_the_last_scheduling_pass(make_client):
    """Smoke test of GET /api/queue; Scheduler.queue_view is tested in test_queue_view.py."""
    client = make_client("priority")
    register(client, "beta", cpu=1)
    submit(client, user="bob")
    waiting = submit(client)
    assert client.get("/api/queue").status_code == 401
    value = get(client, "/api/queue", "alice")
    assert value["policy"] == "priority" and value["computed_at"] == 1000.0
    [entry] = value["entries"]
    assert entry["job_id"] == waiting["id"] and entry["owner"] == "alice" and entry["mine"]
    assert entry["position"] == 1
    assert not get(client, "/api/queue", "bob")["entries"][0]["mine"]
    assert isinstance(value["recent_decisions"], list)
