"""M9 fast lane end to end on real Docker (CPU only): a real Scheduler (uvicorn), a real Worker
with DockerExecutor, and the real `labflux` CLI run as a subprocess in a temporary project."""

import os
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import docker
import httpx
import pytest
import uvicorn

from labflux.api import create_app
from labflux.config import Settings
from labflux.runtime import DockerExecutor
from labflux.worker import Worker

pytestmark = pytest.mark.docker

IMAGE = "python:3.12-slim"
LABFLUX = Path(sys.executable).parent / "labflux"
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


def wait_for(predicate, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("end-to-end condition timed out")


class Lane:
    """One Scheduler, one Worker and a project directory for driving the CLI."""

    def __init__(self, tmp_path: Path, address: str, worker_id: str):
        self.address = address
        self.worker_id = worker_id
        self.project = tmp_path / "project"
        self.config = tmp_path / "client.toml"
        self.config.write_text(f'scheduler_url = "{address}"\nuser_token = "dev-user-token"\n')
        self.env = {
            key: value for key, value in os.environ.items() if not key.startswith("LABFLUX_")
        }
        # The real `python` for local runs and stand-in fallbacks.
        self.env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"
        self.client = httpx.Client(
            base_url=address, headers={"Authorization": "Bearer dev-user-token"}, trust_env=False
        )

    def command(self, *args: str) -> list[str]:
        return [str(LABFLUX), "--config", str(self.config), *args]

    def run(self, *args: str, cwd: Path | None = None, timeout=90) -> subprocess.CompletedProcess:
        return subprocess.run(
            self.command(*args),
            cwd=cwd or self.project,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def popen(self, *args: str) -> subprocess.Popen:
        return subprocess.Popen(
            self.command(*args),
            cwd=self.project,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def current(self) -> dict | None:
        response = self.client.get("/api/sessions/current")
        return response.json() if response.status_code == 200 else None

    def job(self, job_id: str) -> dict:
        return self.client.get(f"/api/jobs/{job_id}").json()

    def containers(self) -> list:
        # Read only: this worker id is unique to the test.
        return docker.from_env().containers.list(
            all=True, filters={"label": f"labflux.worker_id={self.worker_id}"}
        )

    def wait_released(self, session_id: str) -> None:
        wait_for(lambda: self.job(session_id)["state"] in TERMINAL)
        wait_for(lambda: self.client.get("/api/nodes").json()[0]["allocated"]["jobs"] == 0)
        allocated = self.client.get("/api/nodes").json()[0]["allocated"]
        assert allocated["cpu"] == 0 and allocated["memory_mib"] == 0, allocated
        wait_for(lambda: not self.containers(), timeout=30)


@pytest.fixture
def lane(tmp_path):
    scheduler_settings = Settings(
        data_dir=tmp_path / "scheduler",
        scheduler_interval_s=0.05,
        session_command_timeout_s=3,
    )
    worker_id = f"fl-{uuid.uuid4().hex[:8]}"
    worker_settings = Settings(
        data_dir=tmp_path / "worker",
        worker_id=worker_id,
        allocatable_cpu=1,
        allocatable_memory_mib=512,
        heartbeat_interval_s=0.2,
        poll_interval_s=0.05,
        stop_grace_s=1,
    )
    executor = DockerExecutor(worker_settings)
    try:
        executor.check()
        docker.from_env().images.get(IMAGE)
    except (docker.errors.DockerException, OSError, RuntimeError) as exc:
        pytest.skip(f"Docker or {IMAGE} unavailable: {exc}")
    app = create_app(scheduler_settings)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    address = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    wait_for(lambda: server.started)
    worker = Worker(worker_settings.model_copy(update={"scheduler_url": address}))
    worker_thread = threading.Thread(target=worker.run, daemon=True)
    worker_thread.start()
    lane = Lane(tmp_path, address, worker_id)
    lane.project.mkdir()
    (lane.project / ".labflux.toml").write_text(
        f'[session]\ncpu = 1\nmemory_mib = 256\nimage = "{IMAGE}"\n'
    )
    try:
        wait_for(lambda: lane.client.get("/api/nodes").json())
        yield lane
    finally:
        attempts = [
            attempt["id"]
            for job in lane.client.get("/api/jobs").json()
            for attempt in lane.job(job["id"])["attempts"]
        ]
        lane.client.close()
        worker.stop_event.set()
        worker_thread.join(timeout=20)
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        for attempt_id in attempts:
            executor.remove(worker_id, attempt_id)
        assert not worker_thread.is_alive()
        assert not thread.is_alive()


def test_fast_lane_commands_sync_files_and_end_sessions(lane, tmp_path):
    project = lane.project
    (project / "old.py").write_text("print('old')\n")
    (project / "results.json").write_text('{"local": 0}\n')

    # A routed command streams output and returns its exact exit code; written files come back.
    first = lane.run(
        "run",
        "--",
        "python",
        "-c",
        "import pathlib, sys; pathlib.Path('/tmp/marker').write_text('same container'); "
        "pathlib.Path('gen.txt').write_text('generated'); "
        "pathlib.Path('results.json').write_text('{\"remote\": 1}'); print('hi'); sys.exit(3)",
    )
    assert first.returncode == 3, first.stderr
    assert first.stdout == "hi\n"
    assert (project / "gen.txt").read_text() == "generated"
    assert (project / "results.json").read_text() == '{"remote": 1}'
    session = lane.current()
    assert session["state"] == "RUNNING"
    attempt_id = lane.job(session["id"])["current_attempt_id"]
    assert len(lane.containers()) == 1

    # The second command reuses the same session and container (D12: /tmp outlives a command).
    second = lane.run(
        "run",
        "--",
        "python",
        "-c",
        "import pathlib; print(pathlib.Path('/tmp/marker').read_text())",
    )
    assert second.returncode == 0, second.stderr
    assert second.stdout == "same container\n"
    assert lane.current()["id"] == session["id"]
    assert lane.job(session["id"])["current_attempt_id"] == attempt_id

    # A file deleted locally disappears remotely on the next command (D4b).
    (project / "old.py").unlink()
    gone = lane.run(
        "run",
        "--",
        "python",
        "-c",
        "import os, sys; sys.exit(5 if os.path.exists('old.py') else 0)",
    )
    assert gone.returncode == 0, gone.stderr
    assert not (project / "old.py").exists()

    # A file edited locally during the command is kept; the remote copy becomes .remote.
    process = lane.popen(
        "run",
        "--",
        "python",
        "-c",
        "import pathlib, time; print('go', flush=True); time.sleep(1.5); "
        "pathlib.Path('results.json').write_text('{\"remote\": 2}')",
    )
    assert process.stdout.readline() == "go\n"
    (project / "results.json").write_text('{"local": 2}')
    stdout, stderr = process.communicate(timeout=60)
    assert process.returncode == 0, stderr
    assert (project / "results.json").read_text() == '{"local": 2}'
    assert (project / "results.json.remote").read_text() == '{"remote": 2}'
    assert "results.json changed locally during the command" in stderr

    # Stand-ins: inside the project python runs remotely; outside it, and via `local`, locally.
    script = "import os; print(os.getcwd())"
    remote = lane.run("_standin", "python", "--", "-c", script)
    assert (remote.returncode, remote.stdout) == (0, "/workspace\n"), remote.stderr
    outside = tmp_path / "outside"
    outside.mkdir()
    local = lane.run("_standin", "python", "--", "-c", script, cwd=outside)
    assert (local.returncode, local.stdout) == (0, f"{outside}\n"), local.stderr
    local = lane.run("local", "--", "python", "-c", script)
    assert (local.returncode, local.stdout) == (0, f"{project}\n"), local.stderr
    assert lane.current()["id"] == session["id"]

    # Ctrl-C stops only the command; the same session container runs the next one.
    process = lane.popen(
        "run", "--", "python", "-c", "import time; print('started', flush=True); time.sleep(30)"
    )
    assert process.stdout.readline() == "started\n"
    process.send_signal(signal.SIGINT)
    _, stderr = process.communicate(timeout=60)
    assert process.returncode == 130, stderr
    again = lane.run(
        "run",
        "--",
        "python",
        "-c",
        "import pathlib; print(pathlib.Path('/tmp/marker').read_text())",
    )
    assert (again.returncode, again.stdout) == (0, "same container\n"), again.stderr
    assert lane.current()["id"] == session["id"]
    assert lane.job(session["id"])["current_attempt_id"] == attempt_id

    # Over the command time limit (3 s here): exit 124, the container is terminated and its
    # resources are freed.
    started = time.monotonic()
    slow = lane.run("run", "--", "python", "-c", "import time; time.sleep(30)")
    assert slow.returncode == 124, slow.stderr
    assert time.monotonic() - started < 25
    assert "command time limit exceeded" in slow.stderr
    lane.wait_released(session["id"])
    assert lane.current() is None

    # The next command leases a new session.
    fresh = lane.run("run", "--", "python", "-c", "print('new')")
    assert (fresh.returncode, fresh.stdout) == (0, "new\n"), fresh.stderr
    jobs = [job["id"] for job in lane.client.get("/api/jobs").json()]
    assert len(jobs) == 2 and session["id"] in jobs
    assert lane.run("session", "close").returncode == 0
    lane.wait_released(next(job for job in jobs if job != session["id"]))
    assert lane.current() is None


def test_fast_lane_latency_with_a_running_session(lane):
    # About 1.4 MB of source-like files, like the repo the < 1 s target was set for.
    source = lane.project / "src"
    source.mkdir()
    for index in range(20):
        (source / f"module_{index}.py").write_text(
            "\n".join(f"VALUE_{n} = '{os.urandom(16).hex()}'" for n in range(1600)) + "\n"
        )
    size = sum(path.stat().st_size for path in lane.project.rglob("*") if path.is_file())
    assert 1_200_000 < size < 1_800_000
    warm = lane.run("run", "--", "python", "-c", "pass")
    assert warm.returncode == 0, warm.stderr

    def timed(argv, **kwargs):
        started = time.perf_counter()
        completed = subprocess.run(argv, env=lane.env, capture_output=True, timeout=60, **kwargs)
        assert completed.returncode == 0, completed.stderr
        return time.perf_counter() - started

    remote = [
        timed(lane.command("run", "--", "python", "-c", "pass"), cwd=lane.project)
        for _ in range(10)
    ]
    local = [timed([sys.executable, "-c", "pass"]) for _ in range(10)]
    extra = statistics.median(remote) - statistics.median(local)
    print(
        f"\nfast lane latency, project {size / 1e6:.2f} MB: remote median "
        f"{statistics.median(remote):.3f}s max {max(remote):.3f}s; local median "
        f"{statistics.median(local):.3f}s; extra {extra:.3f}s (target < 1 s)"
    )
    assert extra < 2
