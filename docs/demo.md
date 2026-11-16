# 展示腳本（M14.3）

這份文件是實驗室現場展示的操作手冊。展示以 GPU 為主，在 kayle、karma、kindred 三台機器上，
策略是 backfill（優先權、公平性加上 EASY backfill），由兩個展示用的使用者 alice 與 bob 送出工作。
gemma4-31b 改成 `on_demand`，讓觀眾看到冷啟動。第 8 幕放 M13 的比較圖。
「優先權改變」以「高優先權工作抵達」呈現，因為目前沒有修改排隊中工作優先權的指令（決策 7）。

- 各幕的工作與步驟在 `examples/demo/<幕>/`：每個 `scene.toml` 依序列出誰送出哪個工作檔、要等多久，以及每一步觀眾該看到什麼。
  工作都用 `python:3.12-slim` 執行 `sleep`：只占住 CPU、RAM 與 GPU 的預約，不真的消耗共用機器的運算，而且會在時限前自己結束。
- `scripts/demo.py` 以 alice、bob 的身分執行各幕，也負責檢查環境、清場與 bob 的 keepalive。
- `tests/test_demo_scenes.py` 讓 `demo.py` 在測試環境裡，依同一批檔案和分鏡測試（`tests/test_storyboard*.py`）的假節點，把每一幕跑一遍。
  它會檢查每一幕要展示的決定：head_reserved、would_delay_reservation、backfilled、公平性排序、模型搶占、閒置回收。
  排程程式或展示檔案一改，展示會不會走樣，跑這個測試就知道。
- 完整版約 28 分鐘；略過第 4 幕與第 7 幕 (b) 約 24 分鐘。精簡版約 10 分鐘（第 7 節）。

## 1. 前置條件

- 實驗室跑的是含 M12 的版本，三台的 Worker 都是 `ONLINE`。每台的共用 CLI 也已更新（第 5 幕的 `labflux shell` 會用到）。
- 三台節點的容量和預演時相同：kayle 16 核／48 GiB／RTX 3090＋RTX A5000，karma 16 核／24 GiB／RTX 4090，
  kindred 32 核／64 GiB／兩張 A40（卡 3、4）。`demo.py check` 會比對；容量不同時各幕的落點會改變，要重新預演。
- 三台都已經有 `python:3.12-slim` 映像，沒有的話以部署帳號先執行 `docker pull python:3.12-slim`。否則第一個工作要先下載映像，時間會對不上。
- 主講的終端機在一台 LabFlux 節點上（例如 kayle），位置是 repo 根目錄，已經執行過 `uv sync`。8770 只接受 LabFlux 節點的連線。
- 筆電看 Dashboard：先 `ssh -L 8770:<kayle 的位址>:8770 kayle`，再打開 http://localhost:8770/dashboard ，
  用 **alice** 的 token 登入（自己的工作才能點開完整細節）。範圍選「所有人」，開「投影模式」。
  投影機用 1920×1080 最好。1280×720 時保留時段的時間與模型名稱仍完整，但 UNKNOWN 區塊的「節點失聯,資源保留」與較長的資源行會被截斷（滑鼠移上去看完整文字）。
- 實驗室機器與筆電的時鐘要同步：保留時段的倒數是用瀏覽器的時鐘算的。
- 先告訴實驗室的人：展示期間 gemma4-31b 會停下再冷啟動，請不要送請求。第 2–4 幕進行中只要有任何人呼叫 gemma，
  它就會在 kindred 冷啟動、占走兩張 A40，那幾幕就不是預演的樣子了。
- 第 7 幕要停止 karma 的 Worker：準備好 `docs/lab-deploy.md`「日誌」一節的 `$SSH`（部署金鑰與 known_hosts）。
- 第 8 幕的 M13 比較圖放在投影片裡。

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
2. `$D scene 6b`：在暫停處直接按 Enter。腳本等 gemma 閒置滿 65 秒後，以 alice 送出一個要 A40 的工作，gemma 隨即被回收（stopping → stopped）。這一步同時是第 6b 幕的彩排。
3. `$D reset`，再執行 `$D check`，確認看到 `gemma4-31b: on_demand, stopped`。

（另一種做法：把 `startup_timeout_s` 改成和目前不同的值，例如 660。這算設定改變，部署時執行中的 gemma 會立刻停止，之後等下一個請求才冷啟動。）

