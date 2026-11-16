#!/usr/bin/env bash
# 展示前把實驗室帶到起始狀態，並確認一切 OK。每個指令先印出來（$ 開頭）再執行，
# 看得到輸入和輸出，也可以照著自己手打。不需要 sudo。
#
#   demo/0_start.sh          （在 repo 根目錄執行）
#
# 1. 三台的 labflux 服務：沒在跑就啟動
# 2. 清掉 alice、bob 的工作（demo.py reset）
# 3. 清掉 alice、bob 近 60 分鐘的用量：把他們最近一小時內結束的工作，時間往前移 2 小時
#    （Scheduler 的資料庫在 kayle；有東西要移時先備份成 scheduler.sqlite3.before-demo-reset.bak）
# 4. 五張卡沒有別人在用（nvidia-smi）
# 5. demo.py check：策略、節點、gemma、token、用量
# 最後一行是 READY 或 NOT READY。
#
# 真實位址與 token 從不進 git 的 configs/local-lab-demo.toml 和 .labflux/lab/ 讀。

set -u
cd "$(dirname "$0")/.." || exit 1

PY=.venv/bin/python
CFG=.labflux/lab/scheduler.toml
INV=configs/local-lab-demo.toml
D="$PY scripts/demo.py --config $CFG"
SSH="ssh -i $HOME/.ssh/labflux_deploy -o IdentitiesOnly=yes -o UserKnownHostsFile=$PWD/.labflux/lab/known_hosts -o BatchMode=yes -o ConnectTimeout=5"
problems=()

for f in "$PY" "$CFG" "$INV"; do
  [ -e "$f" ] || { echo "找不到 $f"; exit 1; }
done

# host karma -> labflux@<karma 的位址>（從 inventory 讀）
host() {
  "$PY" -c 'import sys, tomllib
inv = tomllib.load(open(sys.argv[1], "rb"))
node = next(n for n in inv["nodes"] if n["name"] == sys.argv[2])
print(inv.get("account", "labflux") + "@" + node["host"])' "$INV" "$1"
}

# gpus kindred -> 3,4（LabFlux 在那台用的卡）
gpus() {
  "$PY" -c 'import sys, tomllib
inv = tomllib.load(open(sys.argv[1], "rb"))
node = next(n for n in inv["nodes"] if n["name"] == sys.argv[2])
print(",".join(str(g) for g in node.get("gpus", [])))' "$INV" "$1"
}

# on <node> <command>：印出指令（輸入），再到那台執行（輸出）
on() {
  echo "\$ ssh $*" >&2
  $SSH "$(host "$1")" "${*:2}"
}

