# 實作與驗證紀錄

每個階段列出驗證命令與結果。尚未執行的實際 Docker／VM 測試不以模擬測試替代勾選。

## M0：專案環境

- 建立 Python 3.12、uv 鎖定依賴、CLI 入口、設定與結構化日誌。
- `uv run pytest tests/test_config.py`：7 passed。
- `uv run ruff check .`：passed。
- `uv sync` 與 `uv run labflux version`：成功建立鎖定環境，CLI 版本為 0.1.0。
- 使用者已完成系統套件安裝；透過 `sg docker`／`sg libvirt` 使用新群組權限。Docker 29.1.3、cgroups v2，CPU／RAM／swap 控制均可用。

## M1：Docker 執行器

- `uv run pytest tests/test_models.py`：8 passed，驗證資源數值、指令、環境變數及資料集名稱。
- `sg docker -c 'uv run pytest tests/test_runtime_docker.py'`：6 passed，實際啟動 Docker 容器，沒有跳過。

| Checklist | 對應驗證 |
|---|---|
| 1.1 | `test_models.py` 的有效／無效執行輸入 |
| 1.2、1.3 | `test_exit_code_and_both_output_streams`、`test_logs_arrive_before_completion` |
| 1.4 | 每個容器以 attempt 標籤驗證執行後清理；Worker 重送測試另見 M2 |
| 1.5、1.6 | `test_cancel_stops_container`、`test_timeout_cleans_up_child_processes` |
| 1.7 | `test_kernel_limits` 直接讀取容器的 `cpu.max`、`memory.max`、`memory.swap.max` |
| 1.8 | `test_memory_excess_is_killed`，加上上述退出、子程序、超時與串流案例 |

## M2～M4：Scheduler、Worker、資源帳本與檔案傳送

- 實際 HTTP／Worker／Docker 的 `test_real_http_worker_source_logs_readonly_dataset_and_output` 通過，包含程式下載、唯讀資料集、執行中日誌、重送命令與輸出下載。
- `sg docker -c 'uv run pytest -m "not vm"'`：60 passed，0 skipped（包含已新增的 M6 單元案例）。

| Checklist | 對應驗證 |
|---|---|
| 0.3、2.1 | 模組入口、`/health`、CLI `scheduler`／`worker` 及實際 HTTP 測試 |
| 2.2 | `test_schema_upgrade_and_persistence`，SQLite migration 1 可重複套用 |
| 2.3～2.7 | 實際 HTTP／Docker 端到端測試及 `test_scheduler.py` 的工作生命週期 |
| 2.8 | `test_commands_are_stable_until_acknowledged`、`test_replayed_start_runs_once_and_durable_result_blocks_rerun`，包含 20 次併發重送 |
| 3.1～3.3 | `test_heterogeneous_placement`、`test_memory_is_reserved_independently_from_cpu`，實際 Worker 探測容量 |
| 3.4 | `test_strict_fcfs_and_resource_release`，隊首阻擋時小工作不插隊 |
| 3.5、3.8 | `test_concurrent_submission_does_not_overallocate`，24 次併發提交；重複完成與啟動失敗案例 |
| 3.6、3.7 | 多個配置並存、完成後自動派送；三台 VM 的實際並行另見 M5 |
| 4.1、4.2 | `test_artifacts.py`、可重現快照與忽略規則測試 |
| 4.3 | `test_archives.py` 拒絕路徑跳脫、symlink、hardlink、裝置、超量與覆寫 |
| 4.4～4.6 | 實際 Docker 工作目錄／輸出目錄及唯讀資料集；`test_dataset_matching` |
| 4.7 | `test_cli.py` 提交、查詢、節點、取消與跨頁／跨 UTF-8 字元日誌 |
| 4.8 | CLI 下載校驗與解壓、實際容器產生的結果往返驗證 |

## M5：三台實際 VM

- `sg libvirt -c 'uv run python scripts/vm_lab.py provision'`：三台 Ubuntu 24.04 VM 建立成功，cloud-init 與 Docker 均完成。官方 image SHA256 為 `04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94`。
- `sg libvirt -c 'uv run python scripts/vm_lab.py deploy'`：wheel 與鎖定依賴安裝到三台 VM，Worker 由 systemd 執行。
- `uv run python scripts/smoke.py --config .labflux/vms/scheduler.toml --faults`：10 checks passed，使用 18 個實際工作；完整紀錄將保存於 `docs/evidence/midterm-smoke.json`。

| Checklist | 實際驗證 |
|---|---|
| 5.1 | `docs/vm-lab.md` 與 `scripts/vm_lab.py`，KVM／libvirt／NoCloud |
| 5.2 | libvirt 三個 domain，分別為 1／2／4 vCPU、2／3／4 GiB |
| 5.3、5.4 | Scheduler 在 host bridge，三個 Guest Worker；CLI 原始碼／資料集／輸出往返通過 |
| 5.5 | CPU 需求 1／2／4 的工作分別配置到 A／B／C，三台同時 RUNNING |
| 5.6 | 隊首等待 4 CPU 時，小工作保持 QUEUED，另兩台 VM 閒置；資源釋放後接續 |
| 5.7 | `cpu.max=25000 100000`、RAM 上限 64 MiB、swap=0；2 秒 wall time 實測約 0.500 秒 CPU time；OOM 被限制在容器 |
| 5.8 | 可重播 smoke script，包含同節點兩容器、跨節點並行、時限與回收 |

VM 的實測可分配 RAM：A 1455 MiB、B 2395 MiB、C 3403 MiB，均已扣除 512 MiB 系統保留量。
