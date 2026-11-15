import base64
import json
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import httpx
from conftest import finish, register, submit, user_headers, worker_headers
from typer.testing import CliRunner

import labflux.cli as cli
from labflux import fastlane
from labflux.archives import pack_directory


def configure(monkeypatch, client):
    client.headers.update(user_headers())
    monkeypatch.setattr(cli, "api_client", lambda _: nullcontext(client))
    return CliRunner()


def test_cli_submit_upload_status_nodes_and_queued_cancel(client, tmp_path, monkeypatch):
    runner = configure(monkeypatch, client)
    spec = tmp_path / "job.json"
    spec.write_text(json.dumps({"image": "python:3.12-slim", "argv": ["python", "main.py"]}))
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('hello')")
    result = runner.invoke(cli.app, ["submit", str(spec), "--source", str(source)])
    assert result.exit_code == 0, result.output
    job = json.loads(result.stdout)
    assert job["state"] == "QUEUED" and job["spec"]["source_id"]
    assert runner.invoke(cli.app, ["status", job["id"]]).exit_code == 0
    assert runner.invoke(cli.app, ["nodes"]).exit_code == 0
    cancelled = runner.invoke(cli.app, ["cancel", job["id"]])
    assert cancelled.exit_code == 0
    assert json.loads(cancelled.stdout)["state"] == "CANCELLED"


def test_cli_logs_handles_utf8_boundaries_and_pagination(client, monkeypatch):
    instance = register(client)
    job = submit(client)
    attempt = job["current_attempt_id"]
    chunks = ["中".encode()[:1], "中".encode()[1:]] + [b"x"] * 68
    for seq, chunk in enumerate(chunks):
        response = client.post(
            f"/internal/workers/vm-a/attempts/{attempt}/logs",
            headers=worker_headers(),
            json={
                "instance_id": instance,
                "seq": seq,
                "stream": "stdout",
                "data_base64": base64.b64encode(chunk).decode(),
            },
        )
        assert response.status_code == 200
    runner = configure(monkeypatch, client)
    result = runner.invoke(cli.app, ["logs", job["id"]])
    assert result.exit_code == 0, result.output
    assert result.stdout == "中" + "x" * 68


def test_cli_fetch_verifies_and_extracts_attempt_output(client, tmp_path, monkeypatch):
    instance = register(client)
    job = submit(client)
    output = tmp_path / "output"
    output.mkdir()
    (output / "result.txt").write_text("completed")
    archive = tmp_path / "output.tar.gz"
    pack_directory(output, archive, source=False)
    response = client.put(
        f"/internal/workers/vm-a/attempts/{job['current_attempt_id']}/output",
        params={"instance_id": instance},
        headers=worker_headers(),
        content=archive.read_bytes(),
    )
    assert response.status_code == 200
    assert finish(client, job, instance).status_code == 200
    runner = configure(monkeypatch, client)
    destination = tmp_path / "downloaded"
    result = runner.invoke(cli.app, ["fetch", job["id"], "--directory", str(destination)])
    assert result.exit_code == 0, result.output
    assert (destination / "result.txt").read_text() == "completed"


def test_cli_wait_keeps_waiting_through_unknown(client, service, clock, tmp_path, monkeypatch):
    instance = register(client)
    runner = configure(monkeypatch, client)
    spec = tmp_path / "job.json"
    spec.write_text(json.dumps({"image": "python:3.12-slim", "argv": ["true"]}))
    steps = []

    def progress(_):
        # First poll: the heartbeat lapses (UNKNOWN). Second: the worker reports completion.
        job = service.get_job(service.list_jobs("alice")[-1]["id"], "alice")
        if not steps:
            clock[0] += 11
            service.tick()
        else:
            finish(client, job, instance)
        steps.append(job["state"])

    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=time.monotonic, sleep=progress))
    result = runner.invoke(cli.app, ["submit", str(spec), "--wait"])
    assert result.exit_code == 0, result.output
    assert "UNKNOWN" in steps and json.loads(result.stdout)["state"] == "SUCCEEDED"


