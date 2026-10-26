import json
import shutil
import threading
import uuid
from contextlib import closing
from pathlib import Path

import docker
import pytest

from labflux.config import Settings
from labflux.models import ExecutionSpec, JobSpec, ResourceRequest
from labflux.runtime import DockerExecutor

pytestmark = pytest.mark.docker

PROBE = Path(__file__).parent / "fixtures" / "uv-probe"


@pytest.fixture
def runtime(tmp_path):
    executor = DockerExecutor(Settings(data_dir=tmp_path, uv_cache_dir=tmp_path / "uv-cache"))
    try:
        executor.check()
        with closing(docker.from_env()) as client:
            client.images.get(executor.settings.default_image)
    except docker.errors.ImageNotFound:
        pytest.skip("base image not built; see images/python-uv/Dockerfile")
    except (docker.errors.DockerException, OSError, RuntimeError) as exc:
        pytest.skip(f"Docker unavailable: {exc}")
    return executor


def test_uv_project_runs_in_the_default_image_with_the_worker_cache(runtime, tmp_path):
    cache = runtime.settings.uv_cache_dir
    workspace = tmp_path / "work"
    shutil.copytree(PROBE, workspace)
    output = []
    spec = ExecutionSpec(
        job_id=uuid.uuid4().hex,
        attempt_id=uuid.uuid4().hex,
        worker_id="uv-test",
        job=JobSpec(
            image=runtime.settings.default_image,
            argv=["uv", "run", "--locked", "--offline", "python", "main.py"],
            # The Worker's values must win over a job's attempt to redirect the cache.
            env={"UV_CACHE_DIR": "/elsewhere", "UV_LINK_MODE": "hardlink"},
            resources=ResourceRequest(cpu=1, memory_mib=256),
            time_limit_s=60,
        ),
    )
    assert not cache.exists()
    result = runtime.run(
        spec,
        workspace,
        tmp_path / "out",
        lambda stream, data: output.append((stream, data)),
        lambda: None,
        threading.Event(),
    )
    stdout = b"".join(data for stream, data in output if stream == "stdout").decode()
    assert result.outcome == "SUCCEEDED", (result, output)
    probe = json.loads(stdout.strip().splitlines()[-1])
    assert probe == {
        "prefix": "/workspace/.venv",
        "home": "/tmp",
        "uv_cache_dir": "/uv-cache",
        "uv_link_mode": "copy",
    }
    assert (workspace / ".venv" / "pyvenv.cfg").is_file()
    assert any(cache.iterdir())  # uv wrote its cache through the bind mount.
