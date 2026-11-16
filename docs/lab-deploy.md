# 實體機部署（kayle、karma、kindred）

本文件說明如何把 LabFlux 部署到實驗室的實體機。每台都是共用機器，
LabFlux 只使用其中一部分資源，並以專屬帳號 `labflux` 執行，不使用任何人的個人帳號。

| 節點 | 角色 | LabFlux 可預約的資源 | GPU |
|---|---|---|---|
| kayle（192.0.2.10） | Scheduler + Worker | 16 核、48 GiB | RTX 3090、RTX A5000（驅動 535） |
| karma（192.0.2.20） | Worker | 16 核、24 GiB | RTX 4090（驅動 580） |
| kindred（192.0.2.30） | Worker | 32 核、64 GiB | A40 × 2（共 5 張，只登記其中兩張；驅動 550） |

本文件的 IP 都是範例（`192.0.2.0/24` 是文件專用位址）。實際的 IP、資源上限與 GPU 清單只放在
被 Git 忽略的 `configs/local-lab.toml`（格式見 `configs/lab.example.toml`）；`firewall-instructions`
與 `deploy` 都依這個檔案的實際值產生指令與設定。

- Scheduler 綁在 `192.0.2.10:8770`，只讓清單上的 Worker 節點與 kayle 本機連入（見「防火牆」）。
- Worker 與 Scheduler 都是 `labflux` 帳號的 systemd **user** service，不需要 sudo 就能部署與重啟。
- 所有工作容器都以 `labflux`（UID 2001，每台相同）執行。

## 事前準備（已完成）

- 三台都有 `labflux` 帳號：UID/GID 2001、在 docker 群組、只能用鑰匙登入、已開 linger
  （沒有人登入時服務也會繼續執行，開機後自動啟動）。新增節點時由管理員建立同樣的帳號，
  host key 先在機器上核對指紋，再加到 `known_hosts`。
- 部署鑰匙是 kayle 上的 `~/.ssh/labflux_deploy`。每台的 host key 固定在被 Git 忽略的
  `.labflux/lab/known_hosts`，腳本一律檢查 host key，不會略過驗證。
- 節點清單在 `configs/local-lab.toml`（被 Git 忽略）；格式見 `configs/lab.example.toml`。

以下指令都在 kayle 的 repository 根目錄執行（`uv sync --locked` 之後）。在 git worktree 裡執行時，
先把兩個被 Git 忽略的路徑以 symlink 指向主 checkout，部署狀態才會只有一份：

```bash
ln -s /path/to/LabFlux/.labflux .labflux
ln -s /path/to/LabFlux/configs/local-lab.toml configs/local-lab.toml
```

## 指令

### `check`：部署前檢查

```bash
uv run python scripts/lab_deploy.py check
```

逐台登入 `labflux`，列出：SSH、linger、Docker 版本、cgroup v2 與 CPU/RAM/swap 限制、
預設 runtime 不是 nvidia、NVIDIA 驅動與 CUDA 版本、設定的 GPU 都存在、nvidia-ctk、
GPU 是否閒置、uv、剩餘磁碟、Worker 狀態、能否連到 Scheduler 的 port，以及 kayle 的防火牆。

**為什麼：** 部署前確定每台機器都具備執行條件。除了一個 GPU 測試容器之外完全唯讀；
測試容器只執行 `nvidia-smi -L`，不佔 VRAM，而且只在每張卡都閒置時才會執行
（沒有運算程式、已用 VRAM 小於 1 GiB）。有任何 `FAIL` 時以非零狀態結束。

目前預期看到的 `WARN`／`SKIP`（2026-11-16 實測，結束狀態 0）：

| 節點 | 項目 | 原因 |
|---|---|---|
| karma | `user systemd  WARN  degraded` | 一個 snap 的 user service（firmware-notifier）失敗，與 LabFlux 無關 |
| kindred | `GPUs idle  WARN` | 卡 3、4 是常駐的 gemma（LabFlux 自己的模型服務），卡 1 是其他人的程式；檢查看的是整台機器的卡，不只資源池 |
| kindred | `GPU container  SKIP` | 有卡在使用中，所以不跑 GPU 測試容器 |

