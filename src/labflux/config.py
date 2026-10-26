"""Shared configuration. Explicit environment overrides never print credentials."""

from __future__ import annotations

import os
import socket
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    poll_interval_s: float = Field(default=0.25, gt=0)
    request_timeout_s: float = Field(default=3, gt=0)
    job_time_limit_s: float = Field(default=3600, gt=0)
    stop_grace_s: int = Field(default=2, ge=0)
    memory_reserve_mib: int = Field(default=512, ge=0)
    allocatable_cpu: float | None = Field(default=None, gt=0)
    allocatable_memory_mib: int | None = Field(default=None, ge=16)
    datasets: dict[str, Path] = Field(default_factory=dict)
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
        self.data_dir = self.data_dir.expanduser().resolve()
        self.datasets = {name: path.expanduser().resolve() for name, path in self.datasets.items()}
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
