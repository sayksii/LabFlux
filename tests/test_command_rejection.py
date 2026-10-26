import copy
import socket
import threading
import time
import uuid

import httpx
import uvicorn
from conftest import heartbeat, register, submit, user_headers, worker_headers
from test_worker import ControlledExecutor

from labflux.api import create_app
from labflux.config import Settings
from labflux.database import commands
from labflux.scheduler import ServiceError
from labflux.worker import Worker

DEMO_USER = {"Authorization": "Bearer dev-user-token"}  # Settings() default user
CARD = {
    "uuid": "GPU-00000001-0000-0000-0000-000000000000",
    "index": 0,
    "name": "NVIDIA GeForce RTX 3090",
    "memory_total_mib": 24576,
}


def worker_for(client, tmp_path, instance, executor=None):
    """A Worker with no pooled GPUs, speaking for an already registered instance."""
    worker = Worker(
        Settings(data_dir=tmp_path / "worker", worker_id="vm-a"),
        executor=executor or ControlledExecutor(),
    )
    worker.instance_id = instance
    worker.client.close()
    worker.client = client
    worker.client.headers["Authorization"] = "Bearer dev-worker-token"
    return worker


def get_job(client, job):
    return client.get(f"/api/jobs/{job['id']}", headers=user_headers()).json()


def allocated(client):
    return client.get("/api/nodes", headers=user_headers()).json()[0]["allocated"]