**展示後**把 `mode` 改回 `"persistent"`（時間設定可留著，常駐模式不看 `idle_stop_s`、`reclaim_idle_s`），再部署一次。
gemma 停著的話，會自己啟動（約 5 分鐘）。要收回 alice、bob 的 token，就把他們從 `users` 移除並重新部署；`policy` 是否改回由你決定。

## 3. 終端機與 demo.py 的設定

`demo.py` 從 `--config` 指定的 TOML 讀 `scheduler_url` 與 `[users]` 裡 alice、bob 的 token。部署產生的 `.labflux/lab/scheduler.toml` 兩者都有，可以直接用。
也可以改用環境變數 `LABFLUX_SCHEDULER_URL`、`LABFLUX_ALICE_TOKEN`、`LABFLUX_BOB_TOKEN`，環境變數優先。
token 不要寫進 repo 或貼到投影片上。

| 終端機 | 用途 | 準備 |
|---|---|---|
| A 主講 | 各幕、清場、檢查 | repo 根目錄：`D="uv run python scripts/demo.py --config .labflux/lab/scheduler.toml"` |
| B bob 的 keepalive | 第 6c 幕開始，第 6b 幕停止 | repo 根目錄，同樣設好 `D`，先打好 `$D keepalive`，不要按 Enter |
| C bob 的快速測試 | 第 5 幕 | 在 repo 根目錄：`cp -r examples/fast-lane-demo /tmp/lf-demo; export PATH="$PWD/.venv/bin:$PATH" LABFLUX_SCHEDULER_URL=<與 scheduler.toml 相同> LABFLUX_USER_TOKEN=<bob 的 token>; cd /tmp/lf-demo` |

指令一覽：

- `$D check`：Scheduler 是否連得上、策略、節點是否 ONLINE、各節點空著的 GPU 卡、gemma 的模式與狀態、
  alice／bob 的 token、兩人近 60 分鐘的用量（bob 超過 0.5 分鐘會警告），以及兩人還沒結束的工作。
- `$D scene`：依展示順序列出各幕。`$D scene 3 --dry-run` 只印出步驟，不送出任何東西。
- `$D scene 3`：開始前先檢查這一幕的條件，例如 gemma 的狀態、有沒有殘留的工作、bob 的用量。不符合就停下並說明原因（`--force` 可以強制執行）。
  執行時依序送出工作並照設定等待，每一步都會印出觀眾該看到什麼（`>` 開頭的行）。遇到 PAUSE 時要手動操作，做完按 Enter。
- `$D reset`：取消 alice、bob 所有未結束的工作（包括 bob 的 session），一直等到每個停止都確認了才結束。
- `$D keepalive`：bob 每 20 秒對 gemma4-31b 送一個短請求，印出 HTTP 狀態碼。逾時設為 900 秒，所以第一個請求可以一直等到冷啟動完成。

## 4. T−10 分鐘檢查清單

- [ ] `$D check` 全部 OK：policy backfill；三台 ONLINE；空著的 GPU 卡 kayle 2/2、karma 1/1、kindred 2/2；
      gemma4-31b `on_demand, stopped`；兩個 token 都正確；bob 的近期用量 0.0；沒有殘留工作。
- [ ] bob 的用量大於 0，表示 60 分鐘內做過彩排：最好等它離開 60 分鐘的視窗。
      等不及的話，只要 alice 的用量不比 bob 少，第 2 幕的排序仍然成立，但分數會和稿子不同，要加 `--force`。
- [ ] Dashboard：以 alice 登入、「所有人」、投影模式、GPU 卡軸；模型服務表顯示「自動釋放 · stopped · 下一個請求時啟動」。
- [ ] 終端機 A、B、C 照第 3 節準備好；第 7 幕要用的 `$SSH` 指令也準備好。
- [ ] 實驗室的人知道這段時間不要呼叫 gemma。
- [ ] （選用）在這個版本上跑過 `uv run pytest tests/test_demo_scenes.py -q`，結果通過。

## 5. 逐分鐘流程

時間從開始講算起。每一幕結束後都在 A 執行 `$D reset`（約 10 秒）。「說」欄是重點，完整台詞在 `demo.py` 每一步印出的 `>` 行。

