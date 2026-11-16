"""Validated values shared by the API, scheduler, and worker."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GPU_UUID = r"^GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
# Model services (M10): a job row of this kind whose container is the model server. They are
# started by the Scheduler, not by a user, so they belong to this owner, which no token maps to.
MODEL_KIND = "MODEL"
MODEL_OWNER = "@models"
MODEL_NAME = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$"
ATTEMPT_ID = r"^[a-f0-9]{32}$"
WORKER_ID = r"^[a-zA-Z0-9_.-]{1,64}$"
# A project name (P1): only a database key, never a path, so any printable text is allowed.
PROJECT_NAME = r"^[^\x00-\x1f\x7f]{1,128}$"


class ResourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cpu: float = Field(default=1, ge=0.01, allow_inf_nan=False)
    memory_mib: int = Field(default=256, ge=16)
    gpu: int = Field(default=0, ge=0, le=8)
    gpu_model: str | None = Field(default=None, min_length=1, max_length=64)
    min_vram_mib: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def gpu_filters_need_gpus(self) -> ResourceRequest:
        if not self.gpu and (self.gpu_model is not None or self.min_vram_mib is not None):
            raise ValueError("gpu_model and min_vram_mib require gpu >= 1")
        return self


class JobSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # None means the Scheduler's default_image, filled in at submission.
    image: str | None = Field(default=None, min_length=1, max_length=512)
    argv: list[str] = Field(min_length=1, max_length=256)
    env: dict[str, str] = Field(default_factory=dict)
    resources: ResourceRequest = Field(default_factory=ResourceRequest)
    time_limit_s: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    # Batch priority 0-9; None means the Scheduler's default_priority. Ignored by fcfs.
    priority: int | None = Field(default=None, ge=0, le=9)
    # Advisory only: shown in explanations; reservations and backfilling use time_limit_s.
    estimated_runtime_s: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    source_id: str | None = None
    datasets: list[str] = Field(default_factory=list)
    # Running the program again from the start is harmless (plan: "restartable"). Required by
    # preemptible and by retries_on_node_failure > 0.
    safe_to_rerun: bool = False
    # The Scheduler may stop it for more important work and queue it to run again (M11).
    preemptible: bool = False
    # New attempts allowed after an infrastructure loss (the node or its container was lost);
    # cancellation, time limits and program exits are never retried. Formerly max_retries.
    retries_on_node_failure: int = Field(default=0, ge=0, le=10)

    # Owner + project identify a project (6.1 P1). The job's project directory is kept on its
    # node afterwards, and the next run of the project on that node starts from its files.
    # None keeps the old behavior: the directory is deleted and nothing is copied.
    project: str | None = Field(default=None, pattern=PROJECT_NAME)

    @model_validator(mode="before")
    @classmethod
    def legacy_max_retries(cls, data):
        """Accept specs stored before M11 (SQLite rows, Worker state.json) that use max_retries.
        Before M11 any retry implied that rerunning was safe, so a value > 0 also sets
        safe_to_rerun unless the spec gives it."""
        if isinstance(data, dict) and "max_retries" in data:
            if "retries_on_node_failure" in data:
                raise ValueError(
                    "give only retries_on_node_failure; max_retries is its former name"
                )
            data = dict(data)
            value = data.pop("max_retries")
            data["retries_on_node_failure"] = value
            if "safe_to_rerun" not in data and isinstance(value, int) and value > 0:
                data["safe_to_rerun"] = True
        return data

    @model_validator(mode="after")
    def rerun_flags_need_safe_to_rerun(self) -> JobSpec:
        if self.preemptible and not self.safe_to_rerun:
            raise ValueError("preemptible requires safe_to_rerun: true")
        if self.retries_on_node_failure and not self.safe_to_rerun:
            raise ValueError("retries_on_node_failure > 0 requires safe_to_rerun: true")
        return self

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
        if "NVIDIA_VISIBLE_DEVICES" in values:
            raise ValueError(
                "NVIDIA_VISIBLE_DEVICES is managed by LabFlux; request GPUs with resources.gpu"
            )
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


class ServiceSpec(BaseModel):
    """How the Worker runs and probes a model server container (M10)."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=MODEL_NAME)
    port: int = Field(default=8000, ge=1, le=65535)
    health_path: str = Field(default="/health", pattern=r"^/[!-~]*$", max_length=256)
    # From container start until the health check first passes; the Worker then reports STARTED.
    startup_timeout_s: float = Field(default=300, gt=0, allow_inf_nan=False)
    shm_mib: int = Field(default=1024, ge=64)
    pids_limit: int = Field(default=4096, ge=64)


