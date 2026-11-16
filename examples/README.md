# 範例

可以直接提交的工作、快速測試專案與 Gateway 客戶端。LabFlux 主要用來共用實驗室的 GPU，所以 GPU 的範例放在前面。
「實測」欄是 2026-11-16 在實驗室三台（kayle：RTX 3090 + RTX A5000；karma：RTX 4090；kindred：兩張 A40，被常駐的 gemma 占用）
各提交一次的結果，當時排程策略是 `fcfs`，程式與 main `bf86074` 相同。

在 repository 根目錄執行；`labflux` 是節點上所有帳號共用的 CLI（先 `labflux login`，見 `docs/lab-deploy.md`），
在自己的 checkout 裡也可以用 `uv run labflux --config 設定檔`。

## GPU 工作

需要 Worker 以 `gpus` 把卡納入資源池。`uv run --locked` 的工作用預設映像（Python 3.12 + uv），
torch 2.6.0（cu124）從節點的 uv 快取安裝；節點第一次執行要下載約 3 GB，所以 `--wait-timeout` 給 1800 秒。

| 範例 | 示範 | 提交 | 實測 |
|---|---|---|---|
| `jobs/gpu-model.json` | 以 `gpu_model` 指定型號（名稱的子字串，不分大小寫） | `labflux submit examples/jobs/gpu-model.json --source examples/uv-gpu --wait --wait-timeout 1800` | karma 的 4090，4.8 秒，GPU 與 CPU 的矩陣乘法結果一致 |
| `jobs/gpu-vram.json` | 以 `min_vram_mib` 要求至少 20 GiB 的卡（實驗室每張卡都符合，改成 24570 只剩 3090） | 同上，換成 `gpu-vram.json` | 前兩個工作占滿可用的卡時依 FCFS 等待，之後分到 4090，6.6 秒 |
| `jobs/gpu-2.json` | 一個工作在同一台節點用兩張卡（`gpu: 2`） | 同上，換成 `gpu-2.json` | kayle 的 3090 與 A5000，5.4 秒；`devices` 列出兩張卡，兩張都算過矩陣乘法 |
| `jobs/uv-gpu.json` | 最小的 GPU 工作，不指定型號（依小卡優先選卡） | 同上，換成 `uv-gpu.json` | 由 `scripts/lab_smoke.py` 經 CLI 提交：karma 的 4090，提交到完成 6.1 秒 |
| `jobs/mnist.json` | MNIST 訓練 3 個 epoch，模型與 `metrics.json` 寫到 `/outputs` | `labflux submit examples/jobs/mnist.json --source examples/mnist --wait --wait-timeout 1800` | 見下方「其他範例的實測」 |
| `jobs/preemptible-gpu.json` | 允許被搶占的 GPU 訓練：優先權 2、`safe_to_rerun` + `preemptible`、`estimated_runtime_s` | `labflux submit examples/jobs/preemptible-gpu.json --source examples/mnist` | kayle 的 3090，10 個 epoch 128 秒，準確率 99.18%（沒有人搶，跑完一次） |
| `jobs/priority-high-gpu.json` | 優先權 8、至少 20 GiB 的 GPU 工作；在 `priority`／`backfill` 下可以搶占優先權較低的可搶占 GPU 工作 | `labflux submit examples/jobs/priority-high-gpu.json --source examples/uv-gpu --wait --wait-timeout 1800` | kayle 的 3090，5.6 秒（`fcfs`，沒有搶占） |
| `jobs/retry-on-node-failure.json` | 節點失聯且容器確認不存在時自動重跑一次（`retries_on_node_failure` + `safe_to_rerun`），占著一張 ≥ 20 GiB 的卡 120 秒 | `labflux submit examples/jobs/retry-on-node-failure.json` | kayle 的 A5000，120 秒完成；節點故障的重跑要停掉 Worker 才看得到，見 `docs/verification.md` M6.5～M6.7 |

