# 展示腳本（M14.3）

這份文件是實驗室現場展示的操作手冊。展示以 GPU 為主，在 kayle、karma、kindred 三台機器上，
策略是 backfill（優先權、公平性加上 EASY backfill），由兩個展示用的使用者 alice 與 bob 送出工作。
gemma4-31b 是 `on_demand`，展示全程保持停止（模型冷啟動不在現場流程）。M13 的比較結果放在簡報最後的備用頁。
「優先權改變」以「高優先權工作抵達」呈現，因為目前沒有修改排隊中工作優先權的指令（決策 7）。

- 各幕的工作與步驟在 `examples/demo/<幕>/`：每個 `scene.toml` 依序列出誰送出哪個工作檔、要等多久，以及每一步觀眾該看到什麼。
  工作都用 `python:3.12-slim` 執行等待程式：只占住 CPU、RAM 與 GPU 的預約，不真的消耗共用機器的運算，而且會在時限前自己結束。
- `scripts/demo.py` 以 alice、bob 的身分執行各幕，也負責檢查環境、清場與 bob 的 keepalive。
- `tests/test_demo_scenes.py` 讓 `demo.py` 在測試環境裡，依同一批檔案和分鏡測試（`tests/test_storyboard*.py`）的假節點，把每一幕跑一遍。
  它會檢查每一幕要展示的決定：head_reserved、would_delay_reservation、backfilled、公平性排序、模型搶占、閒置回收。
  排程程式或展示檔案一改，展示會不會走樣，跑這個測試就知道。
- 現場流程搭配簡報（第 5 節）：開場前執行 Demo 0（`demo/0_start.sh`），報告中依序執行 Demo 1–5
  （`demo/1_priority.sh`、`2_backfill.sh`、`3_preemption.sh`、`4_fastlane.sh`、`5_worker_recovery.sh`）。
  編號和簡報右上角的 `LIVE DEMO · N` 相同。開場手打的 GPU 工作、第 6c、6a 幕已取消；第 1 幕、第 6b 幕和第 7 幕 (b) 是備用（第 7 節）。

## 1. 前置條件

- 三台的 Worker 都是 `ONLINE`，部署的版本和主講終端機的 repo 是**同一個 commit**。
  不同版本的 CLI 不一定相容：例如較新的 `labflux submit` 會多送欄位，較舊的 Scheduler 會回 422。
- 三台節點的容量和預演時相同：kayle 16 核／48 GiB／RTX 3090＋RTX A5000，karma 16 核／24 GiB／RTX 4090，
  kindred 32 核／64 GiB／兩張 A40（卡 3、4）。`demo.py check` 會比對；容量不同時各幕的落點會改變，要重新預演。
- 三台都已經有 `python:3.12-slim` 映像，沒有的話以部署帳號先執行 `docker pull python:3.12-slim`。否則第一個工作要先下載映像，時間會對不上。
- 主講的終端機在一台 LabFlux 節點上（例如 kayle），位置是 repo 根目錄，已經執行過 `uv sync`，
  主講自己的帳號已經 `labflux login`（Demo 4 的 fast lane 用主講自己的身分，不影響 alice、bob 的用量）。8770 只接受 LabFlux 節點的連線。
- 筆電看 Dashboard：先 `ssh -L 8770:<kayle 的位址>:8770 kayle`，再打開 http://localhost:8770/dashboard ，
  用 **alice** 的 token 登入（自己的工作才能點開完整細節）。範圍選「所有人」，開「投影模式」，停在「排隊順序」分頁
  （「全部」分頁會列出 24 小時內所有的工作，包括實驗留下的）。
  投影機用 1920×1080 最好。1280×720 時保留時段的時間與模型名稱仍完整，較長的資源行會被截斷（滑鼠移上去看完整文字）。
- 實驗室機器與筆電的時鐘要同步：保留時段的倒數是用瀏覽器的時鐘算的。
- 先告訴實驗室的人：展示期間 gemma4-31b 會停下再冷啟動，請不要送請求。第 2–4 幕進行中只要有任何人呼叫 gemma，
  它就會在 kindred 冷啟動、占走兩張 A40，那幾幕就不是預演的樣子了。
