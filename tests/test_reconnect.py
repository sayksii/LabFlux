import base64
import json
import time
import uuid

import pytest
from conftest import finish, heartbeat, register, submit, user_headers, worker_headers
from test_worker import ControlledExecutor, prepare

from labflux.config import Settings
from labflux.worker import Worker

CARD = {
    "uuid": "GPU-00000001-0000-0000-0000-000000000000",
    "index": 0,
    "name": "RTX",
    "memory_total_mib": 24576,
}


def get_job(client, job):
    return client.get(f"/api/jobs/{job['id']}", headers=user_headers()).json()


def allocated(client, name="vm-a"):
    return next(
        n for n in client.get("/api/nodes", headers=user_headers()).json() if n["id"] == name
    )["allocated"]


def restart(client, inventory, name="vm-a", cpu=1, instance=None, gpus=None):
    """Register a new instance (or replay one) together with what it found locally."""
    instance = instance or uuid.uuid4().hex
    response = client.post(
        "/internal/workers/register",
        headers=worker_headers(),
        json={
            "worker_id": name,
            "instance_id": instance,
            "capacity": {
                "total_cpu": cpu,
                "total_memory_mib": 1536,
                "cpu": cpu,
                "memory_mib": 1024,
                "gpus": gpus or [],
            },
            "inventory": inventory,
        },
    )
    return instance, response


def deliver(client, service, instance, name="vm-a", started=True):
    """Poll, ack and (optionally) report STARTED for the first pending START."""
    command = next(c for c in service.poll_commands(name, instance) if c["kind"] == "START")
    client.post(
        f"/internal/workers/{name}/commands/{command['id']}/ack",
        headers=worker_headers(),
        json={"instance_id": instance},
    )
    if started:
        response = client.post(
            f"/internal/workers/{name}/attempts/{command['attempt_id']}/events",
            headers=worker_headers(),
            json={"instance_id": instance, "event_id": f"start-{instance}", "kind": "STARTED"},
        )
        assert response.status_code == 200, response.text
    return command


def container(attempt_id, status="running", worker_id="vm-a"):
    return {"attempt_id": attempt_id, "worker_id": worker_id, "status": status, "started_at": 990.0}


@pytest.mark.parametrize("status, state", [("running", "RUNNING"), ("exited", "RUNNING")])
def test_surviving_container_is_adopted_with_its_allocation(client, service, status, state):
    old = register(client, cpu=1)
    job = submit(client)
    queued = submit(client)
    command = deliver(client, service, old)
    attempt_id = command["attempt_id"]
    new, response = restart(
        client,
        {"attempts": [{"attempt_id": attempt_id}], "containers": [container(attempt_id, status)]},
    )
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["status"] == "ONLINE" and value["closed"] == value["restart"] == []
    [adopted] = value["adopt"]
    assert adopted["attempt_id"] == attempt_id and adopted["payload"] == command["payload"]
    assert adopted["cancel"] is False
    assert get_job(client, job)["state"] == state
    assert get_job(client, queued)["state"] == "QUEUED"
    assert allocated(client)["cpu"] == 1
    stale = {"instance_id": old, "event_id": "late", "kind": "FINISHED", "outcome": "SUCCEEDED"}
    path = f"/internal/workers/vm-a/attempts/{attempt_id}/events"
    assert client.post(path, headers=worker_headers(), json=stale).status_code == 409
    assert finish(client, get_job(client, job), new).status_code == 200
    assert get_job(client, job)["state"] == "SUCCEEDED"
    assert get_job(client, queued)["state"] == "STARTING"


def test_created_container_is_adopted_as_starting(client, service):
    old = register(client, cpu=1)
    job = submit(client)
    command = deliver(client, service, old, started=False)
    _, response = restart(
        client,
        {
            "attempts": [{"attempt_id": command["attempt_id"]}],
            "containers": [container(command["attempt_id"], "created")],
        },
    )
    assert [a["attempt_id"] for a in response.json()["adopt"]] == [command["attempt_id"]]
    assert get_job(client, job)["state"] == "STARTING"