第一次部署之前另有 uv 不存在、Scheduler 尚未啟動、kayle 的防火牆尚未安裝等 `WARN`，見 `docs/verification.md` 7.1。

### `firewall-instructions`：產生防火牆指令

```bash
uv run python scripts/lab_deploy.py firewall-instructions
```

印出要由管理員執行的完整指令（見下一節）。**為什麼：** Scheduler 必須綁在 kayle 的公開 IP
才能讓其他節點的 Worker 連入，但 kayle 沒有啟用其他防火牆；不擋的話整個校園網路都連得到。

### `deploy`：安裝或更新

```bash
uv run python scripts/lab_deploy.py deploy --dry-run   # 只印出每一個步驟，不連線、不寫檔
uv run python scripts/lab_deploy.py deploy             # 清單上的節點全部部署，Scheduler 的節點先
uv run python scripts/lab_deploy.py deploy --node karma
```

`--dry-run` 依序印出每台的 `ssh`／`scp` 指令與會寫出的設定檔（路徑與大小），`.labflux/lab/` 不會有任何變動，
可以先拿來確認這次會動到哪些節點。

每台依序執行：

1. 把 kayle 的 uv 0.11.8 複製到 `~labflux/.local/bin/uv`。
2. 上傳本次建置的 wheel、鎖定的依賴清單（含雜湊）與基礎映像的 Dockerfile。
3. 上傳產生的 `worker.toml`（權限 600）；kayle 另外上傳 `scheduler.toml`。
4. 在該節點建置基礎映像 `localhost/labflux/python-uv:3.12-uv0.11.8`。
5. 停止服務，用 uv 管理的 Python 3.12 重建 `~/labflux/.venv` 並安裝。
6. 安裝並啟動 `labflux-worker.service`；kayle 先啟動 `labflux-scheduler.service`，
   確認 `/health` 正常後才啟動 Worker。最後等每台 Worker 在 Scheduler 上變成 `ONLINE`。

Token 只在第一次部署時以 `secrets.token_urlsafe(32)` 產生，之後重複使用；
`scheduler.toml`、各節點的 `worker.toml` 與 CLI 設定 `cli.toml` 都留在 `.labflux/lab/`（權限 600）。
安裝 Worker 本身用的是 `labflux` 帳號預設的 uv 快取，不使用工作可寫入的 `~/labflux/uv-cache`。

腳本會在以下情況**拒絕執行**：

- kayle 的 `labflux-firewall.service` 不是 `active`（Scheduler 不能在沒有防火牆時綁公開 IP）。
  Scheduler 的 service 本身也有同樣的檢查，開機時防火牆還沒好就不會啟動，之後自動重試。
- 只部署 Worker 節點（例如 `--node karma`）但 Scheduler 沒有在執行。

有工作在執行時也可以部署：M6.4 之後 Worker 停止時工作容器會繼續執行，新的 Worker 啟動後接手；
M6.6 之後 Scheduler 重啟時從資料庫還原，等每台 Worker 核對完才恢復派送。重啟 Scheduler 時，
進行中的快速測試指令與推論請求會失敗，需要重送；常駐的模型服務不受影響。

### `status`：查看狀態

```bash
uv run python scripts/lab_deploy.py status
```

列出每台的 service 狀態、安裝的 LabFlux 與 uv 版本、kayle 的防火牆狀態，
以及 Scheduler 看到的節點、預約量與 GPU 張數。

## 防火牆（唯一需要 sudo 的步驟）

只在 kayle 做一次。個人帳號沒有 sudo，請以管理員帳號執行（例如 `su - mvladmin`），
指令由 `firewall-instructions` 產生，內容如下：