- 第 7 幕要停止 karma 的 Worker：用 `docs/lab-deploy.md`「日誌」一節的 `$SSH`（部署金鑰與 known_hosts），終端機 C 裡要用 known_hosts 的絕對路徑（第 3 節）。

## 2. 展示用的 Scheduler 設定

在 `configs/local-lab.toml` 改這兩處（gemma 的 `image`、`argv`、`env`、`resources`、`datasets`、`shm_mib` 維持原樣）：

```toml
[scheduler]
users = ["<你自己的帳號>", "alice", "bob"]   # 保留原本的帳號，自己的 cli.toml 才繼續有效
policy = "backfill"

[models."gemma4-31b"]
mode = "on_demand"
startup_timeout_s = 600      # 冷啟動實測 238–306 秒
cold_wait_s = 600            # 第一個請求等冷啟動完成，不先回 503
idle_stop_s = 3600           # 展示中不會自己停；第 2–4 幕的保留時段和常駐時一樣
reclaim_idle_s = 60          # 有工作在等它的卡時，閒置 1 分鐘就回收（第 6b 幕）
retry_after_failure_s = 60   # 選用：冷啟動失敗後 1 分鐘就能重試（預設 5 分鐘）
```

接著執行 `uv run python scripts/lab_deploy.py deploy --node kayle`。這只會重啟 Scheduler，但進行中的 gemma 請求與快速測試指令會中斷。
alice、bob 的 token 會產生在 `.labflux/lab/scheduler.toml` 的 `[users]`。這兩個是 Scheduler 設定裡的展示用使用者，不是 Unix 帳號。

### 讓 gemma 先停下來

要讓觀眾看到冷啟動，展示開始前 gemma 必須是 `stopped`。只改 `mode` 和時間設定不算設定改變，所以執行中的 gemma 不會重啟。
而且原本以常駐模式啟動的 gemma 會一直保留常駐時的期限（一年），直到切換後有一個請求結束為止。
在那之前，它既不會在 `idle_stop_s` 後停止，有工作在等時也不會被回收（已在測試環境照程式驗證）。
所以部署後（約 T−15 分鐘）這樣做（`$D` 見第 3 節）：

1. `$D keepalive --once`：送一個請求，期限改成「現在＋`idle_stop_s`」。
2. `$D scene 6b`：在暫停處直接按 Enter。腳本等 gemma 閒置滿 65 秒後，以 alice 送出一個要 A40 的工作，gemma 隨即被回收（stopping → stopped）。
3. `$D reset`，再執行 `$D check`，確認看到 `gemma4-31b: on_demand, stopped`。

（另一種做法：把 `startup_timeout_s` 改成和目前不同的值，例如 660。這算設定改變，部署時執行中的 gemma 會立刻停止，之後等下一個請求才冷啟動。）

已經是 `on_demand` 的 gemma 被別人的請求叫醒時，閒置滿 `idle_stop_s`（1 小時）會自己停；等不及就照上面第 2、3 步做。
`check` 那一行的 `in flight` 不是 0、或 `last request` 才幾秒前，表示有人正在用，先請他停下，不要硬停。

**展示後**把 `mode` 改回 `"persistent"`（時間設定可留著，常駐模式不看 `idle_stop_s`、`reclaim_idle_s`），再部署一次。
gemma 停著的話，會自己啟動（約 5 分鐘）。要收回 alice、bob 的 token，就把他們從 `users` 移除並重新部署；`policy` 是否改回由你決定。

## 3. 終端機與 demo.py 的設定

`demo.py` 從 `--config` 指定的 TOML 讀 `scheduler_url` 與 `[users]` 裡 alice、bob 的 token。部署產生的 `.labflux/lab/scheduler.toml` 兩者都有，可以直接用。
也可以改用環境變數 `LABFLUX_SCHEDULER_URL`、`LABFLUX_ALICE_TOKEN`、`LABFLUX_BOB_TOKEN`，環境變數優先。
token 不要寫進 repo 或貼到投影片上。三個終端機都從 repo 根目錄開始。

