"""M9 Worker side: session commands, snapshots, dependency sync and write-back, no Docker."""

import hashlib
import io
import json
import os
import tarfile
import threading
import time
import uuid
from pathlib import Path

import pytest
from test_doorbell import wait_for

from labflux.archives import extract_archive, pack_directory
from labflux.config import Settings
from labflux.models import JobSpec
from labflux.runtime import ExecutionResult
from labflux.session_protocol import (
    KEEPALIVE_ARGV,
    SESSION_ENV,
    WORKSPACE,
    WorkerExec,
    decode,
    iter_chunks,
)
from labflux.worker import Worker
from labflux.workspace import apply_snapshot, pack_files, scan

LIMITS = {
    "timeout_s": 5,
    "sync_timeout_s": 5,
    "writeback_limit_bytes": 1024**2,
    "output_limit_bytes": 1024**2,
}


class SessionExecutor:
    """A fake session container: run() stays up until cancelled; exec() calls a Python
    function on the host workspace, the way a command sees /workspace through the bind mount.

    A script returns an exit code, or "hang" to block until a timeout, a stop or the end of
    the container."""

    def __init__(self):
        self.alive: dict[str, bool] = {}
        self.workspaces: dict[str, Path] = {}
        self.calls: list[list[str]] = []
        self.script = lambda argv, cwd, output: 0
        self.sync_code = 0

    def check(self):
        return {}

    def run(self, spec, workspace, outputs, on_log, on_started, cancel, **options):
        workspace.mkdir(parents=True, exist_ok=True)
        self.workspaces[spec.attempt_id] = workspace
        self.alive[spec.attempt_id] = True
        if spec.session:
            on_started()
        cancel.wait()
        self.alive[spec.attempt_id] = False
        return ExecutionResult("CANCELLED", 137, "cancelled", True)

    def exec(self, spec, argv, workdir, on_output, stop, timeout_s, on_abort=None):
        self.calls.append(list(argv))
        workspace = self.workspaces[spec.attempt_id]
        cwd = workspace / os.path.relpath(workdir, WORKSPACE)
        if argv[0] == "uv":
            (workspace / ".venv").mkdir(exist_ok=True)
            on_output("stdout", b"Resolved 0 packages\n")
            return self.sync_code, False, False
        code = self.script(argv, cwd, on_output)
        if code != "hang":
            return code, False, False
        deadline = time.monotonic() + timeout_s
        while self.alive[spec.attempt_id]:
            if stop.is_set() or time.monotonic() >= deadline:
                timed_out = not stop.is_set()
                on_abort(timed_out)
                wait_for(lambda: not self.alive[spec.attempt_id])
                return None, timed_out, not timed_out
            time.sleep(0.01)
        return None, False, False  # The container went away (STOP).


@pytest.fixture
def lab(tmp_path):
    executor = SessionExecutor()
    worker = Worker(Settings(data_dir=tmp_path / "worker", worker_id="vm-a"), executor=executor)
    worker.sent = []
    worker.send = lambda message: worker.sent.append(message) or True
    yield worker, executor, tmp_path
    for handle in worker.handles.values():
        handle["cancel"].set()
    worker.pool.shutdown(wait=True)
    worker.client.close()


def start_session(worker, session=True):
    attempt_id = uuid.uuid4().hex
    spec = JobSpec(image="python:3.12-slim", argv=list(KEEPALIVE_ARGV), env=dict(SESSION_ENV))
    payload = {
        "job_id": uuid.uuid4().hex,
        "attempt_id": attempt_id,
        "worker_id": "vm-a",
        "job": spec.model_dump(),
        "gpu_uuids": [],
    }
    if session:
        payload["session"] = True
    worker.accept({"id": f"start-{attempt_id}", "kind": "START", "payload": payload})
    if session:
        wait_for(lambda: worker._read(attempt_id)["started"])
    return attempt_id


def snapshot(tmp_path, files: dict[str, bytes]) -> bytes:
    project = tmp_path / f"project-{uuid.uuid4().hex}"
    project.mkdir()
    for name, data in files.items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_bytes(data)
    archive = tmp_path / f"{project.name}.tar.gz"
    pack_directory(project, archive)
    return archive.read_bytes()


