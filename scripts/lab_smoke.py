#!/usr/bin/env python3
"""Repeatable acceptance checks on the physical lab (plan 14.4), GPU checks first.

Run from the repository root with a CLI configuration for the lab Scheduler (the deployer's
.labflux/lab/cli.toml), or without --config to use the file `labflux login` saved:

  uv run python scripts/lab_smoke.py --config .labflux/lab/cli.toml

The report goes to the git-ignored .labflux/lab/lab-smoke.json unless --report says otherwise.

The lab machines are shared, so the script:
- refuses to start while a node holds anything but a model service, or while you have unfinished jobs;
- creates only small jobs of its own and cancels whatever of them is left at the end;
- never restarts a Worker or the Scheduler and never changes the scheduling policy
  (pass --policy when the lab does not run the default fcfs);
- uses only pooled GPUs that are idle (not held by a model service, not in external use) and
  skips a GPU check when none qualifies; the FCFS check blocks the queue for about 10 seconds.

Worker failures are not covered here: they need a Worker restart (see docs/verification.md M6.7).
The report contains no addresses, tokens, GPU UUIDs or user names.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import tempfile
import time
from importlib.metadata import version
from pathlib import Path

import httpx

from labflux.config import load_settings

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
UUID = re.compile(r"GPU-[0-9a-fA-F-]{36}")
# Prints the visible cards and the number of /dev/nvidiaN devices, then holds the allocation.
PROBE = "nvidia-smi -L 2>/dev/null; echo devices=$(ls /dev/nvidia[0-9]* 2>/dev/null | wc -l); "


def short(name: str) -> str:
    return name.replace("NVIDIA ", "").replace("GeForce ", "")


class Smoke:
    def __init__(self, config: Path | None, policy: str, min_nodes: int):
        self.config, self.policy, self.min_nodes = config, policy, min_nodes
        settings = load_settings(config)
        self.client = httpx.Client(
            base_url=settings.scheduler_url,
            trust_env=False,
            timeout=60,
            headers={"Authorization": f"Bearer {settings.user_token}"},
        )
        self.created: list[str] = []
        self.labels: dict[str, str] = {}
        self.names: dict[str, str] = {}  # GPU UUID -> "node:model"; UUIDs stay out of the report
        self.report = {"status": "RUNNING", "policy": policy, "checks": [], "skipped": []}

    # ---- helpers -------------------------------------------------------------------------

    def api(self, method, path, **kwargs):
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    def online(self):
        return [n for n in self.api("GET", "/api/nodes") if n["status"] == "ONLINE"]

    def job(self, job_id):
        return self.api("GET", f"/api/jobs/{job_id}")

    def submit(self, label, spec):
        job = self.api("POST", "/api/jobs", json=spec)
        self.created.append(job["id"])
        self.labels[job["id"]] = label
        return job["id"]

    def wait(self, job_id, states=TERMINAL, timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.job(job_id)
            if value["state"] in states:
                return value
            time.sleep(0.5)
        raise AssertionError(f"job {job_id[:8]} ({self.labels.get(job_id)}) did not reach {states}")

    def succeeded(self, job_id, timeout=180):
        value = self.wait(job_id, timeout=timeout)
        assert value["state"] == "SUCCEEDED", f"{self.labels[job_id]}: {value['reason']}"
        return value

    def logs(self, job_id):
        chunks, after = [], -1
        while True:
            value = self.api("GET", f"/api/jobs/{job_id}/logs", params={"after": after})
            chunks += [base64.b64decode(c["data_base64"]) for c in value["chunks"]]
            after = value["next_seq"]
            if len(value["chunks"]) < 64:
                return b"".join(chunks).decode("utf-8", "replace")

    def cli(self, *args, cwd=ROOT, timeout=1900):
        config = ["--config", str(self.config)] if self.config else []
        command = [sys.executable, "-m", "labflux.cli", *config, *args]
        return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)

    def passed(self, name, **evidence):
        self.report["checks"].append({"name": name, "passed": True, **evidence})
        print(f"PASS {name}", flush=True)

    def skipped(self, name, why):
        self.report["skipped"].append({"name": name, "why": why})
        print(f"SKIP {name}: {why}", flush=True)

    def free_cards(self):
        """Idle pooled cards: (node id, uuid, name)."""
        cards = []
        for node in self.online():
            observed = {g["uuid"]: g for g in node["observed"].get("gpus", [])}
            held = set(node["allocated"].get("gpu_uuids", []))
            for gpu in node["capacity"].get("gpus", []):
                live = observed.get(gpu["uuid"])
                if live and not live["external_use"] and gpu["uuid"] not in held:
                    cards.append((node["id"], gpu["uuid"], gpu["name"]))
        return cards

    def cards_of(self, value):
        return value["attempts"][-1]["gpu_uuids"] if value["attempts"] else []

    @staticmethod
    def sleeper(seconds, *, cpu=1, memory_mib=256, gpu=0, gpu_model=None, limit=120, **extra):
        resources = {"cpu": cpu, "memory_mib": memory_mib}
        if gpu:
            resources.update(gpu=gpu, **({"gpu_model": gpu_model} if gpu_model else {}))
        return {
            "argv": ["sh", "-c", f"{PROBE}sleep {seconds}"],
            "resources": resources,
            "time_limit_s": limit,
            **extra,
        }

    def probe_result(self, job_id):
        text = self.logs(job_id)
        devices = re.search(r"devices=(\d+)", text)
        return set(UUID.findall(text)), int(devices.group(1)) if devices else None

    # ---- checks --------------------------------------------------------------------------

    def preflight(self):
        nodes = self.online()
        assert len(nodes) >= self.min_nodes, f"{len(nodes)} online nodes, expected {self.min_nodes}"
        models = self.api("GET", "/api/models")
        running_models = {}
        for model in models:
            if model["node_id"] and model["state"] in {"starting", "serving", "stopping"}:
                running_models[model["node_id"]] = running_models.get(model["node_id"], 0) + 1
        busy = [
            f"{n['id']} ({n['allocated']['jobs'] - running_models.get(n['id'], 0)})"
            for n in nodes
            if n["allocated"]["jobs"] > running_models.get(n["id"], 0)
        ]
        assert not busy, "nodes hold work other than model services: " + ", ".join(busy)
        mine = [j["id"][:8] for j in self.api("GET", "/api/jobs") if j["state"] not in TERMINAL]
        assert not mine, "you have unfinished jobs: " + ", ".join(mine)
        for node in nodes:
            for gpu in node["capacity"].get("gpus", []):
                self.names[gpu["uuid"]] = f"{node['id']}:{short(gpu['name'])}"
        self.baseline = {n["id"]: (n["allocated"]["jobs"], n["allocated"]["gpu"]) for n in nodes}
        self.report["nodes"] = [
            {
                "id": n["id"],
                "cpu": n["capacity"]["cpu"],
                "memory_mib": n["capacity"]["memory_mib"],
                "gpus": [short(g["name"]) for g in n["capacity"].get("gpus", [])],
                "gpu_driver": n["capacity"].get("gpu_driver_version"),
            }
            for n in sorted(nodes, key=lambda n: n["id"])
        ]
        self.report["models"] = [
            {"name": m["name"], "mode": m["mode"], "state": m["state"], "node": m["node_id"]}
            for m in models
        ]
        self.report["idle_cards_at_start"] = sorted(self.names[u] for _, u, _ in self.free_cards())
        shapes = {(n["capacity"]["cpu"], n["capacity"]["memory_mib"]) for n in nodes}
        assert len(shapes) >= 2, "all nodes have the same CPU/RAM capacity"
        self.passed("nodes online with different CPU, RAM and GPU capacity", nodes=len(nodes))

    def gpu_isolation_and_ledger(self):
        name = "each GPU job sees only its card; no card is given twice; a job waits when all are taken"
        free = self.free_cards()
        if not free:
            return self.skipped(name, "no idle pooled card")
        holders = [self.submit("gpu-holder", self.sleeper(8, gpu=1)) for _ in free]
        extra = self.submit("gpu-extra", self.sleeper(1, gpu=1))
        running = [self.wait(j, {"RUNNING"}, timeout=120) for j in holders]
        waiting = self.job(extra)
        assert waiting["state"] == "QUEUED" and not waiting["attempts"], waiting["state"]
        given = [self.cards_of(v)[0] for v in running]
        assert len(set(given)) == len(free) and set(given) == {u for _, u, _ in free}, (
            "cards reused"
        )
        for job_id in holders + [extra]:
            value = self.succeeded(job_id)
            seen, devices = self.probe_result(job_id)
            assert seen == set(self.cards_of(value)) and devices == 1, (seen, devices)
        value = self.job(extra)
        first_release = min(self.job(j)["attempts"][-1]["finished_at"] for j in holders)
        assert value["attempts"][-1]["started_at"] >= first_release - 1, "extra did not wait"
        self.passed(
            name,
            cards=sorted(self.names[u] for u in given),
            extra_job_waited_s=round(value["attempts"][-1]["started_at"] - value["created_at"], 1),
        )

    def multi_gpu(self):
        name = "a two-GPU job gets two cards of one node and sees exactly those two"
        per_node = {}
        for node, uuid, _ in self.free_cards():
            per_node.setdefault(node, []).append(uuid)
        if not any(len(cards) >= 2 for cards in per_node.values()):
            return self.skipped(name, "no node has two idle pooled cards")
        value = self.succeeded(self.submit("gpu-2", self.sleeper(1, gpu=2)))
        seen, devices = self.probe_result(value["id"])
        cards = self.cards_of(value)
        assert len(cards) == 2 and seen == set(cards) and devices == 2, (seen, devices)
        self.passed(
            name, node=value["attempts"][-1]["node_id"], cards=[self.names[u] for u in cards]
        )

    def cuda_through_cli(self):
        name = "CLI submit/logs/fetch of a real CUDA job (examples/uv-gpu)"
        if not self.free_cards():
            return self.skipped(name, "no idle pooled card")
        assert self.cli("whoami").returncode == 0, "labflux whoami failed"
        started = time.monotonic()
        run = self.cli(
            "submit",
            "examples/jobs/uv-gpu.json",
            "--source",
            "examples/uv-gpu",
            "--wait",
            "--wait-timeout",
            "1800",
        )
        job = json.loads(run.stdout) if run.stdout.strip().startswith("{") else None
        assert job, f"submit failed: {run.stderr[-500:]}"
        self.created.append(job["id"])
        self.labels[job["id"]] = "uv-gpu"
        assert job["state"] == "SUCCEEDED", job["reason"]
        assert "matmul_matches_cpu" in self.cli("logs", job["id"]).stdout
        with tempfile.TemporaryDirectory(prefix="labflux-lab-smoke-") as temp:
            assert self.cli("fetch", job["id"], "--directory", temp).returncode == 0
            result = json.loads((Path(temp) / "result.json").read_text())
        assert result["cuda_available"] and result["matmul_matches_cpu"], result
        self.passed(
            name,
            card=self.names[self.cards_of(self.job(job["id"]))[0]],
            torch=result["torch"],
            submit_to_done_s=round(time.monotonic() - started, 1),
        )

    def session_preempts_gpu_job(self):
        name = "a fast-lane session preempts a preemptible GPU job, which then reruns"
        free = self.free_cards()
        if not free:
            return self.skipped(name, "no idle pooled card")
        rerun = {"priority": 2, "safe_to_rerun": True, "preemptible": True}
        victims = [self.submit("preemptible-gpu", self.sleeper(30, gpu=1, **rerun)) for _ in free]
        for job_id in victims:
            self.wait(job_id, {"RUNNING"}, timeout=120)
        with tempfile.TemporaryDirectory(prefix="labflux-lab-smoke-session-") as project:
            (Path(project) / ".labflux.toml").write_text(
                "[session]\ncpu = 1\nmemory_mib = 512\ngpu = 1\n"
            )
            started = time.monotonic()
            run = self.cli("run", "--", "nvidia-smi", "-L", cwd=project, timeout=300)
            seconds = time.monotonic() - started
            self.cli("session", "close", cwd=project, timeout=60)
        assert run.returncode == 0, f"labflux run exited {run.returncode}: {run.stderr[-500:]}"
        assert len(UUID.findall(run.stdout)) == 1, "the session should see exactly one card"
        preempted = [
            j
            for j in victims
            if any(a["state"] == "PREEMPTED" and a["preempted_by"] for a in self.job(j)["attempts"])
        ]
        assert len(preempted) == 1, f"{len(preempted)} jobs preempted, expected 1"
        for job_id in victims:
            self.succeeded(job_id, timeout=180)
        attempts = [a["state"] for a in self.job(preempted[0])["attempts"]]
        assert attempts == ["PREEMPTED", "SUCCEEDED"], attempts
        self.passed(
            name,
            victims=len(victims),
            session_command_s=round(seconds, 1),
            victim_attempts=attempts,
            others_untouched=all(
                len(self.job(j)["attempts"]) == 1 for j in victims if j != preempted[0]
            ),
        )

    def fcfs_blocking(self):
        name = f"{self.policy}: a job that fits waits behind a blocked GPU head; queued cancel"
        pooled = [g["name"] for n in self.online() for g in n["capacity"].get("gpus", [])]
        unique = [(n, u, g) for n, u, g in self.free_cards() if pooled.count(g) == 1]
        if not unique:
            return self.skipped(name, "no idle card with a unique model name")
        _, uuid, model = unique[0]
        busy = self.submit("fcfs-busy", self.sleeper(10, gpu=1, gpu_model=model))
        self.wait(busy, {"RUNNING"}, timeout=120)
        head = self.submit("fcfs-head", self.sleeper(1, gpu=1, gpu_model=model))
        tail = self.submit("fcfs-tail", self.sleeper(1, limit=30))
        # Asks for the busy card too, so it is still queued under any policy when cancelled.
        dropped = self.submit("fcfs-cancelled", self.sleeper(1, gpu=1, gpu_model=model))
        time.sleep(3)
        head_now, tail_now = self.job(head), self.job(tail)
        assert head_now["state"] == "QUEUED", head_now["state"]
        cancelled = self.api("POST", f"/api/jobs/{dropped}/cancel")
        assert cancelled["state"] == "CANCELLED" and not self.job(dropped)["attempts"]
        if self.policy == "backfill":
            assert tail_now["state"] != "QUEUED", "backfill should start the short tail"
        else:
            assert tail_now["state"] == "QUEUED", f"tail {tail_now['state']} under {self.policy}"
        for job_id in (busy, head, tail):
            self.succeeded(job_id)
        # Dispatch order is the attempt's created_at; containers on different cards or nodes can
        # start in either order (the same transaction often dispatches both).
        head_dispatch = self.job(head)["attempts"][-1]["created_at"]
        tail_dispatch = self.job(tail)["attempts"][-1]["created_at"]
        if self.policy != "backfill":
            assert tail_dispatch >= head_dispatch, "tail was dispatched before the head"
        self.passed(
            name,
            card=self.names[uuid],
            tail_note=tail_now["schedule_note"],
            head_got_the_same_card=self.cards_of(self.job(head)) == [uuid],
        )

    def cpu_limits_oom_timeout_cancel(self):
        script = (
            "import json,os,time\nfrom pathlib import Path\np=Path('/sys/fs/cgroup')\n"
            "v={k:(p/k).read_text().strip() for k in ['cpu.max','memory.max','memory.swap.max']}\n"
            "c=time.process_time(); s=time.monotonic()\nwhile time.monotonic()-s<2: pass\n"
            "v['cpu_seconds']=round(time.process_time()-c,3)\n"
            "v['nvidia_devices']=len([d for d in os.listdir('/dev') if d.startswith('nvidia')])\n"
            "print('LIMITS '+json.dumps(v),flush=True)\n"
        )
        limits = self.submit(
            "limits",
            {
                "argv": ["python", "-c", script],
                "resources": {"cpu": 0.25, "memory_mib": 64},
                "time_limit_s": 60,
            },
        )
        oom = self.submit(
            "oom",
            {
                "argv": ["python", "-c", "a = bytearray(512 * 1024 * 1024)"],
                "resources": {"cpu": 0.5, "memory_mib": 64},
                "time_limit_s": 60,
            },
        )
        timeout = self.submit(
            "timeout",
            {
                "argv": ["sh", "-c", "sleep 30 & sleep 30"],
                "resources": {"cpu": 0.5, "memory_mib": 64},
                "time_limit_s": 1,
            },
        )
        running = self.submit("cancel-running", self.sleeper(60))
        self.succeeded(limits)
        values = json.loads(self.logs(limits).split("LIMITS ", 1)[1].splitlines()[0])
        quota, period = map(int, values["cpu.max"].split())
        assert quota / period == 0.25 and int(values["memory.max"]) == 64 * 1024**2
        assert values["memory.swap.max"] == "0" and values["cpu_seconds"] < 0.9
        assert values["nvidia_devices"] == 0, "a CPU job must not see GPUs"
        self.passed("CPU quota, RAM limit, no swap and no GPU devices in a CPU job", **values)
        value = self.wait(oom)
        oom_attempt = value["attempts"][-1]
        # Killed at the limit = contained. The OOM flag comes from Docker and is occasionally
        # still false when the Worker reads it right after the exit (docs/verification.md, M14).
        assert value["state"] == "FAILED" and oom_attempt["exit_code"] == 137, value["reason"]
        value = self.wait(timeout)
        assert value["state"] == "FAILED" and "time limit" in value["reason"], value["reason"]
        self.passed(
            "memory exhaustion is contained; the time limit stops the whole container",
            oom_exit_code=oom_attempt["exit_code"],
            oom_reported=oom_attempt["oom_killed"],
        )
        self.wait(running, {"RUNNING"})
        assert self.api("POST", f"/api/jobs/{running}/cancel")["state"] == "STOPPING"
        assert self.wait(running)["state"] == "CANCELLED"
        self.passed("a running job is cancelled only after its container stops")

    def gateway(self):
        name = "a model service answers through the /v1 Gateway, normal and streamed"
        models = self.api("GET", "/v1/models")["data"]
        serving = [m for m in models if m["labflux"]["state"] == "serving"]
        if not serving:
            return self.skipped(name, "no model is serving")
        model = serving[0]["id"]
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
            "max_tokens": 8,
        }
        started = time.monotonic()
        reply = self.api("POST", "/v1/chat/completions", json=body)
        normal = time.monotonic() - started
        assert reply["choices"][0]["message"]["content"].strip(), reply
        started, first, chunks = time.monotonic(), None, 0
        with self.client.stream("POST", "/v1/chat/completions", json={**body, "stream": True}) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if line.startswith("data:") and line[5:].strip() != "[DONE]":
                    first, chunks = first or time.monotonic(), chunks + 1
        assert chunks, "no streamed chunks"
        self.passed(
            name,
            model=model,
            node=serving[0]["labflux"]["node_id"],
            normal_s=round(normal, 2),
            stream_first_chunk_s=round(first - started, 2),
            stream_chunks=chunks,
        )

    def released(self):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            now = {n["id"]: (n["allocated"]["jobs"], n["allocated"]["gpu"]) for n in self.online()}
            if now == self.baseline:
                break
            time.sleep(1)
        assert now == self.baseline, f"allocations {now} differ from the start {self.baseline}"
        self.passed("every allocation is back to where it was before the run")

    # ---- run -----------------------------------------------------------------------------

    def run(self, report_path: Path) -> bool:
        self.report["labflux_version"] = version("labflux")
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
        ).stdout.strip()
        self.report["commit"] = commit or None
        self.report["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        try:
            self.preflight()
            self.gpu_isolation_and_ledger()
            self.multi_gpu()
            self.cuda_through_cli()
            self.session_preempts_gpu_job()
            self.fcfs_blocking()
            self.cpu_limits_oom_timeout_cancel()
            self.gateway()
            self.released()
            self.report["status"] = "PASSED"
        except BaseException as exc:
            self.report["status"] = "FAILED"
            self.report["error"] = UUID.sub("GPU-<uuid>", str(exc))
            print(f"FAIL {self.report['error']}", file=sys.stderr, flush=True)
        finally:
            for job_id in self.created:
                try:
                    if self.job(job_id)["state"] not in TERMINAL:
                        self.api("POST", f"/api/jobs/{job_id}/cancel")
                except httpx.HTTPError:
                    pass
            jobs = []
            for job_id in self.created:
                try:
                    value = self.job(job_id)
                except httpx.HTTPError:
                    continue
                jobs.append(
                    {
                        "id": job_id,
                        "check": self.labels[job_id],
                        "state": value["state"],
                        "attempts": [
                            {
                                "state": a["state"],
                                "node": a["node_id"],
                                "cards": [self.names.get(u, "?") for u in a["gpu_uuids"]],
                                "seconds": round(a["finished_at"] - a["started_at"], 1)
                                if a["started_at"] and a["finished_at"]
                                else None,
                            }
                            for a in value["attempts"]
                        ],
                    }
                )
            self.report["jobs"] = jobs
            self.report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(self.report, indent=2, ensure_ascii=False) + "\n")
            self.client.close()
        print(
            f"{self.report['status']}: {len(self.report['checks'])} checks passed, "
            f"{len(self.report['skipped'])} skipped. Report: {report_path}",
            flush=True,
        )
        return self.report["status"] == "PASSED"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--config", type=Path, help="CLI configuration (default: the login file)")
    parser.add_argument(
        "--policy",
        choices=["fcfs", "priority", "backfill"],
        default="fcfs",
        help="the policy the lab Scheduler runs (the API does not report it)",
    )
    parser.add_argument("--min-nodes", type=int, default=3)
    parser.add_argument("--report", type=Path, default=ROOT / ".labflux/lab/lab-smoke.json")
    args = parser.parse_args()
    config = args.config.resolve() if args.config else None
    ok = Smoke(config, args.policy, args.min_nodes).run(args.report.resolve())
    raise SystemExit(0 if ok else 1)
