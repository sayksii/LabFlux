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
（兩者已在 `feat/m6-m8-m9` 交付，見下方「M6.5～M6.7」。）

## M7：實體 GPU 節點

本節的 IP 與 GPU UUID 以範例值記錄（IP 使用文件專用的 `192.0.2.0/24`）；
實際值只放在被 Git 忽略的 `configs/local-lab.toml` 與 `.labflux/lab/`。

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
- 7.8 當時未勾選：checklist 要求至少三個異質節點，當時只有 kayle 與 karma 兩台；第三台見下一節。

### 7.8 第三個節點 kindred 與三節點 GPU 工作（2026-11-16）

kindred（`192.0.2.30`）是實驗室另一台共用機器：Ubuntu 22.04、128 執行緒、251 GiB RAM、5 張 NVIDIA A40（46068 MiB，驅動 550.144.03）、
Docker 28.0.4、NVIDIA Container Toolkit 1.13.5、預設 runtime `runc`（沒有註冊 `nvidia` runtime；LabFlux 的 device request 不需要）。
其中 3 張卡屬於其他人，LabFlux 只登記使用者自己的兩張（`gpus = [3, 4]`），上限 CPU 32、RAM 64 GiB。

部署步驟：

- 管理員建立 `labflux` 帳號（UID／GID 2001、docker 群組、部署鑰匙、linger），host key 先在機器內核對指紋再固定到 `.labflux/lab/known_hosts`。
- `scripts/lab_deploy.py` 的防火牆改成專用 chain `LABFLUX-SCHEDULER`（原本只能放行一台遠端 Worker）：連到 8770 的封包跳進這條 chain，
  本機與每台遠端 Worker `RETURN`，其餘 `DROP`；跳轉規則最後加入，chain 建好前沒有封包會進去。管理員在 kayle 換上新 unit 後手動刪除舊的單一 DROP 規則，
  `iptables -S` 確認 INPUT 只剩一條跳轉、chain 依序為 `lo`、karma、kindred 的 `RETURN` 與 `DROP`。換完後 karma 與 kindred 都能連到 8770。
- `tests/test_lab_deploy.py`：18 passed（三台節點產生兩條 `RETURN`、沒有遠端 Worker 時只放行 `lo`、unit 的停止與重新啟動步驟可重複執行）。
- `lab_deploy.py check`：kindred 除了換防火牆前連不到 Scheduler 之外沒有 `FAIL`；`deploy --node kindred` 只部署這一台（當時 main 的 commit `7308162`，與另外兩台相同），kayle 與 karma 沒有重啟。
  kindred 以 `ONLINE` 註冊，回報兩張 A40。

部署時兩張 A40 正由使用者自己的 vLLM 容器使用（各約 41 GB），Worker 心跳把兩張都回報為 `external_use = true`，沒有工作被排上去。
使用者同意後以 `docker compose stop` 停止該容器，兩張卡回報為可用，再同時提交以下工作（CLI `submit`、`status`、`logs`）：

| 工作 | 需求 | 節點與卡 | 結果 |
|---|---|---|---|
| MNIST | `gpu_model = "A40"` | kindred、A40（卡 3） | 99.01%，每輪 10～14 秒 |
| MNIST | `gpu_model = "A40"` | kindred、A40（卡 4） | 99.02%，每輪 12～14 秒 |
| MNIST | `gpu_model = "3090"` | kayle、RTX 3090 | 98.92%，每輪約 4.5 秒 |
| MNIST | `gpu_model = "4090"` | karma、RTX 4090 | 98.98%，每輪約 4.7 秒 |
| 隔離探測 | 1 張 A40、2 CPU | kindred、A40（卡 4） | `torch.cuda.device_count() = 1`，容器內只有 `/dev/nvidia4` |
| CPU 工作 | 20 CPU、1 GiB（只有 kindred 放得下） | kindred | `cpu.max = 2000000 100000`、`memory.max = 1073741824`、沒有 `/dev/nvidia*` |

- 四個 MNIST 工作提交後 1 秒內各自分到符合型號的卡；kindred 的兩個工作同時執行、分到不同的卡。日誌的 `Device:` 與分到的卡相符。
- 隔離探測排在 CPU 工作之前，而 A40 都被佔用，所以兩者依嚴格 FCFS 一起等待，直到第一個 A40 工作結束（683 秒）才同時開始；探測拿到剛釋放的卡 4。
- kindred 的 uv 快取一開始是空的，兩個 A40 工作同時下載 torch 等套件（約 3 GB），容器執行時間分別為 683 與 1243 秒，其中訓練程式本身 115 與 770 秒，
  差距來自各自從網路下載 MNIST。kindred 的網卡回報 100 Mb/s（kayle、karma 為 1GbE），之後的資料集複製與映像下載都會較慢。
- 結束後三台的 CPU／RAM／GPU 預約都歸零，kindred 沒有 LabFlux 容器，uv 快取 11 GB；kindred 的根分割區剩約 290 GB，是之後快取的主要限制。

## PR #2 審查（2026-10-27）

合併前以 code review 檢查整個 PR，共 15 項，另外人工補查到 1 項。判定原則：
在兩台實體機、少數使用者的環境下實際會遇到、且改動小的才修；少見情況記錄為已知限制，不為它擴大設計。

| # | 位置 | 問題 | 判定與處理 |
|---|---|---|---|
| 1 | `scheduler.py` register | 新 instance 帶 inventory 註冊時不檢查舊 instance 是否還活著；同名的第二個 Worker 會關閉它找不到容器的工作並釋放資源 | 少見（需取得 Worker Token 又用同名啟動）；不修，列為操作限制 |
| 2 | `models.py` env 驗證 | 舊版資料庫中含 `NVIDIA_VISIBLE_DEVICES` 的排隊工作會讓每次排程驗證失敗 | 少見（正式資料庫由新版建立，提交時即被拒絕）；不修 |
| 3 | `worker.py` 拒絕指令 | 拒絕指令後、結果送出前剛好重啟，工作停在 RUNNING | 少見（Scheduler 不會送出會被拒的指令，時間窗約 0.25 秒）；不修 |
| 4 | `worker.py` 接手 | 接手時本機紀錄損壞或卡片已不在清單，工作停在 RUNNING、容器無人監看 | 少見；不修，列為操作限制（GPU 工作執行時不改 `gpus`） |
| 5 | `scheduler.py` STARTED | 節點失聯期間容器啟動，STARTED 被忽略，整段執行都顯示 STARTING | **已修** |
| 6 | `scheduler.py` 對帳 | 重啟超過 10 秒且 START 尚未送達時，工作整段執行都顯示 UNKNOWN | **已修**（與 #5 同一處） |
| 7 | `scheduler.py` GPU 衝突 | 已失聯的舊節點仍佔著 GPU UUID，Worker 改名後無法註冊 | 少見；不修，列為操作限制 |
| 8 | `cli.py` `--wait` | 把 UNKNOWN 當成結束，Worker 重啟時誤報失敗 | **已修** |
| 9 | `lab_deploy.py` | 第二次重啟檢查未傳 Token，部署者不在 `scheduler.users` 時重新部署 kayle 會被擋 | 目前部署者在名單內；不修，列為操作限制 |
| 10 | `lab_deploy.py` | 重啟後只檢查 ONLINE，新 Worker 啟動失敗時仍可能顯示成功 | 少見且 `status` 可看出實際狀態；不修 |
| 11 | `runtime.py` uv 快取 | 同一台 Worker 的工作共用可寫的 uv 快取 | 既定設計（同台使用者互相信任）；文件已說明 |
| 12 | 註解與文件 | 仍寫「SIGTERM 會停止容器」，與 M6.4 後的行為相反 | **已修**（CLI 說明、unit 註解、README、`docs/lab-deploy.md`） |
| 13 | `scheduler.py` | STARTED 只處理 STARTING；失聯判斷依賴原因字串 | STARTED 部分隨 #5 修正；原因字串比對維持不變 |
| 14 | 多處 | attempt ID、GPU UUID 格式重複定義 | 程式風格；不修 |
| 15 | `scheduler.py`、`worker.py` | 狀態選擇與指令驗證重複 | 程式風格；不修 |
| 16 | `docs/lab-deploy.md` | MNIST 範例用 `--wait` 卻沒加 `--wait-timeout`，第一次執行必定顯示逾時 | **已修**（人工補查） |

修正內容與驗證：

- STARTED 也會把因心跳中斷而變成 UNKNOWN 的工作改回 RUNNING（已要求取消則為 STOPPING），並補記開始時間。
  `tests/test_reconnect.py` 新增 3 個案例，分別重現 #5、#6 與取消後的情況；改回原本的程式時這 3 個案例都失敗。
- `submit --wait` 遇到 UNKNOWN 會提示並繼續等待；`tests/test_cli.py` 的新案例在原本的程式上失敗。
- `uv run pytest -m 'not docker and not vm'`：136 passed。
- `uv run pytest -m docker`（kayle，含 3 個 GPU 與 3 個重啟接手測試）：16 passed；測試前兩張卡閒置，測試後沒有殘留容器。
- `ruff check`、`ruff format --check`、`git diff --check`：通過。

## M6.5～M6.7：重跑、Scheduler 重啟與故障驗證（kayle，2026-11-15）

以下各節都在 `feat/m6-m8-m9` 分支、kayle 上執行。Docker 測試只用 CPU 容器，沒有使用 GPU、VM 或實驗室部署。

**6.5 重跑。** 工作的 `max_retries`（預設 0、上限 10）決定執行遺失時最多重跑幾次。只有 Worker 註冊核對時
確認容器已不存在（`_reconcile` 關閉的 attempt）才算遺失：舊 attempt 記為 `FAILED`，工作建立新 attempt，
以原本的 `submission_seq` 重新排隊。程式自己退出、超時、OOM 與已要求取消的工作不重跑。
Worker 以開機時間判斷容器是否因主機關機或當機而停止，這類容器不列入清單，視為已不存在。

| 案例 | 驗證（`tests/test_fault.py`） |
|---|---|
| `max_retries=2` 共 3 個 attempt 後 `FAILED`；預設 0 不重跑 | `test_lost_execution_gets_new_attempts_until_its_retries_run_out`、`test_job_without_retries_fails_on_a_lost_execution` |
| 重跑的工作排在之後提交的工作前面 | `test_retried_job_keeps_its_place_ahead_of_later_submissions` |
| `SUCCEEDED`／exit 0、`FAILED`／exit 3、超時、OOM 都不重跑 | `test_program_exits_and_time_limits_are_never_retried`（逐一參數化） |
| 已取消的工作不重跑；等待重跑時取消不需要 STOP | `test_cancelled_job_whose_container_is_lost_is_not_retried`、`test_cancel_while_queued_for_retry_finishes_without_a_stop` |
| 節點連同容器消失、Worker 回來後重跑 | `test_node_lost_with_its_containers_retries_on_the_returning_worker` |
| 只有被主機重啟停止的容器從清單移除 | `test_only_containers_stopped_by_a_host_restart_are_dropped_from_the_inventory` |

**6.6 Scheduler 重啟。** Scheduler 以相同 `data_dir` 啟動時呼叫 `restore()`：節點標為 `RECONCILING`，
工作與配置保留，不派送新工作；收到原 instance 的心跳（instance_id 相符）或新 instance 帶清單的註冊後才回到 `ONLINE`。

- `test_scheduler_restart_keeps_work_and_dispatches_only_after_the_worker_returns`：停機 3600 秒後，tick 讓節點維持
  `RECONCILING`，新工作 `QUEUED`、輪詢拿不到命令；一次心跳後節點 `ONLINE`，工作派送。
- `test_worker_absent_after_scheduler_restart_expires_after_the_normal_window`：逾時前 1 秒仍為 `RECONCILING`，
  逾時後 1 秒變成 `OFFLINE`，工作 `UNKNOWN`、配置保留。
- `test_worker_restarted_during_scheduler_outage_reconciles_by_registering`：新 instance 的清單接手執行中的 attempt，舊 instance 的心跳得到 409。
- `test_outbox_results_during_reconciliation_release_without_dispatching`。

**6.7 故障情境。**

| 情境 | 驗證 |
|---|---|
| 停止並重啟 Worker | M6.4 的 `tests/test_reconnect_docker.py`（3 個）與 `tests/test_reconnect.py` 維持通過 |
| 機器停機（模擬） | Docker：`test_machine_down_with_its_containers_retries_on_the_returning_worker`。停止 Worker、強制刪除工作容器，節點 `OFFLINE`（逾時 1 秒）、工作 `UNKNOWN`；同一資料目錄的新 Worker 註冊後第一個 attempt `FAILED`（`container missing after worker restart`），第二個 attempt 執行。最後 `SUCCEEDED`、attempt 為 [FAILED, SUCCEEDED]、日誌 `up`／`done`、配置釋放、沒有殘留容器。反向檢查：拿掉 worker.py 的修正時此測試失敗 |
| 網路中斷超過失聯逾時 | Docker：`test_partition_longer_than_the_offline_timeout_loses_no_work`。在 httpx transport 層拒絕 Worker 的請求，節點 `OFFLINE`、工作 `UNKNOWN`、配置保留，逾時後超過 1 秒容器仍在執行；恢復後 `SUCCEEDED`、單一 attempt、exit 0，日誌 `line 0`～`line 24` 各一次。單元測試 `test_network_partition_keeps_the_outbox_and_settles_once_reconnected`：中斷期間工作結束，結果留在本機（`result_sent` 為 False），恢復後才 `SUCCEEDED`，日誌只送一次，等待的工作接著派送，容器只執行一次 |
| Scheduler 重啟 | Docker：`test_scheduler_restart_with_the_same_data_keeps_a_running_job`。uvicorn 停止 2 秒（失聯逾時的兩倍）後以相同 port 與資料目錄重啟；容器持續執行，節點回到 `ONLINE`，工作 `RUNNING` 且 attempt 原因仍為 `dispatched`（沒有被判為逾時），最後 `SUCCEEDED`、單一 attempt，40 行日誌（含停機期間印出的）各一次；重啟後提交的工作也完成 |
| 延遲、重複與亂序事件 | `test_stale_attempt_events_after_a_retry_are_ignored`：重跑後舊 instance 的 FINISHED 得到 409；新 instance 送來舊 attempt 的 FINISHED／STARTED 回傳 `ignored`，舊 attempt 的日誌不改變狀態；新 attempt 的 FINISHED 先於 STARTED 也能結束，重複 FINISHED 回傳 `duplicate`，同 ID 不同內容得到 409。最後 `SUCCEEDED`、attempt 為 [FAILED, SUCCEEDED]、CPU 釋放 |

- `tests/test_fault.py` 17 個、`tests/test_fault_docker.py` 3 個（單獨執行約 19.8 秒，沒有殘留容器）。
- **6.7 未勾選：** checklist 要求「關閉 VM」，本次沒有使用 VM，只以停止 Worker 加上刪除容器模擬；
  依本文件開頭的原則，不以模擬代替勾選。其餘四個情境都已驗證。
- 未驗證：實際 VM 或主機斷電（state.json 寫到一半、Docker daemon 消失）、Docker daemon 或主機重啟後仍保留的已停止容器、
  緩慢或逾時的網路連線（模擬的中斷會立即得到 ConnectError）、半開的 TCP 連線。
  Docker 重啟測試中，重啟後提交的工作可能在節點已 `ONLINE` 後才送出，「核對完成前不派送」只由單元測試證明。

## M8：優先權、公平性與 backfilling（kayle，2026-11-15）

`src/labflux/policy.py` 把排序與放置抽成可替換的策略，Scheduler TOML 的 `scheduling_policy` 選擇 `fcfs`（預設）、
`priority` 或 `backfill`。設定：`default_priority`（5）、`score_priority_weight`（100）、`score_wait_weight`（1）、
`score_usage_weight`（1）、`usage_window_s`（3600）、`job_time_limit_s`（3600）。工作欄位：`priority`（0–9）、
`estimated_runtime_s`（不得超過時限，只用於說明）。近期用量是使用者在時間窗內預約的分鐘數，乘上 CPU／RAM／GPU
中占線上節點總容量比例最高的一項（dominant share）。每個排隊工作的決策寫入 `schedule_note`（migration 3）。

**驗收：同一組輸入、三種模式。** `test_same_workload_under_three_policies_is_explained`：節點 5 CPU，A（3 CPU、3600 秒）
執行中，再提交 B（bob、5 CPU）、C（alice、1 CPU、600 秒）、D（bob、1 CPU、7200 秒、優先權 9）。

