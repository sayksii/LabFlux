"""Fast-lane session protocol (M9): messages and constants shared by the CLI, Scheduler and Worker.

A command travels CLI -> Scheduler over a per-command WebSocket (user token), and Scheduler ->
Worker over the Worker's doorbell connection (T6). Everything here is relayed in memory; only
session bookkeeping is written to the database. Binary payloads (the D4b snapshot, the D12
write-back archive and command output) are base64 text inside JSON messages, at most CHUNK_BYTES
of raw data per message, so every message stays well below the websockets client default limit
of 1 MiB.

Per command, in order:
  CLI -> Scheduler:    ExecRequest, SnapshotChunk..., [CancelRequest]
  Scheduler -> Worker: WorkerExec, SnapshotChunk..., [CancelRequest]   (all carry the command id)
  Worker -> Scheduler: Output/Notice..., WritebackChunk..., CommandResult (last)
  Scheduler -> CLI:    the Worker messages unchanged, or one ErrorMessage instead of a result
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Annotated, BinaryIO, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from labflux.models import ResourceRequest

PROTOCOL_VERSION = 1

# Defaults for the Scheduler Settings fields of the same meaning.
FastLaneMode = Literal["session", "oneshot"]
DEFAULT_LEASE_S = 600.0  # Idle lease; renewed when a command starts and when it ends.
DEFAULT_COMMAND_TIMEOUT_S = 60.0
DEFAULT_SYNC_TIMEOUT_S = 900.0  # `uv sync` before a command; downloads can exceed 60 s.
DEFAULT_WRITEBACK_BYTES = 100 * 1024**2  # Raw file bytes written back per command.
WRITEBACK_OVERHEAD_BYTES = 16 * 1024**2  # Tar headers and gzip slack on top of the cap.
MAX_SKIPPED_LISTED = 100

CHUNK_BYTES = 256 * 1024
CHUNK_TEXT_LIMIT = 4 * -(-CHUNK_BYTES // 3)  # base64 length of one full chunk.

# Session container (the session attempt's START payload and its docker exec calls).
KEEPALIVE_ARGV = ("sleep", "infinity")
WORKSPACE = "/workspace"
# PATH is set by the Worker: /workspace/.venv/bin in front of the image's own PATH.
SESSION_ENV = {"VIRTUAL_ENV": "/workspace/.venv"}
DEPENDENCY_FILES = ("pyproject.toml", "uv.lock")  # A change triggers `uv sync` first.

# Session states are the states of its job row (jobs.kind = "SESSION").
SESSION_KIND = "SESSION"
SESSION_LIVE_STATES = frozenset({"QUEUED", "STARTING", "RUNNING"})  # One per user.
SESSION_READY_STATE = "RUNNING"

# Change detection and write-back (D12) skip these directory names at any depth.
WRITEBACK_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".ipynb_checkpoints",
        ".labflux",
    }
)
WRITEBACK_EXCLUDED_SUFFIXES = (".pyc",)
REMOTE_SUFFIX = ".remote"  # Conflict copy: <name>.remote next to the local file.

# Client routing (T7).
PROJECT_FILE = ".labflux.toml"
DEFAULT_ROUTED_COMMANDS = (
    "python",
    "python3",
    "pytest",
    "torchrun",
    "accelerate",
    "deepspeed",
    "nvidia-smi",
)
UV_COMMAND = "uv"
UV_ROUTED_SUBCOMMAND = "run"
STANDIN_MARKER = ".labflux-standins"  # Present in every stand-in directory.

# CLI exit statuses besides the remote command's own exit code.
EXIT_TIMEOUT = 124
EXIT_ERROR = 125
EXIT_NOT_FOUND = 127
EXIT_CANCELLED = 130

CommandId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
OptionalCommandId = Annotated[str, Field(pattern=r"^([0-9a-f]{32})?$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ChunkText = Annotated[str, Field(max_length=CHUNK_TEXT_LIMIT)]
ErrorCode = Literal[
    "invalid",  # Malformed or out-of-order message.
    "not_found",  # No such session for this user.
    "not_ready",  # The session is not RUNNING (queued, starting, ending or ended).
    "busy",  # Another command is running in this session; the CLI waits and tries again.
    "too_large",  # Snapshot above max_source_bytes, or write-back above its cap.
    "worker_unavailable",  # The session's Worker has no relay connection right now.
    "worker_disconnected",  # The relay dropped during the command; the session is closed.
    "lost",  # No result within the Scheduler's guard time; the session is closed.
]


def safe_relative(value: str) -> str:
    """Normalize a project-relative POSIX path; "" means the root. Raises ValueError."""
    if "\x00" in value or "\\" in value:
        raise ValueError("path cannot contain NUL or backslashes")
    path = PurePosixPath(value or ".")
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("path must stay inside the project")
    text = path.as_posix()
    return "" if text == "." else text


def check_argv(values: list[str]) -> list[str]:
    """The JobSpec argv rules plus a total-length cap; raises ValueError."""
    if not values[0] or any("\x00" in value for value in values):
        raise ValueError("argv needs an executable and cannot contain NUL bytes")
    if sum(len(value) for value in values) > 128 * 1024:
        raise ValueError("argv is too long")
    return values


def writeback_excluded(relative: str) -> bool:
    """Whether a workspace-relative file path is skipped by change detection and write-back."""
    parts = PurePosixPath(relative).parts
    return any(part in WRITEBACK_EXCLUDED_DIRS for part in parts) or relative.endswith(
        WRITEBACK_EXCLUDED_SUFFIXES
    )


def encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decode(text: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except binascii.Error as exc:
        raise ValueError("invalid base64 data") from exc


def iter_chunks(stream: BinaryIO, size: int = CHUNK_BYTES) -> Iterator[tuple[str, bool]]:
    """Yield (base64 text, final) for a binary stream; an empty stream yields one final chunk."""
    current = stream.read(size)
    while True:
        following = stream.read(size) if current else b""
        final = not following
        yield encode(current), final
        if final:
            return
        current = following


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")


# HTTP bodies --------------------------------------------------------------------------------


class SessionRequest(Message):
    """POST /api/sessions: lease the user's session, or return the live one."""

    resources: ResourceRequest = Field(default_factory=ResourceRequest)
    # None means the Scheduler's default_image.
    image: str | None = Field(default=None, min_length=1, max_length=512)
    # Mounted read-only at /datasets/<name>; the Scheduler validates names as for JobSpec.
    datasets: list[str] = Field(default_factory=list, max_length=32)