- `examples/uv-gpu/main.py` 印出 torch／CUDA 版本、看得到的卡（`devices`）並在每張卡上做一次矩陣乘法，結果寫到 `/outputs/result.json`。
- 有 `--source` 的工作會自動帶專案名稱（目錄名稱），工作目錄保留在節點上，下一次同專案的工作從那裡接著開始
  （README「專案目錄與接續」）。MNIST 的資料因此只在每台節點第一次下載。
- 被搶占的條件：`fcfs` 下只有互動 session 與模型服務的啟動會搶占；批次工作搶占要 `priority` 或 `backfill` 策略，
  而且優先權要比被搶的高（README「搶占」）。session 搶占可搶占 GPU 工作的實測見 `scripts/lab_smoke.py`。

## CPU 與 RAM 工作

`sleep-*` 只占預約、不真的燒 CPU 或吃 RAM，適合在共用機器上示範排隊與配置；`oom`、`timeout` 示範限制。

| 範例 | 示範 | 提交 | 實測 |
|---|---|---|---|
| `jobs/hello.json` | 上傳程式、日誌、取回 `/outputs`（指定 `python:3.12-slim`） | `labflux submit examples/jobs/hello.json --source examples/workloads/hello --wait` | karma，1.2 秒；`labflux fetch` 取回 `result.txt` |
| `jobs/sleep-cpu.json` | 預約 8 核、4 GiB 60 秒（`estimated_runtime_s` 60、`time_limit_s` 180） | `labflux submit examples/jobs/sleep-cpu.json` | karma，60.2 秒 |
| `jobs/sleep-memory.json` | 預約 40 GiB RAM：實驗室只有 kayle 放得下（karma 24 GiB，kindred 被 gemma 占了 48 GiB） | `labflux submit examples/jobs/sleep-memory.json` | kayle，60.2 秒 |
| `jobs/priority-high.json` | 優先權 9（`priority` 與 `backfill` 才使用；`fcfs` 不看） | `labflux submit examples/jobs/priority-high.json` | karma，60.2 秒 |
| `jobs/preemptible.json` | 允許被搶占的 4 核工作（優先權 2），讓 session 只停一個小工作 | `labflux submit examples/jobs/preemptible.json` | kayle，300 秒（沒有人搶，跑完一次） |
| `jobs/oom.json` | 超過 64 MiB 上限被 OOM 終止 | `labflux submit examples/jobs/oom.json --wait` | 見下方 |
| `jobs/timeout.json` | 超過 2 秒時限，整個容器被停止 | `labflux submit examples/jobs/timeout.json --wait` | 見下方 |

數字（核數、RAM、時間）直接改 JSON；欄位說明見 README「工作與資料」。

## 快速測試（fast lane）

| 範例 | 示範 | 執行 | 實測 |
|---|---|---|---|
| `fast-lane-gpu/` | 開發機寫程式，`pytest` 與訓練的煙霧測試在拿著一張 GPU 的 session 裡執行；結果檔寫回本機 | 複製到自己的目錄後，在裡面 `labflux run -- uv run pytest -q`、`labflux run -- python train.py --smoke`；或 `labflux shell` 後直接打 `pytest`、`python train.py --smoke`；用完 `labflux session close` | session 排到 karma 的 4090；第一個指令（租用 session、從快取 `uv sync`、3 個測試通過，包含「只看得到一張卡」）8.3 秒；`train.py --smoke` 2.4 秒，`smoke-result.json` 寫回本機 |
| `fast-lane-demo/` | 只用 CPU、在本機 loopback 走一次檔案流程（寫回、只在遠端的大檔、Ctrl-C、超時、閒置到期） | 依 `docs/demo-fast-lane.md` | 2026-11-16 在 kayle 照文件跑過 |

`fast-lane-gpu/.labflux.toml` 設定 `gpu = 1`；要指定型號就打開 `gpu_model`。session 閒置 10 分鐘才回收，
期間一直占著那張卡，所以用完請 `labflux session close`。

## 模型服務與 Gateway

