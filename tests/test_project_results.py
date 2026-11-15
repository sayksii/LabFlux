"""Architecture 6.1: a project job's directory stays on its node, and the project's next run on
that node (a job, a retry or a session) starts from the files it generated."""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_preemption as preemption
from conftest import register, user_headers, worker_headers
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from test_cli import configure
from test_doorbell import wait_for
from test_reconnect import container, deliver, get_job, restart
from test_session_worker import SessionExecutor, result_of, send_command, snapshot, writeback

import labflux.cli as cli
from labflux import fastlane
from labflux.api import create_app
from labflux.config import Settings
from labflux.database import SCHEMA_VERSION, Database, events, project_results, schema_version
from labflux.runtime import ExecutionResult
from labflux.scheduler import Scheduler
from labflux.worker import SNAPSHOT_FILES, Worker
from labflux.workspace import copy_generated

JOB = {"image": "python:3.12-slim", "argv": ["python", "main.py"]}


def files(root):
    """{relative path: bytes} of the regular files under root (links are reported as such)."""
    found = {}
    for directory, _, names in os.walk(root):
        for name in names:
            path = os.path.join(directory, name)
            relative = os.path.relpath(path, root)
            found[relative] = "<link>" if os.path.islink(path) else Path(path).read_bytes()
    return found


class ProjectExecutor(SessionExecutor):
    """A batch run calls run_script(workspace, spec) on the host workspace, as the program
    sees /workspace through the bind mount, after recording what the workspace held when the
    container started. Sessions behave as in the session tests."""

    def __init__(self):
        super().__init__()
        self.seen: dict[str, dict] = {}
        self.run_script = lambda workspace, spec: None

    def run(self, spec, workspace, outputs, on_log, on_started, cancel, **options):
        if spec.session:
            return super().run(spec, workspace, outputs, on_log, on_started, cancel, **options)
        options["on_launch"]()
        self.seen[spec.attempt_id] = files(workspace)
        on_started()
        self.run_script(workspace, spec)
        detach = options.get("detach")
        if detach is not None and detach.is_set():
            return None  # The Worker stopped; the container would outlive it.
        return ExecutionResult("SUCCEEDED", 0, "completed", True)

    def stop_unstarted(self, spec):
        return ExecutionResult("CANCELLED", None, "cancelled before start", True)

    def inventory(self, worker_id):
        return []  # No container survives in these tests.

    def remove(self, worker_id, attempt_id):
        return True


def connect(client, tmp_path, executor, instance=None):
    worker = Worker(Settings(data_dir=tmp_path / "worker", worker_id="vm-a"), executor=executor)
    worker.client.close()
    worker.client = client  # Requests below that act for a user pass user_headers().
    worker.client.headers["Authorization"] = "Bearer dev-worker-token"
    if instance:
        worker.instance_id = instance
    worker.sent = []
    worker.send = lambda message: worker.sent.append(message) or True
    return worker


@pytest.fixture
def lab(client, tmp_path):
    executor = ProjectExecutor()
    worker = connect(client, tmp_path, executor, register(client, cpu=2))
    yield worker, executor
    for handle in worker.handles.values():
        handle["cancel"].set()
    worker.pool.shutdown(wait=True)