順序是由 gemma 決定的：

- 第 2、3、4 幕需要五張卡都空著，也就是 gemma 停著、沒有人呼叫它，所以排在最前面。第 2 幕另外需要 bob 還沒有用量，所以排第一個。
- 接著第 6c 幕讓 gemma 冷啟動。它載入的 4–5 分鐘，正好用來做第 1、5、7 幕：這幾幕本來就假設 gemma 占著 kindred 的兩張 A40、8 核與 48 GiB。
- 等 gemma 開始服務後做第 6a 幕，最後第 6b 幕讓它閒置回收。

`$D scene` 會依這個順序列出各幕，每一幕開始前也會檢查 gemma 的狀態是否符合。

| 時間 | 幕 | 指令 | 說 | Dashboard 上看到 | 軸 |
|---|---|---|---|---|---|
| 0:00 | 0＋2 開場、優先權與公平性 | A：`$D scene 2` | 三台機器、backfill 策略、兩個使用者。分數 = 優先權×100 ＋ 等待分鐘 − 近 60 分鐘用量。alice 先把五張卡占滿 | header 策略 backfill；五張卡各一個 alice 區塊；模型服務表 gemma stopped；誰在用表 alice | GPU 卡 |
| 4:00 | 2 | （自動）bob P9 抵達 | 優先權改變就是高優先權工作抵達 | 排隊表第 1 列 bob head_reserved 900＋0−0；原因 GPU 需要 1 張、karma 空 0／占用 1；欄頭顯示 karma 的保留時段 | GPU 卡 |
| 5:00–6:00 | 2 | （自動）alice P5，一分鐘後 bob P5 | 送出後馬上講：alice 先送、多等 1 分，但近 60 分鐘用了 6 分 | 排序 bob P9 902、bob P5 500、alice P5 495；誰在用表 alice 6、bob 0 | GPU 卡 |
| 6:30 | 2 | A：Enter | 放出一張卡，先給隊首 | bob P9「等 kayle 上的工作停止」→ 落在 kayle A5000；bob P5 成為隊首 | GPU 卡 |
| 8:00 | 3 backfill | A：`$D reset`；`$D scene 3` | alice 占住三張 24 GiB 的卡；bob 的 18 核工作只放得下 kindred | 三個 alice 區塊；切 CPU 核心軸看 kindred 上 18 核的區塊，再切回來 | GPU 卡 → CPU 核心 → GPU 卡 |
| 10:00 | 3 | （自動）bob 要兩張 A40 的 P9 工作、alice 30 分與 2 分的工作，三個接連送出 | 隊首依執行中工作的時限保留 kindred；短的在保留時段前會結束，所以先跑；長的會延後保留時段，所以等 | kindred 欄頭「保留給 bob 的工作」；2 分鐘工作標 backfill；30 分鐘工作 would_delay_reservation；隊首原因變成 GPU 需要 2 張 A40、空 1 | GPU 卡 |
| 11:40–12:50 | 3 | （自動）倒數 | FCFS 下兩個都要等，第 8 幕看圖 | 2 分鐘工作結束；18 核工作結束；隊首落在兩張 A40；30 分鐘工作成為 head_reserved | GPU 卡 |
| 13:30 | 4 搶占（可略過） | A：`$D reset`；`$D scene 4` | 只有標了可搶占的工作會被停；選損失最少的 | alice 五個 P2 區塊；kindred 第二張 A40 變虛線「被 … 搶占 · 停止中」；bob 的區塊落下；alice 的工作「被搶占 1 / 3 次」成為隊首 | GPU 卡 |
| 15:30 | 6c 冷啟動與搶占 | A：`$D reset`；`$D scene 6c`；暫停時在 B 執行 `$D keepalive`，再回 A 按 Enter | 第一個請求啟動模型；模型屬於互動工作，可以搶占可搶占的批次工作 | 排隊表 MODEL「隊首。搶占 kindred 的 …,等它們停止後開始。」；決策欄「gemma4-31b 搶占」；約 10 秒後 MODEL 區塊占兩張 A40（starting · 啟動 0:10），模型服務表「啟動 10 秒」計時 | GPU 卡 |
| 17:00 | 1 抵達與異質配置 | A：`$D reset`；`$D scene 1` | 載入要 4–5 分鐘，先看別的；每個工作落在放得下的節點 | 4090 → karma；VRAM ≥ 20 GiB → kayle A5000；40 GiB RAM → kayle（kindred 被 gemma 占了 48 GiB）；8 核工作分散；點開看配置說明 | GPU 卡 → CPU 核心 → RAM |
| 19:00 | 5 fast lane | A：`$D reset`；`$D scene 5`；暫停時在 C 執行 `labflux shell`、`python hello.py`（兩次） | 互動指令排在批次之前，必要時搶占小的可搶占工作；第二次沿用同一個容器 | CPU 全滿；session 排第 1「互動工作優先」；SESSION 區塊在 kayle（閒置倒數）；alice 的 4 核工作重新排隊 | CPU 核心 |
| 約 21:00 | — | B 印出 200 | 冷啟動完成；之前在等的請求這時才回應 | 模型服務表 serving；已服務每分鐘約 +3 | GPU 卡 |
| 21:00 | 7 Worker 故障 | C：`exit`；A：`$D reset`；`$D scene 7`；暫停時照畫面上的 `$SSH` 指令停止、啟動 karma 的 Worker | 失聯的節點保留資源、不重派，避免同一個工作跑兩次；(b) 容器不見才重跑 | karma 欄紅色斜線「失聯」；bob 的區塊 UNKNOWN；alice 的工作「沒有在線的節點放得下」；恢復時 RECONCILING → ONLINE。(b)：FAILED → kayle A5000，alice 拿到 4090 | GPU 卡 |
| 24:00 | 6a 共用 | A：`$D reset`；`$D scene 6a` | 一個服務，大家共用，用量算在模型上 | 模型服務表「處理中 2 個請求」、已服務 +2；kindred 上只有一個 MODEL 區塊 | GPU 卡 |
| 25:00 | 6b 閒置回收 | A：`$D scene 6b`；暫停時在 B 按 Ctrl-C，再回 A 按 Enter | 沒人等它的卡時留一小時；有人等時，閒置 1 分鐘就讓出來 | 「閒置 5 秒 · 59 分 … 後釋放」；約 65 秒後 alice 的工作「等 gemma4-31b 停止」，模型 stopping（決策欄「gemma4-31b 閒置 1 分 … 秒,有工作在等資源,提前回收」）→ stopped；alice 的區塊落在 A40 | GPU 卡 |
| 27:00 | 8 比較 | A：`$D reset`；切到投影片 | M13：FCFS、priority、backfill 的平均等待時間與配置占用率 | （投影片）header 一直標示目前的策略 | — |
| 28:00 | 結束 | | | | |

