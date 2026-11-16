"""M13 job program: hold an allocation for a fixed wall time and report what it used.

Run inside a LabFlux job as `python -c <this file> MODE RUN_S CPUS`. The wall time is fixed so
that every policy sees the same job lengths on every node; the node's speed only changes how
much work fits in it.

  sleep  hold the allocation without using it (waiting time and allocation only)
  cpu    keep ceil(CPUS) busy processes until the deadline; the CPU quota caps the real use
  gpu    run a float32 matmul loop on the allocated card (needs an image with PyTorch)

The last line is `M13RESULT {json}` with the wall time and the container's CPU seconds from
its cgroup (cpu.stat usage_usec), so actual CPU use is measured per job, not per machine.
"""

import json
import math
import multiprocessing
import os
import sys
import time


def cgroup_cpu_s():
    try:
        with open("/sys/fs/cgroup/cpu.stat") as stream:
            for line in stream:
                key, value = line.split()
                if key == "usage_usec":
                    return int(value) / 1e6
    except (OSError, ValueError):
        pass
    return None


def burn(deadline):
    value = 0
    while time.monotonic() < deadline:
        for _ in range(20000):
            value += 1


def main():
    mode, run_s, cpus = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    start = time.monotonic()
    deadline = start + run_s
    before = cgroup_cpu_s()
    extra = {}
    if mode == "cpu":
        workers = [
            multiprocessing.Process(target=burn, args=(deadline,))
            for _ in range(max(1, math.ceil(cpus)))
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
    elif mode == "gpu":
        import torch

        # Every allocated card gets the same loop; kernels are queued on all cards, then each
        # is synchronized, so a two-card job keeps both busy.
        devices = [f"cuda:{i}" for i in range(torch.cuda.device_count())]
        pairs = [
            (torch.randn(4096, 4096, device=d), torch.randn(4096, 4096, device=d))
            for d in devices
        ]
        iterations = 0
        while time.monotonic() < deadline:
            for _ in range(10):
                for a, b in pairs:
                    a @ b
            for d in devices:
                torch.cuda.synchronize(d)
            iterations += 10
        extra = {
            "matmuls_per_card": iterations,
            "devices": [torch.cuda.get_device_name(d) for d in devices],
        }
    else:
        time.sleep(max(0.0, deadline - time.monotonic()))
    after = cgroup_cpu_s()
    used = None if before is None or after is None else round(after - before, 3)
    result = {
        "mode": mode,
        "run_s": run_s,
        "cpus": cpus,
        "wall_s": round(time.monotonic() - start, 3),
        "cpu_s": used,
        "experiment": os.environ.get("LABFLUX_EXPERIMENT"),
        **extra,
    }
    print("M13RESULT " + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
