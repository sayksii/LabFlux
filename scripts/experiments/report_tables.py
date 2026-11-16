"""Print the Markdown tables of docs/experiments.md from the raw runs (no hand-copied numbers).

    uv run python scripts/experiments/report_tables.py [--sim]
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from analyze import job_rows, load_run, run_dirs, summarize_run  # noqa: E402
from common import EVIDENCE  # noqa: E402

POLICIES = ("fcfs", "priority", "backfill")


def collect(root: Path) -> dict:
    groups: dict[tuple, list] = {}
    for path in run_dirs(root):
        if "_overlapped" in path.parts or "probes" in path.parts:
            continue
        run = load_run(path)
        summary = summarize_run(run)
        summary["rows"] = job_rows(run)
        groups.setdefault((summary["case"], summary["policy"], summary["variant"]), []).append(summary)
    return groups


def ms(items, key, pct=False, digits=0):
    values = [i[key] for i in items if i.get(key) is not None]
    if not values:
        return "–"
    mean = statistics.fmean(values)
    sd = statistics.stdev(values) if len(values) > 1 else 0
    scale = 100 if pct else 1
    text = f"{mean * scale:.{digits}f}"
    if len(values) > 1 and sd * scale >= 0.5 * 10**-digits:
        text += f" ± {sd * scale:.{digits}f}"
    return text + ("%" if pct else "")


def job_stat(items, job, key):
    values = [r[key] for i in items for r in i["rows"] if r["job"] == job and r[key] is not None]
    return f"{statistics.fmean(values):.0f}" if values else "–"


def main_table(groups, cases):
    print("| 案例 | 策略 | 次數 | 平均等待 s | p50 | p95 | makespan s | CPU 配置 | RAM 配置 | GPU 配置 | CPU 實際 | GPU 實際 | Jain |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for case in cases:
        for policy in POLICIES:
            items = groups.get((case, policy, "default"), [])
            if not items:
                continue
            print(
                f"| {case} | {policy} | {len(items)} | {ms(items, 'mean_wait_s')} | {ms(items, 'p50_wait_s')} "
                f"| {ms(items, 'p95_wait_s')} | {ms(items, 'makespan_s')} | {ms(items, 'alloc_cpu', True)} "
                f"| {ms(items, 'alloc_memory', True)} | {ms(items, 'alloc_gpu', True)} "
                f"| {ms(items, 'actual_cpu', True)} | {ms(items, 'actual_gpu', True)} | {ms(items, 'jain_wait', digits=3)} |"
            )


def users_table(groups):
    print("| 策略 | alice 平均等待 s | bob | carol | 全體平均 | Jain |")
    print("|---|---|---|---|---|---|")
    for policy in POLICIES:
        items = groups.get(("users", policy, "default"), [])
        if not items:
            continue
        waits = {
            u: statistics.fmean([i["per_user_wait"][u] for i in items if u in i["per_user_wait"]])
            for u in ("alice", "bob", "carol")
        }
        print(
            f"| {policy} | {waits['alice']:.0f} | {waits['bob']:.0f} | {waits['carol']:.0f} "
            f"| {ms(items, 'mean_wait_s')} | {ms(items, 'jain_wait', digits=3)} |"
        )


def urgent_table(groups):
    print(
        "| 設定 | P9 平均等待 s | bob（P5）平均等待 s | alice（P2）平均 turnaround s | 搶占次數 "
        "| 浪費的執行秒數 | makespan s | CPU 配置 |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for policy, variant, label in (
        ("fcfs", "default", "fcfs"),
        ("priority", "no-preemption", "priority，工作不可搶占"),
        ("priority", "default", "priority，可搶占"),
        ("backfill", "default", "backfill，可搶占"),
    ):
        items = groups.get(("urgent", policy, variant), [])
        if not items:
            continue
        bob = statistics.fmean([i["per_user_wait"].get("bob", 0) for i in items])
        alice = statistics.fmean(
            [r["turnaround_s"] for i in items for r in i["rows"] if r["user"] == "alice"]
        )
        print(
            f"| {label} | {ms(items, 'urgent_mean_wait_s', digits=1)} | {bob:.0f} | {alice:.0f} "
            f"| {ms(items, 'preemptions')} "
            f"| {ms(items, 'wasted_s')} | {ms(items, 'makespan_s')} | {ms(items, 'alloc_cpu', True)} |"
        )


def backfill_cost(groups):
    print("| 案例 | 被保留的大工作 | fcfs 開始 s | priority 開始 s | backfill 開始 s | backfill 先跑的工作數 | 晚於保留時刻開始 |")
    print("|---|---|---|---|---|---|---|")
    for case, job, label in (
        ("gpu", "g03", "g03（2 張卡）"),
        ("contention", "k03", "k03（2 張卡）"),
        ("contention", "k09", "k09（40 GiB）"),
        ("contention", "k07", "k07（24 核）"),
    ):
        cells = [job_stat(groups.get((case, p, "default"), []), job, "dispatch_s") for p in POLICIES]
        items = groups.get((case, "backfill", "default"), [])
        print(
            f"| {case} | {label} | {cells[0]} | {cells[1]} | {cells[2]} | {ms(items, 'backfilled')} "
            f"| {ms(items, 'reservations_broken')} |"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sim", action="store_true")
    parser.add_argument("--probes", action="store_true")
    args = parser.parse_args()
    groups = collect(EVIDENCE / ("sim" if args.sim else "raw"))
    cases = ["gpu", "contention", "users", "urgent"] + (["cpu", "ram", "mixed"] if args.sim else [])
    print("### main\n")
    main_table(groups, cases)
    print("\n### users\n")
    users_table(groups)
    print("\n### urgent\n")
    urgent_table(groups)
    print("\n### backfill cost\n")
    backfill_cost(groups)


if __name__ == "__main__":
    main()


def probes_tables() -> None:
    """13.5 / 13.7 probe summaries from docs/evidence/m13/raw/probes."""
    import json

    root = EVIDENCE / "raw" / "probes"
    print("\n### interactive\n")
    print("| 叢集滿載的 8 核工作 | 次數 | `labflux run` 第一行輸出（冷，s） | 同一 session 第二個指令（熱，s） | 被搶占的工作浪費的秒數 |")
    print("|---|---|---|---|---|")
    for variant, label in (("preemptible", "可搶占"), ("not-preemptible", "不可搶占")):
        items = [json.loads(p.read_text()) for p in sorted((root / "interactive").glob(f"{variant}-rep*.json"))]
        if not items:
            continue
        cold = [next(c for c in i["commands"] if c["command"] == "cold")["first_output_s"] for i in items]
        warm = [next(c for c in i["commands"] if c["command"] == "warm")["first_output_s"] for i in items]
        lost = [
            (a["finished_at"] or 0) - (a["started_at"] or 0)
            for i in items
            for f in i["fill"]
            for a in f["attempts"]
            if a["state"] == "PREEMPTED"
        ]
        print(
            f"| {label} | {len(items)} | {statistics.fmean(cold):.1f}（{min(cold):.1f}–{max(cold):.1f}） "
            f"| {statistics.fmean(warm):.2f} | {statistics.fmean(lost) if lost else 0:.0f}（{len(lost)} 次） |"
        )
    print("\n### model\n")
    print("| 情況 | 次數 | 冷啟動請求（s） | 熱請求平均／最大（s） | 等 4090 的工作等待（s） | 模型閒置到回收（s） |")
    print("|---|---|---|---|---|---|")
    for variant, label in (("free", "4090 空著"), ("contended", "4090 被可搶占工作占用")):
        items = [json.loads(p.read_text()) for p in sorted((root / "model").glob(f"{variant}-rep*.json"))]
        if not items:
            continue
        cold = [i["cold"]["seconds"] for i in items]
        warm = [w["seconds"] for i in items for w in i["warm"]]
        wait = [i["reclaim"]["wait_s"] for i in items]
        idle = [i["reclaim"]["idle_before_dispatch_s"] for i in items]
        print(
            f"| {label} | {len(items)} | {statistics.fmean(cold):.2f} | {statistics.fmean(warm):.3f}／{max(warm):.3f} "
            f"| {statistics.fmean(wait):.1f} | {statistics.fmean(idle):.1f} |"
        )
    failure = root / "failure" / "karma-rep1.json"
    if failure.exists():
        data = json.loads(failure.read_text())
        events = {e["event"]: e["t"] for e in data["events"]}
        print("\n### failure\n")
        print(f"- Worker 停止到 OFFLINE：{events['karma offline'] - events['worker stopped']:.1f} s")
        print(f"- Worker 啟動到 ONLINE：{events['karma online'] - events['worker started']:.1f} s")
        for name, job in data["jobs"].items():
            parts = [
                f"{a['node_id']} {a['state']} {((a['finished_at'] or 0) - (a['started_at'] or 0)):.0f}s"
                for a in job["attempts"]
            ]
            print(f"- {name}: {job['state']}；" + "、".join(parts))


if __name__ == "__main__" and "--probes" in sys.argv:
    probes_tables()
