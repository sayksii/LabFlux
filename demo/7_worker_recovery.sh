#!/usr/bin/env bash
# 第 12 頁：失聯的 Worker 可能還在執行 Job，先保留資源，恢復後繼續原執行。
# 在 kayle 執行：./demo/7_worker_recovery.sh [--dry-run]
set -eu
cd "$(dirname "$0")/.."
PY=.venv/bin/python
[ -x "$PY" ] || { echo "找不到 $PY，請先執行 uv sync。"; exit 1; }
if [ "${1:-}" != --dry-run ]; then
  echo '第 12 頁：When a Machine Fails'
  echo 'Dashboard：以 alice 登入、範圍「所有人」、軸「GPU 卡」、分頁「排隊順序」，看 kayle。'
  echo '第一次 Enter 模擬失聯；OFFLINE / UNKNOWN 顯示 10 秒後自動恢復；第二次 Enter 清場。'
fi
exec "$PY" scripts/demo_worker_recovery.py "$@"
