"""`labflux login` (Q14): a node's Worker vouches for the local account the kernel reports, the
Scheduler issues a token for that account name, and the CLI saves it for later commands."""

import json
import os
import pwd
import socket
import stat
import threading
import uuid
from contextlib import contextmanager, suppress

import pytest
from conftest import user_headers, worker_headers
from sqlalchemy import select, update
from test_doorbell import live_lab
from typer.testing import CliRunner

from labflux.cli import app as cli_app
from labflux.config import Settings, load_settings, login_path
from labflux.database import SCHEMA_VERSION, Database, schema_version, user_tokens
from labflux.scheduler import Scheduler

ME = pwd.getpwuid(os.getuid()).pw_name


def issue(client, user="carol", node="vm-a"):
    response = client.post(
        f"/internal/workers/{node}/logins", headers=worker_headers(), json={"user": user}
    )
    assert response.status_code == 200, response.text
    return response.json()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_an_issued_token_identifies_its_account(client, settings):
    reply = issue(client)
    assert reply["user"] == "carol"
    assert client.get("/api/whoami", headers=bearer(reply["token"])).json() == {"user": "carol"}
    assert client.get("/api/whoami", headers=user_headers("alice")).json() == {"user": "alice"}
    # Another login adds a token; the first one keeps working (e.g. a copy on another machine).
    second = issue(client)
    assert second["token"] != reply["token"]
    assert client.get("/api/whoami", headers=bearer(reply["token"])).status_code == 200
    # Only a digest is stored.
    raw = (settings.data_dir / "scheduler.sqlite3").read_bytes()
    raw += (settings.data_dir / "scheduler.sqlite3-wal").read_bytes()
    assert reply["token"].encode() not in raw


def test_only_a_worker_may_vouch_and_names_are_checked(client):
    body = {"user": "carol"}
    assert (
        client.post("/internal/workers/vm-a/logins", headers=user_headers(), json=body).status_code
        == 401
    )
    for name in ("@models", "", "a/b", "x" * 65, "a b"):
        response = client.post(
            "/internal/workers/vm-a/logins", headers=worker_headers(), json={"user": name}
        )
        assert response.status_code == 422, name
    assert client.get("/api/whoami", headers=bearer("not-a-token")).status_code == 401


def test_a_configured_user_and_an_account_with_its_name_are_one_identity(client):
    job = client.post(
        "/api/jobs",
        headers=user_headers("alice"),
        json={"image": "python:3.12-slim", "argv": ["true"]},
    ).json()
    token = issue(client, user="alice")["token"]
    assert [j["id"] for j in client.get("/api/jobs", headers=bearer(token)).json()] == [job["id"]]


def test_tokens_survive_a_scheduler_restart(settings):
    first = Scheduler(settings)
    token = first.issue_token("vm-a", "carol")["token"]
    first.db.close()
    assert Scheduler(settings).token_owner(token) == "carol"


def test_each_account_keeps_only_its_newest_tokens(tmp_path):
    settings = Settings(data_dir=tmp_path, login_tokens_per_user=2)
    first = Scheduler(settings)
    tokens = [first.issue_token("vm-a", "carol")["token"] for _ in range(3)]
    other = first.issue_token("vm-a", "dave")["token"]
    assert [first.token_owner(t) for t in tokens] == [None, "carol", "carol"]
    first.db.close()
    again = Scheduler(settings)
    assert [again.token_owner(t) for t in tokens] == [None, "carol", "carol"]
    assert again.token_owner(other) == "dave"
    with again.db.transaction() as connection:
        assert len(connection.execute(select(user_tokens)).all()) == 3


def test_a_v7_database_gains_the_token_table(settings):
    first = Scheduler(settings)
    with first.db.transaction() as connection:
        user_tokens.drop(connection)
        connection.exec_driver_sql("ALTER TABLE attempts DROP COLUMN checkpoint_at")  # 9.
        connection.execute(update(schema_version).values(version=7))
    first.db.close()
    database = Database(settings.data_dir)
    assert database.version == SCHEMA_VERSION
    database.close()
    assert Scheduler(settings).issue_token("vm-a", "carol")["user"] == "carol"


def test_saved_login_comes_before_defaults_and_after_config_and_environment(tmp_path, monkeypatch):
    path = login_path()
    path.parent.mkdir(parents=True)
    path.write_text('scheduler_url = "http://192.0.2.10:8770"\nuser_token = "saved"\n')
    settings = load_settings()
    assert (settings.scheduler_url, settings.user_token) == ("http://192.0.2.10:8770", "saved")
    config = tmp_path / "cli.toml"
    config.write_text('scheduler_url = "http://127.0.0.1:9"\n')
    settings = load_settings(config)
    assert (settings.scheduler_url, settings.user_token) == ("http://127.0.0.1:9", "saved")
    monkeypatch.setenv("LABFLUX_USER_TOKEN", "from-env")
    assert load_settings(config).user_token == "from-env"
    path.write_text("not toml [")
    monkeypatch.delenv("LABFLUX_USER_TOKEN")
    assert load_settings().user_token == Settings().user_token


