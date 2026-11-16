#!/usr/bin/env bash
# 第 9 頁：較新的 checkpoint 讓先開始的 Job1 也能成為損失最小的搶占對象。
# bash demo/4_preemption.sh [--dry-run]；第一次 Enter 搶占，第二次 Enter 清場。
set -u
cd "$(dirname "$0")/.." || exit 1

PY=.venv/bin/python
CFG=.labflux/lab/scheduler.toml
[ -x "$PY" ] || { echo "找不到 $PY，請先執行 uv sync。"; exit 1; }
case "${1:-}" in
  --dry-run) "$PY" scripts/demo.py scene 4 --dry-run; exit "$?" ;;
  "") ;;
  *) echo "用法：bash demo/4_preemption.sh [--dry-run]"; exit 2 ;;
esac
[ -f "$CFG" ] || { echo "找不到 $CFG，請使用實驗室部署的設定檔。"; exit 1; }
D=("$PY" scripts/demo.py --config "$CFG")
trap ':' INT

cat <<'EOF'
第 9 頁：Urgent Work: Preemption
Dashboard：以 alice 登入、範圍「所有人」、軸「GPU 卡」、分頁「排隊順序」，看 kindred。
終端機會印出 Job1／Job2 對應的短 ID 與未保存進度秒數，可對照 Dashboard 的工作 ID。
  第一幕：Job1 先跑，每 2 秒存 checkpoint；5 秒後 Job2 進入，不存 checkpoint。
  按 Enter：優先權 8 的 Urgent 進入，搶占距 checkpoint 較近、損失較少的 Job1。
  第二幕：Job2 繼續跑，Urgent 接手 Job1 的 A40，Job1 回到 queue。
最後按 Enter 清場；也可按 Ctrl-C 結束並清場。
EOF

echo '$ demo.py reset（清掉上一幕的 alice、bob 工作）'
"${D[@]}" reset || exit "$?"
echo '$ demo.py scene 4'
"${D[@]}" scene 4
scene_status=$?
echo '$ demo.py reset（收回本幕資源）'
"${D[@]}" reset || exit "$?"
exit "$scene_status"
