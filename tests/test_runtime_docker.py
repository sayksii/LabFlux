import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import docker
import pytest

from labflux.config import Settings
from labflux.models import ExecutionSpec, JobSpec, ResourceRequest
from labflux.runtime import DockerExecutor, container_name

pytestmark = pytest.mark.docker


@pytest.fixture
def runtime(tmp_path):
    executor = DockerExecutor(Settings(data_dir=tmp_path, stop_grace_s=1))
    try:
        executor.check()
    except (docker.errors.DockerException, OSError, RuntimeError) as exc:
        pytest.skip(f"Docker integration prerequisites unavailable: {exc}")
    return executor


def execute(runtime, tmp_path, code, *, seconds=10, memory=64, cancel=None, on_log=None):
    output = []
    spec = ExecutionSpec(
        job_id=uuid.uuid4().hex,
        attempt_id=uuid.uuid4().hex,
        worker_id="runtime-test",
        job=JobSpec(
            image=runtime.settings.docker_image,
            argv=["python", "-u", "-c", code],
            resources=ResourceRequest(cpu=0.5, memory_mib=memory),
            time_limit_s=seconds,
        ),
    )

    def collect(stream, data):
        output.append((stream, data))
        if on_log:
            on_log(stream, data)

    def inspect_started_container():
        with closing(docker.from_env()) as client:
            labels = client.containers.get(container_name(spec)).labels
            assert labels["labflux.job_id"] == spec.job_id
            assert labels["labflux.attempt_id"] == spec.attempt_id
            assert labels["labflux.worker_id"] == spec.worker_id

    result = runtime.run(
        spec,
        tmp_path / "work",
        tmp_path / "out",
        collect,
        inspect_started_container,
        cancel or threading.Event(),
    )
    with closing(docker.from_env()) as client:
        with pytest.raises(docker.errors.NotFound):
            client.containers.get(container_name(spec))
        assert not client.containers.list(
            all=True, filters={"label": f"labflux.attempt_id={spec.attempt_id}"}
        )
    return result, b"".join(data for _, data in output), output


def test_exit_code_and_both_output_streams(runtime, tmp_path):
    result, output, chunks = execute(
        runtime,
        tmp_path,
        "import sys; print('hello'); print('problem', file=sys.stderr); sys.exit(7)",
    )
    assert result.termination_confirmed and result.exit_code == 7 and result.outcome == "FAILED"
    assert b"hello" in output and b"problem" in output
    assert {stream for stream, _ in chunks} == {"stdout", "stderr"}


def test_kernel_limits(runtime, tmp_path):
    result, output, _ = execute(
        runtime,
        tmp_path,
        "from pathlib import Path; p=Path('/sys/fs/cgroup'); "
        "print((p/'cpu.max').read_text()); print((p/'memory.max').read_text()); "
        "print((p/'memory.swap.max').read_text())",
    )
    assert result.outcome == "SUCCEEDED"
    assert b"50000 100000" in output
    assert str(64 * 1024**2).encode() in output
    assert output.strip().endswith(b"0")


def test_memory_excess_is_killed(runtime, tmp_path):
    result, _, _ = execute(runtime, tmp_path, "a = bytearray(512 * 1024 * 1024)")
    assert result.outcome == "FAILED" and result.oom_killed and result.termination_confirmed


def test_timeout_cleans_up_child_processes(runtime, tmp_path):
    result, _, _ = execute(
        runtime,
        tmp_path,
        "import subprocess,time; subprocess.Popen(['sleep','60']); time.sleep(60)",
        seconds=0.5,
    )
    assert result.outcome == "FAILED" and result.reason == "time limit exceeded"
    assert result.termination_confirmed


def test_logs_arrive_before_completion(runtime, tmp_path):
    first = threading.Event()
    with ThreadPoolExecutor() as pool:
        future = pool.submit(
            execute,
            runtime,
            tmp_path,
            "import time; print('first', flush=True); time.sleep(3); print('last')",
            on_log=lambda stream, data: first.set() if b"first" in data else None,
        )
        assert first.wait(timeout=120)
        assert not future.done()
        result, output, _ = future.result(timeout=20)
    assert result.outcome == "SUCCEEDED" and b"last" in output


def test_cancel_stops_container(runtime, tmp_path):
    cancel = threading.Event()
    result, _, _ = execute(
        runtime,
        tmp_path,
        "import time; print('ready',flush=True); time.sleep(60)",
        cancel=cancel,
        on_log=lambda stream, data: cancel.set(),
    )
    assert result.outcome == "CANCELLED" and result.termination_confirmed


def test_stop_before_start_confirms_no_container_without_pulling_image(runtime):
    spec = ExecutionSpec(
        job_id=uuid.uuid4().hex,
        attempt_id=uuid.uuid4().hex,
        worker_id="runtime-test",
        job=JobSpec(image="labflux-do-not-pull/never:absent", argv=["never"]),
    )
    result = runtime.stop_unstarted(spec)
    assert result.outcome == "CANCELLED" and result.termination_confirmed
    with closing(docker.from_env()) as client:
        with pytest.raises(docker.errors.NotFound):
            client.containers.get(container_name(spec))


def test_immediate_output_is_captured_exactly_once(runtime, tmp_path):
    # The shell may exit before any attach issued after start. The stream is attached before
    # start, so output is neither lost nor replayed, and the run does not wait out the 5 s
    # log-reader timeout of attaching to an already-exited container.
    began = time.monotonic()
    for attempt in range(10):
        output = []
        spec = ExecutionSpec(
            job_id=uuid.uuid4().hex,
            attempt_id=uuid.uuid4().hex,
            worker_id="runtime-test",
            job=JobSpec(
                image=runtime.settings.docker_image,
                argv=["sh", "-c", "echo early; echo late >&2"],
                resources=ResourceRequest(cpu=0.5, memory_mib=64),
                time_limit_s=10,
            ),
        )
        result = runtime.run(
            spec,
            tmp_path / str(attempt) / "work",
            tmp_path / str(attempt) / "out",
            lambda stream, data: output.append((stream, data)),
            lambda: None,
            threading.Event(),
        )
        assert result.outcome == "SUCCEEDED"
        assert b"".join(d for s, d in output if s == "stdout") == b"early\n"
        assert b"".join(d for s, d in output if s == "stderr") == b"late\n"
    assert time.monotonic() - began < 30
