import base64
import socket
import threading
import time

import docker
import httpx
import pytest
import uvicorn

from labflux.api import create_app
from labflux.archives import extract_archive, pack_directory
from labflux.config import Settings
from labflux.runtime import DockerExecutor
from labflux.worker import Worker

pytestmark = pytest.mark.docker


def wait_for(predicate, timeout=45):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("end-to-end condition timed out")


def test_real_http_worker_source_logs_readonly_dataset_and_output(tmp_path):
    settings = Settings(data_dir=tmp_path / "scheduler", scheduler_interval_s=0.05)
    try:
        DockerExecutor(settings).check()
    except (docker.errors.DockerException, OSError, RuntimeError) as exc:
        pytest.skip(f"Docker unavailable: {exc}")
    app = create_app(settings)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    address = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    wait_for(lambda: server.started)
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "message.txt").write_text("shared input")
    worker = Worker(
        Settings(
            data_dir=tmp_path / "worker",
            scheduler_url=address,
            worker_id="e2e",
            allocatable_cpu=1,
            allocatable_memory_mib=256,
            heartbeat_interval_s=0.2,
            poll_interval_s=0.05,
            datasets={"demo": dataset},
        )
    )
    worker_thread = threading.Thread(target=worker.run, daemon=True)
    worker_thread.start()
    try:
        with httpx.Client(
            base_url=address, headers={"Authorization": "Bearer dev-user-token"}, trust_env=False
        ) as client:
            wait_for(lambda: client.get("/api/nodes").json())
            source = tmp_path / "source"
            source.mkdir()
            (source / "main.py").write_text(
                "from pathlib import Path\nimport time\n"
                "p=Path('/datasets/demo/message.txt')\n"
                "assert p.read_text() == 'shared input'\n"
                "try:\n p.write_text('changed')\nexcept OSError:\n print('readonly', flush=True)\n"
                "else:\n raise AssertionError('dataset was writable')\n"
                "print('live output',flush=True)\ntime.sleep(2)\n"
                "Path('/outputs/result.txt').write_text('actual container result')\n"
            )
            archive = tmp_path / "source.tar.gz"
            pack_directory(source, archive)
            uploaded = client.post("/api/artifacts/sources", content=archive.read_bytes())
            uploaded.raise_for_status()
            response = client.post(
                "/api/jobs",
                json={
                    "image": "python:3.12-slim",
                    "argv": ["python", "main.py"],
                    "resources": {"cpu": 1, "memory_mib": 64},
                    "time_limit_s": 20,
                    "source_id": uploaded.json()["id"],
                    "datasets": ["demo"],
                },
            )
            response.raise_for_status()
            job = response.json()
            logs = wait_for(lambda: client.get(f"/api/jobs/{job['id']}/logs").json()["chunks"])
            assert b"live output" in b"".join(base64.b64decode(c["data_base64"]) for c in logs)
            assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "RUNNING"
            # Re-delivery while Docker is still executing must preserve this one attempt.
            attempt_id = job["current_attempt_id"]
            command = {
                "kind": "START",
                "payload": {
                    "job_id": job["id"],
                    "attempt_id": attempt_id,
                    "worker_id": "e2e",
                    "job": job["spec"],
                },
            }
            for _ in range(5):
                worker.accept(command)
            completed = wait_for(
                lambda: (
                    value
                    if (value := client.get(f"/api/jobs/{job['id']}").json())["state"]
                    in {"SUCCEEDED", "FAILED", "UNKNOWN"}
                    else None
                )
            )
            assert completed["state"] == "SUCCEEDED", completed
            assert len(completed["attempts"]) == 1
            output = completed["outputs"][0]
            archive.write_bytes(client.get(f"/api/artifacts/{output['id']}").content)
            extract_archive(archive, tmp_path / "downloaded", max_bytes=1024, max_members=10)
            assert (tmp_path / "downloaded/result.txt").read_text() == "actual container result"
            assert (dataset / "message.txt").read_text() == "shared input"
            assert client.get("/api/nodes").json()[0]["allocated"]["jobs"] == 0
    finally:
        worker.stop_event.set()
        worker_thread.join(timeout=20)
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not worker_thread.is_alive()
        assert not thread.is_alive()