| 模式 | 啟動的工作 | 說明（`schedule_note`） |
|---|---|---|
| `fcfs` | A | D：`fcfs: submission #4; waiting behind job <B>` |
| `priority` | A、D | D：`priority: started on kayle (score 900 ...` |
| `backfill` | A、C、D | B：`reserved on kayle at t=8200`（A 在 4600、D 在 8200 結束）；C：`backfilled ... its time limit ends before the reservation` |

| Checklist | 驗證（`tests/test_policy.py`，13 個） |
|---|---|
| 8.1 | 三種模式共用一組測試；策略名稱錯誤時 Settings 拒絕 |
| 8.2、8.5 | 優先權範圍 0–9、預設值取自 Settings、估計時間不得超過時限；預設批次時限 3600 秒 |
| 8.3、8.4 | 插隊只發生在 `priority`；優先權 4 排隊 101 分鐘得 501 分，勝過剛提交的優先權 5；alice 用滿整個叢集 CPU 60 分鐘後得 500 − 60 = 440，bob 先跑，半個時間窗後扣 30、一個時間窗後扣 0 |
| 8.6 | 根據既有配置與最晚釋放時間計算保留時段；`earliest_start` 與 `dominant_share` 的純函式測試 |
| 8.7 | 隊首保留在 t=1600：2 CPU、時限 3600 秒（估計 60 秒）的工作即使有 2 CPU 閒置也被拒絕，300 秒的工作 backfill；隊首的 attempt 剛好在 t=1600 建立。8 CPU 節點上，留下隊首所需資源的工作可以先跑、下一個被拒，另一個工作被放到其他節點。隊首需要 RTX 3090 時保留在 t=4600（A5000 在 t=1600 就空出），不指定型號的 GPU 工作可以 backfill 到 A5000（假的 GPU 資料，沒有使用實體 GPU） |
| 8.8 | 以上插隊、等待加分、用量扣分與 backfill 不延遲保留工作的案例；沒有可保留節點時後續工作照常啟動 |

- 與其他功能的整合（`tests/test_stream_interplay.py`，6 個）：三種模式下，因執行遺失而重排的工作都會在回來的 Worker 上
  重新派送，`schedule_note` 來自目前的策略，門鈴對該節點只響一次；`fcfs` 讓重跑的工作維持原本順序，`priority` 讓之後提交的
  優先權 9 工作勝過優先權 1 的重跑工作；backfill 的派送也會響門鈴。
- 只有單元測試；沒有在實體節點或 Docker 上以三種模式執行。正式部署的預設仍是 `fcfs`；
  當時 `scripts/lab_deploy.py` 與 `configs/lab.example.toml` 尚未提供這個設定，M11 起可以用節點清單 `[scheduler]` 的
  `policy` 設定（見 `docs/lab-deploy.md`）。比較實驗屬於 M13。

## 門鈴長連線與 outbox 喚醒（T4、T8；M9.1～9.4，kayle，2026-11-15）

- Worker 連到 `/internal/workers/{node_id}/ws?instance_id=...`（Worker Token；拒絕時以 close code 1008 關閉），
  斷線後以最多 5 秒的退避重連，連上後立即補輪詢一次。Scheduler 在寫入命令的交易 commit 之後才通知
  （`Database.after_commit`），通知內容只有「有新命令」，Worker 照舊以 HTTP 領取並 ack。
- 通知點：派送的 START（三種策略與重跑都經過 `Scheduler._dispatch`）、取消的 STOP、對帳時重新交付的命令，
  以及節點從 `OFFLINE`／`RECONCILING` 回到 `ONLINE` 的心跳。
- `poll_interval_s` 預設改為 5 秒，作為命令輪詢與 outbox 上傳的備援間隔；心跳維持每 2 秒、10 秒逾時。
  outbox 有新事件或日誌時由程序內的 Event 喚醒，順序仍是 `STARTED` → 日誌 → 輸出 → `FINISHED`。
- 新增依賴 `websockets`（`>=15,<18`，鎖定 17.4）。

| 案例 | 驗證（`tests/test_doorbell.py`，13 個） |
|---|---|
| 認證與 instance 檢查 | `test_worker_socket_requires_the_worker_token`、`test_worker_socket_accepts_only_the_current_instance` |
| commit 後才通知，rollback 不通知 | `test_doorbell_rings_after_start_and_stop_commands_commit`、`test_after_commit_callbacks_run_once_after_commit_and_never_after_rollback` |
| 同一節點的新連線取代舊連線；雙向訊息 | `test_a_newer_connection_replaces_the_older_one_for_the_same_node`、`test_hub_relays_messages_both_ways_by_type_and_node_scoped_id` |
| 失聯後恢復、重啟後重新交付都會通知 | `test_heartbeat_after_expiry_rings_for_the_withheld_start`、`test_restart_rings_for_a_redelivered_start` |
| 通知後立即領取（備援間隔設為 30 秒仍在 2 秒內） | `test_a_ring_makes_the_worker_poll_long_before_its_fallback` |
| 通知遺失：重連後補輪詢領到命令 | `test_worker_catches_up_on_missed_rings_after_reconnecting` |
| 長連線不通：備援輪詢仍能完成工作 | `test_fallback_poll_delivers_commands_while_the_socket_is_down` |
| outbox 喚醒與 Scheduler 無法連線時的退避 | `test_outbox_writes_wake_the_flush_thread`、`test_outbox_backs_off_while_the_scheduler_is_unreachable` |

Scheduler 重啟時的重連與不重複執行，由 M6.7 的 Docker 測試 `test_scheduler_restart_with_the_same_data_keeps_a_running_job`
在合併門鈴之後涵蓋（單一 attempt、日誌各一次、重啟後提交的工作完成）。命令 ID 與 ack 的去重沿用 M2 的測試。

**量測。** `uv run python scripts/measure_doorbell.py`：在 loopback 啟動 Scheduler，同一程序內執行一個使用假執行器的 Worker
（不用 Docker、GPU），全部使用預設值。閒置請求數以 ASGI 層計算 3 秒暖機後 20 秒內的 HTTP 請求；延遲是 15 輪
「隨機等待 0.3～1.3 秒 → 提交（或取消）→ Worker 的 `accept()` 收到 START（或 STOP）」的時間，包含使用者的 HTTP 請求、
通知與 Worker 的 HTTP 領取。「改版前」是以 `git archive` 取出合併前的 `feat/m6-m8-m9` 原始碼放在 PYTHONPATH 最前面。

| | 改版前（每 0.25 秒輪詢） | transport 分支（門鈴，5 秒備援） | 整合後（本分支） |
|---|---|---|---|
| Worker 閒置時 HTTP 請求數 | 4.5 次／秒（20 秒內 80 次輪詢 + 10 次心跳） | 0.7 次／秒（4 次備援輪詢 + 10 次心跳） | 0.7 次／秒（4 + 10） |
| 提交 → Worker 收到 START：平均／中位數／p90／最大 | 117.3／116.1／222.2／248.3 ms | 16.0／15.4／19.1／32.1 ms | 18.5／18.6／20.0／28.6 ms |
| 取消 → Worker 收到 STOP：平均／中位數／p90／最大 | 125.2／139.1／184.6／215.7 ms | 16.4／17.2／19.4／36.0 ms | 17.3／18.0／19.3／20.8 ms |

- 閒置時每個 Worker 的 HTTP 請求約少 6.4 倍；每次命令輪詢都要取得全域資料庫鎖，閒置時的鎖取得從每秒 4 次降到 0.2 次。
  心跳次數依設計不變。
- 長連線中斷時，預期平均送達延遲約 2.5 秒（5 秒備援的一半）；只以功能測試驗證，沒有量測。

## M9：快速測試與可重用 session（kayle，2026-11-15）

本節是 2026-11-15 的紀錄。2026-11-16 的審查修正改變了其中幾項行為（取消只停止指令、跳過檔案的回報方式、忙碌時等待、
session 的 PATH），以「程式審查修正」一節為準。

**實作。** session 是 `jobs.kind = SESSION` 的工作列（migration 4），與批次工作共用資源帳本與排程，排在批次工作之前；
START 啟動執行 `sleep infinity` 的容器，每個指令以 `docker exec` 執行。指令經 CLI 的 WebSocket
（`/api/sessions/{id}/ws`，Token 只放在 Authorization header）送到 Scheduler，Scheduler 只在記憶體中經 Worker 的門鈴長連線轉送；
輸出、提示、寫回與結果照原樣送回，不寫入資料庫。STARTED 設定 lease 為 600 秒，指令開始與結束時延長。
新設定：`fast_lane_mode`、`session_lease_s`、`session_command_timeout_s`（60）、`session_sync_timeout_s`（900）、
`session_writeback_bytes`（100 MiB）。

| Checklist | 驗證 |
|---|---|
| 9.5 | `tests/test_sessions.py`，三種策略參數化：之後開的 session 比優先權 9 的批次工作先啟動；`fcfs` 下排在等待中 session 後的批次工作顯示 `waiting behind job <session>`；`backfill` 下等待的 session 得到保留時段，會延後它的 3600 秒批次工作維持 `QUEUED`（`would delay job`） |
| 9.6、9.15 | `fast_lane_mode = "oneshot"` 時每個指令結束就把 session 轉為 `STOPPING`（原因 `one-shot mode`），下一個指令租用新容器：`test_oneshot_mode_closes_the_session_after_each_command`。可重用模式已通過重用、超時與清理的 Docker 驗收，因此預設為 `session` |
| 9.7 | `tests/test_session_relay.py`（11 個）：exec 與快照分段帶著指令 ID、attempt、正規化的 cwd 與時限送到假的 Worker socket；輸出、Notice、寫回與結果依序原樣送回，以 1000 關閉並延長 lease；錯誤碼 `not_ready`、`worker_unavailable`、`not_found`、`too_large`、`invalid`、`busy`；URL 帶 Token 以 1008 拒絕；Worker 斷線得到 `worker_disconnected`、逾時得到 `lost`，兩者都讓 session `STOPPING` |
| 9.8 | lease 由 STARTED 設定、指令開始與結束延長；lease 到期時忙碌的 session 不停止，閒置的轉為 `STOPPING`、送出 STOP、響門鈴，確認終止後才釋放 CPU；每位使用者一個；`UNKNOWN` 的 session 保留資源，新開的 session 會停止它；其他使用者得到 404 |
| 9.9 | `tests/test_session_worker.py`（20 個）：快照覆蓋、刪除並清掉空目錄，保留產生的檔案與 `.venv`；指向工作目錄外的 symlink 被取代且不寫出或刪除外部檔案；不安全的封存在動到工作目錄前被拒絕；有 `uv.lock` 時第一次與 `uv.lock` 改變時執行 `uv sync --locked`、沒有 `uv.lock` 時執行 `uv sync`、依賴未變不執行，失敗則跳過指令、下次重試 |
| 9.10 | 寫回只含新增與修改的檔案（不含 `.venv`、`__pycache__`、symlink），sha 相符；超過上限的檔案跳過並列出；輸出上限只有一次截斷提示；客戶端的 `.remote` 衝突處理與不安全封存檢查見 `tests/test_fastlane.py`。session 結束時沿用一般 attempt 的 workspace 刪除流程，沒有針對 session 另外測試 |
| 9.11、9.12 | `tests/test_fastlane.py`（27 個）：專案根目錄搜尋、`.labflux.toml` 解析與命令列優先、T7 路由清單與只有 `uv run` 送到遠端、參數中的專案路徑改寫；在真實子程序中執行替身與 `labflux local`（`-c`、`-q`、`--`、含空白的參數照原樣傳入，交給真正指令的 PATH 移除所有替身目錄）；`labflux shell` 結束時刪除替身目錄 |
| 9.13 | 指令超時、取消、轉送中斷都結束整個 session 並仍送回寫回；超時的 Docker 驗證見下方 |

**實際 Docker（CPU 容器，`python:3.12-slim`）。**

- `tests/test_session_docker.py`（3 個，0.5 CPU／128 MiB）：`time_limit_s=1` 的 session 容器超過 1 秒仍在執行；exec 在程序結束前
  1 秒以上就串流出 `first`，exit code 3；前一個 exec 寫的檔案下一個看得到；1 秒逾時呼叫 `on_abort`、停止容器、回傳 `CANCELLED`
  且確認終止，沒有殘留容器；以 python-uv 映像由 Worker 驅動時只執行一次 `uv sync`（`sys.prefix` 為 `/workspace/.venv`）。
- `tests/test_fast_lane_e2e_docker.py`：真實 uvicorn Scheduler（`127.0.0.1:0`、`session_command_timeout_s=3`）、真實 Worker
  （DockerExecutor、1 CPU、512 MiB、沒有 GPU），以子程序執行真正的 `labflux` CLI。專案的 `.labflux.toml` 設定
  cpu 1、memory 256 MiB、image `python:3.12-slim`。第一個測試依序確認：
  - 送到遠端的 `python` 串流出 `hi` 並以 exit code 3 結束；遠端寫的 `gen.txt`、`results.json` 寫回本機（9.10）。
  - 第二個指令使用相同的 session ID、attempt 與容器：第一個指令寫的 `/tmp/marker` 還在，且只有一個容器（9.8、9.14 的跨指令保留）。
  - 本機刪除的 `old.py` 下一個指令在遠端也不存在（9.9）。
  - 指令執行期間本機修改的 `results.json` 保留，遠端版本存成 `results.json.remote` 並印出提示（9.10）。
  - 專案內的 `_standin python` 在遠端執行（cwd 為 `/workspace`），專案外以及 `labflux local` 在本機執行（9.11、9.12）。
  - 第一行輸出後送 SIGINT：exit 130，session 結束，節點的工作數、CPU、RAM 預約歸零，沒有殘留容器。
  - 30 秒的指令在 3 秒時限下於 25 秒內以 124 結束並印出 `command time limit exceeded`；資源釋放、容器刪除，之後沒有 session（9.13）。
- **每個指令的額外延遲**（第二個測試，session 已在執行、專案約 1.51 MB）：遠端 `labflux run -- python -c pass` 10 次的中位數 0.33 秒、
  最大 0.36 秒；本機 `python -c pass` 中位數 0.007 秒。三次獨立執行的額外延遲都是 0.32～0.33 秒，其中約 0.13 秒是 CLI 啟動
  （`labflux version` 中位數 0.126 秒）。測試的判定門檻為 2 秒。
- 本機替身執行（專案外的 `python -c pass`）的額外成本約 0.15 秒，來自載入 typer、httpx、pydantic。

**刻意與規格不同的做法。**

- `workspace.py` 以 `O_NOFOLLOW` 的目錄 file descriptor 逐層走訪路徑，比規格要求的 lstat 檢查更嚴格，防止 session 中仍在執行的背景程序
  製造 symlink 競爭。
- 執行器的 exec 多一個 `on_abort` 參數；轉送在把 session 從忙碌集合移除前先呼叫 `end_command`。
- 替身腳本以 `python -I` 啟動；`route()` 讓不在清單上的名稱留在本機；`not_ready` 時重新租用一次；
  寫回衝突判斷的起點提早 50 ms（`MTIME_SLACK_NS`，因為檔案時間戳來自較粗的核心時鐘，實測寫入時間可能早於剛取得的 `time_ns()`）；
  輸出管線中斷時以 141 結束；`labflux shell` 無法啟動 shell 時以 127 結束；CLI 在 `CommandResult.reason` 非空時印出。

**未勾選與未驗證。**

- **9.14：** 已於 2026-11-16 補上 AI coding 工具的實測並勾選，見下方「9.14 AI coding 工具實測」。
- `oneshot` 模式只有 Scheduler 端的單元測試，沒有以 Docker 端到端執行；它使用的關閉與重新租用路徑已在 Docker 驗證。
- Scheduler 重啟時進行中的互動指令：CLI 在連線關閉時回報錯誤並以 125 結束（依程式碼），沒有專門的測試。
- GPU session、第二台實體機與實驗室部署上的 session 都沒有執行。
- 手動檢查：專案中的替身指向關閉的 port（127.0.0.1:9）時回報 `Connection refused` 並以 125 結束；專案外且本機沒有 `python` 時印出
  `python: command not found` 並以 127 結束。

## feat/m6-m8-m9 整合（kayle，2026-11-15）

- 合併順序：`wip/docs-plan`、`wip/fault`、`wip/policy`、`wip/transport`，再合併 `wip/m9-server`、`wip/m9-client`。
  migration 編號為 2（GPU）、3（`schedule_note`）、4（session）；`Database.upgrade` 依序套用 `current + 1` 到 `SCHEMA_VERSION`。
