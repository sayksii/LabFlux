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

`submit --wait` 等到工作完成；失敗、取消、狀態不明或等待逾時會以非零狀態退出。
`cancel` 對排隊工作立即生效；執行中的工作會先變成 `STOPPING`，確認容器停止才變成 `CANCELLED`。

Scheduler 在 `/dashboard` 提供監控頁（例如 `http://127.0.0.1:8000/dashboard`），以使用者 Token 登入。
節點以課表呈現已預約的 CPU／RAM，清單顯示自己的工作與等待原因；點選工作可看執行紀錄、即時日誌及取消。
設計脈絡與設計系統見 `PRODUCT.md`、`DESIGN.md`。

## 工作與資料

工作 JSON 至少包含指令參數陣列 `argv`；`image` 可省略，見下方的 uv 模式。`resources.cpu` 表示 CPU 時間配額，
`resources.memory_mib` 使用 MiB，`time_limit_s` 表示容器啟動後的執行時限。
工作在 `/workspace` 執行，把要取回的結果寫入 `/outputs`。

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

第一階段採嚴格 FCFS：隊首不能執行時，後續工作等待，即使其他節點仍有空間。
同一節點可同時執行多個工作，但所有預約不得超過其 CPU／RAM 可分配容量。
預約與派送在同一個 SQLite 交易完成；即時負載另外記錄，不與預約重複扣除。

Worker 主動領取命令，心跳由獨立執行緒每 2 秒回報，避免下載映像或執行長工作阻塞心跳。
命令接收紀錄與結果 outbox 保存在 Worker 的資料目錄，重送 START 不會重複執行同一 attempt。
超過 10 秒未收到心跳，節點變成 `OFFLINE`、未確認停止的工作變成 `UNKNOWN`，配置仍保留。
同一個 Worker instance 恢復心跳時，節點直接回到 `ONLINE`；它回報仍在執行的 attempt 從 `UNKNOWN`
回到 `RUNNING`，其餘維持 `UNKNOWN`，直到收到結果才釋放。

Worker 收到 SIGTERM／SIGINT 時只停止自己的執行緒，工作容器繼續執行。重新啟動的 Worker
註冊時附上本機的 attempt 紀錄與帶有自己標籤的容器，Scheduler 在同一個交易內核對每個未釋放的 attempt：

- 容器還在（執行中或已結束），或本機有尚未送出的結果：新 instance 接手，繼續監看與執行時限；
  日誌從中斷處接上，不重複也不遺漏。
- 本機紀錄顯示還沒建立容器：重新送出同一個 START，從頭執行。
- 兩者都沒有：已沒有程序在執行，標記為 `FAILED`（已要求取消則為 `CANCELLED`），並釋放 CPU／RAM／GPU。

核對完成後節點才回到 `ONLINE`，舊 instance 的事件與日誌一律被拒絕。已釋放或 Scheduler
不認得的容器，只有在 Worker 本機也有紀錄時才會停止並移除，以免誤刪同名 Worker ID 的其他安裝。
沒有附上清單的舊版 Worker 維持原本的行為：有未釋放 attempt 時拒絕註冊。

使用者 API 為 `/api/jobs`、`/api/nodes`、`/api/artifacts`，以使用者 Bearer Token 驗證；
內部 `/internal/workers` 使用獨立 Worker Token。使用者只能查看、取消與下載自己的工作。
`/health` 不需認證，`/docs` 提供 API 規格。

SQLite schema 在啟動時依序套用編號 migration（目前為版本 2）；也可執行 `uv run labflux db-upgrade`。
Scheduler 以單一程序執行，所有執行紀錄保存在設定的 `data_dir`。

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

目前交付到 M6.4 與 M7 的 GPU 配置（整張獨占）、uv 模式及實體機部署工具；
進階排程、互動 session 與 vLLM 依後續階段加入。
Worker 重啟後的對帳與接手（M6.4）已交付；確認停止後自動建立新 attempt 重排（M6.5）、
Scheduler 重啟恢復（M6.6）與完整故障驗證（M6.7）尚未交付，因此對帳後關閉的工作不會自動重跑。