| 終端機 | 用途 | 準備 |
|---|---|---|
| A 主講 | 開場的 GPU 工作、各幕、清場、檢查 | `D="uv run python scripts/demo.py --config .labflux/lab/scheduler.toml"`。不要設 `LABFLUX_USER_TOKEN`：`.venv/bin/labflux whoami` 要印出主講自己的帳號 |
| B bob 的 keepalive | 第 6c 幕開始，展示結束才停 | 同樣設好 `D`，先打好 `$D keepalive`，不要按 Enter |
| C bob 的快速測試、karma 的 Worker | 第 5、7 幕 | 見下方 |

終端機 C（`labflux whoami` 要印 `bob`，`$SSH $KARMA hostname` 要印 `karma`）：

```bash
P=.venv/bin/python
export PATH="$PWD/.venv/bin:$PATH"
export LABFLUX_SCHEDULER_URL=$($P -c 'import tomllib; print(tomllib.load(open(".labflux/lab/scheduler.toml", "rb"))["scheduler_url"])')
export LABFLUX_USER_TOKEN=$($P -c 'import tomllib; print(tomllib.load(open(".labflux/lab/scheduler.toml", "rb"))["users"]["bob"])')
SSH="ssh -i ~/.ssh/labflux_deploy -o IdentitiesOnly=yes -o UserKnownHostsFile=$PWD/.labflux/lab/known_hosts"
KARMA=labflux@<karma 的位址>        # inventory 裡 karma 的 host
rm -rf /tmp/lf-demo && cp -r examples/fast-lane-demo /tmp/lf-demo && cd /tmp/lf-demo
labflux whoami
$SSH $KARMA hostname
```

`LABFLUX_USER_TOKEN` 優先於 `labflux login` 存的 token，所以終端機 C 是 bob。`$SSH` 先展開成絕對路徑，`cd` 之後仍然可以用。

指令一覽：

- `$D check`：Scheduler 是否連得上、策略、節點是否 ONLINE、各節點空著的 GPU 卡、gemma 的模式與狀態、
  alice／bob 的 token、兩人近 60 分鐘的用量（bob 超過 0.5 分鐘會警告），以及兩人還沒結束的工作。
- `$D scene`：依現場順序列出各幕，備用的排在最後。`$D scene 3 --dry-run` 只印出步驟，不送出任何東西。
- `$D scene 3`：開始前先檢查這一幕的條件，例如 gemma 的狀態、有沒有殘留的工作、bob 的用量。不符合就停下並說明原因（`--force` 可以強制執行）。
  執行時依序送出工作並照設定等待，每一步都會印出觀眾該看到什麼（`>` 開頭的行）。遇到 PAUSE 時要手動操作，做完按 Enter；按 Ctrl-C 就在那裡結束這一幕。
- `$D reset`：取消 alice、bob 所有未結束的工作（包括 bob 的 session），一直等到每個停止都確認了才結束。
- `$D keepalive`：bob 每 20 秒對 gemma4-31b 送一個短請求，印出 HTTP 狀態碼。逾時設為 900 秒，所以第一個請求可以一直等到冷啟動完成。

## 4. 開場前檢查清單

T−20 分鐘，在終端機 A：

```bash
uv run python scripts/lab_deploy.py status        # 加上部署時用的 --inventory
$D check
nvidia-smi; $SSH labflux@<karma 的位址> nvidia-smi; $SSH labflux@<kindred 的位址> nvidia-smi -i 3,4
$D scene 2 --dry-run | grep wait                  # wait 60 s、wait 60 s、wait 15 s
```

- [ ] `status`：每台 `labflux-worker.service: active`，kayle 另有 `labflux-scheduler.service` 與 `labflux-firewall.service` active，Scheduler `healthy`。
      不是 active 的：`$SSH labflux@<那台> systemctl --user start labflux-worker.service`（kayle 的 Scheduler 是 `labflux-scheduler.service`），再看一次。
- [ ] `$D check` 全部 OK：policy backfill；三台 ONLINE；空著的 GPU 卡 kayle 2/2、karma 1/1、kindred 2/2；
      gemma4-31b `on_demand, stopped`；兩個 token 都正確；bob 的近期用量 0.0；沒有殘留工作。最後一行 `check passed`。
