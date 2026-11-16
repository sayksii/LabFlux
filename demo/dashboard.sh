#!/usr/bin/env bash
# 怎麼打開 Dashboard，以及 alice、bob 的 token。
# 真實位址與 token 從不進 git 的 .labflux/lab/scheduler.toml 讀，這個檔案裡沒有。
# 會把 token 印在終端機上：展示前自己看，不要在投影畫面上執行。
#
#   demo/dashboard.sh          （在 repo 根目錄執行）

set -u
cd "$(dirname "$0")/.." || exit 1

PY=.venv/bin/python
CFG=.labflux/lab/scheduler.toml
for f in "$PY" "$CFG"; do
  [ -e "$f" ] || { echo "找不到 $f"; exit 1; }
done

# Scheduler 的位址、port，以及 alice、bob 的 token（token_urlsafe，不含空白）
read -r HOST PORT ALICE BOB < <("$PY" -c 'import sys, tomllib
cfg = tomllib.load(open(sys.argv[1], "rb"))
users = cfg.get("users", {})
print(cfg["bind_host"], cfg["bind_port"], users.get("alice", "-"), users.get("bob", "-"))' "$CFG")

echo "== Scheduler"
echo "\$ curl http://$HOST:$PORT/health"
if curl -fsS -m 3 "http://$HOST:$PORT/health"; then
  echo
else
  echo "連不到 Scheduler：先跑 demo/0_start.sh"
fi

cat <<EOF

== 打開 Dashboard（在要投影的筆電上）
1. 開一個終端機建立 SSH 轉送，展示期間這個視窗保持開著：
     ssh -L $PORT:$HOST:$PORT $USER@$HOST
   （用你平常從筆電連 kayle 的 key；SSH config 裡有 kayle 的 Host 名稱時，把 $USER@$HOST 換成那個名稱）
   用 VS Code 連著 kayle 的話也可以：下方「連接埠」分頁 → 轉送連接埠 → 輸入 $HOST:$PORT，
   再打開它給的 localhost 位址，後面加 /dashboard。
2. 瀏覽器打開 http://localhost:$PORT/dashboard
3. 貼上 alice 的 token 登入（自己的工作才能點開完整細節）。
4. 範圍「所有人」（預設）、按「投影模式」、軸用「GPU 卡」（預設）。投影機 1920×1080 最好。
   token 只記在這個分頁：重新整理不用再貼，開新分頁要再貼一次。
   打不開時：Scheduler 只聽 $HOST，不聽 kayle 的 localhost，所以轉送的目標一定要寫 $HOST:$PORT，
   只寫 $PORT 的轉送（包括 VS Code 自動轉送的）連不到，先停掉。ssh 印出「Address already in use」
   表示筆電的 $PORT 被舊的轉送占著：關掉舊視窗，或改用 ssh -L 1$PORT:$HOST:$PORT ...，打開 http://localhost:1$PORT/dashboard。

== Token（不要貼到投影片或聊天室）
alice  $ALICE
bob    $BOB
EOF

if [ "$ALICE" = - ] || [ "$BOB" = - ]; then
  echo
  echo "scheduler.toml 裡沒有 alice 或 bob：部署時用的不是展示設定（configs/local-lab-demo.toml）。"
  exit 1
fi