def upload(client, tmp_path, contents):
    response = client.post(
        "/api/artifacts/sources", headers=user_headers(), content=snapshot(tmp_path, contents)
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def submit(client, tmp_path, source=None, user="alice", **fields):
    body = {**JOB, "resources": {"cpu": 1, "memory_mib": 256}, **fields}
    if source is not None:
        body["source_id"] = upload(client, tmp_path, source)
    response = client.post("/api/jobs", headers=user_headers(user), json=body)
    assert response.status_code == 201, response.text
    return response.json()


def run_pending(client, worker):
    """Accept every pending command, wait for the runs to end (a session only to start) and
    flush their results."""
    service = client.app.state.scheduler
    commands = service.poll_commands("vm-a", worker.instance_id)
    worker.process(commands)
    for command in commands:
        attempt_id = command["attempt_id"]
        if command["kind"] == "START" and command["payload"].get("session"):
            wait_for(lambda attempt_id=attempt_id: worker._read(attempt_id)["started"])
        elif attempt_id in worker.handles:
            worker.handles[attempt_id]["future"].result(timeout=5)
    worker.flush()
    return commands


def run(client, worker, tmp_path, source=None, **fields):
    job = submit(client, tmp_path, source, **fields)
    run_pending(client, worker)
    return get_job(client, job)


def workspace(worker, job):
    return worker.root / job["current_attempt_id"] / "workspace"


def results(client, user="alice"):
    return client.get("/api/results", headers=user_headers(user)).json()


def finished(client, job, instance, kept_bytes=None, outcome="SUCCEEDED"):
    """The Worker's FINISHED event, optionally reporting a kept directory."""
    attempt = get_job(client, job)["attempts"][-1]
    response = client.post(
        f"/internal/workers/{attempt['node_id']}/attempts/{attempt['id']}/events",
        headers=worker_headers(),
        json={
            "event_id": f"finish-{attempt['id']}",
            "instance_id": instance,
            "kind": "FINISHED",
            "outcome": outcome,
            "exit_code": 0,
            "termination_confirmed": True,
            "kept_bytes": kept_bytes,
        },
    )
    assert response.status_code == 200, response.text
    return attempt["id"]


def start_payload(service, instance, node="vm-a"):
    return next(c for c in service.poll_commands(node, instance) if c["kind"] == "START")["payload"]


# P1: project names ---------------------------------------------------------------------------


def test_project_name_comes_from_labflux_toml_or_the_source_folder(client, tmp_path, monkeypatch):
    runner = configure(monkeypatch, client)
    spec = tmp_path / "job.json"
    spec.write_text(json.dumps(JOB))

    def submitted(*arguments, spec=spec):
        result = runner.invoke(cli.app, ["submit", str(spec), *arguments])
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)["spec"]["project"]

    trainer = tmp_path / "trainer"
    trainer.mkdir()
    (trainer / "main.py").write_text("print('hi')\n")
    assert submitted("--source", str(trainer)) == "trainer"
    (trainer / fastlane.PROJECT_FILE).write_text('[project]\nname = "vqa cot"\n')
    assert submitted("--source", str(trainer)) == "vqa cot"
    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps({**JOB, "project": "mine"}))
    assert submitted("--source", str(trainer), spec=explicit) == "mine"
    assert submitted() is None  # No source and no field: no project.
    (trainer / fastlane.PROJECT_FILE).write_text("[project]\nnam = 1\n")
    result = runner.invoke(cli.app, ["submit", str(spec), "--source", str(trainer)])
    assert result.exit_code == 1 and "invalid" in result.stderr
    # The fast lane names the session's project the same way, from the project root.
    root = tmp_path / "proj"
    root.mkdir()
    (root / fastlane.PROJECT_FILE).write_text("")
    config = fastlane.load_project(root)
    assert fastlane.project_name(root, config) == "proj"
    assert fastlane.session_request(config, name="proj").project == "proj"
    assert fastlane.session_request(config).project is None
    (root / fastlane.PROJECT_FILE).write_text('[project]\nname = "vqa"\n')
    leased = []
    monkeypatch.setattr(fastlane._Run, "run", lambda self, request: leased.append(request) or 0)
    settings = Settings(data_dir=tmp_path / "client")
    assert fastlane.run_command(settings, root, fastlane.load_project(root), ["python"], root) == 0
    assert leased[0].project == "vqa"


# P0a / P0b / P3: keep the directory, copy what the run generated ------------------------------


