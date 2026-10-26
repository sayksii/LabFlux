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
- 未實作或勾選 M6.5～M6.7：重新排隊、Scheduler 重啟恢復與完整故障驗證（M6.4 見下節）。

## M6.4：Worker 重啟後的對帳與接手（kayle，2026-10-27）

使用者決定：Worker 重啟時，執行中的工作繼續跑，由新 instance 接手。

- Worker 收到 SIGTERM／SIGINT（`stop_event`）時不再停止容器；runtime 回傳「已脫離」，本機結果維持未定。
- 重新啟動的 Worker 在註冊時附上 `inventory`：本機 attempt 紀錄（`launched`、`unsent_result`）
  與帶有 `labflux.worker_id` 標籤的容器（狀態、exit code、OOM、啟動時間）。
- Scheduler 在註冊的同一個交易內核對每個未釋放的 attempt：容器存在或有未送出結果 → `adopt`；
  本機紀錄顯示尚未建立容器 → `restart`（重新交付同一個 START）；START 尚未交付 → 保留；
  其餘 → `closed`，標記 `FAILED`（已要求取消則 `CANCELLED`），同時釋放 CPU／RAM／GPU。
  已釋放或未知 attempt 的容器列入 `remove`，Worker 只移除本機也有紀錄的那些。
- 同一個 instance 在心跳中斷後恢復時，心跳附上 `active_attempts`；節點回到 `ONLINE`，
  因心跳逾時變成 `UNKNOWN` 且仍在執行的 attempt 回到 `RUNNING`，其餘維持 `UNKNOWN`。

**日誌去重。** 接手時以本機 `logs.jsonl` 為準：先截掉崩潰留下的半行，重算 `next_seq`、
已存位元組與上傳進度，再分別計算 stdout／stderr 已擷取的位元組數。接手的容器改用
`docker logs --follow`（json-file 日誌從頭重播後持續跟隨，中間沒有空窗），每個 stream
跳過已擷取的位元組。實際驗證時 Docker 使用 json-file 日誌、沒有設定輪替；若日誌被輪替，
重播起點會改變，這個方法就不再精確。STARTED 事件 ID 改為每個 instance 各自一個，
避免接手的 instance 因不知道舊 instance 是否送過而卡在 ID 衝突。

| 案例 | 驗證 |
|---|---|
| 執行中／已結束／剛建立的容器被接手，配置保留，舊 instance 的事件被拒絕 | `test_surviving_container_is_adopted_with_its_allocation`（running、exited）、`test_created_container_is_adopted_as_starting` |
| 只有未送出結果 | `test_unsent_result_is_adopted_without_a_container` |
| 尚未建立容器 → 重新交付 START；已要求取消 → CANCELLED | `test_never_launched_attempt_restarts_from_a_redelivered_start`、`test_never_launched_cancelled_attempt_closes_as_cancelled`、`test_shutdown_before_launch_restarts_the_attempt_on_the_next_instance` |
| START 尚未交付 | `test_undelivered_start_is_left_for_normal_polling` |
| 容器消失 → FAILED、釋放 GPU，重送註冊不重複釋放 | `test_missing_container_closes_once_and_releases_cards`、`test_new_instance_closes_an_attempt_whose_container_vanished` |
| 待送出的 STOP 在接手後仍有效 | `test_pending_stop_survives_adoption` |
| 重複註冊結果相同 | `test_repeated_registration_adopts_the_same_attempt_again` |
| 其他 Worker 的清單被拒絕，狀態不變 | `test_inventory_from_another_worker_is_rejected` |
| 過期容器只在本機有紀錄時移除 | `test_stale_containers_are_listed_for_removal`、`test_stale_container_is_removed_only_with_a_local_record` |
| 同一 instance 恢復心跳 | `test_same_instance_returns_online_and_resumes_only_active_attempts` |
| 接手的 GPU 仍保留 | `test_adopted_attempt_keeps_its_card` |
| 日誌檔修復 | `test_log_repair_drops_a_torn_line_and_follows_the_file` |

實際 Docker（`tests/test_reconnect_docker.py`，真實 HTTP Scheduler、Worker 與容器）：

