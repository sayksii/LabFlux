"""Replay one M13 workload on the lab Scheduler and record everything needed to analyze it.

Each job is submitted at its offset by its user's experiment account; every second the tool
records each job's state and schedule_note (the Scheduler only keeps the latest decision), every
two seconds the nodes' heartbeat observations (CPU %, available RAM, per-card GPU utilization).
At the end it saves every job with its attempts and the M13RESULT line of each attempt's log.
GPU UUIDs are replaced by labels such as kayle/RTX 3090 before anything is written.

The Scheduler's policy is not set here: lab.py switches it. The run fails if the schedule notes
show a different policy than the one claimed.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))

from common import EVIDENCE, ROOT, account, append_jsonl, job_spec, load_workload, write_json  # noqa: E402
from lab import Lab  # noqa: E402

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
LAB_CODE = (
    "8fcab7a"  # the commit deployed on the lab during M13 (main bf86074 differs in docs only)
)


def gpu_labels(lab: Lab) -> dict[str, str]:
    labels = {}
    for node in lab.get("/api/nodes"):
        for card in node["capacity"].get("gpus", []):
            name = card["name"].removeprefix("NVIDIA ").removeprefix("GeForce ")
            labels[card["uuid"]] = f"{node['id']}/{name}"
    return labels


def node_sample(lab: Lab, labels: dict[str, str], now: float) -> list[dict]:
    rows = []
    for node in lab.get("/api/nodes"):
        observed = node["observed"]
        rows.append(
            {
                "t": now,
                "node": node["id"],
                "status": node["status"],
                "cpu_percent": observed.get("cpu_percent"),
                "available_memory_mib": round(observed.get("available_memory_mib") or 0),
                "total_cpu": node["capacity"]["total_cpu"],
                "allocated": {k: v for k, v in node["allocated"].items() if k != "gpu_uuids"},
                "gpus": [
                    {
                        "label": labels.get(card["uuid"], "unknown"),
                        "utilization_percent": card["utilization_percent"],
                        "memory_used_mib": card["memory_used_mib"],
                        "external_use": card["external_use"],
                    }
                    for card in observed.get("gpus", [])
                ],
            }
        )
    return rows


def replay(
    lab: Lab,
    workload_name: str,
    policy: str,
    rep: int,
    user_set: int,
    out: Path,
    *,
    preemption_enabled: bool = True,
    preemptible_override: bool | None = None,
    timeout_s: float = 1500,
    on_tick=None,
) -> Path:
    workload = load_workload(workload_name)
    if preemptible_override is not None:
        for job in workload.jobs:
            job.preemptible = preemptible_override and job.preemptible
    if out.exists() and (out / "jobs.json").exists():
        raise FileExistsError(f"{out} already holds a run")
    out.mkdir(parents=True, exist_ok=True)
    for name in ("notes.jsonl", "nodes.jsonl", "events.jsonl"):
        (out / name).unlink(missing_ok=True)
    run_id = f"{workload.name}-{policy}-r{rep}-{int(time.time())}"
    labels = gpu_labels(lab)
    problems = lab.check()
    if problems:
        raise RuntimeError("lab is not free: " + "; ".join(problems))
    if lab.experiment_jobs():
        raise RuntimeError("experiment jobs from an earlier run are still live")
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    before = node_sample(lab, labels, time.time())
    submitted: dict[str, tuple[str, str]] = {}  # job id -> (workload id, owner)
    last: dict[str, tuple] = {}
    queue = list(workload.jobs)
    t0 = time.time()
    next_poll = next_nodes = t0
    deadline = t0 + timeout_s
    client = httpx.Client(timeout=10, trust_env=False)
    try:
        while True:
            now = time.time()
            while queue and t0 + queue[0].at <= now:
                job = queue.pop(0)
                owner = account(job.user, user_set)
                response = client.post(
                    lab.url + "/api/jobs", headers=lab.headers(owner), json=job_spec(job, run_id)
                )
                response.raise_for_status()
                value = response.json()
                submitted[value["id"]] = (job.id, owner)
                append_jsonl(
                    out / "events.jsonl",
                    {"t": now, "event": "submit", "wid": job.id, "owner": owner},
                )
            if now >= next_poll:
                next_poll = now + 1
                states = {}
                for owner in {o for _, o in submitted.values()}:
                    for value in client.get(
                        lab.url + "/api/jobs", headers=lab.headers(owner)
                    ).json():
                        if value["id"] not in submitted:
                            continue
                        states[value["id"]] = value["state"]
                        key = (value["state"], value["schedule_note"], value["reason"])
                        if last.get(value["id"]) != key:
                            last[value["id"]] = key
                            note = value["schedule_note"] or ""
                            if note and not note.startswith(f"{policy}:"):
                                raise RuntimeError(
                                    f"Scheduler is not running {policy}: {note[:80]}"
                                )
                            append_jsonl(
                                out / "notes.jsonl",
                                {
                                    "t": now,
                                    "wid": submitted[value["id"]][0],
                                    "state": value["state"],
                                    "note": note,
                                    "reason": value["reason"],
                                },
                            )
                if on_tick:
                    on_tick(now - t0, submitted, states)
                if (
                    not queue
                    and len(states) == len(submitted)
                    and all(s in TERMINAL for s in states.values())
                ):
                    break
            if now >= next_nodes:
                next_nodes = now + 2
                for row in node_sample(lab, labels, now):
                    append_jsonl(out / "nodes.jsonl", row)
            if now > deadline:
                lab.cancel_experiment_jobs()
                raise TimeoutError(f"{run_id} did not finish within {timeout_s} s")
            time.sleep(0.2)
        records, results = [], {}
        for job_id, (wid, owner) in submitted.items():
            value = client.get(f"{lab.url}/api/jobs/{job_id}", headers=lab.headers(owner)).json()
            results[wid] = []
            for attempt in value["attempts"]:
                attempt["gpu_labels"] = [labels.get(u, "unknown") for u in attempt.pop("gpu_uuids")]
                logs = client.get(
                    f"{lab.url}/api/jobs/{job_id}/logs",
                    params={"attempt_id": attempt["id"]},
                    headers=lab.headers(owner),
                ).json()
                text = b"".join(base64.b64decode(c["data_base64"]) for c in logs["chunks"]).decode(
                    errors="replace"
                )
                for line in text.splitlines():
                    if line.startswith("M13RESULT "):
                        item = json.loads(line.removeprefix("M13RESULT "))
                        item["attempt"] = attempt["id"][:8]
                        item["node"] = attempt["node_id"]
                        results[wid].append(item)
            value["spec"]["argv"] = (
                value["spec"]["argv"][:2] + ["<m13_load.py>"] + value["spec"]["argv"][3:]
            )
            value.pop("outputs", None)
            records.append(
                {"wid": wid, "user": owner.split("-", 1)[1], "account": owner, "job": value}
            )
    finally:
        client.close()
    write_json(
        out / "meta.json",
        {
            "run_id": run_id,
            "mode": "lab",
            "case": workload.name,
            "policy": policy,
            "preemption_enabled": preemption_enabled,
            "preemptible_override": preemptible_override,
            "rep": rep,
            "user_set": user_set,
            "t0": t0,
            "started": time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(t0)),
            "lab_code": LAB_CODE,
            "tools_commit": commit,
            "usage_window_s": 600,
            "external_before": before,
            "workload": str(workload.path.relative_to(ROOT)),
        },
    )
    write_json(out / "jobs.json", sorted(records, key=lambda r: r["wid"]))
    write_json(out / "results.json", results)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("workload")
    parser.add_argument("--policy", required=True, choices=["fcfs", "priority", "backfill"])
    parser.add_argument("--rep", type=int, default=1)
    parser.add_argument("--user-set", type=int, default=1)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--not-preemptible", action="store_true")
    args = parser.parse_args()
    from analyze import print_run, summarize_run  # noqa: PLC0415

    name = load_workload(args.workload).name
    variant = "-nopreempt" if args.not_preemptible else ""
    out = args.out or EVIDENCE / "raw" / name / f"{args.policy}{variant}" / f"rep{args.rep}"
    path = replay(
        Lab(),
        args.workload,
        args.policy,
        args.rep,
        args.user_set,
        out,
        preemptible_override=False if args.not_preemptible else None,
    )
    print_run(summarize_run(path))


if __name__ == "__main__":
    main()
