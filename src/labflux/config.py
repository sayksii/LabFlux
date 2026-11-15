"""Shared configuration. Explicit environment overrides never print credentials."""

from __future__ import annotations

import os
import re
import socket
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from labflux.models import GPU_UUID
from labflux.session_protocol import (
    DEFAULT_COMMAND_TIMEOUT_S,
    DEFAULT_LEASE_S,
    DEFAULT_SYNC_TIMEOUT_S,
    DEFAULT_WRITEBACK_BYTES,
    FastLaneMode,
)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_dir: Path = Path("data")
    scheduler_url: str = "http://127.0.0.1:8000"
    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=8000, ge=1, le=65535)
    users: dict[str, str] = Field(default_factory=lambda: {"demo": "dev-user-token"})
    user_token: str = "dev-user-token"
    worker_token: str = "dev-worker-token"
    worker_id: str = Field(default_factory=socket.gethostname, pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    heartbeat_interval_s: float = Field(default=2, gt=0)
    offline_timeout_s: float = Field(default=10, gt=0)
    scheduler_interval_s: float = Field(default=0.25, gt=0)
    # Worker fallback for command polls and outbox flushes when no wake-up arrives (T4, T8).
    poll_interval_s: float = Field(default=5, gt=0)
    request_timeout_s: float = Field(default=3, gt=0)
    job_time_limit_s: float = Field(default=3600, gt=0)
    # fcfs: strict submission order. priority: score order (priority, waiting bonus, recent
    # usage penalty). backfill: priority plus EASY backfilling behind a head-job reservation.
    scheduling_policy: Literal["fcfs", "priority", "backfill"] = "fcfs"
    default_priority: int = Field(default=5, ge=0, le=9)
    # score = priority * w_priority + queued minutes * w_wait - recent usage minutes * w_usage
    score_priority_weight: float = Field(default=100, ge=0)
    score_wait_weight: float = Field(default=1, ge=0)
    score_usage_weight: float = Field(default=1, ge=0)
    # Recent usage: reserved minutes in this window, scaled by the dominant share of the cluster.
    usage_window_s: float = Field(default=3600, gt=0)
    stop_grace_s: int = Field(default=2, ge=0)
    memory_reserve_mib: int = Field(default=512, ge=0)
    allocatable_cpu: float | None = Field(default=None, gt=0)
    allocatable_memory_mib: int | None = Field(default=None, ge=16)
    datasets: dict[str, Path] = Field(default_factory=dict)
    # NVML indexes or "GPU-..." UUIDs this worker may allocate; empty pools no GPU.
    gpus: list[StrictInt | StrictStr] = Field(default_factory=list)
    gpu_external_memory_mib: int = Field(default=1024, ge=1)
    max_source_bytes: int = Field(default=100 * 1024**2, gt=0)
    max_output_bytes: int = Field(default=1024**3, gt=0)
    max_extracted_bytes: int = Field(default=1024**3, gt=0)
    max_archive_members: int = Field(default=10000, gt=0)
    max_parallel_jobs: int = Field(default=32, ge=1)
    log_chunk_bytes: int = Field(default=16384, ge=1, le=65536)
    max_log_bytes: int = Field(default=16 * 1024**2, ge=1024)
    image_pull_timeout_s: int = Field(default=300, ge=1)
    runtime_poll_s: float = Field(default=0.1, gt=0)
    docker_image: str = "python:3.12-slim"
    default_image: str = Field(
        default="localhost/labflux/python-uv:3.12-uv0.11.8", min_length=1, max_length=512
    )
    # Worker-local uv cache mounted into every job at /uv-cache; None mounts nothing.
    uv_cache_dir: Path | None = None
    # Fast lane (M9). session: commands reuse the user's session; oneshot: the Scheduler closes
    # the session after each command, so every command gets a fresh container.
    fast_lane_mode: FastLaneMode = "session"
    # Idle time without commands after which a session is closed; every command renews it.
    session_lease_s: float = Field(default=DEFAULT_LEASE_S, gt=0)
    # A queued session is cancelled when its client has not asked about it for this long.
    session_queue_timeout_s: float = Field(default=30, gt=0)
    session_command_timeout_s: float = Field(default=DEFAULT_COMMAND_TIMEOUT_S, gt=0)
    # `uv sync` before a command when pyproject.toml or uv.lock changed.
    session_sync_timeout_s: float = Field(default=DEFAULT_SYNC_TIMEOUT_S, gt=0)
    # Raw file bytes written back per command; command output reuses max_log_bytes and the
    # snapshot reuses max_source_bytes.
    session_writeback_bytes: int = Field(default=DEFAULT_WRITEBACK_BYTES, ge=0)

    @model_validator(mode="after")
    def validate_settings(self) -> Settings:
        if self.offline_timeout_s <= self.heartbeat_interval_s:
            raise ValueError("offline_timeout_s must exceed heartbeat_interval_s")
        if not self.users or any(not user or not token for user, token in self.users.items()):
            raise ValueError("users must map nonempty identities to nonempty tokens")
        if len(set(self.users.values())) != len(self.users):
            raise ValueError("each user must have a distinct token")
        if not self.worker_token or self.worker_token in self.users.values():
            raise ValueError("worker_token must be distinct from user tokens")
        for gpu in self.gpus:
            if not re.fullmatch(GPU_UUID, gpu) if isinstance(gpu, str) else gpu < 0:
                raise ValueError(f"gpus entries must be NVML indexes or GPU UUIDs: {gpu!r}")
        self.data_dir = self.data_dir.expanduser().resolve()
        self.datasets = {name: path.expanduser().resolve() for name, path in self.datasets.items()}
        if self.uv_cache_dir is not None:
            self.uv_cache_dir = self.uv_cache_dir.expanduser().resolve()
        return self


def load_settings(path: Path | None = None) -> Settings:
    chosen = path or (Path(os.environ["LABFLUX_CONFIG"]) if os.getenv("LABFLUX_CONFIG") else None)
    values = {}
    if chosen:
        with chosen.open("rb") as stream:
            values = tomllib.load(stream)
    for field in ("data_dir", "scheduler_url", "user_token", "worker_token", "worker_id"):
        override = os.getenv(f"LABFLUX_{field.upper()}")
        if override is not None:
            values[field] = override
    return Settings.model_validate(values)