- 每 0.2 秒印一行、共 25 行的工作：停止 Worker 後容器仍在執行，等待 1 秒再啟動新 instance；
  工作 `SUCCEEDED`、單一 attempt、exit code 0，日誌恰好是 `line 0`～`line 24` 各一次，配置釋放，沒有殘留容器。
- 停機期間結束（exit 3）的容器：新 instance 回報真實結果 `FAILED`、exit code 3，日誌 `one`、`two` 各一次。
- 停機期間被手動刪除的容器：新 instance 註冊後工作 `FAILED`（`container missing after worker restart`），
  節點 `ONLINE`、配置釋放，後續工作正常完成。
- 反向檢查：暫時把日誌跳過量改成 0，第一個測試立即失敗（日誌重複），還原後通過。
- 3 個 Docker 測試連續執行 6 次全部通過，每次約 10 秒。

命令與結果：

- `uv run pytest -m 'not docker and not vm'`：110 passed（新增 19 個）。
- `uv run pytest -m docker`：16 passed，0 skipped（新增 3 個，含 3 個 GPU 測試；執行前兩張卡閒置、無 compute process）。
- `uv run ruff check .`、`uv run ruff format --check .`、`git diff --check`：通過。

**Worker 拒絕的命令不再讓 Worker 停止。** 在 systemd `Restart=on-failure` 下，被拒絕的命令每次重啟後
都會再被領取一次，原本會造成 Worker 無限重啟。現在每個命令先分類，再個別 ack：

- 屬於這個 Worker、attempt ID 有效但內容無效（例如分配了本機 GPU 池以外的卡、格式錯誤的工作）：
  記錄 `FAILED`、`termination_confirmed=True`（沒有建立容器），經 outbox 回報後 Scheduler 釋放資源，並 ack。
  已經有本機紀錄的 attempt 不會被覆寫。
- attempt ID 無效或命令指名其他 Worker：記錄 error 並 ack，不建立任何本機狀態；Scheduler 保留原配置。
- `accept()` 中的意外例外：記錄後不 ack，下一次輪詢重試；同一批的其他命令照常處理。
- 接手（adopt）時 payload 已無法驗證（例如 GPU 池改變）：記錄 error，保留配置，不中斷啟動。

`tests/test_command_rejection.py`（4 個）：本機池外 GPU 的 START 變成 `FAILED` 且釋放 CPU／GPU；
格式錯誤、指名其他 Worker、attempt ID 無效與非物件的命令都被 ack，只有有效 ID 得到結果；
拒絕不覆寫執行中的 attempt；真實 HTTP 下的 Worker 迴圈在收到指名其他 Worker 的命令、
以及一次 `accept()` 意外例外後仍在運作，並完成下一個有效工作。反向檢查：換回原本的迴圈後，最後一個測試失敗。

尚未處理：對帳後關閉的工作不會自動重排（M6.5）；Scheduler 重啟後的核對（M6.6）仍未交付。

## M7（進行中）：實體 GPU 節點

### 7.1 實機前置檢查（2026-10-27）

`scripts/lab_deploy.py check` 以部署鑰匙登入兩台的 `labflux` 帳號（host key 固定在 `.labflux/lab/known_hosts`），
逐項列出 Docker、cgroups、NVIDIA 驅動、Toolkit、磁碟與網路狀態。執行前兩台都沒有運算程式、各卡已用
VRAM 小於 1 GiB，所以 GPU 測試容器有執行；它只跑 `nvidia-smi -L`，結束後兩台都沒有殘留容器。
`uv run python scripts/lab_deploy.py check` 結束狀態 0（沒有 `FAIL`）。

