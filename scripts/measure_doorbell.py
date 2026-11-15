#!/usr/bin/env python3
"""Measure command delivery latency and the idle request rate (doorbell T4).

Runs a local Scheduler on a free loopback port and one Worker in this process. The executor is
a fake, so nothing touches Docker or GPUs. Latency is from the submit or cancel request to the
Worker's accept() of the START or STOP command. To measure an older revision, extract its src/
and put it first on PYTHONPATH.

Usage: uv run python scripts/measure_doorbell.py [label] [idle_seconds] [rounds]
"""

import collections
import json
import random
import shutil
import socket
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn

import labflux
from labflux.api import create_app
from labflux.config import Settings
from labflux.runtime import ExecutionResult
from labflux.worker import Worker

label = sys.argv[1] if len(sys.argv) > 1 else "current"
IDLE_S = float(sys.argv[2]) if len(sys.argv) > 2 else 20
ROUNDS = int(sys.argv[3]) if len(sys.argv) > 3 else 15
root = Path(tempfile.mkdtemp(prefix="labflux-measure-"))


class FakeExecutor:
    def run(self, spec, workspace, outputs, on_log, on_started, cancel, **options):
        if options.get("on_launch"):
            options["on_launch"]()
        on_started()
        while not cancel.wait(0.01):
            if options.get("detach") is not None and options["detach"].is_set():
                return None
        return ExecutionResult("CANCELLED", 137, "cancelled", True)

    def check(self):
        return {}

    def stop_unstarted(self, spec):
        return ExecutionResult("CANCELLED", 137, "confirmed stopped", True)

    def inventory(self, worker_id):
        return []

    def remove(self, worker_id, attempt_id):
        return True


counts = collections.Counter()


def counting(app):
    async def wrapper(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope["path"]
            kind = path.rsplit("/", 1)[-1] if path.startswith("/internal/workers/") else path
            if "/attempts/" in path:
                kind = "attempt:" + kind
            counts[(scope["type"], kind)] += 1
        await app(scope, receive, send)

    return wrapper


app = create_app(Settings(data_dir=root / "scheduler"))
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
sock.listen()
address = f"http://127.0.0.1:{sock.getsockname()[1]}"
server = uvicorn.Server(uvicorn.Config(counting(app), log_level="error", lifespan="on"))
threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True).start()
while not server.started:
    time.sleep(0.01)

worker_settings = Settings(
    data_dir=root / "worker",
    scheduler_url=address,
    worker_id="bench-a",
    allocatable_cpu=1,
    allocatable_memory_mib=256,
)
worker = Worker(worker_settings, executor=FakeExecutor())
received = {}
original = worker.accept


def timed_accept(command):
    received.setdefault(command["id"], time.perf_counter())
    return original(command)


worker.accept = timed_accept
worker_thread = threading.Thread(target=worker.run, daemon=True)
worker_thread.start()
user = httpx.Client(
    base_url=address, headers={"Authorization": "Bearer dev-user-token"}, trust_env=False
)


def wait(predicate, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.002)
    raise TimeoutError


wait(lambda: user.get("/api/nodes").json())
time.sleep(3)  # Warm up: registration, first polls, doorbell connection.
counts.clear()
time.sleep(IDLE_S)
idle = dict(counts)
idle_http = sum(v for (kind, _), v in idle.items() if kind == "http")

spec = {"image": "python:3.12-slim", "argv": ["true"], "resources": {"cpu": 1}}
start_latency, stop_latency = [], []
for _ in range(ROUNDS):
    time.sleep(random.uniform(0.3, 1.3))  # Random phase relative to any poll timer.
    t0 = time.perf_counter()
    job = user.post("/api/jobs", json=spec).json()
    command_id = f"start-{job['current_attempt_id']}"
    wait(lambda: command_id in received)
    start_latency.append(received[command_id] - t0)
    wait(lambda: user.get(f"/api/jobs/{job['id']}").json()["state"] == "RUNNING")
    time.sleep(random.uniform(0.3, 1.3))
    t0 = time.perf_counter()
    user.post(f"/api/jobs/{job['id']}/cancel")
    command_id = f"stop-{job['current_attempt_id']}"
    wait(lambda: command_id in received)
    stop_latency.append(received[command_id] - t0)
    wait(lambda: user.get(f"/api/jobs/{job['id']}").json()["state"] == "CANCELLED")


def summary(values):
    ms = sorted(v * 1000 for v in values)
    return {
        "mean_ms": round(statistics.mean(ms), 1),
        "median_ms": round(statistics.median(ms), 1),
        "p90_ms": round(ms[int(0.9 * (len(ms) - 1))], 1),
        "max_ms": round(ms[-1], 1),
    }


worker.stop_event.set()
worker_thread.join(timeout=20)
server.should_exit = True
shutil.rmtree(root, ignore_errors=True)
print(
    json.dumps(
        {
            "label": label,
            "labflux": labflux.__file__,
            "poll_interval_s": worker_settings.poll_interval_s,
            "idle_seconds": IDLE_S,
            "idle_http_requests_per_s": round(idle_http / IDLE_S, 2),
            "idle_breakdown": {f"{k[0]} {k[1]}": v for k, v in sorted(idle.items())},
            "rounds": ROUNDS,
            "start_submit_to_worker": summary(start_latency),
            "cancel_to_worker": summary(stop_latency),
        },
        indent=1,
    )
)
