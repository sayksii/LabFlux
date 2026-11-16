#!/usr/bin/env bash
# 第 8 頁 Backfill: Fill the Gaps。畫面看 Dashboard（GPU 卡軸、排隊順序）。
#
#   bash demo/3_backfill.sh              （在 repo 根目錄執行）
#   bash demo/3_backfill.sh --dry-run    只列出步驟，不連 Scheduler
#
# 開始、結束都會取消 alice、bob 的展示工作，等停止確認後才繼續。
# 場景 1：兩張 A40 各有一個不同的原始工作，上方時限 10 分鐘，下方時限 3 分鐘。
# 場景 2（按 Enter）：大 Job 要兩張 A40，小 Job 要一張 A40，兩個都進入 queue。
# 場景 3（自動）：從送出後開始倒數約 10 秒，原始短工作自然完成，小 Job 自動 backfill。
# 最後按 Enter 清場；任何階段也可用 Ctrl-C 結束並清場。
# 模型需停著、兩張 A40 空著；場景定義在 examples/demo/3-backfill/。

set -u
cd "$(dirname "$0")/.." || exit 1

PY=.venv/bin/python
CFG=.labflux/lab/scheduler.toml
[ -x "$PY" ] || { echo "找不到 $PY，請先執行 uv sync。"; exit 1; }

case "${1:-}" in
  --dry-run)
    "$PY" scripts/demo.py scene 3 --dry-run
    exit "$?"
    ;;
  "") ;;
  *) echo "用法：bash demo/3_backfill.sh [--dry-run]"; exit 2 ;;
esac

[ -f "$CFG" ] || { echo "找不到 $CFG，請使用實驗室部署的設定檔。"; exit 1; }
D=("$PY" scripts/demo.py --config "$CFG")
trap ':' INT  # Ctrl-C 結束場景後，讓下方的清場繼續執行。

cat <<'EOF'
第 8 頁：Backfill: Fill the Gaps
Dashboard：範圍「所有人」、軸「GPU 卡」、分頁「排隊順序」。
看 kindred 這一欄：
  1. 兩張 A40 各跑一個不同的原始工作：上方時限 10 分鐘，下方時限 3 分鐘。
  2. 按 Enter：bob 的大 Job 要兩張 A40，alice 的小 Job 要一張 A40，兩個都進 queue。
  3. 自動倒數約 10 秒，原始短工作自然完成，小 Job 自動先跑，區塊標記 backfill。
指著「一個原始工作 + 小 Job」與大 Job 的保留時段講解。
最後按 Enter 清場，也可按 Ctrl-C 結束並清場。
EOF

echo '$ demo.py reset（清掉上一幕的 alice、bob 工作）'
"${D[@]}" reset || exit "$?"

echo '$ demo.py scene 3'
"${D[@]}" scene 3
scene_status=$?

echo '$ demo.py reset（收回本幕資源）'
"${D[@]}" reset || exit "$?"
exit "$scene_status"
