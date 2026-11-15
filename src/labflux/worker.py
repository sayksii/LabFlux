"""Worker control loop, independent heartbeats, and durable execution outboxes."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import tarfile
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from socket import SHUT_RDWR
from typing import BinaryIO

import httpx
import psutil
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from labflux.archives import extract_archive, pack_directory
from labflux.config import Settings
from labflux.gpu import GpuProbe
from labflux.inference import (
    InferBody,
    InferCancel,
    InferChunk,
    InferEnd,
    InferHead,
    InferRequest,
    parse_to_worker,
)
from labflux.models import Capacity, ExecutionSpec
from labflux.runtime import DockerExecutor, ExecutionResult
from labflux.session_protocol import (
    CHUNK_BYTES,
    MAX_SKIPPED_LISTED,
    WORKSPACE,
    CancelRequest,
    CommandResult,
    Notice,
    Output,
    SkippedFile,
    SnapshotChunk,
    WorkerExec,
    WritebackChunk,
    decode,
    encode,
    iter_chunks,
    parse_worker_command,
)
from labflux.workspace import apply_snapshot, dependency_hash, is_directory, pack_files, scan

log = logging.getLogger("labflux.worker")
ATTEMPT_ID = re.compile(r"[a-f0-9]{32}")
# Command IDs end up in a URL path, so separators and dots are never allowed.
COMMAND_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
# First retry delay after a failed connection or flush; it doubles up to poll_interval_s.
RETRY_S = 0.5
# How long a finished session attempt waits for its in-flight command to send its result.
COMMAND_JOIN_S = 30
CANCELLED = "command cancelled by the client"


class Unaddressed(Exception):
    """A command that cannot safely be tied to one of this worker's attempts."""


class Invalid(Exception):
    """A command for one of this worker's attempts that cannot run as sent."""

    def __init__(self, attempt_id: str, reason: str):
        super().__init__(reason)
        self.attempt_id = attempt_id
        self.reason = reason


class StopEvent(threading.Event):
    """Setting it also wakes the threads waiting on the linked wake-up events."""

    def __init__(self, *linked: threading.Event):
        super().__init__()
        self.linked = linked

    def set(self) -> None:
        super().set()
        for event in self.linked:
            event.set()


@dataclass
class CommandRun:
    """One fast-lane command: its snapshot upload, then its thread (M9)."""

    command: WorkerExec
    file: BinaryIO
    path: Path
    digest: object = field(default_factory=hashlib.sha256)
    received: int = 0
    cancel: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    output_bytes: int = 0
    truncated: bool = False

    @property
    def id(self) -> str:
        return self.command.id

    @property
    def attempt_id(self) -> str:
        return self.command.attempt_id


@dataclass
class InferRun:
    """One relayed inference request (M10): its body upload, then its thread."""

    request: InferRequest
    body: bytearray = field(default_factory=bytearray)
    cancel: threading.Event = field(default_factory=threading.Event)
    reason: str = "cancelled"
    thread: threading.Thread | None = None
    response: httpx.Response | None = None


def write_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    with temp.open("x", encoding="utf-8") as stream:
        temp.chmod(0o600)
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def measure_capacity(settings: Settings, gpus: GpuProbe | None = None) -> Capacity:
    total_cpu = os.cpu_count() or 1
    total_memory = psutil.virtual_memory().total // 1024**2
    cpu = settings.allocatable_cpu or total_cpu
    memory = settings.allocatable_memory_mib or (total_memory - settings.memory_reserve_mib)
    if cpu > total_cpu or memory > total_memory - settings.memory_reserve_mib or memory < 16:
        raise ValueError("configured capacity exceeds CPU or RAM after the system reserve")
    return Capacity(
        total_cpu=total_cpu,
        total_memory_mib=total_memory,
        cpu=cpu,
        memory_mib=memory,
        datasets=sorted(name for name, path in settings.datasets.items() if path.is_dir()),
        gpus=gpus.devices if gpus else [],
        gpu_driver_version=gpus.driver_version if gpus else None,
        cuda_driver_version=gpus.cuda_driver_version if gpus else None,
    )


