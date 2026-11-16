"""Flag lab runs that overlapped another user's LabFlux job (13.6 data hygiene).

The campaign refuses to start a run while anyone else's job is queued or running, but a job
submitted during a run competes with it. This reads each run's window [first submission, last
end] and asks the Scheduler database (read-only) for other users' jobs in it; flagged runs are
moved to raw/_overlapped/ so the campaign redoes them.

    uv run python scripts/experiments/overlap.py [--move]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from analyze import run_dirs  # noqa: E402
from common import EVIDENCE  # noqa: E402
from lab import Lab  # noqa: E402


def window(path: Path) -> tuple[float, float]:
    meta = json.loads((path / "meta.json").read_text())
    ends = [
        a["finished_at"]
        for record in json.loads((path / "jobs.json").read_text())
        for a in record["job"]["attempts"]
        if a["finished_at"]
    ]
    return meta["t0"], max(ends)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--move", action="store_true")
    args = parser.parse_args()
    lab = Lab()
    root = EVIDENCE / "raw"
    for path in run_dirs(root):
        if "_overlapped" in path.parts or "probes" in path.parts:
            continue
        start, end = window(path)
        found = lab.foreign_jobs_between(start, end)
        status = "clean" if not found else f"OVERLAP {found}"
        print(path.relative_to(root), status, flush=True)
        if found and args.move:
            target = root / "_overlapped" / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))


if __name__ == "__main__":
    main()