def test_a_project_run_keeps_its_directory_and_the_scheduler_records_it(client, lab, tmp_path):
    worker, executor = lab

    def train(workspace, spec):
        (workspace / "ckpt").mkdir()
        (workspace / "ckpt" / "model.pt").write_bytes(b"weights")
        (workspace / ".venv" / "lib").mkdir(parents=True)
        (workspace / ".venv" / "lib" / "torch.so").write_bytes(b"x" * 1000)

    executor.run_script = train
    job = run(client, worker, tmp_path, {"main.py": b"train()"}, project="vqa")
    assert job["state"] == "SUCCEEDED"
    kept = workspace(worker, job)
    # The directory stays without its .venv, which the next run never copies.
    assert files(kept) == {"main.py": b"train()", "ckpt/model.pt": b"weights"}
    assert json.loads((kept.parent / SNAPSHOT_FILES).read_text()) == {"files": ["main.py"]}
    [row] = results(client)
    assert row == {
        "attempt_id": job["current_attempt_id"],
        "job_id": job["id"],
        "project": "vqa",
        "node_id": "vm-a",
        "size": len(b"train()weights"),
        "ended_at": 1000.0,
        "outcome": "SUCCEEDED",
        "deleted_by_next_run": False,
    }
    assert worker._read(job["current_attempt_id"])["kept_bytes"] == row["size"]
    assert results(client, "bob") == []


def test_the_next_run_copies_generated_files_and_its_own_snapshot_wins(client, lab, tmp_path):
    worker, executor = lab

    def first(workspace, spec):
        (workspace / "ckpt").mkdir()
        (workspace / "ckpt" / "model.pt").write_bytes(b"w1")
        (workspace / "extra.py").write_bytes(b"generated")
        (workspace / "conf.py").write_bytes(b"changed by the run")  # A snapshot file.
        for cache in ("__pycache__/main.cpython-312.pyc", ".pytest_cache/v", ".git/HEAD"):
            (workspace / cache).parent.mkdir(exist_ok=True)
            (workspace / cache).write_bytes(b"cache")
        (workspace / "latest.pt").symlink_to("ckpt/model.pt")

    executor.run_script = first
    one = run(client, worker, tmp_path, {"main.py": b"v1", "conf.py": b"c1"}, project="p")

    def second(workspace, spec):
        model = workspace / "ckpt" / "model.pt"
        model.write_bytes(model.read_bytes() + b"+w2")

    executor.run_script = second
    two = run(client, worker, tmp_path, {"main.py": b"v2", "extra.py": b"snapshot"}, project="p")
    assert two["state"] == "SUCCEEDED"
    attempt = two["current_attempt_id"]
    # Generated files only: not the old snapshot (conf.py), caches or links; and this run's
    # snapshot replaces a generated file of the same name (D4).
    assert executor.seen[attempt] == {
        "main.py": b"v2",
        "extra.py": b"snapshot",
        "ckpt/model.pt": b"w1",
    }
    assert "starts from the files of job" in two["schedule_note"]
    # The previous directory is copied, never changed.
    assert files(workspace(worker, one))["ckpt/model.pt"] == b"w1"
    # A third run continues from the second, the most recent one.
    executor.run_script = lambda workspace, spec: None
    three = run(client, worker, tmp_path, {"main.py": b"v3"}, project="p")
    assert executor.seen[three["current_attempt_id"]] == {
        "main.py": b"v3",
        "ckpt/model.pt": b"w1+w2",
    }
    assert [row["job_id"] for row in results(client)] == [three["id"], two["id"], one["id"]]


def test_copy_keeps_modification_times_and_skips_snapshot_files(tmp_path):
    previous, target = tmp_path / "previous", tmp_path / "new"
    (previous / "ckpt").mkdir(parents=True)
    (previous / "ckpt" / "model.pt").write_bytes(b"w")
    os.utime(previous / "ckpt" / "model.pt", ns=(10**18, 10**18))
    (previous / "main.py").write_bytes(b"code")
    target.mkdir()
    assert copy_generated(previous, target, ["main.py"]) == ["ckpt/model.pt"]
    assert files(target) == {"ckpt/model.pt": b"w"}
    assert os.stat(target / "ckpt" / "model.pt").st_mtime_ns == 10**18


