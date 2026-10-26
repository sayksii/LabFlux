# 三台 VM 的開發與期中展示環境

本環境使用 Ubuntu 24.04 cloud image、KVM／libvirt、NoCloud cloud-init，
Scheduler 執行於開發主機，三台 VM 分別執行一個 Docker Worker。

| Worker | vCPU | 配置 RAM | 系統保留 RAM |
|---|---:|---:|---:|
| labflux-vm-a | 1 | 2 GiB | 512 MiB |
| labflux-vm-b | 2 | 3 GiB | 512 MiB |
| labflux-vm-c | 4 | 4 GiB | 512 MiB |

Worker 回報的可分配 RAM 以 Guest 實際偵測總量扣除 512 MiB，會略低於上表的 VM 配置。
這個階段不需要 GPU passthrough。

## 主機準備

Ubuntu 主機需要虛擬化支援及下列工具；系統套件安裝需 sudo：

```bash
sudo apt-get update
sudo apt-get install -y docker.io qemu-kvm libvirt-daemon-system libvirt-clients virtinst cloud-image-utils
sudo usermod -aG docker,kvm,libvirt "$USER"
sudo systemctl enable --now docker libvirtd
```

重新登入以取得群組權限，或使用以下指令中的 `sg`。另外確認 default libvirt network 存在。
所有指令均在 repository 根目錄執行，且 uv 已加入 PATH。

```bash
export PATH="$HOME/.local/bin:$PATH"
uv sync --locked
sg libvirt -c 'virsh -c qemu:///system net-list --all'
sg docker -c 'docker info'
```

## 建立與部署

```bash
sg libvirt -c 'uv run python scripts/vm_lab.py provision'
sg libvirt -c 'uv run python scripts/vm_lab.py deploy'
sg libvirt -c 'uv run python scripts/vm_lab.py status'
```

`provision` 下載官方 Ubuntu image 並比對 SHA256，建立三個 overlay 磁碟與 cloud-init seed。
每個 VM 安裝 Docker 與 Python venv、建立範例資料集、使用專用的 VM SSH 金鑰。
套件下載可能需要數分鐘；等待期間可從 libvirt console 或 Guest 的 cloud-init 日誌查看進度。
再次執行會沿用現有 VM 與磁碟，不會重新格式化它們。

`deploy` 會建置 wheel、匯出 uv.lock 鎖定的正式依賴，經 SSH 安裝到各 VM，
並安裝 `labflux-worker` systemd service。Scheduler 只綁定 default libvirt bridge 的 IP，
預設使用 8765 port；可於第一次部署時加上 `--port PORT`。

設定與狀態存放在 `.labflux/vms/`：

- `scheduler.toml`：CLI 與中央服務設定，包含隨機產生的使用者／Worker Token，權限 600。
- `manifest.json`：VM IP 與配置。
- `id_ed25519`：僅供這三台 VM 的私鑰，權限 600，與 GitHub SSH 金鑰分開。
- `scheduler.log`：中央服務日誌。
- `scheduler-data/`：SQLite、程式快照與輸出。
- `smoke-report.json`：最近一次驗收紀錄。

以上目錄整體被 Git 忽略，不應加入版本控制。

## 使用與驗收

```bash
uv run labflux --config .labflux/vms/scheduler.toml nodes
uv run labflux --config .labflux/vms/scheduler.toml submit examples/jobs/hello.json --source examples/workloads/hello --wait
uv run labflux --config .labflux/vms/scheduler.toml status
uv run labflux --config .labflux/vms/scheduler.toml logs JOB_ID --follow
uv run labflux --config .labflux/vms/scheduler.toml fetch JOB_ID --directory outputs/JOB_ID
```

執行可重播驗收：

```bash
uv run python scripts/smoke.py --config .labflux/vms/scheduler.toml --faults
```

此命令建立並追蹤自己的 18 個工作，完成 10 項檢查：

1. 三台不同 CPU／RAM 配置的 Worker 均上線。
2. CLI 上傳程式、唯讀資料集、日誌、查詢及結果下載。
3. 跨三台 VM 的實際並行執行與資源匹配。
4. 同一節點同時執行兩個容器，配置不超額。
5. 嚴格 FCFS、隊首阻擋、等待及資源釋放後自動接續。
6. 直接讀取 cgroups，並實測 0.25 CPU 配額下的 CPU 時間。
7. 工作超過記憶體上限後被 OOM 終止。
8. 工作超時後終止容器程序樹並回收配置。
9. 排隊及執行中工作取消。
10. 暫停 VM-C 的 Worker 超過心跳時限，確認 OFFLINE／UNKNOWN、保留配置及後續取消。

`--faults` 使用 SIGSTOP 暫停 Worker 程序，Docker 與工作容器繼續執行，
因此能檢查「失聯不代表工作已停止」。程式在 finally 中恢復 Worker，
取消本次仍未完成的工作，並寫出通過／失敗紀錄。
驗收時請先讓其他工作結束，以免混入非本次測試的資源需求。

## 停止、重開與查看日誌

先完成或取消所有工作，再執行：

```bash
sg libvirt -c 'uv run python scripts/vm_lab.py stop'
```

腳本拒絕在仍有未結束工作或無法確認狀態時停止／重新部署。
停止只作用於這三台具名 VM 與腳本啟動的 Scheduler 程序群組，保留磁碟、Token、資料與紀錄。
下次以 `provision` 啟動，再執行 `deploy`。
若修改 Scheduler 程式，請在無工作時先停止環境，再部署以載入新程式。

Guest 的 Worker 日誌：

```bash
ssh -i .labflux/vms/id_ed25519 -o UserKnownHostsFile=.labflux/vms/known_hosts ubuntu@VM_IP 'sudo journalctl -u labflux-worker -n 50 --no-pager'
```

M6.4～M6.7 的自動對帳、重排與執行中重啟恢復尚未交付；本階段的停止／重開驗證只在環境閒置時進行。