1. 安裝 `/etc/systemd/system/labflux-firewall.service`。它是 oneshot unit，啟動時建立、停止時刪除一條專用的 chain：
   ```
   iptables -N LABFLUX-SCHEDULER
   iptables -A LABFLUX-SCHEDULER -i lo -j RETURN
   iptables -A LABFLUX-SCHEDULER -s 192.0.2.20 -j RETURN      # 每台遠端 Worker 一條
   iptables -A LABFLUX-SCHEDULER -j DROP
   iptables -I INPUT -d 192.0.2.10 -p tcp --dport 8770 -j LABFLUX-SCHEDULER
   ```
   連到 8770 的封包進入這條 chain：本機與清單上的 Worker 回到原本的 INPUT 規則，其餘丟棄。
   不 ACCEPT 任何東西，也不影響其他 port。kayle 自己的 Worker 與 SSH 轉送連到本機 IP 時走 `lo`，不受影響。
   **不要執行 `ufw enable`**：預設拒絕所有連入，會擋到其他人的 SSH 與服務。
2. `sudo systemctl daemon-reload`、`sudo systemctl enable labflux-firewall.service` 後
   `sudo systemctl restart labflux-firewall.service`，開機時會自動套用。
3. 確認：`systemctl is-active labflux-firewall.service` 是 `active`，
   `sudo iptables -S INPUT | grep -- '--dport 8770'` 有一條跳到 `LABFLUX-SCHEDULER` 的規則，
   `sudo iptables -S LABFLUX-SCHEDULER` 的最後一條是 `-j DROP`。

新增或移除節點後，重新執行 `firewall-instructions` 並照做一次；新節點在這之前連不到 Scheduler。

從舊版（單一條 `! -s <karma> -j DROP` 規則）升級時，照上面的步驟換成新的 unit 並 restart 之後，
舊規則還留在 INPUT，要手動刪除一次，否則新節點仍會被它擋住：

```bash
sudo iptables -D INPUT -d 192.0.2.10 -p tcp --dport 8770 ! -i lo ! -s 192.0.2.20 -j DROP
```

先換新 unit 再刪舊規則，過程中 8770 不會有沒被擋住的時間。

移除：`sudo systemctl disable --now labflux-firewall.service`，刪除 unit 檔後 `sudo systemctl daemon-reload`。
Scheduler 只綁 IPv4，不會從 IPv6 被連到。

## 使用

一般使用者在任一台節點登入一次（見下方「使用者登入」），之後直接用共用的 `labflux` 指令：

```bash
labflux login
labflux nodes
labflux submit examples/jobs/gpu-model.json --source examples/uv-gpu --wait --wait-timeout 1800
labflux models
```

部署者也可以用部署產生的 `.labflux/lab/cli.toml`（`scheduler.users` 的 token）：
`uv run labflux --config .labflux/lab/cli.toml nodes`。

`--wait` 預設只等 120 秒；節點第一次執行 torch 工作要下載約 3 GB 套件，所以要加 `--wait-timeout`。
可直接提交的 CPU、RAM、GPU、快速測試與模型服務範例，以及在實驗室實測的結果，見 [examples/README.md](../examples/README.md)。

### 部署後的驗收（`scripts/lab_smoke.py`）

```bash
uv run python scripts/lab_smoke.py --config .labflux/lab/cli.toml
```

報告預設寫到被 Git 忽略的 `.labflux/lab/lab-smoke.json`；要留作驗收紀錄時再複製到 `docs/evidence/`
（例：[lab-smoke-2026-11-16.json](evidence/lab-smoke-2026-11-16.json)）。約 1～2 分鐘，以 GPU 為主依序檢查：每個 GPU 工作只看得到自己的卡、同一張卡不會配給兩個工作、卡滿時工作排隊；
兩張卡的工作；經 CLI 提交、看日誌、取回結果的真實 CUDA 工作（`examples/uv-gpu`）；快速測試 session 搶占可搶占的
GPU 工作、被搶占的工作之後重跑；FCFS 隊首阻擋與排隊中取消；CPU 配額、RAM 上限、OOM、時限與執行中取消；
以及 Gateway 的一般與串流回應。

