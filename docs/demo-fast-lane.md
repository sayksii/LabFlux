# 快速測試 demo 與驗收步驟

這份文件讓人親自看一次快速測試（M9）的檔案流程：程式怎麼送到 session、結果怎麼寫回本機、
太大的檔案怎麼留在遠端，以及 Ctrl-C、超時、閒置到期的行為。

- 全部在同一台機器的 loopback 上執行，只用 CPU，不會碰到實驗室部署（port 8770）。
- 需要：已經 `uv sync` 的 LabFlux repo、可以使用 Docker 的帳號、本機已有 `python:3.12-slim` 映像
  （沒有的話先 `docker pull python:3.12-slim`）。
- demo 用的設定在 `configs/fast-lane-demo.toml`，為了不用久等，縮短了三個數值：
  寫回上限 5 MiB（正式 100 MB）、單一指令上限 20 秒（正式 60 秒）、閒置到期 120 秒（正式 10 分鐘）。
- demo 專案在 `examples/fast-lane-demo/`，執行時複製到 `/tmp/lf-demo`，不會在 repo 裡產生檔案。

2026-11-16 已在 kayle 依本文件完整跑過一次；下面「預期看到」的內容就是那次的實際輸出
（容器 ID 每次都不同）。

## 1. 開三個終端機

三個終端機都先進到 LabFlux repo 的根目錄。

**終端機 A：Scheduler**

```bash
export LABFLUX_CONFIG=$PWD/configs/fast-lane-demo.toml
LABFLUX_DATA_DIR=/tmp/lf-demo-data/scheduler .venv/bin/labflux scheduler
```

看到 `Uvicorn running on http://127.0.0.1:8790` 就好了。

**終端機 B：Worker**

```bash
export LABFLUX_CONFIG=$PWD/configs/fast-lane-demo.toml
LABFLUX_DATA_DIR=/tmp/lf-demo-data/worker .venv/bin/labflux worker
```

看到 `worker registered` 就好了。Scheduler 那邊會出現一行 `WebSocket /internal/workers/fast-lane-demo/ws ... [accepted]`，
那就是 Worker 的門鈴長連線。

**終端機 C：你的工作環境**

```bash
cp -r examples/fast-lane-demo /tmp/lf-demo
export PATH="$PWD/.venv/bin:$PATH"
export LABFLUX_CONFIG=$PWD/configs/fast-lane-demo.toml
cd /tmp/lf-demo
labflux shell
```

會印出 `labflux: stand-ins for accelerate, deepspeed, nvidia-smi, pytest, python, python3, torchrun, uv are first on PATH`。
之後的步驟都在這個 shell 裡輸入。

## 2. 驗收步驟

每一步的「對應決定」指 `docs/architecture.md` 的編號。

