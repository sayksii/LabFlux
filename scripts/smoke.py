#!/usr/bin/env python3
"""Repeatable three-VM acceptance checks for M2–M6.3.

Uses only jobs created by this run. --faults briefly freezes the labflux-vm-c Worker
(not its VM or Docker daemon), then resumes it even if an assertion fails.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from labflux.archives import extract_archive
from labflux.config import load_settings

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


def wait_for(predicate, *, timeout=60, description="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.15)
    raise AssertionError(f"Timed out: {description}")


def run_smoke(config: Path, *, faults=False, report_path: Path):
    settings = load_settings(config)
    report = {"status": "RUNNING", "checks": [], "jobs": [], "fault_test": faults}
    created = []
    frozen = None
    client = httpx.Client(
        base_url=settings.scheduler_url,
        trust_env=False,
        timeout=15,
        headers={"Authorization": f"Bearer {settings.user_token}"},
    )

    def request(method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    def passed(name, **evidence):
        report["checks"].append({"name": name, "passed": True, **evidence})
        print(f"PASS {name}", flush=True)

    def get(job):
        return request("GET", f"/api/jobs/{job['id']}")

    def submit(cpu=1, memory=64, code="print('completed')", limit=20):
        job = request(
            "POST",
            "/api/jobs",
            json={
                "image": settings.docker_image,
                "argv": ["python", "-u", "-c", code],
                "resources": {"cpu": cpu, "memory_mib": memory},
                "time_limit_s": limit,
            },
        )
        created.append(job)
        return job

    def wait_state(job, states=TERMINAL, timeout=60):
        return wait_for(
            lambda: value if (value := get(job))["state"] in states else None,
            timeout=timeout,
            description=f"job {job['id']} -> {states}",
        )

    def successful(job):
        value = wait_state(job)
        assert value["state"] == "SUCCEEDED", value["reason"]
        return value

    def logs(job):
        chunks, after = [], -1
        while True:
            value = request("GET", f"/api/jobs/{job['id']}/logs", params={"after": after})
            chunks.extend(base64.b64decode(chunk["data_base64"]) for chunk in value["chunks"])
            after = value["next_seq"]
            if len(value["chunks"]) < 64:
                return b"".join(chunks).decode("utf-8", "replace")

    def node_list():
        return request("GET", "/api/nodes")

    def cli(*args):
        result = subprocess.run(
            [sys.executable, "-m", "labflux.cli", "--config", str(config), *args],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=90,
        )
        if result.returncode:
            raise AssertionError(f"CLI {args[0]} failed: {result.stderr}")
        return result.stdout

    def read_output(job, name, directory):
        value = get(job)
        artifact = value["outputs"][-1]
        response = client.get(f"/api/artifacts/{artifact['id']}")
        response.raise_for_status()
        assert hashlib.sha256(response.content).hexdigest() == artifact["sha256"]
        archive = directory / f"{job['id']}.tar.gz"
        archive.write_bytes(response.content)
        destination = directory / job["id"]
        extract_archive(
            archive,
            destination,
            max_bytes=settings.max_extracted_bytes,
            max_members=settings.max_archive_members,
        )
        return (destination / name).read_text()

    try:
        nodes = wait_for(
            lambda: (
                values
                if len(values := [n for n in node_list() if n["status"] == "ONLINE"]) == 3
                else None
            ),
            description="three online VM workers",
        )
        assert sorted(n["capacity"]["cpu"] for n in nodes) == [1, 2, 4]
        assert len({n["capacity"]["total_memory_mib"] for n in nodes}) == 3
        report["nodes"] = [{"id": n["id"], "capacity": n["capacity"]} for n in nodes]
        passed("three heterogeneous VM workers", cpu=[1, 2, 4])

        with tempfile.TemporaryDirectory(prefix="labflux-smoke-") as temp:
            directory = Path(temp)
            source = directory / "source"
            source.mkdir()
            (source / "main.py").write_text(
                "from pathlib import Path\n"
                "p=Path('/datasets/demo/message.txt')\n"
                "assert p.read_text().strip() == 'labflux dataset'\n"
                "try:\n p.write_text('changed')\nexcept OSError:\n print('readonly dataset',flush=True)\n"
                "else:\n raise AssertionError('dataset was writable')\n"
                "Path('/outputs/result.txt').write_text('source -> VM -> output')\n"
                "print('source job completed',flush=True)\n"
            )
            spec = directory / "job.json"
            spec.write_text(
                json.dumps(
                    {
                        "image": settings.docker_image,
                        "argv": ["python", "main.py"],
                        "resources": {"cpu": 1, "memory_mib": 64},
                        "datasets": ["demo"],
                        "time_limit_s": 20,
                    }
                )
            )
            source_job = json.loads(cli("submit", str(spec), "--source", str(source), "--wait"))
            created.append(source_job)
            assert source_job["state"] == "SUCCEEDED"
            assert json.loads(cli("status", source_job["id"]))["state"] == "SUCCEEDED"
            assert len(json.loads(cli("nodes"))) == 3
            assert "readonly dataset" in cli("logs", source_job["id"])
            output_dir = directory / "cli-output"
            cli("fetch", source_job["id"], "--directory", str(output_dir))
            assert (output_dir / "result.txt").read_text() == "source -> VM -> output"
            passed("CLI source upload, readonly dataset, logs, status and result download")

            parallel = [
                submit(cpu=cpu, code="import time; print('parallel',flush=True); time.sleep(5)")
                for cpu in (1, 2, 4)
            ]
            wait_for(
                lambda: all(get(job)["state"] == "RUNNING" for job in parallel),
                description="jobs running concurrently across three VMs",
            )
            assigned = [get(job)["attempts"][0]["node_id"] for job in parallel]
            assert len(set(assigned)) == 3
            for job in parallel:
                successful(job)
            passed("resource-aware placement and real cross-VM concurrency", nodes=assigned)

            shared = [submit(cpu=cpu, code="import time; time.sleep(5)") for cpu in (1, 2, 2, 2)]
            wait_for(
                lambda: all(get(job)["state"] == "RUNNING" for job in shared),
                description="two simultaneous jobs on the four-CPU worker",
            )
            large = next(n for n in node_list() if n["capacity"]["cpu"] == 4)
            assert large["allocated"]["cpu"] == 4 and large["allocated"]["jobs"] == 2
            for job in shared:
                successful(job)
            passed("multiple actual containers share one node without over-allocation")

            busy = submit(cpu=4, code="import time; time.sleep(5)")
            wait_state(busy, {"RUNNING"})
            head = submit(cpu=4)
            tail = submit(cpu=1)
            assert get(head)["state"] == get(tail)["state"] == "QUEUED"
            assert sum(n["allocated"]["jobs"] == 0 for n in node_list()) == 2
            for job in (busy, head, tail):
                successful(job)
            passed("strict FCFS blocking, queueing and automatic resource release")

            limit_job = submit(
                cpu=0.25,
                code="from pathlib import Path; import json,time\n"
                "p=Path('/sys/fs/cgroup')\n"
                "values={k:(p/k).read_text().strip() for k in ['cpu.max','memory.max','memory.swap.max']}\n"
                "cpu=time.process_time(); start=time.monotonic()\n"
                "while time.monotonic()-start < 2: pass\n"
                "values['cpu_seconds']=time.process_time()-cpu\n"
                "values['wall_seconds']=time.monotonic()-start\n"
                "Path('/outputs/limits.json').write_text(json.dumps(values))\n",
            )
            successful(limit_job)
            values = json.loads(read_output(limit_job, "limits.json", directory))
            quota, period = map(int, values["cpu.max"].split())
            assert quota / period == 0.25
            assert int(values["memory.max"]) == 64 * 1024**2
            assert int(values["memory.swap.max"]) == 0
            assert values["cpu_seconds"] < 0.9, values
            passed("VM kernel limits and measured CPU throttling", **values)

            oom = submit(cpu=0.5, code="a=bytearray(512*1024*1024)")
            value = wait_state(oom)
            assert value["state"] == "FAILED" and value["attempts"][-1]["oom_killed"]
            passed("actual memory exhaustion is contained")
            timeout = submit(
                cpu=0.5,
                code="import subprocess,time; subprocess.Popen(['sleep','30']); time.sleep(30)",
                limit=0.5,
            )
            value = wait_state(timeout)
            assert value["state"] == "FAILED" and "time limit" in value["reason"]
            assert all(n["allocated"]["jobs"] == 0 for n in node_list())
            passed("timeout stops the container process tree and releases its allocation")

            active = submit(
                cpu=4, code="import time; print('waiting',flush=True); time.sleep(60)", limit=80
            )
            wait_state(active, {"RUNNING"})
            queued = submit(cpu=4)
            assert queued["state"] == "QUEUED"
            assert json.loads(cli("cancel", queued["id"]))["state"] == "CANCELLED"
            assert get(queued)["attempts"] == []
            cli("cancel", active["id"])
            assert wait_state(active)["state"] == "CANCELLED"
            assert all(n["allocated"]["jobs"] == 0 for n in node_list())
            passed("queued and running cancellation, with confirmed resource release")

            if faults:
                import vm_lab

                manifest = json.loads((config.parent / "manifest.json").read_text())
                target = next(vm for vm in manifest if vm["name"] == "labflux-vm-c")
                active = submit(cpu=4, code="import time; time.sleep(80)", limit=100)
                wait_state(active, {"RUNNING"})
                before = time.monotonic()
                vm_lab.ssh(
                    target["ip"],
                    "sudo systemctl kill --signal=SIGSTOP --kill-who=main labflux-worker",
                )
                frozen = target["ip"]
                wait_for(
                    lambda: (
                        next(n for n in node_list() if n["id"] == target["name"])["status"]
                        == "OFFLINE"
                    ),
                    timeout=settings.offline_timeout_s + 5,
                    description="heartbeat expiry",
                )
                elapsed = time.monotonic() - before
                assert get(active)["state"] == "UNKNOWN"
                lost = next(n for n in node_list() if n["id"] == target["name"])
                assert lost["allocated"]["cpu"] == 4
                following = submit(cpu=4)
                assert following["state"] == "QUEUED" and not following["attempts"]
                assert request("POST", f"/api/jobs/{active['id']}/cancel")["state"] == "STOPPING"
                assert (
                    next(n for n in node_list() if n["id"] == target["name"])["allocated"]["cpu"]
                    == 4
                )
                vm_lab.ssh(
                    frozen, "sudo systemctl kill --signal=SIGCONT --kill-who=main labflux-worker"
                )
                frozen = None
                assert wait_state(active)["state"] == "CANCELLED"
                successful(following)
                passed(
                    "heartbeat expiry excludes node, preserves UNKNOWN allocation and confirms cancellation",
                    detection_seconds=round(elapsed, 3),
                )

            report["jobs"] = [
                {
                    "id": job["id"],
                    "state": get(job)["state"],
                    "node": get(job)["attempts"][-1]["node_id"] if get(job)["attempts"] else None,
                }
                for job in created
            ]
            assert all(n["allocated"]["jobs"] == 0 for n in node_list())
            report["status"] = "PASSED"
    except BaseException as exc:
        report["status"] = "FAILED"
        report["error"] = str(exc)
        raise
    finally:
        if frozen:
            import vm_lab

            vm_lab.ssh(
                frozen,
                "sudo systemctl kill --signal=SIGCONT --kill-who=main labflux-worker",
                check=False,
            )
        for job in created:
            try:
                if get(job)["state"] not in TERMINAL:
                    request("POST", f"/api/jobs/{job['id']}/cancel")
            except httpx.HTTPError:
                pass
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        client.close()
    print(
        f"All {len(report['checks'])} acceptance checks passed. Evidence: {report_path}", flush=True
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--faults", action="store_true")
    parser.add_argument("--report", type=Path, default=ROOT / ".labflux/vms/smoke-report.json")
    args = parser.parse_args()
    run_smoke(args.config.resolve(), faults=args.faults, report_path=args.report)