class ExecutionSpec(BaseModel):
    job_id: str
    attempt_id: str
    worker_id: str
    job: JobSpec
    gpu_uuids: list[Annotated[str, Field(pattern=GPU_UUID)]] = Field(default_factory=list)
    # A fast-lane session container (M9): kept alive for commands, no time limit enforced.
    session: bool = False
    # A model server container (M10): no time limit; STARTED is reported once it is healthy.
    service: ServiceSpec | None = None
    # A kept project directory on this node to start from (6.1 P0b): its generated files are
    # copied into the new workspace before the snapshot is applied.
    previous_attempt_id: str | None = Field(default=None, pattern=ATTEMPT_ID)


class GpuDevice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uuid: str = Field(pattern=GPU_UUID)
    index: int = Field(ge=0)
    name: str = Field(min_length=1, max_length=128)
    memory_total_mib: int = Field(ge=1)


class Capacity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total_cpu: float = Field(gt=0, allow_inf_nan=False)
    total_memory_mib: int = Field(ge=16)
    cpu: float = Field(gt=0, allow_inf_nan=False)
    memory_mib: int = Field(ge=16)
    datasets: list[str] = Field(default_factory=list)
    gpus: list[GpuDevice] = Field(default_factory=list)
    gpu_driver_version: str | None = Field(default=None, max_length=64)
    cuda_driver_version: str | None = Field(default=None, max_length=64)

    @field_validator("gpus")
    @classmethod
    def distinct_gpus(cls, values: list[GpuDevice]) -> list[GpuDevice]:
        if len(values) != len({g.uuid for g in values}) or len(values) != len(
            {g.index for g in values}
        ):
            raise ValueError("GPU UUIDs and indexes must be distinct")
        return values


class InventoryAttempt(BaseModel):
    """A local attempt record that may still matter to the Scheduler."""

    model_config = ConfigDict(extra="forbid")
    attempt_id: str = Field(pattern=ATTEMPT_ID)
    # False only while no container can have been created for the attempt.
    launched: bool = True
    # A confirmed result that the outbox has not delivered yet.
    unsent_result: bool = False


class InventoryContainer(BaseModel):
    """A Docker container carrying this worker's LabFlux labels."""

    model_config = ConfigDict(extra="forbid")
    attempt_id: str = Field(pattern=ATTEMPT_ID)
    worker_id: str = Field(pattern=WORKER_ID)
    status: str = Field(min_length=1, max_length=32)
    exit_code: int | None = None
    oom_killed: bool = False
    started_at: float | None = None


class Inventory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempts: list[InventoryAttempt] = Field(default_factory=list, max_length=10000)
    containers: list[InventoryContainer] = Field(default_factory=list, max_length=10000)


class WorkerRegistration(BaseModel):
    worker_id: str = Field(pattern=WORKER_ID)
    instance_id: str = Field(pattern=ATTEMPT_ID)
    capacity: Capacity
    # Sent by restarting workers; None keeps the old refusal for unreleased attempts.
    inventory: Inventory | None = None


class GpuObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uuid: str = Field(pattern=GPU_UUID)
    memory_used_mib: int = Field(ge=0)
    utilization_percent: float = Field(ge=0, le=100, allow_inf_nan=False)
    external_use: bool


class Heartbeat(BaseModel):
    instance_id: str
    observed: dict[str, float] = Field(default_factory=dict)
    gpus: list[GpuObservation] = Field(default_factory=list)
    # Attempts this instance is executing right now; lets a lapsed node resume them.
    active_attempts: list[Annotated[str, Field(pattern=ATTEMPT_ID)]] = Field(
        default_factory=list, max_length=10000
    )


class WorkerEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    instance_id: str
    kind: Literal["STARTED", "FINISHED"]
    exit_code: int | None = None
    outcome: Literal["SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"] | None = None
    reason: str = Field(default="", max_length=2048)
    termination_confirmed: bool = False
    oom_killed: bool = False
    # FINISHED of a project job only: the attempt's directory stays on the node (6.1 P0a) and
    # holds this many bytes; None means it was deleted.
    kept_bytes: int | None = Field(default=None, ge=0)


class LogUpload(BaseModel):
    instance_id: str
    seq: int = Field(ge=0)
    stream: Literal["stdout", "stderr"]
    data_base64: str = Field(max_length=90000)
