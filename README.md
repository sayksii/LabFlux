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