- `uv run pytest -q -m "not docker and not vm and not gpu"`：276 passed、24 deselected，約 36 秒（合併前 136）。
- `uv run pytest -q -m "docker and not gpu and not vm"`：21 passed、沒有跳過，約 66 秒；`tests/test_fast_lane_e2e_docker.py`
  另外單獨連續執行 3 次都通過（每次約 15 秒）。測試後沒有殘留的測試容器。
- `uv run ruff check src tests`、`ruff format --check`：通過；`uv lock --check`：通過。沒有執行 GPU 或 VM 測試。

## feat/m6-m8-m9 程式審查修正（kayle，2026-11-16）

對 `feat/m6-m8-m9` 做了一輪程式審查，與使用者逐項確認後只修實際會遇到、而且修起來便宜的問題；其餘記為已知限制（見下一節）。
修正分三個分支（`wip/fix-sched`、`wip/fix-worker`、`wip/fix-client`）完成後合併，再對齊兩端的行為。
每個修正都有一個「還原修正就會失敗」的測試；括號內註明例外。設計決定見 `docs/architecture.md` 的 T9～T13。

**Scheduler 與轉送**

| 問題 | 修正 | 驗證 |
|---|---|---|
| backfill 以「開始時間 + 一個 lease」估計 session，延長過的 session 被當成已經釋放 | 已啟動的 session 以目前的 `lease_expires_at` 計入；尚未啟動的維持原估計 | `test_sessions.py::test_backfill_books_a_session_until_its_renewed_lease` |
| 隊首有保留時段時，後續工作只試小卡優先挑出的卡，和隊首重疊就被拒絕 | 有保留時段時，候選卡改成「隊首用不到的卡」優先 | `test_policy.py::test_backfill_prefers_a_card_the_reserved_job_cannot_use`（假的 GPU 資料） |
| CLI 在等待 session 時被強制結束，排隊中的 session 永遠佔著隊首 | 排隊中的 session 超過 `session_queue_timeout_s`（新設定，預設 30 秒）沒有被擁有者查詢就取消；時間記在記憶體 | `test_sessions.py::test_a_queued_session_is_cancelled_once_its_client_stops_polling`、`::test_a_queued_session_gets_a_full_window_after_a_scheduler_restart` |
| 轉送先標記忙碌才檢查擁有者，其他使用者能短暫把別人的 session 標成忙碌 | 擁有者與狀態檢查（`begin_command`）移到忙碌檢查之前；不是自己的 session 一律回 `not_found` | `test_session_relay.py::test_another_user_cannot_mark_a_session_busy`；`::test_a_command_rejected_as_busy_does_not_end_the_running_one` 是防止退化的測試，修正前後都通過 |
| `oneshot` 模式下 Worker 的結果寫 `session_ended = false`，CLI 會把即將刪除的檔案列進清單 | `oneshot` 模式下轉送把每個結果標成 `session_ended` | `test_session_relay.py::test_one_shot_mode_tells_the_client_that_the_session_ended` |
| `test_fault.py` 的 RECONCILING 斷言永遠成立；8.3 的 GPU 正規化沒有測試 | 只改測試：tick 之後檢查工作仍是 `QUEUED`、沒有 attempt、節點帳本不變；新增 GPU 占比的用量測試 | 暫時改壞程式（讓 `RECONCILING` 節點也派送、從 `dominant_share` 拿掉 GPU）確認新測試會失敗，再還原 |

**Worker 與執行器**

| 問題 | 修正 | 驗證 |
|---|---|---|
| session 容器的 PATH 寫死，自訂映像的直譯器（例如 `/opt/conda/bin`）找不到 | PATH = `/workspace/.venv/bin` + 映像自己的 PATH（映像沒設時用 Docker 的預設值） | `test_session_worker.py::test_session_path_puts_the_workspace_venv_before_the_images_own_path`；Docker：`test_session_docker.py::test_a_session_keeps_the_image_path_behind_the_workspace_venv` |
| Ctrl-C 或客戶端斷線會結束整個 session 容器 | 指令透過 `sh -c ... exec "$@"` 啟動並記下 PID；取消時由另一個 exec 對 process group 送 TERM，`stop_grace_s` 後送 KILL，容器保留；停不掉（約 2 × `stop_grace_s` + 5 秒後）才終止容器 | `test_session_worker.py::test_a_cancel_stops_only_the_command_and_the_session_stays_usable`、`::test_a_cancel_during_uv_sync_keeps_the_session_and_syncs_again`、`::test_a_command_that_does_not_finish_ends_the_session_and_keeps_its_output[unstoppable]`；Docker：`test_session_docker.py::test_a_stop_ends_only_the_commands_process_group`（忽略 TERM 的父子程序在寬限後被 KILL、沒有殘留程序、容器仍在執行）、`::test_a_stop_that_cannot_end_the_command_asks_for_the_container_to_end` |
| 門鈴 handler 拋出例外時，指令要等到轉送的保護期限才失敗 | 立刻回覆 `worker error: ...`；指令的執行緒已啟動時由它自己回覆，不會送兩次 | `test_session_worker.py::test_a_failing_message_handler_answers_the_command_at_once` |
| 寫回只看位元組上限，檔案數可能超過封存檔的成員上限而整個寫回被拒絕 | 寫回最多 `max_archive_members` 個檔案，其餘和超過大小的檔案一樣留在遠端並列出 | `test_session_worker.py::test_the_archive_member_limit_also_caps_the_write_back` |
| 跳過的檔案只在當次指令回報一次，而且 Worker 與 CLI 各印一行 | `session.json` 記錄累計的「只在遠端」清單，每個由指令執行緒送出的結果都帶著它；Worker 不再送這個 Notice | `test_session_worker.py::test_remote_only_files_stay_listed_until_written_back_or_deleted`、`::test_the_write_back_cap_skips_and_lists_files` |
| 測試缺口 | 批次 attempt 被拒絕的原因確實是缺少 session 旗標；門鈴測試先證明沒有補輪詢再放行通知；session 結束後工作目錄刪除；HTTP 與門鈴同時中斷後只靠備援輪詢送達 | `test_session_worker.py::test_commands_only_for_this_workers_running_sessions`、`::test_the_session_workspace_is_deleted_when_the_session_ends`；`test_doorbell.py::test_a_ring_makes_the_worker_poll_long_before_its_fallback`、`::test_fallback_poll_delivers_after_a_partition_that_also_cut_the_socket` |

**CLI**

| 問題 | 修正 | 驗證 |
|---|---|---|
| `~/.bashrc` 的 conda／pyenv／venv 把自己的目錄放到 PATH 最前面，替身被蓋掉 | bash 以產生的 `--rcfile` 啟動：先讀使用者的 `~/.bashrc`，再把替身目錄放回最前面，並掛在 `PROMPT_COMMAND` 最後（保留 `$?`）；zsh 以 `ZDOTDIR` 包一層加 `precmd`；啟動時對被 alias 或 function 蓋住的指令印警告 | `test_fastlane.py::test_labflux_shell_keeps_standins_first_after_startup_files[bash-.bashrc]`（在 pseudo-terminal 執行真正的 `labflux shell`）；`test_cli.py::test_cli_shell_puts_standins_first_and_cleans_up`。**zsh 的版本因為這台機器沒有 zsh 而跳過，尚未執行過** |
| session 忙碌時第二個指令立刻以 125 失敗（AI 工具會平行送指令） | 每秒重試、每次重新打包快照，最多等 `[session] busy_wait_s`（預設 120 秒） | `test_fastlane.py::test_run_waits_for_a_busy_session_with_a_fresh_snapshot`、`::test_run_gives_up_on_a_busy_session_after_busy_wait_s` |
| 專案裡有 symbolic link 時每個送到遠端的指令都失敗 | 原始碼快照略過連結（不跟隨）並回報；輸出封存與解壓仍然拒絕連結 | `test_archives.py::test_source_snapshot_skips_symlinks_without_following_them`、`test_fastlane.py::test_run_skips_symlinks_with_one_notice_per_command`、`test_cli.py::test_cli_submit_upload_status_nodes_and_queued_cancel` |
| 留在遠端的檔案只有一行會離開上下文的提示 | `.labflux/remote-files.txt`、清單變動時一行提示、`labflux session files`；新 session、`session_ended` 的結果與 `labflux session close` 會移除清單 | `test_fastlane.py::test_run_mirrors_the_remote_only_list_and_hints_once_per_change`、`::test_remote_only_list_ends_with_the_session`；`test_cli.py::test_cli_session_files_prints_the_list_and_close_removes_it` |
| 訊息仍說 Ctrl-C 會關閉 session | 取消且 session 還在時只印一行 `the command was stopped; the session is still running`（不重複 Worker 的原因）；`the session was closed` 只在結果帶 `session_ended` 時出現 | `test_fastlane.py::test_ctrl_c_sends_cancel_and_keeps_the_result[False]`、`[True]` |
| 不經指令執行緒的錯誤結果不帶清單，CLI 會誤刪 `.labflux/remote-files.txt` | outcome 為 `error` 的結果不更新清單 | `test_run_mirrors_the_remote_only_list_and_hints_once_per_change` 中的錯誤結果段落 |

其他對齊：`SESSION_ENV` 不再帶 PATH；`session_protocol.py` 的註解改成新的取消語意。

**實際 Docker（CPU 容器，沒有 GPU）。** `tests/test_fast_lane_e2e_docker.py`（真實 Scheduler、Worker 與 `labflux` CLI，
`session_command_timeout_s = 3`、`session_writeback_bytes = 1 MiB`）新增與修改的段落：

- 指令先寫出 `partial.txt` 再睡 30 秒，印出第一行後送 SIGINT：exit 130，訊息是 `the command was stopped; the session is still running`，
  `partial.txt` 已寫回本機；下一個指令在同一個 session、attempt 與容器執行，讀得到第一個指令留下的 `/tmp/marker`、`gen.txt` 與 `partial.txt`。
- 3 秒時限仍以 124 結束並終止容器、釋放資源，之後的指令租用新的 session（沒有改變）。
- 新測試 `test_fast_lane_remote_only_files_and_concurrent_commands`：指令寫出 2 MiB 的 `checkpoints/model.bin`、小檔 `small.txt`
  與 `/outputs/keep.txt`。小檔寫回；`checkpoints/` 沒有出現在本機；stderr 只有一行提到這個檔案
  （`1 file (2.0 MB) exists only in the remote session`，並指向 `.labflux/remote-files.txt` 與 `/outputs`）；清單只有這一個檔案。
  下一個替身指令在同一路徑讀到 2097152 位元組，清單沒變所以沒有提示；`labflux session files` 印出同一份清單。
- 同一個測試：同時啟動兩個各睡 1.5 秒的指令，兩個都以 0 結束並印出結果，其中恰好一個印出等待訊息；session、attempt 與容器不變。
- 同一個測試：`labflux session close` 之後本機清單被移除、資源釋放、容器刪除；`labflux fetch <session 的工作 ID>` 取回的輸出裡有 `keep.txt`
  （session 的 `/outputs` 沿用批次工作的上傳流程，這是第一次對 session 驗證）。

**執行結果。**

- `uv run pytest -q -m "not docker and not vm and not gpu"`：301 passed、1 skipped（zsh 版本的 shell 測試，沒有 zsh）、約 45 秒（修正前 276）。
- `uv run pytest -q -m "docker and not gpu and not vm"`：25 passed、沒有跳過，約 85 秒（修正前 21）；
  完整執行之後只再改過 `tests/test_fast_lane_e2e_docker.py`（加上 `/outputs` 的檢查）與文件，
  這個檔案另外單獨執行 6 次都通過（每次約 23 秒）。測試後沒有殘留的測試容器。
- `uv run ruff check src tests`、`ruff format --check`：通過。沒有執行 GPU 或 VM 測試，也沒有在實驗室部署上執行。
- 手動檢查（Worker 分支）：指令與送訊號的 `sh` 包裝在 dash（`python:3.12-slim`、ubuntu）、bash 與 busybox sh 都可用；
  `kill -TERM -PID` 是三者都接受的寫法。

## feat/m6-m8-m9 已知限制

判定原則與 PR #2 相同：少見情況記錄為限制，不為它擴大設計。

**故障與重跑（M6.5～M6.7）**

- 沒有「宣告節點永久停機」的操作路徑，也沒有管理者角色；永遠不回來的節點保留它的 `UNKNOWN` attempt 與資源。
  替代做法：確認機器已停機後，以相同 `worker_id` 和空的資料目錄啟動 Worker，註冊時找不到紀錄與容器，attempt 就會關閉並依設定重跑。
- 只有容器確實不存在才算遺失。Docker daemon 或主機重啟後若已停止的容器仍保留，新 Worker 接手並把它的 exit code（常見 137 或 255）
  當作一般 `FAILED`，不重跑。開機時間檢查只認得主機關機、重啟或當機；單獨重啟 Docker daemon 不改變開機時間，同樣不重跑。
- 程式自己以非零 exit code（OOM 以外）結束、而主機在 Worker 記錄結果前停機，會被當成遺失而重跑；exit 0 與 OOM 排除在外，成功的結果不會被丟棄。
- 容器在 Worker 盤點與接手之間消失時，結果經 FINISHED 以 `FAILED`（`container missing after worker restart`）送達，不重跑；重跑只在 `_reconcile` 觸發。
- 準備階段的失敗（例如 Scheduler 剛好重啟導致原始碼下載失敗、Worker 執行中的 Docker API 錯誤）回報為 `FAILED`，不重跑。
- 重跑次數為 attempt 數減 1。M11 起不計 `PREEMPTED` 的 attempt，搶占另有自己的計數（見「M11」一節）。
- 重跑的工作保留原本的 `submission_seq`。嚴格 FCFS 下若已經沒有節點放得下（例如遺失的節點是唯一夠大的），它會擋住隊首直到容量回來。
  M11 起工作原因保留重跑說明（`queued for retry n of m`），等待的原因只寫在 `schedule_note`。重跑不會指定回原節點，通常因為該節點在同一個交易內註冊而落在那裡。
- Worker 只在程序啟動時註冊。Scheduler 以空的或換過的資料目錄重啟時，執行中的 Worker 每次心跳都得到 404，要手動重啟；
  被另一個 instance 接手 `worker_id` 而得到 409 的舊 Worker 也會繼續空轉。
- Scheduler 重啟後，存活的 Worker instance 只以 instance_id 檢查核對，不做完整盤點；這依賴 SQLite 保存所有已 commit 的變更，以及 Worker outbox 保存所有尚未 ack 的資料。
- Docker 測試中的「機器停機」是正常停止 Worker 加上強制刪除容器，「網路中斷」是在 httpx transport 層立即拒絕請求，見 M6.7 的未驗證項目。
  Docker 的網路中斷測試只切斷 HTTP；HTTP 與門鈴長連線同時中斷的情況只在單元層級驗證（`tests/test_doorbell.py`，假的容器）。

**排程策略（M8）**

- 預設 `scheduling_policy = "fcfs"`，正式部署的行為不變；部署腳本與範例設定當時尚未提供這個設定（M11 起可以用節點清單的 `scheduler.policy` 設定）。CLI 沒有 `--priority`，優先權與估計時間寫在工作 JSON。
- `JobSpec` 使用 `extra="forbid"`，舊版 Worker 或 CLI 會拒絕新欄位；Scheduler、Worker 與 CLI 必須一起升級。
- 保留時段以 `(started_at 或 created_at) + time_limit_s` 計算，不含 STARTED 之前的映像下載與原始碼下載時間，所以可能偏早；
  隊首只會等得更久，不會被 backfill 的工作超配，但實際開始時間可能延後。
- 已超過時限但尚未釋放的 attempt（`STOPPING`、停止較慢，或仍 `ONLINE` 節點上的 `UNKNOWN`）視為「現在」釋放；
  `OFFLINE` 或心跳過期節點上的 attempt 不列入保留計算，節點回來後下一輪重新計算。
- 與一般 EASY 相同，保留時段每輪重新計算、不保存，只有第一個被擋住的工作有保留；之後到達的高分工作可以合法地先跑並把保留時段往後推。
- 沒有任何線上節點放得下隊首時（例如需要的 GPU 比單一節點多，或符合的卡正被外部使用或尚未回報）就沒有保留，後續工作自由啟動；外部使用結束後隊首才再受保護。外部使用中的 GPU 完全不列入保留，因為釋放時間未知。
- 隊首有保留時段時，後續工作先拿隊首用不到的卡，即使它比另一張空卡大；只在這種情況下不照小卡優先（2026-11-16 修正後的行為）。
- 近期用量以目前線上節點的容量正規化；離線節點上的 attempt 仍計入，比例上限為 1。用量計算的是從派送開始的預約時間，不是實際使用率。
- `priority`／`backfill` 模式下，每輪有排隊工作的排程都會掃描時間窗內的 attempt（約每 0.25 秒及每次心跳），`finished_at` 沒有索引；
  `fcfs` 模式每輪仍驗證所有排隊工作的 JobSpec 並比較說明文字，佇列很長時都會持有資料庫鎖較久。在課程專案的規模下可接受。