| 檢查 | kayle | karma |
|---|---|---|
| SSH、linger、user systemd | `labflux@kayle`、yes、running | `labflux@karma`、yes、running |
| Docker、cgroup、限制 | 29.1.3、v2、memory／swap／CPU quota | 同左 |
| 預設 runtime | `runc`（另註冊 `nvidia`） | 同左 |
| 驅動、CUDA（驅動上限） | 535.309.01、12.2 | 580.178.04、13.0 |
| GPU 與資源池 | 3090 24576 MiB、A5000 24564 MiB；清單 `[0, 1]` 都存在 | 4090 24564 MiB；清單 `[0]` 存在 |
| nvidia-ctk | 1.20.1 | 1.20.1 |
| GPU 容器（`--gpus all`） | 看到 2/2 張 | 看到 1/1 張 |
| CPU 容器（不加 `--gpus`） | 0 個 `/dev/nvidia*` | 0 個 |
| 剩餘磁碟（家目錄／Docker） | 736／736 GiB | 3467／3467 GiB |
| 到 `192.0.2.10:8770` | 連線被拒絕：路由正常，Scheduler 尚未啟動 | 同左 |

預期中的 `WARN`：兩台的 `labflux` 帳號還沒有 uv（部署時安裝）、Scheduler 尚未啟動，
以及 kayle 的 `labflux-firewall.service` 尚未安裝（部署前必須由管理員安裝）。

- `deploy --dry-run` 對兩台印出完整步驟，沒有執行任何遠端或本機指令，也沒有寫出任何檔案。
- `tests/test_lab_deploy.py`：18 passed，涵蓋節點清單驗證、產生的 TOML 可被 `Settings` 載入、
  systemd unit、防火牆規則（只 DROP，啟動加入與停止刪除同一條）、Token 重用、重啟保護與檢查結果判讀。
- 7.8 尚未執行：實際部署要等管理員安裝防火牆，且有工作執行時的重啟要等 M6.4。

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
- 這時尚未驗證 Worker 以 NVML 回報 GPU、容器實際只拿到分配的卡，所以 7.5 先不勾選；
  型號與 VRAM 匹配要在實機以 `gpu_model` 指定 3090／A5000／4090 驗證後再勾。

### 7.3、7.6、7.7 Worker 的 GPU 偵測與容器隔離（kayle）

- Worker 啟動時用 NVML（`nvidia-ml-py` 13.615.71）把設定的 index／UUID 解析成 GPU UUID、型號與總 VRAM，
  連同驅動版本（535.309.01）與 CUDA 驅動版本（12.2）一起註冊。設定了 GPU 卻讀不到 NVML、
  卡不存在或重複列出時拒絕啟動；沒有設定 GPU 的 Worker 不載入 NVML。
- 心跳回報每張卡的已用 VRAM 與使用率。不屬於本機未結束 attempt 的卡，只要有 compute process
  或已用 VRAM ≥ 1024 MiB 就標為外部使用。已用 VRAM 採用 NVML v2 記憶體資訊（不含驅動保留量），
  與 nvidia-smi 一致：kayle 閒置時為 12 MiB，v1 會算成約 330 MiB。
- GPU 工作以 `DeviceRequest(device_ids=[UUID...], capabilities=[["gpu"]])` 只取得分配的卡；
  CPU 工作維持 `NVIDIA_VISIBLE_DEVICES=void`，沒有 device request。工作的 env 不能設定 `NVIDIA_VISIBLE_DEVICES`。
- 測試前確認兩張卡閒置（12 MiB、0%、沒有 compute process）且沒有其他容器；
  測試只執行 `nvidia-smi -L`，結束後沒有殘留容器或 GPU process。
- `tests/test_gpu_docker.py`（`docker` 與 `gpu` marker，實際 Docker 與 NVML，沒有跳過）：
  - 3090 與 A5000 各自只看得到被分配的那張卡，容器內只有一個 `/dev/nvidiaN`。
  - CPU 工作沒有 `/dev/nvidia*`，也沒有 `nvidia-smi`。
  - 真的 Scheduler + Worker（`gpus = [0, 1]`）同時執行兩個 `gpu=1` 的工作：執行時間重疊、
    分到不同的卡、各自只看到自己的卡，完成後釋放。
- `tests/test_gpu_worker.py`：16 個案例，以假的 NVML 測 index／UUID 解析、不存在或重複的卡、NVML 不可用、
  未設定 GPU 時不載入 NVML、外部使用判斷、讀不到的卡不回報、START 指定本機以外的卡被拒，以及設定與 env 驗證。
