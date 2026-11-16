#!/usr/bin/env bash
# Demo5（Fast Lane）：進入 labflux shell，在裡面打平常的 python，就在叢集的 4090 上跑。
#
#   demo/5_fastlane.sh          （在 repo 根目錄執行）
#
# 進去後打：
#   time python hello.py        第一次：租一個 session（assigned to karma），約 1 秒
#   time python hello.py        第二次：同一個容器，更快
#   exit                        離開；接著自動收掉 session，把 4090 還回去
#
# 專案設定在 demo/5_fastlane/.labflux.toml：一張 4090。用你自己的帳號（labflux login）。
# 用 repo .venv 裡的 labflux：它的程式已經編譯好，每個指令比 /opt 的共用 CLI 少約 0.4 秒。

set -u
cd "$(dirname "$0")/.." || exit 1
L="$PWD/.venv/bin/labflux"
cd demo/5_fastlane || exit 1
export TIMEFORMAT='time: %R s'

echo "\$ labflux shell"
"$L" shell
echo
echo "\$ labflux session close"
"$L" session close >/dev/null 2>&1
for _ in $(seq 30); do  # The container stops within a few seconds.
  "$L" session status 2>&1 | grep -q "no fast-lane session" && { echo "session 已收掉，4090 還回去了。"; exit 0; }
  sleep 1
done
echo "session 還沒收掉：再執行一次 .venv/bin/labflux session close"
exit 1