- 只使用自己建立的小工作，結束時取消剩下的；任何節點上有模型服務以外的配置、或自己還有未結束的工作時拒絕開始。
- 不重啟 Worker 或 Scheduler，不改排程策略；實驗室不是 `fcfs` 時加 `--policy priority|backfill`（API 不回報目前的策略）。
- GPU 檢查只用資源池裡閒置的卡（沒有被模型服務占用、沒有外部使用），沒有就跳過並記在報告的 `skipped`。
  FCFS 檢查會讓佇列被擋約 10 秒。
- Worker 故障需要重啟 Worker，不在這裡測（見 `docs/verification.md` M6.7）。
- 報告不含 IP、token、GPU UUID 與使用者名稱。舊的 `scripts/smoke.py` 只適用於三台 VM（見 `docs/vm-lab.md`）。

從自己的電腦看 Dashboard：先建立 SSH 轉送，再打開 http://localhost:8770/dashboard ，
以自己的 token（`login.toml` 或 `cli.toml` 裡的 `user_token`）登入。

```bash
ssh -L 8770:192.0.2.10:8770 kayle
```

## 使用者登入（Q14）

LabFlux 的使用者就是節點上的 Linux 帳號，不用改設定或重新部署。在任一台 LabFlux 節點裝好 CLI
（每台一份所有帳號共用的安裝，見下一節）後執行：

```bash
labflux login     # Logged in as alice (http://192.0.2.10:8770); saved to ~/.config/labflux/login.toml
labflux whoami    # alice
```

- CLI 連到這台 Worker 的本機 socket（Linux abstract socket `labflux-login`），kernel 告訴 Worker 對方的 UID，
  Worker 查出帳號名稱，再向 Scheduler 申請這個名稱的 token。CLI 會再向 Scheduler 確認 token 屬於自己，才存檔。
- 任何帳號都能搶先佔用 socket 名稱（例如 Worker 重啟時）。所以 CLI 也向 kernel 查開 socket 的帳號，
  只接受 root、自己或 `login_worker_user`（預設 `labflux`，即部署帳號），其他帳號一律拒絕、不存檔。
  清單的 `account` 不是 `labflux` 時，使用者的 CLI 設定（`--config` 或 `LABFLUX_CONFIG`）
  要把 `login_worker_user` 設成同一個名稱。Worker 開不了 socket 時記錄 error，這台節點就不能登入。
- token 存在 `~/.config/labflux/login.toml`（權限 600，內含 Scheduler 位址），之後的指令與 `labflux shell`
  自動帶上；Dashboard 登入與 Gateway 的金鑰也貼上它（`user_token` 那一行）。
- 身分是帳號名稱，不是 UID：同一個人在每台機器的 UID 不同，名稱相同就是同一個人。
  `scheduler.users` 裡的名稱與 Linux 帳號同名時，也是同一個身分。
- 每次登入都多發一個 token，舊的繼續有效（例如複製到別台的那份）。每個帳號只保留最新的 20 個
  （Scheduler 設定 `login_tokens_per_user`），更舊的 token 會失效。目前沒有撤銷指令：要撤銷就刪掉
  Scheduler 資料庫 `user_tokens` 表格裡那一列，再重啟 Scheduler。
- 已知限制：docker 群組等同 root，成員可用 `docker run --network host --user <UID>` 以任何 UID 連上 socket；
  一般使用者若在 docker 群組，登入無法防止他們互相冒用。
- 不是 LabFlux 節點的機器沒有 Worker：在節點上登入後，把 `login.toml` 複製到同一個路徑，
  再把 `scheduler_url` 改成 SSH 轉送的位址（8770 只放行 LabFlux 節點）。
- 設定檔（`--config`、`LABFLUX_CONFIG`）或 `LABFLUX_USER_TOKEN` 裡的 `user_token` 優先於登入檔。

`scheduler.users` 仍保留給部署者自己的 `cli.toml`。從清單移除的名稱，設定裡的 Token 會在下次部署時失效，
但同名帳號以 `labflux login` 拿到的 token 不受影響。