echo "== 1. 三台的服務"
for unit in kayle:labflux-scheduler kayle:labflux-worker karma:labflux-worker kindred:labflux-worker; do
  node=${unit%%:*}
  service=${unit#*:}.service
  state=$(on "$node" systemctl --user is-active "$service")
  echo "$state"
  if [ "$state" != active ]; then
    on "$node" systemctl --user start "$service"
    sleep 3
    state=$(on "$node" systemctl --user is-active "$service")
    echo "$state"
    [ "$state" = active ] || problems+=("$node 的 $service 啟動不了：看 docs/lab-deploy.md「日誌」")
  fi
done
sleep 2  # 剛啟動的 Worker 要幾秒才會 ONLINE

echo
echo "== 2. 清掉 alice、bob 的工作"
echo "\$ demo.py reset"
$D reset || problems+=("demo.py reset 沒有確認所有工作都停了")

echo
echo "== 3. 清掉 alice、bob 近 60 分鐘的用量"
echo "   用量只算最近 60 分鐘結束的執行紀錄，所以把他們最近一小時內結束的工作往前移 2 小時；其他人的紀錄不動。"
cat <<'SQL'
$ ssh kayle sqlite3 ~/labflux/scheduler-data/scheduler.sqlite3
  UPDATE attempts SET created_at = created_at - 7200, started_at = started_at - 7200,
                      finished_at = finished_at - 7200  WHERE job_id IN (<alice、bob 最近一小時結束的工作>);
  UPDATE jobs     SET created_at = created_at - 7200, updated_at = updated_at - 7200
                                                       WHERE id     IN (<同一批工作>);
SQL
$SSH "$(host kayle)" '~/labflux/.venv/bin/python -' <<'PY' || problems+=("清用量失敗：看上面的訊息")
import os, sqlite3, time
data = os.path.expanduser("~/labflux/scheduler-data/")
db = sqlite3.connect(data + "scheduler.sqlite3", timeout=30)
users = ("alice", "bob")
since = time.time() - 3600  # The Scheduler runs on this machine: same clock.
held = db.execute(
    "select count(*) from attempts a join jobs j on j.id = a.job_id "
    "where j.owner in (?, ?) and a.released = 0", users).fetchone()[0]
if held:
    print(f"alice、bob 還有 {held} 個工作沒釋放資源：等 demo.py reset 完成再跑一次")
    raise SystemExit(1)
ids = [row[0] for row in db.execute(
    "select distinct a.job_id from attempts a join jobs j on j.id = a.job_id "
    "where j.owner in (?, ?) and a.finished_at > ?", (*users, since))]
if not ids:
    print("alice、bob 最近一小時沒有用量，不用清。")
    raise SystemExit(0)
backup = sqlite3.connect(data + "scheduler.sqlite3.before-demo-reset.bak")
db.backup(backup)
backup.close()
with db:
    db.executemany(
        "update attempts set created_at = created_at - 7200, started_at = started_at - 7200, "
        "finished_at = finished_at - 7200 where job_id = ?", [(i,) for i in ids])
    db.executemany(
        "update jobs set created_at = created_at - 7200, updated_at = updated_at - 7200 where id = ?",
        [(i,) for i in ids])
print(f"已把 {len(ids)} 個工作移出用量視窗（備份：scheduler.sqlite3.before-demo-reset.bak）")
PY
sleep 2  # 下一輪排程才會重算用量

echo
echo "== 4. 五張卡有沒有別人在用"
for node in kayle karma kindred; do
  cards=$(on "$node" nvidia-smi -i "$(gpus "$node")" --query-gpu=index,name,memory.used,utilization.gpu --format=csv,noheader)
  echo "$cards"
  # 第 3 欄是「12 MiB」：用了 1 GiB 以上就當作有人在用
  busy=$(echo "$cards" | awk -F', ' '{ split($3, m, " "); if (m[1] + 0 >= 1024) n++ } END { print n + 0 }')
  [ "$busy" = 0 ] || problems+=("$node 有 $busy 張卡被占用（用了 1 GiB 以上）")
done

echo
echo "== 5. demo.py check"
echo "\$ demo.py check"
out=$($D check)
echo "$out"
echo "$out" | grep -q "check passed" || problems+=("demo.py check 沒有通過：看上面 FAIL 的那行")
echo "$out" | grep -q "gemma4-31b: on_demand, stopped" ||
  problems+=("gemma 沒有停著：確認沒人在用後，$D scene 6b（PAUSE 按 Enter），再 $D reset")
echo "$out" | grep -qE "^OK   alice's recent usage 0\.0 min" ||
  problems+=("alice 的用量不是 0：第 2 幕的分數會和講者備註不同")
echo "$out" | grep -qE "^OK   bob's recent usage 0\.0 min" ||
  problems+=("bob 的用量不是 0：第 2 幕的排序要靠 bob 的用量是 0")

echo
if [ ${#problems[@]} -eq 0 ]; then
  echo "READY：服務都在跑、沒有殘留工作、alice 和 bob 的用量是 0、卡都空著、gemma 停著、check 通過。"
  exit 0
fi
echo "NOT READY："
for p in "${problems[@]}"; do echo "  - $p"; done
exit 1
