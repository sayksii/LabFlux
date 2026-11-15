"""M11 with real HTTP, a Worker thread and Docker containers: under the priority policy a
higher-priority job preempts a running preemptible container, the preempted attempt's
/outputs stays downloadable, the higher job runs, and the preempted job runs again and
succeeds."""

import threading
import uuid
from contextlib import closing, contextmanager

import docker
import httpx
import pytest
from test_fault_docker import SchedulerProcess
from test_reconnect_docker import log_text, settled, wait_for

from labflux.archives import extract_archive
from labflux.config import Settings
from labflux.runtime import DockerExecutor
from labflux.worker import Worker

pytestmark = pytest.mark.docker

# Writes its partial result first, then works long enough to still be running when the
# higher-priority job arrives; a rerun repeats the same work and exits 0.
LOW = (
    "from pathlib import Path\nimport time\n"
    "Path('/outputs/progress.txt').write_text('partial result')\n"
    "print('ready')\ntime.sleep(4)\nprint('done')\n"
)
HIGH = (
    "from pathlib import Path\nimport time\n"
    "print('high running')\ntime.sleep(1)\n"
    "Path('/outputs/high.txt').write_text('high result')\n"
)


@contextmanager
def lab(tmp_path, **worker_settings):
    """A priority-policy Scheduler and one Worker whose whole capacity is one CPU, unless
    worker_settings say otherwise."""
    settings = Settings(
        data_dir=tmp_path / "scheduler",
        scheduler_interval_s=0.05,
        scheduling_policy="priority",
    )
    try:
        DockerExecutor(settings).check()
    except (docker.errors.DockerException, OSError, RuntimeError) as exc:
        pytest.skip(f"Docker unavailable: {exc}")
    worker_id = f"preempt-e2e-{uuid.uuid4().hex[:8]}"
    scheduler = SchedulerProcess(settings)
    scheduler.start()
    worker_settings = {"allocatable_cpu": 1, "allocatable_memory_mib": 256, **worker_settings}
    worker = Worker(
        Settings(
            data_dir=tmp_path / "worker",
            scheduler_url=scheduler.address,
            worker_id=worker_id,
            heartbeat_interval_s=0.2,
            poll_interval_s=0.05,
            **worker_settings,
        )
    )
    worker_thread = threading.Thread(target=worker.run, daemon=True)
    worker_thread.start()

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
            yield client, containers
    finally:
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
            # Each job needs the Worker's only CPU, so the two can never run together.
            "resources": {"cpu": 1, "memory_mib": 64},
            "time_limit_s": 60,
            **fields,
        },
    )
    response.raise_for_status()
    return response.json()


def job(client, value):
    return client.get(f"/api/jobs/{value['id']}").json()


def output_file(client, value, attempt_id, name, tmp_path):
    """Download the output archive of one attempt and return the named file's text."""
    [output] = [o for o in value["outputs"] if o["attempt_id"] == attempt_id]
    archive = tmp_path / f"{attempt_id}.tar.gz"
    archive.write_bytes(client.get(f"/api/artifacts/{output['id']}").content)
    target = tmp_path / f"out-{attempt_id}"
    extract_archive(archive, target, max_bytes=1024, max_members=10)
    return (target / name).read_text()


def test_higher_priority_job_preempts_a_real_container_which_then_reruns(tmp_path):
    with lab(tmp_path) as (client, containers):
        wait_for(lambda: client.get("/api/nodes").json())
        low = submit(client, LOW, priority=1, preemptible=True, safe_to_rerun=True)
        wait_for(lambda: "ready" in log_text(client, low))
        first = job(client, low)["current_attempt_id"]
        assert job(client, low)["state"] == "RUNNING"
        high = submit(client, HIGH, priority=8)

        # The STOP took effect: the attempt is PREEMPTED and the job queued again, while the
        # higher job holds the only CPU.
        preempted = wait_for(
            lambda: (value := job(client, low))["attempts"][0]["state"] == "PREEMPTED" and value
        )
        assert preempted["state"] == "QUEUED", preempted
        assert preempted["reason"] == (
            f"preempted by job {high['id'][:8]}; queued to run again (preemption 1 of 3)"
        )
        [attempt] = preempted["attempts"]
        assert attempt["id"] == first and attempt["preempted_by"] == high["id"]
        assert attempt["reason"].startswith(f"preempted by job {high['id'][:8]} (")
        # Stopped by the STOP (SIGTERM), not finished on its own.
        assert attempt["released"] and attempt["exit_code"] != 0

        # The file written before the stop can be fetched for the preempted attempt.
        with_output = wait_for(lambda: (value := job(client, low))["outputs"] and value)
        assert output_file(client, with_output, first, "progress.txt", tmp_path) == (
            "partial result"
        )

        done_high = wait_for(lambda: settled(client, high), timeout=60)
        assert done_high["state"] == "SUCCEEDED", done_high
        [high_attempt] = done_high["attempts"]
        # Dispatched only after the preempted container's termination was confirmed.
        assert high_attempt["created_at"] >= attempt["finished_at"]
        assert output_file(client, done_high, high_attempt["id"], "high.txt", tmp_path) == (
            "high result"
        )

        done_low = wait_for(lambda: settled(client, low), timeout=60)
        assert done_low["state"] == "SUCCEEDED", done_low
        assert [a["state"] for a in done_low["attempts"]] == ["PREEMPTED", "SUCCEEDED"]
        rerun = done_low["attempts"][1]
        assert rerun["id"] != first and rerun["exit_code"] == 0
        assert rerun["preempted_by"] is None
        assert rerun["created_at"] >= high_attempt["finished_at"]
        assert output_file(client, done_low, rerun["id"], "progress.txt", tmp_path) == (
            "partial result"
        )
        assert "done" in log_text(client, low)

        assert client.get("/api/nodes").json()[0]["allocated"]["jobs"] == 0
        wait_for(lambda: not containers())
