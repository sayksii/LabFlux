#!/usr/bin/env python3
"""Slide 12: pause only the local Worker, retain its live container, then resume it.

Uses the existing Scheduler and Docker; no deployment or SSH key is needed. The privileged
helper runs as the Worker's owner and holds a pidfd, so it cannot signal a reused PID. EOF,
Ctrl-C in the presenter, and the helper's deadline all resume the Worker.
"""

from __future__ import annotations

import argparse
import contextlib
import re
import select
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import docker
import psutil

from demo import ROOT, TERMINAL, load_config, wait_running

GUARD = r"""
import os,select,signal,sys
for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):
    signal.signal(sig,signal.SIG_IGN)
pid=int(sys.argv[1]); expected=sys.argv[2]; duration=float(sys.argv[3])
fd=os.pidfd_open(pid)
try:
    with open(f'/proc/{pid}/stat') as f:
        actual=f.read().rsplit(')',1)[1].split()[19]
    if actual != expected or os.stat(f'/proc/{pid}').st_uid != os.getuid():
        raise SystemExit('Worker identity changed; no signal sent')
    print('READY',flush=True)
    if os.read(sys.stdin.fileno(),1) != b'P':
        raise SystemExit(0)
    try:
        signal.pidfd_send_signal(fd,signal.SIGSTOP)
        print('PAUSED',flush=True)
        select.select([sys.stdin],[],[],duration)
    finally:
        signal.pidfd_send_signal(fd,signal.SIGCONT)
        print('RESUMED',flush=True)
finally:
    os.close(fd)
"""

COUNTER = "import time\nfor i in range(590):\n print(f'progress={i}',flush=True)\n time.sleep(1)\n"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def wait_for(check, message, timeout=45):
    deadline = time.monotonic() + timeout
    while True:
        if value := check():
            return value
        if time.monotonic() >= deadline:
            raise RuntimeError(message)
        time.sleep(0.25)


def enter(message):
    try:
        input(message + "\n[Enter] 繼續；[Ctrl-C] 恢復 Worker 並清場 ")
    except EOFError:
        raise KeyboardInterrupt from None