def test_concurrent_runs_of_a_project_get_separate_directories(client, lab, tmp_path):
    worker, executor = lab
    executor.run_script = lambda workspace, spec: (workspace / "ckpt.pt").write_bytes(b"base")
    base = run(client, worker, tmp_path, {"main.py": b"x"}, project="p")
    both_running = threading.Barrier(2)

    def concurrent(workspace, spec):
        both_running.wait(timeout=5)
        (workspace / "ckpt.pt").write_bytes(spec.attempt_id.encode())

    executor.run_script = concurrent
    jobs = [submit(client, tmp_path, {"main.py": b"x"}, project="p") for _ in range(2)]
    commands = run_pending(client, worker)
    assert len(commands) == 2
    assert {c["payload"]["previous_attempt_id"] for c in commands} == {base["current_attempt_id"]}
    for job in jobs:
        value = get_job(client, job)
        assert value["state"] == "SUCCEEDED"
        attempt = value["current_attempt_id"]
        assert executor.seen[attempt] == {"main.py": b"x", "ckpt.pt": b"base"}
        assert files(workspace(worker, value))["ckpt.pt"] == attempt.encode()
    assert len(results(client)) == 3


def test_jobs_without_a_project_keep_the_old_behavior(client, lab, tmp_path):
    worker, executor = lab
    executor.run_script = lambda workspace, spec: (workspace / "ckpt.pt").write_bytes(b"w")
    first = run(client, worker, tmp_path, {"main.py": b"x"})
    assert not workspace(worker, first).exists()
    assert not (workspace(worker, first).parent / SNAPSHOT_FILES).exists()
    second = submit(client, tmp_path, {"main.py": b"x"})
    command = run_pending(client, worker)[0]
    assert "previous_attempt_id" not in command["payload"]
    assert executor.seen[get_job(client, second)["current_attempt_id"]] == {"main.py": b"x"}
    assert results(client) == []
    assert "starts from" not in get_job(client, second)["schedule_note"]


def test_a_run_that_never_launched_is_not_a_previous_run(client, lab, tmp_path):
    worker, executor = lab
    executor.run_script = lambda workspace, spec: (workspace / "ckpt.pt").write_bytes(b"w")
    trained = run(client, worker, tmp_path, project="p")
    job = submit(client, tmp_path, project="p")
    client.post(f"/api/jobs/{job['id']}/cancel", headers=user_headers())  # STOP before START.
    run_pending(client, worker)
    assert get_job(client, job)["state"] == "CANCELLED"
    assert [row["job_id"] for row in results(client)] == [trained["id"]]


# P0c: placement ------------------------------------------------------------------------------


def test_placement_prefers_the_node_with_the_latest_result(client, service, tmp_path):
    b = register(client, "vm-b", cpu=1)
    first = submit(client, tmp_path, project="p")
    assert first["attempts"][0]["node_id"] == "vm-b"
    kept = finished(client, first, b, kept_bytes=10)
    a = register(client, "vm-a", cpu=1)
    # Equal nodes otherwise tie-break to vm-a; the project's result wins.
    second = submit(client, tmp_path, project="p")
    assert second["attempts"][0]["node_id"] == "vm-b"
    assert start_payload(service, b, "vm-b")["previous_attempt_id"] == kept
    assert f"starts from the files of job {first['id'][:8]}" in second["schedule_note"]
    # A preference, not a wait: with vm-b busy the next run starts on vm-a, without the files.
    third = submit(client, tmp_path, project="p")
    assert third["attempts"][0]["node_id"] == "vm-a"
    assert "previous_attempt_id" not in start_payload(service, a)
    assert "no node with the project's files (vm-b) can run it now" in third["schedule_note"]
    finished(client, second, b, kept_bytes=10)
    finished(client, third, a, kept_bytes=10)
    # Owner + project: another user's project of the same name starts fresh.
    other = submit(client, tmp_path, user="bob", project="p")
    assert other["attempts"][0]["node_id"] == "vm-a"
    assert "starts from" not in other["schedule_note"]


