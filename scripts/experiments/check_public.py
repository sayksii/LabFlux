"""Refuse to publish M13 files that contain lab identifiers (the repository is public).

Scans the M13 evidence, workloads, scripts and report for IPv4 addresses other than the
documentation range and loopback, real GPU UUIDs (the simulator's are all zeros), the Unix
account names configured on the lab, and every token in the git-ignored deploy state.

    uv run python scripts/experiments/check_public.py
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".labflux" / "lab"
TARGETS = [
    ROOT / "docs" / "evidence" / "m13",
    ROOT / "docs" / "experiments.md",
    ROOT / "experiments",
    ROOT / "scripts" / "experiments",
]
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
ALLOWED_IP = re.compile(r"^(192\.0\.2\.\d+|127\.0\.0\.1|0\.0\.0\.0)$")
GPU = re.compile(r"GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
FAKE_GPU = re.compile(r"GPU-0000000\d-0000-0000-0000-000000000000")


def secrets() -> set[str]:
    found: set[str] = set()

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, str) and ("token" in key.lower() or len(item) >= 32):
                    found.add(item)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for path in STATE.glob("*.toml"):
        try:
            values = tomllib.loads(path.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            continue
        walk(values)
        for section in ("users",):
            for name, token in values.get(section, {}).items():
                found.add(token)
                if not name.startswith("m13s"):
                    found.add(name)  # a real Unix account name
    return {s for s in found if len(s) >= 6}


def main() -> int:
    needles = secrets()
    problems = []
    for target in TARGETS:
        files = (
            [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())
        )
        for path in files:
            if path.suffix in (".png", ".pyc") or "__pycache__" in path.parts:
                continue
            text = path.read_text(errors="replace")
            rel = path.relative_to(ROOT)
            for ip in set(IPV4.findall(text)):
                if not ALLOWED_IP.match(ip) and all(0 <= int(p) <= 255 for p in ip.split(".")):
                    problems.append(f"{rel}: IP address {ip}")
            for uuid in set(GPU.findall(text)):
                if not FAKE_GPU.fullmatch(uuid):
                    problems.append(f"{rel}: GPU UUID {uuid[:12]}...")
            for needle in needles:
                if needle in text:
                    problems.append(
                        f"{rel}: contains a lab secret or account name ({needle[:3]}...)"
                    )
    for problem in problems:
        print(problem)
    print("clean" if not problems else f"{len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
