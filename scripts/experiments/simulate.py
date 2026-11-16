"""Replay an M13 workload against the real Scheduler code with fake Workers and a fake clock.

No Docker, no network, no lab: the Scheduler class runs in-process on a temporary SQLite file;
three fake Workers with the lab's capacities (gemma holds kindred's two A40s, as on the lab)
start each job after a short delay, finish it after its run time and stop it when asked. The
output has the same layout as a lab run (scripts/experiments/replay.py), so analyze.py treats
both alike. Used to check a workload's placements before spending lab time, and to cover the
cases that are only simulated (docs/experiments.md says which).

    uv run python scripts/experiments/simulate.py contention --policy all
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from common import EVIDENCE, account, job_spec, load_workload, write_json  # noqa: E402
from sqlalchemy import select  # noqa: E402

from labflux.config import ModelConfig, Settings  # noqa: E402
from labflux.database import jobs as jobs_table  # noqa: E402
from labflux.models import (  # noqa: E402
    MODEL_KIND,
    Heartbeat,
    JobSpec,
    WorkerEvent,
    WorkerRegistration,
)
from labflux.scheduler import TERMINAL, Scheduler  # noqa: E402

T0 = 1_800_000_000.0  # fake epoch of the first submission


def gpu(n: int, index: int, name: str, mib: int) -> dict:
    return {
        "uuid": f"GPU-0000000{n}-0000-0000-0000-000000000000",
        "index": index,
        "name": name,
        "memory_total_mib": mib,
    }


NODES = {
    "kayle": (
        16,
        49152,
        [gpu(1, 0, "NVIDIA GeForce RTX 3090", 24576), gpu(2, 1, "NVIDIA RTX A5000", 24564)],
    ),
    "karma": (16, 24576, [gpu(3, 0, "NVIDIA GeForce RTX 4090", 24564)]),
    "kindred": (32, 65536, [gpu(4, 3, "NVIDIA A40", 46068), gpu(5, 4, "NVIDIA A40", 46068)]),
}
GPU_LABELS = {
    card["uuid"]: f"{node}/{card['name'].removeprefix('NVIDIA ').removeprefix('GeForce ')}"
    for node, (_, _, cards) in NODES.items()
    for card in cards
}


class FakeWorker:
    def __init__(self, scheduler: Scheduler, name: str, run_times: dict):
        cpu, memory, cards = NODES[name]
        self.scheduler, self.name, self.cards, self.run_times = scheduler, name, cards, run_times
        self.instance = uuid.uuid4().hex
        self.pending: list[tuple[float, str, dict]] = []  # (due, attempt, event fields)
        self.stopped: set[str] = set()
        scheduler.register(
            WorkerRegistration(
                worker_id=name,
                instance_id=self.instance,
                capacity={
                    "total_cpu": cpu,
                    "total_memory_mib": memory + 512,
                    "cpu": cpu,
                    "memory_mib": memory,
                    "datasets": [],
                    "gpus": cards,
                },
            )
        )

    def heartbeat(self) -> None:
        self.scheduler.heartbeat(
            self.name,
            Heartbeat(
                instance_id=self.instance,
                gpus=[
                    {
                        "uuid": c["uuid"],
                        "memory_used_mib": 0,
                        "utilization_percent": 0,
                        "external_use": False,
                    }
                    for c in self.cards
                ],
            ),
        )

    def poll(self, now: float, start_delay: float, stop_delay: float) -> None:
        for command in self.scheduler.poll_commands(self.name, self.instance):
            attempt = command["attempt_id"]
            if command["kind"] == "START":
                payload = command["payload"]
                if payload.get("service"):
                    run = None  # a model server runs until stopped
                else:
                    run = self.run_times[payload["job"]["env"]["M13_JOB"]]
                self.pending.append((now + start_delay, attempt, {"kind": "STARTED"}))
                if run is not None:
                    done = {"kind": "FINISHED", "outcome": "SUCCEEDED", "exit_code": 0}
                    self.pending.append((now + start_delay + run, attempt, done))
            elif command["kind"] == "STOP" and attempt not in self.stopped:
                self.stopped.add(attempt)
                self.pending = [
                    p for p in self.pending if p[1] != attempt or p[2]["kind"] == "STARTED"
                ]
                stop = {"kind": "FINISHED", "outcome": "CANCELLED", "exit_code": 143}
                self.pending.append((now + stop_delay, attempt, stop))
            self.scheduler.ack(self.name, self.instance, command["id"])

    def deliver(self, now: float) -> None:
        due = sorted((p for p in self.pending if p[0] <= now), key=lambda p: p[0])
        self.pending = [p for p in self.pending if p[0] > now]
        for _, attempt, fields in due:
            kind = fields["kind"]
            self.scheduler.handle_event(
                self.name,
                attempt,
                WorkerEvent(
                    event_id=f"{kind.lower()}-{attempt}",
                    instance_id=self.instance,
                    termination_confirmed=kind == "FINISHED",
                    reason="simulated",
                    **fields,
                ),
            )


def simulate(
    workload_name: str,
    policy: str,
    out: Path,
    *,
    preemption: bool = True,
    usage_window_s: float = 600,
    start_delay: float = 1.5,
    stop_delay: float = 1.0,
    step: float = 0.5,
    rep: int = 1,
    preemptible_override: bool | None = None,
) -> Path:
    workload = load_workload(workload_name)
    if preemptible_override is not None:
        for job in workload.jobs:
            job.preemptible = preemptible_override and job.preemptible
    run_id = f"sim-{workload.name}-{policy}-{rep}"
    clock = [T0 - 20]
    users = {account(u, 1): f"token-{u}" for u in ("alice", "bob", "carol", "dave")}
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            data_dir=Path(tmp),
            users=users,
            worker_token="worker-token",
            scheduling_policy=policy,
            preemption_enabled=preemption,
            usage_window_s=usage_window_s,
            models={
                "gemma4-31b": ModelConfig(
                    image="vllm/vllm-openai:v0.30.0",
                    argv=["serve"],
                    resources={"cpu": 8, "memory_mib": 49152, "gpu": 2, "gpu_model": "A40"},
                    mode="persistent",
                )
            },
        )
        scheduler = Scheduler(settings, clock=lambda: clock[0])
        run_times = {job.id: job.run_s for job in workload.jobs}
        workers = [FakeWorker(scheduler, name, run_times) for name in NODES]
        submitted: dict[str, tuple[str, str]] = {}  # job id -> (workload id, owner)
        notes: list[dict] = []
        last_note: dict[str, tuple] = {}
        queue = list(workload.jobs)
        next_heartbeat = clock[0]
        while True:
            now = clock[0]
            while queue and T0 + queue[0].at <= now:
                job = queue.pop(0)
                owner = account(job.user, 1)
                value = scheduler.submit(owner, JobSpec.model_validate(job_spec(job, run_id)))
                submitted[value["id"]] = (job.id, owner)
            if now >= next_heartbeat:
                for worker in workers:
                    worker.heartbeat()
                next_heartbeat = now + 2
            for worker in workers:
                worker.poll(now, start_delay, stop_delay)
                worker.deliver(now)
            scheduler.tick()
            with scheduler.db.transaction() as connection:
                rows = connection.execute(
                    select(jobs_table).where(jobs_table.c.kind != MODEL_KIND)
                ).all()
            states = {}
            for row in rows:
                if row.id not in submitted:
                    continue
                states[row.id] = row.state
                key = (row.state, row.schedule_note, row.reason)
                if last_note.get(row.id) != key:
                    last_note[row.id] = key
                    notes.append(
                        {
                            "t": now,
                            "wid": submitted[row.id][0],
                            "state": row.state,
                            "note": row.schedule_note,
                            "reason": row.reason,
                        }
                    )
            if not queue and states and all(s in TERMINAL for s in states.values()):
                break
            if now > T0 + 7200:
                raise RuntimeError(f"{run_id}: not finished after 2 simulated hours")
            clock[0] += step
        records = []
        for job_id, (wid, owner) in submitted.items():
            value = scheduler.get_job(job_id, owner)
            for attempt in value["attempts"]:
                attempt["gpu_labels"] = [GPU_LABELS[u] for u in attempt.pop("gpu_uuids")]
            value["spec"]["argv"] = (
                value["spec"]["argv"][:2] + ["<m13_load.py>"] + value["spec"]["argv"][3:]
            )
            records.append(
                {"wid": wid, "user": owner.split("-", 1)[1], "account": owner, "job": value}
            )
        scheduler.db.engine.dispose()
    out.mkdir(parents=True, exist_ok=True)
    write_json(
        out / "meta.json",
        {
            "run_id": run_id,
            "mode": "sim",
            "case": workload.name,
            "policy": policy,
            "preemption_enabled": preemption,
            "preemptible_override": preemptible_override,
            "rep": rep,
            "t0": T0,
            "usage_window_s": usage_window_s,
            "start_delay_s": start_delay,
            "stop_delay_s": stop_delay,
            "workload": str(workload.path.relative_to(workload.path.parents[2])),
        },
    )
    write_json(out / "jobs.json", sorted(records, key=lambda r: r["wid"]))
    (out / "notes.jsonl").write_text("".join(__import__("json").dumps(n) + "\n" for n in notes))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("workload")
    parser.add_argument("--policy", default="all", choices=["all", "fcfs", "priority", "backfill"])
    parser.add_argument("--no-preemption", action="store_true")
    parser.add_argument("--out", type=Path, default=EVIDENCE / "sim")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    from analyze import print_run, summarize_run  # noqa: PLC0415

    policies = ["fcfs", "priority", "backfill"] if args.policy == "all" else [args.policy]
    for policy in policies:
        suffix = "-nopreempt" if args.no_preemption else ""
        name = load_workload(args.workload).name
        out = simulate(
            args.workload,
            policy,
            args.out / f"{name}/{policy}{suffix}",
            preemption=not args.no_preemption,
        )
        if not args.quiet:
            print_run(summarize_run(out))


if __name__ == "__main__":
    main()