def test_placement_falls_back_to_a_node_with_an_older_result(client, service, tmp_path):
    a = register(client, "vm-a", cpu=2)
    older = submit(client, tmp_path, project="p")
    kept = finished(client, older, a, kept_bytes=10)
    blocker = submit(client, tmp_path, resources={"cpu": 2, "memory_mib": 256})
    b = register(client, "vm-b", cpu=1)
    latest = submit(client, tmp_path, project="p")
    assert latest["attempts"][0]["node_id"] == "vm-b"
    finished(client, latest, b, kept_bytes=10)
    busy = submit(client, tmp_path, project="p")
    assert busy["attempts"][0]["node_id"] == "vm-b"
    finished(client, blocker, a)
    register(client, "vm-c", cpu=1)
    # vm-b, with the latest files, is busy. vm-a still holds an older result of the project
    # and goes before vm-c, which best fit alone would pick (the tighter fit).
    job = submit(client, tmp_path, project="p")
    assert job["attempts"][0]["node_id"] == "vm-a"
    assert start_payload(service, a)["previous_attempt_id"] == kept
    assert f"starts from the files of job {older['id'][:8]}" in job["schedule_note"]


# P2: retries ---------------------------------------------------------------------------------


def test_a_retry_continues_from_its_own_previous_attempt(client, service, tmp_path):
    """Even when another run of the project ended on the node in between."""
    old = register(client, cpu=2)
    retried = submit(client, tmp_path, project="p", retries_on_node_failure=1, safe_to_rerun=True)
    lost = deliver(client, service, old)["attempt_id"]
    other = submit(client, tmp_path, project="p")
    survivor = deliver(client, service, old, started=False)["attempt_id"]
    # The node restarts: the retried job's container is gone, the other one survived. With one
    # CPU the retry has to wait until the other run ends.
    inventory = {
        "attempts": [{"attempt_id": lost}, {"attempt_id": survivor}],
        "containers": [container(survivor)],
    }
    new, response = restart(client, inventory, cpu=1)
    assert response.json()["closed"] == [lost]
    assert get_job(client, retried)["state"] == "QUEUED"
    [row] = results(client)
    assert row["attempt_id"] == lost and row["size"] is None  # The Worker keeps it.
    finished(client, other, new, kept_bytes=5)
    assert [r["attempt_id"] for r in results(client)] == [survivor, lost]
    value = get_job(client, retried)
    assert value["state"] == "STARTING" and len(value["attempts"]) == 2
    assert start_payload(service, new)["previous_attempt_id"] == lost


def test_a_preempted_run_continues_from_its_own_directory(tmp_path, clock):
    """M11 with 6.1: the rerun of a preempted project job starts from what its stopped attempt
    wrote, e.g. the checkpoint saved before the STOP."""
    values = Settings(
        data_dir=tmp_path / "preempt",
        users={"alice": "alice-token", "bob": "bob-token"},
        scheduling_policy="priority",
    )
    with TestClient(create_app(values, run_loop=False, clock=lambda: clock[0])) as client:
        preempted_run_continues(client, clock)


def preempted_run_continues(client, clock):
    job, commands = preemption.job, preemption.commands
    instance, victim, preemptor = preemption.preempting(client, clock, project="p")
    [attempt] = job(client, victim)["attempts"]
    # The Worker confirms the stop and keeps the directory, since the run had launched.
    response = client.post(
        f"/internal/workers/kayle/attempts/{attempt['id']}/events",
        headers=worker_headers(),
        json={
            "event_id": f"finish-{attempt['id']}",
            "instance_id": instance,
            "kind": "FINISHED",
            "outcome": "CANCELLED",
            "exit_code": 137,
            "termination_confirmed": True,
            "kept_bytes": 7,
        },
    )
    assert response.status_code == 200, response.text
    assert job(client, victim)["attempts"][0]["state"] == "PREEMPTED"
    assert [r["attempt_id"] for r in results(client)] == [attempt["id"]]
    preemptor = job(client, preemptor)
    response = client.post(
        f"/internal/workers/kayle/attempts/{preemptor['current_attempt_id']}/events",
        headers=worker_headers(),
        json={
            "event_id": "finish-preemptor",
            "instance_id": instance,
            "kind": "FINISHED",
            "outcome": "SUCCEEDED",
            "exit_code": 0,
            "termination_confirmed": True,
        },
    )
    assert response.status_code == 200, response.text
    value = job(client, victim)
    assert value["state"] == "STARTING" and len(value["attempts"]) == 2
    rerun = value["attempts"][-1]["id"]
    [start] = [c for c in commands(client, instance) if c["attempt_id"] == rerun]
    assert start["payload"]["previous_attempt_id"] == attempt["id"]


