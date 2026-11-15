"""Fast-lane client (M9): project discovery, routing, stand-ins, write-back and the run loop."""

import io
import json
import os
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from labflux import fastlane
from labflux.archives import pack_directory
from labflux.config import Settings
from labflux.models import ResourceRequest
from labflux.session_protocol import (
    STANDIN_MARKER,
    CommandResult,
    ErrorMessage,
    Notice,
    Output,
    SessionView,
    WritebackChunk,
    decode,
    encode,
    iter_chunks,
)

SID = "5" * 32
CID = "c" * 32
DONE = CommandResult(id=CID, outcome="exited", exit_code=0)


def project(path: Path, text: str = "") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".labflux.toml").write_text(text)
    return path


# Project discovery and configuration ---------------------------------------------------------


def test_project_root_is_the_nearest_directory_with_the_file(tmp_path):
    outer = project(tmp_path / "outer")
    inner = project(outer / "inner")
    (inner / "src" / "pkg").mkdir(parents=True)
    assert fastlane.find_project_root(inner / "src" / "pkg") == inner
    assert fastlane.find_project_root(outer) == outer
    (tmp_path / "plain").mkdir()
    assert fastlane.find_project_root(tmp_path / "plain") is None


def test_project_file_parsing_defaults_routes_and_flag_precedence(tmp_path):
    empty = fastlane.load_project(project(tmp_path / "a"))
    request = fastlane.session_request(empty)
    assert request.resources == ResourceRequest() and request.image is None
    config = fastlane.load_project(
        project(
            tmp_path / "b",
            '[session]\ncpu = 4\nmemory_mib = 8192\ngpu = 1\ngpu_model = "A100"\n'
            'image = "img:1"\ndatasets = ["imagenet"]\n'
            '[route]\nadd = ["mytool"]\nremove = ["nvidia-smi"]\n',
        )
    )
    assert config.route.add == ["mytool"] and config.route.remove == ["nvidia-smi"]
    request = fastlane.session_request(config, cpu=2, gpu=2)
    assert request.resources.cpu == 2 and request.resources.gpu == 2
    assert request.resources.memory_mib == 8192 and request.resources.gpu_model == "A100"
    assert request.image == "img:1" and request.datasets == ["imagenet"]
    for text in ("[session]\ncpus = 4\n", "[other]\n", '[route]\nadd = ["bin/x"]\n', "cpu ="):
        with pytest.raises(fastlane.ProjectError):
            fastlane.load_project(project(tmp_path / "bad", text))


# Routing -------------------------------------------------------------------------------------


def test_route_follows_the_t7_list_and_uv_run_only(tmp_path):
    root = project(tmp_path / "p", '[route]\nadd = ["mytool"]\nremove = ["nvidia-smi"]\n')
    (root / "sub").mkdir()
    where = root / "sub"
    for name, args in [("python", ["-c", "x"]), ("pytest", []), ("mytool", ["a"])]:
        decision = fastlane.route(name, args, where)
        assert isinstance(decision, fastlane.Remote)
        assert decision.root == root and decision.argv == [name, *args]
    assert fastlane.route("uv", ["run", "pytest"], where).argv == ["uv", "run", "pytest"]
    assert isinstance(fastlane.route("uv", ["run", "x"], where), fastlane.Remote)
    local = [("uv", ["add", "torch"]), ("uv", ["lock"]), ("uv", []), ("nvidia-smi", [])]
    for name, args in [*local, ("git", ["status"])]:
        assert fastlane.route(name, args, where) == fastlane.Local([name, *args])
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert fastlane.route("python", ["-V"], outside) == fastlane.Local(["python", "-V"])


def test_argv_rewrites_only_whole_arguments_under_the_root(tmp_path):
    root = tmp_path / "proj"
    argv = ["python", str(root / "train.py"), str(root), f"--out={root}/x", str(root) + "2"]
    assert fastlane.rewrite_argv(argv, root) == [
        "python",
        "/workspace/train.py",
        "/workspace",
        f"--out={root}/x",
        str(root) + "2",
    ]


# Stand-ins and local execution ---------------------------------------------------------------


def fake_bin(tmp_path: Path) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir()
    script = directory / "python"
    script.write_text('#!/bin/sh\necho "PATH=$PATH"\nfor a in "$@"; do echo "arg=$a"; done\n')
    script.chmod(0o755)
    return directory


