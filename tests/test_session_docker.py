"""M9 session containers on real Docker (CPU only): keep-alive, docker exec, timeouts, uv sync."""

import hashlib
import io
import json
import shutil
import threading
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

import docker
import pytest
from test_doorbell import wait_for

from labflux.archives import pack_directory
from labflux.config import Settings
from labflux.models import ExecutionSpec, JobSpec, ResourceRequest
from labflux.runtime import DockerExecutor
from labflux.session_protocol import (
    KEEPALIVE_ARGV,
    SESSION_ENV,
    WORKSPACE,
    WorkerExec,
    decode,
    iter_chunks,
)
from labflux.worker import Worker

pytestmark = pytest.mark.docker

PROBE = Path(__file__).parent / "fixtures" / "uv-probe"


@pytest.fixture
def runtime(tmp_path):
    executor = DockerExecutor(
        Settings(data_dir=tmp_path, stop_grace_s=1, uv_cache_dir=tmp_path / "uv-cache")
    )
    try:
        executor.check()
    except (docker.errors.DockerException, OSError, RuntimeError) as exc:
        pytest.skip(f"Docker integration prerequisites unavailable: {exc}")
    return executor


def session_spec(image="python:3.12-slim") -> ExecutionSpec:
    return ExecutionSpec(
        job_id=uuid.uuid4().hex,
        attempt_id=uuid.uuid4().hex,
        worker_id=f"fl-{uuid.uuid4().hex[:8]}",
        job=JobSpec(
            image=image,
            argv=list(KEEPALIVE_ARGV),
            env=dict(SESSION_ENV),
            resources=ResourceRequest(cpu=0.5, memory_mib=128),
            time_limit_s=1,  # Booking estimate only: a session container ignores it.
        ),
        session=True,
    )


