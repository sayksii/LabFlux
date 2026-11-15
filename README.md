# LabFlux

OS 課程專案：以中央 Scheduler 與多個 Docker Worker 管理 CPU／RAM／GPU 批次工作。
實作進度與驗收條件見 [plan.md](plan.md)；驗證紀錄見 [docs/verification.md](docs/verification.md)。

## 開發環境

需要 Python 3.12 與 [uv](https://docs.astral.sh/uv/getting-started/installation/)。

```bash
uv sync --locked
uv run labflux version
uv run pytest -m 'not docker and not vm'
uv run ruff check .
```

Docker 與 VM 驗證需要另外安裝 Docker Engine、KVM／libvirt 與 cloud-init 工具。
各服務使用 TOML 設定；CLI 可透過 `--config path.toml` 或 `LABFLUX_CONFIG` 指定。
環境變數 `LABFLUX_SCHEDULER_URL`、`LABFLUX_USER_TOKEN`、`LABFLUX_WORKER_TOKEN`、
`LABFLUX_WORKER_ID`、`LABFLUX_DATA_DIR` 可覆蓋對應設定。

如果 uv 不在 PATH，先執行 `export PATH="$HOME/.local/bin:$PATH"`。

## 本機啟動

在兩個終端機分別啟動 Scheduler 與 Worker：

```bash
uv run labflux --config configs/demo.toml scheduler
```

```bash
uv run labflux --config configs/demo.toml worker
```

Worker 需要 Docker socket 權限；剛加入 docker 群組但尚未重新登入時，可使用
`sg docker -c 'uv run labflux --config configs/demo.toml worker'`。
範例設定的 Token 僅供本機開發；三台 VM 的部署腳本會產生獨立隨機 Token。

```bash
uv run labflux --config configs/demo.toml submit examples/jobs/hello.json --source examples/workloads/hello
uv run labflux --config configs/demo.toml status
uv run labflux --config configs/demo.toml status JOB_ID
uv run labflux --config configs/demo.toml logs JOB_ID --follow
uv run labflux --config configs/demo.toml fetch JOB_ID --directory outputs/JOB_ID
uv run labflux --config configs/demo.toml nodes
uv run labflux --config configs/demo.toml cancel JOB_ID
```

`submit --wait` 等到工作完成；失敗、取消或等待逾時（預設 120 秒，可用 `--wait-timeout` 調整）會以非零狀態退出。
狀態不明（`UNKNOWN`）不算結束：Worker 恢復連線或重啟接手後工作會繼續，所以會持續等待。
`cancel` 對排隊工作立即生效；執行中的工作會先變成 `STOPPING`，確認容器停止才變成 `CANCELLED`。

Scheduler 在 `/dashboard` 提供監控頁（例如 `http://127.0.0.1:8000/dashboard`），以使用者 Token 登入。
節點以課表呈現已預約的 CPU／RAM，清單顯示自己的工作與等待原因；點選工作可看執行紀錄、即時日誌及取消。
設計脈絡與設計系統見 `PRODUCT.md`、`DESIGN.md`。

## 工作與資料

工作 JSON 至少包含指令參數陣列 `argv`；`image` 可省略，見下方的 uv 模式。`resources.cpu` 表示 CPU 時間配額，
`resources.memory_mib` 使用 MiB，`time_limit_s` 表示容器啟動後的執行時限。
工作在 `/workspace` 執行，把要取回的結果寫入 `/outputs`。

`time_limit_s` 省略時使用 Scheduler 的 `job_time_limit_s`（預設 3600 秒）。其他選填欄位：

| 欄位 | 預設 | 說明 |
|---|---|---|
| `priority` | Scheduler 的 `default_priority`（5） | 0–9，數字越大越優先；`fcfs` 模式不使用 |
| `estimated_runtime_s` | 無 | 只用於說明，不得超過 `time_limit_s`；保留時段與 backfill 一律以硬性時限計算 |
| `safe_to_rerun` | `false` | 程式從頭再跑一次不會造成傷害（例如不會重複寫入外部資料庫）；`preemptible` 與 `retries_on_node_failure` 都需要它 |
| `preemptible` | `false` | 允許 Scheduler 為了更重要的工作停止它並重新排隊；需要 `"safe_to_rerun": true`，見下方「搶占」 |
| `retries_on_node_failure` | 0 | 執行遺失（節點失聯且確認容器已不存在）時最多重跑幾次，上限 10；大於 0 時需要 `"safe_to_rerun": true`。舊名稱 `max_retries` 仍可使用；見下方「故障與重跑」 |

`--source` 上傳可重現的程式快照，預設排除 Git、虛擬環境、資料集、輸出與 `.env`，
並讀取來源根目錄的 `.gitignore` 與 `.labfluxignore`。來源與輸出封存檔都拒絕路徑跳脫、
連結與特殊檔案，且有大小／檔案數限制；下載結果不覆寫既有檔案。

大型資料集先放在 Worker 上，於 Worker TOML 設定：

```toml
[datasets]
images = "/srv/datasets/images"
```

工作設定 `"datasets": ["images"]`，Scheduler 只選擇具有該資料集的節點，
容器中以唯讀方式掛載於 `/datasets/images`。

## uv 模式

工作 JSON 省略 `image` 時，Scheduler 在提交當下填入 `default_image`，預設是
`localhost/labflux/python-uv:3.12-uv0.11.8`（Python 3.12 + uv 0.11.8）。程式目錄只要有
`pyproject.toml` 與 `uv.lock`，以 `uv run --locked ...` 執行；需要其他 Python 版本或系統套件時，
請在工作 JSON 自己指定 `image`。

每台 Worker 先建置基礎映像。名稱的 `localhost/` 前綴讓缺少映像時直接失敗，
不會從 Docker Hub 拉到同名的映像：

```bash
docker build -t localhost/labflux/python-uv:3.12-uv0.11.8 images/python-uv
```

Worker TOML 設定 `uv_cache_dir` 後，這個本機目錄以讀寫方式掛載於容器的 `/uv-cache`，
並設定 `UV_CACHE_DIR=/uv-cache`、`UV_LINK_MODE=copy`（快取與工作目錄是不同的 bind mount，無法 hardlink）。
虛擬環境建立在 Worker 本機該 attempt 的 `/workspace/.venv`；確認容器停止且輸出已回傳後，
Worker 刪除這個 workspace，保留狀態、日誌與輸出封存檔。同一台 Worker 上的工作共用這份可寫的快取。

```toml
uv_cache_dir = "~/labflux/uv-cache"
```

`examples/uv-gpu/` 透過 `[tool.uv.sources]` 從 PyTorch cu124 index 安裝 torch，印出分配到的 GPU 型號。
它需要 Worker 以 `gpus` 設定納入 GPU：

```bash
uv run labflux --config CONFIG submit examples/jobs/uv-gpu.json --source examples/uv-gpu --wait --wait-timeout 1800
```

`examples/mnist/` 是改寫自 PyTorch 官方範例（BSD-3-Clause，見該目錄的 `LICENSE`）的 MNIST 手寫數字訓練，
預設 3 個 epoch，把模型與 `metrics.json`（每個 epoch 的 loss、準確率與總時間）寫入 `/outputs`。
它與 `examples/uv-gpu` 使用相同的 torch 版本與 index，兩者共用 Worker 的 uv 快取。
預設的工作自己下載約 11 MB 的資料到 `/workspace/data`：

```bash
uv run labflux --config CONFIG submit examples/jobs/mnist.json --source examples/mnist --wait --wait-timeout 1800
uv run labflux --config CONFIG logs JOB_ID
uv run labflux --config CONFIG fetch JOB_ID --directory outputs/mnist
```

節點也可以預先放好 MNIST（torchvision 的目錄結構，例如 `/srv/datasets/mnist/MNIST/raw/`），
在 Worker TOML 設定 `[datasets]` 的 `mnist = "/srv/datasets/mnist"`，並在工作 JSON 加上
`"datasets": ["mnist"]`；程式偵測到唯讀的 `/datasets/mnist` 就直接使用，不再下載。

## 排程與通訊

排程策略由 Scheduler TOML 的 `scheduling_policy` 選擇，預設 `fcfs`：

| 模式 | 行為 |
|---|---|
| `fcfs` | 嚴格 FCFS：隊首不能執行時，後續工作等待，即使其他節點仍有空間 |
| `priority` | 依分數排序：`優先權 × 100 + 排隊分鐘 − 近期資源占用分鐘`，同分依提交順序 |
| `backfill` | `priority` 再加上 EASY backfilling：替無法執行的第一個工作依硬性時限計算保留時段，只讓不會延後該時段的工作先跑 |

近期占用是使用者在 `usage_window_s`（預設 3600 秒）內預約的分鐘數，乘上 CPU／RAM／GPU 中占叢集比例最高的一項。
權重為 `score_priority_weight`、`score_wait_weight`、`score_usage_weight`（預設 100、1、1）。
每個排隊工作的決策說明（例如「waiting behind job」、「reserved on 節點 at 時間」、「backfilled」）
出現在工作的 `schedule_note`。互動 session 排在批次工作之前。
同一節點可同時執行多個工作，但所有預約不得超過其 CPU／RAM 可分配容量。
預約與派送在同一個 SQLite 交易完成；即時負載另外記錄，不與預約重複扣除。

Worker 主動註冊，並維持一條連到 Scheduler 的 WebSocket（`/internal/workers/{node_id}/ws`）當門鈴：
Scheduler 寫入命令後通知 Worker，Worker 再以原本的 API 領取並 ack。命令仍以資料庫與 ack 為準，
門鈴通知遺失時由備援輪詢補上。`poll_interval_s`（預設 5 秒）是 Worker 的備援間隔，
同時用於命令輪詢與 outbox 上傳，也是門鈴重連與 outbox 重試退避的上限；Worker 的 outbox
有新事件或日誌時立即喚醒上傳。舊設定若把 `poll_interval_s` 設成 0.25，會恢復頻繁輪詢，應刪除該設定。
心跳由獨立執行緒每 2 秒回報，避免下載映像或執行長工作阻塞心跳。
命令接收紀錄與結果 outbox 保存在 Worker 的資料目錄，重送 START 不會重複執行同一 attempt。
超過 10 秒未收到心跳，節點變成 `OFFLINE`、未確認停止的工作變成 `UNKNOWN`，配置仍保留。
同一個 Worker instance 恢復心跳時，節點直接回到 `ONLINE`；它回報仍在執行的 attempt 從 `UNKNOWN`
回到 `RUNNING`，其餘維持 `UNKNOWN`，直到收到結果才釋放。

Worker 收到 SIGTERM／SIGINT 時只停止自己的執行緒，工作容器繼續執行；Worker 停止期間不檢查執行時限，
要停止工作請用 `labflux cancel`。重新啟動的 Worker
註冊時附上本機的 attempt 紀錄與帶有自己標籤的容器，Scheduler 在同一個交易內核對每個未釋放的 attempt：

- 容器還在（執行中或已結束），或本機有尚未送出的結果：新 instance 接手，繼續監看與執行時限；
  日誌從中斷處接上，不重複也不遺漏。
- 本機紀錄顯示還沒建立容器：重新送出同一個 START，從頭執行。
- 兩者都沒有：已沒有程序在執行，標記為 `FAILED`（已要求取消則為 `CANCELLED`），並釋放 CPU／RAM／GPU。

核對完成後節點才回到 `ONLINE`，舊 instance 的事件與日誌一律被拒絕。已釋放或 Scheduler
不認得的容器，只有在 Worker 本機也有紀錄時才會停止並移除，以免誤刪同名 Worker ID 的其他安裝。
沒有附上清單的舊版 Worker 維持原本的行為：有未釋放 attempt 時拒絕註冊。

### 故障與重跑

- 核對時發現容器已不存在（例如整台機器停機），而且工作設定了 `retries_on_node_failure`（需同時設定 `safe_to_rerun`），
  Scheduler 關閉舊 attempt（`FAILED`），建立新 attempt 並以原本的提交順序重新排隊。程式自己退出、超過時限、OOM
  與已要求取消的工作不重跑。被搶占的 attempt 不計入這個次數（見下方「搶占」）。
- Scheduler 以相同 `data_dir` 重啟後從 SQLite 還原：節點先標為 `RECONCILING`，工作與配置保留，
  該節點收到原 Worker instance 的心跳或新 instance 的註冊核對後才回到 `ONLINE` 並恢復派送；
  超過 `offline_timeout_s` 仍未出現則照常變成 `OFFLINE`，工作 `UNKNOWN`。
- 網路中斷期間 Worker 的結果留在本機 outbox，恢復連線後送出，不會重複執行。

使用者 API 為 `/api/jobs`、`/api/nodes`、`/api/artifacts`、`/api/sessions`，以使用者 Bearer Token 驗證；
內部 `/internal/workers` 使用獨立 Worker Token。使用者只能查看、取消與下載自己的工作。
`/health` 不需認證，`/docs` 提供 API 規格。

SQLite schema 在啟動時依序套用編號 migration（目前為版本 6）；也可執行 `uv run labflux db-upgrade`。
Scheduler 以單一程序執行，所有執行紀錄保存在設定的 `data_dir`。
新增的工作欄位與 WebSocket 介面需要 Scheduler、Worker 與 CLI 同時升級。

## 搶占

叢集滿載時，Scheduler 可以停掉擁有者允許中止的批次工作，把資源讓給更重要的工作。工作預設不會被搶占；
要允許時，在工作 JSON 加上兩個欄位：

```json
{
  "argv": ["uv", "run", "--locked", "python", "main.py", "--epochs", "3"],
  "resources": {"cpu": 4, "memory_mib": 8192, "gpu": 1},
  "priority": 3,
  "safe_to_rerun": true,
  "preemptible": true
}
```

`safe_to_rerun` 表示程式從頭再跑一次不會造成傷害；`preemptible` 必須搭配它，否則提交時回傳 422，訊息指出缺少的欄位。

被搶占時：

- 容器照取消的流程停止，工作先變成 `STOPPING`，原因寫 `preempted by job ...`；確認容器終止後才釋放資源，搶占它的工作才開始執行。
- 那次 attempt 變成 `PREEMPTED`，`/outputs` 照常上傳；`labflux status JOB_ID` 列出所有 attempt，
  `labflux fetch JOB_ID --attempt-id ATTEMPT_ID` 取回被搶占那次的輸出。
- 工作以原本的提交順序回到 `QUEUED`，之後以新的 attempt 從頭執行（還不支援從 checkpoint 接續）。
- 每個工作最多被搶占 `max_preemptions_per_job` 次（預設 3），之後不再被選中，照常排隊與執行，不會因此失敗。
  被搶占不消耗 `retries_on_node_failure`。
- 停止期間工作剛好自己結束時，保留原本的結果；使用者同時取消時變成 `CANCELLED`。

誰可以搶占：

- 互動 session：在所有排程策略下（包含 `fcfs`），可以搶占任何優先權的可搶占批次工作。
- 批次工作：只在 `priority` 與 `backfill` 策略下，而且只搶占顯式優先權（`priority`）比自己低、重新排隊後也會排在自己後面的工作。
  排了很久的低優先權工作等待加分很高，重新排隊後可能又排到第一；這種工作不搶，以免兩個工作一直互搶。
- 只有排在最前面、無法啟動的工作會搶占，整個叢集同時只進行一輪；執行中的 session 不會被搶占。
- 有多個可停的工作時，先停這次執行跑得最短的（從頭重跑損失最少），一樣時停優先權較低的，只停到放得下為止。

Scheduler TOML 的相關設定：

| 設定 | 預設 | 說明 |
|---|---|---|
| `preemption_enabled` | `true` | `false` 時完全不搶占；沒有工作標記 `preemptible` 時本來就不會搶占 |
| `max_preemptions_per_job` | 3 | 每個工作最多被搶占幾次，之後不再被選為搶占對象 |

## 快速測試（fast lane）

AI coding 工具與 `git` 留在開發機；只有執行程式的指令送到遠端 session，在同一個容器與工作目錄執行，
即時印出輸出並以遠端的 exit code 結束。專案根目錄放一個 `.labflux.toml`（可以是空檔），
CLI 從目前目錄往上找到的那一層就是專案根目錄與快照範圍：

```toml
[session]          # 全部選填；命令列的 --cpu、--memory-mib、--gpu 優先
cpu = 2
memory_mib = 4096
gpu = 1
gpu_model = "3090"
image = "localhost/labflux/python-uv:3.12-uv0.11.8"   # 省略時使用 default_image
datasets = ["mnist"]
busy_wait_s = 120  # session 正在執行另一個指令時最多等幾秒；0 表示不等

[route]            # 增減預設送到遠端的指令
add = ["mypy"]
remove = ["nvidia-smi"]
```

```bash
uv run labflux run -- pytest -q        # 在自己的 session 執行，沒有就自動租用
uv run labflux shell                   # 開一個 PATH 前面放了指令替身的 shell，再從裡面啟動 AI 工具
uv run labflux local -- python -V      # 跳過替身，執行本機的真正指令
uv run labflux session status          # 目前的 session 與剩餘 lease 秒數
uv run labflux session files           # 只存在遠端 session 的檔案清單
uv run labflux session close           # 關閉 session，停止容器並釋放資源
```

親自走一次檔案流程（寫回、只在遠端的大檔、Ctrl-C、超時、閒置到期）的步驟見 [docs/demo-fast-lane.md](docs/demo-fast-lane.md)。

- 預設送到遠端的指令：`python`、`python3`、`pytest`、`torchrun`、`accelerate`、`deepspeed`、`nvidia-smi`；
  `uv` 只有 `uv run` 送到遠端。其他指令與專案外的指令都在本機執行。`bash run.sh` 在本機執行，
  腳本裡的每個 `python` 各自送到遠端；整個腳本要在遠端執行請用 `labflux run -- bash run.sh`。
- `labflux shell` 在 bash 與 zsh 的啟動檔執行完之後、以及每次顯示提示字元之前，把替身目錄放回 PATH 最前面，
  所以 conda、pyenv 或之後的 `conda activate` 不會蓋掉替身；alias 或 shell function 蓋住清單上的指令時，啟動時會印出警告。
  其他 shell 只在啟動前設定一次 PATH；可用 `command -v python` 確認。
- 每個指令執行前送出一份完整快照（打包規則與 `--source` 相同）：遠端覆蓋快照中的檔案、刪除上一份快照有而這份沒有的檔案，
  保留指令自己產生的檔案與 `.venv`；`pyproject.toml` 或 `uv.lock` 改變時先執行 `uv sync`。
  專案裡的 symbolic link 不會送出（不跟隨連結），每個指令提示一次。
- 指令新增或修改的檔案寫回本機同一位置（不含 `.venv`、`__pycache__` 等快取，每個指令上限 100 MB、10000 個檔案）；
  本機同一檔案在指令執行期間也被改過時不覆蓋，改存成 `<檔名>.remote`。遠端刪除的檔案不會刪除本機檔案。
- 超過寫回上限的檔案（例如 checkpoint）只留在遠端 session：累計列在專案的 `.labflux/remote-files.txt`
  （`labflux session files` 可印出），清單變動時終端機印一行提示。之後送到遠端的指令以同一個路徑讀得到這些檔案；
  session 結束時它們跟著刪除，要保留請寫到 `/outputs`（session 結束時上傳，之後以 session 的工作 ID 用 `labflux fetch` 取回）。
- 同一個 session 一次執行一個指令；同時送出的第二個指令會等前一個結束再執行（`busy_wait_s`，預設最多 120 秒）。
- exit code：遠端程式的 exit code；124 為指令超時，130 為 Ctrl-C 取消，125 為 LabFlux 錯誤，
  127 為替身找不到本機指令。
- Ctrl-C（或 CLI 的連線中斷）只停止這個指令：容器、工作目錄與 `.venv` 都保留，已寫出的檔案照常寫回，下一個指令沿用同一個 session。
  指令超過時限時則結束整個 session 容器，下一個指令自動租用新的 session。
- session 的 lease 是閒置時間：連續 10 分鐘沒有指令才回收，每個指令都會重新起算。
- session 與批次工作共用同一個資源帳本，每位使用者同時最多一個；session 映像需要支援 `sleep infinity`，
  並且有 `sh` 與可寫的 `/tmp`（取消指令時使用）。沒有 stdin／TTY，也不轉送開發機的環境變數。

Scheduler TOML 的相關設定：

| 設定 | 預設 | 說明 |
|---|---|---|
| `fast_lane_mode` | `session` | `session` 重用同一個容器；`oneshot` 每個指令結束就關閉 session，下一個指令使用新容器 |
| `session_lease_s` | 600 | 閒置 lease；指令開始與結束時延長，到期且沒有指令在執行時停止容器 |
| `session_queue_timeout_s` | 30 | 排隊中的 session 超過這段時間沒有被客戶端查詢（CLI 已結束）就取消 |
| `session_command_timeout_s` | 60 | 單一指令的時限 |
| `session_sync_timeout_s` | 900 | 指令前 `uv sync` 的時限 |
| `session_writeback_bytes` | 104857600 | 每個指令寫回的檔案總量上限 |

## 三台 VM 與驗證

建置、啟動與停止方式見 [docs/vm-lab.md](docs/vm-lab.md)。
VM 磁碟、SSH 私鑰、Token、執行日誌及 Scheduler 資料庫都放在被 Git 忽略的 `.labflux/vms/`。

```bash
uv run pytest -m 'not docker and not vm'
sg docker -c 'uv run pytest -m docker'
uv run python scripts/smoke.py --config .labflux/vms/scheduler.toml --faults
```

Docker 測試會啟動真實容器，直接驗證 cgroups、OOM、超時、串流及整個 HTTP 流程。
VM smoke test 使用三個不同 CPU／RAM 配置的 VM，驗證 CLI、實際並行、FCFS、
資源限制、取消，以及暫停 Worker 後的心跳逾時；完成或失敗時都寫出 JSON 紀錄。

## 實體機部署

kayle 執行 Scheduler 與 Worker，karma 執行 Worker，都以專屬帳號 `labflux` 的 systemd user service 執行。
步驟、防火牆與日誌見 [docs/lab-deploy.md](docs/lab-deploy.md)。節點清單放在被 Git 忽略的
`configs/local-lab.toml`（格式見 `configs/lab.example.toml`），產生的 Token 與設定放在 `.labflux/lab/`。

```bash
uv run python scripts/lab_deploy.py check                  # 前置檢查，唯讀
uv run python scripts/lab_deploy.py firewall-instructions  # 管理員要執行的防火牆指令
uv run python scripts/lab_deploy.py deploy --dry-run       # 印出部署步驟，不執行
uv run python scripts/lab_deploy.py status
```

## 目前邊界

目前交付到 M6.6、M7 的 GPU 配置（整張獨占）、uv 模式、實體機部署工具、M8 的排程策略，
M9 的門鈴長連線與快速測試 session（已實測 Claude Code 從 `labflux shell` 啟動後使用替身），
以及 M11 的搶占（被搶占的工作從頭重跑；checkpoint 與模型服務相關的部分之後加入）。
故障驗證（M6.7）缺少實際 VM 關機；vLLM 依後續階段加入。
已知限制見 [docs/verification.md](docs/verification.md)。
