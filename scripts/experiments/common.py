"""Shared pieces of the M13 experiment tools: workload files, job specs and raw run files.

A workload (experiments/workloads/*.toml) lists jobs with a submission offset, a logical user
(alice, bob, ...), resources, time limit, priority, preemptibility, the real run time and the
kind of load. The replay tool (lab) and the simulator (fake clock) both turn it into the same
JobSpec, so a simulated run and a lab run of one workload are directly comparable.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKLOADS = ROOT / "experiments" / "workloads"
EVIDENCE = ROOT / "docs" / "evidence" / "m13"
LOAD_PROGRAM = (Path(__file__).parent / "m13_load.py").read_text()

CPU_IMAGE = "python:3.12-slim"
# CUDA 12.4 wheels run on kayle's driver 535 through minor-version compatibility, as the
# uv-gpu example already showed on the lab.
GPU_IMAGE = "pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime"
LOGICAL_USERS = ("alice", "bob", "carol", "dave")
# Experiment capacity: what LabFlux may allocate on each lab node minus the persistent gemma
# service on kindred (8 CPU, 48 GiB, both A40s). Utilization is measured against this.
CAPACITY = {
    "kayle": {"cpu": 16, "memory_mib": 49152, "gpu": 2},
    "karma": {"cpu": 16, "memory_mib": 24576, "gpu": 1},
    "kindred": {"cpu": 24, "memory_mib": 16384, "gpu": 0},
}


def account(user: str, user_set: int) -> str:
    """The Scheduler account for a logical user in one rotation set, e.g. m13s2-alice.

    Sets rotate between runs so that the fairness score's recent usage of one run does not leak
    into the next one under the priority and backfill policies."""
    return f"m13s{user_set}-{user}"


def logical(owner: str) -> str:
    return owner.split("-", 1)[1] if owner.startswith("m13s") else owner


@dataclass
class WorkloadJob:
    id: str
    at: float
    user: str
    cpu: float
    memory_mib: int
    run_s: float
    time_limit_s: float
    gpu: int = 0
    gpu_model: str | None = None
    priority: int = 5
    preemptible: bool = False
    retries_on_node_failure: int = 0
    load: str = "sleep"
    estimate_s: float | None = None


@dataclass
class Workload:
    name: str
    description: str
    jobs: list[WorkloadJob]
    path: Path
    extra: dict = field(default_factory=dict)


def load_workload(name_or_path: str) -> Workload:
    path = Path(name_or_path)
    if not path.exists():
        path = WORKLOADS / f"{name_or_path}.toml"
    values = tomllib.loads(path.read_text())
    defaults = values.get("defaults", {})
    jobs = []
    for item in values["jobs"]:
        merged = {**defaults, **item}
        merged.setdefault("estimate_s", merged["run_s"])
        jobs.append(WorkloadJob(**merged))
    ids = [job.id for job in jobs]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: job ids must be distinct")
    for job in jobs:
        if job.user not in LOGICAL_USERS:
            raise ValueError(f"{path}: unknown user {job.user!r}")
        if job.load == "gpu" and not job.gpu:
            raise ValueError(f"{path}: job {job.id} has a GPU load without a GPU")
    jobs.sort(key=lambda job: (job.at, job.id))
    extra = {
        k: v for k, v in values.items() if k not in ("name", "description", "defaults", "jobs")
    }
    return Workload(values.get("name", path.stem), values.get("description", ""), jobs, path, extra)


def job_spec(job: WorkloadJob, run_id: str) -> dict:
    """The LabFlux JobSpec for one workload job; marked so experiment jobs are recognizable."""
    resources = {"cpu": job.cpu, "memory_mib": job.memory_mib, "gpu": job.gpu}
    if job.gpu_model:
        resources["gpu_model"] = job.gpu_model
    spec = {
        "image": GPU_IMAGE if job.load == "gpu" else CPU_IMAGE,
        "argv": ["python", "-c", LOAD_PROGRAM, job.load, str(job.run_s), str(job.cpu), "m13"],
        "env": {
            "LABFLUX_EXPERIMENT": f"m13:{run_id}:{job.id}",
            "M13_JOB": job.id,
            "HOME": "/tmp",
        },
        "resources": resources,
        "time_limit_s": job.time_limit_s,
        "priority": job.priority,
        "estimated_runtime_s": job.estimate_s,
    }
    if job.preemptible or job.retries_on_node_failure:
        spec["safe_to_rerun"] = True
    if job.preemptible:
        spec["preemptible"] = True
    if job.retries_on_node_failure:
        spec["retries_on_node_failure"] = job.retries_on_node_failure
    return spec


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n")


def append_jsonl(path: Path, value) -> None:
    with path.open("a") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