def send_command(worker, attempt_id, data, argv=("python", "main.py"), cwd="", **limits):
    """Deliver a command as the Scheduler would; returns its ID."""
    command = WorkerExec(
        id=uuid.uuid4().hex,
        attempt_id=attempt_id,
        argv=list(argv),
        cwd=cwd,
        snapshot_size=len(data),
        snapshot_sha256=hashlib.sha256(data).hexdigest(),
        **{**LIMITS, **limits},
    )
    worker._on_message(json.dumps(command.model_dump()))
    for text, final in iter_chunks(io.BytesIO(data)):
        message = {"type": "snapshot_chunk", "id": command.id, "data": text, "final": final}
        worker._on_message(json.dumps(message))
    return command.id


def messages(worker, command_id):
    return [message for message in worker.sent if message.get("id") == command_id]


def result_of(worker, command_id):
    def done():
        found = messages(worker, command_id)
        return found if found and found[-1]["type"] == "result" else None

    return wait_for(done)


def run(worker, attempt_id, data, **options):
    found = result_of(worker, send_command(worker, attempt_id, data, **options))
    return found, found[-1]


def writeback(found, tmp_path) -> dict[str, bytes]:
    data = b"".join(decode(m["data"]) for m in found if m["type"] == "writeback_chunk")
    if not data:
        return {}
    assert hashlib.sha256(data).hexdigest() == found[-1]["writeback_sha256"]
    archive = tmp_path / f"writeback-{uuid.uuid4().hex}.tar.gz"
    archive.write_bytes(data)
    target = tmp_path / archive.name.replace(".tar.gz", "")
    extract_archive(archive, target, max_bytes=1024**2, max_members=100)
    return {
        p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()
    }


# Workspace functions -------------------------------------------------------------------------


def test_apply_snapshot_mirrors_snapshot_files_and_keeps_generated_ones(tmp_path):
    workspace = tmp_path / "attempt" / "workspace"
    archive = tmp_path / "first.tar.gz"
    archive.write_bytes(snapshot(tmp_path, {"a.py": b"1", "pkg/old.py": b"2", "keep.py": b"3"}))
    first = apply_snapshot(workspace, archive, [], 1024**2, 100)
    assert first == ["a.py", "keep.py", "pkg/old.py"]
    (workspace / "generated.txt").write_text("made by a command")
    (workspace / ".venv" / "bin").mkdir(parents=True)
    (workspace / ".venv" / "bin" / "python").write_text("venv")
    archive.write_bytes(snapshot(tmp_path, {"a.py": b"changed", "keep.py": b"3"}))
    second = apply_snapshot(workspace, archive, first, 1024**2, 100)
    assert second == ["a.py", "keep.py"]
    assert (workspace / "a.py").read_bytes() == b"changed"
    assert not (workspace / "pkg").exists()  # Deleted, and its empty directory pruned.
    assert (workspace / "generated.txt").exists()
    assert (workspace / ".venv" / "bin" / "python").exists()
    assert not (tmp_path / "attempt" / "snapshot-new").exists()


def test_apply_snapshot_never_writes_or_deletes_through_links(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "old.py").write_text("outside")
    (outside / "a.py").write_text("outside")
    workspace = tmp_path / "attempt" / "workspace"
    archive = tmp_path / "first.tar.gz"
    archive.write_bytes(snapshot(tmp_path, {"pkg/old.py": b"1", "file.py": b"2"}))
    first = apply_snapshot(workspace, archive, [], 1024**2, 100)
    # A command replaces the directory and a file with links pointing outside.
    for name in ("pkg/old.py",):
        (workspace / name).unlink()
    (workspace / "pkg").rmdir()
    (workspace / "pkg").symlink_to(outside)
    (workspace / "file.py").unlink()
    (workspace / "file.py").symlink_to(outside / "a.py")
    archive.write_bytes(snapshot(tmp_path, {"pkg/a.py": b"new", "file.py": b"new"}))
    apply_snapshot(workspace, archive, first, 1024**2, 100)
    assert (outside / "old.py").read_text() == "outside"  # Not deleted through the link.
    assert (outside / "a.py").read_text() == "outside"  # Not written through either link.
    assert not (workspace / "pkg").is_symlink() and (workspace / "pkg" / "a.py").read_bytes()
    assert not (workspace / "file.py").is_symlink()
    assert (workspace / "file.py").read_bytes() == b"new"