- `schedule_note` 是純文字，M12 要以文字顯示或自行解析；保留時間使用 Scheduler 主機的時區。`priority`／`backfill` 模式下排隊工作的說明約每分鐘變一次（分數與等待分鐘四捨五入），每個工作每分鐘多一次小 UPDATE。
- `priority` 模式下 Dashboard 的排隊位置文字（「第 N 位 · 排在 X 後」）仍假設提交順序；Dashboard 屬於 M12，未修改。
- 重跑的工作保留原本的 `created_at`，等待加分從第一次提交起算。

**門鈴與 outbox（M9.1～9.4）**

- instance 只在 WebSocket 握手時檢查。Worker 重啟而舊連線仍開著時，通知會送到舊 instance，直到新 instance 連上取代它；
  舊 instance 的輪詢得到 409，新 instance 註冊後與連上時各輪詢一次，所以無害。
- 未連線的節點（或 hub 尚未綁定事件迴圈、正在關閉時）的通知直接丟棄；長連線中斷期間要等備援輪詢（最多 5 秒），
  Scheduler 重啟後另加最多 5 秒的重連退避，重連後的補輪詢讓第二段延遲接近重連時間。
- 之後新增寫入或重新交付命令的地方都必須呼叫 `Scheduler._ring(node_id)`；忘了仍然正確，只是最多等 5 秒。
- `Database.after_commit` 假設同一執行緒不會巢狀交易；目前沒有任何路徑巢狀（巢狀會在 `BEGIN IMMEDIATE` 死結）。
- 每條連線的送出佇列沒有上限或背壓；對門鈴訊息沒有問題，快速測試的大量輸出之後若需要應加上流量控制。
- Hub handler 在 Scheduler 的事件迴圈、Worker handler 在門鈴執行緒上執行，阻塞的 handler 會卡住 API 或門鈴，長工作要自行轉交。
- `Worker.send`、`Hub.send` 盡力送出，沒有 ack 或重送（T6 接受 Scheduler 重啟時互動輸出遺失）。
- outbox 任何一次上傳例外後退避（最多 5 秒）。一個永久失敗的 attempt（例如舊 attempt 的 409）會中斷整輪上傳，讓其他 attempt 的事件每次重試延後最多 5 秒；這個根本原因在改版前就存在。
- Worker 關閉約多 1 秒：門鈴的 recv 以 1 秒逾時檢查 `stop_event`，之後還有關閉握手（`close_timeout = request_timeout_s`）。
- WebSocket keepalive ping（uvicorn 與 websockets 預設每 20 秒雙向一次）每分鐘多幾個 frame，不是 HTTP 請求，也不取資料庫鎖。
- 非 ASCII 的 Authorization header 會讓 WebSocket 檢查的 `hmac.compare_digest` 拋出 TypeError，與既有的 HTTP 檢查相同。
- `poll_interval_s` 名稱不變但意義改為備援間隔；手寫設定若明確設成很小的值會維持頻繁輪詢（產生的實驗室設定沒有設定它）。

**快速測試 session（M9.5～9.15）**

- 指令超時、`uv sync` 超時與指令中途 Worker 的轉送連線中斷，都會結束整個 session 容器；下一個指令租用新的 session。
  Ctrl-C 與客戶端斷線只停止指令（T10）。
- 取消的限制：session 映像需要 PATH 上有 POSIX `sh` 與可寫的 `/tmp`（PID 記在 `/tmp/.labflux-command`）。`/tmp` 不可寫時指令仍會執行
  （`sh` 在 stderr 多印一行重導向錯誤），但取消會退回結束整個 session；指令自己改動這個檔案也一樣。
  自行脫離 process group 的程序（`setsid`、daemon）不會被取消，留到 session 結束。找不到程式時由 `sh` 回報，exit code 127。
- `uv sync` 執行中被取消時 session 保留，`.venv` 可能只裝到一半；依賴的 hash 沒有記下，所以下一個指令會重新同步。
- `oneshot` 模式下，Worker 沒有轉送連線（`worker_unavailable`）時 session 也會被關閉：指令還沒送到 Worker，結束指令的流程照樣執行。
  CLI 會重試並租用新的 session。
- Worker 關閉時，每個執行中的快速測試指令最多等 30 秒（`COMMAND_JOIN_S`）讓它送出結果。
- Worker 無聲消失（主機斷電、網路中斷但沒有收到連線關閉）時，轉送要等 WebSocket keepalive 逾時（uvicorn 預設 20 秒 ping 加 20 秒逾時）
  才發現連線已斷，這段時間指令沒有輸出也沒有錯誤；使用者可以按兩次 Ctrl-C 離開。
- lease 不看指令的結果：失敗的指令、被拒絕為忙碌的指令都會延長 lease。不斷失敗重試的迴圈可以一直佔著 session。
- 排隊中的 session 以記憶體中的時間戳判斷客戶端是否還在：Scheduler 重啟後每個排隊中的 session 重新得到完整的 30 秒；
  直接呼叫 API、查詢間隔超過 `session_queue_timeout_s` 的客戶端會失去排隊位置。`labflux session status` 也算一次查詢。
- 不是自己的 session 一律回 `not_found`；忙碌中而且已經不是 `RUNNING` 的 session 回 `not_ready` 而不是 `busy`。
- 指令剛好在 STOP 到達時正常結束，會回報為 `cancelled`（`session closed`）。
- CLI 連上但一直沒送完快照時，轉送等到保護期限（同步 + 指令 + 60 秒，預設約 16 分鐘）才回報 `lost` 並關閉 session。
- `apply_snapshot` 解開後若中途發生 OSError（例如磁碟滿），`session.json` 保留舊的清單，已套用的檔案會被當成產生的檔案，之後的快照不會刪除它們。
- 背景程序在 `pack_files` 讀取時縮小檔案會讓打包失敗，指令得到錯誤結果、沒有寫回；檔案在掃描與打包之間變大可能讓封存略超過 `session_writeback_bytes`，但仍在轉送的上限（再加 16 MiB）內。
- 只在遠端的檔案清單（T11）：結果最多列 `MAX_SKIPPED_LISTED`（100）個並附 `skipped_count`，總大小只算列出的檔案；大小來自最後一次掃描。
  清單上沒被指令改過的檔案不會重試寫回，即使這次放得進上限；背景程序在兩個指令之間的修改偵測不到（與寫回相同）。
  不經指令執行緒的結果（`invalid command`、session 不在這台 Worker、Worker 端的 `busy`、handler 例外）不帶清單，CLI 對 `error` 結果因此不更新清單，
  清單可能晚一個指令才反映變動。
- `.labflux/remote-files.txt` 只和這個專案最後一次執行的指令一樣新：session 因 lease 到期而結束時，清單留到下一個指令才移除；
  同一路徑、同樣大小的檔案再次被跳過時不會再提示；指令本身讓 session 結束時（超時、`oneshot` 模式），超過上限的檔案只回報一次
  `ended with the session without being written back`。在專案目錄外執行 `labflux session close` 不會移除清單。
- 寫回除了位元組上限，每個指令最多 `max_archive_members`（預設 10000）個檔案，其餘留在遠端並列入清單。
- session 的 `/outputs` 在 session 結束時才上傳（上限 `max_output_bytes`），以 session 的工作 ID 用 `labflux fetch` 取回；session 還在時拿不到。
- 忙碌等待：等待中的指令每次重試都重新打包專案，大專案會多花 CPU；多個等待中的指令沒有先後保證；
  上限只能在 `.labflux.toml` 的 `[session] busy_wait_s` 設定，Scheduler 設定沒有對應的全站預設值。
- 原始碼快照略過 symbolic link（`labflux run`、替身、`labflux submit --source`），連結指向的內容在 session 裡不存在；
  FIFO、socket 等特殊檔案仍會讓快照失敗。
- `labflux shell`：只包裝 bash 與 zsh，其他 shell 只在啟動前設定一次 PATH 並印出提示；zsh 的包裝尚未實際執行過（這台機器沒有 zsh），
  並且會替原本沒設 `ZDOTDIR` 的使用者把它設成 `$HOME`。只處理最外層的 shell，巢狀的 bash／zsh 重新讀啟動檔後不會再放回替身；
  bash 的 login 專用檔（`~/.bash_profile`）和以前一樣不讀。PATH 在下一次提示字元才修正，所以同一行的
  `conda activate x && python ...` 仍可能用到本機的 python。alias 與 function 只警告、不移除。
- session 自己結束（超時、取消、轉送中斷）時，工作的最終原因是 Worker 的原因；被 lease 到期或關閉停止時，最終原因是 runtime 的一般 `cancelled`，`STOPPING` 期間顯示 `session lease expired`。關閉已在 `STOPPING` 的 session 會把原因改成一般的「等待確認容器終止」。
- 指令執行中 Worker 重啟，依時間點可能停止 session，也可能留下等待接手；兩種情況該指令都失敗。
- `run.thread` 指派之前的極短時間內，`_execute` 可能把剛啟動的指令當成尚未開始而回覆 `cancelled`，執行緒也可能再送一個錯誤結果；轉送只保留第一個結果，不影響資源。
- backfill 以 session 目前的 lease 到期時間估計它的釋放時間；每個指令都會延長 lease，所以 session 後面的隊首保留時段會跟著往後移。
  尚未啟動的 session 以派送時間加一個 lease 估計。lease 已過但仍佔著資源的 session（指令比 lease 長、`STOPPING`、`UNKNOWN`）視為「現在」釋放，
  與超過時限的 attempt 相同。
- 沒有 stdin、TTY，不轉送客戶端環境變數；session 映像需要接受 `sleep infinity`。
- 單元測試中客戶端斷線要以 `ws.close()` 模擬；離開 TestClient 的 websocket context 會取消 handler，真實 uvicorn 不會。
- 寫回依 D12 只送回新增或修改的檔案；指令執行期間遠端刪除的檔案不會刪除本機檔案。寫回衝突產生的 `<檔名>.remote` 是一般檔案，下一份快照會送到 session。
- 衝突判斷假設檔案時間戳夠精細；起點提早 50 ms，所以指令開始前 50 ms 內在本機改過、遠端也改過的檔案會存成 `.remote`（安全但可能是誤判）。
- 寫回路徑中某一層在本機是 symlink 或一般檔案、但封存需要目錄時，該檔案跳過並提示，不存成 `.remote`；同一次寫回已建立的目錄保留。寫回 checksum 不符時完全不寫回，即使遠端成功也以 125 結束。
- 路由只看 `args[0]`：`uv --quiet run x` 留在本機，只有 `uv run ...` 送到遠端。參數路徑改寫使用實體專案根目錄（`os.getcwd()`，解開 symlink），經 symlink 或 `$PWD` 等邏輯路徑寫的參數，以及 `--opt=/abs/root/...` 形式都不改寫。
- 以完整路徑指定替身腳本（`labflux local -- /tmp/labflux-standins-x/python`）仍會經過替身。替身仍會載入使用者設定，`LABFLUX_CONFIG` 無效或不存在時連本機執行也失敗。
- `session status` 的 `lease_remaining_s` 使用本機時鐘，受開發機與 Scheduler 的時鐘差影響。
- session 變成 `RUNNING` 之後、指令送出之前按 Ctrl-C 會以 130 結束，閒置的 session 留給 lease 處理；只有仍在等待的 session 會被關閉。等待期間的 SIGTERM／SIGHUP 也會關閉這次呼叫建立的 session。
- 與掛住的 Scheduler 進行 HTTP 呼叫時（租用 session、狀態輪詢、關閉），Ctrl-C 要等請求完成或逾時（最多 30 秒）才生效。

**資料庫與規劃文件**

- 在 `wip/policy` 分支建立的開發資料庫把 `schedule_note` 記為版本 4，與目前的版本 4（session）不同，必須重建。
- `plan.md` 第 4 節（最終驗收）還沒有門鈴、快速測試的路由與寫回，以及 M15 資料集快取的條目，短測試那一行仍是舊寫法。
- M15 的項目假設 S2～S4 的版本識別方式，仍是提議；版本格式與正本位置（NAS，或依 S10 經 kayle 以 HTTP 提供）定案後，15.1、15.2 可能要改寫。
- 9.5 維持資源不足時互動工作等待；是否保留容量給互動工作（Q1）原定與 M8 一起決定，目前仍未決定，沒有列成項目。
- 尚未決定的提議刻意沒有寫進 plan：D1、D2、D6（Ctrl-C 取消的範圍已於 2026-11-16 決定，見 T10 與 9.13）、D8（在送出指令的機器上開 session）、D9、D11、F2、F5、1 秒延遲目標與保留容量（R17）。

## 驗證時另外發現的罕見情況（2026-11-16，使用者確認記為限制）

審查修正後，兩個獨立的檢查 agent 回報 6 個罕見情況。使用者確認都記為已知限制、不修；其中兩項上一節已有記錄。

- **lease 過期但指令仍在執行**：backfill 把這個 session 視為「現在」釋放（上一節 backfill 那一條已記錄）。
- **本機新建與遠端同名的檔案**：只在遠端的檔案（超過寫回上限）如果在本機新建了同一個路徑，下一份快照會依 D4b 以本機為準覆蓋遠端那份，
  而且該檔案直接從 `.labflux/remote-files.txt` 移除，不另外提示。
- **AI 工具直接砍掉指令**（第二次 Ctrl-C、SIGTERM、程序被殺）：Worker 會停止指令並照樣送出寫回，但 CLI 已經離開，Scheduler 丟棄這份寫回。
  這次指令改過的檔案留在遠端，不會出現在本機，也不在遠端檔案清單裡；之後的指令也不會再寫回它們，因為變更偵測只比對每個指令的前後。
- **同一個 session 並行的兩個指令**：等待中的指令在連線前就打包快照，而前一個指令送出結果後就釋放忙碌標記，
  此時前一個指令的寫回可能還沒套用到本機。所以第二個指令的快照可能比較舊，套用到遠端時會把前一個指令剛改過的檔案換回舊版。
- **寫回路徑經過本機的 symlink 目錄**：該檔案跳過並提示（上一節寫回路徑那一條已記錄）。
- **子 shell 重新讀取啟動檔**：AI 工具另外開的 shell 如果重新讀取 `~/.profile` 或 `~/.bashrc`，而它們無條件把其他目錄加到 PATH 最前面，
  替身可能被蓋掉。2026-11-16 在 kayle 實測 `bash -lc 'command -v python'` 仍找到替身（見下一節），但結果取決於使用者的啟動檔。

## 9.14 AI coding 工具實測（kayle，2026-11-16）

環境：臨時的 Scheduler 與 Worker（本機 loopback 的空閒 port、暫存資料目錄、唯一的 Worker ID，只用 CPU，映像 `python:3.12-slim`），
不經過實驗室部署的 Scheduler。測試專案的 `.labflux.toml` 設定 `cpu = 1`、`memory_mib = 256`。
以虛擬終端機執行 `labflux shell`（bash，使用者原本的 `~/.bashrc`，內含 conda 設定），再從這個 shell 以 headless 模式啟動
Claude Code（2.1.325，`claude -p ... --allowedTools Bash`），由它的 Bash 工具執行指令。

**替身是否被使用。**請 Claude 依序執行下列指令並原樣回報輸出：

| 指令 | 輸出 | 判讀 |
|---|---|---|
| `command -v python` | `/tmp/labflux-standins-…/python` | Bash 工具保留了 `labflux shell` 的 PATH |
| `python hello.py` | `hello from cf6c5d747b66 cwd /workspace` | 在遠端 session 容器內執行 |
| `python -c "…gethostname(), getcwd()"` | `cf6c5d747b66 /workspace` | 同上，且沿用同一個容器 |
| `hostname` | `kayle` | 不在清單上的指令留在本機 |
| `cat result.txt` | `written remotely on cf6c5d747b66` | 遠端寫出的檔案已寫回本機（D12） |
| `bash -lc 'command -v python'` | `/tmp/labflux-standins-…/python` | 重新讀取 `~/.profile` 的登入 shell 也沒有蓋掉替身 |

