import base64
import json
from contextlib import nullcontext

from conftest import finish, register, submit, user_headers, worker_headers
from typer.testing import CliRunner

import labflux.cli as cli
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


def test_cli_invalid_spec_is_a_useful_error(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text('{"image": "python", "argv": []}')
    result = CliRunner().invoke(cli.app, ["submit", str(path)])
    assert result.exit_code == 1 and "Invalid job specification" in result.output