- [ ] `nvidia-smi`：五張卡都只用了幾十 MiB、0 %，沒有別人的程序。
- [ ] gemma 不是 `stopped`：照第 2 節「讓 gemma 先停下來」處理。
- [ ] bob 的用量大於 0，表示 60 分鐘內做過彩排：最好等它離開 60 分鐘的視窗。
      等不及的話，只要 alice 的用量不比 bob 少，第 2 幕的排序仍然成立，但分數會和稿子不同，要加 `--force`。
- [ ] Dashboard：以 alice 登入、「所有人」、投影模式、GPU 卡軸、「排隊順序」分頁；模型服務表顯示「自動釋放 · stopped · 下一個請求時啟動」。
- [ ] 終端機 A、B、C 照第 3 節準備好，`whoami` 和 `hostname` 都對。
- [ ] 實驗室的人知道這段時間不要呼叫 gemma。
- [ ] （選用）在這個版本上跑過 `uv run pytest tests/test_demo_scenes.py -q`，結果通過。

## 5. 現場流程（搭配簡報）

時間是簡報的時間。每一幕結束後在 A 執行 `$D reset`（約 10 秒）。「說」的重點在簡報的講者備註，完整台詞在 `demo.py` 每一步印出的 `>` 行。

第 8 頁講完後執行 `bash demo/2_backfill.sh`：腳本先清掉上一幕的 alice、bob 工作，再讓 kindred 的兩張 A40 各跑一個原始工作。
上方原始長工作的時限為 10 分鐘，下方原始短工作的時限為 3 分鐘，Dashboard 的「時限到」會顯示下方較早結束。
第一次 Enter 才送出指定兩張 A40 的大 Job 與指定一張 A40 的小 Job；送出後啟動原始短工作的 10 秒完成倒數。
倒數後工作自然完成，小 Job 自動 backfill 到剛空出的卡，大 Job 繼續保有保留時段。
最後停在「一個原始工作 + 小 Job」的畫面，第二次 Enter 清場；任何階段也可以按 Ctrl-C 結束並清場。
`bash demo/2_backfill.sh --dry-run` 只列出步驟，不連 Scheduler，也不清場。

2026-11-17 在 kindred 的兩張 A40 實測：第一個畫面停留超過 10 秒，兩個原始工作仍在執行；
第一次 Enter 後兩個新工作進入佇列，約 11 秒後原始短工作以 exit code 0 完成，小 Job 在原卡上 backfill，
大 Job 的保留時間沒有延後。第二次 Enter 後清場成功，alice、bob 沒有未結束工作，三個節點的配置資源均歸零。

第 9 頁講完後執行 `bash demo/3_preemption.sh`：Job1 先占住 kindred 上方的 A40，每 2 秒將進度寫入
`checkpoint.json`；Job1 開始後 5 秒才送入 Job2，占住下方的 A40。兩者都是優先權 2 且允許搶占重跑。
Job2 沒有 checkpoint，所以它的未保存進度從開始執行時算起。腳本等 Worker 回報 Job1 的新 checkpoint
後停在第一個 Enter，並印出兩者會損失的秒數。

- 第一次 Enter：送入優先權 8、指定一張 A40 的 Urgent。Job1 雖然先開始，但 checkpoint 比較近，
  因此成為損失較小的搶占對象。確認它的容器停止後，Urgent 才接手同一張卡。
- 第二幕：Job2 繼續執行，Job1 的 attempt 標成 `PREEMPTED`，工作回到 queue；再次 Enter 清場。
- 沿用現場的 Dashboard 即可操作：終端機會印出 Job1／Job2 對應的短 ID 與未保存進度秒數。
  程式碼已備妥新版顯示：部署 `views.py` 與 `dashboard.html` 後，卡片可顯示名稱及
  「距 checkpoint 0:02」或「未存 checkpoint · 已執行 0:20」。本次展示先使用終端機的比較數字。
- `bash demo/3_preemption.sh --dry-run` 可預覽步驟。這個腳本不使用部署 SSH 金鑰。