def test_apply_snapshot_rejects_unsafe_archives_before_touching_the_workspace(tmp_path):
    workspace = tmp_path / "attempt" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "kept.py").write_text("x")
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as bad:
        member = tarfile.TarInfo("../escape.py")
        member.size = 1
        bad.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(ValueError):
        apply_snapshot(workspace, archive, ["kept.py"], 1024**2, 100)
    assert (workspace / "kept.py").exists()
    assert not (tmp_path / "attempt" / "escape.py").exists()


def test_scan_and_pack_include_only_regular_files_outside_excluded_directories(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "src" / "__pycache__").mkdir(parents=True)
    (workspace / "src" / "a.py").write_text("a")
    (workspace / "src" / "__pycache__" / "a.cpython-312.pyc").write_text("c")
    (workspace / "b.pyc").write_text("c")
    (workspace / ".venv").mkdir()
    (workspace / ".venv" / "x").write_text("v")
    (workspace / "link.txt").symlink_to(workspace / "src" / "a.py")
    assert list(scan(workspace)) == ["src/a.py"]
    target = tmp_path / "out.tar.gz"
    assert pack_files(workspace, ["src/a.py", "link.txt", "gone.txt"], target) == ["src/a.py"]
    with tarfile.open(target) as packed:
        [member] = packed.getmembers()
        assert (member.name, member.mtime, member.uid) == ("src/a.py", 0, 0)


# Commands ------------------------------------------------------------------------------------


def test_a_command_streams_output_then_write_back_then_its_result(lab):
    worker, executor, tmp_path = lab
    attempt_id = start_session(worker)

    def script(argv, cwd, output):
        output("stdout", b"hello\n")
        output("stderr", b"warn\n")
        (cwd / "new.txt").write_text("new")
        (cwd / "a.txt").write_text("modified!")
        (cwd / "__pycache__").mkdir()
        (cwd / "__pycache__" / "m.pyc").write_text("cache")
        (cwd / ".venv").mkdir()
        (cwd / ".venv" / "lib.py").write_text("venv")
        (cwd / "link.txt").symlink_to(cwd / "b.txt")
        return 3

    executor.script = script
    data = snapshot(tmp_path, {"a.txt": b"a", "b.txt": b"b"})
    found, result = run(worker, attempt_id, data)
    assert [m["type"] for m in found] == ["output", "output", "writeback_chunk", "result"]
    assert decode(found[0]["data"]) == b"hello\n" and found[1]["stream"] == "stderr"
    assert (result["outcome"], result["exit_code"], result["session_ended"]) == ("exited", 3, False)
    assert result["writeback_files"] == 2
    assert writeback(found, tmp_path) == {"a.txt": b"modified!", "new.txt": b"new"}
    assert executor.calls == [["python", "main.py"]]
    # Files persist into the next command, which sees the same workspace.
    executor.script = lambda argv, cwd, output: 0 if (cwd / "new.txt").exists() else 1
    found, result = run(worker, attempt_id, data)
    assert result["exit_code"] == 0
    assert result["writeback_files"] == 0 and len(found) == 1
    assert not list((tmp_path / "worker").rglob("snapshot-*.tar.gz"))
    assert not list((tmp_path / "worker").rglob("writeback-*.tar.gz"))


def test_the_command_runs_in_its_working_directory_inside_the_workspace(lab):
    worker, executor, tmp_path = lab
    attempt_id = start_session(worker)
    seen = []
    executor.script = lambda argv, cwd, output: seen.append(cwd) or 0
    data = snapshot(tmp_path, {"src/main.py": b""})
    assert run(worker, attempt_id, data, cwd="src")[1]["exit_code"] == 0
    assert seen == [executor.workspaces[attempt_id] / "src"]
    workspace = executor.workspaces[attempt_id]
    (workspace / "out").symlink_to(tmp_path)
    for cwd in ("missing", "out"):
        result = run(worker, attempt_id, data, cwd=cwd)[1]
        assert result["outcome"] == "error"
        assert result["reason"] == f"working directory {cwd} is not in the session workspace"