## 6. 備援

- **gemma 冷啟動失敗**（健康檢查超過 600 秒，或容器結束）：模型服務表顯示「failed · N 後才會重試」，B 印出 503 或錯誤。
  B 的迴圈會繼續送請求，設了 `retry_after_failure_s = 60` 時，1 分鐘後的下一個請求就會重試。如果又失敗：
  - 略過 6a、6b，改用 `tests/test_storyboard_model.py` 說明。
  - 第 1、5、7 幕假設 gemma 占住 kindred，這時 `demo.py` 會顯示 NOT READY。可以加 `--force` 照常執行，但落點會不同（例如 40 GiB 的工作可能改到 kindred），要照畫面講。
  - 展示後再看 kindred 的 Worker 日誌（`docs/lab-deploy.md`）。
- **冷啟動超過 600 秒**：第一個請求會收到 503（model_not_ready）。B 的迴圈 20 秒後再送，會接上同一個正在載入的服務，不用處理。
- **某一幕走樣**：在 A 按 Ctrl-C，執行 `$D reset` 與 `$D check`，再重跑那一幕。
  第 2 幕只有在 bob 的用量仍接近 0 時才重跑，否則直接進下一幕。第 3、4 幕可以直接重跑。
- **demo.py 顯示 NOT READY**：照提示處理。有殘留工作就 `$D reset`；gemma 狀態不對，就照第 5 節的順序走；
  bob 的用量太高時加 `--force`（alice 的用量不比 bob 少，排序就仍然成立），或略過這一幕。