- 修正既有 runtime 的日誌擷取：原本在 `start` 之後才以 `logs=True` attach，立刻輸出的工作
  （例如 `nvidia-smi -L`）偶爾會遺失或重複輸出，修正前 `pytest -m docker` 4 次中 3 次失敗；
  已經結束的容器也要等 5 秒的讀取逾時。改成 start 前先 attach 後連續 8 次通過，
  並新增 `test_immediate_output_is_captured_exactly_once`。
- `uv run pytest -m 'not docker and not vm'`：84 passed。`uv run pytest -m docker`：12 passed（含 3 個 GPU 測試）。
  `uv run pytest -m gpu`：3 passed。
- 只在 kayle 驗證；karma 的 4090 在部署（步驟 6）時驗證。7.4 的外部使用判斷目前只有假 NVML 的單元測試，
  用真的 GPU process 驗證後才勾選。

### uv 模式（kayle，2026-10-27）

plan.md 沒有對應的 checklist 項目，以下只記錄驗證結果。

- 基礎映像 `localhost/labflux/python-uv:3.12-uv0.11.8` 以 `images/python-uv/Dockerfile` 在 kayle 建置：
  `python:3.12-slim-bookworm` 與 `ghcr.io/astral-sh/uv:0.11.8` 都以 digest 固定，映像約 70 MB，
  內含 Python 3.12.15 與 uv 0.11.8。以沒有 passwd 的 UID 2001 執行時，`HOME=/tmp` 可寫入。
- 工作省略 `image` 時，Scheduler 在提交當下填入 `default_image`。Worker 設定 `uv_cache_dir` 後，
  快取以讀寫方式掛載於 `/uv-cache`，並覆蓋工作自己設定的 `UV_CACHE_DIR`、`UV_LINK_MODE`。
  確認容器停止且輸出已回傳後才刪除 workspace；終止未確認時保留，刪除失敗只記錄警告。
- `tests/test_uv_mode.py`：6 個案例。`tests/test_uv_docker.py`：在基礎映像以 `uv run --locked --offline`
  執行無依賴的專案，`.venv` 位於 `/workspace/.venv`，快取寫入 `uv_cache_dir`，`HOME` 可寫入。
- 實際執行 `examples/uv-gpu`：Scheduler 只綁 127.0.0.1，Worker 使用 `gpus = [0, 1]` 與暫時的 `uv_cache_dir`。
  執行前兩張卡閒置（12 MiB、0%、沒有 compute process）、沒有其他容器。為了量測，指令外包一層 shell，
  結束時讀取容器 cgroup 的 `pids.peak` 與 `memory.peak`。

  | | 第一次（下載 torch） | 第二次（快取命中） |
  |---|---:|---:|
  | 提交到完成 | 153.3 秒 | 10.1 秒 |
  | 容器執行時間 | 152.5 秒 | 9.1 秒 |
  | uv 安裝 24 個套件 | 下載約 3.1 GB 後 3.5 秒 | 5.5 秒（從快取複製） |
  | `pids.peak`（上限 256） | 51 | 28 |
  | `memory.peak`（上限 8 GiB） | 8 GiB | 8 GiB |

  - 兩次都分到 A5000（VRAM 較小的卡優先），輸出 `torch 2.6.0+cu124`、`torch.version.cuda` 12.4、
    `cuda_available: true`、`device_name: NVIDIA RTX A5000`，GPU 與 CPU 的矩陣乘法結果一致。
    kayle 的驅動 535 只支援到 CUDA 12.2，cu124 wheel 透過 CUDA minor version compatibility 可以正常執行。
  - `memory.peak` 達到 8 GiB 上限是寫入約 5 GB 套件產生的 page cache，可回收，兩次都沒有 OOM。
  - uv 快取 5.1 GB；兩個 attempt 的 workspace（含 `.venv`）都在完成後刪除，保留 state、日誌與輸出封存檔。
  - torch 會印出找不到 NumPy 的警告；範例沒有用到 NumPy，不影響結果。
  - 結束後沒有殘留容器或 GPU process，暫時的 uv 快取已刪除。
- `uv run pytest -m 'not docker and not vm'`：90 passed。`uv run pytest -m docker`：13 passed（含 3 個 GPU 測試）。
  `uv run pytest -m gpu`：3 passed。

### MNIST 訓練範例（kayle，2026-10-27）