排程策略：在 `configs/local-lab.toml` 的 `[scheduler]` 加上 `policy = "backfill"`（可用 `fcfs`、`priority`、`backfill`，
其他值在解析清單時就被拒絕），重新 `deploy --node kayle`，就會寫進 `scheduler.toml` 的 `scheduling_policy`。
沒有設定時不寫這一行，Scheduler 維持預設的 `fcfs`。各模式的行為見 README 的「排程與通訊」。
實驗室目前沒有設定 `policy`，所以是 `fcfs`。改策略要重啟 Scheduler：進行中的快速測試指令與推論請求會失敗，常駐模型不受影響。

## 所有帳號共用的 CLI

部署只把 LabFlux 裝在 `labflux` 帳號的家目錄，其他帳號讀不到。每台節點另外裝一份共用的 CLI 到 `/opt/labflux`，
由有 sudo 權限的帳號執行。安裝來源是部署時放到 `labflux` 帳號的 uv 與 wheel，所以三台的指令相同：

```bash
B=/home/labflux/labflux            # deploy 放 wheel 與 requirements.txt 的地方
UV=/home/labflux/.local/bin/uv
sudo mkdir -p /opt/labflux
sudo env UV_PYTHON_INSTALL_DIR=/opt/labflux/python $UV venv --python 3.12 --python-preference only-managed /opt/labflux/venv
sudo env UV_PYTHON_INSTALL_DIR=/opt/labflux/python $UV pip install --python /opt/labflux/venv/bin/python -r $B/requirements.txt
sudo env UV_PYTHON_INSTALL_DIR=/opt/labflux/python $UV pip install --python /opt/labflux/venv/bin/python --no-deps --reinstall $B/labflux-0.1.0-py3-none-any.whl
sudo chmod -R a+rX /opt/labflux
sudo ln -sf /opt/labflux/venv/bin/labflux /usr/local/bin/labflux
```

- Python 3.12 也裝在 `/opt/labflux/python`：系統內建的 Python 可能太舊，而 uv 預設把 Python 裝在執行者自己的家目錄，
  其他帳號讀不到。同理，不要用 `sudo uv tool install`，那會裝進 root 的家目錄。
- 清單的 `account` 不是 `labflux` 時，把 `B`、`UV` 裡的 `/home/labflux` 換成那個帳號的家目錄。
- **每次 `deploy` 之後都要重跑一次**（`venv` 那一行可以略過）：部署腳本沒有 sudo 權限，不會更新 `/opt/labflux`。
  沒更新時，共用 CLI 停在舊版本，新指令或新欄位用不到。
- 確認：用一般帳號執行 `labflux version`、`labflux login`、`labflux whoami`。
- 移除：`sudo rm -rf /opt/labflux /usr/local/bin/labflux`。

## 模型服務（M10）

在 `configs/local-lab.toml` 加一個 `[models.<名稱>]` 表格（欄位見 `src/labflux/config.py` 的 `ModelConfig`，
完整範例見 `configs/lab.example.toml`），`deploy --node kayle` 時會原樣寫進 `scheduler.toml`。
名稱就是客戶端送出的 `model`；含 `/`、`:`、`.` 時要加引號，例如 `[models."qwen2.5-14b"]`，
vLLM 也要以 `--served-model-name` 設成同一個名稱。

權重不會在啟動時下載，要先放到節點上，再登錄成該節點的資料集：

```toml
[[nodes]]
name = "kayle"
# ...
datasets = { hf-models = "/srv/models/hf" }
```

模型設定 `datasets = ["hf-models"]` 後，權重以唯讀方式掛載在 `/datasets/hf-models`，模型也只會排到有這個資料集的節點；
列出的資料集沒有任何節點提供時，腳本會拒絕部署。容器以 `labflux` 帳號執行、`HOME=/workspace`，
所以權重目錄要讓 `labflux` 讀得到。搭配 `HF_HUB_OFFLINE=1` 與 `HF_HOME=/datasets/hf-models`，vLLM 只讀本機快取。