**是否會漏看只在遠端的檔案。**Scheduler 的寫回上限改為 1 MiB，請 Claude「執行 `python make_ckpt.py` 訓練，確認
`ckpt/model.pt` 已存檔並回報大小與 `metrics.json` 的 loss」。腳本寫出 3 MiB 的 `ckpt/model.pt` 與小的 `metrics.json`。
Claude 依提示讀了 `.labflux/remote-files.txt`，再用一個送到遠端的 `python -c` 確認檔案存在與大小（3,145,728 bytes），
回報 loss 0.123，並指出檔案只在遠端、session 結束後刪除、要保留需寫到 `/outputs`。
它沒有因為本機找不到檔案而誤判訓練失敗，所以先不補說明檔（docs/architecture.md D12 的備案）。

**清理。**測試後關閉 session，確認這兩個 Worker ID 沒有殘留容器，停止臨時的 Scheduler 與 Worker，並刪除替身暫存目錄。

**限制。**只測了 Claude Code 一種 AI 工具與 bash；zsh 與其他 AI 工具沒有實測。觀察只有一次，屬於定性結果。

## 以 Claude Code 在快速測試上開發一個 uv 專案（kayle，2026-11-16）

目的：確認 AI coding 工具可以直接在這套系統上開發專案，而不只是執行單一指令。
環境：本機 loopback 上的臨時 Scheduler 與 Worker，使用正式的預設值（寫回 100 MB、單一指令 60 秒、閒置 10 分鐘），只用 CPU，
session 映像 `localhost/labflux/python-uv:3.12-uv0.11.8`。測試專案只有一個空的 `pyproject.toml`、README 與 `.labflux.toml`。

從 `labflux shell` 啟動 Claude Code（headless，允許 Bash、Edit、Write、Read），交代的工作是：用 uv 加入 numpy 與 pytest、
以 numpy 實作 `moving_average`、寫 pytest 測試、執行 `uv run pytest -q` 直到通過，並執行一個印出主機名稱的 `python -c`。

結果：

- `uv add numpy`、`uv add --dev pytest` 與 `uv lock` 在本機執行（改的是 `pyproject.toml` 與 `uv.lock`，依 T7 不送遠端）。
- `uv run pytest -q` 送到遠端 session：`uv.lock` 改變，所以先在容器內執行 `uv sync`，再執行測試。
- 第一次執行時，遠端的 `uv sync --locked` 因為 lockfile 過期而拒絕（Claude 在 `uv add` 之後才補上 `[build-system]`）。
  Claude 讀了錯誤訊息，在本機執行 `uv lock` 後重跑，結果 `7 passed in 0.18s`。
- `python -c` 印出 `a91ce25972c0 3.12.15`：在 session 容器內執行，和本機的 CPython 3.12.13 不同。
- Claude 自己說明了 `uv` 與 `python` 是 LabFlux 的替身，並提醒專案缺少 `.gitignore`。

判讀：編輯、依賴管理、遠端測試與依錯誤修正的迴圈都能完成。遠端用 `--locked` 同步，確保跑的是 `uv.lock` 鎖定的版本（D4a）；
lockfile 過期時錯誤訊息足以讓 AI 自行修正。測試後關閉 session、確認沒有殘留容器並刪除暫存檔。
未涵蓋：GPU session、跨機器（Scheduler 與 Worker 在同一台），以及 zsh。

## M10：共享模型服務與推論 Gateway（kayle，2026-11-16）

設計見 `docs/architecture.md` 第 9 節（G1～G7）。模型服務是 `kind = "MODEL"`、擁有者 `@models` 的工作紀錄，
`jobs.service` 記錄模型名稱（migration 5）；Gateway 在 `src/labflux/inference.py`，Worker 端在 `worker.py` 的 M10 段落。
Scheduler 設定新增 `models`（每個模型的映像、參數、資源、權重資料集、port、健康檢查、模式與時間設定）、
`gateway_max_request_bytes`（16 MiB）與 `gateway_read_timeout_s`（600 秒）。CLI 新增 `labflux models`。

| Checklist | 驗證 |
|---|---|
| 10.1 | `test_requests_start_one_service_on_a_node_with_the_weights`：START 帶有 `service`（port、健康檢查、啟動上限、`shm_mib`、`pids_limit`），`HOME=/workspace`，只排到有權重資料集的節點；模型名稱、`NVIDIA_VISIBLE_DEVICES` 與模式的設定檢查 |
| 10.2 | `examples/fake-model/server.py`（只用標準函式庫）；`tests/test_model_docker.py` 以 `python:3.12-slim` 容器執行，權重資料集的位置改放這個腳本 |
| 10.3 | `test_states_from_stopped_to_serving_and_back`：stopped → waiting（資源不足）→ starting → serving → stopping → stopped；`/api/models` 與 `/v1/models` 顯示狀態 |
| 10.4 | 同一模型的兩次請求得到同一個服務、只有一個 START；Docker 測試中兩位使用者同時冷啟動，只建立一個容器 |
| 10.5 | 假 Worker（WebSocket）轉送串流請求，內容、`Content-Type` 與順序不變；Worker 端對假模型轉送串流、一般與 embeddings 請求（`tests/test_inference_worker.py`）；錯誤使用 OpenAI 格式（401、400、404） |
| 10.6 | `cold_wait_s = 0` 時立即回 503、`Retry-After: 10`，模型已開始啟動；失敗後在重試時間前回 503 `model_failed` 並帶剩餘秒數 |
| 10.7 | 假模型 `/health` 回 503 期間不送 `STARTED`，通過後才送；超過 `startup_timeout_s` 停止容器並回報 `FAILED`（`model server not healthy within 1s`）；Docker 測試中 3 秒未就緒的模型以 503 回應請求，容器被清除 |
| 10.8 | 進行中的請求阻止閒置停止；請求結束（完成、Worker 端錯誤、Worker 中途斷線、客戶端中途離開）時計數歸零並重新計算閒置時間；Docker 測試中客戶端讀到第一段就離開，假模型記錄 `client disconnected mid-stream` |
| 10.9 | 閒置 300 秒停止；有工作排隊時閒置 60 秒提前回收；常駐模型自己啟動、不因閒置或排隊工作停止，失敗後等 `retry_after_failure_s` 才重新啟動；從設定移除的模型被停止或取消；模型服務排在先提交的批次工作之前 |

- `uv run pytest -q -m "not docker and not vm and not gpu"`：343 passed、1 skipped（main 為 301 passed）。
- `sg docker -c 'uv run pytest -q -m "not vm and not gpu"'`：370 passed、1 skipped，其中 `tests/test_model_docker.py` 2 個；
  模型相關的三個測試檔另外連續執行 3 次，每次 42 passed。兩位使用者同時冷啟動（假模型啟動 1 秒）到兩個串流回應都完成約 2.7 秒；
  服務中時一般請求經 Gateway、Worker 到容器再回來約 51 ms。
- `scripts/lab_deploy.py` 把節點清單的 `[models.<name>]` 寫進產生的 `scheduler.toml`（巢狀的 `resources`、`env` 寫成 inline table，
  名稱含 `/`、`:`、`.` 時加引號），模型要求的資料集沒有任何節點提供時拒絕；`tests/test_lab_deploy.py` 20 passed。

**程式審查與修正（2026-11-16）**：獨立審查提出 11 項，依原則修正實際會發生且成本低的項目，其餘列入下方已知限制。

- `fcfs`／`priority` 下排隊的模型服務不再擋住後面的工作；自動釋放的模型排隊超過 `cold_wait_s + 30` 秒沒有請求就取消。
- 設定改變時停止執行中的模型並以新設定重啟（排隊中的直接換成新設定）；比較時忽略優先權。
- 所有使用者都能讀模型服務的日誌（`labflux logs <job_id>`）。
- Worker 重啟接手時，尚未通過健康檢查的模型維持 `STARTING`，不再以容器的啟動時間當成可服務。
- 模型的時間設定拒絕 `inf` 與 `nan`（否則排程迴圈會停住）。
- 回應中途被切斷時切斷連線、不送結尾（真實 uvicorn 測試中客戶端得到 `RemoteProtocolError`，而不是看似完整的 200）；
  回應完整送完後才到的取消不會把它標成未完成。
- 取消改為關閉連到模型的 socket：假模型每個字間隔 5 秒時，取消後 1.5 秒內結束（原本要等下一個字或讀取逾時）。


### 10.10 實驗室部署與 gemma-4-31B（kindred，2026-11-16）

以 commit `17b1486`（main `1504cdd` 加上 `feat/third-node` 與本次 M10）執行 `lab_deploy.py deploy`，三台一起升級。
部署前三台都沒有配置；先在 kayle 以 SQLite backup API 備份 Scheduler 資料庫（schema 版本 2、21 個工作），
新版 Scheduler 啟動時升級到版本 5，原本 21 個工作都保留。三台部署後都是 `ONLINE`。

gemma 的設定沿用原本 compose 的參數（`vllm/vllm-openai:v0.30.0`、tensor parallel 2、`--max-model-len=65536`、
`--gpu-memory-utilization=0.90`、`--language-model-only`）：資源 8 CPU、48 GiB、2 張 A40，`shm_mib = 16384`；
權重是 kindred 上登錄為資料集 `hf-models` 的 Hugging Face 快取（唯讀掛載、`HF_HUB_OFFLINE=1`）。
容器以 `labflux` 帳號（UID 2001，容器內沒有對應的 passwd 項目）執行，所以另設 `USER`／`LOGNAME`；
`cap_drop=ALL`、`no-new-privileges` 與記憶體上限都照常生效，vLLM 可以啟動。

第一階段為自動釋放模式（閒置 180 秒），驗證啟動、共用與交還 GPU：

| 檢查 | 結果 |
|---|---|
| 第一個請求（冷啟動） | `/v1/chat/completions` 等待中模型被排到 kindred 的卡 3、4；306 秒後回應 HTTP 200，內容為 gemma 的中文自我介紹 |
| 三個客戶端同時請求（kayle、karma、kindred，同一個帳號） | 都是 HTTP 200；`labflux models` 顯示進行中 3 個請求、同一個服務；一般請求約 2.7～3.0 秒完成，串流請求 0.09 秒收到第一個位元組、2.3 秒完成 |
| 提前回收與交還 GPU | 模型服務中時提交 `gpu_model = "A40"` 的 MNIST，因兩張卡都被佔用而排隊；最後一個請求結束約 60 秒後模型被停止，MNIST 立刻分到卡 3 並完成（142 秒、99.0%） |

第二階段改為常駐模式（啟動上限 600 秒），只重新部署 kayle（重啟 Scheduler）：沒有任何請求，gemma 在部署完成後
238 秒自己啟動並進入可服務狀態；`/v1/models` 顯示 `persistent`、`serving`、`kindred`，`/v1/completions` 0.29 秒回應；
模型容器沒有開放任何 port，卡 3、4 各用約 41 GB VRAM，與原本的 compose 容器相同。

- 重新部署 kayle 時沒有未釋放的配置，所以這次沒有實際驗證「有常駐模型執行時重啟 Scheduler」；這個情況依賴 M6.6 的還原機制，
  已在 Docker 測試驗證。
- 「多人共用」以三台機器上的三個客戶端同時使用同一個服務驗證；實驗室目前只有一個 LabFlux 帳號，沒有以不同帳號測試。
- 指令調整過的模型對 `/v1/completions` 的純文字續寫會產生沒有意義的文字，這是模型本身的行為，聊天介面正常。

**已知限制（M10）**

判定原則與前幾次審查相同：少見情況記錄為限制，不為它擴大設計。

- Gateway 只能從 LabFlux 節點連到（G2）；其他機器以 SSH 轉送連入。
- Scheduler 重啟時進行中的推論請求失敗，需要重送；模型服務的工作紀錄保留，進行中請求數從 0 開始。
- 提前回收的條件只看「有沒有工作在排隊」：任何排隊工作（包含另一個模型的服務）都會讓閒置超過 `reclaim_idle_s`
  的自動釋放模型被停掉，即使那個工作用不到釋放出來的資源；代價是多幾次冷啟動。
- 閒置停止的判斷與新請求之間有極短的空窗（`ensure_model` 回傳之後、開始計數之前），剛好落在閒置期限上時，
  該請求會失敗或被截斷。
- 慢速的客戶端最多累積 64 則未讀的回應訊息（每則最多 256 KiB 原始資料，實際多為一次網路讀取的大小），
  超過時請求被中止；走 SSH 轉送接收很大的 embeddings 回應時可能遇到。
- 客戶端取消時，模型還沒送出回應開頭之前（非串流請求就是整段生成期間）無法傳給模型，模型會生成完才結束，
  最長 `gateway_read_timeout_s`。
- 卡在 `UNKNOWN`（節點失聯）的模型不會改到其他節點啟動，也無法由使用者取消，要等節點回來。
- 模型容器在 Docker 預設的 bridge 網路上，同一台機器的其他容器可以不經 Gateway 直接連到模型的 port。
- vLLM 映像約 10 GB，`image_pull_timeout_s` 預設 300 秒；模型節點上要先拉好映像。
- 模型的權重要預先放在節點上（G4），LabFlux 不下載權重。
- 模型的日誌與批次工作共用 `max_log_bytes`（16 MiB）上限；長時間執行的常駐模型寫滿後不再保存新的日誌。
- Gateway 不改寫請求，`--served-model-name` 必須與設定的模型名稱相同；所有使用者都可以使用所有模型。
- 模型服務的資源占用不計入任何使用者的近期用量（擁有者是 `@models`）。
- 每個中途被切斷的回應，uvicorn 會另外記一筆 `Exception in ASGI application` 的 ERROR 與 traceback（切斷連線的副作用）。
- 設定改變時停止模型也會中斷它正在處理的請求。Scheduler 重啟後的第一次排程之前，若有心跳或註冊觸發派送，
  排隊中的模型可能先以舊設定啟動，下一輪再被停止並以新設定重啟；節點在重啟後先處於 `RECONCILING`，實際上很少發生。

## M11：終止後重跑的搶占（kayle，2026-11-16）

在 `feat/m11` 分支、kayle 上執行，用單元測試、本機 Docker 容器與 kayle 的一張實體 GPU；之後部署到實驗室三台節點實測（見「實驗室實測」）。
規則見 `docs/architecture.md` 4.4（PE1～PE10）。

**接到 M10 之上。** M11 原本建在 `main`（1504cdd）上，之後 rebase 到 M10 的 commit 17b1486（M10 與它的審查修正，
其下是 535e78c 的多 Worker 部署與第三個節點），原本的合併 commit 攤平成線性歷史。M10 已使用 migration 5（`jobs.service`），
M11 的 `attempts.preempted_by` 改為 migration 6；M10 程式與測試裡的 `max_retries` 改成 `retries_on_node_failure`（模型服務為 0）。
接上之後才加入與模型服務有關的部分（11.2、11.4、11.8），並修正審查找到的問題（見下方「程式審查」）。

**欄位改名。** M11 起 `max_retries` 改名為 `retries_on_node_failure`，大於 0 時必須同時設定 `safe_to_rerun: true`。
本文件較早的章節（M6.5～M6.7 的 6.5 重跑）保留當時的名稱 `max_retries`，指的是同一個設定；舊名稱仍然接受（見下表 11.1）。

**實作。** `JobSpec` 加入 `safe_to_rerun`、`preemptible`，`max_retries` 改名為 `retries_on_node_failure`（舊名稱由
`model_validator(mode="before")` 轉換）。設定 `preemption_enabled`（`true`）與 `max_preemptions_per_job`（3）。
migration 6 加入 `attempts.preempted_by`，attempt 狀態多一個 `PREEMPTED`。`_schedule` 的隊首啟動不了時呼叫 `_preempt`；
選擇被搶占工作的純函式在 `policy.py`（`Victim`、`lost_seconds`、`choose_victims`、`cheapest_preemption`）。
停止沿用 `_request_stop`，多一個 `preempted_by` 參數：不設 `cancel_requested`，在 attempt 記下搶占它的工作。
`_finish` 把停止生效或執行遺失的 attempt 記為 `PREEMPTED`，工作以原本的 `submission_seq` 回到 `QUEUED`。
`handle_event` 的 STARTED、`_resume` 與 `_reconcile` 把 `preempted_by` 當成取消處理，不會回到執行中，也不會重送 START。
backfill 的 `_bookings` 把停止中的被搶占 attempt 當作現在結束（接到 M10 之後的審查改成所有停止中的 attempt，見下方「程式審查」）。
Dashboard 的 attempt 表格顯示 `PREEMPTED`；
被搶占而停止中的工作仍可以取消。