- `examples/mnist/main.py` 改寫自 pytorch/examples 的 `mnist/main.py`（commit
  `d0b7e3709407074ac64bb34c5af33fe226abb47c`），保留 BSD-3-Clause 授權（`examples/mnist/LICENSE`）。
  torch 2.6.0 與 torchvision 0.21.0 都來自 cu124 index，與 `examples/uv-gpu` 共用快取。
- 以 CLI 透過真的 Scheduler（只綁 127.0.0.1）與 Worker（`gpus = [0, 1]`、全新的暫時 `uv_cache_dir`）執行；
  執行前兩張卡閒置（12 MiB、0%、沒有 compute process），沒有其他容器。

  ```bash
  labflux submit examples/jobs/mnist.json --source examples/mnist --wait --wait-timeout 1800
  labflux logs JOB_ID
  labflux fetch JOB_ID --directory DIR
  ```

  | 項目 | 結果 |
  |---|---|
  | 狀態 | SUCCEEDED，image 由 Scheduler 填入 `localhost/labflux/python-uv:3.12-uv0.11.8` |
  | 使用的卡 | NVIDIA RTX A5000（`GPU-11111111-…`） |
  | `submit --wait` 總時間 | 4 分 26 秒（快取是空的：下載 torch／torchvision 等 27 個套件） |
  | 容器執行時間 | 265.3 秒 |
  | 程式總時間（`metrics.json`） | 92.7 秒，其中 3 個 epoch 共約 13.3 秒，其餘是下載約 11 MB 的 MNIST 與 CUDA 初始化 |
  | 各 epoch 測試準確率 | 98.35%、98.68%、99.02% |
  | 最終測試準確率 | **99.02%**（9902/10000） |

- `labflux logs` 取回完整日誌（含 `Device: NVIDIA RTX A5000`）；`labflux fetch` 下載並解開
  `mnist_cnn.pt`（4.8 MB）與 `metrics.json`，校驗碼相符。
- 完成後 attempt 的 workspace（含 `.venv` 與 `/workspace/data`）已刪除，GPU 配置釋放；uv 快取 5.2 GB。
- 結束後沒有殘留容器、GPU process 或監聽的 port；暫時的 uv 快取與資料已刪除。

### Dashboard 的 GPU 卡課表（2026-10-27）

- 資源切換多一個「GPU 卡」，只有節點回報了 `capacity.gpus` 才出現；每張卡一格、整張獨占。
  自己的工作用工作色塊（兩張卡就是兩個同色色塊），其他使用者的卡是灰色，外部使用中的卡是灰色斜線，
  尚未回報的卡標「尚未回報」，失聯節點保留紅色斜線且不顯示過期讀數。叢集列、節點欄頭、排隊說明、
  工作詳情（GPU 需求與分到的卡）都依 API 的 GPU 欄位顯示；CPU、RAM 課表不變。
- 實際 HTTP 驗證：以本分支在 127.0.0.1 啟動 Scheduler（含排程迴圈），經內部 API 註冊模擬 kayle（3090 + A5000）、
  karma（4090）、一台尚未回報 GPU 的節點，以及一台註冊時完全沒有 GPU 欄位的節點，持續送心跳；
  兩個使用者提交 1 張卡、2 張卡、CPU 與需要排隊的 GPU 工作，之後讓 2 卡工作完成、把 3090 標成外部使用、
  停止一台節點的心跳直到失聯。
- 以 Playwright headless Chromium 擷取 GPU／CPU／RAM 課表在 1440px 與 390px、投影模式及工作詳情共 12 張截圖：
  console 錯誤 0、失敗請求 0、頁面沒有水平捲動；版面依截圖修正（卡片標題放型號與 VRAM，狀態放在第二行開頭，
  避免窄欄截掉「外部使用中」）。
- 另把 API 回應裡所有 GPU 欄位移除，模擬舊版 API：沒有「GPU 卡」選項、叢集列與圖例都沒有 GPU 項目。
- `uv run pytest -m 'not docker and not vm'`：69 passed（新增 Dashboard GPU 標記與 API 欄位的檢查）。
- 這些是以模擬 Worker 驗證畫面；實際 Worker 回報的 GPU 資料要到步驟 3、部署後再確認。