class Worker:
    def __init__(self, settings: Settings, executor=None, gpus: GpuProbe | None = None):
        self.settings = settings
        self.instance_id = uuid.uuid4().hex
        self.root = settings.data_dir / "workers" / settings.worker_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.executor = executor or DockerExecutor(settings)
        # Fails before registration when configured cards cannot be resolved.
        self.gpus = gpus or GpuProbe(settings.gpus, settings.gpu_external_memory_mib)
        # Cards held by attempts whose result is not yet recorded; never reported as external.
        self.assigned_gpus: dict[str, list[str]] = {}
        # Attempts executing in this instance, reported with each heartbeat.
        self.active: set[str] = set()
        # Doorbell from the Scheduler: commands are waiting (T4).
        self.commands_ready = threading.Event()
        # Something new was written to the outbox (T8).
        self.outbox_ready = threading.Event()
        self.stop_event = StopEvent(self.commands_ready, self.outbox_ready)
        # Handlers for other message types on the doorbell connection (fast lane, T6); they run
        # on the doorbell thread and must not block.
        self.handlers: dict[str, Callable[[dict], None]] = {
            "exec": self._on_exec,
            "snapshot_chunk": self._on_snapshot_chunk,
            "cancel": self._on_cancel,
            "infer": self._on_infer,
            "infer_body": self._on_infer_body,
            "infer_cancel": self._on_infer_cancel,
        }
        # Fast-lane commands by command ID, and why the Worker itself ended a session (or a
        # model service, which may also replace the outcome).
        self.runs: dict[str, CommandRun] = {}
        self.end_reasons: dict[str, str] = {}
        self.end_outcomes: dict[str, str] = {}
        # Inference requests by request ID, and model server base URLs by attempt (M10).
        self.inference: dict[str, InferRun] = {}
        self.addresses: dict[str, str] = {}
        # For model containers on this host; never through a proxy.
        self.local = httpx.Client(trust_env=False)
        self.socket = None
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(
            max_workers=settings.max_parallel_jobs, thread_name_prefix="job"
        )
        self.handles: dict[str, dict] = {}
        self.client = httpx.Client(
            base_url=settings.scheduler_url,
            headers={"Authorization": f"Bearer {settings.worker_token}"},
            timeout=settings.request_timeout_s,
            trust_env=False,
        )

    def _url(self, suffix: str) -> str:
        return f"/internal/workers/{self.settings.worker_id}/{suffix}"

    def _post(self, path: str, payload: dict):
        response = self.client.post(path, json=payload)
        response.raise_for_status()
        return response.json()

    def _metadata(self, attempt_id: str) -> Path:
        return self.root / attempt_id / "state.json"

    def _read(self, attempt_id: str) -> dict:
        return json.loads(self._metadata(attempt_id).read_text())

    def _change(self, attempt_id: str, **changes) -> dict:
        with self.lock:
            value = self._read(attempt_id)
            value.update(changes)
            write_json(self._metadata(attempt_id), value)
            return value

    def _spec(self, payload: dict) -> ExecutionSpec:
        spec = ExecutionSpec.model_validate(payload)
        # IDs are generated by the scheduler; disallow path injection even for a malformed server.
        if not ATTEMPT_ID.fullmatch(spec.attempt_id):
            raise ValueError("invalid attempt ID")
        if spec.worker_id != self.settings.worker_id:
            raise ValueError("command is addressed to another worker")
        pool = {device.uuid for device in self.gpus.devices}
        if len(set(spec.gpu_uuids)) != len(spec.gpu_uuids) or not pool.issuperset(spec.gpu_uuids):
            raise ValueError("command assigns GPUs outside this worker's pool")
        return spec

    def _validate(self, command) -> None:
        """Classify a polled command before any local state is touched."""
        if not isinstance(command, dict):
            raise Unaddressed("command is not an object")
        payload = command.get("payload")
        attempt_id = payload.get("attempt_id") if isinstance(payload, dict) else None
        if attempt_id is None:
            attempt_id = command.get("attempt_id")
        if (
            not isinstance(attempt_id, str)
            or not ATTEMPT_ID.fullmatch(attempt_id)
            or command.get("attempt_id", attempt_id) != attempt_id
        ):
            raise Unaddressed("invalid attempt ID")
        named = payload.get("worker_id") if isinstance(payload, dict) else None
        if (named is not None and named != self.settings.worker_id) or command.get(
            "node_id", self.settings.worker_id
        ) != self.settings.worker_id:
            raise Unaddressed("command is addressed to another worker")
        if command.get("kind") not in {"START", "STOP"}:
            raise Invalid(attempt_id, f"unknown command kind {command.get('kind')!r}")
        try:
            self._spec(payload)
        except (ValueError, TypeError) as exc:
            raise Invalid(attempt_id, str(exc)) from exc

    def process(self, commands: list) -> None:
        """Accept and ack one poll's commands; a bad command never holds up the others."""
        for command in commands:
            try:
                self._validate(command)
            except Unaddressed as exc:
                # Nothing local can be tied to it; the Scheduler keeps its allocation, which
                # stays safe because nothing is released without confirmed termination.
                log.error("acked a command that is not for this worker: %s", exc)
                self._ack(command)
                continue
            except Invalid as exc:
                log.error("rejected command: %s", exc.reason, extra={"attempt_id": exc.attempt_id})
                self._reject(exc.attempt_id, f"rejected command: {exc.reason}")
                self._ack(command)
                continue
            try:
                self.accept(command)
            except Exception:
                # Unacked, so the next poll retries it; the remaining commands still run.
                log.exception(
                    "command failed; it is retried at the next poll",
                    extra={"attempt_id": command.get("attempt_id")},
                )
                continue
            self._ack(command)

    def _ack(self, command) -> None:
        command_id = command.get("id") if isinstance(command, dict) else None
        if not isinstance(command_id, str) or not COMMAND_ID.fullmatch(command_id):
            log.error("cannot ack a command without a valid ID")
            return
        self._post(self._url(f"commands/{command_id}/ack"), {"instance_id": self.instance_id})

    def _reject(self, attempt_id: str, reason: str) -> None:
        """Report FAILED for a command that never created a container, via the outbox."""
        with self.lock:
            if attempt_id in self.handles or self._metadata(attempt_id).exists():
                log.error(
                    "rejected command left existing attempt state untouched",
                    extra={"attempt_id": attempt_id},
                )
                return
            (self.root / attempt_id).mkdir(parents=True, exist_ok=True)
            record = self._record(None, attempt_id=attempt_id, launched=False)
            record.update(
                result=ExecutionResult("FAILED", None, reason[:2048], True).as_dict(),
                result_event_id=uuid.uuid4().hex,
                output_sent=True,  # Nothing ran, so there is no output to collect.
            )
            write_json(self._metadata(attempt_id), record)
            done = Future()
            done.set_result(None)
            self.handles[attempt_id] = {"cancel": threading.Event(), "future": done}
        self.outbox_ready.set()

    def accept(self, command: dict) -> None:
        spec = self._spec(command["payload"])
        with self.lock:
            handle = self.handles.get(spec.attempt_id)
            if handle:
                if command["kind"] == "STOP":
                    handle["cancel"].set()
                    state = self._read(spec.attempt_id)
                    if (
                        handle["future"].done()
                        and state["result"] is not None
                        and not state["result"]["termination_confirmed"]
                    ):
                        handle["future"] = self.pool.submit(
                            self._execute, spec, handle["cancel"], True
                        )
                return
            directory = self.root / spec.attempt_id
            directory.mkdir(parents=True, exist_ok=True)
            if self._metadata(spec.attempt_id).exists():
                previous = self._read(spec.attempt_id)
                if previous["result"] is not None:
                    return  # Replayed command cannot rerun a completed attempt.
                raise RuntimeError("unfinished attempt requires reconciliation before restart")
            cancel = threading.Event()
            if command["kind"] == "STOP":
                cancel.set()
            write_json(self._metadata(spec.attempt_id), self._record(spec, launched=False))
            self._launch(spec, cancel, stop_only=command["kind"] == "STOP")

    def _record(
        self,
        spec: ExecutionSpec | None,
        *,
        launched: bool,
        next_seq: int = 0,
        attempt_id: str | None = None,
    ) -> dict:
        return {
            # None for a rejected command, whose payload could not be validated.
            "spec": spec.model_dump() if spec else None,
            "attempt_id": spec.attempt_id if spec else attempt_id,
            "instance_id": self.instance_id,
            # Set just before Docker can first create the container.
            "launched": launched,
            "started": False,
            "start_sent": False,
            "result": None,
            "result_sent": False,
            "result_event_id": None,
            "next_seq": next_seq,
            "log_bytes": 0,
            "log_offset": 0,
            "logs_truncated": False,
            "output_sent": False,
        }

    def _launch(
        self,
        spec: ExecutionSpec,
        cancel: threading.Event,
        *,
        stop_only: bool = False,
        adopt: bool = False,
        skip: dict[str, int] | None = None,
    ) -> None:
        self.handles[spec.attempt_id] = {"cancel": cancel}
        self.active.add(spec.attempt_id)
        if spec.gpu_uuids:
            self.assigned_gpus[spec.attempt_id] = spec.gpu_uuids
        future = self.pool.submit(self._execute, spec, cancel, stop_only, adopt, skip)
        self.handles[spec.attempt_id]["future"] = future

    def inventory(self) -> dict:
        """Local records and labelled containers a restarted instance reports when registering."""
        records = []
        for path in sorted(self.root.glob("*/state.json")):
            attempt_id = path.parent.name
            if not ATTEMPT_ID.fullmatch(attempt_id):
                continue
            try:
                state = json.loads(path.read_text())
                result = state["result"]
                confirmed = bool(result and result["termination_confirmed"])
                sent = bool(state["result_sent"])
                launched = bool(state.get("launched", True))
            except (OSError, ValueError, KeyError, TypeError):
                # Reported as possibly launched, so only a surviving container can keep it.
                log.warning("unreadable attempt record", extra={"attempt_id": attempt_id})
                records.append({"attempt_id": attempt_id})
                continue
            if confirmed and sent:
                continue
            records.append(
                {
                    "attempt_id": attempt_id,
                    "launched": launched,
                    "unsent_result": confirmed and not sent,
                }
            )
        pending = {item["attempt_id"] for item in records if not item.get("unsent_result")}
        containers = self._drop_interrupted(
            self.executor.inventory(self.settings.worker_id), pending
        )
        return {"attempts": records, "containers": containers}

    def _drop_interrupted(self, containers: list[dict], pending: set[str]) -> list[dict]:
        """Remove stopped containers that a host shutdown, reboot or crash interrupted.

        Docker keeps them across a reboot, and adopting one would report the shutdown's exit
        (143, 137 or 255) as an ordinary program failure. Removing it (it is already stopped,
        so termination is confirmed) and leaving it out lets the Scheduler close the execution
        as lost and retry it. A clean exit or an OOM kill is still adopted as it happened.
        """
        booted = psutil.boot_time()
        kept = []
        for item in containers:
            started = item.get("started_at")
            if (
                item["attempt_id"] in pending
                and item["status"] in ("exited", "dead")
                and started is not None
                and started < booted
                and item.get("exit_code") != 0
                and not item.get("oom_killed")
            ):
                try:
                    removed = self.executor.remove(self.settings.worker_id, item["attempt_id"])
                except Exception:
                    log.exception(
                        "interrupted container removal failed",
                        extra={"attempt_id": item["attempt_id"]},
                    )
                    removed = False
                if removed:
                    log.info(
                        "removed a container interrupted by a host restart",
                        extra={"attempt_id": item["attempt_id"]},
                    )
                    continue
            kept.append(item)
        return kept

    def register(self) -> dict:
        capacity = measure_capacity(self.settings, self.gpus)
        response = self._post(
            "/internal/workers/register",
            {
                "worker_id": self.settings.worker_id,
                "instance_id": self.instance_id,
                "capacity": capacity.model_dump(),
                "inventory": self.inventory(),
            },
        )
        self._reconcile(response)
        return response

    def _reconcile(self, response: dict) -> None:
        for attempt_id in response.get("closed", []):
            self._retire(attempt_id, "closed by the scheduler after a worker restart")
        for attempt_id in response.get("restart", []):
            if ATTEMPT_ID.fullmatch(attempt_id):
                # Nothing was launched, so the redelivered START begins from a clean record.
                shutil.rmtree(self.root / attempt_id, ignore_errors=True)
        for attempt_id in response.get("remove", []):
            if not ATTEMPT_ID.fullmatch(attempt_id):
                continue
            if not self._metadata(attempt_id).exists():
                # Labels alone could also match another installation using this worker ID.
                log.warning(
                    "left a container without a local record", extra={"attempt_id": attempt_id}
                )
                continue
            try:
                removed = self.executor.remove(self.settings.worker_id, attempt_id)
            except Exception:
                log.exception("stale container removal failed", extra={"attempt_id": attempt_id})
                continue
            if removed:
                log.info("removed a stale container", extra={"attempt_id": attempt_id})
                self._retire(attempt_id, "stale container removed after a worker restart")
        for entry in response.get("adopt", []):
            try:
                self._adopt(entry)
            except (ValueError, TypeError, KeyError):
                # E.g. its cards left this worker's pool. The allocation stays reserved, which
                # is safe; failing here would only crash-loop the Worker under systemd.
                log.exception(
                    "could not adopt an attempt", extra={"attempt_id": entry.get("attempt_id")}
                )

    def _retire(self, attempt_id: str, reason: str) -> None:
        """Mark a record settled so later restarts no longer report it."""
        if not ATTEMPT_ID.fullmatch(attempt_id) or not self._metadata(attempt_id).exists():
            return
        try:
            state = self._read(attempt_id)
            result = state["result"]
            if not (result and result["termination_confirmed"]):
                result = ExecutionResult("FAILED", None, reason, True).as_dict()
            self._change(attempt_id, result=result, result_sent=True, output_sent=True)
        except (OSError, ValueError, KeyError):
            write_json(
                self._metadata(attempt_id),
                {
                    "result": ExecutionResult("FAILED", None, reason, True).as_dict(),
                    "result_sent": True,
                },
            )
        self._remove_workspace(attempt_id)

    def _adopt(self, entry: dict) -> None:
        """Resume an attempt the scheduler still holds for this worker."""
        spec = self._spec(entry["payload"])
        attempt_id = spec.attempt_id
        with self.lock:
            if attempt_id in self.handles:
                return
            (self.root / attempt_id).mkdir(parents=True, exist_ok=True)
            cancel = threading.Event()
            if entry.get("cancel"):
                cancel.set()
            if self._metadata(attempt_id).exists():
                skip = self._repair_logs(attempt_id)
                state = self._change(attempt_id, instance_id=self.instance_id)
                result = state["result"]
                if result is not None and result["termination_confirmed"]:
                    # Only delivery is left; the outbox sends the result under a fresh event ID.
                    self._change(attempt_id, result_event_id=uuid.uuid4().hex, result_sent=False)
                    done = Future()
                    done.set_result(None)
                    self.handles[attempt_id] = {"cancel": cancel, "future": done}
                    self.outbox_ready.set()
                    return
                self._change(
                    attempt_id, result=None, result_sent=False, output_sent=False, launched=True
                )
            else:
                # The local record is gone; continue the scheduler's log sequence instead.
                skip = {k: int(v) for k, v in entry.get("log_bytes", {}).items()}
                record = self._record(spec, launched=True, next_seq=entry.get("next_seq", 0))
                record["log_bytes"] = sum(skip.values())
                write_json(self._metadata(attempt_id), record)
            log.info("adopted attempt", extra={"job_id": spec.job_id, "attempt_id": attempt_id})
            self._launch(spec, cancel, adopt=True, skip=skip)

    def _repair_logs(self, attempt_id: str) -> dict[str, int]:
        """Make logs.jsonl the source of truth after an unclean stop and return the bytes it
        holds per stream. A torn last line is dropped, and the counters follow the file."""
        path = self.root / attempt_id / "logs.jsonl"
        captured = {"stdout": 0, "stderr": 0}
        next_seq, valid = None, 0
        data = path.read_bytes() if path.exists() else b""
        for line in data.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                break
            try:
                value = json.loads(line)
                size = len(base64.b64decode(value["data_base64"], validate=True))
            except (ValueError, KeyError):
                break
            captured[value["stream"]] += size
            next_seq = value["seq"] + 1
            valid += len(line)
        if valid < len(data):
            with path.open("r+b") as target:
                target.truncate(valid)
        state = self._read(attempt_id)
        self._change(
            attempt_id,
            next_seq=state["next_seq"] if next_seq is None else next_seq,
            log_bytes=sum(captured.values()),
            log_offset=min(state["log_offset"], valid),
        )
        return captured

    def _log(self, attempt_id: str, stream: str, data: bytes) -> None:
        with self.lock:
            state = self._read(attempt_id)
            remaining = self.settings.max_log_bytes - state["log_bytes"]
            if len(data) > remaining:
                state["logs_truncated"] = True
            data = data[: max(0, remaining)]
            with (self.root / attempt_id / "logs.jsonl").open("ab") as target:
                for offset in range(0, len(data), self.settings.log_chunk_bytes):
                    chunk = data[offset : offset + self.settings.log_chunk_bytes]
                    value = {
                        "seq": state["next_seq"],
                        "stream": stream,
                        "data_base64": base64.b64encode(chunk).decode(),
                    }
                    target.write(json.dumps(value).encode() + b"\n")
                    state["next_seq"] += 1
                    state["log_bytes"] += len(chunk)
                target.flush()
            write_json(self._metadata(attempt_id), state)
        self.outbox_ready.set()

    def _prepare(self, spec: ExecutionSpec, workspace: Path) -> None:
        workspace.mkdir(parents=True, exist_ok=True)
        if spec.job.source_id:
            archive = workspace.parent / "source.tar.gz"
            digest = hashlib.sha256()
            size = 0
            with self.client.stream(
                "GET",
                self._url(f"attempts/{spec.attempt_id}/source"),
                params={"instance_id": self.instance_id},
            ) as response:
                response.raise_for_status()
                with archive.open("wb") as target:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self.settings.max_source_bytes:
                            raise ValueError("source exceeds configured size limit")
                        digest.update(chunk)
                        target.write(chunk)
                if digest.hexdigest() != response.headers["X-Content-SHA256"]:
                    raise ValueError("source checksum mismatch")
            extract_archive(
                archive,
                workspace,
                max_bytes=self.settings.max_extracted_bytes,
                max_members=self.settings.max_archive_members,
            )

    def _execute(
        self,
        spec: ExecutionSpec,
        cancel: threading.Event,
        stop_only: bool,
        adopt: bool = False,
        skip: dict[str, int] | None = None,
    ) -> None:
        directory = self.root / spec.attempt_id
        preparation_finished = False
        try:
            if stop_only:
                preparation_finished = True
                result = self.executor.stop_unstarted(spec)
            else:
                if not adopt:
                    self._prepare(spec, directory / "workspace")
                preparation_finished = True
                result = self.executor.run(
                    spec,
                    directory / "workspace",
                    directory / "outputs",
                    lambda stream, data: self._log(spec.attempt_id, stream, data),
                    # A model server counts as started only once it is healthy (plan 10.7).
                    (lambda: self._service_started(spec))
                    if spec.service
                    else (lambda: self._started(spec.attempt_id)),
                    cancel,
                    detach=self.stop_event,
                    on_launch=lambda: self._change(spec.attempt_id, launched=True),
                    adopt=adopt,
                    skip=skip,
                )
        except Exception as exc:
            if self.stop_event.is_set() and not self._read(spec.attempt_id).get("launched", True):
                # Shutdown interrupted preparation; nothing ran, so the next instance restarts it.
                result = None
            else:
                log.exception(
                    "attempt preparation failed",
                    extra={"job_id": spec.job_id, "attempt_id": spec.attempt_id},
                )
                # Only failures before Docker invocation prove no process was started.
                result = ExecutionResult(
                    "UNKNOWN" if preparation_finished else "FAILED",
                    None,
                    str(exc)[:2048],
                    not preparation_finished,
                )
        if spec.session:
            # The last command still writes back from the workspace, which the outbox removes
            # once the result is recorded; its reason replaces the generic "cancelled".
            with self.lock:
                runs = [run for run in self.runs.values() if run.attempt_id == spec.attempt_id]
            for run in runs:
                if run.thread is None:
                    self._drop_run(run)  # Its snapshot was still arriving; nothing ran.
                    self._reply(run.id, **self._session_ended(spec.attempt_id, "cancelled"))
                else:
                    run.thread.join(COMMAND_JOIN_S)
        if spec.service:
            self._abort_inference(spec.attempt_id, "the model service ended")
            with self.lock:
                self.addresses.pop(spec.attempt_id, None)
        if spec.session or spec.service:
            with self.lock:
                reason = self.end_reasons.pop(spec.attempt_id, None)
                outcome = self.end_outcomes.pop(spec.attempt_id, None)
            if result is not None and reason:
                result.reason = reason
            if result is not None and outcome and result.termination_confirmed:
                result.outcome = outcome
        with self.lock:
            # None means the worker is shutting down: the result stays open for adoption.
            if result is not None:
                self._change(
                    spec.attempt_id,
                    result=result.as_dict(),
                    result_event_id=uuid.uuid4().hex,
                    result_sent=False,
                    output_sent=False,
                )
            self.assigned_gpus.pop(spec.attempt_id, None)
            self.active.discard(spec.attempt_id)
        self.outbox_ready.set()

    def _started(self, attempt_id: str) -> None:
        self._change(attempt_id, started=True)
        self.outbox_ready.set()

    def flush(self) -> None:
        with self.lock:
            attempt_ids = list(self.handles)
        for attempt_id in attempt_ids:
            state = self._read(attempt_id)
            if state["result_sent"]:
                continue
            prefix = f"attempts/{attempt_id}"
            if state["started"] and not state["start_sent"]:
                self._post(
                    self._url(f"{prefix}/events"),
                    {
                        # Per instance: an adopting instance may not know whether it was sent.
                        "event_id": f"{attempt_id}:started:{self.instance_id}",
                        "instance_id": self.instance_id,
                        "kind": "STARTED",
                    },
                )
                state = self._change(attempt_id, start_sent=True)
            log_path = self.root / attempt_id / "logs.jsonl"
            if log_path.exists():
                with log_path.open("rb") as source:
                    source.seek(state["log_offset"])
                    for _ in range(64):
                        line = source.readline()
                        if not line or not line.endswith(b"\n"):
                            break
                        payload = json.loads(line)
                        self._post(
                            self._url(f"{prefix}/logs"),
                            {**payload, "instance_id": self.instance_id},
                        )
                        state = self._change(attempt_id, log_offset=source.tell())
                    else:
                        self.outbox_ready.set()  # More lines may remain; flush again.
            state = self._read(attempt_id)
            if state["result"] is not None:
                if log_path.exists() and state["log_offset"] < log_path.stat().st_size:
                    continue
                self._send_output(attempt_id, state)
                state = self._read(attempt_id)
                if state["output_sent"] and state["result"]["termination_confirmed"]:
                    self._remove_workspace(attempt_id)
                result = state["result"]
                reason = result["reason"]
                if state["logs_truncated"]:
                    reason = (reason + "; log limit reached")[:2048]
                self._post(
                    self._url(f"{prefix}/events"),
                    {
                        "event_id": f"{attempt_id}:{state['result_event_id']}",
                        "instance_id": self.instance_id,
                        "kind": "FINISHED",
                        **result,
                        "reason": reason,
                    },
                )
                with self.lock:
                    if self._read(attempt_id)["result_event_id"] == state["result_event_id"]:
                        self._change(attempt_id, result_sent=True)

    def _send_output(self, attempt_id: str, state: dict) -> None:
        if state["output_sent"]:
            return
        if not state["result"]["termination_confirmed"]:
            self._change(attempt_id, output_sent=True)
            return
        directory = self.root / attempt_id
        outputs = directory / "outputs"
        outputs.mkdir(exist_ok=True)
        archive = directory / "output.tar.gz"
        try:
            if not archive.exists():
                pack_directory(outputs, archive, source=False)
            if archive.stat().st_size > self.settings.max_output_bytes:
                raise ValueError("output exceeds configured upload limit")
            with archive.open("rb") as content:
                response = self.client.put(
                    self._url(f"attempts/{attempt_id}/output"),
                    params={"instance_id": self.instance_id},
                    content=content,
                    headers={"Content-Type": "application/gzip"},
                )
            response.raise_for_status()
        except ValueError as exc:
            result = dict(
                state["result"], outcome="FAILED", reason=f"output collection failed: {exc}"
            )
            self._change(attempt_id, result=result, output_sent=True)
            return
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 413:
                raise
            result = dict(
                state["result"], outcome="FAILED", reason="output upload exceeds server limit"
            )
            self._change(attempt_id, result=result, output_sent=True)
            return
        self._change(attempt_id, output_sent=True)

    def heartbeat_payload(self) -> dict:
        with self.lock:
            assigned = {uuid for uuids in self.assigned_gpus.values() for uuid in uuids}
            active = sorted(self.active)
        return {
            "instance_id": self.instance_id,
            "observed": {
                "cpu_percent": psutil.cpu_percent(),
                "available_memory_mib": psutil.virtual_memory().available / 1024**2,
            },
            "gpus": [gpu.model_dump() for gpu in self.gpus.observe(assigned)],
            "active_attempts": active,
        }

    def _remove_workspace(self, attempt_id: str) -> None:
        """Drop the job's files (a uv .venv can be several GB) once nothing can still run in
        them; state.json, logs and the output archive stay."""
        workspace = self.root / attempt_id / "workspace"
        if not workspace.exists():
            return
        try:
            shutil.rmtree(workspace)
        except OSError as exc:
            log.warning("workspace cleanup failed: %s", exc, extra={"attempt_id": attempt_id})

    def _heartbeats(self) -> None:
        with httpx.Client(
            base_url=self.settings.scheduler_url,
            headers={"Authorization": f"Bearer {self.settings.worker_token}"},
            timeout=self.settings.request_timeout_s,
            trust_env=False,
        ) as client:
            while not self.stop_event.is_set():
                try:
                    response = client.post(self._url("heartbeat"), json=self.heartbeat_payload())
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    log.warning("heartbeat failed: %s", type(exc).__name__)
                self.stop_event.wait(self.settings.heartbeat_interval_s)

    def _outbox(self) -> None:
        """Flush when something is written (T8), else every poll_interval_s; back off while
        the Scheduler is unreachable instead of retrying on every new log line."""
        delay = min(RETRY_S, self.settings.poll_interval_s)
        while not self.stop_event.is_set():
            self.outbox_ready.clear()
            try:
                self.flush()
            except Exception as exc:
                if isinstance(exc, httpx.HTTPError):
                    log.warning("outbox will retry: %s", type(exc).__name__)
                else:
                    log.exception("outbox failure")
                self.stop_event.wait(delay)
                delay = min(delay * 2, self.settings.poll_interval_s)
                continue
            delay = min(RETRY_S, self.settings.poll_interval_s)
            self.outbox_ready.wait(self.settings.poll_interval_s)

    def _doorbell(self) -> None:
        """Keep the WebSocket to the Scheduler open; a ring wakes the command loop (T4)."""
        base = self.settings.scheduler_url.rstrip("/").replace("http", "ws", 1)
        url = f"{base}{self._url('ws')}?instance_id={self.instance_id}"
        delay = min(RETRY_S, self.settings.poll_interval_s)
        while not self.stop_event.is_set():
            try:
                with connect(
                    url,
                    additional_headers={"Authorization": f"Bearer {self.settings.worker_token}"},
                    open_timeout=self.settings.request_timeout_s,
                    close_timeout=self.settings.request_timeout_s,
                    proxy=None,
                ) as socket:
                    self.socket = socket
                    delay = min(RETRY_S, self.settings.poll_interval_s)
                    self.commands_ready.set()  # Catch up on rings missed while disconnected.
                    while not self.stop_event.is_set():
                        try:
                            message = socket.recv(timeout=1)
                        except TimeoutError:
                            continue  # Only to notice stop_event; nothing is sent.
                        self._on_message(message)
            except (OSError, WebSocketException) as exc:
                log.warning("doorbell connection will retry: %s", type(exc).__name__)
            finally:
                self.socket = None
                self._relay_lost()
            self.stop_event.wait(delay)
            delay = min(delay * 2, self.settings.poll_interval_s)

    def _on_message(self, raw) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            log.warning("ignored a malformed doorbell message")
            return
        if not isinstance(message, dict):
            return
        if message.get("type") == "commands":
            self.commands_ready.set()
            return
        handler = self.handlers.get(message.get("type"))
        if handler is None:
            return
        try:
            handler(message)
        except Exception as exc:
            log.exception("doorbell handler failed for a %r message", message.get("type"))
            # Answer at once; otherwise the client waits for the Scheduler's guard time.
            command_id = message.get("id")
            run = self.runs.get(command_id) if isinstance(command_id, str) else None
            if run is not None and run.thread is not None:
                return  # The command is running; its thread sends the result.
            if run is not None:
                self._drop_run(run)
            if isinstance(command_id, str) and re.fullmatch(r"[0-9a-f]{32}", command_id):
                self._reply(command_id, outcome="error", reason=f"worker error: {exc}"[:2048])

    def send(self, message: dict) -> bool:
        """Send a JSON message to the Scheduler over the doorbell connection; False when it is
        not connected. Delivery is best effort, like the relay it feeds (T6)."""
        socket = self.socket
        if socket is None:
            return False
        try:
            socket.send(json.dumps(message))
        except (OSError, WebSocketException):
            return False
        return True

    # Fast-lane session commands (M9, T6) ---------------------------------------------------
    # The handlers run on the doorbell thread and never block; each command runs on its own
    # daemon thread (the pool is sized to max_parallel_jobs and already holds the sessions).

    def _relay(self, message: dict) -> None:
        """Send a fast-lane message; a lost relay ends every session with a command in flight,
        since nobody can receive that command's output or cancel it any more."""
        if not self.send(message):
            self._relay_lost()

    def _reply(self, command_id: str, **fields) -> None:
        self._relay(CommandResult(id=command_id, **fields).model_dump())

    def _session_state(self, attempt_id: str) -> dict:
        """What a session keeps between commands: the last snapshot's file list, the synced
        dependency hash, and the files ({path: size}) that exist only in its workspace because
        they were over a write-back limit."""
        state = {"files": [], "deps": None, "remote": {}}
        path = self.root / attempt_id / "session.json"
        if path.exists():
            state.update(json.loads(path.read_text()))
        return state

    @staticmethod
    def _remote_only(session: dict) -> dict:
        """The result fields listing a session's remote-only files (CommandResult.skipped)."""
        listed = [SkippedFile(path=p, size=size) for p, size in sorted(session["remote"].items())]
        return {"skipped": listed[:MAX_SKIPPED_LISTED], "skipped_count": len(listed)}

    def _reply_early(self, run: CommandRun, **fields) -> None:
        """Result of a command that ended before its thread started; the session lives on, so
        the result still carries its remote-only files."""
        self._reply(run.id, **fields, **self._remote_only(self._session_state(run.attempt_id)))

    def _relay_lost(self) -> None:
        self._abort_inference(None, "relay connection lost")
        with self.lock:
            runs = list(self.runs.values())
        for run in runs:
            if run.thread is None:
                self._drop_run(run)  # Still receiving its snapshot; nothing ran.
            else:
                self._end_session(run.attempt_id, "relay connection lost during a command")

    def _drop_run(self, run: CommandRun) -> None:
        with self.lock:
            self.runs.pop(run.id, None)
        try:
            run.file.close()
        except OSError:
            pass
        run.path.unlink(missing_ok=True)

    def _end_session(self, attempt_id: str, reason: str) -> None:
        """Terminate a session container from the Worker side; run() confirms the stop and the
        attempt's FINISHED event carries this reason."""
        with self.lock:
            handle = self.handles.get(attempt_id)
            if handle is None:
                return
            self.end_reasons.setdefault(attempt_id, reason)
            handle["cancel"].set()

    def _session_running(self, attempt_id: str) -> bool:
        with self.lock:
            handle = self.handles.get(attempt_id)
            if handle is None or attempt_id not in self.active or handle["cancel"].is_set():
                return False
            try:
                state = self._read(attempt_id)
            except (OSError, ValueError):
                return False
            return bool(state.get("started")) and bool((state.get("spec") or {}).get("session"))

    def _on_exec(self, message: dict) -> None:
        try:
            command = parse_worker_command(message)
            if not isinstance(command, WorkerExec):
                raise ValueError("not an exec command")
        except ValueError as exc:
            command_id = message.get("id")
            if isinstance(command_id, str) and re.fullmatch(r"[0-9a-f]{32}", command_id):
                self._reply(command_id, outcome="error", reason=f"invalid command: {exc}"[:2048])
            return
        if not self._session_running(command.attempt_id):
            self._reply(command.id, outcome="error", reason="session is not running on this worker")
            return
        with self.lock:
            busy = any(run.attempt_id == command.attempt_id for run in self.runs.values())
            if not busy:
                path = self.root / command.attempt_id / f"snapshot-{command.id}.tar.gz"
                self.runs[command.id] = CommandRun(command, path.open("wb"), path)
        if busy:
            self._reply(command.id, outcome="error", reason="busy")

    def _on_snapshot_chunk(self, message: dict) -> None:
        try:
            chunk = parse_worker_command(message)
        except ValueError:
            log.warning("ignored a malformed snapshot chunk")
            return
        run = self.runs.get(chunk.id) if isinstance(chunk, SnapshotChunk) else None
        if run is None or run.thread is not None:
            return
        command = run.command
        try:
            data = decode(chunk.data)
            run.received += len(data)
            if run.received > command.snapshot_size:
                raise ValueError("snapshot exceeds its declared size")
            run.file.write(data)
            run.digest.update(data)
            if not chunk.final:
                return
            run.file.close()
            if (
                run.received != command.snapshot_size
                or run.digest.hexdigest() != command.snapshot_sha256
            ):
                raise ValueError("snapshot size or checksum mismatch")
        except (ValueError, OSError) as exc:
            self._drop_run(run)
            self._reply_early(run, outcome="error", reason=str(exc)[:2048])
            return
        thread = threading.Thread(
            target=self._run_command, args=(run,), name=f"command-{run.id[:8]}", daemon=True
        )
        thread.start()
        run.thread = thread  # Only once started, so _execute never joins an unstarted thread.

    def _on_cancel(self, message: dict) -> None:
        try:
            request = parse_worker_command(message)
        except ValueError:
            return
        run = self.runs.get(request.id) if isinstance(request, CancelRequest) else None
        if run is None:
            return
        if run.thread is None:
            self._drop_run(run)  # Nothing ran yet, so the session stays as it was.
            self._reply_early(
                run, outcome="cancelled", reason="cancelled before the command started"
            )
        else:
            run.cancel.set()  # Stops only the command; the session stays usable.

    def _command_output(self, run: CommandRun, stream: str, data: bytes) -> None:
        limit = run.command.output_limit_bytes
        kept = data[: max(0, limit - run.output_bytes)]
        run.output_bytes += len(kept)
        for offset in range(0, len(kept), CHUNK_BYTES):
            piece = kept[offset : offset + CHUNK_BYTES]
            self._relay(Output(id=run.id, stream=stream, data=encode(piece)).model_dump())
        if len(kept) < len(data) and not run.truncated:
            run.truncated = True
            self._relay(Notice(id=run.id, text=f"output truncated at {limit} bytes").model_dump())

    def _session_ended(self, attempt_id: str, outcome: str) -> dict:
        with self.lock:
            reason = self.end_reasons.get(attempt_id, "session closed")
        return {"outcome": outcome, "reason": reason, "session_ended": True}

    def _run_command(self, run: CommandRun) -> None:
        """Apply the snapshot, sync dependencies if they changed, run the command, then write
        back new and modified files; the result is always the last message."""
        command = run.command
        attempt_id = command.attempt_id
        directory = self.root / attempt_id
        workspace = directory / "workspace"
        session_path = directory / "session.json"
        archive = directory / f"writeback-{run.id}.tar.gz"
        with self.lock:
            handle = self.handles[attempt_id]

        session = {"remote": {}}

        def abort(reason: str) -> Callable[[bool], None]:
            # Called for a time limit, or for a cancel whose processes could not be stopped.
            return lambda timed_out: self._end_session(
                attempt_id, reason if timed_out else "the cancelled command could not be stopped"
            )

        def reply(**fields) -> None:
            self._reply(run.id, **fields, **self._remote_only(session))

        try:
            spec = ExecutionSpec.model_validate(self._read(attempt_id)["spec"])
            session = self._session_state(attempt_id)
            try:
                session["files"] = apply_snapshot(
                    workspace,
                    run.path,
                    session["files"],
                    self.settings.max_extracted_bytes,
                    self.settings.max_archive_members,
                )
            except (ValueError, OSError, tarfile.TarError, EOFError) as exc:
                reply(outcome="error", reason=f"snapshot rejected: {exc}"[:2048])
                return
            finally:
                run.path.unlink(missing_ok=True)
            for path in session["files"]:
                session["remote"].pop(path, None)  # The snapshot's copy replaced it.
            write_json(session_path, session)

            # Only the snapshot's dependency files count: a uv.lock that `uv sync` generated
            # in the workspace must neither force --locked nor change the hash.
            deps = dependency_hash(workspace, session["files"])
            pyproject = workspace / "pyproject.toml"
            if pyproject.is_file() and not pyproject.is_symlink() and deps != session["deps"]:
                self._relay(Notice(id=run.id, text="running uv sync").model_dump())
                locked = "uv.lock" in session["files"]
                code, timed_out, stopped = self.executor.exec(
                    spec,
                    ["uv", "sync", "--locked"] if locked else ["uv", "sync"],
                    WORKSPACE,
                    lambda _stream, data: self._command_output(run, "stderr", data),
                    run.cancel,
                    command.sync_timeout_s,
                    on_abort=abort("dependency sync time limit exceeded"),
                )
                # In each case deps is not stored, so the next command syncs again.
                if timed_out or handle["cancel"].is_set():
                    outcome = "timeout" if timed_out else "cancelled"
                    reply(**self._session_ended(attempt_id, outcome))
                    return
                if stopped:
                    reply(outcome="cancelled", reason=CANCELLED)
                    return
                if code is None:
                    reply(outcome="error", reason="uv sync exit status unavailable")
                    return
                if code != 0:
                    reply(outcome="exited", exit_code=code, reason="uv sync failed")
                    return
                session["deps"] = deps
                write_json(session_path, session)

            if not is_directory(workspace, command.cwd):
                reply(
                    outcome="error",
                    reason=f"working directory {command.cwd or '.'} is not in the session "
                    "workspace",
                )
                return
            before = scan(workspace)
            code, timed_out, stopped = self.executor.exec(
                spec,
                command.argv,
                f"{WORKSPACE}/{command.cwd}" if command.cwd else WORKSPACE,
                lambda stream, data: self._command_output(run, stream, data),
                run.cancel,
                command.timeout_s,
                on_abort=abort("command time limit exceeded"),
            )
            if timed_out:
                fields = self._session_ended(attempt_id, "timeout")
            elif handle["cancel"].is_set():
                fields = self._session_ended(attempt_id, "cancelled")
            elif stopped:  # Only the command's processes were stopped; the session stays.
                fields = {"outcome": "cancelled", "reason": CANCELLED}
            elif code is None:
                fields = {"outcome": "error", "reason": "the command's exit status is unavailable"}
            else:
                fields = {"outcome": "exited", "exit_code": code}
            fields.update(self._write_back(run, workspace, before, archive, session))
            write_json(session_path, session)
            fields.setdefault("session_ended", handle["cancel"].is_set())
            reply(**fields)
        except Exception as exc:
            log.exception("fast-lane command failed", extra={"attempt_id": attempt_id})
            reply(outcome="error", reason=f"worker error: {exc}"[:2048])
        finally:
            archive.unlink(missing_ok=True)
            self._drop_run(run)

    def _write_back(
        self, run: CommandRun, workspace: Path, before: dict, archive: Path, session: dict
    ) -> dict:
        """Stream new and modified files, up to the size cap and the archive member limit, and
        return the write-back result fields. session["remote"] becomes the files that now exist
        only here: those over a limit, now or in an earlier command, until they are written back
        or deleted. A listed file is tried again only when a command changes it."""
        after = scan(workspace)
        limit = run.command.writeback_limit_bytes
        remote = set(session["remote"])
        selected, total = [], 0
        for path in sorted(p for p, value in after.items() if before.get(p) != value):
            size = after[path][0]
            if total + size <= limit and len(selected) < self.settings.max_archive_members:
                selected.append(path)
                total += size
            else:
                remote.add(path)
        packed = pack_files(workspace, selected, archive) if selected else []
        remote = (remote & after.keys()) - set(packed)
        session["remote"] = {path: after[path][0] for path in remote}
        if not packed:
            return {}
        digest = hashlib.sha256()
        with archive.open("rb") as stream:
            for text, final in iter_chunks(stream):
                data = decode(text)
                digest.update(data)
                self._relay(WritebackChunk(id=run.id, data=text, final=final).model_dump())
        return {
            "writeback_size": archive.stat().st_size,
            "writeback_sha256": digest.hexdigest(),
            "writeback_files": len(packed),
        }

    # Model services and inference requests (M10, G1) ---------------------------------------
    # Like fast-lane commands, the handlers run on the doorbell thread and never block; each
    # request runs on its own daemon thread and talks to the container over its Docker network.

    def _service_started(self, spec: ExecutionSpec) -> None:
        threading.Thread(
            target=self._await_ready, args=(spec,), name=f"ready-{spec.attempt_id[:8]}", daemon=True
        ).start()

    def _await_ready(self, spec: ExecutionSpec) -> None:
        """Report STARTED once the model server passes its health check (plan 10.7); stop it
        as FAILED when that takes longer than startup_timeout_s. After a Worker restart the
        adopted container gets a full startup window again."""
        attempt_id, service = spec.attempt_id, spec.service
        deadline = time.monotonic() + service.startup_timeout_s
        while True:
            with self.lock:
                handle = self.handles.get(attempt_id)
                running = attempt_id in self.active
            if handle is None or not running or handle["cancel"].is_set():
                return
            try:
                if self._read(attempt_id).get("started"):
                    return  # Adopted after it was already serving.
                response = self.local.get(self._service_url(spec) + service.health_path, timeout=2)
                if response.status_code == 200:
                    log.info("model service is healthy", extra={"attempt_id": attempt_id})
                    self._started(attempt_id)
                    return
            except Exception:
                with self.lock:
                    self.addresses.pop(attempt_id, None)  # E.g. no address assigned yet.
            if time.monotonic() >= deadline:
                with self.lock:
                    self.end_outcomes[attempt_id] = "FAILED"
                self._end_session(
                    attempt_id,
                    f"model server not healthy within {service.startup_timeout_s:.0f}s",
                )
                return
            handle["cancel"].wait(1)

    def _service_spec(self, attempt_id: str) -> ExecutionSpec | None:
        """The spec of a model service that is serving on this worker, or None."""
        with self.lock:
            handle = self.handles.get(attempt_id)
            if handle is None or attempt_id not in self.active or handle["cancel"].is_set():
                return None
            try:
                state = self._read(attempt_id)
            except (OSError, ValueError):
                return None
        if not state.get("started") or not (state.get("spec") or {}).get("service"):
            return None
        return ExecutionSpec.model_validate(state["spec"])

    def _service_url(self, spec: ExecutionSpec) -> str:
        with self.lock:
            address = self.addresses.get(spec.attempt_id)
        if address is None:
            address = self.executor.service_address(spec)
            with self.lock:
                self.addresses[spec.attempt_id] = address
        return address

    def _infer_end(self, request_id: str, error: str | None = None) -> None:
        self.send(InferEnd(id=request_id, error=error).model_dump())

    def _drop_infer(self, run: InferRun) -> None:
        with self.lock:
            self.inference.pop(run.request.id, None)

    def _abort_inference(self, attempt_id: str | None, reason: str) -> None:
        """End the requests of one model service (or all of them, for None)."""
        with self.lock:
            runs = [
                run
                for run in self.inference.values()
                if attempt_id is None or run.request.attempt_id == attempt_id
            ]
        for run in runs:
            self._cancel_infer(run, reason)

    def _cancel_infer(self, run: InferRun, reason: str) -> None:
        run.reason = reason
        run.cancel.set()
        if run.thread is None:
            self._drop_infer(run)
            self._infer_end(run.request.id, reason)
            return
        # Shutting the socket down wakes the thread blocked reading the model's response and
        # makes the model server see the disconnect, so it stops generating; Response.close()
        # from a thread other than the reader does neither until the read times out. Known
        # limit: before the response headers (for a non-streamed request, the whole
        # generation) there is no response to shut down, so the model finishes and the thread
        # then drops the result.
        with self.lock:  # See _run_infer: the connection is not serving another request.
            response = run.response
            if response is None:
                return
            try:
                response.extensions["network_stream"].get_extra_info("socket").shutdown(SHUT_RDWR)
            except Exception:
                try:
                    response.close()
                except Exception:
                    pass

    def _on_infer(self, message: dict) -> None:
        request_id = message.get("id")
        valid_id = isinstance(request_id, str) and re.fullmatch(r"[0-9a-f]{32}", request_id)
        try:
            request = parse_to_worker(message)
            if not isinstance(request, InferRequest):
                raise ValueError("not an inference request")
        except ValueError as exc:
            if valid_id:
                self._infer_end(request_id, f"invalid request: {exc}"[:2048])
            return
        if request.body_size > self.settings.gateway_max_request_bytes:
            self._infer_end(request.id, "request body too large")
            return
        if self._service_spec(request.attempt_id) is None:
            self._infer_end(request.id, "the model is not serving on this worker")
            return
        with self.lock:
            self.inference[request.id] = InferRun(request)

    def _on_infer_body(self, message: dict) -> None:
        try:
            chunk = parse_to_worker(message)
        except ValueError:
            log.warning("ignored a malformed inference body chunk")
            return
        run = self.inference.get(chunk.id) if isinstance(chunk, InferBody) else None
        if run is None or run.thread is not None:
            return
        try:
            run.body += decode(chunk.data)
            size = run.request.body_size
            if len(run.body) > size or (chunk.final and len(run.body) != size):
                raise ValueError("request body does not match its declared size")
        except ValueError as exc:
            self._drop_infer(run)
            self._infer_end(run.request.id, str(exc))
            return
        if not chunk.final:
            return
        thread = threading.Thread(
            target=self._run_infer, args=(run,), name=f"infer-{run.request.id[:8]}", daemon=True
        )
        thread.start()
        run.thread = thread

    def _on_infer_cancel(self, message: dict) -> None:
        try:
            request = parse_to_worker(message)
        except ValueError:
            return
        run = self.inference.get(request.id) if isinstance(request, InferCancel) else None
        if run is not None:
            self._cancel_infer(run, "cancelled by the client")

    def _run_infer(self, run: InferRun) -> None:
        """Send the request to the model server and stream its response back as it arrives."""
        request = run.request
        error = None
        complete = False  # The model's whole response was relayed.
        try:
            spec = self._service_spec(request.attempt_id)
            if spec is None:
                error = "the model is not serving on this worker"
                return
            with self.local.stream(
                "POST",
                f"{self._service_url(spec)}/v1/{request.path}",
                content=bytes(run.body),
                headers={"Content-Type": request.content_type, "Accept-Encoding": "identity"},
                timeout=httpx.Timeout(request.read_timeout_s, connect=10),
            ) as response:
                run.response = response
                try:
                    content_type = response.headers.get("content-type", "application/json")
                    head = InferHead(
                        id=request.id, status=response.status_code, content_type=content_type[:256]
                    )
                    if run.cancel.is_set() or not self.send(head.model_dump()):
                        return
                    # iter_raw without a size yields bytes as they arrive (one token at a time).
                    for data in response.iter_raw():
                        for offset in range(0, len(data), CHUNK_BYTES):
                            piece = encode(data[offset : offset + CHUNK_BYTES])
                            chunk = InferChunk(id=request.id, data=piece).model_dump()
                            if run.cancel.is_set() or not self.send(chunk):
                                return
                    # A cancel's socket shutdown can end the body just like the model finishing.
                    complete = not run.cancel.is_set()
                finally:
                    # Cleared before the connection can go back to the pool and serve another
                    # request, so a late cancel never shuts that request's socket down.
                    with self.lock:
                        run.response = None
        except Exception as exc:
            if not run.cancel.is_set():
                error = f"model request failed: {type(exc).__name__}: {exc}"[:2048]
        finally:
            self._drop_infer(run)
            if not complete and error is None:
                # Cancelled, or a send failed (then this end most likely fails too).
                error = run.reason if run.cancel.is_set() else "relay connection lost"
            self._infer_end(request.id, error)

    def run(self) -> None:
        self.executor.check()
        self.register()
        heartbeat = threading.Thread(target=self._heartbeats, name="heartbeats", daemon=True)
        outbox = threading.Thread(target=self._outbox, name="outbox", daemon=True)
        doorbell = threading.Thread(target=self._doorbell, name="doorbell", daemon=True)
        heartbeat.start()
        outbox.start()
        doorbell.start()
        log.info("worker registered", extra={"job_id": None, "attempt_id": None})
        try:
            while not self.stop_event.is_set():
                # Cleared before polling, so a ring arriving mid-poll triggers another poll.
                self.commands_ready.clear()
                try:
                    response = self.client.get(
                        self._url("commands"), params={"instance_id": self.instance_id}
                    )
                    response.raise_for_status()
                    commands = response.json()
                    if isinstance(commands, list):
                        self.process(commands)
                    else:
                        log.error("command poll returned a non-list response")
                except httpx.HTTPError as exc:
                    log.warning("command polling will retry: %s", type(exc).__name__)
                self.commands_ready.wait(self.settings.poll_interval_s)
        finally:
            # Job containers keep running; the next instance adopts them when it registers.
            self.stop_event.set()
            self.pool.shutdown(wait=True)
            heartbeat.join(timeout=self.settings.request_timeout_s + 1)
            outbox.join(timeout=self.settings.request_timeout_s + 1)
            doorbell.join(timeout=self.settings.request_timeout_s + 2)
            try:
                self.flush()
            except httpx.HTTPError:
                log.warning("final events remain in the local outbox")
            self.client.close()
            self.local.close()
            self.gpus.close()