| 範例 | 示範 | 執行 | 實測 |
|---|---|---|---|
| `gateway/chat.py` | 只用標準函式庫（Python 3.8 以上）送到 `/v1` Gateway：列出模型、一般回應、串流 | `python3 examples/gateway/chat.py --list`；`python3 examples/gateway/chat.py "問題"`；`python3 examples/gateway/chat.py --stream "問題"` | gemma4-31b（kindred，常駐）：一般回應 0.73 秒；串流第一段 0.05 秒、完成 0.46 秒；兩個串流同時送出時 `labflux models` 顯示進行中 2，兩個都在約 2.3 秒完成 |
| `configs/lab.example.toml` 的 `[models."small-chat"]` | 自動釋放（`on_demand`）的模型設定：`cold_wait_s`、`startup_timeout_s`、`idle_stop_s`、`reclaim_idle_s`、`retry_after_failure_s`；同一檔案的 `[models."qwen2.5-14b"]` 是常駐（`persistent`） | 放進自己的 `configs/local-lab.toml` 後 `lab_deploy.py deploy --node kayle`（見 `docs/lab-deploy.md`「模型服務」） | 同樣的欄位以 `fake-model` 在本機 loopback 實測，見下方 |
| `fake-model/server.py` | 只用標準函式庫的 CPU 假模型（OpenAI 相容），供測試或沒有 GPU 時登錄成模型服務 | 見檔案開頭的說明；設定方式見 `docs/lab-deploy.md`「模型服務」 | `tests/test_model_docker.py` |

`chat.py` 的位址與金鑰取自 `OPENAI_BASE_URL`、`OPENAI_API_KEY`，沒有設定時取自 `labflux login` 存的 `~/.config/labflux/login.toml`。
任何 OpenAI 相容的客戶端也可以直接用：

```bash
export OPENAI_BASE_URL=http://192.0.2.10:8770/v1   # 不是 LabFlux 節點時改成 SSH 轉送的 http://localhost:8770/v1
export OPENAI_API_KEY=<login.toml 裡的 user_token>
curl -sN $OPENAI_BASE_URL/chat/completions -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model": "gemma4-31b", "stream": true, "messages": [{"role": "user", "content": "Hello"}]}'
```

**自動釋放模型的實測（本機，2026-11-16）。** 實驗室目前只有常駐的 gemma，改模式要重新部署 kayle，所以 `on_demand` 的欄位改在本機
loopback 以臨時的 Scheduler、Worker 與 `fake-model`（`python:3.12-slim`，假裝啟動 5 秒）驗證，設定 `cold_wait_s = 30`、`idle_stop_s = 20`：

- 一開始 `labflux models` 顯示 `stopped`；第一個 `chat.py` 請求觸發冷啟動，等模型通過健康檢查後回應，共 6.6 秒；狀態變成 `serving`。
- 之後的串流請求 0.16 秒完成；最後一個請求結束 20 秒後模型自己停止，回到 `stopped`。
- 把 `cold_wait_s` 改成 0：請求立即得到 `HTTP 503 (retry after 10 s)`，模型照樣開始啟動；8 秒後再送就成功。

實際模型的冷啟動要算上權重載入：gemma4-31b 在 kindred 兩張 A40 上量到約 240～306 秒（`docs/verification.md` 10.10），
所以 `cold_wait_s`、`startup_timeout_s` 至少要給 300 秒以上；`chat.py` 預設等 900 秒。

## 其他範例的實測

同一天另外提交既有的三個範例：

| 範例 | 實測 |
|---|---|
| `jobs/oom.json` | karma，0.2 秒後 `FAILED`，原因 `memory limit exceeded (OOM)` |
| `jobs/timeout.json` | karma，印出 `running` 後在 2 秒時限 `FAILED`（`time limit exceeded`） |
| `jobs/mnist.json` | kayle 的 A5000，20.8 秒，準確率 98.91%；`schedule_note` 寫 `starts from the files of job 9df4a2cf`：沿用同一台上同專案（`mnist`）的 `preemptible-gpu.json` 留下的資料，沒有重新下載（程式總時間 13.4 秒，前一次含下載 120.8 秒） |