**接上 M10 後加入的部分。**

- 不為已經在釋放的資源搶占（PE10、11.2）：`_preempt` 選擇被搶占的工作之前，由 `_releases` 收集可用節點上已經在釋放的資源
  （`policy.Release`）：`preempted_by` 為空的停止中 attempt，以及 M10 的提前回收規則現在就會停掉的執行中自動釋放模型。
  M10 的閒置停止與提前回收判斷從 `_manage_models` 抽成 `_idle_stop`，兩邊共用，`_manage_models` 的行為不變。
  `choose_victims` 與 `fits_after_stopping` 多一個 `released` 參數，把這些資源算成空的：只靠它們就放得下時不搶占，
  隊首的 `schedule_note` 寫 `waiting for job <id> on <節點> to stop`、`waiting for model <名稱> on <節點> to stop` 或
  `waiting for idle model <名稱> on <節點> to be reclaimed`；放不下時以它們已空出來選擇，停較少的工作。
- 模型服務可以搶占（PE9、11.4）：搶占者的判斷由「kind 是 `SESSION`」改成互動類（`class_rank`），模型服務因此和 session
  一樣在所有策略下可以搶占；被搶占對象仍然只查 `kind = BATCH` 的工作。`_schedule` 在 M10「模型服務不擋住後面的工作」的
  `continue` 之前先呼叫 `_preempt`；搶占不了時，下一個被擋住的工作成為隊首。
- `labflux logs --follow`（審查問題 3）：沒有指定 `--attempt-id` 時，CLI 記住讀到的 attempt id；伺服器換成新的 attempt
  （被搶占或節點失聯後重跑）時，先送出解碼器裡剩下的內容，在 stderr 印出 `--- attempt <短 id> (rerun) ---`，捨棄這次回應，
  下一次從新 attempt 的 seq 0 開始讀。只改 CLI，伺服器不變。

API 測試在 `tests/test_preemption.py`（以 HTTP API 操作，Worker 的命令與事件由測試送出，時間由測試控制），
接上 M10 後的 API 測試在 `tests/test_preemption_models.py`（同樣的做法，模型的狀態沿用 M10 測試的輔助函式回報），
純函式測試在 `tests/test_preemption_policy.py`，欄位驗證另有 `tests/test_models.py`，Docker 端到端測試在 `tests/test_preemption_docker.py`，
實體 GPU 測試在 `tests/test_gpu_docker.py`。未註明檔案的測試名稱都在 `tests/test_preemption.py`。

| Checklist | 行為 | 驗證 |
|---|---|---|
| 11.1 | 三個欄位預設為否／0；`preemptible` 或大於 0 的 `retries_on_node_failure` 沒有 `safe_to_rerun` 時回傳 422，訊息指出是哪個欄位需要它；重跑上限 10 | `test_rerun_fields_default_off`、`test_preemptible_or_node_failure_retries_require_safe_to_rerun`（4 組）、`test_rerun_fields_are_accepted_with_safe_to_rerun`；`tests/test_models.py` 的 `test_rerun_flags_require_safe_to_rerun` |
| 11.1 | 舊名稱 `max_retries` 轉成 `retries_on_node_failure`（大於 0 時 `safe_to_rerun` 設為真，明確給 `false` 則 422），輸出只有新名稱；兩個名稱都給回傳 422 | `test_legacy_max_retries_is_mapped_to_retries_on_node_failure`、`test_giving_both_retry_field_names_is_rejected`；`tests/test_models.py` 的 `test_legacy_max_retries_still_validates`（`JobSpec.model_validate`：資料庫的 `jobs.spec` 與 Worker `state.json` 裡的 `ExecutionSpec.job` 都經過它；沒有以實際的舊資料庫或舊 `state.json` 測試） |
| 11.2 | 停止中的工作不搶占：使用者取消後立刻重送，或 session 結束後立刻開新的 session，舊的那個停止中、資源很快就會空出來；新的工作只等待（`waiting for job <id> on kayle to stop`），只有舊的那個收到 STOP，已經執行一小時的可搶占工作繼續執行；確認停止後新的工作派送 | `tests/test_preemption_models.py` 的 `test_head_waits_for_a_stop_in_progress_instead_of_preempting`（取消、session 結束 2 組） |
| 11.2 | 停止中的資源不夠時以它已空出來計算：只多停一個（損失較少的）工作；被搶占的工作停下後，隊首繼續等取消中的那個，確認後派送 | `tests/test_preemption_models.py` 的 `test_a_stop_in_progress_that_is_not_enough_means_fewer_victims` |
| 11.2 | 閒置 61 秒的自動釋放模型（`reclaim_idle_s` 60 秒）：隊首等它被回收（`waiting for idle model fake on kayle to be reclaimed`），不搶占；M10 回收後模型停止中，隊首改等它停止（`waiting for model fake on kayle to stop`），確認後派送。閒置 30 秒時模型照常服務，隊首搶占可搶占的工作 | `tests/test_preemption_models.py` 的 `test_head_waits_for_an_idle_model_to_be_reclaimed_instead_of_preempting`（2 組） |
| 11.2 | 心跳逾時（還沒被標成離線）的節點上停止中的模型服務不算，隊首照常在另一個節點搶占 | `tests/test_preemption_models.py` 的 `test_a_stop_on_an_unavailable_node_does_not_hold_back_preemption_elsewhere` |
| 11.2 | 純函式：在釋放的 CPU、RAM、卡與同時執行數一開始就算空的；只靠它們就放得下時回傳空清單，不夠時少選，全部算上也放不下時回傳 `None` | `tests/test_preemption_policy.py` 的 `test_resources_on_their_way_back_count_as_free` |
| 11.3 | `priority` 與 `backfill` 下高優先權批次工作搶占；`fcfs` 的批次工作不搶占 | `test_higher_priority_job_gets_the_node_only_after_the_victim_is_confirmed_stopped`（`priority`）、`test_backfill_does_not_hand_resources_being_freed_to_a_later_job`（`backfill`）、`test_batch_job_does_not_preempt_under_fcfs`；`tests/test_preemption_policy.py` 的 `test_policy_order_check_used_against_ping_pong`（`batch_preemption` 旗標） |
| 11.3 | 不可搶占的工作不停；優先權相同或較高不停（5、7 對 5） | `test_job_not_marked_preemptible_is_not_stopped`、`test_job_with_equal_or_higher_priority_is_not_stopped`（2 組） |
| 11.3 | 互搶規則：優先權 4 的工作執行 10 分鐘時被優先權 5 搶占（400 + 10 − 10 = 400 < 500）；執行 200 分鐘時重新排隊會排在前面（400 + 200 − 60 = 540 > 500），不搶占 | `test_victim_that_would_rank_first_again_once_queued_is_not_stopped`（2 組）、`test_policy_order_check_used_against_ping_pong` |
| 11.3 | 整個叢集同時只進行一輪：停止中時，能在另一節點搶占的新隊首等待，`schedule_note` 列出停止中的工作；該輪結束後才開始下一輪 | `test_no_second_preemption_starts_while_one_is_in_progress` |
| 11.3 | 每一輪只有隊首可以搶占：`priority` 下沒有進行中的搶占，隊首（優先權 9、2 CPU）因為不可搶占的工作占著一半節點，停掉可搶占的工作也放不下；排在後面的工作（優先權 5、1 CPU）停掉它就放得下，但沒有送出 STOP。隊首取消後，後面的工作成為隊首，同一個工作才被停掉，表示擋住它的是隊首規則，不是一輪的限制或對象的條件 | `test_only_the_first_job_in_line_may_preempt` |
| 設定 | `preemption_enabled=false` 時批次工作與 session 都不搶占 | `test_nothing_is_preempted_when_preemption_is_disabled`（2 組） |
| 11.4 | session 在 `fcfs`、`priority`、`backfill` 下都搶占優先權 9 的可搶占批次工作，確認停止後 session 的 START 送到該節點；批次工作不搶占執行中的 session | `test_session_preempts_a_preemptible_batch_job_of_any_priority`（3 組）、`test_batch_job_never_preempts_a_running_session` |
| 11.4 | 冷啟動請求排入的自動釋放模型在 `fcfs`、`priority`、`backfill` 下都搶占優先權 9 的可搶占批次工作（不設 `cancel_requested`，原因為 `preempted by job <模型的 job id>`），確認停止後模型啟動、可服務；之後 session 與優先權較高的批次工作都不停掉可服務的模型 | `tests/test_preemption_models.py` 的 `test_a_queued_model_preempts_a_preemptible_batch_job_and_is_never_a_victim`（3 組） |
| 11.4 | `fcfs` 下常駐模型的啟動搶占可搶占的批次工作，確認停止後模型啟動 | `tests/test_preemption_models.py` 的 `test_a_persistent_model_start_may_preempt` |
| 11.4 | `priority` 下排在最前面的模型搶占不了（唯一有權重的節點上是不可搶占的工作）時，下一個被擋住的批次工作搶占另一個節點上的可搶占工作；不可搶占的工作繼續執行，模型繼續排隊 | `tests/test_preemption_models.py` 的 `test_when_a_blocked_model_cannot_preempt_the_next_blocked_job_may` |
| 11.5 | 損失 = 現在減 STARTED 時間（還沒 STARTED 為 0，時鐘偏差不會變負）；順序為損失、優先權、job id | `tests/test_preemption_policy.py` 的 `test_lost_work_counts_from_the_reported_start`、`test_order_is_lost_work_then_priority_then_job_id` |
| 11.5 | 依序加入直到放得下，再移除用不到的；CPU、RAM 與同時執行數都計入；已經放得下時不停任何工作；全部停掉也放不下時不搶占 | `tests/test_preemption_policy.py` 的 `test_one_victim_with_less_lost_work_is_enough`、`test_victims_are_added_until_it_fits`、`test_victims_that_are_no_longer_needed_are_dropped`、`test_memory_and_the_job_slot_limit_count`、`test_it_already_fits_without_stopping_anything` |
| 11.5 | 一個就夠時停損失少（較晚開始）的；多個節點選損失總和最少，相同時選工作數少的，再依節點 ID | `test_of_two_candidates_the_one_with_less_lost_work_is_stopped`、`test_node_with_less_total_lost_work_is_chosen`；`tests/test_preemption_policy.py` 的 `test_across_nodes_the_least_total_lost_work_wins` |
| 11.5 | 只考慮有隊首資料集的節點：另一個節點上的工作損失較少（較晚開始），但那個節點沒有資料集，不停；停有資料集節點上的工作，隊首派送到那裡 | `test_only_a_node_with_the_preemptors_dataset_is_considered` |
| 11.5 | GPU：只停持有符合卡片的工作（假的 3090／A5000 資料），新工作拿到那張卡 | `test_gpu_request_stops_the_job_holding_a_matching_card`；`tests/test_preemption_policy.py` 的 `test_a_gpu_request_frees_a_matching_card_only`。符合的卡片沿用 `_eligible_gpus`（型號、最低 VRAM、外部使用），VRAM 與外部使用的篩選由 M7 的測試涵蓋 |
| 11.6 | STOP 命令為 `stop-<attempt_id>`；被搶占的工作 `STOPPING`、不設 `cancel_requested`，原因為 `preempted by job <id>; waiting for confirmed container termination`；確認終止前再排程一次也不派送，確認後才派送到該節點 | `test_higher_priority_job_gets_the_node_only_after_the_victim_is_confirmed_stopped`；Docker：`test_higher_priority_job_preempts_a_real_container_which_then_reruns` |
| 11.6 | backfill：停止期間，之後提交、會延遲搶占工作的小工作與重新排隊的被搶占工作都拿不到先釋放的 CPU | `test_backfill_does_not_hand_resources_being_freed_to_a_later_job` |
| 11.2、11.6 | backfill：隊首等取消中的工作停止（不搶占）時，保留時段落在現在（`reserved on kayle as soon as they stop`）；之後提交、600 秒的短工作放得進旁邊空著的 CPU，但會讓隊首在停止確認後又放不下，所以不派送；確認後隊首派送（審查問題 R1） | `test_backfill_does_not_hand_the_room_beside_a_cancel_in_progress_to_a_later_job` |
| 11.6 | 停止期間心跳中斷又恢復維持 `STOPPING`，STOP 照樣送達；遲到的 STARTED 不讓它回到執行中；Worker 重啟後容器已不在，記為 `PREEMPTED` 並派送搶占的工作 | `test_heartbeat_lapse_during_the_stop_keeps_the_attempt_stopping`、`test_start_reported_after_a_lapse_during_the_stop_does_not_resume_the_victim`、`test_worker_restart_with_the_victims_container_gone_counts_as_preempted` |
| 11.7 | attempt `PREEMPTED`（`preempted_by` 為搶占的工作），工作以原本的 `submission_seq` 回到 `QUEUED`，原因含 `1 of 3`；之後以新的 attempt 執行 | `test_higher_priority_job_gets_the_node_only_after_the_victim_is_confirmed_stopped` |
| 11.7 | 實際容器：被搶占那次的 `/outputs` 可以下載，重跑以新 attempt 成功 | Docker：`test_higher_priority_job_preempts_a_real_container_which_then_reruns` |
| 11.6、11.7 | 實體 GPU：優先權較高的 GPU 工作在被搶占的 attempt 結束後才建立 attempt，拿到同一張卡；被搶占的工作 [PREEMPTED, SUCCEEDED]，重跑也在這張卡上（見下方「GPU 端到端」） | GPU：`tests/test_gpu_docker.py` 的 `test_higher_priority_gpu_job_preempts_the_card_and_the_victim_reruns_on_it` |
| 11.7 | 停止期間自己結束保留 `SUCCEEDED`／`FAILED`；擁有者在停止期間取消變成 `CANCELLED`，不重新排隊 | `test_victim_that_ends_on_its_own_during_the_stop_keeps_its_outcome`（2 組）、`test_victim_cancelled_by_its_owner_during_the_stop_is_cancelled` |
| 11.7 | 達到 `max_preemptions_per_job`（測試設為 1）後不再被選中，照常執行到 `SUCCEEDED`，等待的工作接著派送 | `test_job_preempted_the_maximum_number_of_times_is_not_chosen_again` |
| 11.7 | 搶占不消耗 `retries_on_node_failure`（attempt 為 [PREEMPTED, FAILED, STARTING]）；節點失聯的重跑不算搶占（[FAILED, PREEMPTED]，原因 `1 of 3`） | `test_preemptions_do_not_consume_node_failure_retries`、`test_node_failure_retries_do_not_count_as_preemptions` |
| 11.8 | 高優先權批次工作、session 與模型服務的啟動透過搶占取得資源，不可搶占的工作不受影響 | 上列 11.3、11.4 的測試，Docker 與 GPU 測試；不可搶占的工作：`test_job_not_marked_preemptible_is_not_stopped`、`test_only_the_first_job_in_line_may_preempt`、`tests/test_preemption_models.py` 的 `test_when_a_blocked_model_cannot_preempt_the_next_blocked_job_may` |
| CLI | `labflux logs --follow` 追蹤到節點失聯後的重跑（搶占後的重跑走同一段程式）：舊 attempt 的輸出之後，stderr 印出 `--- attempt <id> (rerun) ---`，新 attempt 的輸出從第一行起完整印出 | `tests/test_cli.py` 的 `test_cli_logs_follow_prints_a_rerun_from_its_start` |

**Docker 端到端。** `test_higher_priority_job_preempts_a_real_container_which_then_reruns`：`priority` 策略的 Scheduler（uvicorn，loopback）
與同一程序內的一個 Worker 執行緒，Worker 只配置 1 CPU、256 MiB；兩個工作都用 `python:3.12-slim`、各需要 1 CPU，不能同時執行。
優先權 1 的可搶占工作先寫 `/outputs/progress.txt`，印出 `ready` 後 sleep 4 秒；看到 `ready` 後提交優先權 8 的工作。結果：

- 第一個 attempt `PREEMPTED`、`preempted_by` 為優先權 8 的工作、已釋放、exit code 不為 0（被 STOP 停止，不是自己結束）；
  工作 `QUEUED`，原因為 `preempted by job <id>; queued to run again (preemption 1 of 3)`。
- 被搶占那次的輸出可以下載，`progress.txt` 內容為 `partial result`。
- 優先權 8 的 attempt 在被搶占的 attempt 結束之後才建立，`SUCCEEDED`，輸出 `high.txt` 可以下載。
- 被搶占的工作以新 attempt 重跑：attempt 為 [PREEMPTED, SUCCEEDED]、新 attempt exit 0、`preempted_by` 為空，
  在優先權 8 的工作結束後才建立；日誌有 `done`。最後節點的工作數為 0，沒有殘留容器。

