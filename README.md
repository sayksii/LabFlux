# LabFlux

OS 課程專案：以中央 Scheduler 與多個 Docker Worker 管理 CPU／RAM／GPU 批次工作。
實作進度與驗收條件見 [plan.md](plan.md)；驗證紀錄見 [docs/verification.md](docs/verification.md)。

## 開發環境

需要 Python 3.12 與 [uv](https://docs.astral.sh/uv/getting-started/installation/)。

```bash
uv sync --locked
uv run labflux version
uv run pytest -m 'not docker and not vm and not gpu'
uv run ruff check .
```

Docker 與 VM 驗證需要另外安裝 Docker Engine、KVM／libvirt 與 cloud-init 工具。
各服務使用 TOML 設定；CLI 可透過 `--config path.toml` 或 `LABFLUX_CONFIG` 指定，
沒有指定的 `scheduler_url`、`user_token` 取自 `labflux login` 存的 `~/.config/labflux/login.toml`。
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
範例設定的 Token 僅供本機開發；VM 與實驗室的部署腳本各自產生隨機 Token。

```bash
uv run labflux --config configs/demo.toml submit examples/jobs/hello.json --source examples/workloads/hello
uv run labflux --config configs/demo.toml status
uv run labflux --config configs/demo.toml status JOB_ID
uv run labflux --config configs/demo.toml logs JOB_ID --follow
uv run labflux --config configs/demo.toml fetch JOB_ID --directory outputs/JOB_ID
uv run labflux --config configs/demo.toml nodes
uv run labflux --config configs/demo.toml cancel JOB_ID
uv run labflux --config configs/demo.toml results ls
```

`submit --wait` 等到工作完成；失敗、取消或等待逾時（預設 120 秒，可用 `--wait-timeout` 調整）會以非零狀態退出。
狀態不明（`UNKNOWN`）不算結束：Worker 恢復連線或重啟接手後工作會繼續，所以會持續等待。
`cancel` 對排隊工作立即生效；執行中的工作會先變成 `STOPPING`，確認容器停止才變成 `CANCELLED`。

Scheduler 在 `/dashboard` 提供監控頁（例如 `http://127.0.0.1:8000/dashboard`），以使用者 Token 登入。
header 顯示目前的排程策略。節點以課表呈現已預約的 CPU／RAM／GPU 卡（有節點回報 GPU 時預設顯示 GPU 卡），
backfill 隊首的保留時段標在節點欄頭，session、模型服務與 backfill 先跑的工作各有標記。
「排隊順序」照 Scheduler 最近一輪排程實際考慮的順序，列出排隊的工作、分數拆解與排程器的決定（為什麼還不能開始），
下方是最近的排程決定（派送、backfill、搶占、重新排隊、節點失聯與上線、模型服務停止）；
「模型服務」顯示狀態、進行中與已服務的請求數和閒置時間，「誰在用」顯示每位使用者已預約的資源、執行／排隊數與近期占用。
範圍可選「所有人」（預設）或「我的」：每個登入的使用者都看得到所有人工作的摘要（擁有者、類別、狀態、資源、節點、
優先權、分數與排程說明），點開別人的工作也只顯示這份摘要；指令、環境變數、日誌、輸出與取消仍只限擁有者。
點選自己的工作可看執行紀錄、即時日誌及取消。設計脈絡與設計系統見 `PRODUCT.md`、`DESIGN.md`。

## 工作與資料

可直接提交的範例（CPU、RAM、GPU 型號／VRAM／多卡、優先權、可搶占、故障重跑、快速測試與 Gateway 請求）
與實驗室實測結果見 [examples/README.md](examples/README.md)。

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
| `project` | `--source` 目錄的專案名稱 | 專案名稱（與擁有者一起識別專案）；有它的工作保留專案目錄，見下方「專案目錄與接續」 |
| `checkpoint` | 無 | 程式存 checkpoint 的位置，相對於 `/workspace` 的檔案或資料夾（例如 `"ckpt/"`）；不能是絕對路徑，也不能含 `..`。被搶占時，損失從最後一次寫 checkpoint 算起，見下方「搶占」 |

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

### 專案目錄與接續

程式照常把 checkpoint 寫在專案目錄下（例如 `ckpt/model.pt`），下一次同專案的工作就從這裡接著開始，程式不用改路徑
（設計見 `docs/architecture.md` 6.1）：

- `submit --source DIR` 自動填入 `project`：`DIR/.labflux.toml` 的 `[project] name`，沒有時用 `DIR` 的資料夾名稱；
  工作 JSON 自己寫的 `project` 優先。沒有 `project` 的工作照舊：結束後刪除 `/workspace`，也不複製任何檔案。
- 有 `project` 的工作結束後（成功、失敗或取消），它的 `/workspace` 留在執行的節點上（刪除 `.venv`）。
- 同一位使用者同一個專案的下一個工作排到同一台時，Worker 先把上一次執行「產生」的檔案（不在它程式快照裡的檔案，
  不含 `.venv`、`__pycache__` 等快取與 symbolic link）複製到新的 `/workspace`，再放上這次的快照，同名時以這次送出的程式為準。
  同時執行的工作各有一份目錄。重跑的 attempt 從同一個工作上一個 attempt 的目錄接著開始。
- 排程優先選有這個專案最近結果的節點，只在放得下的節點中挑選、不會等待；排到別台時就從頭開始，`schedule_note` 會說明。
- 每個專案在每台節點保留最近 3 份（Scheduler 設定 `results_keep`），更舊的自動刪除。要長期保存的結果寫到 `/outputs`。
- `labflux results ls` 列出保留的目錄：專案、節點、工作、大小、結束時間；`deleted_by_next_run` 表示下一次執行就會刪除它。
- 節點永久故障時，那台保留的結果跟著遺失。

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
Worker 刪除這個 workspace（有 `project` 的工作只刪除 `.venv`，其餘保留，見上方「專案目錄與接續」），保留狀態、日誌與輸出封存檔。
同一台 Worker 上的工作共用這份可寫的快取。

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

排程策略由 Scheduler TOML 的 `scheduling_policy` 選擇，預設 `fcfs`
（實驗室部署時由節點清單 `[scheduler]` 的 `policy` 設定，見 [docs/lab-deploy.md](docs/lab-deploy.md)）：

| 模式 | 行為 |
|---|---|
| `fcfs` | 嚴格 FCFS：隊首不能執行時，後續工作等待，即使其他節點仍有空間 |
| `priority` | 依分數排序：`優先權 × 100 + 排隊分鐘 − 近期資源占用分鐘`，同分依提交順序 |
| `backfill` | `priority` 再加上 EASY backfilling：替無法執行的第一個工作依硬性時限計算保留時段，只讓不會延後該時段的工作先跑 |

近期占用是使用者在 `usage_window_s`（預設 3600 秒）內預約的分鐘數，乘上 CPU／RAM／GPU 中占叢集比例最高的一項。
權重為 `score_priority_weight`、`score_wait_weight`、`score_usage_weight`（預設 100、1、1）。
每個排隊工作的決策說明（例如「waiting behind job」、「reserved on 節點 at 時間」、「backfilled」）
出現在工作的 `schedule_note`。互動 session 排在批次工作之前。
`labflux queue` 顯示 Scheduler 最近一輪排程的排隊順序、分數、決定與 backfill 保留時段；`labflux status --all` 列出所有使用者工作的摘要（不含指令、環境變數與日誌）。
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

使用者 API 為 `/api/jobs`、`/api/nodes`、`/api/queue`、`/api/models`、`/api/artifacts`、`/api/sessions`、`/api/results`，以使用者 Bearer Token 驗證；
內部 `/internal/workers` 使用獨立 Worker Token。每個登入的使用者都看得到所有人工作的摘要（`/api/jobs?scope=all`、
`/api/nodes` 的 `allocations`、`/api/queue`）：擁有者、類別、狀態、資源、節點、優先權、分數與排程說明。
工作的指令、環境變數、映像、程式來源、attempt 紀錄、日誌與輸出只有擁有者看得到（模型服務的日誌例外，所有人都能讀），也只有擁有者能取消與下載；
別人工作的原因只在是 Scheduler 或 Worker 的固定說明（例如 `time limit exceeded`、`preempted by job …`）時照原文顯示，
其他原因（例如可能引用映像或指令的 Docker 錯誤）只顯示狀態（`failed`）。
`/health` 不需認證，`/docs` 提供 API 規格。

SQLite schema 在啟動時依序套用編號 migration（目前為版本 8）；也可執行 `uv run labflux db-upgrade`。
Scheduler 以單一程序執行，所有執行紀錄保存在設定的 `data_dir`。
新增的工作欄位與 WebSocket 介面需要 Scheduler、Worker 與 CLI 同時升級。

## 搶占

叢集滿載時，Scheduler 可以停掉擁有者允許中止的批次工作，把資源讓給更重要的工作。工作預設不會被搶占；
要允許時，在工作 JSON 加上兩個欄位（`checkpoint` 選填）：

```json
{
  "argv": ["uv", "run", "--locked", "python", "main.py", "--epochs", "3"],
  "resources": {"cpu": 4, "memory_mib": 8192, "gpu": 1},
  "priority": 3,
  "safe_to_rerun": true,
  "preemptible": true,
  "checkpoint": "ckpt/"
}
```

`safe_to_rerun` 表示程式從頭再跑一次不會造成傷害；`preemptible` 必須搭配它，否則提交時回傳 422，訊息指出缺少的欄位。
`checkpoint` 告訴 Scheduler 程式把 checkpoint 存在哪裡（相對於 `/workspace`），用來估計被停掉時損失多少，見下方「選誰」。

被搶占時：

- 容器照取消的流程停止，工作先變成 `STOPPING`，原因寫 `preempted by job ...`；確認容器終止後才釋放資源，搶占它的工作才開始執行。
- 那次 attempt 變成 `PREEMPTED`，`/outputs` 照常上傳；`labflux status JOB_ID` 列出所有 attempt，
  `labflux fetch JOB_ID --attempt-id ATTEMPT_ID` 取回被搶占那次的輸出。
- 工作以原本的提交順序回到 `QUEUED`，之後以新的 attempt 再執行一次，程式從進入點重新開始。有 `project` 的工作
  （`submit --source` 會自動填）重跑時優先排回原本的節點（只在放得下的節點中挑選，不會等待）；排回同一台時，新的 `/workspace`
  從被停掉那次的目錄開始（見上方「專案目錄與接續」），程式自己讀取停止前存的 checkpoint 就能接著跑。
  排到別台節點、或沒有 `project` 時，新的目錄裡沒有這些檔案，只能從頭跑。
  `labflux logs JOB_ID --follow` 追蹤時，重跑開始後在 stderr 印出 `--- attempt <id> (rerun) ---`，接著從頭印出新 attempt 的輸出。
- 每個工作最多被搶占 `max_preemptions_per_job` 次（預設 3），之後不再被選中，照常排隊與執行，不會因此失敗。
  被搶占不消耗 `retries_on_node_failure`。
- 停止期間工作剛好自己結束時，保留原本的結果；使用者同時取消時變成 `CANCELLED`。

誰可以搶占：

- 互動 session 與模型服務的啟動（自動釋放模型的冷啟動、常駐模型的啟動）：在所有排程策略下（包含 `fcfs`），
  可以搶占任何優先權的可搶占批次工作。
- 批次工作：只在 `priority` 與 `backfill` 策略下，而且只搶占顯式優先權（`priority`）比自己低、重新排隊後也會排在自己後面的工作。
  排了很久的低優先權工作等待加分很高，重新排隊後可能又排到第一；這種工作不搶，以免兩個工作一直互搶。
- 只有排在最前面、無法啟動的工作會搶占，整個叢集同時只進行一輪；執行中的 session 與模型服務不會被搶占。
  排在最前面的模型服務搶占不了時（`fcfs`、`priority` 下它不擋住後面的工作），下一個無法啟動的工作可以搶占。
- 已經在釋放的資源先算成空的：同一台節點上有工作正在停止（例如剛被取消、session 剛結束），或有閒置的自動釋放模型
  馬上會被回收時，只要等它們釋放就放得下，就不搶占，`schedule_note`（`labflux status JOB_ID`）寫出在等哪個工作或模型；
  不夠時，以它們已經空出來計算，只停補足差額所需的工作。
- 選誰：有多個可停的工作時，先停損失最少的，一樣時停優先權較低的，只停到放得下為止。損失是這次執行已經跑了多久；
  工作設了 `checkpoint` 時，改成距離最後一次寫 checkpoint 多久。Worker 每次心跳找出這個路徑下最新的檔案修改時間
  （不跟隨 symbolic link），回報給 Scheduler；`labflux status JOB_ID` 的 attempt 顯示為 `checkpoint_at`（Unix 時間）。
  早於這次開始執行的時間不算，例如從上一次執行複製過來、保留原本修改時間的檔案：等於這次還沒存過。
  限制：只看修改時間，寫到一半的檔案也算存好了；沒有 `project`、或重跑排到別台節點時，實際損失是整次執行，
  但 Scheduler 選擇時仍以距離 checkpoint 的較小損失計算。

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
[project]          # 選填；省略時用專案根目錄的資料夾名稱
name = "vqa"       # 新的 session 從這個專案最近保留的結果開始；submit --source 也用這個名稱

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
- 新開的 session 若排在保留著這個專案最近結果的節點（見「專案目錄與接續」），先複製那次執行產生的檔案，
  所以可以直接讀上一個批次工作的 checkpoint；這些檔案沒被指令改過就不寫回。session 自己的檔案仍在結束時刪除，不算一次執行。
  已經開著的 session 不會因為換了專案目錄而重新複製。
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

## 測試與驗收

```bash
uv run pytest -m 'not docker and not vm and not gpu'          # 單元測試
sg docker -c 'uv run pytest -m "docker and not gpu"'           # 真實容器
sg docker -c 'uv run pytest -m gpu'                            # 只在 GPU 閒置的節點執行
uv run python scripts/lab_smoke.py --config .labflux/lab/cli.toml   # 實驗室三台，見 docs/lab-deploy.md
```

Docker 測試會啟動真實容器，直接驗證 cgroups、OOM、超時、串流及整個 HTTP 流程。
GPU 測試直接使用本機的卡（只執行 `nvidia-smi -L`），實驗室的 kayle 上這兩張卡同時在 Worker 的資源池裡，
請在沒有工作排隊、卡閒置時執行。實驗室 smoke 以 GPU 為主，檢查卡的隔離、不重複配置、排隊、多卡、
真實 CUDA、session 搶占、FCFS、資源限制與 Gateway，結果寫成 JSON（例：[docs/evidence/](docs/evidence/)）。

### 三台 VM（期中）

期中驗收用的三台 VM（KVM／libvirt）已經移除，建置、啟動與停止方式保留在 [docs/vm-lab.md](docs/vm-lab.md)。
VM 磁碟、SSH 私鑰、Token、執行日誌及 Scheduler 資料庫都放在被 Git 忽略的 `.labflux/vms/`。
`uv run python scripts/smoke.py --config .labflux/vms/scheduler.toml --faults` 只適用於這三台 VM，
驗證 CLI、實際並行、FCFS、資源限制、取消，以及暫停 Worker 後的心跳逾時（紀錄：[docs/evidence/midterm-smoke.json](docs/evidence/midterm-smoke.json)）。

## 實體機部署

kayle 執行 Scheduler 與 Worker，karma 與 kindred 執行 Worker，都以專屬帳號 `labflux` 的 systemd user service 執行。
步驟、防火牆與日誌見 [docs/lab-deploy.md](docs/lab-deploy.md)。節點清單放在被 Git 忽略的
`configs/local-lab.toml`（格式見 `configs/lab.example.toml`），產生的 Token 與設定放在 `.labflux/lab/`。

使用者就是節點上的 Linux 帳號：在任一台 LabFlux 節點執行 `labflux login`，本機 Worker 透過 kernel 確認帳號後，
向 Scheduler 申請個人 token，存到 `~/.config/labflux/login.toml`（權限 600），之後的指令自動帶上。
細節見 [docs/lab-deploy.md](docs/lab-deploy.md) 的「使用者登入」；每台節點所有帳號共用的 CLI
（`/opt/labflux`，需要 sudo）見同一份文件的「所有帳號共用的 CLI」。

```bash
uv run python scripts/lab_deploy.py check                  # 前置檢查，唯讀
uv run python scripts/lab_deploy.py firewall-instructions  # 管理員要執行的防火牆指令
uv run python scripts/lab_deploy.py deploy --dry-run       # 印出部署步驟，不執行
uv run python scripts/lab_deploy.py status                 # 各台服務、版本與 Scheduler 看到的預約量
```

啟動、停止、更新與移除見 [docs/lab-deploy.md](docs/lab-deploy.md)；排程策略（清單的 `[scheduler] policy`）與
模型服務（清單的 `[models.<名稱>]`）也在同一份文件。

## 目前邊界

目前交付到 M6.6、M7 的 GPU 配置（整張獨占）、uv 模式、實體機部署工具、M8 的排程策略，
M9 的門鈴長連線與快速測試 session（已實測 Claude Code 從 `labflux shell` 啟動後使用替身），
M10 的共享模型服務與推論 Gateway（`docs/architecture.md` 第 9 節）、M11 的搶占（依最後一次 checkpoint 計算損失），
以及專案目錄保留在節點、下一次同專案的工作接著開始（`docs/architecture.md` 6.1；被搶占的專案工作重跑時也從這裡接著跑）、
以 Linux 帳號登入（`labflux login`），與 M14 的安裝說明、範例和乾淨環境的驗收（14.1、14.2、14.4）。
M12 的監控與決策說明（排隊順序、分數、等待原因、backfill 保留時段與最近的排程決定由 Scheduler 的 `/api/queue` 提供，
Dashboard 與 CLI 呈現）已以單元測試、展示分鏡測試與 headless Chromium 驗證，實驗室實測尚未進行。
展示腳本（M14.3）的操作手冊見 [docs/demo.md](docs/demo.md)，各幕的工作檔在 `examples/demo/`，以 `scripts/demo.py` 執行；
已在測試環境逐幕重播，實驗室預演尚未進行。
故障驗證（M6.7）缺少實際 VM 關機。
已知限制見 [docs/verification.md](docs/verification.md)。
