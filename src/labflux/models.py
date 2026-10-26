"""Validated values shared by the API, scheduler, and worker."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ResourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cpu: float = Field(default=1, ge=0.01, allow_inf_nan=False)
    memory_mib: int = Field(default=256, ge=16)


class JobSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image: str = Field(min_length=1, max_length=512)
    argv: list[str] = Field(min_length=1, max_length=256)
    env: dict[str, str] = Field(default_factory=dict)
    resources: ResourceRequest = Field(default_factory=ResourceRequest)
    time_limit_s: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    source_id: str | None = None
    datasets: list[str] = Field(default_factory=list)

    @field_validator("argv")
    @classmethod
    def valid_argv(cls, values: list[str]) -> list[str]:
        if not values[0] or any("\x00" in value for value in values):
            raise ValueError("argv needs an executable and cannot contain NUL bytes")
        return values

    @field_validator("env")
    @classmethod
    def valid_env(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not k or "=" in k or "\x00" in k + v for k, v in values.items()):
            raise ValueError("invalid environment variable")
        return values

    @field_validator("datasets")
    @classmethod
    def valid_datasets(cls, values: list[str]) -> list[str]:
        import re

        if len(values) != len(set(values)) or any(
            not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", value) or value in {".", ".."}
            for value in values
        ):
            raise ValueError("datasets require distinct, safe names")
        return values


class ExecutionSpec(BaseModel):
    job_id: str
    attempt_id: str
    worker_id: str
    job: JobSpec


class Capacity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total_cpu: float = Field(gt=0, allow_inf_nan=False)
    total_memory_mib: int = Field(ge=16)
    cpu: float = Field(gt=0, allow_inf_nan=False)
    memory_mib: int = Field(ge=16)
    datasets: list[str] = Field(default_factory=list)


class WorkerRegistration(BaseModel):
    worker_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    instance_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    capacity: Capacity


class Heartbeat(BaseModel):
    instance_id: str
    observed: dict[str, float] = Field(default_factory=dict)


class WorkerEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    instance_id: str
    kind: Literal["STARTED", "FINISHED"]
    exit_code: int | None = None
    outcome: Literal["SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"] | None = None
    reason: str = Field(default="", max_length=2048)
    termination_confirmed: bool = False
    oom_killed: bool = False


class LogUpload(BaseModel):
    instance_id: str
    seq: int = Field(ge=0)
    stream: Literal["stdout", "stderr"]
    data_base64: str = Field(max_length=90000)