class SessionView(BaseModel):
    """A session as returned by the session endpoints."""

    id: str
    state: str
    reason: str
    node_id: str | None = None
    resources: ResourceRequest
    image: str
    datasets: list[str] = Field(default_factory=list)
    lease_expires_at: float | None = None
    created_at: float
    mode: FastLaneMode
    created: bool = False  # True only in the POST response that created it.


# CLI -> Scheduler ----------------------------------------------------------------------------


class ExecRequest(Message):
    """First message on /api/sessions/{id}/ws; the snapshot chunks follow."""

    type: Literal["exec"] = "exec"
    argv: list[str] = Field(min_length=1, max_length=256)
    cwd: str = ""  # Relative to the project root, which is /workspace remotely.
    snapshot_size: int = Field(ge=0)
    snapshot_sha256: Sha256

    _argv = field_validator("argv")(lambda values: check_argv(values))
    _cwd = field_validator("cwd")(lambda value: safe_relative(value))


class SnapshotChunk(Message):
    type: Literal["snapshot_chunk"] = "snapshot_chunk"
    id: OptionalCommandId = ""  # Empty from the CLI; the Scheduler fills in the command id.
    data: ChunkText
    final: bool


class CancelRequest(Message):
    """Ctrl-C, or a dropped client connection: stops only the command's processes; the session
    container stays, and the output and files written so far are kept."""

    type: Literal["cancel"] = "cancel"
    id: OptionalCommandId = ""


# Scheduler -> Worker -------------------------------------------------------------------------


class WorkerExec(Message):
    type: Literal["exec"] = "exec"
    id: CommandId
    attempt_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    argv: list[str] = Field(min_length=1, max_length=256)
    cwd: str = ""
    snapshot_size: int = Field(ge=0)
    snapshot_sha256: Sha256
    timeout_s: float = Field(gt=0)
    sync_timeout_s: float = Field(gt=0)
    writeback_limit_bytes: int = Field(ge=0)
    output_limit_bytes: int = Field(ge=0)

    _argv = field_validator("argv")(lambda values: check_argv(values))
    _cwd = field_validator("cwd")(lambda value: safe_relative(value))