**GPU 端到端。** `test_higher_priority_gpu_job_preempts_the_card_and_the_victim_reruns_on_it`（`docker`、`gpu` 標記，沒有閒置的卡就略過）：
沿用上面的 Scheduler 與 Worker，Worker 只交出一張閒置的實體卡（另配 2 CPU、1024 MiB）；兩個工作各需要 0.5 CPU 與 1 張 GPU，
只有卡讓它們不能同時執行。容器只執行 `nvidia-smi -L` 與 sleep，不使用 VRAM。優先權 1 的可搶占工作印出 `ready` 後 sleep 8 秒，
這時提交優先權 8 的工作。結果：

- 優先權 8 的工作 `SUCCEEDED`，分到那張卡（attempt 的 `gpu_uuids` 與容器內 `nvidia-smi -L` 都只有它），attempt 在被搶占的 attempt 結束之後才建立。
- 被搶占的工作 `SUCCEEDED`，attempt 為 [PREEMPTED, SUCCEEDED]，`preempted_by` 為優先權 8 的工作，兩次都在同一張卡；
  重跑在優先權 8 的工作結束之後才建立；被搶占那次的日誌沒有 `done`（被停止，不是自己結束）。

**命令與結果。** 前五項在接上 M10 後的 a4021b1 執行；rebase 到 main `29f66ce` 之後（bead7f4）程式與測試內容相同。

- `uv run pytest -q -m "not docker and not vm and not gpu"`：404 passed、1 skipped（`tests/test_fastlane.py:230`，沒有 zsh）、32 deselected。
  M10 的 17b1486 為 343 passed；接到 M10 之前，M11 在 main 上為 348 passed（main 為 301 passed）。
- `uv run pytest -q -m "docker and not gpu and not vm"`（全部不用 GPU 的 Docker 測試）：28 passed、0 skipped，包含 M10 的
  `tests/test_model_docker.py`（2 個模型端到端測試）與 M11 的 `tests/test_preemption_docker.py`。執行前沒有執行中的容器，結束後也沒有留下容器。
- GPU：執行前 `nvidia-smi --query-compute-apps=pid --format=csv,noheader` 沒有輸出，兩張卡都只用了 12 MiB。
  `uv run pytest -q -m gpu`：4 passed、0 skipped，包含新的 `test_higher_priority_gpu_job_preempts_the_card_and_the_victim_reruns_on_it`；
  結束後沒有留下容器或 GPU 程序。
- `uv run ruff check src tests`：通過；`uv run ruff format --check src tests`：61 個檔案都已格式化。
- 沒有執行 `-m vm`。
- 接到 M10 之後的審查修正（R1）、資料集測試與部署腳本的排程策略（commit 7eb2571）：
  `uv run pytest -q -m "not docker and not vm and not gpu"`：411 passed、1 skipped（同上，沒有 zsh）、32 deselected；
  `uv run ruff check src tests scripts`：通過；`uv run ruff format --check src tests scripts`：65 個檔案都已格式化。
  這些改動只動 Scheduler 的 backfill 預約、測試與部署腳本，沒有改 Worker，所以沒有重跑 Docker 與 GPU 測試。

**程式審查（2026-11-16）。** 接到 M10 之前，兩個審查 agent 獨立審查 M11，合併重複後 3 個問題，都以臨時測試重現：

1. 已經有工作在停止、資源很快就會空出來，隊首卻還是去搶別的工作（例如取消後立刻重送、session 到期後馬上開新的 session），
   讓已經跑了很久的可搶占工作白白重跑。已修正：PE10、plan 11.2。
2. 被搶占 GPU 工作所在節點的 Worker 在停止期間重新註冊，可能在別的節點多搶占一個工作。很少發生，列入下方已知限制，不修正。
3. `labflux logs --follow` 追蹤時，工作重跑後新 attempt 開頭的輸出被跳過，也沒有提示。節點失聯後的重跑本來就有這個問題，
   搶占讓重跑變常見。已修正（只改 CLI）。

**接到 M10 之後的程式審查（2026-11-16）。** rebase 到 M10、加入模型服務的部分之後再審查一次，找到 2 個問題，
都以臨時測試重現：

1. （R1）backfill 的 `_bookings` 只把停止中的被搶占 attempt 當作現在結束；因使用者取消、session 結束、模型閒置停止或回收、
   模型從設定移除而停止中的 attempt，仍然算到硬性時限或 lease 到期。隊首等這些停止（PE10）時，保留時段因此落在很久以後，
   停止的幾秒內，短工作或重新排隊的被搶占工作可以 backfill 進隊首也需要的空間，停止確認後隊首又放不下，再搶占一次。
   已修正：每一種停止都會讓工作 `cancel_requested`，或讓 attempt 記下 `preempted_by`，`_bookings` 以這個條件把停止中的
   attempt 都當作現在結束（attempt 是 `STOPPING`，或心跳中斷後是 `UNKNOWN` 都一樣）。上表的
   `test_backfill_does_not_hand_the_room_beside_a_cancel_in_progress_to_a_later_job` 在修正前失敗（短工作被派送）。
2. （R2）被擋住的模型服務在等已經在釋放的資源時，不會占住它們（`fcfs`、`priority` 下模型服務不擋住後面的工作），
   下一個被擋住的工作可能把同一份釋放算成自己的，選太少被搶占的工作。要同時有一個在等的模型和另一個被擋住的工作才會發生，
   列入下方已知限制，不修正。

**部署腳本的排程策略。** 批次工作只在 `priority` 與 `backfill` 下搶占，但 `scripts/lab_deploy.py` 原本不寫 `scheduling_policy`，
實驗室一律是 `fcfs`。節點清單的 `[scheduler]` 現在可以加 `policy`（`fcfs`、`priority`、`backfill`，其他值在解析清單時拒絕），
有設定時寫進產生的 `scheduler.toml`；沒有設定時產生的檔案不變，維持 `fcfs`。`configs/lab.example.toml` 附註解掉的範例，
用法見 `docs/lab-deploy.md`。`tests/test_lab_deploy.py`：25 passed（三種策略產生的 TOML 都能被 `Settings` 載入並帶有該策略、
錯誤的值被拒絕、沒有設定時只少這一行）。

**實驗室實測（2026-11-16）。** 以 commit `2e6685d`（main `29f66ce` 加上 M11）執行 `lab_deploy.py deploy`，三台一起升級。
部署前以 SQLite backup API 備份 Scheduler 資料庫（schema 版本 5、24 個工作）；新版 Scheduler 啟動時升級到版本 6，24 個工作都保留。
kindred 的 Worker 重新啟動時，常駐的 gemma4-31b 沒有被重啟：`labflux models` 顯示同一個工作與 attempt，卡 3、4 各用約 41 GB，
`/v1/chat/completions` 0.27 秒回應 HTTP 200。這是第一次在實驗室驗證「常駐模型執行中重新啟動 Worker 與 Scheduler」，M10 只在 Docker 測過。

測試工作只用 kayle 與 karma 的卡，每個情境送出前確認要用的卡沒有運算程序、VRAM 用量低於 1 GiB；kindred 的 A40 沒有使用。
先以實驗室平常的 `fcfs` 測 F1、F2；再在節點清單加上 `policy = "priority"`，只重新部署 kayle（Scheduler）測 P1、P2；
最後拿掉這個設定、再部署 kayle 一次，回到 `fcfs`。每次重新部署後 gemma 都還是同一個 attempt。

| 情境 | 策略 | 檢查 | 結果 |
|---|---|---|---|
| F1 session 搶占批次工作 | `fcfs` | 可搶占的批次工作在 karma 的 4090 上跑 6 秒後，要求同一張卡的 session（`labflux run -- nvidia-smi -L`）讓它停止：attempt 變成 `PREEMPTED`、`preempted_by` 是 session，工作回到 `QUEUED`（`preemption 1 of 3`）；session 在確認終止的同一個交易內派送到同一張卡；接下來 10 秒批次工作沒有反過來搶 session；被搶占那次的 `/outputs` 可以取回；session 關閉後批次工作在同一張卡重跑成功 | 24 項全部通過 |
| F2 `fcfs` 下批次工作不搶占 | `fcfs` | 優先權 8 的工作等優先權 1 的可搶占工作 20 秒，沒有停止它；之後取消，結果是 `CANCELLED`，不是 `PREEMPTED` | 14 項全部通過 |
| P1 搶占實際的 MNIST 訓練 | `priority` | 優先權 1 的 MNIST 在 4090 上訓練到第 5 個 epoch 時，優先權 8 的 GPU 工作讓它停止，拿到同一張卡（UUID 相同）並完成；MNIST 之後在同一張卡重跑，準確率 99.26%；被搶占那次寫到一半的訓練日誌可以取回；`labflux logs --follow` 先印第一次的日誌，接著是 `--- attempt … (rerun) ---` 與重跑的開頭 | 24 項全部通過 |
| P2 跨節點選擇被搶占的工作 | `priority` | kayle 的 3090 與 karma 的 4090 各有一個優先權 1 的可搶占工作（當時各已跑約 66 秒與 5 秒），A5000 被不可搶占的工作占用；要一張 24000 MiB 以上的卡的優先權 8 工作只停止損失較少的 4090 那個，3090 上的工作繼續執行；優先權 8 的工作完成後，被搶占的工作在同一張卡重跑 | 31 項全部通過 |

- 從送出停止到確認終止為 0.2～0.8 秒；搶占者都在確認終止的同一個交易內派送，時間相差約 1 毫秒。
- 每個情境結束後都沒有留下工作或容器，三張卡都回到閒置。
- P1 的重跑花了 661 秒（時限 900 秒），其中約 10 分鐘在從外部網站下載 MNIST 資料集（第一次只花約 80 秒），與 LabFlux 無關；
  之後重複這個測試時，可以把 MNIST 登錄為 karma 的資料集。
- 實測用的腳本沒有放進 repo。

**尚未驗證。**

- 實驗室的實體節點上驗證了 session 與 `priority` 下批次工作的搶占、跨節點的選擇與 GPU（見上）。`backfill` 與模型服務當搶占者
  只有單元測試：實驗室唯一的模型 gemma 是常駐的，一直占住 kindred 的兩張 A40，沒有拿來測。
- 模型服務的搶占（模型當搶占者、等待閒置模型回收）只在 Scheduler API 層級測試，沒有以實際的模型容器或 vLLM 測試。
- 「session 與模型服務不作為搶占對象」測到的組合：批次工作不停執行中的 session；session 與優先權較高的批次工作不停可服務的模型。
  session 或模型服務當搶占者時遇到執行中的 session、模型服務遇到另一個模型服務，沒有個別測試；被搶占對象的查詢只取 `kind = BATCH`
  的工作，與搶占者是誰無關。
- PE10 列出的停止原因中，模型從設定移除而停止中的情況沒有個別測試；它與其他停止走同一個查詢（停止中、`preempted_by` 為空）。
- 11.9 的 checkpoint：損失目前以這次執行已經跑了多久計算（`lost_seconds`）。

**Checklist。** 勾選 11.1～11.8；11.9 等 checkpoint 的儲存方式。

**M11 已知限制**

- 整個叢集同時只進行一輪：被搶占的 attempt 卡在離線節點上（`UNKNOWN`，資源未釋放）時，新的搶占會暫停，直到該節點回來或它的 Worker 重新註冊。
- 停止開始後不撤回：等待的工作在別的節點拿到資源、被取消（包括客戶端已離開而被取消的排隊 session）時，被停掉的工作照樣變成
  `PREEMPTED` 並重新排隊，也算一次搶占。
- 釋放的資源不專門保留給搶占的工作，隊首在等的已經在釋放的資源（PE10）也一樣：釋放當下策略排在最前面的工作（例如新到的 session）會先拿到。
- 被搶占 GPU 工作所在節點的 Worker 在停止期間重新註冊時，該節點的卡在 Worker 第一次心跳之前看不到；被搶占的 attempt 一結束，
  隊首就可能在另一個節點開始第二次、其實不需要的搶占，多停一個工作、讓它重跑一次。要在停止的幾秒內剛好重啟 Worker 才會發生，
  不修正（程式審查問題 2）。
- 被擋住的模型服務（MODEL 工作）在等已經在釋放的資源時不會占住它們，下一個被擋住的工作可能把同一份釋放算成自己的，
  選太少被搶占的工作；結果多一輪搶占，那個工作還是啟動不了，要等下一輪。要同時有一個在等的模型和另一個被擋住的工作才會發生，
  不修正（接到 M10 之後的程式審查問題 R2）。
- 被搶占容器的輸出上傳失敗（例如超過伺服器上限）時，Worker 回報 `FAILED`，工作以 `FAILED` 結束，不會重新排隊。
- STOP 到達時程式剛好正常結束，Worker 回報 `CANCELLED`，attempt 記為 `PREEMPTED` 並重跑。
- 舊版 Worker 會拒絕含新欄位的 START（`extra="forbid"`），Scheduler、Worker 與 CLI 要一起升級；資料庫與 Worker 本機紀錄裡用
  `max_retries` 的舊工作照常讀得懂。同時給 `max_retries`（大於 0）與 `"safe_to_rerun": false` 回傳 422。
- `fcfs` 下被擋住的批次隊首不能搶占，所以 `schedule_note` 不列出正在停止的工作，也不列出它在等的已經在釋放的資源；
  能搶占的隊首（session、模型服務，或 `priority`、`backfill` 的批次工作）才列出。`preemption_enabled = false` 時都不列出。
- 隊首被擋住的每一輪排程（約每 0.25 秒及每次心跳）都查詢一次執行中的批次工作並驗證它們的 JobSpec，再查詢一次停止中的 attempt
  與執行中的模型服務；`fcfs`、`priority` 下排在最前面的模型服務被擋住時也會查詢。成本隨執行中的工作數增加；在實驗室規模下可接受。

## 6.1 專案目錄保留在節點、下一次接著開始（kayle，2026-11-16）

範圍：`docs/architecture.md` 6.1 的 P0～P7（做法 B），使用者同意 P1～P7 的提議預設值。資料庫新增 `project_results` 表（rebase 到 M11 之後為 schema 版本 7，見「6.1 接在 M11 之後」）。

驗證方式：

- `tests/test_project_results.py`（19 個，含下方審查修正新增的 4 個；假的容器，Worker 以 TestClient 連到真的 Scheduler）：
  - P1：`submit --source` 以資料夾名稱或 `.labflux.toml` 的 `[project] name` 填入 `project`，工作 JSON 的欄位優先，
    沒有來源就沒有 `project`，無效的 `.labflux.toml` 以 1 結束；快速測試的專案名稱規則相同。
  - P0a：目錄保留、`.venv` 刪除、大小與 `labflux results ls` 的內容；別的使用者看不到。
  - P0b／P3：下一個工作看到上一次產生的 `ckpt/model.pt`，但看不到上一次快照的檔案、快取與 symbolic link；
    同名時以這次的快照為準；第三個工作接續第二個（不是第一個）；上一份目錄沒有被改動；複製保留修改時間。
  - 同時執行：兩個同專案的工作以 barrier 確認同時在跑，各自一份目錄，都從同一份結果開始，互相看不到。
  - P0c：有結果的節點優先（否則依 ID 會選另一台）；那台滿了就排到別台、不等待，`schedule_note` 說明有檔案的節點都排不下；別的使用者同名專案不受影響。
    有最新結果的節點忙碌時，排到有較舊結果的節點、從那份開始，而不是 best fit 會選的空節點。
  - P2：重跑從自己上一個 attempt 開始，即使中間有同專案的另一個工作在那台結束；Worker 重啟判定遺失時目錄保留給重跑，重跑讀到中斷前寫的檔案。
    開始前就取消的 attempt 不算一次執行。
    節點重啟一次判定同專案 4 個工作遺失時，每個重跑都從自己的 attempt 開始（記錄第 4 份時不淘汰第 1 份）；每個工作結束後它的舊目錄才被淘汰，最後留 3 份。
  - P4：第 4 次執行後只留 3 份，最舊的一份產生 `prune-<attempt>` 命令；Worker 刪除目錄並 ack，重送無害；其他專案不受影響；
    `deleted_by_next_run` 標在第 3 份。兩個複製以 barrier 確認同時從同一份目錄進行；這時另一份目錄的 PRUNE 立即執行（「複製最新的、刪除最舊的」不互等），
    這份目錄的 PRUNE 不 ack、目錄保留，之後才要開始的複製不讀它；兩個複製結束後命令迴圈被喚醒，PRUNE 再次處理時刪除並 ack。
  - 重送的事件：附 `kept_bytes` 的 FINISHED 原樣重送只接受一次（只有一列保留紀錄），內容不同回 409；舊版 Scheduler 存的事件（沒有 `kept_bytes`）重送時視為重複。
  - P5：session 從最近的結果開始，指令讀得到 checkpoint、只寫回指令改過的檔案；session 結束時目錄刪除，不算一次執行，下一個工作仍從原本的結果開始。
  - 沒有 `project` 的工作：目錄照舊刪除，START 沒有 `previous_attempt_id`，`results ls` 是空的。
  - 舊版資料庫升級後有 `project_results` 表（目前為版本 7）。
