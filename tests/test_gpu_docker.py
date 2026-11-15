import base64
import re
import socket
import threading
import uuid
from contextlib import closing

import docker
import httpx
import pytest
import uvicorn
from test_end_to_end_docker import wait_for
from test_preemption_docker import lab
from test_reconnect_docker import log_text, settled

from labflux.api import create_app
from labflux.config import Settings
from labflux.gpu import GpuProbe
from labflux.models import ExecutionSpec, JobSpec, ResourceRequest
from labflux.runtime import DockerExecutor
from labflux.worker import Worker

pytestmark = [pytest.mark.docker, pytest.mark.gpu]

IMAGE = "ubuntu:latest"
COUNT_DEVICES = "ls /dev | grep -cE '^nvidia[0-9]+$' || true"


@pytest.fixture
def idle_cards():
    """Pooled-card candidates that no one else is using; tests only run nvidia-smi -L."""
    import pynvml

    try:
        pynvml.nvmlInit()
    except pynvml.NVMLError as exc:
        pytest.skip(f"NVML unavailable: {exc}")
    try:
        count = pynvml.nvmlDeviceGetCount()
    finally:
        pynvml.nvmlShutdown()
    if not count:
        pytest.skip("no NVIDIA GPU")
    probe = GpuProbe(list(range(count)), 1024)
    try:
        busy = {o.uuid for o in probe.observe(set()) if o.external_use}
        return [device for device in probe.devices if device.uuid not in busy]
    finally:
        probe.close()


@pytest.fixture
def runtime(tmp_path):
    executor = DockerExecutor(Settings(data_dir=tmp_path, stop_grace_s=1))
    try:
        executor.check()
    except (docker.errors.DockerException, OSError, RuntimeError) as exc:
        pytest.skip(f"Docker unavailable: {exc}")
    return executor


def run(runtime, tmp_path, script, gpu_uuids=()):
    output = []
    spec = ExecutionSpec(
        job_id=uuid.uuid4().hex,
        attempt_id=uuid.uuid4().hex,
        worker_id="gpu-test",
        job=JobSpec(
            image=IMAGE,
            argv=["sh", "-c", script],
            resources=ResourceRequest(cpu=0.5, memory_mib=256, gpu=len(gpu_uuids)),
            time_limit_s=60,
        ),
        gpu_uuids=list(gpu_uuids),
    )
    result = runtime.run(
        spec,
        tmp_path / spec.attempt_id / "work",
        tmp_path / spec.attempt_id / "out",
        lambda stream, data: output.append((stream, data)),
        lambda: None,
        threading.Event(),
    )
    with closing(docker.from_env()) as client:
        leftover = client.containers.list(
            all=True, filters={"label": f"labflux.attempt_id={spec.attempt_id}"}
        )
    assert leftover == []
    stdout = b"".join(data for stream, data in output if stream == "stdout").decode()
    return result, stdout


def visible(text):
    return re.findall(r"UUID: (GPU-[0-9a-f-]+)", text)


def test_gpu_job_sees_only_its_assigned_card(runtime, tmp_path, idle_cards):
    if not idle_cards:
        pytest.skip("no idle GPU")
    for card in idle_cards:
        result, stdout = run(runtime, tmp_path, f"nvidia-smi -L; {COUNT_DEVICES}", [card.uuid])
        assert result.outcome == "SUCCEEDED", (result, stdout)
        assert visible(stdout) == [card.uuid]
        assert stdout.strip().splitlines()[-1] == "1"  # One /dev/nvidiaN node.


def test_cpu_job_sees_no_gpu(runtime, tmp_path, idle_cards):
    script = "ls /dev/nvidia* 2>/dev/null; command -v nvidia-smi; echo done"
    result, stdout = run(runtime, tmp_path, script)
    assert result.outcome == "SUCCEEDED"
    assert stdout == "done\n"


