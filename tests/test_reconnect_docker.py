import base64
import socket
import threading
import time
from contextlib import closing, contextmanager

import docker
import httpx
import pytest
import uvicorn

from labflux.api import create_app
from labflux.config import Settings
from labflux.runtime import DockerExecutor
from labflux.worker import Worker

pytestmark = pytest.mark.docker
WORKER_ID = "reconnect-e2e"


def wait_for(predicate, timeout=45):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("condition timed out")


@contextmanager
def lab(tmp_path):
    """A real Scheduler over HTTP plus a factory for Worker instances sharing one data_dir."""
    settings = Settings(data_dir=tmp_path / "scheduler", scheduler_interval_s=0.05)
    try:
        DockerExecutor(settings).check()
    except (docker.errors.DockerException, OSError, RuntimeError) as exc:
        pytest.skip(f"Docker unavailable: {exc}")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    address = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(create_app(settings), log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    wait_for(lambda: server.started)
    running = []

    def start_worker():
        worker = Worker(
            Settings(
                data_dir=tmp_path / "worker",
                scheduler_url=address,
                worker_id=WORKER_ID,
                allocatable_cpu=1,
                allocatable_memory_mib=256,
                heartbeat_interval_s=0.2,
                poll_interval_s=0.05,
            )
        )
        worker_thread = threading.Thread(target=worker.run, daemon=True)
        worker_thread.start()
        running.append((worker, worker_thread))
        return worker, worker_thread

    try:
        with httpx.Client(
            base_url=address, headers={"Authorization": "Bearer dev-user-token"}, trust_env=False
        ) as client:
            yield client, start_worker
    finally:
        for worker, worker_thread in running:
            worker.stop_event.set()
            worker_thread.join(timeout=20)
        with closing(docker.from_env()) as engine:
            for container in engine.containers.list(
                all=True, filters={"label": f"labflux.worker_id={WORKER_ID}"}
            ):
                container.remove(force=True)
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def stop(worker, worker_thread):
    worker.stop_event.set()
    worker_thread.join(timeout=20)
    assert not worker_thread.is_alive()


def submit(client, code, seconds=60):
    response = client.post(
        "/api/jobs",
        json={
            "image": "python:3.12-slim",
            "argv": ["python", "-u", "-c", code],
            "resources": {"cpu": 0.5, "memory_mib": 64},
            "time_limit_s": seconds,
        },
    )
    response.raise_for_status()
    return response.json()


def log_text(client, job):
    chunks, after = [], -1
    while True:
        page = client.get(f"/api/jobs/{job['id']}/logs", params={"after": after}).json()
        chunks += page["chunks"]
        after = page["next_seq"]
        if len(page["chunks"]) < 64:
            return b"".join(base64.b64decode(c["data_base64"]) for c in chunks).decode()


def job_container(job):
    with closing(docker.from_env()) as engine:
        found = engine.containers.list(
            all=True, filters={"label": f"labflux.attempt_id={job['current_attempt_id']}"}
        )
        return found[0] if found else None


def settled(client, job):
    value = client.get(f"/api/jobs/{job['id']}").json()
    return value if value["state"] in {"SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"} else None


def no_containers_left():
    with closing(docker.from_env()) as engine:
        return not engine.containers.list(
            all=True, filters={"label": f"labflux.worker_id={WORKER_ID}"}
        )


COUNTER = "import time\nfor i in range(25):\n print(f'line {i}')\n time.sleep(0.2)\n"


def test_restarted_worker_adopts_a_running_container_without_losing_or_repeating_logs(
    tmp_path,
):
    with lab(tmp_path) as (client, start_worker):
        first = start_worker()
        wait_for(lambda: client.get("/api/nodes").json())
        job = submit(client, COUNTER)
        wait_for(lambda: "line 3" in log_text(client, job))
        stop(*first)
        container = job_container(job)
        assert container is not None and container.status == "running"
        time.sleep(1)  # Output written while no Worker runs must still arrive.
        start_worker()
        done = wait_for(lambda: settled(client, job), timeout=60)
        assert done["state"] == "SUCCEEDED", done
        assert len(done["attempts"]) == 1 and done["attempts"][0]["exit_code"] == 0
        assert log_text(client, job).splitlines() == [f"line {i}" for i in range(25)]
        assert client.get("/api/nodes").json()[0]["allocated"]["jobs"] == 0
        assert no_containers_left()


def test_restarted_worker_reports_the_real_result_of_a_container_that_exited_meanwhile(
    tmp_path,
):
    with lab(tmp_path) as (client, start_worker):
        first = start_worker()
        wait_for(lambda: client.get("/api/nodes").json())
        code = "import time, sys\nprint('one')\ntime.sleep(1)\nprint('two')\nsys.exit(3)\n"
        job = submit(client, code)
        wait_for(lambda: "one" in log_text(client, job))
        stop(*first)
        wait_for(lambda: job_container(job).status == "exited")
        start_worker()
        done = wait_for(lambda: settled(client, job), timeout=60)
        assert done["state"] == "FAILED" and done["attempts"][0]["exit_code"] == 3, done
        assert log_text(client, job).splitlines() == ["one", "two"]
        assert client.get("/api/nodes").json()[0]["allocated"]["jobs"] == 0
        assert no_containers_left()


def test_container_removed_while_the_worker_was_down_is_closed_and_released(tmp_path):
    with lab(tmp_path) as (client, start_worker):
        first = start_worker()
        wait_for(lambda: client.get("/api/nodes").json())
        job = submit(client, "import time\nprint('up')\ntime.sleep(30)\n")
        wait_for(lambda: "up" in log_text(client, job))
        stop(*first)
        job_container(job).remove(force=True)
        start_worker()
        done = wait_for(lambda: settled(client, job), timeout=30)
        assert done["state"] == "FAILED", done
        assert done["reason"] == "container missing after worker restart"
        node = wait_for(lambda: client.get("/api/nodes").json()[0])
        assert node["status"] == "ONLINE" and node["allocated"]["jobs"] == 0
        follow_up = submit(client, "print('after')")
        assert wait_for(lambda: settled(client, follow_up))["state"] == "SUCCEEDED"
        assert no_containers_left()