展示程式存檔時，以 Scheduler `/health` 的 HTTP Date 校準時間，再依 monotonic clock 記錄每次存檔的
mtime，避免節點時鐘偏差影響比較；每次都先寫入實際進度，再以原子替換完成 checkpoint。
2026-11-17 實測：Job1 距 checkpoint 約 3.6 秒、Job2 已執行約 20.7 秒，高優先權工作搶占 Job1；
Job1 回到 queue，Job2 與 Urgent 各占一張 A40。第二次 Enter 清場成功。

第 12 頁使用 `bash demo/5_worker_recovery.sh`，在 **kayle** 的 repo 執行，Dashboard 看 kayle 的 GPU 卡。
這個版本暫停本機 Worker 程序，讓 Docker 容器持續執行；不用部署 SSH 金鑰或更新服務。
開始時會要求一次 sudo 密碼，因為 Worker 由 `labflux` 帳號執行。控制助手會在建立 Job 前就完成授權。

1. 腳本先用一個極短工作建立本機專案，再讓正式 Job 沿用該專案檔案，優先落在 kayle。
   正式 Job 沒有指定 GPU 型號，也允許節點故障後重跑；其他有空卡的節點仍符合它的資源需求。
2. 第一幕顯示 Job 正在執行；**第一次 Enter** 暫停 Worker 程序。預設約 10 秒未回報心跳後，
   kayle 顯示 `OFFLINE`、Job 顯示 `UNKNOWN`，原 CPU／RAM／GPU 配置繼續保留。
3. 從確認 `OFFLINE / UNKNOWN` 起，維持完整 **10 秒**；終端機逐秒列出仍在增加的容器進度。
   腳本持續確認沒有新增 attempt、沒有釋放資源，也沒有其他節點接走這個 Job。
4. 自動恢復 Worker，原 instance 的心跳確認 attempt 還在執行，狀態回到 `ONLINE / RUNNING`。
   這是同一個 Worker 程序恢復，不是重啟程序；不必刻意等待短暫的 `RECONCILING` 畫面。
   終端機會確認 node、attempt、容器 ID 與容器程序 PID 都相同，進度持續累積。
5. **第二次 Enter** 結束，只取消本次腳本建立的工作並等待資源釋放。

`--dry-run` 可預覽流程。Ctrl-C、展示程序意外退出時，助手會先恢復 Worker；另外有 60 秒保底恢復。
開始前及暫停前都會確認 kayle 沒有其他工作配置，避免暫停別人的 Worker 控制流程。
驗證使用獨立 Scheduler、兩個真正的 Worker 程序與 Docker 容器，確認即使另一個 Worker 有空閒資源，
失聯期間仍保留原工作；預設 10 秒失聯門檻與之後 10 秒展示等待都按真實時間執行。
2026-11-17 另在實驗室驗證準備工作與正式 GPU Job 均落在 kayle，容器持續印出進度，
最後清場成功；此項定位檢查沒有暫停部署中的 Worker。部署中的 Worker 暫停由主講執行腳本時授權。

順序：Demo 1 需要五張卡都空著、bob 還沒有用量，所以排第一個；Demo 2、3 使用 kindred 的兩張 A40，
gemma 要停著、沒有人呼叫它。模型冷啟動已取消，gemma 全程保持 `stopped`。