def test_unsent_result_is_adopted_without_a_container(client, service):
    old = register(client, cpu=1)
    job = submit(client)
    command = deliver(client, service, old)
    new, response = restart(
        client, {"attempts": [{"attempt_id": command["attempt_id"], "unsent_result": True}]}
    )
    assert [a["attempt_id"] for a in response.json()["adopt"]] == [command["attempt_id"]]
    assert allocated(client)["cpu"] == 1
    assert finish(client, get_job(client, job), new).status_code == 200
    assert allocated(client)["cpu"] == 0


def test_never_launched_attempt_restarts_from_a_redelivered_start(client, service):
    old = register(client, cpu=1)
    job = submit(client)
    command = deliver(client, service, old, started=False)
    new, response = restart(
        client, {"attempts": [{"attempt_id": command["attempt_id"], "launched": False}]}
    )
    value = response.json()
    assert value["restart"] == [command["attempt_id"]] and value["adopt"] == []
    assert get_job(client, job)["state"] == "STARTING"
    assert allocated(client)["cpu"] == 1
    assert [c["id"] for c in service.poll_commands("vm-a", new)] == [command["id"]]


def test_never_launched_cancelled_attempt_closes_as_cancelled(client, service):
    old = register(client, cpu=1)
    job = submit(client)
    command = deliver(client, service, old, started=False)
    client.post(f"/api/jobs/{job['id']}/cancel", headers=user_headers())
    _, response = restart(
        client, {"attempts": [{"attempt_id": command["attempt_id"], "launched": False}]}
    )
    assert response.json()["closed"] == [command["attempt_id"]]
    assert get_job(client, job)["state"] == "CANCELLED"
    assert allocated(client)["cpu"] == 0


def test_undelivered_start_is_left_for_normal_polling(client, service):
    register(client, cpu=1)
    job = submit(client)
    new, response = restart(client, {})
    value = response.json()
    assert value["adopt"] == value["restart"] == value["closed"] == []
    assert get_job(client, job)["state"] == "STARTING"
    assert [c["kind"] for c in service.poll_commands("vm-a", new)] == ["START"]


def report_started(client, instance, command, name="vm-a"):
    response = client.post(
        f"/internal/workers/{name}/attempts/{command['attempt_id']}/events",
        headers=worker_headers(),
        json={"instance_id": instance, "event_id": f"late-start-{instance}", "kind": "STARTED"},
    )
    assert response.status_code == 200, response.text


def test_start_reported_after_a_heartbeat_lapse_runs_the_job(client, service, clock):
    """The container started during a partition; STARTED arrives before the next heartbeat."""
    instance = register(client, cpu=1)
    job = submit(client)
    command = deliver(client, service, instance, started=False)
    clock[0] += 11
    service.tick()
    assert get_job(client, job)["state"] == "UNKNOWN"
    report_started(client, instance, command)
    value = get_job(client, job)
    assert value["state"] == "RUNNING" and value["attempts"][0]["started_at"] is not None
    heartbeat(client, "vm-a", instance)
    assert get_job(client, job)["state"] == "RUNNING"


def test_undelivered_start_run_after_a_long_restart_is_running(client, service, clock):
    """A restart outlasting offline_timeout_s: the redelivered START's STARTED is honoured."""
    register(client, cpu=1)
    job = submit(client)
    clock[0] += 11
    service.tick()
    assert get_job(client, job)["state"] == "UNKNOWN"
    new, _ = restart(client, {})
    deliver(client, service, new)
    value = get_job(client, job)
    assert value["state"] == "RUNNING" and value["attempts"][0]["started_at"] is not None


def test_start_after_cancel_and_a_lapse_stays_stopping(client, service, clock):
    instance = register(client, cpu=1)
    job = submit(client)
    command = deliver(client, service, instance, started=False)
    client.post(f"/api/jobs/{job['id']}/cancel", headers=user_headers())
    clock[0] += 11
    service.tick()
    report_started(client, instance, command)
    assert get_job(client, job)["state"] == "STOPPING"