def test_a_worker_restart_keeps_the_lost_run_for_its_retry(client, service, clock, tmp_path):
    executor = ProjectExecutor()
    old = connect(client, tmp_path, executor, register(client, cpu=1))
    job = submit(
        client,
        tmp_path,
        {"main.py": b"train"},
        project="p",
        retries_on_node_failure=1,
        safe_to_rerun=True,
    )

    def interrupted(workspace, spec):
        (workspace / "ckpt.pt").write_bytes(b"epoch 1")
        old.stop_event.set()  # The machine goes down with the container mid-run.

    executor.run_script = interrupted
    try:
        run_pending(client, old)
        lost = get_job(client, job)["current_attempt_id"]
        clock[0] += service.settings.offline_timeout_s + 1
        service.tick()
        new = connect(client, tmp_path, executor)
        assert new.register()["closed"] == [lost]
        assert files(new.root / lost / "workspace") == {"main.py": b"train", "ckpt.pt": b"epoch 1"}
        executor.run_script = lambda workspace, spec: None
        run_pending(client, new)
        value = get_job(client, job)
        assert value["state"] == "SUCCEEDED" and len(value["attempts"]) == 2
        retry = value["current_attempt_id"]
        assert executor.seen[retry] == {"main.py": b"train", "ckpt.pt": b"epoch 1"}
        assert [r["attempt_id"] for r in results(client)] == [retry, lost]
        new.pool.shutdown(wait=True)
    finally:
        old.pool.shutdown(wait=True)


# P4: retention -------------------------------------------------------------------------------


def test_only_the_latest_three_directories_per_project_and_node_are_kept(client, lab, tmp_path):
    worker, executor = lab
    executor.run_script = lambda workspace, spec: (workspace / "ckpt.pt").write_bytes(b"w")
    jobs = [run(client, worker, tmp_path, project="p") for _ in range(3)]
    other = run(client, worker, tmp_path, project="q")
    assert [r["deleted_by_next_run"] for r in results(client) if r["project"] == "p"] == [
        False,
        False,
        True,
    ]
    jobs.append(run(client, worker, tmp_path, project="p"))
    oldest = jobs[0]["current_attempt_id"]
    service = client.app.state.scheduler
    [prune] = service.poll_commands("vm-a", worker.instance_id)
    assert prune["id"] == f"prune-{oldest}" and prune["kind"] == "PRUNE"
    assert workspace(worker, jobs[0]).exists()
    worker.process([prune])
    worker.process([prune])  # Redelivered: nothing left to do.
    assert not workspace(worker, jobs[0]).exists()
    assert all(workspace(worker, job).exists() for job in [*jobs[1:], other])
    assert service.poll_commands("vm-a", worker.instance_id) == []
    kept = [r["job_id"] for r in results(client) if r["project"] == "p"]
    assert kept == [job["id"] for job in reversed(jobs[1:])]


