import json
import logging
import time
from pathlib import Path

from conftest import register, user_headers
from test_cli import configure
from test_worker import ControlledExecutor, prepare

import labflux.cli as cli
import labflux.worker as worker_module
from labflux.config import Settings

EXAMPLE_JOB = Path(__file__).parents[1] / "examples" / "jobs" / "uv-gpu.json"


def test_omitted_image_is_stored_as_the_default_image(client, service):
    instance = register(client, cpu=1)
    response = client.post("/api/jobs", headers=user_headers(), json={"argv": ["true"]})
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["spec"]["image"] == service.settings.default_image
    (command,) = service.poll_commands("vm-a", instance)
    assert command["payload"]["job"]["image"] == "localhost/labflux/python-uv:3.12-uv0.11.8"
    explicit = client.post(
        "/api/jobs", headers=user_headers(), json={"image": "ubuntu:24.04", "argv": ["true"]}
    ).json()
    assert explicit["spec"]["image"] == "ubuntu:24.04"


def test_cli_submits_the_uv_gpu_example_without_an_image(client, monkeypatch):
    runner = configure(monkeypatch, client)
    assert "image" not in json.loads(EXAMPLE_JOB.read_text())
    result = runner.invoke(cli.app, ["submit", str(EXAMPLE_JOB)])
    assert result.exit_code == 0, result.output
    job = json.loads(result.stdout)
    assert job["spec"]["image"] == "localhost/labflux/python-uv:3.12-uv0.11.8"
    assert job["spec"]["resources"]["gpu"] == 1 and job["state"] == "QUEUED"


def test_uv_cache_dir_is_expanded_and_optional(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert Settings().uv_cache_dir is None
    assert Settings(uv_cache_dir="~/labflux/uv-cache").uv_cache_dir == tmp_path / "labflux/uv-cache"


def run_to_result(client, tmp_path, executor, *, fill_workspace=True):
    worker, job, command = prepare(client, tmp_path, executor)
    attempt = worker.root / command["attempt_id"]
    worker.accept(command)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (attempt / "workspace").exists():
        time.sleep(0.01)
    if fill_workspace:
        (attempt / "workspace" / ".venv").mkdir(parents=True, exist_ok=True)
        (attempt / "workspace" / ".venv" / "torch.so").write_bytes(b"large")
    executor.release.set()
    worker.handles[command["attempt_id"]]["future"].result(timeout=5)
    worker.flush()
    state = client.get(f"/api/jobs/{job['id']}", headers=user_headers()).json()["state"]
    return worker, attempt, state


def test_workspace_is_removed_after_confirmed_termination_and_output(client, tmp_path):
    worker, attempt, state = run_to_result(client, tmp_path, ControlledExecutor())
    try:
        assert state == "SUCCEEDED"
        assert not (attempt / "workspace").exists()
        for kept in ("state.json", "logs.jsonl", "output.tar.gz"):
            assert (attempt / kept).exists(), kept
    finally:
        worker.pool.shutdown(wait=True)


def test_workspace_is_kept_while_termination_is_unconfirmed(client, tmp_path):
    executor = ControlledExecutor(fail=True)
    worker, attempt, state = run_to_result(client, tmp_path, executor, fill_workspace=False)
    try:
        assert state == "UNKNOWN"
        assert (attempt / "workspace").is_dir()
    finally:
        worker.pool.shutdown(wait=True)


def test_workspace_cleanup_failure_is_logged_not_fatal(client, tmp_path, monkeypatch, caplog):
    def refuse(path):
        raise PermissionError(f"cannot remove {path}")

    monkeypatch.setattr(worker_module.shutil, "rmtree", refuse)
    with caplog.at_level(logging.WARNING, logger="labflux.worker"):
        worker, attempt, state = run_to_result(client, tmp_path, ControlledExecutor())
    try:
        assert state == "SUCCEEDED"
        assert (attempt / "workspace").exists()
        assert "workspace cleanup failed" in caplog.text
    finally:
        worker.pool.shutdown(wait=True)