def submit_gpu(client):
    response = client.post(
        "/api/jobs",
        headers=user_headers(),
        json={"image": "python:3.12-slim", "argv": ["true"], "resources": {"cpu": 1, "gpu": 1}},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_foreign_gpu_start_ends_failed_and_releases_its_resources(client, service, tmp_path):
    instance = register(client, cpu=1, gpus=[CARD])
    heartbeat(client, "vm-a", instance, gpus=[CARD])
    executor = ControlledExecutor()
    worker = worker_for(client, tmp_path, instance, executor)  # Its own pool has no cards.
    try:
        job = submit_gpu(client)
        assert allocated(client)["gpu_uuids"] == [CARD["uuid"]]
        [command] = service.poll_commands("vm-a", instance)
        worker.process([command])
        assert service.poll_commands("vm-a", instance) == []  # Acked, so never redelivered.
        assert executor.calls == 0
        worker.flush()
        failed = get_job(client, job)
        assert failed["state"] == "FAILED"
        assert "outside this worker's pool" in failed["reason"]
        assert allocated(client) == {
            "cpu": 0,
            "memory_mib": 0,
            "jobs": 0,
            "gpu": 0,
            "gpu_uuids": [],
        }
    finally:
        worker.pool.shutdown(wait=True)


def test_malformed_commands_are_acked_and_only_valid_ids_get_a_result(client, service, tmp_path):
    instance = register(client, cpu=4)
    worker = worker_for(client, tmp_path, instance)
    try:
        broken, foreign, garbled = submit(client), submit(client), submit(client)
        polled = {c["attempt_id"]: c for c in service.poll_commands("vm-a", instance)}
        bad_spec = copy.deepcopy(polled[broken["current_attempt_id"]])
        bad_spec["payload"]["job"]["argv"] = []  # Still names a valid attempt of this worker.
        other_worker = copy.deepcopy(polled[foreign["current_attempt_id"]])
        other_worker["payload"]["worker_id"] = "vm-b"
        bad_id = copy.deepcopy(polled[garbled["current_attempt_id"]])
        bad_id["payload"]["attempt_id"] = "../../escape"
        bad_id.pop("attempt_id")
        worker.process([bad_spec, other_worker, bad_id, "not a command", {"kind": "START"}])
        assert service.poll_commands("vm-a", instance) == []  # All three real commands acked.
        assert [p.name for p in worker.root.iterdir()] == [broken["current_attempt_id"]]
        worker.flush()
        assert get_job(client, broken)["state"] == "FAILED"
        assert "rejected command" in get_job(client, broken)["reason"]
        # Not tied to a local attempt: the Scheduler keeps them reserved until confirmation.
        assert get_job(client, foreign)["state"] == get_job(client, garbled)["state"] == "STARTING"
        assert allocated(client)["cpu"] == 2
    finally:
        worker.pool.shutdown(wait=True)


def test_rejection_never_overwrites_an_attempt_that_is_already_running(client, service, tmp_path):
    instance = register(client, cpu=1)
    executor = ControlledExecutor()
    worker = worker_for(client, tmp_path, instance, executor)
    try:
        job = submit(client)
        [command] = service.poll_commands("vm-a", instance)
        worker.process([command])
        bad_stop = copy.deepcopy(command)
        bad_stop["kind"] = "STOP"
        bad_stop["payload"]["job"] = {"argv": []}
        worker._reject(command["attempt_id"], "rejected command: test")
        assert worker._read(command["attempt_id"])["result"] is None
        executor.release.set()
        worker.handles[command["attempt_id"]]["future"].result(timeout=5)
        worker.flush()
        assert get_job(client, job)["state"] == "SUCCEEDED"
    finally:
        executor.release.set()
        worker.pool.shutdown(wait=True)


def wait_for(predicate, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition timed out")


def test_worker_loop_survives_bad_commands_and_runs_the_next_valid_one(tmp_path):
    settings = Settings(data_dir=tmp_path / "scheduler", scheduler_interval_s=0.05)
    app = create_app(settings)
    service = app.state.scheduler
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    address = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    wait_for(lambda: server.started)
    executor = ControlledExecutor()
    executor.release.set()  # Every valid job finishes at once.
    worker = Worker(
        Settings(
            data_dir=tmp_path / "worker",
            scheduler_url=address,
            worker_id="vm-a",
            allocatable_cpu=2,
            allocatable_memory_mib=1024,
            heartbeat_interval_s=0.2,
            poll_interval_s=0.05,
        ),
        executor=executor,
    )
    worker_thread = None
    try:
        with httpx.Client(base_url=address, trust_env=False) as client:
            capacity = {"total_cpu": 2, "total_memory_mib": 2048, "cpu": 2, "memory_mib": 1024}
            client.post(
                "/internal/workers/register",
                headers=worker_headers(),
                json={"worker_id": "vm-a", "instance_id": uuid.uuid4().hex, "capacity": capacity},
            ).raise_for_status()
            spec = {"image": "python:3.12-slim", "argv": ["true"], "resources": {"cpu": 1}}
            stray = client.post("/api/jobs", headers=DEMO_USER, json=spec).json()
            stray_attempt = stray["current_attempt_id"]
            # A START that names another worker: the poll must ack it and move on.
            with service.db.transaction() as connection:
                connection.execute(
                    commands.update()
                    .where(commands.c.id == f"start-{stray_attempt}")
                    .values(
                        payload={
                            "job_id": stray["id"],
                            "attempt_id": stray_attempt,
                            "worker_id": "vm-b",
                            "job": stray["spec"],
                        }
                    )
                )
            accepted, original = [], worker.accept

            def flaky(command):
                accepted.append(command["attempt_id"])
                if len(accepted) == 1:
                    raise RuntimeError("transient failure inside accept()")
                return original(command)

            worker.accept = flaky
            worker_thread = threading.Thread(target=worker.run, daemon=True)
            worker_thread.start()

            def stray_acked():
                try:
                    return service.poll_commands("vm-a", worker.instance_id) == []
                except ServiceError:
                    return False  # The new instance has not registered yet.

            wait_for(stray_acked)
            valid = client.post("/api/jobs", headers=DEMO_USER, json=spec).json()

            def succeeded():
                value = client.get(f"/api/jobs/{valid['id']}", headers=DEMO_USER)
                return value.json() if value.json()["state"] == "SUCCEEDED" else None

            done = wait_for(succeeded)
            assert done["state"] == "SUCCEEDED"
            assert worker_thread.is_alive()
            # The transient failure was retried at the next poll rather than lost or acked.
            assert accepted[:2] == [valid["current_attempt_id"]] * 2
            assert stray_attempt not in accepted and not (worker.root / stray_attempt).exists()
            stray_now = client.get(f"/api/jobs/{stray['id']}", headers=DEMO_USER).json()
            assert stray_now["state"] == "STARTING"
            nodes = client.get("/api/nodes", headers=DEMO_USER).json()
            assert nodes[0]["allocated"]["cpu"] == 1  # Still held for the stray attempt.
    finally:
        worker.stop_event.set()
        if worker_thread:
            worker_thread.join(timeout=20)
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