def test_copies_share_a_kept_directory_and_its_prune_waits_for_them(tmp_path, monkeypatch):
    worker = Worker(
        Settings(data_dir=tmp_path / "worker", worker_id="vm-a"), executor=ProjectExecutor()
    )
    acked = []
    worker._ack = acked.append
    source, other = "a" * 32, "b" * 32
    for attempt in (source, other):
        (worker.root / attempt / "workspace").mkdir(parents=True)
        (worker.root / attempt / "workspace" / "ckpt.pt").write_bytes(b"w")
    copying, release = threading.Barrier(3), threading.Event()

    def held_copy(previous, target, names):
        copying.wait(timeout=5)  # Both copies are inside the directory at the same time.
        release.wait(timeout=5)
        return copy_generated(previous, target, names)

    monkeypatch.setattr("labflux.worker.copy_generated", held_copy)

    def copy_into(name):
        target = tmp_path / name
        target.mkdir()
        spec = SimpleNamespace(previous_attempt_id=source, job_id="j", attempt_id=name)
        worker._continue_from(spec, target)
        return target

    def prune(attempt):
        payload = {"attempt_id": attempt, "worker_id": "vm-a"}
        return {"id": f"prune-{attempt}", "kind": "PRUNE", "payload": payload}

    try:
        with ThreadPoolExecutor(2) as pool:
            copies = [pool.submit(copy_into, name) for name in ("one", "two")]
            copying.wait(timeout=5)
            # A chain of runs prunes its oldest directory while copying its newest.
            worker.process([prune(other)])
            assert not (worker.root / other / "workspace").exists()
            # The copied directory's PRUNE stays unacked; the command loop goes on.
            worker.process([prune(source)])
            assert [command["id"] for command in acked] == [f"prune-{other}"]
            assert (worker.root / source / "workspace").exists()
            # A copy that would start after the PRUNE arrived goes without the files.
            assert files(copy_into("late")) == {}
            assert not worker.commands_ready.is_set()
            release.set()
            targets = [copy.result(timeout=5) for copy in copies]
        assert [files(target) for target in targets] == [{"ckpt.pt": b"w"}] * 2
        assert worker.commands_ready.is_set()  # The last copy wakes the command loop.
        worker.process([prune(source)])
        assert [command["id"] for command in acked] == [f"prune-{other}", f"prune-{source}"]
        assert not (worker.root / source / "workspace").exists()
    finally:
        worker.pool.shutdown(wait=True)
        worker.client.close()


def test_pruning_spares_the_directories_of_jobs_waiting_for_a_retry(
    client, service, clock, tmp_path
):
    """A node restart closes four runs of a project as lost; recording the fourth must not
    prune the first, whose retry continues from it (P2). Each goes once its job has ended."""
    register(client, cpu=4)
    jobs = []
    for _ in range(4):
        jobs.append(
            submit(client, tmp_path, project="p", retries_on_node_failure=1, safe_to_rerun=True)
        )
        clock[0] += 1  # The restart closes the attempts in this order.
    lost = [job["attempts"][0]["id"] for job in jobs]
    inventory = {"attempts": [{"attempt_id": attempt} for attempt in lost], "containers": []}
    new, response = restart(client, inventory, cpu=1)
    assert response.json()["closed"] == lost
    assert [r["attempt_id"] for r in results(client)] == lost[::-1]
    assert not any(r["deleted_by_next_run"] for r in results(client))

    def prunes():
        return {c["id"] for c in service.poll_commands("vm-a", new) if c["kind"] == "PRUNE"}

    assert prunes() == set()
    retries = []
    for job, attempt in zip(jobs, lost, strict=True):
        value = get_job(client, job)
        assert value["state"] == "STARTING" and len(value["attempts"]) == 2
        assert start_payload(service, new)["previous_attempt_id"] == attempt
        retries.append(finished(client, job, new, kept_bytes=5))
    # Each lost directory went once its retry had ended; the three latest runs stay.
    assert [r["attempt_id"] for r in results(client)] == retries[:0:-1]
    assert prunes() == {f"prune-{attempt}" for attempt in [*lost, retries[0]]}


def test_cli_lists_kept_results(client, lab, tmp_path, monkeypatch):
    worker, _ = lab
    job = run(client, worker, tmp_path, project="p")
    result = configure(monkeypatch, client).invoke(cli.app, ["results", "ls"])
    assert result.exit_code == 0, result.output
    [row] = json.loads(result.stdout)
    assert (row["project"], row["node_id"], row["job_id"]) == ("p", "vm-a", job["id"])


# Resent events -------------------------------------------------------------------------------


def post_event(client, attempt, body):
    return client.post(
        f"/internal/workers/{attempt['node_id']}/attempts/{attempt['id']}/events",
        headers=worker_headers(),
        json=body,
    )