def test_login_through_the_local_worker_saves_a_working_token(tmp_path):
    name = f"labflux-test-{uuid.uuid4().hex}"
    with live_lab(tmp_path, worker_login=True, login_socket=name) as (app, worker, client, _):
        config = tmp_path / "cli.toml"
        config.write_text(f'login_socket = "{name}"\n')
        result = CliRunner().invoke(cli_app, ["--config", str(config), "login"])
        assert result.exit_code == 0, result.output
        assert f"Logged in as {ME}" in result.output
        path = login_path()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        saved = load_settings()
        assert saved.scheduler_url == worker.settings.scheduler_url
        assert app.state.scheduler.token_owner(saved.user_token) == ME
        result = CliRunner().invoke(cli_app, ["whoami"])
        assert result.exit_code == 0, result.output
        assert result.output.strip() == ME


def test_login_without_a_local_worker_explains_what_to_do(tmp_path):
    config = tmp_path / "cli.toml"
    config.write_text(f'login_socket = "labflux-test-{uuid.uuid4().hex}"\n')
    result = CliRunner().invoke(cli_app, ["--config", str(config), "login"])
    assert result.exit_code == 1
    assert "no LabFlux Worker accepts logins on this machine" in result.output
    assert not login_path().exists()


@pytest.mark.parametrize(
    "reply",
    [
        {"user": "someone-else", "token": "t", "scheduler_url": "http://127.0.0.1:9"},
        {"error": "the Scheduler refused the login (401)"},
    ],
)
def test_login_refuses_an_answer_that_is_not_for_this_account(tmp_path, reply):
    with fake_worker(reply) as name:
        config = tmp_path / "cli.toml"
        config.write_text(f'login_socket = "{name}"\n')
        result = CliRunner().invoke(cli_app, ["--config", str(config), "login"])
    assert result.exit_code == 1
    assert not login_path().exists()


@contextmanager
def fake_worker(reply):
    """A stand-in on a fresh login socket name that answers one connection with reply."""
    name = f"labflux-test-{uuid.uuid4().hex}"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind("\0" + name)
    server.listen(1)

    def answer():
        connection, _ = server.accept()
        with connection, suppress(OSError):
            connection.sendall(json.dumps(reply).encode())

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    try:
        yield name
    finally:
        thread.join(timeout=5)
        server.close()


def test_login_refuses_a_socket_held_by_another_account(tmp_path, monkeypatch):
    # Squatting: another account bound the name first and sends its own Scheduler URL.
    reply = {"user": ME, "token": "t", "scheduler_url": "http://127.0.0.1:9"}
    monkeypatch.setattr("labflux.cli.listener_uid", lambda connection: os.getuid() + 1)
    with fake_worker(reply) as name:
        config = tmp_path / "cli.toml"
        config.write_text(f'login_socket = "{name}"\nlogin_worker_user = "labflux-test-none"\n')
        result = CliRunner().invoke(cli_app, ["--config", str(config), "login"])
    assert result.exit_code == 1
    assert f"UID {os.getuid() + 1}" in result.output
    assert "refusing a possible fake Worker" in result.output
    assert not login_path().exists()


def test_login_trusts_a_socket_held_by_the_worker_account(tmp_path, monkeypatch):
    account = next(a for a in pwd.getpwall() if a.pw_uid not in (0, os.getuid()))
    monkeypatch.setattr("labflux.cli.listener_uid", lambda connection: account.pw_uid)
    name = f"labflux-test-{uuid.uuid4().hex}"
    with live_lab(tmp_path, worker_login=True, login_socket=name) as (app, *_):
        config = tmp_path / "cli.toml"
        config.write_text(f'login_socket = "{name}"\nlogin_worker_user = "{account.pw_name}"\n')
        result = CliRunner().invoke(cli_app, ["--config", str(config), "login"])
        assert result.exit_code == 0, result.output
        assert app.state.scheduler.token_owner(load_settings().user_token) == ME


def test_an_error_in_one_login_does_not_stop_the_next(tmp_path, monkeypatch):
    name = f"labflux-test-{uuid.uuid4().hex}"
    with live_lab(tmp_path, worker_login=True, login_socket=name) as (app, worker, *_):
        post, failed = worker.client.post, []

        def flaky(url, *args, **kwargs):
            if url.endswith("/logins") and not failed:
                failed.append(url)
                raise RuntimeError("unexpected")
            return post(url, *args, **kwargs)

        monkeypatch.setattr(worker.client, "post", flaky)
        config = tmp_path / "cli.toml"
        config.write_text(f'login_socket = "{name}"\n')
        result = CliRunner().invoke(cli_app, ["--config", str(config), "login"])
        assert result.exit_code == 1
        assert "the Worker failed (RuntimeError)" in result.output
        result = CliRunner().invoke(cli_app, ["--config", str(config), "login"])
        assert result.exit_code == 0, result.output
        assert app.state.scheduler.token_owner(load_settings().user_token) == ME


def test_a_worker_without_worker_login_leaves_the_socket_free(tmp_path):
    name = f"labflux-test-{uuid.uuid4().hex}"
    with live_lab(tmp_path, login_socket=name):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with probe, pytest.raises(ConnectionRefusedError):
            probe.connect("\0" + name)