# Worker -> Scheduler -> CLI ------------------------------------------------------------------


class Output(Message):
    type: Literal["output"] = "output"
    id: CommandId
    stream: Literal["stdout", "stderr"]
    data: ChunkText


class Notice(Message):
    """One line for the user's terminal (stderr), e.g. "running uv sync" or a truncation."""

    type: Literal["notice"] = "notice"
    id: CommandId
    text: str = Field(max_length=2048)


class WritebackChunk(Message):
    type: Literal["writeback_chunk"] = "writeback_chunk"
    id: CommandId
    data: ChunkText
    final: bool


class SkippedFile(Message):
    path: str = Field(max_length=4096)
    size: int = Field(ge=0)


class CommandResult(Message):
    """Last message of a command; any write-back chunks were sent before it."""

    type: Literal["result"] = "result"
    id: CommandId
    # exited: the process ended on its own; timeout: the session container was terminated;
    # cancelled: only the command was stopped (session_ended is false) unless its processes
    # could not be stopped; error: LabFlux could not run it (bad snapshot, uv sync start, ...).
    outcome: Literal["exited", "timeout", "cancelled", "error"]
    exit_code: int | None = None
    reason: str = Field(default="", max_length=2048)
    # The session is gone with its workspace; in one-shot mode the Scheduler sets this on
    # every result it relays.
    session_ended: bool = False
    writeback_size: int = Field(default=0, ge=0)
    writeback_sha256: Sha256 | None = None
    writeback_files: int = Field(default=0, ge=0)
    # Every file that currently exists only in the remote session workspace because it was
    # over the write-back limit (cumulative across the session's commands, not just this one):
    # a file leaves the list when it is deleted remotely or later written back. The client
    # mirrors this list in .labflux/remote-files.txt. At most MAX_SKIPPED_LISTED are listed.
    skipped: list[SkippedFile] = Field(default_factory=list, max_length=MAX_SKIPPED_LISTED)
    skipped_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def consistent(self) -> CommandResult:
        if self.outcome == "exited" and self.exit_code is None:
            raise ValueError("an exited command needs its exit code")
        if self.writeback_files and self.writeback_sha256 is None:
            raise ValueError("a write-back needs its checksum")
        return self


class ErrorMessage(Message):
    """Scheduler -> CLI only, in place of a CommandResult; the socket closes after it."""

    type: Literal["error"] = "error"
    code: ErrorCode
    message: str = Field(max_length=2048)


ClientMessage = Annotated[ExecRequest | SnapshotChunk | CancelRequest, Field(discriminator="type")]
WorkerCommandMessage = Annotated[
    WorkerExec | SnapshotChunk | CancelRequest, Field(discriminator="type")
]
WorkerMessage = Annotated[
    Output | Notice | WritebackChunk | CommandResult, Field(discriminator="type")
]
ServerMessage = Annotated[
    Output | Notice | WritebackChunk | CommandResult | ErrorMessage, Field(discriminator="type")
]

_client = TypeAdapter(ClientMessage)
_worker_command = TypeAdapter(WorkerCommandMessage)
_worker = TypeAdapter(WorkerMessage)
_server = TypeAdapter(ServerMessage)


def parse_client_message(value: object) -> ExecRequest | SnapshotChunk | CancelRequest:
    return _client.validate_python(value)


def parse_worker_command(value: object) -> WorkerExec | SnapshotChunk | CancelRequest:
    return _worker_command.validate_python(value)


def parse_worker_message(value: object) -> Output | Notice | WritebackChunk | CommandResult:
    return _worker.validate_python(value)


def parse_server_message(
    value: object,
) -> Output | Notice | WritebackChunk | CommandResult | ErrorMessage:
    return _server.validate_python(value)


def exit_status(message: CommandResult | ErrorMessage) -> int:
    """The CLI's exit status: the remote exit code, or 124 / 130 / 125 like timeout(1) and
    docker run."""
    if isinstance(message, ErrorMessage):
        return EXIT_ERROR
    if message.outcome == "exited":
        return message.exit_code & 0xFF
    return {"timeout": EXIT_TIMEOUT, "cancelled": EXIT_CANCELLED}.get(message.outcome, EXIT_ERROR)