def test_a_finished_event_with_kept_bytes_is_accepted_once(client, tmp_path):
    instance = register(client)
    attempt = submit(client, tmp_path, project="p")["attempts"][0]
    body = {
        "event_id": f"finish-{attempt['id']}",
        "instance_id": instance,
        "kind": "FINISHED",
        "outcome": "SUCCEEDED",
        "exit_code": 0,
        "termination_confirmed": True,
        "kept_bytes": 7,
    }
    assert post_event(client, attempt, body).json() == {"accepted": True}
    assert post_event(client, attempt, body).json() == {"duplicate": True}
    assert [(r["attempt_id"], r["size"]) for r in results(client)] == [(attempt["id"], 7)]
    assert post_event(client, attempt, {**body, "kept_bytes": 8}).status_code == 409


def test_an_event_stored_before_kept_bytes_still_matches_its_resend(client, service, tmp_path):
    """An event stored by a Scheduler without kept_bytes, resent after the upgrade."""
    instance = register(client)
    attempt = submit(client, tmp_path, project="p")["attempts"][0]
    body = {"event_id": "started-1", "instance_id": instance, "kind": "STARTED"}
    assert post_event(client, attempt, body).json() == {"accepted": True}
    with service.db.transaction() as connection:
        stored = dict(
            connection.execute(
                select(events.c.payload).where(events.c.id == "started-1")
            ).scalar_one()
        )
        del stored["kept_bytes"]
        connection.execute(update(events).where(events.c.id == "started-1").values(payload=stored))
    response = post_event(client, attempt, body)
    assert response.status_code == 200 and response.json() == {"duplicate": True}
    assert post_event(client, attempt, {**body, "kind": "FINISHED"}).status_code == 409


# P5: sessions --------------------------------------------------------------------------------


def test_a_session_starts_from_the_latest_result_and_never_counts_as_one(client, lab, tmp_path):
    worker, executor = lab
    executor.run_script = lambda workspace, spec: (workspace / "ckpt.pt").write_bytes(b"w1")
    trained = run(client, worker, tmp_path, {"main.py": b"train"}, project="p")
    response = client.post(
        "/api/sessions", headers=user_headers(), json={"project": "p", "resources": {"cpu": 1}}
    )
    assert response.status_code == 200, response.text
    session = response.json()
    [start] = run_pending(client, worker)
    attempt = start["attempt_id"]
    assert start["payload"]["session"] is True
    assert start["payload"]["previous_attempt_id"] == trained["current_attempt_id"]
    session_workspace = worker.root / attempt / "workspace"
    assert files(session_workspace) == {"ckpt.pt": b"w1"}

    def command(argv, cwd, output):
        output("stdout", (cwd / "ckpt.pt").read_bytes())
        (cwd / "result.txt").write_bytes(b"ok")
        return 0

    executor.script = command
    found = result_of(worker, send_command(worker, attempt, snapshot(tmp_path, {"main.py": b"t"})))
    assert found[-1]["outcome"] == "exited" and found[-1]["exit_code"] == 0
    assert any(m["type"] == "output" for m in found)
    # Only what the command changed is written back, not the copied checkpoint.
    assert writeback(found, tmp_path) == {"result.txt": b"ok"}
    client.post("/api/sessions/current/close", headers=user_headers())
    run_pending(client, worker)
    worker.handles[attempt]["future"].result(timeout=5)
    worker.flush()
    assert get_job(client, session)["state"] == "CANCELLED"
    assert not session_workspace.exists()  # The session's files end with it.
    assert [r["job_id"] for r in results(client)] == [trained["id"]]
    again = submit(client, tmp_path, project="p")
    assert (
        start_payload(client.app.state.scheduler, worker.instance_id)["previous_attempt_id"]
        == (trained["current_attempt_id"])
    )
    assert again["state"] == "STARTING"


# Migration -----------------------------------------------------------------------------------


def test_a_version_6_database_gains_the_results_table(settings):
    first = Scheduler(settings)
    with first.db.transaction() as connection:
        project_results.drop(connection)
        connection.execute(update(schema_version).values(version=6))
    first.db.close()
    database = Database(settings.data_dir)
    assert database.version == SCHEMA_VERSION == 7
    with database.transaction() as connection:
        assert connection.execute(select(func.count()).select_from(project_results)).scalar() == 0
    database.close()