- `tests/test_project_results_docker.py`（真的 HTTP、Worker 執行緒與 Docker 容器，`python:3.12-slim`，只用 CPU，唯一的 Worker ID `results-e2e-*`）：
  以 `labflux submit --source trainer --wait` 送兩次同一個程式；第一次寫出 `ckpt/model.pt`（`step`），第二次讀它並附加一行。
  檢查第二次的日誌為兩行、Scheduler 列出兩份保留的目錄（專案 `trainer`）、兩個目錄在磁碟上的內容各自正確，測試後沒有殘留容器。

指令與結果：

- `uv run pytest -q -m "not docker and not vm and not gpu"`：320 passed、1 skipped（審查修正後；第一版 316 passed，改動前 301 passed；跳過的是沒有 zsh 的測試）。
- `uv run ruff check src tests`、`uv run ruff format --check src tests`：通過。
- `uv run pytest -q tests/test_project_results_docker.py`：1 passed。

未驗證：實驗室節點（kayle、karma）與兩台以上的 Docker Worker、GPU 工作、uv 模式實際建立的 `.venv`、大型 checkpoint 的複製時間、Docker 中的 session。

已知限制（依「少見情況記為限制」的原則，不另加機制）：

- **專案以擁有者加名稱識別。**同一位使用者兩個不同的 repo 剛好資料夾同名（例如都叫 `experiments`），會被當成同一個專案而複製到對方產生的檔案；
  遇到時在 `.labflux.toml` 設定不同的 `[project] name`。CLI 只讀 `--source` 目錄本身的 `.labflux.toml`，不往上找。
- **節點故障（P7）。**保留的結果只在那台機器上，節點永久故障就遺失；它的列仍留在 `results ls`，排程說明會提到結果在那台。
  同一個 `worker_id` 以清空的磁碟回來時，下一個工作找不到目錄，記錄警告後從頭開始，之後的執行照常把舊列淘汰。
- **沒有刪除指令與磁碟預算。**只有同專案在同一台的新執行會淘汰舊目錄；不再執行的專案的 3 份目錄一直保留，沒有 `labflux results rm`。
  複製是完整複製（沒有 hardlink 或 copy-on-write），一個專案在一台最多佔「保留份數 + 執行中份數」倍的 checkpoint 空間；
  磁碟滿時複製失敗，工作在準備階段 `FAILED`，不重跑。F5 的磁碟門檻仍是提議。
- **複製的範圍。**上一次快照路徑上的檔案一律不複製，即使程式在執行中改寫過它（例如原地修改的設定檔）；symbolic link（例如 `latest.pt -> ckpt/epoch3.pt`）
  不複製，要用固定檔名或重新產生連結。保留的目錄刪除 `.venv`；大小在結束時量一次，不再更新。
- **Worker 重啟時判定遺失的 attempt** 記為保留，但大小未知（`size` 為 null）；Worker 本機紀錄損壞時，這一列可能指向已刪除的目錄，下一個工作就從頭開始。
- **派送與複製之間被淘汰。**工作派送時選定的來源目錄，若在它開始複製前被淘汰（需要同專案在同一台、短短幾秒內又有 `results_keep` 個執行結束），
  工作從頭開始。已經開始的複製不受影響：PRUNE 等它們結束才刪除，不會留下複製到一半的目錄。
- **等待重跑時可能超過保留份數。**排隊等重跑的工作，它的目錄不淘汰，所以一台機器上同專案的目錄可能暫時多於 `results_keep` 份，
  在那個工作結束（或被取消）時刪除；`deleted_by_next_run` 不標示這種目錄。
- **session。**session 的專案在開啟時決定；每位使用者只有一個 session，從另一個專案目錄送來的指令會沿用已開的 session，不重新複製。
  session 也偏好有專案結果的節點，D8（在送出指令的機器上開 session）仍是提議、未實作。
- **重跑。**只有 Worker 重啟核對時判定遺失的 attempt 會重跑（M6.5）；原節點不回來時，重跑排到別台，就從那台這個專案最近的結果開始（若有）。
- **PRUNE 命令**只在節點 `ONLINE` 時送出；Scheduler 在決定淘汰時就刪除那一列，所以 `results ls` 不再列出、但磁碟上的目錄要等 Worker 處理命令才刪除。

### 6.1 審查修正（2026-11-16）

審查找到四個問題，各加一個沒有修正就會失敗的測試（以第一版的 `scheduler.py`、`worker.py` 執行新測試確認失敗）：

- **淘汰刪掉等待重跑的目錄（MAJOR）。**淘汰原本計算該節點這個專案的所有列，包括排隊等重跑的工作；它的列被刪後，重跑改從別的工作最新的結果開始。
  修正：只淘汰工作已結束（SUCCEEDED／FAILED／CANCELLED）的列；淘汰改在記錄結束、更新工作狀態之後執行，涵蓋這個專案在每台的列，
  所以等待重跑的列在它的工作結束時（或排隊中被取消時）被刪除，保留份數仍會收斂。`results ls` 的 `deleted_by_next_run` 也只標示會被刪的列。
  測試：`test_pruning_spares_the_directories_of_jobs_waiting_for_a_retry`。
- **只偏好有最新結果的那台（MINOR）。**那台排不下時，有較舊結果的節點不被偏好，best fit 可能選空節點。修正：依每台最好的來源在清單中的位置排序，
  沒有結果的節點最後；`schedule_note` 在有檔案的節點都排不下時列出這些節點。測試：`test_placement_falls_back_to_a_node_with_an_older_result`。
- **同一份目錄的複製輪流進行（MINOR）。**改為讀者計數：多個複製可以同時讀同一份目錄。PRUNE 到達時標記這份目錄，之後的複製不讀它；
  還有複製在進行時 PRUNE 不 ack（留給之後的輪詢），命令迴圈不等待，最後一個複製結束時喚醒命令迴圈。
  測試：`test_copies_share_a_kept_directory_and_its_prune_waits_for_them`（取代原本的互斥測試）。
- **升級後重送的事件一直回 409（MINOR）。**比對重送的事件前，先以目前的 `WorkerEvent` 正規化存下的內容，舊版存的事件沒有 `kept_bytes` 也能比對。
  測試：`test_an_event_stored_before_kept_bytes_still_matches_its_resend`；另加 `test_a_finished_event_with_kept_bytes_is_accepted_once` 補上原本沒有測試的重送路徑。

指令與結果：

- `uv run pytest -q -m "not docker and not vm and not gpu"`：320 passed、1 skipped。
- `uv run ruff check src tests`、`uv run ruff format --check src tests`：通過。
- `uv run pytest -q tests/test_project_results_docker.py`：1 passed（只用 CPU）。

### 6.1 接在 M10 之後（rebase 到 main `29f66ce`，2026-11-16）

以上兩次的測試數是 rebase 前（main `1504cdd`）的結果。分支重新接在 #5（多 Worker 部署）與 M10 之後：

- migration：M10 的版本 5（`jobs.service`）不變，`project_results` 改為版本 6。`test_a_version_5_database_gains_the_results_table`
  從 M10 的版本 5 升級；`test_v1_database_upgrades_to_current_and_keeps_jobs` 也先刪掉 `project_results`，從版本 1 一路升級。
- 兩邊都改到的地方合併保留：Scheduler 設定（`models`、Gateway 的兩個設定與 `results_keep`）、`ExecutionSpec`（`service` 與 `previous_attempt_id`）、
  START 命令（模型服務帶 `service`，有來源的專案工作帶 `previous_attempt_id`）。
- 模型服務不當成專案工作：`_model_spec` 明確設 `project=None`；Scheduler 記錄保留、淘汰與挑選來源時排除 `MODEL`；Worker 不保留 `service` 容器的目錄。
  測試：`test_a_model_service_is_not_a_project_job`（START 沒有 `previous_attempt_id`；FINISHED 即使帶了 `kept_bytes` 也不記錄保留）。

指令與結果：

- `uv run pytest -q -m "not docker and not vm and not gpu"`：363 passed、1 skipped（main `29f66ce` 為 343 passed；加上本分支的 19 個與上面 1 個）。
- `uv run ruff check src tests`、`uv run ruff format --check src tests`：通過。
- Docker（只用 CPU，一次一個檔案）：`tests/test_project_results_docker.py` 1 passed、`tests/test_model_docker.py` 2 passed，測試後沒有殘留容器。

### 6.1 接在 M11 之後（rebase 到 main `090b230`，2026-11-16）

M11（搶占）先合併，`attempts.preempted_by` 用了 migration 6，所以 `project_results` 改為 migration 7。

- `JobSpec` 同時有 M11 的 `retries_on_node_failure`、`safe_to_rerun`、`preemptible` 與本分支的 `project`。
  本分支測試裡的 `max_retries=1` 改成 `retries_on_node_failure=1, safe_to_rerun=True`。
- `test_a_version_6_database_gains_the_results_table` 從 M11 的版本 6 升級；版本 1 一路升級的測試也先刪掉 `project_results`。
- 新增 `test_a_preempted_run_continues_from_its_own_directory`：專案工作被搶占，Worker 確認停止並保留目錄後，
  這份目錄記為結果；搶占它的工作結束後，重跑的 attempt 的 START 帶著被搶占那個 attempt 的 `previous_attempt_id`。
  每個 PREEMPTED 結果都經過 `Scheduler._finish`，所以不需要另外的程式。

指令與結果：

- `uv run pytest -q`：465 passed、1 skipped（zsh）。
- `uv run ruff check src tests scripts`、`uv run ruff format --check src tests scripts`：通過。
- Docker（只用 CPU）：`tests/test_project_results_docker.py`、`tests/test_end_to_end_docker.py` 2 passed；`tests/test_preemption_docker.py`（非 GPU）1 passed。

## Q14 以 Linux 帳號登入（kayle，2026-11-16）

使用者決定：採用「登入一次、之後用 token」，任何帳號都能登入（architecture 3.2）。

- `tests/test_login.py`（11 個）：
  - Worker 代為申請的 token 能識別帳號；同一帳號再登入會多一個 token，舊的仍有效；資料庫裡沒有 token 明文。
  - 只有 Worker token 能申請；`@models`、空字串、含 `/` 或空白、超過 64 字元的名稱被拒絕。
  - 設定檔裡的 `alice` 與登入的帳號 `alice` 看到同一批工作。
  - Scheduler 重啟後 token 仍有效；舊版資料庫升級後有 `user_tokens`（rebase 到 project-results 之後為 migration 8）。
  - 設定的優先順序：預設值 < 登入檔 < 設定檔 < 環境變數；登入檔損壞時忽略。
  - CLI 經過真的 Worker socket 登入：檔案權限 600，`labflux whoami` 回傳目前的帳號。
  - 沒有 Worker 時，提示到節點上登入再複製檔案；socket 上的回答不是給自己的帳號，或 Scheduler 拒絕時，不存檔。
  - Worker 沒有開 `worker_login` 時不佔用 socket。
- 全部單元測試：384 passed、1 skipped（zsh）；`test_end_to_end_docker.py`、`test_fast_lane_e2e_docker.py`：4 passed。
- 實機操作（kayle，臨時 Scheduler 與 Worker，port 8791、socket `labflux-login-check`，不影響實驗室部署）：
  `labflux login` 印出 `Logged in as <自己的帳號>`，登入檔權限 600；`labflux whoami` 回傳同一個帳號；
  送出的工作 `owner` 是這個帳號，結果 SUCCEEDED。沒有 Worker 的 socket 名稱時，CLI 印出到節點登入的提示並以 1 結束。
- 修正：最初 Worker 先註冊才開 socket，全套測試在負載下撞到這個空檔（節點已上線但登入被拒）；改為註冊前先開好 socket。
- 尚未做：部署到實驗室、以其他帳號實際登入（Claude 無法切換成其他使用者）。

### Q14 審查修正（2026-11-16）

- 搶佔 socket：任何帳號都能先佔用 `labflux-login`，回答假的 `scheduler_url`；`/api/whoami` 也問同一個假位址，擋不住。
  CLI 改為向 kernel 查開 socket 的帳號（client 端的 `SO_PEERCRED`），只接受 root、自己或新設定 `login_worker_user`
  （預設 `labflux`），其他帳號拒絕、不存檔。Worker 開不了 socket 時改記 `error`，說明這台不能登入。
- 一次例外結束登入執行緒，名稱隨之空出：改為每個連線各自接住例外、記錄並回覆 `{"error": ...}`，繼續服務。
- `user_tokens` 無限增長：新設定 `login_tokens_per_user`（預設 20），發 token 時在同一個交易刪掉該帳號更舊的列，
  提交後從記憶體移除。
- docker 群組的成員能以任何 UID 連線，程式無法防止：在 lab-deploy 與 architecture 3.2 記為已知限制。
- `tests/test_login.py` 增為 15 個：別的帳號開的 socket 被拒絕；`login_worker_user` 的帳號開的被接受；
  一次登入出錯不影響下一次；上限 2 時登入 3 次，第一個 token 失效、後兩個有效，Scheduler 重啟後亦同。
- 全部單元測試：388 passed、1 skipped（zsh）；`test_end_to_end_docker.py`：1 passed。

### Q14 接在 M11 與 6.1 之後（rebase 到 main `c0ba149`，2026-11-16）

`user_tokens` 改為 migration 8（M11 的 6、6.1 的 7 之後）；`test_a_v7_database_gains_the_token_table` 從版本 7 升級，
版本 1 一路升級的測試也先刪掉 `user_tokens`。全部單元測試 480 passed、1 skipped（zsh）；
Docker（只用 CPU）：`test_end_to_end_docker.py`、`test_project_results_docker.py`、`test_preemption_docker.py`（非 GPU）、
`test_fast_lane_e2e_docker.py` 共 6 passed。

## 實驗室部署 main `8fcab7a` 與共用 CLI（2026-11-16）

- 部署前 kayle、karma 沒有工作；kindred 只有常駐的 gemma。Scheduler 資料庫先以 SQLite backup API 備份（schema 6）。
- 部署後三台 `ONLINE`；資料庫升級到 schema 8（`project_results`、`user_tokens`），34 個工作都保留；
  gemma 由新的 Worker 接手（同一個 attempt，狀態 serving）；三台的 Worker 都開著 `@labflux-login`。
- 共用 CLI 依 `docs/lab-deploy.md`「所有帳號共用的 CLI」由管理員裝在三台的 `/opt/labflux`；
  `find /opt/labflux ! -perm -o+r` 沒有結果。
- 實際登入（登入檔放在臨時目錄）：同一位使用者在 kayle 與 kindred 的 UID 不同，兩台 `labflux login` 都得到同一個身分，
  `labflux whoami` 相同，`labflux models` 看得到 gemma；登入 token 看到的工作與設定檔裡同名使用者的相同。
  karma 只確認了安裝與 socket（只有 `labflux` 帳號可用，沒有以它登入，避免多出一個使用者）。

## 最後驗證與交付

- `uv sync --locked`：通過。
- `uv run ruff check .`、`uv run ruff format --check .`、`git diff --check`：通過。
- `sg docker -c 'uv run pytest'`：**61 passed，0 skipped**；包含 8 個實際 Docker 測試，其餘為 API／排程／Worker／CLI／封存檔等測試。
- 三台 VM smoke：10 checks passed，使用 18 個工作；其中 OOM／超時為預期 FAILED，取消為預期 CANCELLED。
- 環境閒置時，三台 VM 正常關機、Scheduler 程序群組停止，再重新啟動及部署成功；未以此宣稱執行中故障恢復已完成。
- 本次完成 50 個編號 checklist 項目，對應 M0～M5、M6.1～M6.3。
- Git 僅追蹤程式、範例、規劃、測試與去除認證資料的驗收紀錄；私鑰、Token、VM 磁碟與即時資料目錄均被忽略。
