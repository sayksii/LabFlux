"""M6.7 with real HTTP, Worker threads and Docker containers: a network partition longer than
the offline timeout, a machine going down together with its containers, and a Scheduler
restart over the same data directory while a job keeps running."""

import socket
import threading
import time
import uuid
from contextlib import closing, contextmanager

import docker
import httpx
import pytest
import uvicorn
from test_reconnect_docker import log_text, settled, stop, wait_for

from labflux.api import create_app
from labflux.config import Settings
from labflux.runtime import DockerExecutor
from labflux.worker import Worker

pytestmark = pytest.mark.docker
WORKER_TOKEN = "Bearer dev-worker-token"


class SchedulerProcess:
    """A real Scheduler on a fixed port that can be stopped and started again."""

    def __init__(self, settings: Settings):
        self.settings = settings
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.port = probe.getsockname()[1]
        probe.close()
        self.address = f"http://127.0.0.1:{self.port}"
        self.server = None

    def start(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", self.port))
        self.sock.listen()
        self.server = uvicorn.Server(uvicorn.Config(create_app(self.settings), log_level="error"))
        self.thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True
        )
        self.thread.start()
        wait_for(lambda: self.server.started)

    def stop(self):
        if self.server is None:
            return
        self.server.should_exit = True
        self.thread.join(timeout=15)
        self.sock.close()
        self.server = None


