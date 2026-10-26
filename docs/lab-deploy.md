# 實體機部署（kayle、karma）

本文件說明如何把 LabFlux 部署到實驗室的兩台實體機。兩台都是共用機器，
LabFlux 只使用其中一部分資源，並以專屬帳號 `labflux` 執行，不使用任何人的個人帳號。

| 節點 | 角色 | LabFlux 可預約的資源 | GPU |
|---|---|---|---|
| kayle（192.0.2.10） | Scheduler + Worker | 16 核、48 GiB | RTX 3090、RTX A5000（驅動 535） |
| karma（192.0.2.20） | Worker | 16 核、24 GiB | RTX 4090（驅動 580） |

本文件的 IP 都是範例（`192.0.2.0/24` 是文件專用位址）。實際的 IP、資源上限與 GPU 清單只放在
被 Git 忽略的 `configs/local-lab.toml`（格式見 `configs/lab.example.toml`）；`firewall-instructions`
與 `deploy` 都依這個檔案的實際值產生指令與設定。

- Scheduler 綁在 `192.0.2.10:8770`，只讓 karma 與 kayle 本機連入（見「防火牆」）。
- Worker 與 Scheduler 都是 `labflux` 帳號的 systemd **user** service，不需要 sudo 就能部署與重啟。
- 所有工作容器都以 `labflux`（UID 2001，兩台相同）執行。

## 事前準備（已完成）

- 兩台都有 `labflux` 帳號：UID/GID 2001、在 docker 群組、只能用鑰匙登入、已開 linger
  （沒有人登入時服務也會繼續執行，開機後自動啟動）。
- 部署鑰匙是 kayle 上的 `~/.ssh/labflux_deploy`。兩台的 host key 固定在被 Git 忽略的
  `.labflux/lab/known_hosts`，腳本一律檢查 host key，不會略過驗證。
- 節點清單在 `configs/local-lab.toml`（被 Git 忽略）；格式見 `configs/lab.example.toml`。

以下指令都在 kayle 的 repository 根目錄執行。

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

### `firewall-instructions`：產生防火牆指令

```bash
uv run python scripts/lab_deploy.py firewall-instructions
```

印出要由管理員執行的完整指令（見下一節）。**為什麼：** Scheduler 必須綁在 kayle 的公開 IP
才能讓 karma 連入，但 kayle 目前沒有啟用任何防火牆；不擋的話整個校園網路都連得到。

### `deploy`：安裝或更新

```bash
uv run python scripts/lab_deploy.py deploy --dry-run   # 只印出每一個步驟，不執行
uv run python scripts/lab_deploy.py deploy             # 兩台都部署
uv run python scripts/lab_deploy.py deploy --node karma
```

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
- 要重啟 Scheduler（部署 kayle）時，任何節點還有未釋放的配置。Scheduler 重啟恢復（M6.6）
  還沒完成，有工作在執行時重啟 Scheduler 可能讓狀態對不上。
  重啟 Worker 不受這個限制：M6.4 之後，Worker 停止時工作容器會繼續執行，新的 Worker
  啟動後會接手。只部署 karma 時，正在執行的工作不受影響。
- 只部署 karma 但 Scheduler 沒有在執行。

### `status`：查看狀態

```bash
uv run python scripts/lab_deploy.py status
```

列出每台的 service 狀態、安裝的 LabFlux 與 uv 版本、kayle 的防火牆狀態，
以及 Scheduler 看到的節點、預約量與 GPU 張數。

## 防火牆（唯一需要 sudo 的步驟）

只在 kayle 做一次。個人帳號沒有 sudo，請以管理員帳號執行（例如 `su - mvladmin`），
指令由 `firewall-instructions` 產生，內容如下：

1. 安裝 `/etc/systemd/system/labflux-firewall.service`。它是 oneshot unit，啟動時加入、停止時刪除這一條規則：
   ```
   iptables -I INPUT -d 192.0.2.10 -p tcp --dport 8770 ! -i lo ! -s 192.0.2.20 -j DROP
   ```
   只丟棄「不是 karma、也不是本機」連到 8770 的封包，不 ACCEPT 任何東西，也不影響其他 port。
   kayle 自己的 Worker 與 SSH 轉送連到本機 IP 時走 `lo`，不受影響。**不要執行 `ufw enable`**：
   預設拒絕所有連入，會擋到其他人的 SSH 與服務。
2. `sudo systemctl daemon-reload` 後 `sudo systemctl enable --now labflux-firewall.service`，開機時會自動套用。
3. 確認：`systemctl is-active labflux-firewall.service` 是 `active`，
   `sudo iptables -S INPUT | grep -- '--dport 8770'` 有一條 `-j DROP` 規則。

移除：`sudo systemctl disable --now labflux-firewall.service`，刪除 unit 檔後 `sudo systemctl daemon-reload`。
Scheduler 只綁 IPv4，不會從 IPv6 被連到。

## 使用

```bash
uv run labflux --config .labflux/lab/cli.toml nodes
uv run labflux --config .labflux/lab/cli.toml submit examples/jobs/mnist.json --source examples/mnist --wait
```

從自己的電腦看 Dashboard：先建立 SSH 轉送，再打開 http://localhost:8770/dashboard ，
以 `cli.toml` 裡的 `user_token` 登入。

```bash
ssh -L 8770:192.0.2.10:8770 kayle
```

新增使用者：把名稱加進 `configs/local-lab.toml` 的 `scheduler.users`，重新 `deploy --node kayle`，
再從 `.labflux/lab/scheduler.toml` 把該使用者的 Token 私下交給他。從清單移除的使用者，Token 會在下次部署時失效。

## 日誌

```bash
SSH="ssh -i ~/.ssh/labflux_deploy -o IdentitiesOnly=yes -o UserKnownHostsFile=.labflux/lab/known_hosts"
$SSH labflux@192.0.2.20 journalctl --user -u labflux-worker -n 100 --no-pager
$SSH labflux@192.0.2.10 journalctl --user -u labflux-scheduler -f
$SSH labflux@192.0.2.10 systemctl --user status labflux-worker
```

## 停止、更新與移除

- **更新：** 合併新版程式後重新執行 `deploy`。有工作在執行時腳本會拒絕重啟，等工作結束或取消後再部署。
- **停止：** 先確認節點上沒有工作（目前停止 Worker 會停掉它的容器），再執行
  `$SSH labflux@<host> systemctl --user stop labflux-worker.service`；
  kayle 的 Scheduler 是 `labflux-scheduler.service`。`disable` 可避免開機時自動啟動。
- **移除：** 在每台執行
  `systemctl --user disable --now labflux-worker.service labflux-scheduler.service`，
  刪除 `~/.config/systemd/user/labflux-*.service` 後 `systemctl --user daemon-reload`，
  再刪除 `~/labflux`（包含 Worker 資料、uv 快取，以及 kayle 上 Scheduler 的資料庫與輸出），
  最後 `docker image rm localhost/labflux/python-uv:3.12-uv0.11.8`。防火牆依上一節移除。
  `labflux` 帳號本身由管理員以 `userdel` 移除。

## 目前限制

- M6.5、M6.6 尚未完成：有工作在執行時不能重啟或更新 Scheduler（kayle）；Worker 可以。
- 防火牆規則只支援一台遠端 Worker；之後要加節點時需改成專用 chain。
- 同一台 Worker 上的工作共用可寫的 uv 快取，彼此需要互相信任。