修正前，被 OOM 終止的工作偶爾會被記成 `FAILED`、原因 `completed`、`oom_killed: false`（exit code 仍是 137）；
已在 `runtime.py` 修正，Worker 重新部署後生效，見 `docs/verification.md` 的 M14 一節。

## 展示分鏡各幕會用到的工作類型（給 14.3）

M14 的展示分鏡（9 幕）用到的工作類型與可以拿來改的範例；展示本身（14.3）已經有自己的工作檔，這裡是通用的對照。展示以 GPU 為主（使用者決定）。這裡只列通用的類型：
排程是 best fit、同分時依節點名稱，各幕實際的大小、時限與送出順序要先預演，
以 14.3 的 [docs/demo.md](../docs/demo.md)、`examples/demo/`、`scripts/demo.py`，以及分鏡測試 `tests/test_storyboard.py`、
`tests/test_storyboard_gpu.py`（第 2、3 幕的 GPU 版）、`tests/test_storyboard_model.py`（模型服務）、`tests/test_demo_scenes.py` 為準。

| 幕 | 需要的工作類型 | 可以改的範例 | 前提或依賴 |
|---|---|---|---|
| 0 開場 | 不送工作；看三台節點、GPU 卡與模型 | `labflux nodes`、`labflux models`、`gateway/chat.py --list` | Dashboard 的 GPU 卡課表（M12） |
| 1 工作抵達與異質配置 | 指定型號的 GPU 工作、VRAM 下限的 GPU 工作、兩張卡的工作；CPU／RAM 工作為輔 | `gpu-model.json`、`gpu-vram.json`、`gpu-2.json`、`sleep-memory.json`、`sleep-cpu.json` | 配置說明（M12） |
| 2 優先權插隊與公平性 | 高優先權的 GPU 工作；兩位使用者各一個現在拿不到卡的普通優先權 GPU 工作 | `priority-high-gpu.json`、`gpu-vram.json`（加 `priority`）或 sleep 的 GPU 工作（同 `retry-on-node-failure.json` 的寫法） | `priority` 或 `backfill` 策略（要重新部署 kayle）；兩個使用者身分；`tests/test_storyboard_gpu.py` |
| 3 backfill | 硬性時限不同（例如 2 分、30 分）的 GPU 工作，加一個要等卡的高優先權隊首 | sleep 的 GPU 工作（改 `time_limit_s`）、`priority-high-gpu.json` | `backfill` 策略；保留時段的顯示（M12）；`tests/test_storyboard_gpu.py` |
| 4 搶占 | 低優先權、可搶占的 GPU 工作占滿卡；高優先權、VRAM 下限的 GPU 工作 | `preemptible-gpu.json`（或 sleep 的可搶占 GPU 工作）、`priority-high-gpu.json` | 批次搶占要 `priority` 或 `backfill` |
| 5 fast lane | 開發機在 `labflux shell` 裡跑測試與煙霧訓練，session 拿一張卡；叢集上有可搶占的 GPU 工作 | `fast-lane-gpu/`、`preemptible-gpu.json` | `fcfs` 下 session 也能搶占（已實測，`scripts/lab_smoke.py`） |
| 6 模型服務 | (a) 兩位使用者同時發串流請求 (b) 自動釋放模型的冷啟動、閒置停止與交還 GPU | (a) 兩個 `gateway/chat.py --stream` (b) gemma 改成 `on_demand`（設定寫法見 `configs/lab.example.toml` 的 `small-chat`），用 `chat.py` 發第一個請求 | (b) 改模式要重新部署 kayle（使用者決定時機）；gemma 冷啟動約 5 分鐘，`cold_wait_s` 要夠長 |
| 7 Worker 故障與恢復 | (a) 任何執行中的工作 (b) 可在別台重跑的 GPU 工作 | (a) 任一個 `sleep-*` (b) `retry-on-node-failure.json` | 要停止 karma 的 Worker，需使用者同意 |
| 8 和 FCFS 比較 | 不送工作 | — | M13 的對照結果：[docs/experiments.md](../docs/experiments.md)、[總結圖](../docs/evidence/m13/figures/summary_16x9.png) |
