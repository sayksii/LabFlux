#!/usr/bin/env bash
# Demo 1（第 6–7 頁 Where a Job Runs → Who Goes First）：demo.py 的第 2 幕。畫面看 Dashboard（GPU 卡軸、排隊順序）。
#
#   demo/1_priority.sh              （在 repo 根目錄執行）
#   demo/1_priority.sh --dry-run    只印出步驟，不送任何東西
#
# 階段 1：alice 送 5 個工作，系統決定每個放哪台，占滿五張卡。
#   PAUSE → 按 Enter
# 階段 2：alice 送 P5，5 秒後 bob 送 P9，再 5 秒 bob 送 P5。看排序：bob P9、bob P5、alice P5。
#   PAUSE → 按 Enter
# 收尾：alice 取消一個工作，空出來的卡給 bob 的 P9。
#   PAUSE → 按 Enter 結束
# 任何 PAUSE 按 Ctrl-C 都會在那裡結束。結束後自動清場（demo.py reset）。
#
# 工作與每一步的說明在 examples/demo/2-priority/。

set -u
cd "$(dirname "$0")/.." || exit 1
D=".venv/bin/python scripts/demo.py --config .labflux/lab/scheduler.toml"
trap ':' INT  # Ctrl-C ends the scene (demo.py), not this script, so the cleanup below still runs.

echo "\$ demo.py scene 2 $*"
$D scene 2 "$@"
[ "${1:-}" = --dry-run ] && exit 0

echo
echo "\$ demo.py reset"
$D reset
