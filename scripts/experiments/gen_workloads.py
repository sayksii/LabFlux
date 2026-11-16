"""Write the simulation-only M13 workloads (cpu, ram, mixed) to experiments/workloads/.

cpu and ram isolate one contended resource each; mixed is drawn once from a fixed seed so that
it is not hand-tuned to favor any policy. The lab cases (gpu, contention, users, urgent) are
hand-written TOML files next to them.

    uv run python scripts/experiments/gen_workloads.py
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from common import WORKLOADS  # noqa: E402

USERS = ("alice", "bob", "carol")


def render(name: str, description: str, jobs: list[dict]) -> str:
    lines = [f'name = "{name}"', f'description = """{description}"""', ""]
    for job in jobs:
        lines.append("[[jobs]]")
        for key, value in job.items():
            text = f'"{value}"' if isinstance(value, str) else str(value).lower() if isinstance(value, bool) else value
            lines.append(f"{key} = {text}")
        lines.append("")
    return "\n".join(lines)


def cpu() -> list[dict]:
    jobs = [
        dict(id="c01", at=0, user="alice", cpu=16, memory_mib=2048, run_s=90, time_limit_s=150, load="cpu"),
        dict(id="c02", at=1, user="bob", cpu=16, memory_mib=2048, run_s=120, time_limit_s=180, load="cpu"),
        dict(id="c03", at=2, user="carol", cpu=8, memory_mib=2048, run_s=60, time_limit_s=120, load="cpu"),
        dict(id="c04", at=4, user="alice", cpu=24, memory_mib=2048, run_s=60, time_limit_s=120, load="cpu"),
    ]
    for i in range(8):
        jobs.append(
            dict(id=f"c{i + 5:02d}", at=6 + 4 * i, user=USERS[i % 3], cpu=4, memory_mib=1024,
                 run_s=30 + 15 * (i % 3), time_limit_s=60 + 30 * (i % 3), load="cpu")
        )
    return jobs


def ram() -> list[dict]:
    jobs = [
        dict(id="r01", at=0, user="alice", cpu=2, memory_mib=40960, run_s=90, time_limit_s=150),
        dict(id="r02", at=1, user="bob", cpu=2, memory_mib=20480, run_s=75, time_limit_s=120),
        dict(id="r03", at=2, user="carol", cpu=2, memory_mib=40960, run_s=60, time_limit_s=120),
    ]
    for i in range(8):
        jobs.append(
            dict(id=f"r{i + 4:02d}", at=4 + 4 * i, user=USERS[i % 3], cpu=1,
                 memory_mib=4096 * (1 + i % 3), run_s=30 + 15 * (i % 3), time_limit_s=60 + 30 * (i % 3))
        )
    return jobs


def mixed(seed: int = 13, count: int = 24) -> list[dict]:
    rng = random.Random(seed)
    jobs, at = [], 0.0
    for i in range(count):
        kind = rng.choices(["small", "wide", "ram", "gpu"], weights=[50, 20, 15, 15])[0]
        run = rng.choice([30, 45, 60, 90])
        limit = int(run * rng.choice([1.5, 2, 3]))
        job = dict(id=f"m{i + 1:02d}", at=round(at, 1), user=rng.choice(USERS))
        if kind == "small":
            job.update(cpu=rng.choice([2, 4]), memory_mib=rng.choice([2048, 4096, 6144]))
        elif kind == "wide":
            job.update(cpu=rng.choice([12, 16, 24]), memory_mib=4096)
        elif kind == "ram":
            job.update(cpu=2, memory_mib=rng.choice([24576, 32768, 40960]))
        else:
            job.update(cpu=4, memory_mib=8192, gpu=rng.choice([1, 1, 2]), load="gpu")
        job.update(run_s=run, time_limit_s=limit)
        jobs.append(job)
        at += rng.expovariate(1 / 8)  # one job every 8 s on average
    return jobs


def main() -> None:
    WORKLOADS.mkdir(parents=True, exist_ok=True)
    (WORKLOADS / "cpu.toml").write_text(render(
        "cpu",
        "CPU contention, big and small jobs (simulation only). A 24-core job only fits kindred and\n"
        "blocks the queue while small 4-core jobs could run elsewhere.",
        cpu(),
    ))
    (WORKLOADS / "ram.toml").write_text(render(
        "ram",
        "RAM contention (simulation only). Two 40 GiB jobs only fit kayle; small 4-12 GiB jobs\n"
        "could use karma and kindred meanwhile.",
        ram(),
    ))
    (WORKLOADS / "mixed.toml").write_text(render(
        "mixed",
        "Mixed CPU/RAM/GPU workload drawn once with random seed 13 (simulation only): 24 jobs,\n"
        "Poisson arrivals every 8 s on average, 50% small, 20% wide CPU, 15% RAM, 15% GPU.",
        mixed(),
    ))


if __name__ == "__main__":
    main()