@contextmanager
def lab(tmp_path, monkeypatch):
    """Scheduler, Worker factory and a switch that cuts the Workers' network link."""
    settings = Settings(
        data_dir=tmp_path / "scheduler",
        scheduler_interval_s=0.05,
        heartbeat_interval_s=0.2,
        offline_timeout_s=1,
    )
    try:
        DockerExecutor(settings).check()
    except (docker.errors.DockerException, OSError, RuntimeError) as exc:
        pytest.skip(f"Docker unavailable: {exc}")
    worker_id = f"fault-e2e-{uuid.uuid4().hex[:8]}"
    cut = threading.Event()
    real = httpx.HTTPTransport.handle_request

    def gated(transport, request):
        # Only Worker requests carry the worker token; the test's own client stays connected.
        if cut.is_set() and request.headers.get("authorization") == WORKER_TOKEN:
            raise httpx.ConnectError("network is unreachable", request=request)
        return real(transport, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", gated)
    scheduler = SchedulerProcess(settings)
    scheduler.start()
    running = []

    def start_worker():
        worker = Worker(
            Settings(
                data_dir=tmp_path / "worker",
                scheduler_url=scheduler.address,
                worker_id=worker_id,
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

    def containers():
        with closing(docker.from_env()) as engine:
            return engine.containers.list(
                all=True, filters={"label": f"labflux.worker_id={worker_id}"}
            )

    try:
        with httpx.Client(
            base_url=scheduler.address,
            headers={"Authorization": "Bearer dev-user-token"},
            trust_env=False,
        ) as client:
            yield client, start_worker, scheduler, cut, containers
    finally:
        cut.clear()
        for worker, worker_thread in running:
            worker.stop_event.set()
            worker_thread.join(timeout=20)
        for container in containers():  # Only this test's uniquely labelled containers.
            container.remove(force=True)
        scheduler.stop()


def submit(client, code, **fields):
    response = client.post(
        "/api/jobs",
        json={
            "image": "python:3.12-slim",
            "argv": ["python", "-u", "-c", code],
            "resources": {"cpu": 0.5, "memory_mib": 64},
            "time_limit_s": 60,
            **fields,
        },
    )
    response.raise_for_status()
    return response.json()


def job(client, value):
    return client.get(f"/api/jobs/{value['id']}").json()


def node(client):
    nodes = client.get("/api/nodes").json()
    return nodes[0] if nodes else None


def counter(lines):
    return f"import time\nfor i in range({lines}):\n print(f'line {{i}}')\n time.sleep(0.2)\n"


def test_partition_longer_than_the_offline_timeout_loses_no_work(tmp_path, monkeypatch):
    with lab(tmp_path, monkeypatch) as (client, start_worker, _, cut, containers):
        start_worker()
        wait_for(lambda: node(client))
        work = submit(client, counter(25))
        wait_for(lambda: "line 3" in log_text(client, work))
        cut.set()
        wait_for(lambda: node(client)["status"] == "OFFLINE", timeout=10)
        assert job(client, work)["state"] == "UNKNOWN"
        assert node(client)["allocated"]["jobs"] == 1
        time.sleep(1)  # Partitioned for well over the 1 s offline timeout.
        [container] = containers()
        container.reload()
        assert container.status == "running"
        cut.clear()
        done = wait_for(lambda: job(client, work)["state"] != "UNKNOWN" and settled(client, work))
        assert done["state"] == "SUCCEEDED", done
        assert len(done["attempts"]) == 1 and done["attempts"][0]["exit_code"] == 0
        assert log_text(client, work).splitlines() == [f"line {i}" for i in range(25)]
        assert node(client)["allocated"]["jobs"] == 0
        wait_for(lambda: not containers())


def test_machine_down_with_its_containers_retries_on_the_returning_worker(tmp_path, monkeypatch):
    with lab(tmp_path, monkeypatch) as (client, start_worker, _, _cut, containers):
        first = start_worker()
        wait_for(lambda: node(client))
        code = "import time\nprint('up')\ntime.sleep(2)\nprint('done')\n"
        work = submit(client, code, retries_on_node_failure=1, safe_to_rerun=True)
        wait_for(lambda: "up" in log_text(client, work))
        lost = job(client, work)["current_attempt_id"]
        # The machine goes down: the Worker process and its containers end together. Docker
        # keeps the killed container across the reboot, as it would for a real VM shutdown.
        stop(*first)
        [container] = containers()
        container.kill()
        container.reload()
        assert container.status == "exited"
        wait_for(lambda: node(client)["status"] == "OFFLINE", timeout=10)
        assert job(client, work)["state"] == "UNKNOWN"
        # The machine is back: the host booted after the container started.
        booted = time.time()
        monkeypatch.setattr("labflux.worker.psutil.boot_time", lambda: booted)
        start_worker()
        done = wait_for(lambda: len(job(client, work)["attempts"]) == 2 and settled(client, work))
        assert done["state"] == "SUCCEEDED", done
        assert [a["state"] for a in done["attempts"]] == ["FAILED", "SUCCEEDED"]
        assert done["attempts"][0]["id"] == lost
        assert done["attempts"][0]["reason"] == "container missing after worker restart"
        assert log_text(client, work).splitlines() == ["up", "done"]
        assert node(client)["allocated"]["jobs"] == 0
        wait_for(lambda: not containers())


def test_scheduler_restart_with_the_same_data_keeps_a_running_job(tmp_path, monkeypatch):
    with lab(tmp_path, monkeypatch) as (client, start_worker, scheduler, _cut, containers):
        start_worker()
        wait_for(lambda: node(client))
        work = submit(client, counter(40))
        wait_for(lambda: "line 3" in log_text(client, work))
        scheduler.stop()
        time.sleep(2)  # Down for twice the offline timeout; the job keeps printing.
        [container] = containers()
        container.reload()
        assert container.status == "running"
        scheduler.start()
        # Placed once the node is reconciled (the unit tests check that it waits for that).
        follow_up = submit(client, "print('after')")
        restored = wait_for(lambda: node(client)["status"] == "ONLINE" and node(client))
        assert restored["allocated"]["jobs"] >= 1
        value = job(client, work)
        # Never expired: the attempt still carries its dispatch reason, not a resumption.
        assert value["state"] == "RUNNING", value
        assert value["attempts"][0]["reason"] == "dispatched"
        done = wait_for(lambda: settled(client, work), timeout=60)
        assert done["state"] == "SUCCEEDED" and len(done["attempts"]) == 1, done
        assert log_text(client, work).splitlines() == [f"line {i}" for i in range(40)]
        assert wait_for(lambda: settled(client, follow_up))["state"] == "SUCCEEDED"
        assert node(client)["allocated"]["jobs"] == 0
        wait_for(lambda: not containers())