def test_strip_standins_removes_every_marked_directory(tmp_path):
    first = fastlane.write_standins({"python"})
    second = fastlane.write_standins({"python", "pytest"})
    try:
        bin_dir = fake_bin(tmp_path)
        path = os.pathsep.join([str(second), str(first), str(bin_dir), "/usr/bin"])
        assert fastlane.strip_standins(path) == os.pathsep.join([str(bin_dir), "/usr/bin"])
        assert fastlane.shutil.which("python", path=fastlane.strip_standins(path)) == str(
            bin_dir / "python"
        )
    finally:
        for directory in (first, second):
            fastlane.shutil.rmtree(directory)


def test_generated_standins_pass_arguments_and_run_locally_outside_a_project(tmp_path):
    names = fastlane.standin_names(fastlane.ProjectConfig.model_validate({"route": {"add": ["t"]}}))
    assert {"python", "pytest", "uv", "t"} <= names
    directory = fastlane.write_standins(names)
    try:
        assert (directory / STANDIN_MARKER).exists()
        script = directory / "python"
        assert os.access(script, os.X_OK) and '-- "$@"' in script.read_text()
        bin_dir = fake_bin(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        env = {
            "PATH": os.pathsep.join([str(directory), str(bin_dir), "/usr/bin", "/bin"]),
            "HOME": str(tmp_path),
        }
        result = subprocess.run(
            [str(script), "-c", "print(1)", "-q", "--", "x y"],
            cwd=outside,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        lines = result.stdout.splitlines()
        assert lines[0] == "PATH=" + os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"])
        assert lines[1:] == ["arg=-c", "arg=print(1)", "arg=-q", "arg=--", "arg=x y"]
    finally:
        fastlane.shutil.rmtree(directory)


def test_labflux_local_runs_the_real_command_with_a_stripped_path(tmp_path):
    directory = fastlane.write_standins({"python"})
    try:
        bin_dir = fake_bin(tmp_path)
        env = {"PATH": os.pathsep.join([str(directory), str(bin_dir), "/usr/bin", "/bin"])}
        command = [sys.executable, "-I", "-m", "labflux.cli", "local", "--"]
        result = subprocess.run(
            [*command, "python", "-q"],
            cwd=project(tmp_path / "p"),  # Inside a project, `local` still stays local.
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [
            "PATH=" + os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]),
            "arg=-q",
        ]
        missing = subprocess.run(
            [*command, "no-such-command-xyz"], env=env, capture_output=True, text=True, timeout=30
        )
        assert missing.returncode == 127
        assert "no-such-command-xyz: command not found" in missing.stderr
    finally:
        fastlane.shutil.rmtree(directory)


@pytest.mark.parametrize(("signum", "expected"), [(signal.SIGTERM, 143), (signal.SIGHUP, 129)])
def test_labflux_shell_passes_hangup_on_and_removes_its_standins(tmp_path, signum, expected):
    seen = tmp_path / "path.txt"
    shell = tmp_path / "fake-shell"
    shell.write_text(f'#!/bin/sh\necho "$PATH" > {seen}.tmp\nmv {seen}.tmp {seen}\nexec sleep 30\n')
    shell.chmod(0o755)
    process = subprocess.Popen(
        [sys.executable, "-I", "-m", "labflux.cli", "shell", "--shell", str(shell)],
        cwd=tmp_path,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while not seen.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        standins = Path(seen.read_text().split(os.pathsep)[0])
        assert (standins / STANDIN_MARKER).exists()
        process.send_signal(signum)
        assert process.wait(timeout=30) == expected
        assert not standins.exists()
    finally:
        process.kill()


# Write-back ----------------------------------------------------------------------------------


def archive_of(tmp_path: Path, files: dict[str, bytes]) -> Path:
    source = tmp_path / "remote"
    for relative, data in files.items():
        (source / relative).parent.mkdir(parents=True, exist_ok=True)
        (source / relative).write_bytes(data)
    target = tmp_path / "writeback.tar.gz"
    pack_directory(source, target, source=False)
    return target


def apply(root, archive, t0):
    return fastlane.apply_writeback(root, archive, t0, max_bytes=10**6, max_members=100)


def test_writeback_places_new_and_unchanged_files(tmp_path):
    root = project(tmp_path / "p")
    (root / "results.json").write_text("old")
    os.utime(root / "results.json", ns=(1, 1))
    t0 = time.time_ns()
    archive = archive_of(
        tmp_path,
        {"results.json": b"new", "a/b/gen.txt": b"gen", ".venv/x": b"v", "m.pyc": b"c"},
    )
    assert apply(root, archive, t0) == []
    assert (root / "results.json").read_text() == "new"
    assert (root / "a" / "b" / "gen.txt").read_text() == "gen"
    assert not (root / ".venv").exists() and not (root / "m.pyc").exists()
    assert list((root / ".labflux").iterdir()) == []  # The staging directory is removed.


def test_writeback_conflicts_keep_local_files_and_save_remote_copies(tmp_path):
    root = project(tmp_path / "p")
    outside = tmp_path / "outside"
    outside.mkdir()
    t0 = time.time_ns()
    (root / "edited.txt").write_text("local edit")
    os.utime(root / "edited.txt", ns=(t0, t0))  # Edited when the command started.
    (root / "link.txt").symlink_to(outside / "target.txt")
    (root / "dir.txt").mkdir()
    (root / "linked").symlink_to(outside)
    archive = archive_of(
        tmp_path,
        {
            "edited.txt": b"remote",
            "link.txt": b"remote",
            "dir.txt": b"remote",
            "linked/escape.txt": b"remote",
        },
    )
    notices = apply(root, archive, t0)
    assert (root / "edited.txt").read_text() == "local edit"
    assert (root / "edited.txt.remote").read_text() == "remote"
    assert (root / "link.txt").is_symlink() and (root / "link.txt.remote").read_text() == "remote"
    assert (root / "dir.txt").is_dir() and (root / "dir.txt.remote").read_text() == "remote"
    assert list(outside.iterdir()) == []
    assert any("edited.txt changed locally" in notice for notice in notices)
    assert any("linked/escape.txt was not written back" in notice for notice in notices)


@pytest.mark.parametrize("name", ["../escape.txt", "/abs.txt"])
def test_writeback_rejects_unsafe_archives(tmp_path, name):
    root = project(tmp_path / "p")
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError):
        apply(root, archive, time.time_ns())
    assert not (tmp_path / "escape.txt").exists()
    assert sorted(path.name for path in root.iterdir()) == [".labflux", ".labflux.toml"]


# The run loop against a fake Scheduler -------------------------------------------------------


class FakeScheduler:
    """The three session endpoints, the job lookup and the command WebSocket."""

    def __init__(self):
        self.states = ["RUNNING"]  # None: no current session (GET answers 404).
        self.job = None  # GET /api/jobs/{id} body; 404 when None.
        self.post_response = None
        self.created = True
        self.opened = 0
        self.closed = 0
        self.polls = 0
        self.on_poll = None
        self.connections = 0
        self.received = []
        self.disconnected = threading.Event()
        self.script = None
        self.read_all = True  # False: the script runs right after the ExecRequest.
        app = FastAPI()

        def view(state):
            return SessionView(
                id=SID,
                state=state,
                reason="interactive session waits for cpu",
                resources=ResourceRequest(),
                image="python:3.12-slim",
                created_at=0,
                mode="session",
                created=self.created,
            ).model_dump()

        @app.post("/api/sessions")
        def open_session(body: dict):
            self.opened += 1
            if self.post_response:
                return JSONResponse(*self.post_response)
            return view(self.states[0])

        @app.get("/api/sessions/current")
        def current():
            self.polls += 1
            if self.on_poll:
                self.on_poll(self.polls)
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return view(state) if state else JSONResponse({"detail": "no session"}, 404)

        @app.get("/api/jobs/{job_id}")
        def job(job_id: str):
            return self.job if self.job else JSONResponse({"detail": "unknown job"}, 404)

        @app.post("/api/sessions/current/close")
        def close():
            self.closed += 1
            return view("CANCELLED")

        @app.websocket("/api/sessions/{session_id}/ws")
        async def command(websocket: WebSocket, session_id: str):
            assert websocket.headers["authorization"] == "Bearer alice-token"
            await websocket.accept()
            self.connections += 1
            messages = [await websocket.receive_json()]
            while self.read_all and not messages[-1].get("final"):
                messages.append(await websocket.receive_json())
            self.received.append(messages)
            try:
                await (self.script or finish_with(DONE))(websocket, messages)
                await websocket.close()
            except WebSocketDisconnect:
                self.disconnected.set()

        self.app = app


def finish_with(*messages):
    async def script(websocket, _received):
        for message in messages:
            await websocket.send_text(message.model_dump_json())

    return script


@pytest.fixture
def fake():
    scheduler = FakeScheduler()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    scheduler.url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(scheduler.app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    yield scheduler
    server.should_exit = True
    thread.join(timeout=10)


def run(fake, tmp_path, argv, cwd=None, **settings):
    root = project(tmp_path / "proj")
    (root / "main.py").write_text("print('hi')\n")
    config = Settings(
        data_dir=tmp_path / "data",
        scheduler_url=fake.url,
        users={"alice": "alice-token"},
        user_token="alice-token",
        **settings,
    )
    return fastlane.run_command(config, root, fastlane.load_project(root), argv, cwd or root)


def test_run_passes_output_bytes_and_exit_codes_through(fake, tmp_path, capsysbinary):
    fake.script = finish_with(
        Output(id=CID, stream="stdout", data=encode(b"hi\x00\xff\n")),
        Output(id=CID, stream="stderr", data=encode(b"\x1b[31merr\n")),
        Notice(id=CID, text="running uv sync"),
        CommandResult(id=CID, outcome="exited", exit_code=3),
    )
    (tmp_path / "proj" / "sub").mkdir(parents=True)
    root = tmp_path / "proj"
    code = run(fake, tmp_path, ["python", str(root / "main.py")], cwd=root / "sub")
    out = capsysbinary.readouterr()
    assert code == 3
    assert out.out == b"hi\x00\xff\n"
    assert b"\x1b[31merr\n" in out.err and b"labflux: running uv sync" in out.err
    exec_request = fake.received[0][0]
    assert exec_request["argv"] == ["python", "/workspace/main.py"]
    assert exec_request["cwd"] == "sub"
    chunks = b"".join(decode(message["data"]) for message in fake.received[0][1:])
    assert len(chunks) == exec_request["snapshot_size"]
    assert fastlane.hashlib.sha256(chunks).hexdigest() == exec_request["snapshot_sha256"]


def test_run_applies_the_writeback_and_reports_timeouts(fake, tmp_path, capsysbinary):
    archive = archive_of(tmp_path, {"results.json": b'{"acc": 1}'})
    with archive.open("rb") as stream:
        chunks = [WritebackChunk(id=CID, data=data, final=end) for data, end in iter_chunks(stream)]
    fake.script = finish_with(
        *chunks,
        CommandResult(
            id=CID,
            outcome="timeout",
            reason="command time limit exceeded",
            session_ended=True,
            writeback_size=archive.stat().st_size,
            writeback_sha256=fastlane._sha256(archive),
            writeback_files=1,
        ),
    )
    assert run(fake, tmp_path, ["python", "main.py"]) == 124
    assert json.loads((tmp_path / "proj" / "results.json").read_text()) == {"acc": 1}
    err = capsysbinary.readouterr().err
    assert b"command time limit exceeded" in err and b"next command starts a new one" in err


@pytest.mark.parametrize("wrong", ["sha256", "size"])
def test_run_rejects_a_writeback_that_fails_its_checksum(fake, tmp_path, capsysbinary, wrong):
    archive = archive_of(tmp_path, {"results.json": b"{}"})
    with archive.open("rb") as stream:
        chunks = [WritebackChunk(id=CID, data=data, final=end) for data, end in iter_chunks(stream)]
    size, digest = archive.stat().st_size, fastlane._sha256(archive)
    fake.script = finish_with(
        *chunks,
        CommandResult(
            id=CID,
            outcome="exited",
            exit_code=0,
            writeback_size=size + 1 if wrong == "size" else size,
            writeback_sha256="0" * 64 if wrong == "sha256" else digest,
            writeback_files=1,
        ),
    )
    assert run(fake, tmp_path, ["python", "main.py"]) == 125
    assert not (tmp_path / "proj" / "results.json").exists()
    assert b"failed its checksum" in capsysbinary.readouterr().err


def test_run_errors_and_worker_unavailable_retry(fake, tmp_path, capsysbinary):
    replies = [
        ErrorMessage(code="worker_unavailable", message="no relay"),
        ErrorMessage(code="busy", message="one command at a time"),
    ]

    async def script(websocket, _received):
        await websocket.send_text(replies.pop(0).model_dump_json())

    fake.script = script
    assert run(fake, tmp_path, ["python", "main.py"]) == 125
    assert fake.connections == 2
    assert b"busy: one command at a time" in capsysbinary.readouterr().err


def test_run_leases_again_once_after_not_ready(fake, tmp_path, capsysbinary):
    replies = [ErrorMessage(code="not_ready", message="session restarted"), DONE]

    async def script(websocket, _received):
        await websocket.send_text(replies.pop(0).model_dump_json())

    fake.script = script
    assert run(fake, tmp_path, ["python", "main.py"]) == 0
    assert fake.opened == 2 and fake.connections == 2


def test_run_reads_an_error_sent_while_the_snapshot_is_uploading(fake, tmp_path, capsysbinary):
    root = project(tmp_path / "proj")
    (root / "weights.bin").write_bytes(os.urandom(4 * 1024**2))  # Many chunks, incompressible.
    fake.read_all = False
    fake.script = finish_with(ErrorMessage(code="busy", message="one command at a time"))
    assert run(fake, tmp_path, ["python", "main.py"]) == 125
    assert len(fake.received[0]) == 1  # The reply came before any chunk was read.
    err = capsysbinary.readouterr().err
    assert b"busy: one command at a time" in err and b"connection" not in err


def test_run_conflict_and_oversized_snapshot_fail_with_125(fake, tmp_path, capsysbinary):
    fake.post_response = ({"detail": "you already have session 55555555"}, 409)
    assert run(fake, tmp_path, ["python", "main.py"]) == 125
    assert b"you already have session 55555555" in capsysbinary.readouterr().err
    fake.post_response = None
    assert run(fake, tmp_path, ["python", "main.py"], max_source_bytes=10) == 125
    assert b"above max_source_bytes" in capsysbinary.readouterr().err
    assert fake.connections == 0


def test_run_waits_once_and_ctrl_c_closes_a_session_it_created(fake, tmp_path, capsysbinary):
    fake.states = ["QUEUED"]
    fake.on_poll = lambda count: count == 3 and os.kill(os.getpid(), signal.SIGINT)
    assert run(fake, tmp_path, ["python", "main.py"]) == 130
    assert fake.closed == 1 and fake.connections == 0
    assert capsysbinary.readouterr().err.count(b"waiting for a session") == 1
    fake.created, fake.polls = False, 0
    assert run(fake, tmp_path, ["python", "main.py"]) == 130
    assert fake.closed == 1  # Not created by this invocation: left alone.


def test_run_reports_a_session_that_ended_while_waiting(fake, tmp_path, capsysbinary):
    fake.states = ["QUEUED", "UNKNOWN"]
    assert run(fake, tmp_path, ["python", "main.py"]) == 125
    assert b"labflux session close" in capsysbinary.readouterr().err
    # No longer the current session: the state and reason come from the job row.
    fake.states = ["QUEUED", None]
    fake.job = {"id": SID, "state": "CANCELLED", "reason": "closed by the user"}
    assert run(fake, tmp_path, ["python", "main.py"]) == 125
    assert b"ended (CANCELLED): closed by the user" in capsysbinary.readouterr().err
    assert fake.connections == 0


def test_ctrl_c_sends_cancel_and_keeps_the_result(fake, tmp_path, capsysbinary):
    seen = []

    async def script(websocket, _received):
        await websocket.send_text(
            Output(id=CID, stream="stdout", data=encode(b"1\n")).model_dump_json()
        )
        os.kill(os.getpid(), signal.SIGINT)
        seen.append(await websocket.receive_json())
        await websocket.send_text(Notice(id=CID, text="still here").model_dump_json())
        result = CommandResult(id=CID, outcome="cancelled", session_ended=True)
        await websocket.send_text(result.model_dump_json())

    fake.script = script
    assert run(fake, tmp_path, ["python", "main.py"]) == 130
    assert seen == [{"type": "cancel", "id": ""}]
    out = capsysbinary.readouterr()
    assert out.out == b"1\n" and b"still here" in out.err


@pytest.mark.parametrize(
    ("signals", "expected"),
    [([signal.SIGINT, signal.SIGINT], 130), ([signal.SIGTERM], 143), ([signal.SIGHUP], 129)],
)
def test_second_ctrl_c_and_termination_close_the_socket(fake, tmp_path, signals, expected):
    async def script(websocket, _received):
        await websocket.send_text(
            Output(id=CID, stream="stdout", data=encode(b"1\n")).model_dump_json()
        )
        for signum in signals:
            os.kill(os.getpid(), signum)
            if signum == signal.SIGINT:
                assert (await websocket.receive_json())["type"] == "cancel"
        await websocket.receive_json()  # Until the client disconnects.

    fake.script = script
    assert run(fake, tmp_path, ["python", "main.py"]) == expected
    assert fake.disconnected.wait(5)