- **節點突然離線**：`$D check` 顯示 FAIL。用 `uv run python scripts/lab_deploy.py status` 確認後，依 `docs/lab-deploy.md` 重新啟動那台的 Worker。
  其他節點上的幕仍可進行，但落點可能不同。
- **第 7 幕 karma 的 Worker 沒回來**：再執行一次 start，並看它的日誌。在 Worker 回來之前，bob 的工作會維持 UNKNOWN 並保留資源，
  `$D reset` 會等到逾時並列出「still UNKNOWN」。
- **投影機解析度太低、畫面被截斷**：關掉投影模式，或把瀏覽器縮小（Ctrl −）。
  也可以改用 CLI：`labflux --config <alice 的設定> queue`、`labflux --config <alice 的設定> status --all`。
- **第 5 幕的 session 沒有啟動**：檢查 C 的環境變數（bob 的 token、Scheduler 位址），再看 `labflux session status`。

## 7. 10 分鐘精簡版

| 時間 | 幕 | 做什麼 |
|---|---|---|
| 0:00 | 3（兼開場） | `$D scene 3`。利用前 2 分鐘的等待介紹 Dashboard、策略與分數公式。第 2 分鐘起講 head_reserved、would_delay、backfill，並切 CPU 核心軸看 18 核的工作。第 5 分鐘隊首開始 |
| 5:30 | 6c | `$D reset`；`$D scene 6c`，在 B 開 keepalive：模型搶占 alice 的 A40 工作，開始冷啟動計時 |
| 7:00 | 1 | `$D reset`；`$D scene 1`，趁 gemma 載入時看異質配置 |
| 9:00 | 8 | 投影片的 M13 比較圖。gemma 這時可能還在載入：指著計時說明第一個請求會一直等，不會先拿到 503 |
| 10:00 | 收尾 | B 按 Ctrl-C；`$D reset` |

精簡版沒有第 2 幕：講第 3 幕時指著排隊表的分數拆解（900 ＋ … − …）帶過公平性。

## 8. 彩排注意事項

- **第 2 幕需要 bob 的近期用量接近 0**（視窗是 60 分鐘）。完整彩排一次會讓 bob 累積約 3–4 分鐘，主要來自第 3 幕的 18 核工作和第 5 幕的訓練工作。
  所以彩排要在正式展示前 60 分鐘以上做完，或者正式展示前只彩排不含 bob 的部分。`$D check` 會顯示 bob 的用量。
- **第 3 幕的時間窗**：保留時段從 18 核工作開始後 300 秒起算，2 分鐘的工作必須在那之前至少 120 秒送到，否則它會延後保留時段，也就看不到 backfill。
  `demo.py` 在 18 核工作送出 120 秒後，接連送出三個工作，大約還有 60 秒餘裕。不要手動一步一步送。
  需要更多餘裕的話，把 `prep.json` 的時限改成 420–600 秒，並同步修改 `tests/test_demo_scenes.py` 裡的時間。
- **GPU 卡是預設的軸**：只用 CPU 的工作不會畫在上面，例如第 3 幕的 18 核工作、第 1 幕的 CPU／RAM 工作、第 5 幕。
  要看就切到「CPU 核心」或「RAM」；重新整理頁面會回到 GPU 卡。
- 第 2–4 幕進行中不要對 gemma 送任何請求，連測試用的 curl 也不行。
- 從 6c 到 6b，B 的 keepalive 要一直開著。有工作在等、又 60 秒沒有請求時，gemma 會被回收，下一個請求又要冷啟動 5 分鐘。
- 第 6b 幕要閒置滿 65 秒才送工作（`demo.py` 會等）。太早送的話，那一分鐘裡工作會顯示一小時後的保留時段（已知限制）。
- 第 7 幕 alice 的工作優先權是 6。(b) 中 bob 重跑的工作和 alice 的工作會同時排隊；如果兩者優先權相同，順序就看近期用量，
  而到第 7 幕時 alice 用得比 bob 多很多，bob 的工作會排在前面，又把 karma 的 4090 拿回去。
- 部署或重啟 Scheduler 後，排隊表要等第一輪排程才有內容，決策欄和「已服務」的計數會從零開始。
- 排程程式有任何修改，就跑 `uv run pytest tests/test_demo_scenes.py tests/test_storyboard.py tests/test_storyboard_gpu.py tests/test_storyboard_model.py -q`。