Gateway 在 `http://192.0.2.10:8770/v1`，與 OpenAI API 相容，金鑰就是自己的 LabFlux 使用者 Token
（`labflux login` 存在 `~/.config/labflux/login.toml` 的 `user_token`）：

```bash
export OPENAI_BASE_URL=http://192.0.2.10:8770/v1
export OPENAI_API_KEY=<自己的 user_token>
curl -s $OPENAI_BASE_URL/models -H "Authorization: Bearer $OPENAI_API_KEY"
```

8770 只放行 LabFlux 節點；其他機器先建立上一節的 SSH 轉送，再改用 `http://localhost:8770/v1`。
`labflux --config .labflux/lab/cli.toml models` 列出每個模型的模式、狀態（`waiting`、`starting`、`serving`、
`stopping`、`failed`、`stopped`）、所在節點與進行中的請求數。

## 日誌

```bash
SSH="ssh -i ~/.ssh/labflux_deploy -o IdentitiesOnly=yes -o UserKnownHostsFile=.labflux/lab/known_hosts"
$SSH labflux@192.0.2.20 journalctl --user -u labflux-worker -n 100 --no-pager
$SSH labflux@192.0.2.10 journalctl --user -u labflux-scheduler -f
$SSH labflux@192.0.2.10 systemctl --user status labflux-worker
```

## 啟動、停止、更新與移除

- **啟動：** 服務已 `enable`，開機後自動啟動（linger）。手動啟動時先啟動 kayle 的 Scheduler，再啟動各台的 Worker：
  `$SSH labflux@192.0.2.10 systemctl --user start labflux-scheduler.service`，
  之後每台 `$SSH labflux@<host> systemctl --user start labflux-worker.service`；`lab_deploy.py status` 確認都是 `ONLINE`。
- **更新：** 合併新版程式後重新執行 `deploy`，再請管理員更新每台的共用 CLI（見「所有帳號共用的 CLI」）。有工作在執行時也可以照常部署：工作由新的 Worker 接手，
  Scheduler 重啟後從資料庫還原；只有進行中的快速測試指令與推論請求需要重送。
- **停止：** `$SSH labflux@<host> systemctl --user stop labflux-worker.service`；
  kayle 的 Scheduler 是 `labflux-scheduler.service`。`disable` 可避免開機時自動啟動。
  停止 Worker **不會**停止它的工作容器：工作繼續執行並佔用 GPU，但 Worker 停止期間不檢查執行時限、
  也不回傳日誌，下一個 Worker 啟動並接手後才補上。要停止工作，先用 `labflux cancel`。
- **移除：** 在每台執行
  `systemctl --user disable --now labflux-worker.service labflux-scheduler.service`，
  刪除 `~/.config/systemd/user/labflux-*.service` 後 `systemctl --user daemon-reload`，
  再刪除 `~/labflux`（包含 Worker 資料、uv 快取，以及 kayle 上 Scheduler 的資料庫與輸出），
  最後 `docker image rm localhost/labflux/python-uv:3.12-uv0.11.8`。防火牆依上一節移除。
  `labflux` 帳號本身由管理員以 `userdel` 移除。

## 目前限制

- 同一台 Worker 上的工作共用可寫的 uv 快取，彼此需要互相信任。
- 操作上要避免（PR #2 審查中判定為少見、暫不處理的情況，詳見 `docs/verification.md`）：
  - 不要用同一個 `worker_id` 再啟動第二個 Worker；新註冊的 instance 會接手並關閉它找不到容器的工作。
  - 有 GPU 工作執行時不要修改 Worker 的 `gpus` 清單再重啟；接手時卡片不在清單內會失敗。
  - 不要替已註冊的 Worker 改名；舊名稱仍佔著同一批 GPU，新名稱會註冊失敗，需手動清理資料庫。
  - 部署者必須是 `scheduler.users` 之一，否則部署結束時無法向 Scheduler 確認 Worker 已上線。