def test_scheduler_and_worker_place_concurrent_jobs_on_different_cards(tmp_path, idle_cards):
    if len(idle_cards) < 2:
        pytest.skip("needs two idle GPUs")
    cards = idle_cards[:2]
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
    worker = Worker(
        Settings(
            data_dir=tmp_path / "worker",
            scheduler_url=address,
            worker_id="gpu-e2e",
            allocatable_cpu=2,
            allocatable_memory_mib=1024,
            heartbeat_interval_s=0.2,
            poll_interval_s=0.05,
            gpus=[card.index for card in cards],
        )
    )
    worker_thread = threading.Thread(target=worker.run, daemon=True)
    worker_thread.start()
    try:
        with httpx.Client(
            base_url=address, headers={"Authorization": "Bearer dev-user-token"}, trust_env=False
        ) as client:
            node = wait_for(
                lambda: next(
                    (n for n in client.get("/api/nodes").json() if n["observed"].get("gpus")),
                    None,
                )
            )
            assert [g["uuid"] for g in node["capacity"]["gpus"]] == [c.uuid for c in cards]
            assert node["capacity"]["cuda_driver_version"]
            assert not any(g["external_use"] for g in node["observed"]["gpus"])
            jobs = []
            for _ in range(2):
                response = client.post(
                    "/api/jobs",
                    json={
                        "image": IMAGE,
                        "argv": ["sh", "-c", f"nvidia-smi -L; {COUNT_DEVICES}; sleep 3"],
                        "resources": {"cpu": 0.5, "memory_mib": 256, "gpu": 1},
                        "time_limit_s": 60,
                    },
                )
                response.raise_for_status()
                jobs.append(response.json())

            def finished(job):
                value = client.get(f"/api/jobs/{job['id']}").json()
                return value if value["state"] in {"SUCCEEDED", "FAILED", "UNKNOWN"} else None

            done = [wait_for(lambda job=job: finished(job), timeout=90) for job in jobs]
            assert [job["state"] for job in done] == ["SUCCEEDED", "SUCCEEDED"], done
            attempts = [job["attempts"][-1] for job in done]
            assigned = [attempt["gpu_uuids"] for attempt in attempts]
            assert sorted(u for uuids in assigned for u in uuids) == sorted(c.uuid for c in cards)
            first, second = attempts
            assert first["started_at"] < second["finished_at"]
            assert second["started_at"] < first["finished_at"]  # They ran at the same time.
            for job, uuids in zip(done, assigned):
                chunks = client.get(f"/api/jobs/{job['id']}/logs").json()["chunks"]
                text = b"".join(base64.b64decode(c["data_base64"]) for c in chunks).decode()
                assert visible(text) == uuids
                assert text.strip().splitlines()[-1] == "1"
            node = client.get("/api/nodes").json()[0]
            assert node["allocated"]["gpu"] == 0
    finally:
        worker.stop_event.set()
        worker_thread.join(timeout=20)
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not worker_thread.is_alive()
        assert not thread.is_alive()


def test_higher_priority_gpu_job_preempts_the_card_and_the_victim_reruns_on_it(
    tmp_path, idle_cards
):
    """M11 on a real card. The Worker pools exactly one idle card; a preemptible GPU job holds
    it, a higher-priority GPU job preempts it and gets the same card, then the preempted job
    runs again on that card and succeeds. The containers only list the card and sleep."""
    if not idle_cards:
        pytest.skip("no idle GPU")
    card = idle_cards[0]
    # Two CPUs for two half-CPU jobs: only the card keeps them from running together.
    worker = {"allocatable_cpu": 2, "allocatable_memory_mib": 1024, "gpus": [card.index]}
    with lab(tmp_path, stop_grace_s=1, **worker) as (client, containers):
        node = wait_for(
            lambda: next(
                (n for n in client.get("/api/nodes").json() if n["observed"].get("gpus")), None
            )
        )
        assert [g["uuid"] for g in node["capacity"]["gpus"]] == [card.uuid]

        def submit(script, **fields):
            response = client.post(
                "/api/jobs",
                json={
                    "image": IMAGE,
                    "argv": ["sh", "-c", script],
                    "resources": {"cpu": 0.5, "memory_mib": 256, "gpu": 1},
                    "time_limit_s": 60,
                    **fields,
                },
            )
            response.raise_for_status()
            return response.json()

        def attempt_log(value, attempt_id):
            params = {"after": -1, "attempt_id": attempt_id}
            chunks = client.get(f"/api/jobs/{value['id']}/logs", params=params).json()["chunks"]
            return b"".join(base64.b64decode(c["data_base64"]) for c in chunks).decode()

        # Long enough to still be running when the higher job arrives; the rerun repeats it.
        low = submit(
            "nvidia-smi -L; echo ready; sleep 8; echo done",
            priority=1,
            preemptible=True,
            safe_to_rerun=True,
        )
        wait_for(lambda: "ready" in log_text(client, low), timeout=60)
        first = client.get(f"/api/jobs/{low['id']}").json()["current_attempt_id"]
        high = submit("nvidia-smi -L; sleep 1", priority=8)

        done_high = wait_for(lambda: settled(client, high), timeout=90)
        assert done_high["state"] == "SUCCEEDED", done_high
        [high_attempt] = done_high["attempts"]
        assert high_attempt["gpu_uuids"] == [card.uuid]
        assert visible(attempt_log(high, high_attempt["id"])) == [card.uuid]

        done_low = wait_for(lambda: settled(client, low), timeout=90)
        assert done_low["state"] == "SUCCEEDED", done_low
        preempted, rerun = done_low["attempts"]
        assert preempted["id"] == first and preempted["state"] == "PREEMPTED"
        assert preempted["preempted_by"] == high["id"]
        assert preempted["gpu_uuids"] == rerun["gpu_uuids"] == [card.uuid]
        # The card changed hands only after each container's termination was confirmed.
        assert high_attempt["created_at"] >= preempted["finished_at"]
        assert rerun["created_at"] >= high_attempt["finished_at"]
        assert "done" not in attempt_log(low, first)
        rerun_log = attempt_log(low, rerun["id"])
        assert visible(rerun_log) == [card.uuid] and rerun_log.endswith("done\n")
        assert client.get("/api/nodes").json()[0]["allocated"]["gpu"] == 0
        wait_for(lambda: not containers())
