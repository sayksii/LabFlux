import threading
import time
from concurrent.futures import ThreadPoolExecutor

from conftest import register, submit, user_headers

from labflux.config import Settings
from labflux.runtime import ExecutionResult
from labflux.worker import Worker


class ControlledExecutor:
    """A fake container: it lives in `containers` until it finishes, like Docker would keep it
    across a Worker restart."""

    def __init__(self, *, fail=False):
        self.calls = 0
        self.release = threading.Event()
        self.fail = fail
        self.containers: dict[str, dict] = {}
        self.adopted: list[tuple[str, dict | None]] = []
        self.removed: list[str] = []

    def run(self, spec, workspace, outputs, on_log, on_started, cancel, **options):
        self.calls += 1
        if self.fail:
            raise OSError("daemon is unavailable; state unknown")
        detach = options.get("detach")
        if options.get("adopt"):
            self.adopted.append((spec.attempt_id, options.get("skip")))
            if spec.attempt_id not in self.containers:
                return ExecutionResult(
                    "FAILED", None, "container missing after worker restart", True
                )
        else:
            if options.get("on_launch"):
                options["on_launch"]()
            self.containers[spec.attempt_id] = {"worker_id": spec.worker_id, "status": "running"}
            on_log("stdout", b"live output\n")
        on_started()
        while not self.release.wait(0.01):
            if cancel.is_set():
                self.containers.pop(spec.attempt_id, None)
                return ExecutionResult("CANCELLED", 137, "cancelled", True)
            if detach is not None and detach.is_set():
                return None
        self.containers.pop(spec.attempt_id, None)
        return ExecutionResult("SUCCEEDED", 0, "completed", True)

    def check(self):
        return {}

    def stop_unstarted(self, spec):
        self.containers.pop(spec.attempt_id, None)
        return ExecutionResult("CANCELLED", 137, "confirmed stopped", True)

    def inventory(self, worker_id):
        return [
            {"attempt_id": attempt_id, "worker_id": value["worker_id"], "status": value["status"]}
            for attempt_id, value in self.containers.items()
            if value["worker_id"] == worker_id
        ]

    def remove(self, worker_id, attempt_id):
        self.removed.append(attempt_id)
        self.containers.pop(attempt_id, None)
        return True


def prepare(client, tmp_path, executor):
    instance = register(client, cpu=1)
    worker = Worker(Settings(data_dir=tmp_path / "worker", worker_id="vm-a"), executor=executor)
    worker.instance_id = instance
    worker.client.close()
    worker.client = client
    worker.client.headers["Authorization"] = "Bearer dev-worker-token"
    job = submit(client)
    command = client.app.state.scheduler.poll_commands("vm-a", instance)[0]
    return worker, job, command


def test_replayed_start_runs_once_and_durable_result_blocks_rerun(client, tmp_path):
    executor = ControlledExecutor()
    worker, job, command = prepare(client, tmp_path, executor)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker.accept, [command] * 20))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not worker._read(command["attempt_id"])["started"]:
            time.sleep(0.01)
        worker.flush()
        running = client.get(f"/api/jobs/{job['id']}", headers=user_headers()).json()
        assert running["state"] == "RUNNING"
        assert client.get(f"/api/jobs/{job['id']}/logs", headers=user_headers()).json()["chunks"]
        executor.release.set()
        worker.handles[command["attempt_id"]]["future"].result(timeout=5)
        worker.flush()
        worker.accept(command)
        worker.handles.clear()
        worker.accept(command)  # The disk record is sufficient after in-memory handle eviction.
        assert executor.calls == 1
        assert (
            client.get(f"/api/jobs/{job['id']}", headers=user_headers()).json()["state"]
            == "SUCCEEDED"
        )
    finally:
        executor.release.set()
        worker.pool.shutdown(wait=True)


def test_unexpected_runtime_error_does_not_claim_termination(client, tmp_path):
    executor = ControlledExecutor(fail=True)
    worker, job, command = prepare(client, tmp_path, executor)
    try:
        worker.accept(command)
        worker.handles[command["attempt_id"]]["future"].result(timeout=5)
        worker.flush()
        assert (
            client.get(f"/api/jobs/{job['id']}", headers=user_headers()).json()["state"]
            == "UNKNOWN"
        )
        assert client.get("/api/nodes", headers=user_headers()).json()[0]["allocated"]["cpu"] == 1
    finally:
        worker.pool.shutdown(wait=True)


def test_cancel_can_confirm_an_earlier_unknown_execution(client, tmp_path):
    executor = ControlledExecutor(fail=True)
    worker, job, command = prepare(client, tmp_path, executor)
    try:
        worker.accept(command)
        worker.handles[command["attempt_id"]]["future"].result(timeout=5)
        worker.flush()
        assert (
            client.get(f"/api/jobs/{job['id']}", headers=user_headers()).json()["state"]
            == "UNKNOWN"
        )
        client.post(f"/api/jobs/{job['id']}/cancel", headers=user_headers())
        stop = client.app.state.scheduler.poll_commands("vm-a", worker.instance_id)[0]
        assert stop["kind"] == "STOP"
        worker.accept(stop)
        worker.handles[command["attempt_id"]]["future"].result(timeout=5)
        worker.flush()
        assert (
            client.get(f"/api/jobs/{job['id']}", headers=user_headers()).json()["state"]
            == "CANCELLED"
        )
        assert client.get("/api/nodes", headers=user_headers()).json()[0]["allocated"]["cpu"] == 0
        assert executor.calls == 1
    finally:
        worker.pool.shutdown(wait=True)