def test_dependency_changes_trigger_uv_sync_once(lab):
    worker, executor, tmp_path = lab
    attempt_id = start_session(worker)
    project = {"pyproject.toml": b"[project]\nname='x'\n", "uv.lock": b"v1", "main.py": b""}
    found, result = run(worker, attempt_id, snapshot(tmp_path, project))
    assert executor.calls == [["uv", "sync", "--locked"], ["python", "main.py"]]
    assert found[0] == {"type": "notice", "id": found[0]["id"], "text": "running uv sync"}
    assert (found[1]["stream"], decode(found[1]["data"])) == ("stderr", b"Resolved 0 packages\n")
    assert result["exit_code"] == 0
    run(worker, attempt_id, snapshot(tmp_path, {**project, "main.py": b"# edited"}))
    assert executor.calls[2:] == [["python", "main.py"]]  # Unchanged dependencies.
    run(worker, attempt_id, snapshot(tmp_path, {**project, "uv.lock": b"v2"}))
    assert executor.calls[3:] == [["uv", "sync", "--locked"], ["python", "main.py"]]
    del project["uv.lock"]
    run(worker, attempt_id, snapshot(tmp_path, project))
    assert executor.calls[5:] == [["uv", "sync"], ["python", "main.py"]]


def test_a_failed_sync_skips_the_command_and_is_retried(lab):
    worker, executor, tmp_path = lab
    attempt_id = start_session(worker)
    data = snapshot(tmp_path, {"pyproject.toml": b"[project]\n", "uv.lock": b"v1"})
    executor.sync_code = 2
    result = run(worker, attempt_id, data)[1]
    assert (result["outcome"], result["exit_code"], result["reason"]) == (
        "exited",
        2,
        "uv sync failed",
    )
    assert executor.calls == [["uv", "sync", "--locked"]]
    executor.sync_code = 0
    run(worker, attempt_id, data)
    assert executor.calls[1:] == [["uv", "sync", "--locked"], ["python", "main.py"]]


def test_the_write_back_cap_skips_and_lists_files(lab):
    worker, executor, tmp_path = lab
    attempt_id = start_session(worker)

    def script(argv, cwd, output):
        (cwd / "big.bin").write_bytes(b"x" * 100)
        (cwd / "small.txt").write_bytes(b"12345")
        return 0

    executor.script = script
    found, result = run(worker, attempt_id, snapshot(tmp_path, {}), writeback_limit_bytes=10)
    assert result["skipped"] == [{"path": "big.bin", "size": 100}]
    assert result["skipped_count"] == 1
    [notice] = [m for m in found if m["type"] == "notice"]
    assert notice["text"].startswith("1 files (0.0 MB) over the write-back limit stay in")
    assert notice["text"].endswith(": big.bin")
    assert writeback(found, tmp_path) == {"small.txt": b"12345"}


def test_the_output_cap_truncates_with_one_notice(lab):
    worker, executor, tmp_path = lab
    attempt_id = start_session(worker)

    def script(argv, cwd, output):
        for _ in range(3):
            output("stdout", b"0123456789ab")
        return 0

    executor.script = script
    found, _ = run(worker, attempt_id, snapshot(tmp_path, {}), output_limit_bytes=20)
    assert b"".join(decode(m["data"]) for m in found if m["type"] == "output") == (
        b"0123456789ab01234567"
    )
    assert [m["text"] for m in found if m["type"] == "notice"] == ["output truncated at 20 bytes"]


@pytest.mark.parametrize("ending", ["timeout", "cancel", "relay"])
def test_a_command_that_does_not_finish_ends_the_session_and_keeps_its_output(lab, ending):
    worker, executor, tmp_path = lab
    attempt_id = start_session(worker)

    def script(argv, cwd, output):
        output("stdout", b"partial\n")
        (cwd / "results.json").write_text("{}")
        return "hang"

    executor.script = script
    timeout = 0.2 if ending == "timeout" else 30
    command_id = send_command(worker, attempt_id, snapshot(tmp_path, {}), timeout_s=timeout)
    wait_for(lambda: messages(worker, command_id))
    if ending == "cancel":
        worker._on_message(json.dumps({"type": "cancel", "id": command_id}))
    elif ending == "relay":
        worker._relay_lost()  # The doorbell connection dropped.
    found = result_of(worker, command_id)
    result = found[-1]
    outcome, reason = {
        "timeout": ("timeout", "command time limit exceeded"),
        "cancel": ("cancelled", "command cancelled by the client"),
        "relay": ("cancelled", "relay connection lost during a command"),
    }[ending]
    assert (result["outcome"], result["reason"], result["session_ended"]) == (outcome, reason, True)
    assert writeback(found, tmp_path) == {"results.json": b"{}"}
    # run() confirmed the stop; the attempt's FINISHED reason says why.
    wait_for(lambda: worker._read(attempt_id)["result"])
    assert worker._read(attempt_id)["result"]["reason"] == reason
    assert worker._read(attempt_id)["result"]["outcome"] == "CANCELLED"
    assert attempt_id not in worker.end_reasons


