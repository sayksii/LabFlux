"""M13 metrics from raw run directories (lab replays and simulations) and the CSV exports.

    uv run python scripts/experiments/analyze.py            # lab runs -> docs/evidence/m13/*.csv
    uv run python scripts/experiments/analyze.py --sim      # simulated runs -> .../sim/*.csv

Definitions (docs/experiments.md repeats them):
- wait: submission until the first attempt is dispatched (resources reserved). start_wait also
  includes the container start until the Worker reports STARTED.
- turnaround: submission until the last attempt ends. bounded slowdown: turnaround divided by
  max(run time, 10 s), at least 1.
- makespan: first submission until the last job ends.
- allocation (CPU/RAM/GPU): reserved resource-seconds of every attempt inside the makespan,
  divided by the experiment capacity (lab caps minus gemma) times the makespan.
- actual CPU: CPU seconds the jobs' cgroups used (M13RESULT lines), same denominator.
- actual GPU: mean NVML utilization of the three experiment cards, sampled from heartbeats.
- fairness: Jain's index over the per-user mean waits, (sum x)^2 / (n * sum x^2); 1 is equal.
- cost: execution seconds thrown away by preemption (PREEMPTED attempts), reruns, and whether
  a backfill reservation was broken (a job started more than 2 s after its first reservation).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from common import CAPACITY, EVIDENCE, load_workload, read_jsonl  # noqa: E402

TAU_S = 10.0
RESERVED = re.compile(r"reserved on (\w+) at (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
EXPERIMENT_CARDS = ("kayle/RTX 3090", "kayle/RTX A5000", "karma/RTX 4090")


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low, high = int(position), min(int(position) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def jain(values: list[float]) -> float | None:
    if not values or not any(values):
        return None if not values else 1.0
    return sum(values) ** 2 / (len(values) * sum(v * v for v in values))


def load_run(path: Path) -> dict:
    meta = json.loads((path / "meta.json").read_text())
    records = json.loads((path / "jobs.json").read_text())
    workload = load_workload(str(Path(__file__).resolve().parents[2] / meta["workload"]))
    results = {}
    if (path / "results.json").exists():
        results = json.loads((path / "results.json").read_text())
    return {
        "path": path,
        "meta": meta,
        "records": records,
        "workload": {job.id: job for job in workload.jobs},
        "notes": read_jsonl(path / "notes.jsonl"),
        "nodes": read_jsonl(path / "nodes.jsonl"),
        "results": results,
    }


def job_rows(run: dict) -> list[dict]:
    meta, notes = run["meta"], run["notes"]
    t0 = meta["t0"]
    by_job: dict[str, list[dict]] = {}
    for note in notes:
        by_job.setdefault(note["wid"], []).append(note)
    rows = []
    for record in run["records"]:
        job, wid = record["job"], record["wid"]
        planned = run["workload"][wid]
        attempts = sorted(job["attempts"], key=lambda a: a["created_at"])
        submit = job["created_at"]
        first = attempts[0] if attempts else None
        last = attempts[-1] if attempts else None
        preempted = [a for a in attempts if a["state"] == "PREEMPTED"]
        wasted = sum(
            (a["finished_at"] or 0) - (a["started_at"] or a["finished_at"] or 0) for a in preempted
        )
        queued = (first["created_at"] - submit) if first else None
        for before, after in zip(attempts, attempts[1:]):
            queued += after["created_at"] - (before["finished_at"] or after["created_at"])
        history = by_job.get(wid, [])
        # The reservation in force when the job started: its last queued note, if that note
        # reserved it. A job that lost the head to reordering keeps no promise.
        reservations = []
        queued_notes = []
        for note in history:
            if note["state"] != "QUEUED":
                break
            queued_notes.append(note)
        match = RESERVED.search(queued_notes[-1].get("note") or "") if queued_notes else None
        if match:
            at = time.mktime(time.strptime(match.group(2), "%Y-%m-%d %H:%M:%S"))
            reservations.append((match.group(1), at))
        end = last["finished_at"] if last and last["finished_at"] else None
        turnaround = end - submit if end else None
        dispatch_note = next(
            (n["note"] for n in history if n["state"] == "STARTING"), job.get("schedule_note", "")
        )
        rows.append(
            {
                "case": meta["case"],
                "policy": meta["policy"],
                "variant": variant(meta),
                "rep": meta["rep"],
                "mode": meta["mode"],
                "job": wid,
                "user": record["user"],
                "priority": planned.priority,
                "preemptible": planned.preemptible,
                "cpu": planned.cpu,
                "memory_mib": planned.memory_mib,
                "gpu": planned.gpu,
                "run_s": planned.run_s,
                "time_limit_s": planned.time_limit_s,
                "submit_s": round(submit - t0, 2),
                "dispatch_s": round(first["created_at"] - t0, 2) if first else None,
                "start_s": round(first["started_at"] - t0, 2)
                if first and first["started_at"]
                else None,
                "end_s": round(end - t0, 2) if end else None,
                "wait_s": round(first["created_at"] - submit, 2) if first else None,
                "start_wait_s": round(first["started_at"] - submit, 2)
                if first and first["started_at"]
                else None,
                "queued_total_s": round(queued, 2) if queued is not None else None,
                "turnaround_s": round(turnaround, 2) if turnaround else None,
                "bounded_slowdown": round(max(1.0, turnaround / max(planned.run_s, TAU_S)), 3)
                if turnaround
                else None,
                "state": job["state"],
                "attempts": len(attempts),
                "preemptions": len(preempted),
                "wasted_s": round(wasted, 2),
                "node": last["node_id"] if last else None,
                "gpus": "+".join(last.get("gpu_labels", [])) if last else "",
                "backfilled": any("backfilled on" in (n.get("note") or "") for n in history),
                "reserved_s": round(reservations[0][1] - t0, 2) if reservations else None,
                "reservation_broken": bool(reservations)
                and first is not None
                and first["created_at"] > reservations[0][1] + 2,
                "dispatch_note": dispatch_note,
            }
        )
    return rows


def variant(meta: dict) -> str:
    if meta.get("preemptible_override") is False:
        return "no-preemption"
    if meta.get("preemption_enabled") is False:
        return "no-preemption"
    return "default"


def attempt_intervals(run: dict):
    for record in run["records"]:
        for attempt in record["job"]["attempts"]:
            yield record, attempt


def summarize_run(path: Path) -> dict:
    run = load_run(path) if isinstance(path, Path) else path
    meta = run["meta"]
    rows = job_rows(run)
    t0 = meta["t0"]
    ends = [r["end_s"] for r in rows if r["end_s"] is not None]
    makespan = max(ends) if ends else None
    waits = [r["wait_s"] for r in rows if r["wait_s"] is not None]
    capacity = {k: sum(node[k] for node in CAPACITY.values()) for k in ("cpu", "memory_mib", "gpu")}
    used = {"cpu": 0.0, "memory_mib": 0.0, "gpu": 0.0}
    if makespan:
        horizon = t0 + makespan
        for _, attempt in attempt_intervals(run):
            start = max(attempt["created_at"], t0)
            stop = min(attempt["finished_at"] or horizon, horizon)
            seconds = max(0.0, stop - start)
            used["cpu"] += seconds * attempt["resources"]["cpu"]
            used["memory_mib"] += seconds * attempt["resources"]["memory_mib"]
            used["gpu"] += seconds * len(attempt.get("gpu_labels", []))
    alloc = {k: (used[k] / (capacity[k] * makespan) if makespan else None) for k in used}
    users: dict[str, list[float]] = {}
    for row in rows:
        if row["wait_s"] is not None and not row["preemptible"] and row["priority"] < 8:
            users.setdefault(row["user"], []).append(row["wait_s"])
    per_user = {user: statistics.fmean(values) for user, values in sorted(users.items())}
    urgent = [r["wait_s"] for r in rows if r["priority"] >= 8 and r["wait_s"] is not None]
    cpu_seconds = None
    if run["results"]:
        cpu_seconds = sum(
            item.get("cpu_s") or 0 for items in run["results"].values() for item in items
        )
    gpu_util = None
    if run["nodes"] and makespan:
        samples = [
            card["utilization_percent"]
            for sample in run["nodes"]
            if 0 <= sample["t"] - t0 <= makespan
            for card in sample.get("gpus", [])
            if card["label"] in EXPERIMENT_CARDS
        ]
        gpu_util = statistics.fmean(samples) / 100 if samples else None
    return {
        "path": str(Path(run["path"]).resolve().relative_to(Path(__file__).resolve().parents[2])),
        "case": meta["case"],
        "policy": meta["policy"],
        "variant": variant(meta),
        "rep": meta["rep"],
        "mode": meta["mode"],
        "jobs": len(rows),
        "mean_wait_s": statistics.fmean(waits) if waits else None,
        "p50_wait_s": percentile(waits, 0.5),
        "p95_wait_s": percentile(waits, 0.95),
        "mean_turnaround_s": statistics.fmean(
            [r["turnaround_s"] for r in rows if r["turnaround_s"]]
        )
        if rows
        else None,
        "mean_bounded_slowdown": statistics.fmean(
            [r["bounded_slowdown"] for r in rows if r["bounded_slowdown"]]
        )
        if rows
        else None,
        "makespan_s": makespan,
        "alloc_cpu": alloc["cpu"],
        "alloc_memory": alloc["memory_mib"],
        "alloc_gpu": alloc["gpu"],
        "actual_cpu": cpu_seconds / (capacity["cpu"] * makespan)
        if cpu_seconds and makespan
        else None,
        "actual_gpu": gpu_util,
        "per_user_wait": per_user,
        "jain_wait": jain(list(per_user.values())),
        "urgent_mean_wait_s": statistics.fmean(urgent) if urgent else None,
        "preemptions": sum(r["preemptions"] for r in rows),
        "wasted_s": sum(r["wasted_s"] for r in rows),
        "backfilled": sum(r["backfilled"] for r in rows),
        "reservations_broken": sum(r["reservation_broken"] for r in rows),
        "failed": sum(r["state"] != "SUCCEEDED" for r in rows),
    }


def fmt(value, digits=0, pct=False):
    if value is None:
        return "-"
    return f"{value * 100:.{digits}f}%" if pct else f"{value:.{digits}f}"


def print_run(s: dict) -> None:
    users = " ".join(f"{u}={fmt(w)}" for u, w in s["per_user_wait"].items())
    print(
        f"{s['case']:<11} {s['policy']:<8} {s['variant']:<13} rep{s['rep']} "
        f"wait mean {fmt(s['mean_wait_s'])} p50 {fmt(s['p50_wait_s'])} p95 {fmt(s['p95_wait_s'])} "
        f"| makespan {fmt(s['makespan_s'])} | alloc cpu {fmt(s['alloc_cpu'], pct=True)} "
        f"ram {fmt(s['alloc_memory'], pct=True)} gpu {fmt(s['alloc_gpu'], pct=True)} "
        f"| actual cpu {fmt(s['actual_cpu'], pct=True)} gpu {fmt(s['actual_gpu'], pct=True)} "
        f"| jain {fmt(s['jain_wait'], 3)} [{users}] | urgent {fmt(s['urgent_mean_wait_s'])} "
        f"| preempt {s['preemptions']} wasted {fmt(s['wasted_s'])} | backfilled {s['backfilled']} "
        f"broken {s['reservations_broken']} | failed {s['failed']}"
    )


def run_dirs(root: Path) -> list[Path]:
    return sorted(p.parent for p in root.rglob("meta.json"))


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def aggregate(summaries: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for s in summaries:
        groups.setdefault((s["case"], s["policy"], s["variant"]), []).append(s)
    metrics = [
        "mean_wait_s",
        "p50_wait_s",
        "p95_wait_s",
        "mean_turnaround_s",
        "mean_bounded_slowdown",
        "makespan_s",
        "alloc_cpu",
        "alloc_memory",
        "alloc_gpu",
        "actual_cpu",
        "actual_gpu",
        "jain_wait",
        "urgent_mean_wait_s",
        "preemptions",
        "wasted_s",
        "backfilled",
        "reservations_broken",
        "failed",
    ]
    rows = []
    for (case, policy, var), items in sorted(groups.items()):
        row = {"case": case, "policy": policy, "variant": var, "reps": len(items)}
        for metric in metrics:
            values = [i[metric] for i in items if i[metric] is not None]
            row[f"{metric}_mean"] = round(statistics.fmean(values), 4) if values else None
            row[f"{metric}_sd"] = round(statistics.stdev(values), 4) if len(values) > 1 else None
        users: dict[str, list[float]] = {}
        for item in items:
            for user, wait in item["per_user_wait"].items():
                users.setdefault(user, []).append(wait)
        for user, values in sorted(users.items()):
            row[f"wait_{user}_mean"] = round(statistics.fmean(values), 2)
        rows.append(row)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--sim", action="store_true", help="analyze the simulated runs")
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    root = args.root or (EVIDENCE / "sim" if args.sim else EVIDENCE / "raw")
    out = root.parent if args.root is None and not args.sim else root
    dirs = run_dirs(root)
    summaries, jobs = [], []
    for path in dirs:
        run = load_run(path)
        summary = summarize_run(run)
        summaries.append(summary)
        jobs.extend(job_rows(run))
        print_run(summary)
    prefix = "sim_" if args.sim else ""
    write_csv(out / f"{prefix}jobs.csv", jobs)
    flat = [
        {k: (json.dumps(v) if isinstance(v, dict) else v) for k, v in s.items()} for s in summaries
    ]
    write_csv(out / f"{prefix}runs.csv", flat)
    write_csv(out / f"{prefix}summary.csv", aggregate(summaries))
    print(f"{len(dirs)} runs -> {out}")


if __name__ == "__main__":
    main()