class WorkerPause:
    def __init__(self, pid, *, prefix=(), python=sys.executable, max_seconds=60):
        self.pid, self.prefix, self.python, self.max_seconds = (
            pid,
            list(prefix),
            python,
            max_seconds,
        )
        self.started = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        self.guard = None

    def start(self):
        if self.guard is not None:
            return
        # Keep the presenter's TTY so sudo can use the timestamp from sudo -v. The helper
        # ignores terminal Ctrl-C; EOF from the presenter is what tells it to resume safely.
        self.guard = subprocess.Popen(
            [
                *self.prefix,
                self.python,
                "-u",
                "-c",
                GUARD,
                str(self.pid),
                self.started,
                str(self.max_seconds),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.expect("READY")

    def expect(self, expected):
        ready, _, _ = select.select([self.guard.stdout], [], [], 10)
        require(
            ready and self.guard.stdout.readline().strip() == expected,
            "Worker 控制助手未就緒；請重新執行腳本取得 sudo 授權",
        )

    def close(self):
        guard, self.guard = self.guard, None
        if guard is None:
            return
        guard.stdin.close()
        try:
            guard.wait(timeout=10)
        except subprocess.TimeoutExpired:
            raise RuntimeError("恢復助手尚未退出；它會在 60 秒保底期限恢復 Worker") from None
        error = guard.stderr.read().strip()
        guard.stdout.close()
        guard.stderr.close()
        require(guard.returncode == 0, f"Worker 恢復助手失敗：{error}")

    @contextlib.contextmanager
    def paused(self):
        try:
            self.start()
            self.guard.stdin.write("P")
            self.guard.stdin.flush()
            self.expect("PAUSED")
            yield
        finally:
            # Closing the only write end also resumes the Worker if this presenter dies.
            self.close()


def local_worker(account):
    found = []
    for proc in psutil.process_iter(["pid", "username", "cmdline"]):
        argv = proc.info["cmdline"] or []
        if proc.info["username"] != account or not argv or argv[-1] != "worker":
            continue
        try:
            groups = Path(f"/proc/{proc.pid}/cgroup").read_text()
            if "labflux-worker.service" in groups:
                found.append(proc.pid)
        except FileNotFoundError:
            continue
    require(len(found) == 1, f"找不到唯一的 {account} labflux-worker.service；請在 kayle 執行")
    return found[0]


def get_job(demo, user, job_id):
    return demo.call(user, "GET", f"/api/jobs/{job_id}")


def get_nodes(demo, user):
    return demo.call(user, "GET", "/api/nodes")


def ensure_idle_node(demo, user, node_id, allowed=()):
    node = next((n for n in get_nodes(demo, user) if n["id"] == node_id), None)
    require(node and node["status"] == "ONLINE", f"{node_id} 必須先 ONLINE")
    require(
        all(a["job_id"] in allowed for a in node["allocations"]),
        f"{node_id} 還有其他工作，請等它們結束再展示",
    )
    return node


def prepare_project(demo, user, node_id, submitted):
    """Seed project locality on kayle; the real demo job remains eligible on other nodes."""
    nodes = get_nodes(demo, user)
    local = ensure_idle_node(demo, user, node_id)
    others = [g for n in nodes if n["id"] != node_id for g in n["capacity"]["gpus"]]
    seen = {g["uuid"]: g for g in local["observed"].get("gpus", [])}
    unique = [
        g
        for g in local["capacity"]["gpus"]
        if g["uuid"] in seen
        and not seen[g["uuid"]]["external_use"]
        and not any(g["name"].lower() in o["name"].lower() for o in others)
    ]
    require(unique, f"{node_id} 需要一張本節點獨有型號的空閒 GPU（kayle 的 A5000／3090）")
    project = "Worker-recovery-" + uuid.uuid4().hex[:8]
    spec = {
        "image": "python:3.12-slim",
        "project": project,
        "argv": [
            "python",
            "-c",
            "from pathlib import Path; Path('demo-node.txt').write_text('ready')",
        ],
        "resources": {"cpu": 1, "memory_mib": 256, "gpu": 1, "gpu_model": unique[0]["name"]},
        "time_limit_s": 30,
    }
    print(
        f"準備 {node_id} 的展示專案（短工作完成後，正式 Job 會優先沿用此節點的檔案）。", flush=True
    )
    seed = demo.call(user, "POST", "/api/jobs", json=spec)
    submitted.append(seed["id"])

    def finished():
        result = get_job(demo, user, seed["id"])
        return result if result["state"] in TERMINAL else None

    result = wait_for(finished, "準備工作尚未完成", timeout=60)
    require(result["state"] == "SUCCEEDED", f"準備工作失敗：{result['state']}")
    require(result["attempts"][-1]["node_id"] == node_id, "準備工作落在其他節點")
    return project


def counter(container):
    values = re.findall(rb"progress=(\d+)", container.logs(tail=10))
    return int(values[-1]) if values else -1


def same_attempt(demo, user, job_id, original, expected_state):
    job = get_job(demo, user, job_id)
    require(job["state"] == expected_state, f"預期 {expected_state}，實際 {job['state']}")
    require(
        len(job["attempts"]) == 1 and job["current_attempt_id"] == original["id"],
        "Job 出現新 attempt，停止展示",
    )
    attempt = job["attempts"][0]
    require(
        not attempt["released"] and attempt["node_id"] == original["node_id"],
        "原配置已釋放或移到別台節點",
    )
    require(
        attempt["started_at"] == original["started_at"]
        and attempt["gpu_uuids"] == original["gpu_uuids"],
        "執行起點或 GPU 配置改變",
    )
    allocations = [
        (n["id"], a)
        for n in get_nodes(demo, user)
        for a in n["allocations"]
        if a["job_id"] == job_id
    ]
    require(
        len(allocations) == 1 and allocations[0][0] == original["node_id"], "配置數量或節點改變"
    )
    return job


def demonstrate(demo, user, node_id, spec, pause, engine, submitted, *, hold_seconds=10):
    job = demo.call(user, "POST", "/api/jobs", json=spec)
    job_id = job["id"]
    submitted.append(job_id)
    wait_running(demo, [(user, job_id)], 60)
    original = get_job(demo, user, job_id)["attempts"][-1]
    require(original["node_id"] == node_id, f"Job 落在 {original['node_id']}；不會暫停其他節點")
    containers = engine.containers.list(
        filters={"label": [f"labflux.attempt_id={original['id']}", f"labflux.worker_id={node_id}"]}
    )
    require(len(containers) == 1, "找不到本機對應的唯一工作容器")
    container = containers[0]
    container_id = container.id
    process_id = container.attrs["State"]["Pid"]
    wait_for(lambda: counter(container) >= 0, "工作尚未印出進度")
    print(
        f"第一幕：Job {job_id[:8]} 在 {node_id} RUNNING；attempt {original['id'][:8]}，container {container_id[:12]}。",
        flush=True,
    )
    enter("按 Enter 模擬 Worker 失聯。")
    ensure_idle_node(demo, user, node_id, [job_id])
    same_attempt(demo, user, job_id, original, "RUNNING")
    before = counter(container)
    with pause.paused():
        print(
            "Worker 已暫停回報，Job 容器持續執行；等 Scheduler 偵測失聯（預設約 10 秒）。",
            flush=True,
        )

        def offline():
            node = next(n for n in get_nodes(demo, user) if n["id"] == node_id)
            return (
                node
                if node["status"] == "OFFLINE" and get_job(demo, user, job_id)["state"] == "UNKNOWN"
                else None
            )

        lost = wait_for(offline, "等不到 OFFLINE / UNKNOWN", timeout=30)
        print(
            f"第二幕：{node_id} OFFLINE、Job UNKNOWN；原資源保留，沒有交給其他 Worker。", flush=True
        )
        for left in range(hold_seconds, 0, -1):
            same_attempt(demo, user, job_id, original, "UNKNOWN")
            node = next(n for n in get_nodes(demo, user) if n["id"] == node_id)
            require(node["last_seen"] == lost["last_seen"], "Worker 已提早恢復心跳")
            container.reload()
            require(
                container.status == "running" and container.attrs["State"]["Pid"] == process_id,
                "工作容器已停止或重啟",
            )
            print(
                f"  {left:2d} 秒後恢復 Worker；同一容器 progress={counter(container)}", flush=True
            )
            time.sleep(1)
        during = counter(container)
        require(during > before, "失聯期間沒有觀察到工作進度")
    print("已恢復 Worker，等原 instance 回報仍在執行的 attempt。", flush=True)

    def recovered():
        node = next(n for n in get_nodes(demo, user) if n["id"] == node_id)
        return node["status"] == "ONLINE" and get_job(demo, user, job_id)["state"] == "RUNNING"

    wait_for(recovered, "Worker 尚未恢復 ONLINE / RUNNING", timeout=30)
    same_attempt(demo, user, job_id, original, "RUNNING")
    container.reload()
    require(
        container.id == container_id and container.attrs["State"]["Pid"] == process_id,
        "容器不是原本的執行",
    )
    print(
        f"第三幕：{node_id} ONLINE、原 Job RUNNING；同一 attempt、同一 container，progress {before} → {counter(container)}，沒有重跑。",
        flush=True,
    )
    enter("講完按 Enter 結束並清場。")


def cleanup(demo, user, submitted):
    for job_id in submitted:
        if get_job(demo, user, job_id)["state"] not in TERMINAL:
            demo.call(user, "POST", f"/api/jobs/{job_id}/cancel")

    def stopped():
        return all(
            (j := get_job(demo, user, job_id))["state"] in TERMINAL
            and all(a["released"] for a in j["attempts"])
            for job_id in submitted
        )

    wait_for(stopped, "尚未確認本幕工作全部停止；請查看 Dashboard", timeout=60)
    print("本幕工作已清場，停止與資源釋放皆已確認。", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / ".labflux/lab/scheduler.toml")
    parser.add_argument("--account", default="labflux")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        print(
            "第 12 頁 / 腳本 7：本機 Worker 與真實容器\n1. 啟動 Job，Enter 暫停 Worker。\n2. 等 OFFLINE / UNKNOWN，保留原配置 10 秒。\n3. 自動恢復 Worker，確認同一 attempt / container 繼續執行；Enter 清場。\n正式執行會先取得一次 sudo 授權；只控制本機 labflux-worker.service 的程序。"
        )
        return 0
    demo = load_config(args.config)
    user, node_id, submitted = "alice", socket.gethostname().split(".")[0], []
    pause = None
    engine = docker.from_env()
    try:
        ensure_idle_node(demo, user, node_id)
        pid = local_worker(args.account)
        engine.ping()
        print(
            "需要一次 sudo 授權，以 labflux 帳號暫停與恢復本機 Worker；請只在終端機輸入密碼。",
            flush=True,
        )
        subprocess.run(["sudo", "-v"], check=True)
        pause = WorkerPause(
            pid, prefix=["sudo", "-n", "-u", args.account], python="/usr/bin/python3"
        )
        pause.start()  # Authenticate before creating jobs; Enter can then wait without sudo expiry.
        project = prepare_project(demo, user, node_id, submitted)
        spec = {
            "image": "python:3.12-slim",
            "argv": ["python", "-u", "-c", COUNTER],
            "resources": {"cpu": 1, "memory_mib": 256, "gpu": 1},
            "project": project,
            "time_limit_s": 600,
            "safe_to_rerun": True,
            "retries_on_node_failure": 1,
        }
        demonstrate(demo, user, node_id, spec, pause, engine, submitted)
        return 0
    except KeyboardInterrupt:
        print("\n結束展示；先恢復 Worker，再清場。", flush=True)
        return 130
    except Exception as exc:
        print(f"demo: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            if pause is not None:
                pause.close()
            if submitted:
                cleanup(demo, user, submitted)
        finally:
            engine.close()


if __name__ == "__main__":
    import signal

    def terminate(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    sys.exit(main())