def test_a_stop_during_a_command_reports_the_session_closed(lab):
    worker, executor, tmp_path = lab
    attempt_id = start_session(worker)
    executor.script = lambda argv, cwd, output: "hang"
    command_id = send_command(worker, attempt_id, snapshot(tmp_path, {}), timeout_s=30)
    wait_for(lambda: executor.calls)
    worker.handles[attempt_id]["cancel"].set()  # A STOP: lease expiry or `session close`.
    result = result_of(worker, command_id)[-1]
    assert (result["outcome"], result["reason"], result["session_ended"]) == (
        "cancelled",
        "session closed",
        True,
    )


def test_commands_only_for_this_workers_running_sessions(lab):
    worker, executor, tmp_path = lab
    data = snapshot(tmp_path, {})
    batch = start_session(worker, session=False)
    executor.script = lambda argv, cwd, output: "hang"
    for attempt_id in (uuid.uuid4().hex, batch):
        command_id = send_command(worker, attempt_id, data)
        [result] = messages(worker, command_id)
        assert (result["outcome"], result["reason"]) == (
            "error",
            "session is not running on this worker",
        )
    attempt_id = start_session(worker)
    first = send_command(worker, attempt_id, data, timeout_s=30)
    wait_for(lambda: executor.calls)
    second = send_command(worker, attempt_id, data)
    assert [(m["outcome"], m["reason"]) for m in messages(worker, second)] == [("error", "busy")]
    worker._on_message(json.dumps({"type": "cancel", "id": first}))
    assert result_of(worker, first)[-1]["outcome"] == "cancelled"
    wait_for(lambda: worker._read(attempt_id)["result"])
    later = send_command(worker, attempt_id, data)  # The session has ended.
    assert messages(worker, later)[-1]["reason"] == "session is not running on this worker"


def test_a_bad_snapshot_fails_the_command_but_not_the_session(lab):
    worker, executor, tmp_path = lab
    attempt_id = start_session(worker)
    data = snapshot(tmp_path, {"a.py": b"1"})
    command = WorkerExec(
        id=uuid.uuid4().hex,
        attempt_id=attempt_id,
        argv=["python"],
        snapshot_size=len(data),
        snapshot_sha256="0" * 64,
        **LIMITS,
    )
    worker._on_message(json.dumps(command.model_dump()))
    for text, final in iter_chunks(io.BytesIO(data)):
        message = {"type": "snapshot_chunk", "id": command.id, "data": text, "final": final}
        worker._on_message(json.dumps(message))
    [result] = messages(worker, command.id)
    assert (result["outcome"], result["reason"]) == ("error", "snapshot size or checksum mismatch")
    truncated = send_command(worker, attempt_id, data[:10], argv=["python"])
    result = result_of(worker, truncated)[-1]
    assert result["outcome"] == "error" and result["reason"].startswith("snapshot rejected")
    # A cancel while the snapshot is still arriving drops the command; nothing ran.
    pending = WorkerExec(**{**command.model_dump(), "id": uuid.uuid4().hex})
    worker._on_message(json.dumps(pending.model_dump()))
    worker._on_message(json.dumps({"type": "cancel", "id": pending.id}))
    assert messages(worker, pending.id)[-1]["outcome"] == "cancelled"
    assert run(worker, attempt_id, data)[1]["outcome"] == "exited"
    assert executor.calls == [["python", "main.py"]]


def test_a_failed_send_ends_sessions_with_a_command_in_flight(lab):
    worker, executor, tmp_path = lab
    attempt_id = start_session(worker)
    sending = threading.Event()

    def script(argv, cwd, output):
        sending.wait(5)
        output("stdout", b"nobody is listening\n")
        return "hang"

    executor.script = script
    send_command(worker, attempt_id, snapshot(tmp_path, {}), timeout_s=30)
    wait_for(lambda: executor.calls)
    worker.send = lambda message: False  # The doorbell connection is gone.
    sending.set()
    wait_for(lambda: worker._read(attempt_id)["result"])
    assert worker._read(attempt_id)["result"]["reason"] == "relay connection lost during a command"
