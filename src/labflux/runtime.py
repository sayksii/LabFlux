"""Docker execution with real kernel limits and conservative resource-release proof."""

from __future__ import annotations

import hashlib
import logging
import os
import re
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
ATTEMPT = re.compile(r"[a-f0-9]{32}")


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
        *,
        detach: threading.Event | None = None,
        on_launch: Callable[[], None] | None = None,
        adopt: bool = False,
        skip: dict[str, int] | None = None,
    ) -> ExecutionResult | None:
        """Run or adopt one attempt's container until it ends.

        Returns None when `detach` is set: the container keeps running so that the next worker
        instance can adopt it. `on_launch` runs just before the container can first exist, and
        `skip` gives the per-stream byte counts already captured for an adopted container.
        """
        client = docker.from_env(timeout=max(10, self.settings.stop_grace_s + 5))
        container = None
        readers: list[threading.Thread] = []
        streams = []
        reason = "completed"
        name = container_name(spec)
        context = {"job_id": spec.job_id, "attempt_id": spec.attempt_id}

        def detached() -> bool:
            return detach is not None and detach.is_set()

        try:
            try:
                container = client.containers.get(name)
            except NotFound:
                if adopt:
                    # Nothing is left of the adopted execution, which confirms its termination.
                    return ExecutionResult(
                        "FAILED", None, "container missing after worker restart", True
                    )
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
                if detached():
                    return None  # No container exists yet; the next instance starts afresh.
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
                if self.settings.uv_cache_dir:
                    # Created here so Docker never creates a missing bind source as root.
                    self.settings.uv_cache_dir.mkdir(parents=True, exist_ok=True)
                    volumes[str(self.settings.uv_cache_dir)] = {"bind": "/uv-cache", "mode": "rw"}
                    # Separate bind mounts cannot share hardlinks, so uv copies into the venv.
                    environment.update(UV_CACHE_DIR="/uv-cache", UV_LINK_MODE="copy")
                devices = None
                if spec.gpu_uuids:
                    # Docker sets NVIDIA_VISIBLE_DEVICES to exactly these UUIDs.
                    devices = [DeviceRequest(device_ids=spec.gpu_uuids, capabilities=[["gpu"]])]
                else:
                    # Keeps CPU jobs GPU-less even if a GPU runtime were made the default.
                    environment["NVIDIA_VISIBLE_DEVICES"] = "void"
                if on_launch:
                    on_launch()
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

            def collect_logs(stream) -> None:
                try:
                    for stdout, stderr in stream:
                        if stdout:
                            on_log("stdout", stdout)
                        if stderr:
                            on_log("stderr", stderr)
                except Exception:
                    log.exception("job log capture interrupted", extra=context)

            def follow_logs(kind: str, stream, captured: int) -> None:
                try:
                    for chunk in stream:
                        if captured:
                            dropped = min(captured, len(chunk))
                            chunk, captured = chunk[dropped:], captured - dropped
                        if chunk:
                            on_log(kind, chunk)
                except Exception:
                    log.exception("job log capture interrupted", extra=context)

            container.reload()
            if container.status == "created" and not cancel.is_set():
                # Attach before start: attaching afterwards with logs=True can lose or repeat
                # output written between the log replay and the live stream.
                stream = container.attach(stream=True, stdout=True, stderr=True, demux=True)
                streams.append(stream)
                readers.append(threading.Thread(target=collect_logs, args=(stream,), daemon=True))
                container.start()
                container.reload()
            if container.status == "created":
                container.remove()
                return ExecutionResult("CANCELLED", None, "cancelled before start", True)
            on_started()
            if not readers:
                # An adopted container already ran under an earlier worker instance. Its log
                # file replays from the beginning and then follows without a gap, one stream
                # at a time, so the bytes captured before the restart are skipped per stream.
                for kind in ("stdout", "stderr"):
                    stream = container.logs(
                        stdout=kind == "stdout", stderr=kind == "stderr", stream=True, follow=True
                    )
                    streams.append(stream)
                    readers.append(
                        threading.Thread(
                            target=follow_logs,
                            args=(kind, stream, (skip or {}).get(kind, 0)),
                            daemon=True,
                        )
                    )
            for reader in readers:
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
                if detached():
                    return None  # Left running; the next instance adopts it.
                time.sleep(self.settings.runtime_poll_s)
            container.reload()
            if container.attrs["State"]["Running"]:
                return ExecutionResult("UNKNOWN", None, "termination could not be confirmed", False)
            for reader in readers:
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
            for stream in streams:
                with suppress(Exception):
                    stream.close()
            # Closing the sockets ends the readers; wait so no log write outlives this call.
            for reader in readers:
                if reader.ident is not None:
                    reader.join(timeout=5)
            client.close()

    def inventory(self, worker_id: str) -> list[dict]:
        """Containers carrying this worker's labels, as reported when it registers."""
        found = []
        with closing(docker.from_env(timeout=max(10, self.settings.request_timeout_s))) as client:
            for container in client.containers.list(
                all=True,
                filters={"label": ["labflux.managed=true", f"labflux.worker_id={worker_id}"]},
            ):
                labels = container.labels
                attempt_id = labels.get("labflux.attempt_id", "")
                if labels.get("labflux.worker_id") != worker_id or not ATTEMPT.fullmatch(
                    attempt_id
                ):
                    continue
                state = container.attrs["State"]
                started_at = None
                if container.status != "created" and state.get("StartedAt"):
                    value = datetime.fromisoformat(state["StartedAt"].replace("Z", "+00:00"))
                    started_at = value.timestamp() if value.year > 1 else None
                found.append(
                    {
                        "attempt_id": attempt_id,
                        "worker_id": worker_id,
                        "status": container.status,
                        "exit_code": None if state.get("Running") else state.get("ExitCode"),
                        "oom_killed": bool(state.get("OOMKilled")),
                        "started_at": started_at,
                    }
                )
        return found

    def remove(self, worker_id: str, attempt_id: str) -> bool:
        """Stop and remove one of this worker's containers; True once none is left."""
        with closing(docker.from_env(timeout=max(10, self.settings.stop_grace_s + 5))) as client:
            for container in client.containers.list(
                all=True,
                filters={
                    "label": [
                        "labflux.managed=true",
                        f"labflux.worker_id={worker_id}",
                        f"labflux.attempt_id={attempt_id}",
                    ]
                },
            ):
                labels = container.labels
                if (
                    labels.get("labflux.worker_id") != worker_id
                    or labels.get("labflux.attempt_id") != attempt_id
                ):
                    return False
                self._stop(container)
                container.reload()
                if container.attrs["State"]["Running"]:
                    return False
                container.remove()
        return True

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