| Demo | 投影片 | 指令 | Dashboard 上看到 |
|---|---|---|---|
| 0 | 開場前 | A：`demo/0_start.sh`，最後要印出 READY | |
| 1 | 第 6 頁 Where a Job Runs | A：`demo/1_priority.sh`（階段 1） | 五張卡各一個 alice 區塊；決策欄「放得下 kayle、kindred；選 kayle：剩餘的合適 GPU 最少」。按 Enter，換下一張 |
| 1 | 第 7 頁 Who Goes First | 階段 2：alice P5，5 秒後 bob P9，再 5 秒 bob P5；Enter | 排序 bob P9、bob P5、alice P5；分數拆解在每個分數下面 |
| | | 收尾：alice 取消一個工作；再按 Enter 結束（腳本自動清場） | 空出來的卡給 bob 的 P9 |
| 2 | 第 8 頁 Backfill | A：`bash demo/2_backfill.sh`；第一次 Enter | 兩張 A40 各跑一個原始工作；按 Enter 後，排隊表出現雙 A40 大 Job、一張 A40 小 Job；約 10 秒後原始短工作自然完成，小 Job 帶 backfill 標記先跑，大 Job 保有保留時段 |
| | | 講完後第二次 Enter | 自動取消本幕 alice、bob 的工作，確認停止後清場 |
| 3 | 第 9 頁 Preemption | A：`bash demo/3_preemption.sh` | Job1 先跑，每 2 秒存 checkpoint；5 秒後 Job2 進入、不存 checkpoint，兩張 A40 都被占住 |
| | | 第一次 Enter；講完後第二次 Enter | Urgent 搶占 checkpoint 較近的 Job1，Job2 繼續跑，Job1 回到 queue；最後清場 |
| 4 | 第 11 頁 Fast Lane | A：`demo/4_fastlane.sh`（以主講自己的帳號進入 `labflux shell`），打 `time python hello.py` 兩次，再 `exit`（腳本接著收掉 session） | karma 的 4090 出現 SESSION 區塊；第一次約 0.8 秒印出 `assigned to karma` 與 `GPU: NVIDIA GeForce RTX 4090`，第二次約 0.3 秒、容器 ID 相同；`exit` 後 SESSION 區塊消失 |
| 5 | 第 12 頁 When a Machine Fails | A：`bash demo/5_worker_recovery.sh`；第一次 Enter | kayle 的 Worker 暫停回報；約 10 秒後 OFFLINE、Job UNKNOWN，資源保留；該畫面再維持 10 秒 |
| | | 自動恢復；講完後第二次 Enter | kayle ONLINE、同一個 Job／attempt／容器繼續跑；最後清場 |

## 6. 備援

- **gemma 冷啟動失敗**（健康檢查超過 600 秒，或容器結束）：模型服務表顯示「failed · N 後才會重試」，B 印出 503 或錯誤。
  B 的迴圈會繼續送請求，設了 `retry_after_failure_s = 60` 時，1 分鐘後的下一個請求就會重試。如果又失敗：
  - 略過 6a，講 Results。
  - 第 5、7 幕假設 gemma 占住 kindred，這時 `demo.py` 會顯示 NOT READY。可以加 `--force` 照常執行，但落點會不同，要照畫面講。
  - 展示後再看 kindred 的 Worker 日誌（`docs/lab-deploy.md`）。
- **冷啟動超過 600 秒**：第一個請求會收到 503（model_not_ready）。B 的迴圈 20 秒後再送，會接上同一個正在載入的服務，不用處理。
- **某一幕走樣**：在 A 按 Ctrl-C，執行 `$D reset` 與 `$D check`，換下一張投影片。
  第 2 幕只有在 bob 的用量仍接近 0 時才重跑，否則直接進下一幕。第 3、4 幕可以直接重跑。
- **demo.py 顯示 NOT READY**：照提示處理。有殘留工作就 `$D reset`；gemma 狀態不對，就照第 5 節的順序走；
  bob 的用量太高時加 `--force`（alice 的用量不比 bob 少，排序就仍然成立），或略過這一幕。
- **節點突然離線**：`$D check` 顯示 FAIL。用 `uv run python scripts/lab_deploy.py status` 確認後，依 `docs/lab-deploy.md` 重新啟動那台的 Worker。
  其他節點上的幕仍可進行，但落點可能不同。
- **第 7 幕 karma 的 Worker 沒回來**：再執行一次 start，並看它的日誌。在 Worker 回來之前，bob 的工作會維持 UNKNOWN 並保留資源，
  `$D reset` 會等到逾時並列出「still UNKNOWN」。
- **投影機解析度太低、畫面被截斷**：關掉投影模式，或把瀏覽器縮小（Ctrl −）。
  也可以改用 CLI：`labflux --config <alice 的設定> queue`、`labflux --config <alice 的設定> status --all`。
- **Fast lane 的 session 沒有啟動**：確認在 `demo/4_fastlane/` 裡（`python` 只在有 `.labflux.toml` 的專案裡才送到叢集）、`labflux whoami` 是自己，再看 `labflux session status`。結束時一定要收掉 session，否則它占著 4090，第 7 幕 bob 的工作落不到 karma。