def test_cli_invalid_spec_is_a_useful_error(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text('{"image": "python", "argv": []}')
    result = CliRunner().invoke(cli.app, ["submit", str(path)])
    assert result.exit_code == 1 and "Invalid job specification" in result.output


# Fast lane (M9) ------------------------------------------------------------------------------


def fast_lane_project(tmp_path, monkeypatch, text="[session]\ncpu = 2\n"):
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    (root / ".labflux.toml").write_text(text)
    monkeypatch.chdir(root / "sub")
    calls = []

    def run_command(settings, root, project, argv, cwd, **flags):
        calls.append(("remote", root, project.session.cpu, argv, cwd, flags))
        return 3

    def exec_local(argv):
        calls.append(("local", argv))
        return 127

    monkeypatch.setattr(fastlane, "run_command", run_command)
    monkeypatch.setattr(fastlane, "exec_local", exec_local)
    return root, calls


def test_cli_run_passes_flags_argv_and_exit_status(tmp_path, monkeypatch):
    root, calls = fast_lane_project(tmp_path, monkeypatch)
    argv = ["run", "--gpu", "1", "--memory-mib", "512", "--", "python", "-c", "x", "--gpu"]
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 3, result.output
    assert calls == [
        (
            "remote",
            root,
            2,
            ["python", "-c", "x", "--gpu"],
            root / "sub",
            {"cpu": None, "memory_mib": 512, "gpu": 1},
        )
    ]
    monkeypatch.chdir(tmp_path)
    missing = CliRunner().invoke(cli.app, ["run", "--", "python"])
    assert missing.exit_code == 125 and "no .labflux.toml found above" in missing.output
    (root / ".labflux.toml").write_text("[session]\nunknown = 1\n")
    monkeypatch.chdir(root)
    invalid = CliRunner().invoke(cli.app, ["run", "--", "python"])
    assert invalid.exit_code == 125 and "unknown" in invalid.output


def test_cli_standin_routes_and_local_bypasses(tmp_path, monkeypatch):
    root, calls = fast_lane_project(tmp_path, monkeypatch)
    runner = CliRunner()
    assert runner.invoke(cli.app, ["_standin", "python", "--", "-c", "x", "-q"]).exit_code == 3
    assert runner.invoke(cli.app, ["_standin", "uv", "--", "add", "torch"]).exit_code == 127
    assert runner.invoke(cli.app, ["local", "--", "python", "-V"]).exit_code == 127
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(cli.app, ["_standin", "pytest", "--", "-x"]).exit_code == 127
    assert calls == [
        ("remote", root, 2, ["python", "-c", "x", "-q"], root / "sub", {}),
        ("local", ["uv", "add", "torch"]),
        ("local", ["python", "-V"]),
        ("local", ["pytest", "-x"]),
    ]
    assert "_standin" not in runner.invoke(cli.app, ["--help"]).output


def test_cli_shell_puts_standins_first_and_cleans_up(tmp_path, monkeypatch):
    fast_lane_project(tmp_path, monkeypatch, '[route]\nadd = ["mytool"]\n')
    report = tmp_path / "report.txt"
    program = tmp_path / "fake-shell"
    program.write_text(
        f'#!/bin/sh\nfirst="${{PATH%%:*}}"\necho "$first" > {report}\n'
        f'ls -a "$first" >> {report}\necho "config=$LABFLUX_CONFIG" >> {report}\nexit 7\n'
    )
    program.chmod(0o755)
    config = tmp_path / "user.toml"
    config.write_text('user_token = "alice-token"\nusers = {alice = "alice-token"}\n')
    result = CliRunner().invoke(
        cli.app, ["--config", str(config), "shell", "--shell", str(program)]
    )
    assert result.exit_code == 7, result.output
    lines = report.read_text().splitlines()
    assert "labflux-standins-" in lines[0] and not Path(lines[0]).exists()
    assert {".labflux-standins", "python", "pytest", "uv", "mytool"} <= set(lines[1:-1])
    assert lines[-1] == f"config={config.resolve()}"


def test_cli_session_status_and_close(monkeypatch):
    session = {"id": "s" * 32, "state": "RUNNING", "lease_expires_at": time.time() + 100}
    paths = []

    def handler(request):
        paths.append((request.method, request.url.path))
        if not session:
            return httpx.Response(404, json={"detail": "no session"})
        return httpx.Response(200, json=session)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    monkeypatch.setattr(cli, "api_client", lambda _: nullcontext(client))
    runner = CliRunner()
    status = runner.invoke(cli.app, ["session", "status"])
    assert status.exit_code == 0, status.output
    assert 95 <= json.loads(status.stdout)["lease_remaining_s"] <= 100
    closed = runner.invoke(cli.app, ["session", "close"])
    assert closed.exit_code == 0 and json.loads(closed.stdout)["state"] == "RUNNING"
    session.clear()
    missing = runner.invoke(cli.app, ["session", "close"])
    assert missing.exit_code == 1 and "no fast-lane session" in missing.output
    assert paths == [
        ("GET", "/api/sessions/current"),
        ("POST", "/api/sessions/current/close"),
        ("POST", "/api/sessions/current/close"),
    ]