@contextmanager
def session_container(runtime, tmp_path, spec):
    started = threading.Event()
    cancel = threading.Event()
    outcome = {}

    def run():
        outcome["result"] = runtime.run(
            spec,
            tmp_path / "workspace",
            tmp_path / "outputs",
            lambda stream, data: None,
            started.set,
            cancel,
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert started.wait(120), "session container did not start"
        yield cancel, thread, outcome
    finally:
        cancel.set()
        thread.join(timeout=30)
        runtime.remove(spec.worker_id, spec.attempt_id)


def test_a_session_container_runs_commands_with_docker_exec(runtime, tmp_path):
    spec = session_spec()
    with session_container(runtime, tmp_path, spec) as (cancel, thread, outcome):
        arrivals = []

        def output(stream, data):
            arrivals.append((time.monotonic(), stream, data))

        code = (
            "import sys, time; print('first', flush=True); time.sleep(1.5); "
            "print('oops', file=sys.stderr, flush=True); sys.exit(3)"
        )
        begun = time.monotonic()
        result = runtime.exec(spec, ["python", "-c", code], WORKSPACE, output, cancel, 30)
        finished = time.monotonic()
        assert result == (3, False, False)
        assert arrivals[0][1:] == ("stdout", b"first\n")
        assert finished - arrivals[0][0] > 1  # Streamed while the process still ran.
        assert (b"oops\n", "stderr") in [(data, stream) for _, stream, data in arrivals]
        assert finished - begun > 1 and thread.is_alive()  # time_limit_s=1 is not enforced.

        write = ["python", "-c", "open('gen.txt', 'w').write('kept')"]
        assert runtime.exec(spec, write, WORKSPACE, output, cancel, 30) == (0, False, False)
        assert (tmp_path / "workspace" / "gen.txt").read_text() == "kept"
        arrivals.clear()
        read = ["python", "-c", "print(open('gen.txt').read())"]
        assert runtime.exec(spec, read, WORKSPACE, output, cancel, 30) == (0, False, False)
        assert b"".join(data for _, _, data in arrivals) == b"kept\n"
        # Arguments reach the program unchanged, and a missing program is exit 127.
        arrivals.clear()
        echo = ["python", "-c", "import sys; print(sys.argv[1:])", "a b", "$HOME;", ""]
        assert runtime.exec(spec, echo, WORKSPACE, output, cancel, 30) == (0, False, False)
        assert b"".join(data for _, _, data in arrivals) == b"['a b', '$HOME;', '']\n"
        missing = runtime.exec(spec, ["no-such-program"], WORKSPACE, output, cancel, 30)
        assert missing == (127, False, False)
        assert thread.is_alive()


def test_a_session_keeps_the_image_path_behind_the_workspace_venv(runtime, tmp_path):
    spec = session_spec()
    with closing(docker.from_env()) as client:
        environment = client.images.get(spec.job.image).attrs["Config"]["Env"]
    [image_path] = [item[5:] for item in environment if item.startswith("PATH=")]
    with session_container(runtime, tmp_path, spec) as (cancel, thread, outcome):
        output = []
        show = ["sh", "-c", 'echo "$PATH $VIRTUAL_ENV"']
        result = runtime.exec(
            spec, show, WORKSPACE, lambda _, data: output.append(data), cancel, 30
        )
        assert result == (0, False, False)
        assert b"".join(output).decode().split() == [
            f"/workspace/.venv/bin:{image_path}",
            "/workspace/.venv",
        ]


def test_a_stop_ends_only_the_commands_process_group(runtime, tmp_path):
    spec = session_spec()
    with session_container(runtime, tmp_path, spec) as (cancel, thread, outcome):
        output, aborted = [], []

        def stopped_once_started(argv):
            """Run argv and stop it as soon as it has printed something."""
            output.clear()
            stop = threading.Event()

            def on_output(stream, data):
                output.append(data)
                stop.set()

            begun = time.monotonic()
            result = runtime.exec(spec, argv, WORKSPACE, on_output, stop, 60, aborted.append)
            return result, time.monotonic() - begun

        # An ordinary command and its child end on TERM, well within the grace period.
        polite = ["sh", "-c", "sleep 311 & echo started; sleep 311"]
        result, elapsed = stopped_once_started(polite)
        assert result == (None, False, True) and elapsed < 1
        # A command and a child that ignore TERM are killed after the 1 s grace.
        stubborn = ["sh", "-c", "trap '' TERM; (trap '' TERM; sleep 311) & echo started; sleep 311"]
        result, elapsed = stopped_once_started(stubborn)
        assert result == (None, False, True) and 1 <= elapsed < 8
        assert aborted == []  # The container was never asked to end.
        # Nothing of either command is left, and the same container runs the next one.
        output.clear()
        listing = "import glob; print([open(p).read() for p in glob.glob('/proc/[0-9]*/cmdline')])"
        result = runtime.exec(
            spec,
            ["python", "-c", listing],
            WORKSPACE,
            lambda _, data: output.append(data),
            cancel,
            30,
        )
        assert result == (0, False, False)
        assert b"sleep" in b"".join(output) and b"311" not in b"".join(output)
        assert thread.is_alive()


def test_a_stop_that_cannot_end_the_command_asks_for_the_container_to_end(runtime, tmp_path):
    spec = session_spec()
    with session_container(runtime, tmp_path, spec) as (cancel, thread, outcome):
        aborted = []
        stop = threading.Event()

        def abort(timed_out):
            aborted.append(timed_out)
            cancel.set()  # What the Worker's _end_session does.

        # The command removes the record of its pid, so its process group cannot be signalled.
        argv = ["sh", "-c", "rm /tmp/.labflux-command; echo started; sleep 311"]
        result = runtime.exec(spec, argv, WORKSPACE, lambda *_: stop.set(), stop, 60, abort)
        assert result == (None, False, True)
        assert aborted == [False]
        thread.join(timeout=30)
        assert outcome["result"].termination_confirmed


def test_a_command_timeout_terminates_the_whole_container(runtime, tmp_path):
    spec = session_spec()
    with session_container(runtime, tmp_path, spec) as (cancel, thread, outcome):
        aborted = []

        def abort(timed_out):
            aborted.append(timed_out)
            cancel.set()  # What the Worker's _end_session does.

        result = runtime.exec(
            spec, ["sleep", "30"], WORKSPACE, lambda *_: None, threading.Event(), 1, abort
        )
        assert result == (None, True, False)
        assert aborted == [True]
        thread.join(timeout=30)
        assert outcome["result"].outcome == "CANCELLED"
        assert outcome["result"].termination_confirmed
        assert not [
            item
            for item in runtime.inventory(spec.worker_id)
            if item["attempt_id"] == spec.attempt_id
        ]


def test_uv_sync_runs_once_for_unchanged_dependencies(runtime, tmp_path):
    image = runtime.settings.default_image
    try:
        with closing(docker.from_env()) as client:
            client.images.get(image)
    except docker.errors.ImageNotFound:
        pytest.skip(f"{image} is not built locally")
    worker_id = f"fl-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        runtime.settings.model_copy(
            update={"data_dir": tmp_path / "worker", "worker_id": worker_id}
        ),
        executor=runtime,
    )
    sent = []
    worker.send = lambda message: sent.append(message) or True
    spec = session_spec(image)
    payload = {**spec.model_dump(), "worker_id": worker_id}
    attempt_id = spec.attempt_id
    project = tmp_path / "project"
    shutil.copytree(PROBE, project)
    archive = tmp_path / "snapshot.tar.gz"
    pack_directory(project, archive)
    data = archive.read_bytes()

    def command(argv):
        message = WorkerExec(
            id=uuid.uuid4().hex,
            attempt_id=attempt_id,
            argv=argv,
            snapshot_size=len(data),
            snapshot_sha256=hashlib.sha256(data).hexdigest(),
            timeout_s=60,
            sync_timeout_s=300,
            writeback_limit_bytes=1024**2,
            output_limit_bytes=1024**2,
        )
        worker._on_message(json.dumps(message.model_dump()))
        for text, final in iter_chunks(io.BytesIO(data)):
            chunk = {"type": "snapshot_chunk", "id": message.id, "data": text, "final": final}
            worker._on_message(json.dumps(chunk))
        found = wait_for(
            lambda: [m for m in sent if m.get("id") == message.id and m["type"] == "result"],
            timeout=300,
        )
        return [m for m in sent if m.get("id") == message.id], found[-1]

    try:
        worker.accept({"id": f"start-{attempt_id}", "kind": "START", "payload": payload})
        wait_for(lambda: worker._read(attempt_id)["started"], timeout=120)
        prefix = ["python", "-c", "import sys; print(sys.prefix)"]
        messages, result = command(prefix)
        assert result["outcome"] == "exited" and result["exit_code"] == 0, messages
        assert messages[0]["text"] == "running uv sync"
        stdout = b"".join(
            decode(m["data"]) for m in messages if m["type"] == "output" and m["stream"] == "stdout"
        )
        assert stdout == b"/workspace/.venv\n"
        assert (
            tmp_path / "worker" / "workers" / worker_id / attempt_id / "workspace" / ".venv"
        ).is_dir()
        messages, result = command(prefix)
        assert result["exit_code"] == 0
        assert not [m for m in messages if m["type"] == "notice"]  # No second sync.
    finally:
        for handle in worker.handles.values():
            handle["cancel"].set()
        worker.pool.shutdown(wait=True)
        worker.client.close()
        runtime.remove(worker_id, attempt_id)
