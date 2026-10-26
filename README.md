# LabFlux

OS 課程專案：以中央 Scheduler 與多個 Docker Worker 管理 CPU／RAM 批次工作。
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

工作 JSON 至少包含 `image` 與指令參數陣列 `argv`。`resources.cpu` 表示 CPU 時間配額，
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

## 排程與通訊

第一階段採嚴格 FCFS：隊首不能執行時，後續工作等待，即使其他節點仍有空間。
同一節點可同時執行多個工作，但所有預約不得超過其 CPU／RAM 可分配容量。
預約與派送在同一個 SQLite 交易完成；即時負載另外記錄，不與預約重複扣除。

Worker 主動領取命令，心跳由獨立執行緒每 2 秒回報，避免下載映像或執行長工作阻塞心跳。
命令接收紀錄與結果 outbox 保存在 Worker 的資料目錄，重送 START 不會重複執行同一 attempt。
超過 10 秒未收到心跳，節點變成 `OFFLINE`、未確認停止的工作變成 `UNKNOWN`，配置仍保留。

使用者 API 為 `/api/jobs`、`/api/nodes`、`/api/artifacts`，以使用者 Bearer Token 驗證；
內部 `/internal/workers` 使用獨立 Worker Token。使用者只能查看、取消與下載自己的工作。
`/health` 不需認證，`/docs` 提供 API 規格。

SQLite schema 由啟動時套用 migration 1；也可執行 `uv run labflux db-upgrade`。
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

## 目前邊界

目前交付到 M6.3，使用 CPU／RAM；GPU、進階排程、互動 session 與 vLLM 依後續階段加入。
Worker 失聯後的自動對帳、重排，以及完整 Scheduler 重啟恢復屬於 M6.4～M6.7，尚未交付。
有未釋放 attempt 的 Worker 若以新 instance 重啟，註冊會被拒絕並保留配置，避免重複執行。
