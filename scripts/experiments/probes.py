"""M13 13.5/13.7 probes on the lab: interactive acquisition, model cold/warm start and idle
reclaim, and a Worker failure. Each probe writes one JSON file under docs/evidence/m13/raw/probes.

    uv run python scripts/experiments/probes.py interactive --rep 1 [--not-preemptible]
    uv run python scripts/experiments/probes.py model --rep 1 [--contended]
    uv run python scripts/experiments/probes.py failure

interactive: alice fills all seven 8-core slots with priority-2 jobs (preemptible, or not, as the
  preemption on/off comparison); then dave runs `labflux run -- python -c ...` and the time to
  its first output line is the interactive job's resource acquisition time. A second command in
  the same session gives the warm per-command latency for comparison.
model: a cold request to the on-demand m13-fake model (stopped), ten warm requests, then a job
  that needs karma's 4090 while the model idles on it: the model is reclaimed after 60 s idle
  and the job starts. --contended first occupies the 4090 with a preemptible job, so the cold
  start has to preempt it.
failure: two jobs on karma (an 8-core CPU job that may rerun, a 4090 job); karma's Worker is
  stopped; once karma is OFFLINE the CPU job's container is removed (a crash); a third job that
  needs the 4090 arrives; the Worker starts again. Expected: the 4090 job is adopted and
  finishes in one attempt, the CPU job's attempt fails and it reruns, the third job waits.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))

from common import (  # noqa: E402
    EVIDENCE,
    ROOT,
    WorkloadJob,
    account,
    job_spec,
    write_json,
)
from lab import FAKE_MODEL, Lab  # noqa: E402
from replay import gpu_labels  # noqa: E402

OUT = EVIDENCE / "raw" / "probes"
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


def submit(lab: Lab, job: WorkloadJob, user_set: int, run_id: str) -> dict:
    owner = account(job.user, user_set)
    response = httpx.post(
        lab.url + "/api/jobs",
        headers=lab.headers(owner),
        json=job_spec(job, run_id),
        timeout=10,
        trust_env=False,
    )
    response.raise_for_status()
    return response.json()


def job_view(lab: Lab, value: dict) -> dict:
    return lab.get(f"/api/jobs/{value['id']}", value["owner"])


def wait_for(predicate, timeout: float, step: float = 0.5, what: str = "condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(step)
    raise TimeoutError(f"timed out waiting for {what}")


def clean_attempts(value: dict, labels: dict) -> list[dict]:
    keep = ("node_id", "state", "created_at", "started_at", "finished_at", "reason", "preempted_by")
    rows = []
    for attempt in value["attempts"]:
        row = {k: attempt.get(k) for k in keep}
        row["id"] = attempt["id"][:8]
        row["preempted_by"] = (row["preempted_by"] or "")[:8] or None
        row["gpus"] = [labels.get(u, "unknown") for u in attempt.get("gpu_uuids", [])]
        rows.append(row)
    return rows


def finish_all(lab: Lab, values: list[dict], timeout: float = 400) -> None:
    wait_for(
        lambda: all(job_view(lab, v)["state"] in TERMINAL for v in values),
        timeout,
        2,
        "probe jobs to end",
    )


# ---- interactive acquisition (13.5) and preemption on/off (13.7) ----
def interactive(lab: Lab, rep: int, user_set: int, preemptible: bool) -> Path:
    run_id = f"probe-interactive-{'pre' if preemptible else 'nopre'}-{rep}"
    labels = gpu_labels(lab)
    if lab.check() or lab.experiment_jobs():
        raise RuntimeError("lab is not free")
    fill = []
    for i in range(7):
        run = 60 + 15 * i
        job = WorkloadJob(
            id=f"f{i + 1:02d}",
            at=0,
            user="alice",
            cpu=8,
            memory_mib=4096,
            run_s=run,
            time_limit_s=run + 60,
            priority=2,
            preemptible=preemptible,
        )
        fill.append(submit(lab, job, user_set, run_id))
        time.sleep(0.3)
    wait_for(
        lambda: all(job_view(lab, v)["state"] == "RUNNING" for v in fill), 60, 0.5, "fill jobs"
    )
    full_at = time.time()
    time.sleep(2)
    dave = account("dave", user_set)
    env = {
        "PATH": "/usr/bin:/bin",
        "LABFLUX_SCHEDULER_URL": lab.url,
        "LABFLUX_USER_TOKEN": lab.tokens[dave],
        "NO_PROXY": "*",
    }
    labflux = str(ROOT / ".venv" / "bin" / "labflux")
    timings = []
    with tempfile.TemporaryDirectory() as project:
        Path(project, ".labflux.toml").write_text(
            '[session]\ncpu = 1\nmemory_mib = 256\nimage = "python:3.12-slim"\n'
        )
        env["XDG_CONFIG_HOME"] = project
        env["HOME"] = project
        for label in ("cold", "warm"):
            start = time.time()
            process = subprocess.Popen(
                [labflux, "run", "--", "python", "-c", "print('m13-ready')"],
                cwd=project,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            first = None
            output = []
            for line in process.stdout:
                output.append(line.rstrip())
                if "m13-ready" in line and first is None:
                    first = time.time()
            code = process.wait()
            timings.append(
                {
                    "command": label,
                    "start": start,
                    "first_output_s": round(first - start, 3) if first else None,
                    "exit_s": round(time.time() - start, 3),
                    "exit_code": code,
                    "output": [o for o in output if "m13-ready" not in o][-5:],
                }
            )
        session = httpx.get(
            lab.url + "/api/sessions/current",
            headers=lab.headers(dave),
            timeout=10,
            trust_env=False,
        ).json()
        subprocess.run([labflux, "session", "close"], cwd=project, env=env, capture_output=True)
    session_job = None
    if session.get("session_id") or session.get("id"):
        sid = session.get("session_id") or session.get("id")
        try:
            session_job = clean_attempts(lab.get(f"/api/jobs/{sid}", dave), labels)
        except httpx.HTTPError:
            session_job = None
    fills = [job_view(lab, v) for v in fill]
    lab.cancel_experiment_jobs()
    finish_all(lab, fill)
    wait_for(lambda: not lab.experiment_jobs(), 120, 2, "experiment jobs to end")
    fills = [
        {"job": f"f{i + 1:02d}", "attempts": clean_attempts(job_view(lab, v), labels)}
        for i, v in enumerate(fill)
    ]
    out = (
        OUT / "interactive" / f"{'preemptible' if preemptible else 'not-preemptible'}-rep{rep}.json"
    )
    write_json(
        out,
        {
            "probe": "interactive",
            "preemptible_fill": preemptible,
            "rep": rep,
            "full_at": full_at,
            "commands": timings,
            "session_attempts": session_job,
            "session": {k: session.get(k) for k in ("state", "node_id", "reason")},
            "fill": fills,
        },
    )
    print(out, [(t["command"], t["first_output_s"], t["exit_code"]) for t in timings], flush=True)
    return out


# ---- model cold / warm start and idle reclaim (13.7) ----
class ModelWatch(threading.Thread):
    def __init__(self, lab: Lab):
        super().__init__(daemon=True)
        self.lab, self.changes, self.stop_flag = lab, [], False

    def run(self):
        last = None
        while not self.stop_flag:
            try:
                model = next(m for m in self.lab.get("/api/models") if m["name"] == FAKE_MODEL)
                key = (model["state"], model.get("node_id"), (model.get("attempt_id") or "")[:8])
                if key != last:
                    last = key
                    self.changes.append(
                        {"t": time.time(), "state": key[0], "node": key[1], "attempt": key[2]}
                    )
            except (httpx.HTTPError, StopIteration):
                pass
            time.sleep(0.25)


def chat(lab: Lab, owner: str, text: str) -> tuple[int, float]:
    start = time.time()
    response = httpx.post(
        lab.url + "/v1/chat/completions",
        headers=lab.headers(owner),
        json={"model": FAKE_MODEL, "messages": [{"role": "user", "content": text}]},
        timeout=200,
        trust_env=False,
    )
    return response.status_code, round(time.time() - start, 3)


def model_state(lab: Lab) -> str:
    return next(m for m in lab.get("/api/models") if m["name"] == FAKE_MODEL)["state"]


def model(lab: Lab, rep: int, user_set: int, contended: bool) -> Path:
    run_id = f"probe-model-{rep}{'-contended' if contended else ''}"
    labels = gpu_labels(lab)
    if lab.check() or lab.experiment_jobs():
        raise RuntimeError("lab is not free")
    wait_for(
        lambda: model_state(lab) in ("stopped", "idle", None), 400, 5, "m13-fake to be stopped"
    )
    watch = ModelWatch(lab)
    watch.start()
    record: dict = {"probe": "model", "rep": rep, "contended": contended}
    holder = None
    if contended:
        job = WorkloadJob(
            id="m-hold",
            at=0,
            user="alice",
            cpu=2,
            memory_mib=2048,
            gpu=1,
            gpu_model="4090",
            run_s=120,
            time_limit_s=180,
            priority=2,
            preemptible=True,
        )
        holder = submit(lab, job, user_set, run_id)
        wait_for(lambda: job_view(lab, holder)["state"] == "RUNNING", 120, 0.5, "4090 holder")
        time.sleep(3)
    dave = account("dave", user_set)
    status, seconds = chat(lab, dave, "cold start")
    record["cold"] = {"status": status, "seconds": seconds}
    warm = [chat(lab, dave, f"warm {i}") for i in range(10)]
    record["warm"] = [{"status": s, "seconds": t} for s, t in warm]
    if holder:
        record["holder_attempts"] = clean_attempts(job_view(lab, holder), labels)
    # Idle reclaim: a job waits for the 4090 the idle model holds.
    last_request = time.time()
    job = WorkloadJob(
        id="m-wait",
        at=0,
        user="bob",
        cpu=2,
        memory_mib=2048,
        gpu=1,
        gpu_model="4090",
        run_s=20,
        time_limit_s=60,
        load="gpu",
    )
    waiting = submit(lab, job, user_set, run_id)
    view = wait_for(
        lambda: (v := job_view(lab, waiting))["attempts"] and v, 200, 0.5, "the waiting job"
    )
    first = view["attempts"][0]
    record["reclaim"] = {
        "last_request": last_request,
        "submitted": waiting["created_at"],
        "dispatched": first["created_at"],
        "wait_s": round(first["created_at"] - waiting["created_at"], 2),
        "idle_before_dispatch_s": round(first["created_at"] - last_request, 2),
    }
    finish_all(lab, [waiting] + ([holder] if holder else []))
    if holder:
        record["holder_attempts"] = clean_attempts(job_view(lab, holder), labels)
    record["waiting_attempts"] = clean_attempts(job_view(lab, waiting), labels)
    time.sleep(2)
    watch.stop_flag = True
    watch.join()
    record["model_states"] = watch.changes
    out = OUT / "model" / f"{'contended' if contended else 'free'}-rep{rep}.json"
    write_json(out, record)
    print(out, record["cold"], record["reclaim"]["wait_s"], flush=True)
    return out


# ---- Worker failure on karma (13.7) ----
def failure(lab: Lab, user_set: int) -> Path:
    run_id = "probe-failure"
    labels = gpu_labels(lab)
    if lab.check() or lab.experiment_jobs():
        raise RuntimeError("lab is not free")
    events = []

    def log(event, **fields):
        events.append({"t": time.time(), "event": event, **fields})
        print(time.strftime("%H:%M:%S"), event, fields, flush=True)

    rerun = WorkloadJob(
        id="w-cpu",
        at=0,
        user="alice",
        cpu=8,
        memory_mib=4096,
        run_s=150,
        time_limit_s=240,
        retries_on_node_failure=1,
        load="sleep",
    )
    survivor = WorkloadJob(
        id="w-gpu",
        at=0,
        user="bob",
        cpu=4,
        memory_mib=8192,
        gpu=1,
        gpu_model="4090",
        run_s=150,
        time_limit_s=240,
        load="gpu",
    )
    late = WorkloadJob(
        id="w-late",
        at=0,
        user="carol",
        cpu=4,
        memory_mib=8192,
        gpu=1,
        gpu_model="4090",
        run_s=30,
        time_limit_s=90,
        load="gpu",
    )
    a = submit(lab, rerun, user_set, run_id)
    b = submit(lab, survivor, user_set, run_id)
    log("submitted", jobs=["w-cpu", "w-gpu"])
    views = wait_for(
        lambda: (
            (vs := [job_view(lab, a), job_view(lab, b)])
            and all(v["state"] == "RUNNING" for v in vs)
            and vs
        ),
        120,
        0.5,
        "both jobs running",
    )
    nodes = [v["attempts"][-1]["node_id"] for v in views]
    log("running", nodes=nodes)
    if nodes != ["karma", "karma"]:
        lab.cancel_experiment_jobs()
        raise RuntimeError(f"jobs did not land on karma: {nodes}")
    time.sleep(20)
    lab.worker("karma", "stop")
    log("worker stopped")
    wait_for(
        lambda: next(n for n in lab.get("/api/nodes") if n["id"] == "karma")["status"] == "OFFLINE",
        60,
        0.5,
        "karma OFFLINE",
    )
    log("karma offline", states=[job_view(lab, a)["state"], job_view(lab, b)["state"]])
    removed = lab.remove_container(
        "karma", a["current_attempt_id"] or views[0]["attempts"][-1]["id"]
    )
    log("container removed", job="w-cpu", removed=bool(removed))
    c = submit(lab, late, user_set, run_id)
    log("submitted", jobs=["w-late"])
    time.sleep(30)
    log(
        "while offline",
        late_reason=job_view(lab, c)["reason"],
        late_note=job_view(lab, c)["schedule_note"][:200],
    )
    lab.worker("karma", "start")
    log("worker started")
    wait_for(
        lambda: next(n for n in lab.get("/api/nodes") if n["id"] == "karma")["status"] == "ONLINE",
        90,
        0.5,
        "karma ONLINE",
    )
    log("karma online", states=[job_view(lab, a)["state"], job_view(lab, b)["state"]])
    finish_all(lab, [a, b, c], 500)
    log("all done")
    out = OUT / "failure" / "karma-rep1.json"
    write_json(
        out,
        {
            "probe": "failure",
            "events": events,
            "jobs": {
                name: {
                    "state": job_view(lab, v)["state"],
                    "attempts": clean_attempts(job_view(lab, v), labels),
                }
                for name, v in (("w-cpu", a), ("w-gpu", b), ("w-late", c))
            },
        },
    )
    print(out, flush=True)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("probe", choices=["interactive", "model", "failure"])
    parser.add_argument("--rep", type=int, default=1)
    parser.add_argument("--user-set", type=int, default=1)
    parser.add_argument("--not-preemptible", action="store_true")
    parser.add_argument("--contended", action="store_true")
    args = parser.parse_args()
    lab = Lab()
    if args.probe == "interactive":
        interactive(lab, args.rep, args.user_set, not args.not_preemptible)
    elif args.probe == "model":
        model(lab, args.rep, args.user_set, args.contended)
    else:
        failure(lab, args.user_set)


if __name__ == "__main__":
    main()