def test_missing_container_closes_once_and_releases_cards(client, service):
    old = register(client, cpu=2, gpus=[CARD])
    heartbeat(client, "vm-a", old, gpus=[CARD])
    job = submit_gpu(client)
    waiting = submit_gpu(client)
    assert get_job(client, waiting)["state"] == "QUEUED"
    command = deliver(client, service, old)
    new, response = restart(client, {}, cpu=2, gpus=[CARD])
    assert response.json()["closed"] == [command["attempt_id"]]
    closed = get_job(client, job)
    assert closed["state"] == "FAILED"
    assert closed["reason"] == "container missing after worker restart"
    assert allocated(client)["gpu"] == 0
    heartbeat(client, "vm-a", new, gpus=[CARD])
    assert get_job(client, waiting)["state"] == "STARTING"
    # Replaying the registration (lost response) finds nothing new to close or release.
    _, replay = restart(client, {}, cpu=2, gpus=[CARD], instance=new)
    assert replay.json()["closed"] == []
    assert allocated(client)["gpu_uuids"] == [CARD["uuid"]]
    assert get_job(client, waiting)["state"] == "STARTING"


def submit_gpu(client):
    response = client.post(
        "/api/jobs",
        headers=user_headers(),
        json={"image": "python:3.12-slim", "argv": ["true"], "resources": {"cpu": 1, "gpu": 1}},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_adopted_attempt_keeps_its_card(client, service):
    old = register(client, cpu=2, gpus=[CARD])
    heartbeat(client, "vm-a", old, gpus=[CARD])
    submit_gpu(client)
    command = deliver(client, service, old)
    new, response = restart(
        client,
        {"containers": [container(command["attempt_id"])]},
        cpu=2,
        gpus=[CARD],
    )
    assert len(response.json()["adopt"]) == 1
    heartbeat(client, "vm-a", new, gpus=[CARD])
    assert submit_gpu(client)["state"] == "QUEUED"
    assert allocated(client)["gpu_uuids"] == [CARD["uuid"]]


def test_pending_stop_survives_adoption(client, service):
    old = register(client, cpu=1)
    job = submit(client)
    command = deliver(client, service, old)
    assert (
        client.post(f"/api/jobs/{job['id']}/cancel", headers=user_headers()).json()["state"]
        == "STOPPING"
    )
    new, response = restart(client, {"containers": [container(command["attempt_id"])]})
    [adopted] = response.json()["adopt"]
    assert adopted["cancel"] is True
    assert get_job(client, job)["state"] == "STOPPING"
    assert [c["kind"] for c in service.poll_commands("vm-a", new)] == ["STOP"]
    finish(client, get_job(client, job), new, outcome="CANCELLED", exit_code=137)
    assert get_job(client, job)["state"] == "CANCELLED"
    assert allocated(client)["cpu"] == 0


def test_repeated_registration_adopts_the_same_attempt_again(client, service):
    old = register(client, cpu=1)
    submit(client)
    command = deliver(client, service, old)
    inventory = {"containers": [container(command["attempt_id"])]}
    new, first = restart(client, inventory)
    _, again = restart(client, inventory, instance=new)
    assert first.json()["adopt"] == again.json()["adopt"]
    assert allocated(client)["cpu"] == 1


def test_inventory_from_another_worker_is_rejected(client, service):
    old = register(client, cpu=1)
    register(client, "vm-b", cpu=1)
    submit(client)
    command = deliver(client, service, old)
    _, response = restart(
        client, {"containers": [container(command["attempt_id"], worker_id="vm-b")]}
    )
    assert response.status_code == 422
    _, response = restart(
        client, {"attempts": [{"attempt_id": command["attempt_id"]}]}, name="vm-b"
    )
    assert response.status_code == 409
    # Nothing was reconciled: the original instance still owns the running attempt.
    assert allocated(client)["cpu"] == 1
    assert service.poll_commands("vm-a", old) == []


def test_stale_containers_are_listed_for_removal(client, service):
    old = register(client, cpu=1)
    job = submit(client)
    command = deliver(client, service, old)
    finish(client, get_job(client, job), old)
    unknown = uuid.uuid4().hex
    _, response = restart(
        client, {"containers": [container(command["attempt_id"], "exited"), container(unknown)]}
    )
    assert sorted(response.json()["remove"]) == sorted([command["attempt_id"], unknown])
    assert response.json()["adopt"] == []


def test_same_instance_returns_online_and_resumes_only_active_attempts(client, service, clock):
    instance = register(client, cpu=2)
    first, second = submit(client), submit(client)
    for _ in range(2):
        command = next(c for c in service.poll_commands("vm-a", instance))
        client.post(
            f"/internal/workers/vm-a/commands/{command['id']}/ack",
            headers=worker_headers(),
            json={"instance_id": instance},
        )
        client.post(
            f"/internal/workers/vm-a/attempts/{command['attempt_id']}/events",
            headers=worker_headers(),
            json={"instance_id": instance, "event_id": command["id"], "kind": "STARTED"},
        )
    clock[0] += 11
    service.tick()
    assert get_job(client, first)["state"] == get_job(client, second)["state"] == "UNKNOWN"
    assert submit(client)["state"] == "QUEUED"
    response = client.post(
        "/internal/workers/vm-a/heartbeat",
        headers=worker_headers(),
        json={"instance_id": instance, "active_attempts": [first["current_attempt_id"]]},
    )
    assert response.json() == {"status": "ONLINE"}
    assert get_job(client, first)["state"] == "RUNNING"
    assert get_job(client, second)["state"] == "UNKNOWN"
    assert allocated(client)["cpu"] == 2
    finish(client, get_job(client, second), instance)
    assert get_job(client, second)["state"] == "SUCCEEDED"
    assert allocated(client)["cpu"] == 2  # The queued job took the released core.


def wait(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    raise AssertionError("condition timed out")


def restarted(client, tmp_path, executor, old):
    """A second Worker instance over the same data directory, talking to the same app."""
    worker = Worker(Settings(data_dir=tmp_path / "worker", worker_id="vm-a"), executor=executor)
    worker.client.close()
    worker.client = client
    worker.client.headers["Authorization"] = "Bearer dev-worker-token"
    assert worker.instance_id != old.instance_id
    return worker


def test_worker_restart_leaves_the_container_and_the_new_instance_adopts_it(client, tmp_path):
    executor = ControlledExecutor()
    old, job, command = prepare(client, tmp_path, executor)
    attempt_id = command["attempt_id"]
    try:
        old.accept(command)
        wait(lambda: old._read(attempt_id)["started"])
        assert old.heartbeat_payload()["active_attempts"] == [attempt_id]
        old.flush()
        old.stop_event.set()
        old.handles[attempt_id]["future"].result(timeout=5)
        assert old._read(attempt_id)["result"] is None
        assert attempt_id in executor.containers  # Still running for the next instance.
        new = restarted(client, tmp_path, executor, old)
        response = new.register()
        assert [a["attempt_id"] for a in response["adopt"]] == [attempt_id]
        assert executor.adopted == [(attempt_id, {"stdout": len(b"live output\n"), "stderr": 0})]
        assert new.heartbeat_payload()["active_attempts"] == [attempt_id]
        executor.release.set()
        new.handles[attempt_id]["future"].result(timeout=5)
        new.flush()
        value = get_job(client, job)
        assert value["state"] == "SUCCEEDED" and len(value["attempts"]) == 1
        chunks = client.get(f"/api/jobs/{job['id']}/logs", headers=user_headers()).json()["chunks"]
        assert b"".join(base64.b64decode(c["data_base64"]) for c in chunks) == b"live output\n"
        assert executor.calls == 2 and not executor.containers
        assert allocated(client)["cpu"] == 0
    finally:
        executor.release.set()
        old.pool.shutdown(wait=True)


class DetachedBeforeLaunch(ControlledExecutor):
    """Shutdown arrives while the image is still being pulled, before any container exists."""

    def run(self, spec, workspace, outputs, on_log, on_started, cancel, **options):
        if not options.get("adopt") and options["detach"].is_set():
            return None
        return super().run(spec, workspace, outputs, on_log, on_started, cancel, **options)


def test_shutdown_before_launch_restarts_the_attempt_on_the_next_instance(client, tmp_path):
    executor = DetachedBeforeLaunch()
    old, job, command = prepare(client, tmp_path, executor)
    attempt_id = command["attempt_id"]
    try:
        old.stop_event.set()
        old.accept(command)
        old.handles[attempt_id]["future"].result(timeout=5)
        assert old._read(attempt_id)["launched"] is False
        new = restarted(client, tmp_path, executor, old)
        assert new.register()["restart"] == [attempt_id]
        assert not (new.root / attempt_id).exists()
        redelivered = client.app.state.scheduler.poll_commands("vm-a", new.instance_id)
        assert [c["id"] for c in redelivered] == [command["id"]]
        new.accept(redelivered[0])
        executor.release.set()
        new.handles[attempt_id]["future"].result(timeout=5)
        new.flush()
        assert get_job(client, job)["state"] == "SUCCEEDED"
        assert executor.adopted == []
    finally:
        executor.release.set()
        old.pool.shutdown(wait=True)


def test_new_instance_closes_an_attempt_whose_container_vanished(client, tmp_path):
    executor = ControlledExecutor()
    old, job, command = prepare(client, tmp_path, executor)
    attempt_id = command["attempt_id"]
    try:
        old.accept(command)
        wait(lambda: old._read(attempt_id)["started"])
        old.stop_event.set()
        old.handles[attempt_id]["future"].result(timeout=5)
        executor.containers.clear()  # Removed by hand while no Worker was running.
        new = restarted(client, tmp_path, executor, old)
        assert new.register()["closed"] == [attempt_id]
        assert get_job(client, job)["state"] == "FAILED"
        assert allocated(client)["cpu"] == 0
        state = new._read(attempt_id)
        assert state["result_sent"] and state["result"]["termination_confirmed"]
        assert new.inventory()["attempts"] == []  # Settled; later restarts don't report it.
    finally:
        executor.release.set()
        old.pool.shutdown(wait=True)


def test_stale_container_is_removed_only_with_a_local_record(client, tmp_path):
    executor = ControlledExecutor()
    old, job, command = prepare(client, tmp_path, executor)
    try:
        finish(client, get_job(client, job), old.instance_id)  # Released elsewhere.
        (old.root / command["attempt_id"]).mkdir()
        old._metadata(command["attempt_id"]).write_text(
            json.dumps({"result": None, "result_sent": False})
        )
        foreign = uuid.uuid4().hex
        executor.containers[command["attempt_id"]] = {"worker_id": "vm-a", "status": "exited"}
        executor.containers[foreign] = {"worker_id": "vm-a", "status": "running"}
        new = restarted(client, tmp_path, executor, old)
        response = new.register()
        assert sorted(response["remove"]) == sorted([command["attempt_id"], foreign])
        assert executor.removed == [command["attempt_id"]]
        assert foreign in executor.containers  # No local record: possibly another installation's.
    finally:
        old.pool.shutdown(wait=True)


def test_log_repair_drops_a_torn_line_and_follows_the_file(client, tmp_path):
    executor = ControlledExecutor()
    worker, _, command = prepare(client, tmp_path, executor)
    attempt_id = command["attempt_id"]
    try:
        worker.accept(command)
        wait(lambda: worker._read(attempt_id)["started"])
        worker.stop_event.set()
        worker.handles[attempt_id]["future"].result(timeout=5)
        path = worker.root / attempt_id / "logs.jsonl"
        extra = {"seq": 1, "stream": "stderr", "data_base64": base64.b64encode(b"oops").decode()}
        with path.open("ab") as target:
            target.write(json.dumps(extra).encode() + b"\n")  # Written, but state.json was not.
            target.write(b'{"seq": 2, "stream": "std')  # Torn by a crash.
        assert worker._repair_logs(attempt_id) == {"stdout": len(b"live output\n"), "stderr": 4}
        state = worker._read(attempt_id)
        assert state["next_seq"] == 2 and state["log_bytes"] == len(b"live output\n") + 4
        assert path.read_bytes().endswith(b"\n")
    finally:
        executor.release.set()
        worker.pool.shutdown(wait=True)
