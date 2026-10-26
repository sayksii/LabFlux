"""Docker execution with real kernel limits and conservative resource-release proof."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections.abc import Callable
from contextlib import closing, suppress
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import docker
from docker.errors import DockerException, ImageNotFound, NotFound
from docker.types import DeviceRequest

from labflux.config import Settings
from labflux.models import ExecutionSpec

log = logging.getLogger("labflux.runtime")


@dataclass
class ExecutionResult:
    outcome: str
    exit_code: int | None
    reason: str
    termination_confirmed: bool
    oom_killed: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def container_name(spec: ExecutionSpec) -> str:
    digest = hashlib.sha256(f"{spec.worker_id}:{spec.attempt_id}".encode()).hexdigest()
    return f"labflux-{digest[:40]}"


class DockerExecutor:
    def __init__(self, settings: Settings):
        self.settings = settings

    def check(self) -> dict:
        with closing(docker.from_env(timeout=self.settings.request_timeout_s)) as client:
            info = client.info()
        missing = [
            name for name in ("MemoryLimit", "SwapLimit", "CpuCfsQuota") if not info.get(name)
        ]
        if missing:
            raise RuntimeError("Docker cannot enforce required limits: " + ", ".join(missing))
        return {
            "server_version": info["ServerVersion"],
            "cgroup_version": info.get("CgroupVersion"),
        }

    def run(
        self,
        spec: ExecutionSpec,
        workspace: Path,
        outputs: Path,
        on_log: Callable[[str, bytes], None],
        on_started: Callable[[], None],
        cancel: threading.Event,
    ) -> ExecutionResult:
        client = docker.from_env(timeout=max(10, self.settings.stop_grace_s + 5))
        container = None
        reader = None
        stream = None
        reason = "completed"
        name = container_name(spec)
        context = {"job_id": spec.job_id, "attempt_id": spec.attempt_id}
        try:
            try:
                container = client.containers.get(name)
            except NotFound:
                if cancel.is_set():
                    return ExecutionResult("CANCELLED", None, "cancelled before start", True)
                try:
                    client.images.get(spec.job.image)
                except ImageNotFound:
                    with closing(
                        docker.from_env(timeout=self.settings.image_pull_timeout_s)
                    ) as puller:
                        puller.images.pull(spec.job.image)
                if cancel.is_set():
                    return ExecutionResult("CANCELLED", None, "cancelled before start", True)
                workspace.mkdir(parents=True, exist_ok=True)
                outputs.mkdir(parents=True, exist_ok=True)
                volumes = {
                    str(workspace.resolve()): {"bind": "/workspace", "mode": "rw"},
                    str(outputs.resolve()): {"bind": "/outputs", "mode": "rw"},
                }
                for dataset in spec.job.datasets:
                    path = self.settings.datasets.get(dataset)
                    if not path or not path.is_dir():
                        raise ValueError(f"dataset is unavailable: {dataset}")
                    volumes[str(path)] = {"bind": f"/datasets/{dataset}", "mode": "ro"}
                memory = spec.job.resources.memory_mib * 1024**2
                environment = {**spec.job.env, "PYTHONUNBUFFERED": "1"}
                devices = None
                if spec.gpu_uuids:
                    # Docker sets NVIDIA_VISIBLE_DEVICES to exactly these UUIDs.
                    devices = [DeviceRequest(device_ids=spec.gpu_uuids, capabilities=[["gpu"]])]
                else:
                    # Keeps CPU jobs GPU-less even if a GPU runtime were made the default.
                    environment["NVIDIA_VISIBLE_DEVICES"] = "void"
                container = client.containers.create(
                    spec.job.image,
                    command=spec.job.argv,
                    name=name,
                    detach=True,
                    working_dir="/workspace",
                    environment=environment,
                    device_requests=devices,
                    user=f"{os.getuid()}:{os.getgid()}",
                    volumes=volumes,
                    nano_cpus=round(spec.job.resources.cpu * 1_000_000_000),
                    mem_limit=memory,
                    memswap_limit=memory,
                    pids_limit=256,
                    cap_drop=["ALL"],
                    security_opt=["no-new-privileges:true"],
                    init=True,
                    labels={
                        "labflux.managed": "true",
                        "labflux.job_id": spec.job_id,
                        "labflux.attempt_id": spec.attempt_id,
                        "labflux.worker_id": spec.worker_id,
                    },
                )
            labels = container.labels
            if (
                labels.get("labflux.attempt_id") != spec.attempt_id
                or labels.get("labflux.worker_id") != spec.worker_id
            ):
                raise RuntimeError("container identity does not match the dispatched attempt")
            container.reload()
            if container.status == "created" and not cancel.is_set():
                # Attach before start: attaching afterwards with logs=True can lose or repeat
                # output written between the log replay and the live stream.
                stream = container.attach(stream=True, stdout=True, stderr=True, demux=True)
                container.start()
                container.reload()
            if container.status == "created":
                container.remove()
                return ExecutionResult("CANCELLED", None, "cancelled before start", True)
            on_started()
            if stream is None:
                stream = container.attach(
                    stream=True, stdout=True, stderr=True, logs=True, demux=True
                )

            def collect_logs() -> None:
                try:
                    for stdout, stderr in stream:
                        if stdout:
                            on_log("stdout", stdout)
                        if stderr:
                            on_log("stderr", stderr)
                except Exception:
                    log.exception("job log capture interrupted", extra=context)

            reader = threading.Thread(
                target=collect_logs, name=f"logs-{spec.attempt_id}", daemon=True
            )
            reader.start()
            started_at = container.attrs["State"].get("StartedAt")
            started_wall = datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp()
            remaining = (spec.job.time_limit_s or self.settings.job_time_limit_s) - max(
                0, time.time() - started_wall
            )
            deadline = time.monotonic() + max(0, remaining)
            while True:
                container.reload()
                if not container.attrs["State"]["Running"]:
                    break
                if cancel.is_set() or time.monotonic() >= deadline:
                    reason = "cancelled" if cancel.is_set() else "time limit exceeded"
                    self._stop(container)
                    break
                time.sleep(self.settings.runtime_poll_s)
            container.reload()
            if container.attrs["State"]["Running"]:
                return ExecutionResult("UNKNOWN", None, "termination could not be confirmed", False)
            if reader:
                reader.join(timeout=5)
            state = container.attrs["State"]
            exit_code = state.get("ExitCode")
            oom = bool(state.get("OOMKilled"))
            if oom:
                reason = "memory limit exceeded (OOM)"
            outcome = (
                "CANCELLED"
                if cancel.is_set()
                else ("SUCCEEDED" if exit_code == 0 and reason == "completed" else "FAILED")
            )
            result = ExecutionResult(outcome, exit_code, reason, True, oom)
            container.remove()
            return result
        except Exception as exc:
            log.exception("execution failed", extra=context)
            # An API timeout can hide a successful create/start; inspect before claiming release.
            confirmed = False
            try:
                existing = client.containers.get(name)
                if existing.labels.get("labflux.attempt_id") == spec.attempt_id:
                    self._stop(existing)
                    existing.reload()
                    confirmed = not existing.attrs["State"]["Running"]
                    if confirmed:
                        existing.remove()
            except NotFound:
                confirmed = True
            except DockerException:
                pass
            return ExecutionResult(
                "FAILED" if confirmed else "UNKNOWN", None, str(exc)[:2048], confirmed
            )
        finally:
            if stream is not None:
                with suppress(Exception):
                    stream.close()
            client.close()

    def stop_unstarted(self, spec: ExecutionSpec) -> ExecutionResult:
        """Handle a STOP received before its START, or a lost local task handle."""
        with closing(docker.from_env(timeout=max(10, self.settings.stop_grace_s + 5))) as client:
            try:
                container = client.containers.get(container_name(spec))
                if container.labels.get("labflux.attempt_id") != spec.attempt_id:
                    return ExecutionResult("UNKNOWN", None, "container identity mismatch", False)
                self._stop(container)
                container.reload()
                if container.attrs["State"]["Running"]:
                    return ExecutionResult("UNKNOWN", None, "container still running", False)
                container.remove()
            except NotFound:
                pass
            except DockerException as exc:
                return ExecutionResult("UNKNOWN", None, str(exc)[:2048], False)
        return ExecutionResult("CANCELLED", None, "cancelled", True)

    def _stop(self, container) -> None:
        container.reload()
        if container.attrs["State"]["Running"]:
            try:
                container.stop(timeout=self.settings.stop_grace_s)
            except DockerException:
                container.kill()
            container.reload()
            if container.attrs["State"]["Running"]:
                container.kill()
                container.reload()
