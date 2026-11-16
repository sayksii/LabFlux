"""Architecture 6.1 with real HTTP, a Worker thread and Docker containers: the second run of a
project on the same worker continues from the checkpoint the first run left in its project
directory, and the Scheduler lists both kept directories."""

import base64
import json
import socket
import threading
import uuid
from contextlib import closing

import docker
import httpx
import pytest
import uvicorn
from test_end_to_end_docker import wait_for
from typer.testing import CliRunner

import labflux.cli as cli
from labflux.api import create_app
from labflux.config import Settings
from labflux.runtime import DockerExecutor
from labflux.worker import Worker

pytestmark = pytest.mark.docker

# Appends one line per run to a checkpoint at a path relative to the project, as a training
# script that resumes would.
TRAIN = """from pathlib import Path
model = Path("ckpt/model.pt")
model.parent.mkdir(exist_ok=True)
previous = model.read_text() if model.exists() else ""
model.write_text(previous + "step\\n")
print(model.read_text(), end="")
"""


def log_text(client, job):
    chunks = client.get(f"/api/jobs/{job['id']}/logs").json()["chunks"]
    return b"".join(base64.b64decode(chunk["data_base64"]) for chunk in chunks).decode()


def test_the_next_run_of_a_project_reads_and_extends_its_checkpoint(tmp_path, monkeypatch):
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
    worker_id = f"results-e2e-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        Settings(
            data_dir=tmp_path / "worker",
            scheduler_url=address,
            worker_id=worker_id,
            allocatable_cpu=1,
            allocatable_memory_mib=256,
            heartbeat_interval_s=0.2,
            poll_interval_s=0.05,
        )
    )
    worker_thread = threading.Thread(target=worker.run, daemon=True)
    worker_thread.start()
    # The CLI's own logging setup would bind to the runner's temporary stderr.
    monkeypatch.setattr(cli, "configure_logging", lambda: None)
    environment = {
        "LABFLUX_SCHEDULER_URL": address,
        "LABFLUX_USER_TOKEN": "dev-user-token",
        "LABFLUX_CONFIG": None,
    }
    try:
        with httpx.Client(
            base_url=address, headers={"Authorization": "Bearer dev-user-token"}, trust_env=False
        ) as client:
            wait_for(lambda: client.get("/api/nodes").json())
            source = tmp_path / "trainer"
            source.mkdir()
            (source / "main.py").write_text(TRAIN)
            spec = tmp_path / "job.json"
            spec.write_text(
                json.dumps(
                    {
                        "image": "python:3.12-slim",
                        "argv": ["python", "main.py"],
                        "resources": {"cpu": 0.5, "memory_mib": 64},
                        "time_limit_s": 60,
                    }
                )
            )
            runs = []
            for _ in range(2):
                result = CliRunner().invoke(
                    cli.app,
                    [
                        "submit",
                        str(spec),
                        "--source",
                        str(source),
                        "--wait",
                        "--wait-timeout",
                        "60",
                    ],
                    env=environment,
                )
                assert result.exit_code == 0, result.output
                runs.append(json.loads(result.stdout))
            first, second = runs
            assert first["spec"]["project"] == second["spec"]["project"] == "trainer"
            assert log_text(client, first) == "step\n"
            assert log_text(client, second) == "step\nstep\n"
            assert f"starts from the files of job {first['id'][:8]}" in second["schedule_note"]
            kept = client.get("/api/results").json()
            assert [(row["project"], row["node_id"], row["job_id"]) for row in kept] == [
                ("trainer", worker_id, second["id"]),
                ("trainer", worker_id, first["id"]),
            ]
            assert all(row["size"] > 0 and row["outcome"] == "SUCCEEDED" for row in kept)
            root = tmp_path / "worker" / "workers" / worker_id
            # Each run has its own directory; the first one is unchanged by the second run.
            model = "workspace/ckpt/model.pt"
            assert (root / first["current_attempt_id"] / model).read_text() == "step\n"
            assert (root / second["current_attempt_id"] / model).read_text() == "step\nstep\n"
    finally:
        worker.stop_event.set()
        worker_thread.join(timeout=20)
        with closing(docker.from_env()) as engine:
            for container in engine.containers.list(
                all=True, filters={"label": f"labflux.worker_id={worker_id}"}
            ):
                container.remove(force=True)  # Only this test's uniquely labelled containers.
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
