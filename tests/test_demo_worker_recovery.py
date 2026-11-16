"""Slide 12: real process suspension, no duplicate attempt, and guaranteed resumption."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import docker
import psutil
import pytest
from test_fault_docker import SchedulerProcess

from labflux.config import Settings
from labflux.runtime import DockerExecutor

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


demo = load("demo")
recovery = load("demo_worker_recovery")


def test_dry_run_needs_no_sudo_network_or_docker(monkeypatch, capsys):
    def forbidden(*_, **kwargs):
        raise AssertionError("dry-run must not touch the lab")

    monkeypatch.setattr(recovery, "load_config", forbidden)
    monkeypatch.setattr(recovery.docker, "from_env", forbidden)
    monkeypatch.setattr(recovery.subprocess, "run", forbidden)
    assert recovery.main(["--dry-run"]) == 0
    assert "OFFLINE / UNKNOWN" in capsys.readouterr().out


@pytest.mark.parametrize("interrupted", [False, True])
def test_pause_resumes_the_same_process_even_when_the_presenter_is_interrupted(interrupted):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        pause = recovery.WorkerPause(child.pid)
        if not interrupted:
            pause.start()  # Production primes sudo before the first presenter pause.
            assert psutil.Process(child.pid).status() != psutil.STATUS_STOPPED
        try:
            with pause.paused():
                recovery.wait_for(
                    lambda: psutil.Process(child.pid).status() == psutil.STATUS_STOPPED,
                    "not paused",
                    3,
                )
                if interrupted:
                    raise KeyboardInterrupt
        except KeyboardInterrupt:
            assert interrupted
        recovery.wait_for(
            lambda: psutil.Process(child.pid).status() != psutil.STATUS_STOPPED, "not resumed", 3
        )
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_pause_watchdog_resumes_without_a_presenter_command():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with recovery.WorkerPause(child.pid, max_seconds=0.5).paused():
            recovery.wait_for(
                lambda: psutil.Process(child.pid).status() == psutil.STATUS_STOPPED, "not paused", 3
            )
            recovery.wait_for(
                lambda: psutil.Process(child.pid).status() != psutil.STATUS_STOPPED,
                "watchdog failed",
                3,
            )
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_changed_process_identity_is_never_paused():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        pause = recovery.WorkerPause(child.pid)
        pause.started = "not-the-original-process"
        with pytest.raises(RuntimeError, match="identity changed"):
            with pause.paused():
                pytest.fail("should not pause")
        assert psutil.Process(child.pid).status() != psutil.STATUS_STOPPED
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_presenter_killed_mid_scene_still_resumes_worker():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    code = (
        "import sys,time; sys.path.insert(0,sys.argv[1]); from demo_worker_recovery import WorkerPause\n"
        "with WorkerPause(int(sys.argv[2])).paused():\n"
        " print('PAUSED',flush=True)\n time.sleep(60)\n"
    )
    presenter = subprocess.Popen(
        [sys.executable, "-u", "-c", code, str(ROOT / "scripts"), str(child.pid)],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert presenter.stdout.readline().strip() == "PAUSED"
        assert psutil.Process(child.pid).status() == psutil.STATUS_STOPPED
        presenter.kill()
        presenter.wait(timeout=5)
        recovery.wait_for(
            lambda: psutil.Process(child.pid).status() != psutil.STATUS_STOPPED, "orphaned pause", 5
        )
    finally:
        if presenter.poll() is None:
            presenter.kill()
            presenter.wait(timeout=5)
        presenter.stdout.close()
        import signal

        child.send_signal(signal.SIGCONT)
        child.terminate()
        child.wait(timeout=5)


@pytest.mark.docker
def test_two_real_workers_keep_the_same_container_through_ten_seconds_offline(
    tmp_path, monkeypatch, capsys
):
    settings = Settings(
        data_dir=tmp_path / "scheduler",
        users={"alice": "alice-token", "bob": "bob-token"},
        scheduler_interval_s=0.1,
        heartbeat_interval_s=2,
        offline_timeout_s=10,
    )
    try:
        DockerExecutor(settings).check()
    except (docker.errors.DockerException, OSError, RuntimeError) as exc:
        pytest.skip(f"Docker unavailable: {exc}")
    scheduler = SchedulerProcess(settings)
    scheduler.start()
    runner = demo.Demo(scheduler.address, dict(settings.users))
    engine = docker.from_env()
    workers, logs, submitted = [], [], []
    prefix = "demo-recovery-" + tmp_path.name[-12:]
    names = [prefix + "-a", prefix + "-b"]
    try:
        for index, name in enumerate(names):
            config = tmp_path / f"{index}.toml"
            config.write_text(
                f"data_dir = {json.dumps(str(tmp_path / name))}\n"
                f"scheduler_url = {json.dumps(scheduler.address)}\n"
                f"worker_id = {json.dumps(name)}\n"
                f"allocatable_cpu = {index + 1}\nallocatable_memory_mib = 256\n"
                "heartbeat_interval_s = 2\npoll_interval_s = 0.1\n"
            )
            output = (tmp_path / f"{index}.log").open("w")
            logs.append(output)
            workers.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        str(ROOT / ".venv/bin/labflux"),
                        "--config",
                        str(config),
                        "worker",
                    ],
                    stdout=output,
                    stderr=output,
                )
            )
        recovery.wait_for(
            lambda: len(recovery.get_nodes(runner, "alice")) == 2, "workers did not register"
        )
        states, prompts = [], []
        real_nodes = recovery.get_nodes

        def nodes(*args):
            result = real_nodes(*args)
            states.append({n["id"]: (n["status"], n["allocated"]["jobs"]) for n in result})
            return result

        monkeypatch.setattr(recovery, "get_nodes", nodes)

        def enter(prompt):
            job = recovery.get_job(runner, "alice", submitted[0])
            assert job["state"] == "RUNNING" and len(job["attempts"]) == 1
            prompts.append(prompt)
            return ""

        monkeypatch.setattr("builtins.input", enter)
        spec = {
            "image": "python:3.12-slim",
            "argv": ["python", "-u", "-c", recovery.COUNTER],
            "resources": {"cpu": 0.5, "memory_mib": 64},
            "time_limit_s": 600,
            "safe_to_rerun": True,
            "retries_on_node_failure": 1,
        }
        recovery.demonstrate(
            runner, "alice", names[0], spec, recovery.WorkerPause(workers[0].pid), engine, submitted
        )
        assert len(prompts) == 2
        assert any(
            s.get(names[0]) == ("OFFLINE", 1) and s.get(names[1]) == ("ONLINE", 0) for s in states
        )
        output = capsys.readouterr().out
        assert "10 秒後恢復 Worker" in output and "沒有重跑" in output
        recovery.cleanup(runner, "alice", submitted)
        assert all(n["allocated"]["jobs"] == 0 for n in real_nodes(runner, "alice"))
    finally:
        # Only this test's process IDs and container labels; no deployed Worker is touched.
        import signal

        for worker in workers:
            if worker.poll() is None:
                worker.send_signal(signal.SIGCONT)
        for container in engine.containers.list(all=True):
            if container.labels.get("labflux.worker_id") in names:
                container.remove(force=True)
        for worker in workers:
            if worker.poll() is None:
                worker.terminate()
            try:
                worker.wait(timeout=20)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)
        for output in logs:
            output.close()
        engine.close()
        scheduler.stop()