## 7. 不在現場流程裡的幕

- **第 1 幕（工作抵達與異質配置）**：指定 4090、VRAM ≥ 20 GiB、40 GiB RAM 與三個 8 核工作的落點，約 2 分鐘。
  需要 gemma 在 starting 或 serving；有人問到異質配置時，在 6a 之後做。現場改用開場的 GPU 工作說明「依卡的型號送工作」。
- **第 6b 幕（閒置回收）**：關掉 B 的 keepalive，閒置滿 65 秒後 alice 要一張 A40，gemma 被回收。約 2 分鐘。
  現場不做；展示前用它讓 gemma 停下來（第 2 節）。
- **第 7 幕 (b)（容器不見才重跑）**：在 (a) 的 PAUSE 按 Enter，照畫面停止 Worker、刪掉 bob 工作的容器、再啟動。
  bob 的工作 FAILED 後換到 kayle 的 A5000，alice 拿到 4090。現場不做：要手打三個 SSH 指令，最容易出錯。

## 8. 彩排注意事項

- **第 2 幕需要 bob 的近期用量接近 0**（視窗是 60 分鐘）。其他幕的彩排會累積 bob 的近期用量。
  所以彩排要在正式展示前 60 分鐘以上做完，或者正式展示前只彩排不含 bob 的部分。`$D check` 會顯示 bob 的用量。
- **第 2 幕的時間**：alice 的 P5 剛好在她的五個工作開始後 2 分鐘送出，她的分數一直是 500 − 2，
  Dashboard 四捨五入後的拆解（500 + 0 − 2）才會加得起來。間隔不是整數分鐘時會出現「499 = 500 + 0 − 2」這種畫面，改時間時要注意。
- **第 3 幕由 Enter 送出排隊工作**：先確認兩個原始工作已在 A40 上執行，再按 Enter 送出大 Job 與小 Job。
  原始短工作透過 Scheduler API 等待本輪的小 Job，使用 alice 的展示 token 與每次重新產生的識別碼；
  小 Job 送出後，原始程式自行倒數 10 秒並正常退出，不需要部署 SSH 金鑰。第一個畫面可停留解說，倒數從按 Enter 後啟動。
  腳本確認小 Job 已 RUNNING，才停下來讓講者解說。
  兩個新 Job 都指定 A40，GPU 軸只需看 kindred。小 Job 的硬性時限 2 分鐘，早於大 Job 約 10 分鐘的保留時段。
  現場可說「原始工作完成後，空出一張卡；大工作仍需要兩張，所以小工作利用空檔先跑。」最後第二次 Enter 清場。
- **GPU 卡是預設的軸**：只用 CPU 的工作不會畫在上面，例如第 1 幕的 CPU／RAM 工作。
  要看就切到「CPU 核心」或「RAM」；重新整理頁面會回到 GPU 卡。
- 第 2–4 幕進行中不要對 gemma 送任何請求，連測試用的 curl 也不行。
- 從 6c 到展示結束，B 的 keepalive 要一直開著。有工作在等、又 60 秒沒有請求時，gemma 會被回收，下一個請求又要冷啟動 5 分鐘。
- 第 6b 幕要閒置滿 65 秒才送工作（`demo.py` 會等）。太早送的話，那一分鐘裡工作會顯示一小時後的保留時段（已知限制）。
- 第 7 幕 alice 的工作優先權是 6。(b) 中 bob 重跑的工作和 alice 的工作會同時排隊；如果兩者優先權相同，順序就看近期用量，
  而到第 7 幕時 alice 用得比 bob 多很多，bob 的工作會排在前面，又把 karma 的 4090 拿回去。
- 部署或重啟 Scheduler 後，排隊表要等第一輪排程才有內容，決策欄和「已服務」的計數會從零開始。
- 彩排或展示後 gemma 還在服務的話，閒置 1 小時會自己停；要馬上停就照第 2 節做。
- 排程程式有任何修改，就跑 `uv run pytest tests/test_demo_scenes.py tests/test_storyboard.py tests/test_storyboard_gpu.py tests/test_storyboard_model.py -q`。