| # | 輸入 | 預期看到 | 說明 | 對應決定 |
|---|---|---|---|---|
| 1 | `command -v python; hostname` | `/tmp/labflux-standins-…/python`，然後 `kayle` | `python` 被替身接手；`hostname` 不在清單上，留在本機 | T2、T3、T7 |
| 2 | `python hello.py` | `labflux: waiting for a session: assigned to fast-lane-demo`，然後 `hello from b829a345b2d2, cwd /workspace` | 第一次會租一個 session；程式在容器裡執行，主機名稱是容器 ID | T6、D4b |
| 3 | `cat result.txt` | `written remotely on b829a345b2d2` | 遠端寫的檔案自動送回本機 | D12 |
| 4 | `python hello.py` | 同一個容器 ID，沒有 waiting | 第二個指令沿用同一個 session | M9 session |
| 5 | `echo hi > scratch.txt; python -c "print(open('scratch.txt').read())"` | `hi` | 本機新建的檔案跟著快照送過去 | D4b |
| 6 | `rm scratch.txt; python -c "import os; print(os.path.exists('scratch.txt'))"` | `False` | 本機刪掉的檔案，遠端也刪掉 | D4b |
| 7 | `python make_ckpt.py` | `training done: …`，然後一行 `labflux: 1 file (20.0 MB) exists only in the remote session: ckpt/model.pt; list: .labflux/remote-files.txt; write files you want to keep to /outputs` | 20 MiB 超過寫回上限，只印一行提示 | D12（2026-11-16） |
| 8 | `ls ckpt; cat metrics.json; cat .labflux/remote-files.txt` | `ls: cannot access 'ckpt'`；`{"loss": 0.123, "epochs": 1}`；清單列出 `ckpt/model.pt	20971520 bytes (20.0 MB)` | 小檔案寫回了，大檔案只在遠端，本機有清單 | D12 |
| 9 | `python -c "import os; print(os.path.getsize('ckpt/model.pt'))"` | `20971520` | 送到遠端的指令用同一個路徑讀得到大檔案 | D12 |
| 10 | `labflux session files` | 和步驟 8 的清單相同 | 也可以用指令查清單 | D12 |
| 11 | `python conflict.py`，在它等待的 10 秒內，到另一個終端機執行 `echo "edited locally" > /tmp/lf-demo/shared.txt`；結束後 `ls shared.txt*; cat shared.txt; cat shared.txt.remote` | `labflux: shared.txt changed locally during the command; the remote copy is saved as shared.txt.remote`；`shared.txt` 是 `edited locally`，`shared.txt.remote` 是 `written remotely` | 兩邊都改到同一個檔案時不覆蓋本機 | D12 |
| 12 | `python slow.py`，印到 5 左右按 Ctrl-C；再 `echo $?; cat progress.txt` | `labflux: the command was stopped; the session is still running`，`130`，`reached 5` 或 `6` | Ctrl-C 只停指令；停之前寫的檔案照樣寫回 | 2026-11-16 決定 |
| 13 | `python hello.py` | 和步驟 2 同一個容器 ID | Ctrl-C 之後 session 還在 | 2026-11-16 決定 |
| 14 | `python -c "import sys; sys.exit(3)"; echo $?` | `3` | exit code 原樣回傳 | M9 |
| 15 | `python -c "import time; time.sleep(30)"; echo $?` | 約 20 秒後 `labflux: command time limit exceeded`、`… ckpt/model.pt ended with the session …`、`labflux: the session was closed; the next command starts a new one`，`124` | 超過單一指令上限會終止整個容器；只在遠端的檔案跟著消失 | plan M9 |
| 16 | `python hello.py` | 又出現 waiting，容器 ID 和之前不同 | 下一個指令自動租新的 session | M9 |
| 17 | `labflux session status` | `"state": "RUNNING"`、`"lease_remaining_s": 119` 左右 | 每個指令都把 lease 續到 120 秒 | 2026-11-16 決定 |
| 18 | 什麼都不做，等 2 分鐘以上，再 `labflux session status` | `You have no fast-lane session.` | 閒置到期，session 收掉、資源釋放 | 2026-11-16 決定 |

選做：在步驟 1 之後，從這個 shell 啟動 AI 工具（例如 `claude`），請它「執行 `python make_ckpt.py`，確認 checkpoint 存檔並回報大小」。
預期它會讀 `.labflux/remote-files.txt`，再用送到遠端的 `python` 確認大小（見 `docs/verification.md` 的 9.14）。

## 3. 驗收時也可以看的地方

- 終端機 A、B 的日誌：每個指令會看到 CLI 的 WebSocket 連線，以及 Worker 收到的命令。
- `docker ps --filter label=labflux.worker_id=fast-lane-demo`：session 執行時有一個容器；步驟 15 之後換成新的；步驟 18 之後沒有容器。
- 資源帳：`curl -s -H "Authorization: Bearer demo-user-token" http://127.0.0.1:8790/api/nodes`，
  session 執行時 `allocated` 是 1 CPU、512 MiB，步驟 18 之後回到 0。

## 4. 收尾

1. 在終端機 C 輸入 `exit` 離開 `labflux shell`（替身的暫存目錄會自動刪除）。
2. 如果 session 還在：`labflux session close`。
3. 在終端機 B、A 依序按 Ctrl-C 停掉 Worker 和 Scheduler。
4. 確認沒有留下容器：`docker ps -a --filter label=labflux.worker_id=fast-lane-demo` 應該是空的。
5. 刪掉 demo 的暫存檔：`rm -rf /tmp/lf-demo /tmp/lf-demo-data`。

## 5. 這個 demo 沒有涵蓋的

- GPU：demo 只用 CPU。GPU session 要在部署到實驗室機器後另外驗收。
- 跨機器：Scheduler 與 Worker 在同一台。kayle 與 karma 之間的實際網路要部署後再測。
- `uv sync`：demo 專案沒有 `pyproject.toml`，所以不會觸發；已由自動化測試涵蓋。
