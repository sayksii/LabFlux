"""M13 charts from the raw runs (matplotlib, `uv run --group experiments`).

    uv run --group experiments python scripts/experiments/plots.py

Colors follow the policy everywhere: fcfs blue, priority orange, backfill aqua (the first three
slots of the dataviz reference palette, validated for all pairs). Gantt bars are neutral gray;
only backfilled jobs take the backfill color and preempted attempts carry a hatch, so the eye
goes to the scheduling behavior being explained.
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))

from analyze import job_rows, load_run, run_dirs, summarize_run  # noqa: E402
from common import EVIDENCE  # noqa: E402

POLICIES = ("fcfs", "priority", "backfill")
COLOR = {"fcfs": "#2a78d6", "priority": "#eb6834", "backfill": "#1baf7a"}
INK, INK2, MUTED, GRID, AXIS, SURFACE = (
    "#0b0b0b",
    "#52514e",
    "#898781",
    "#e1e0d9",
    "#c3c2b7",
    "#fcfcfb",
)
NEUTRAL = "#c3c2b7"
CASE_TITLE = {
    "gpu": "GPU 爭用",
    "contention": "CPU／RAM／GPU 混合爭用",
    "users": "使用者競爭（公平性）",
    "urgent": "高優先權插入",
    "cpu": "CPU 爭用",
    "ram": "RAM 爭用",
    "mixed": "固定種子混合負載",
}

plt.rcParams.update(
    {
        "font.family": ["Noto Sans CJK TC", "DejaVu Sans"],
        "font.size": 11,
        "axes.edgecolor": AXIS,
        "axes.labelcolor": INK2,
        "axes.titlecolor": INK,
        "axes.titlesize": 12,
        "axes.titleweight": "bold",
        "axes.facecolor": SURFACE,
        "figure.facecolor": SURFACE,
        "xtick.color": INK2,
        "ytick.color": INK2,
        "axes.grid": True,
        "axes.grid.axis": "y",
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.axisbelow": True,
        "legend.frameon": False,
        "savefig.dpi": 200,
    }
)


def collect(root: Path) -> dict:
    """{(case, policy, variant): [summary, ...]} plus the runs themselves."""
    groups: dict[tuple, list] = {}
    for path in run_dirs(root):
        run = load_run(path)
        summary = summarize_run(run)
        summary["run"] = run
        groups.setdefault((summary["case"], summary["policy"], summary["variant"]), []).append(
            summary
        )
    return groups


def stat(items: list[dict], key: str):
    values = [i[key] for i in items if i.get(key) is not None]
    if not values:
        return None, None
    return statistics.fmean(values), (statistics.stdev(values) if len(values) > 1 else 0.0)


def bars(ax, labels, values, errors, colors, fmt="{:.0f}", unit=""):
    xs = range(len(labels))
    ax.bar(
        xs,
        [v or 0 for v in values],
        width=0.62,
        color=colors,
        yerr=[e or 0 for e in errors],
        error_kw={"ecolor": INK2, "elinewidth": 1, "capsize": 3},
        edgecolor=SURFACE,
        linewidth=2,
    )
    for x, v in zip(xs, values):
        if v is not None:
            ax.annotate(
                fmt.format(v) + unit,
                (x, v),
                xytext=(0, 4),
                textcoords="offset points",
                ha="center",
                va="bottom",
                color=INK,
                fontsize=10,
            )
    ax.set_xticks(list(xs), labels)
    ax.tick_params(axis="x", length=0)


def fig_wait(groups: dict, cases: list[str], out: Path, title: str) -> None:
    fig, axes = plt.subplots(1, len(cases), figsize=(3.2 * len(cases), 3.6), squeeze=False)
    for ax, case in zip(axes[0], cases):
        means, errs = [], []
        for policy in POLICIES:
            m, e = stat(groups.get((case, policy, "default"), []), "mean_wait_s")
            means.append(m)
            errs.append(e)
        bars(ax, POLICIES, means, errs, [COLOR[p] for p in POLICIES], unit=" s")
        ax.set_title(CASE_TITLE.get(case, case), loc="left")
        ax.set_ylabel("平均等待（秒）" if ax is axes[0][0] else "")
        ax.margins(y=0.18)
    fig.suptitle(title, x=0.01, ha="left", fontsize=13, fontweight="bold", color=INK)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def fig_utilization(groups: dict, cases: list[str], out: Path) -> None:
    metrics = [
        ("alloc_cpu", "CPU 配置"),
        ("alloc_memory", "RAM 配置"),
        ("alloc_gpu", "GPU 配置"),
        ("actual_cpu", "CPU 實際"),
        ("actual_gpu", "GPU 實際"),
    ]
    fig, axes = plt.subplots(1, len(cases), figsize=(5.6 * len(cases), 3.8), squeeze=False)
    width = 0.26
    for ax, case in zip(axes[0], cases):
        shown = [
            (key, label)
            for key, label in metrics
            if any(stat(groups.get((case, p, "default"), []), key)[0] for p in POLICIES)
        ]
        for i, policy in enumerate(POLICIES):
            items = groups.get((case, policy, "default"), [])
            values = [stat(items, key) for key, _ in shown]
            xs = [j + (i - 1) * width for j in range(len(shown))]
            ax.bar(
                xs,
                [(v[0] or 0) * 100 for v in values],
                width=width,
                color=COLOR[policy],
                yerr=[(v[1] or 0) * 100 for v in values],
                error_kw={"ecolor": INK2, "elinewidth": 1, "capsize": 2},
                edgecolor=SURFACE,
                linewidth=1.5,
                label=policy,
            )
        ax.set_xticks(range(len(shown)), [label for _, label in shown])
        ax.tick_params(axis="x", length=0)
        ax.set_ylim(0, 100)
        ax.set_ylabel("占實驗容量 %")
        ax.set_title(CASE_TITLE.get(case, case), loc="left")
        ax.axvline(2.5, color=GRID, linewidth=1) if len(shown) > 3 else None
    axes[0][0].legend(loc="upper right", ncols=3)
    fig.suptitle(
        "配置占用率（排程器預約）與實際利用率（cgroup／NVML 量測）分開呈現",
        x=0.01,
        ha="left",
        fontsize=13,
        fontweight="bold",
        color=INK,
    )
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def fig_fairness(groups: dict, out: Path) -> None:
    fig, ax = plt.subplots(figsize=(6.4, 3.8))
    users = ["alice", "bob", "carol"]
    width = 0.26
    for i, policy in enumerate(POLICIES):
        items = groups.get(("users", policy, "default"), [])
        values = []
        for user in users:
            waits = [it["per_user_wait"].get(user) for it in items if user in it["per_user_wait"]]
            values.append(statistics.fmean(waits) if waits else 0)
        jain, _ = stat(items, "jain_wait")
        xs = [j + (i - 1) * width for j in range(len(users))]
        label = f"{policy}（Jain {jain:.2f}）" if jain else policy
        ax.bar(
            xs,
            values,
            width=width,
            color=COLOR[policy],
            edgecolor=SURFACE,
            linewidth=1.5,
            label=label,
        )
    ax.set_xticks(range(len(users)), ["alice（最早送出）", "bob（晚 10 秒）", "carol（晚 20 秒）"])
    ax.tick_params(axis="x", length=0)
    ax.set_ylabel("每位使用者平均等待（秒）")
    ax.set_title("需求相同的三位使用者：公平性", loc="left")
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def fig_urgent(groups: dict, out: Path) -> None:
    variants = [
        ("fcfs", "default", "fcfs"),
        ("priority", "no-preemption", "priority\n不搶占"),
        ("priority", "default", "priority\n搶占"),
        ("backfill", "default", "backfill\n搶占"),
    ]
    fig, axes = plt.subplots(1, 2, figsize=(9.6, 3.8))
    means, errs, wasted, colors, labels = [], [], [], [], []
    for policy, var, label in variants:
        items = groups.get(("urgent", policy, var), [])
        m, e = stat(items, "urgent_mean_wait_s")
        w, _ = stat(items, "wasted_s")
        means.append(m)
        errs.append(e)
        wasted.append(w)
        colors.append(COLOR[policy])
        labels.append(label)
    bars(axes[0], labels, means, errs, colors, unit=" s")
    axes[0].set_title("P9 工作的反應時間（送出到配置）", loc="left")
    axes[0].set_ylabel("秒")
    axes[0].margins(y=0.18)
    bars(axes[1], labels, wasted, [0] * len(wasted), colors, unit=" s")
    axes[1].set_title("代價：被搶占而浪費的執行秒數", loc="left")
    axes[1].margins(y=0.18)
    for ax in axes:
        for tick, (_, var, _) in zip(ax.get_xticklabels(), variants):
            if var == "no-preemption":
                tick.set_color(INK2)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def lanes_for(rows: list[dict], case: str):
    """Rows of the Gantt chart: one lane per GPU card for the GPU case, otherwise packed
    sub-lanes per node (each job keeps one lane for all its time on that node)."""
    if case == "gpu":
        return None
    return True


def fig_gantt(run: dict, out: Path, title: str) -> None:
    rows = job_rows(run)
    case = run["meta"]["case"]
    attempts = []
    t0 = run["meta"]["t0"]
    by_wid = {r["job"]: r for r in rows}
    for record in run["records"]:
        for attempt in record["job"]["attempts"]:
            if attempt["finished_at"] is None:
                continue
            attempts.append(
                {
                    "wid": record["wid"],
                    "node": attempt["node_id"],
                    "gpus": attempt.get("gpu_labels", []),
                    "start": attempt["created_at"] - t0,
                    "end": attempt["finished_at"] - t0,
                    "preempted": attempt["state"] == "PREEMPTED",
                    "backfilled": by_wid[record["wid"]]["backfilled"]
                    and attempt is record["job"]["attempts"][-1],
                    "label": f"{record['wid']} {record['user']}",
                    "priority": by_wid[record["wid"]]["priority"],
                }
            )
    lanes: list[tuple[str, list]] = []
    placed = []
    if case == "gpu":
        cards = ["kayle/RTX 3090", "kayle/RTX A5000", "karma/RTX 4090"]
        lanes = [(c, []) for c in cards]
        for a in attempts:
            for card in a["gpus"]:
                placed.append((cards.index(card), a))
    else:
        for node in ("kayle", "karma", "kindred"):
            sub: list[list] = []
            for a in sorted((a for a in attempts if a["node"] == node), key=lambda a: a["start"]):
                for i, lane in enumerate(sub):
                    if lane[-1]["end"] <= a["start"] + 0.5:
                        lane.append(a)
                        placed.append((len(lanes) + i, a))
                        break
                else:
                    sub.append([a])
                    placed.append((len(lanes) + len(sub) - 1, a))
            lanes += [(node if i == 0 else "", []) for i in range(max(1, len(sub)))]
    height = max(2.4, 0.34 * len(lanes) + 1.2)
    fig, ax = plt.subplots(figsize=(10, height))
    for lane, a in placed:
        color = COLOR["backfill"] if a["backfilled"] else NEUTRAL
        ax.barh(
            lane,
            a["end"] - a["start"],
            left=a["start"],
            height=0.72,
            color=color,
            edgecolor=SURFACE,
            linewidth=1.5,
            hatch="////" if a["preempted"] else None,
        )
        text = a["label"] + (" P9" if a["priority"] >= 8 else "")
        if a["end"] - a["start"] > 22:
            ax.text(a["start"] + 1.5, lane, text, va="center", fontsize=7.5, color=INK)
    ax.set_yticks(range(len(lanes)), [name for name, _ in lanes])
    ax.invert_yaxis()
    ax.tick_params(axis="y", length=0)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("自第一個工作送出起的秒數（含排程與容器啟動）")
    ax.set_title(title, loc="left")
    legend = [Patch(facecolor=NEUTRAL, label="依序開始")]
    if any(a["backfilled"] for a in attempts):
        legend.append(Patch(facecolor=COLOR["backfill"], label="backfill 先跑"))
    if any(a["preempted"] for a in attempts):
        legend.append(Patch(facecolor=NEUTRAL, hatch="////", label="被搶占的執行"))
    ax.set_xlim(0, max(a["end"] for a in attempts) * 1.02)
    ax.legend(
        handles=legend,
        loc="lower right",
        bbox_to_anchor=(1.0, 1.0),
        ncols=len(legend),
        fontsize=9,
        borderaxespad=0.2,
    )
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def fig_summary(groups: dict, out: Path, footnote: str = "") -> None:
    """One 16:9 slide: GPU case mean wait and GPU allocation, plus the contention case."""
    fig = plt.figure(figsize=(13.33, 7.5))
    fig.suptitle(
        "LabFlux 排程策略比較（實驗室三台實機，每組 3 次）",
        x=0.03,
        y=0.97,
        ha="left",
        fontsize=20,
        fontweight="bold",
        color=INK,
    )
    grid = fig.add_gridspec(
        2, 3, left=0.06, right=0.98, top=0.86, bottom=0.15, hspace=0.55, wspace=0.3
    )
    panels = [
        (grid[0, 0], "gpu", "mean_wait_s", "GPU 爭用：平均等待", " s", 1),
        (grid[0, 1], "gpu", "alloc_gpu", "GPU 爭用：GPU 配置占用率", "%", 100),
        (grid[0, 2], "gpu", "actual_gpu", "GPU 爭用：GPU 實際利用率", "%", 100),
        (grid[1, 0], "contention", "mean_wait_s", "混合爭用：平均等待", " s", 1),
        (grid[1, 1], "contention", "alloc_cpu", "混合爭用：CPU 配置占用率", "%", 100),
        (grid[1, 2], "users", "jain_wait", "使用者競爭：Jain 公平性指數", "", 1),
    ]
    for spec, case, key, title, unit, scale in panels:
        ax = fig.add_subplot(spec)
        means, errs = [], []
        for policy in POLICIES:
            m, e = stat(groups.get((case, policy, "default"), []), key)
            means.append(None if m is None else m * scale)
            errs.append(None if e is None else e * scale)
        fmt = "{:.2f}" if key == "jain_wait" else "{:.0f}"
        bars(ax, POLICIES, means, errs, [COLOR[p] for p in POLICIES], fmt=fmt, unit=unit)
        ax.set_title(title, loc="left", fontsize=13)
        ax.margins(y=0.2)
        if key == "jain_wait":
            ax.set_ylim(0, 1.15)
    if footnote:
        fig.text(0.03, 0.015, footnote, ha="left", va="bottom", fontsize=11.5, color=INK2)
    fig.savefig(out)
    plt.close(fig)


def dispatch_mean(groups: dict, case: str, policy: str, job: str):
    values = [
        r["dispatch_s"]
        for item in groups.get((case, policy, "default"), [])
        for r in job_rows(item["run"])
        if r["job"] == job
    ]
    return statistics.fmean(values) if values else None


def summary_footnote(groups: dict) -> str:
    """Costs and non-improvements for the slide, computed from the runs."""
    parts = []
    wait = {
        p: stat(groups.get(("contention", p, "default"), []), "mean_wait_s")[0] for p in POLICIES
    }
    if wait["priority"] and wait["fcfs"]:
        parts.append(
            f"priority 沒有縮短等待（混合爭用 {wait['priority']:.0f} s 對 FCFS {wait['fcfs']:.0f} s）："
            "隊首放不下時後面仍全部等待"
        )
    late = dispatch_mean(groups, "gpu", "backfill", "g03")
    base = dispatch_mean(groups, "gpu", "fcfs", "g03")
    if late is not None and base is not None:
        parts.append(
            f"backfill 的代價：要 2 張卡的工作比 FCFS 晚 {late - base:.0f} s 開始（未超過保留時刻）"
        )
    if not parts:
        return ""
    parts[-1] += "。工作數：GPU 12、混合 16、使用者 30（3 人 × 10）"
    return "註：" + "。\n　　".join(parts) + "。"


def main() -> None:
    root = EVIDENCE / "raw"
    figures = EVIDENCE / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    groups = collect(root)
    lab_cases = [
        c for c in ("gpu", "contention", "users", "urgent") if any(k[0] == c for k in groups)
    ]
    if lab_cases:
        fig_wait(
            groups,
            lab_cases,
            figures / "mean_wait.png",
            "平均等待時間（實機，誤差線為 3 次的標準差）",
        )
        loaded = [c for c in ("gpu", "contention") if c in lab_cases]
        if loaded:
            fig_utilization(groups, loaded, figures / "utilization.png")
        if "users" in lab_cases:
            fig_fairness(groups, figures / "fairness.png")
        if "urgent" in lab_cases:
            fig_urgent(groups, figures / "urgent.png")
        for case in ("gpu", "contention", "urgent"):
            for policy in POLICIES:
                items = sorted(groups.get((case, policy, "default"), []), key=lambda s: s["rep"])
                if items:
                    title = f"{CASE_TITLE[case]} · {policy} · 第 {items[0]['rep']} 次"
                    fig_gantt(items[0]["run"], figures / f"gantt_{case}_{policy}.png", title)
        fig_summary(groups, figures / "summary_16x9.png", summary_footnote(groups))
    sim = collect(EVIDENCE / "sim")
    sim_cases = [
        c
        for c in ("gpu", "contention", "users", "urgent", "cpu", "ram", "mixed")
        if any(k[0] == c for k in sim)
    ]
    if sim_cases:
        fig_wait(
            sim,
            sim_cases,
            figures / "sim_mean_wait.png",
            "模擬（真實排程程式碼、假 Worker 與假時鐘）：平均等待時間",
        )
    print("figures ->", figures)


if __name__ == "__main__":
    main()
