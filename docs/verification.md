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
| 1.4 | 啟動後直接檢查 job／attempt／worker 標籤，結束後以名稱與標籤確認容器均不存在；Worker 重送測試另見 M2 |
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
- `uv run python scripts/smoke.py --config .labflux/vms/scheduler.toml --faults`：10 checks passed，使用 18 個實際工作；完整紀錄保存於 [midterm-smoke.json](evidence/midterm-smoke.json)。

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

## M6.1～M6.3：心跳、失聯與取消

| Checklist | 對應驗證 |
|---|---|
| 6.1 | `test_heartbeat_renews_deadline_then_expiry_retains_allocations` 驗證心跳重設時限；VM 實際暫停 Worker，約 9.686 秒後偵測失聯 |
| 6.2 | 失聯節點 OFFLINE、工作 UNKNOWN、保留 4 CPU 配置；新工作保持 QUEUED；新 Worker instance 不得掩蓋舊配置 |
| 6.3 | 排隊取消不產生 attempt；執行中取消先 STOPPING、確認停止後 CANCELLED；延遲 START 不覆蓋取消；UNKNOWN 執行可重新確認停止 |

- `tests/test_heartbeat_cancel.py`：6 個排程／生命週期案例。
- `tests/test_worker.py`：3 個 Worker 案例，包含 20 次併發重送、磁碟接單紀錄、未確認終止時保留配置，以及對 UNKNOWN 的停止確認。
- 實際 Docker 的 `test_stop_before_start_confirms_no_container_without_pulling_image`：STOP 先於 START 時不建立容器、不需下載映像，即可確認取消。
- VM fault test 暫停的是 Worker 程序，而非整台 VM 或 Docker；恢復後確認舊容器停止，再釋放資源與派送等待工作。
- 未實作或勾選 M6.4～M6.7：自動清理／對帳、重新排隊與執行中重啟恢復。

## M7（進行中）：實體 GPU 節點

### 7.2 NVIDIA Container Toolkit 手動驗證（2026-10-26）

兩台 GPU 節點都已安裝 NVIDIA Container Toolkit 1.20.1。`/etc/docker/daemon.json` 只註冊 `nvidia` runtime，
Docker 預設 runtime 仍是 `runc`，一般容器不會自動取得 GPU。測試前確認 GPU 閒置、沒有其他容器在執行；
測試只執行 `nvidia-smi -L`，不佔用 VRAM，結束後沒有殘留容器。

| 檢查 | kayle（RTX 3090 + RTX A5000，驅動 535.309.01） | karma（RTX 4090，驅動 580.178.04） |
|---|---|---|
| `docker run --rm --gpus all ubuntu nvidia-smi -L` | 列出 3090 與 A5000 | 列出 4090 |
| `--gpus '"device=<UUID>"'` 指定單張卡 | 只看到 A5000，容器內只有 `/dev/nvidia1` | 只看到 4090 |
| 不加 `--gpus` | 沒有 `nvidia-smi`，也沒有 `/dev/nvidia*` | 同左 |

- kayle 另外確認：使用者以 `-e` 傳入 `NVIDIA_VISIBLE_DEVICES=all` 或 `void` 時，
  Docker 為 device request 附加的值優先，容器仍只看到指定的卡。

### 7.5、7.7 Scheduler 的 GPU 配置（單元測試）

- SQLite migration 2 新增 `attempt_gpus`，記錄每個 attempt 分到的 GPU UUID。
  partial unique index 讓同一張卡最多只有一筆未釋放紀錄；預約與 START 命令在同一個交易，
  確認容器終止後才與 attempt 一起釋放。
- `tests/test_gpu_scheduling.py`：14 個案例，涵蓋張數／型號／VRAM 匹配、單一節點多卡、
  CPU 工作不佔卡、外部使用中與尚未回報的卡不分配、GPU 隊首阻擋 FCFS、24 個 GPU 工作併發提交到 3 張卡
  （剛好 3 個 STARTING 且 UUID 不重複）、重送及延遲完成事件不會釋放下一個工作的卡、
  未確認終止與心跳逾時保留配置、資料庫拒絕同一張卡的第二筆未釋放紀錄，以及 v1 升級到 v2。
- 以 `main`（schema v1）的程式實際建立 SQLite，再用新程式開啟：升級到 v2、工作保留、index 建立，
  重開後版本不變；`labflux db-upgrade` 顯示 `Schema version 2`。
- `uv run pytest -m 'not docker and not vm'`：68 passed。`uv run pytest -m docker`：8 passed（CPU 容器回歸）。
- 尚未驗證：Worker 以 NVML 回報 GPU、容器實際只拿到分配的卡（7.3、7.4、7.6），所以 7.5 先不勾選。

## 最後驗證與交付

- `uv sync --locked`：通過。
- `uv run ruff check .`、`uv run ruff format --check .`、`git diff --check`：通過。
- `sg docker -c 'uv run pytest'`：**61 passed，0 skipped**；包含 8 個實際 Docker 測試，其餘為 API／排程／Worker／CLI／封存檔等測試。
- 三台 VM smoke：10 checks passed，使用 18 個工作；其中 OOM／超時為預期 FAILED，取消為預期 CANCELLED。
- 環境閒置時，三台 VM 正常關機、Scheduler 程序群組停止，再重新啟動及部署成功；未以此宣稱執行中故障恢復已完成。
- 本次完成 50 個編號 checklist 項目，對應 M0～M5、M6.1～M6.3。
- Git 僅追蹤程式、範例、規劃、測試與去除認證資料的驗收紀錄；私鑰、Token、VM 磁碟與即時資料目錄均被忽略。