### 7.4、7.5、7.8 實機部署與 GPU 工作（2026-10-27）

部署前兩台的卡都閒置、沒有 LabFlux 容器，kayle 的 `labflux-firewall.service` 為 `active`。
以 commit `3db06f0` 執行 `uv run python scripts/lab_deploy.py deploy`，結束狀態 0；
兩台的 `labflux` 帳號都以 systemd user unit 執行 Worker，kayle 另執行 Scheduler（`192.0.2.10:8770`）。
`lab_deploy.py status` 顯示兩台 ONLINE，Worker 回報的卡與 7.1 的檢查一致。

防火牆實測：在 `192.0.2.10:8770` 暫時開一個只提供空目錄的 HTTP 伺服器（最多 60 秒），測完關閉。

| 來源 | 結果 |
|---|---|
| kayle 本機（經 `lo`） | HTTP 200 |
| karma（192.0.2.20） | HTTP 200 |
| kayle 上的 Docker 容器（經 `docker0`） | 連線逾時，被 DROP |
| 同一個容器連 kayle 的 22 port | 可連線，規則只影響 8770 |

三個 MNIST 工作以 `gpu_model` 指定型號同時提交（`examples/jobs/mnist.json` 加上 `gpu_model`），
經 CLI `submit`、`logs`、`fetch` 完成。兩台的 uv 快取一開始都是空的，所以包含下載 torch 的時間；
kayle 的兩個工作同時寫入同一個快取。

| `gpu_model` | 節點與卡 | 容器執行時間 | 三輪測試準確率 |
|---|---|---:|---|
| `3090` | kayle、RTX 3090 | 278 s | 98.23% → 98.80% → 99.04% |
| `A5000` | kayle、RTX A5000 | 308 s | 98.27% → 98.68% → 98.98% |
| `4090` | karma、RTX 4090 | 152 s | 98.32% → 98.65% → 98.98% |

- 日誌的 `Device:` 與分到的卡相符；`fetch` 取回 `mnist_cnn.pt` 與 `metrics.json`。
- 結束後兩台的 CPU／RAM／GPU 預約都歸零，沒有 LabFlux 容器；karma 的 attempt workspace 已刪除，uv 快取 5.2 GB。
- **7.4 實際外部使用：** 工作結束後，另一位使用者在 kayle 的 3090 上執行 Python（約 19.5 GB VRAM）。
  Worker 心跳只把 3090 回報為 `external_use = true`，A5000 與 4090 仍為可用。
  提交 `gpu = 1, gpu_model = "3090"` 的探測工作，5 秒後仍為 QUEUED（沒有 attempt），原因為
  `waiting for matching CPU/RAM/GPU capacity or datasets`，隨後取消。LabFlux 沒有碰觸該程式。
- **7.5 最低 VRAM：** `min_vram_mib = 24570` 只有 3090（24576 MiB）符合，但它正被外部使用，工作保持 QUEUED 後取消；
  `min_vram_mib = 24000` 分到 karma 的 4090 並完成。張數與型號匹配由上表的三個工作驗證。
- 7.8 未勾選：checklist 要求至少三個異質節點，目前實體節點只有 kayle 與 karma 兩台。

## 最後驗證與交付

- `uv sync --locked`：通過。
- `uv run ruff check .`、`uv run ruff format --check .`、`git diff --check`：通過。
- `sg docker -c 'uv run pytest'`：**61 passed，0 skipped**；包含 8 個實際 Docker 測試，其餘為 API／排程／Worker／CLI／封存檔等測試。
- 三台 VM smoke：10 checks passed，使用 18 個工作；其中 OOM／超時為預期 FAILED，取消為預期 CANCELLED。
- 環境閒置時，三台 VM 正常關機、Scheduler 程序群組停止，再重新啟動及部署成功；未以此宣稱執行中故障恢復已完成。
- 本次完成 50 個編號 checklist 項目，對應 M0～M5、M6.1～M6.3。
- Git 僅追蹤程式、範例、規劃、測試與去除認證資料的驗收紀錄；私鑰、Token、VM 磁碟與即時資料目錄均被忽略。
