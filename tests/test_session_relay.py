"""M9 relay (T6): CLI WebSocket -> Scheduler -> Worker doorbell socket and back, in memory."""

import hashlib
import io
import time
from contextlib import ExitStack, contextmanager

import pytest
from conftest import register, user_headers, worker_headers
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_doorbell import socket_url, wait_for
from test_sessions import current, job, open_session, start

from labflux import relay
from labflux.api import create_app
from labflux.config import Settings
from labflux.session_protocol import (
    CommandResult,
    ExecRequest,
    Notice,
    Output,
    WritebackChunk,
    decode,
    encode,
    iter_chunks,
)

SNAPSHOT = b"0123456789" * 30000  # 300 kB: two chunks.


@pytest.fixture
def make_client(tmp_path, clock):
    with ExitStack() as stack:

        def build(**settings):
            values = Settings(
                data_dir=tmp_path / "scheduler",
                users={"alice": "alice-token", "bob": "bob-token"},
                **settings,
            )
            app = create_app(values, run_loop=False, clock=lambda: clock[0])
            return stack.enter_context(TestClient(app))

        yield build


def exec_request(data=SNAPSHOT, size=None, **fields):
    return ExecRequest(
        argv=fields.pop("argv", ["pytest", "-q"]),
        snapshot_size=len(data) if size is None else size,
        snapshot_sha256=hashlib.sha256(data).hexdigest(),
        **fields,
    ).model_dump()


def send_snapshot(ws, data=SNAPSHOT):
    for text, final in iter_chunks(io.BytesIO(data)):
        ws.send_json({"type": "snapshot_chunk", "data": text, "final": final})


def next_message(worker):
    """The next relayed message, skipping doorbell rings."""
    while (message := worker.receive_json()) == {"type": "commands"}:
        pass
    return message


def session_url(session):
    return f"/api/sessions/{session['id']}/ws"


@contextmanager
def ready_session(client, *, connect_worker=True):
    """A RUNNING session on vm-a, with the fake Worker's doorbell socket when asked."""
    instance = register(client, cpu=2)
    with ExitStack() as stack:
        worker = None
        if connect_worker:
            worker = stack.enter_context(
                client.websocket_connect(socket_url(instance), headers=worker_headers())
            )
            wait_for(lambda: "vm-a" in client.app.state.hub.workers)
        session = open_session(client)
        start(client, session, instance)
        yield worker, session


def begin(client, worker, session, **request):
    """Open a CLI connection, send one command with its snapshot, and return the client
    socket's context, the client socket and the WorkerExec the fake Worker received."""
    context = client.websocket_connect(session_url(session), headers=user_headers())
    ws = context.__enter__()
    ws.send_json(exec_request(**request))
    send_snapshot(ws)
    command = next_message(worker)
    return context, ws, command


def receive_error(ws):
    message = ws.receive_json()
    assert message["type"] == "error", message
    with pytest.raises(WebSocketDisconnect) as closed:
        ws.receive_json()
    assert closed.value.code == 1000
    return message["code"]


def test_a_command_is_relayed_both_ways_in_order(make_client, clock):
    client = make_client()
    service = client.app.state.scheduler
    with ready_session(client) as (worker, session):
        clock[0] += 100
        with client.websocket_connect(session_url(session), headers=user_headers()) as ws:
            ws.send_json(exec_request(cwd="src/./pkg"))
            send_snapshot(ws)
            command = next_message(worker)
            assert command == {
                "type": "exec",
                "id": command["id"],
                "attempt_id": job(client, session["id"])["current_attempt_id"],
                "argv": ["pytest", "-q"],
                "cwd": "src/pkg",
                "snapshot_size": len(SNAPSHOT),
                "snapshot_sha256": hashlib.sha256(SNAPSHOT).hexdigest(),
                "timeout_s": 60.0,
                "sync_timeout_s": 900.0,
                "writeback_limit_bytes": 100 * 1024**2,
                "output_limit_bytes": 16 * 1024**2,
            }
            assert len(command["id"]) == 32
            assert service.session_busy == {session["id"]}
            assert current(client).json()["lease_expires_at"] == 1100 + 600
            received = b""
            while True:
                chunk = next_message(worker)
                assert chunk["type"] == "snapshot_chunk" and chunk["id"] == command["id"]
                received += decode(chunk["data"])
                if chunk["final"]:
                    break
            assert received == SNAPSHOT
            clock[0] += 50
            replies = [
                Output(id=command["id"], stream="stdout", data=encode(b"out\n")),
                Output(id=command["id"], stream="stderr", data=encode(b"err\n")),
                Notice(id=command["id"], text="running uv sync"),
                WritebackChunk(id=command["id"], data=encode(b"tar"), final=True),
                CommandResult(
                    id=command["id"],
                    outcome="exited",
                    exit_code=3,
                    writeback_size=3,
                    writeback_sha256=hashlib.sha256(b"tar").hexdigest(),
                    writeback_files=1,
                ),
            ]
            for reply in replies:
                worker.send_json(reply.model_dump())
            for reply in replies:
                assert ws.receive_json() == reply.model_dump()
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 1000
        wait_for(lambda: not service.session_busy)
        assert current(client).json()["lease_expires_at"] == 1150 + 600
        assert current(client).json()["state"] == "RUNNING"


def test_session_errors_before_a_command_reaches_the_worker(make_client):
    client = make_client()
    service = client.app.state.scheduler
    instance = register(client, cpu=2)
    queued = open_session(client)  # Dispatched but not started.
    with client.websocket_connect(session_url(queued), headers=user_headers()) as ws:
        ws.send_json(exec_request())
        assert receive_error(ws) == "not_ready"
    start(client, queued, instance)  # Running, but its Worker has no relay connection.
    with client.websocket_connect(session_url(queued), headers=user_headers()) as ws:
        ws.send_json(exec_request())
        assert receive_error(ws) == "worker_unavailable"
    with client.websocket_connect(session_url(queued), headers=user_headers("bob")) as ws:
        ws.send_json(exec_request())
        assert receive_error(ws) == "not_found"
    with client.websocket_connect(session_url(queued), headers=user_headers()) as ws:
        ws.send_json(exec_request(size=service.settings.max_source_bytes + 1))
        assert receive_error(ws) == "too_large"
    for first in ({"type": "cancel"}, {"type": "bogus"}, {"type": "exec", "argv": []}):
        with client.websocket_connect(session_url(queued), headers=user_headers()) as ws:
            ws.send_json(first)
            assert receive_error(ws) == "invalid"
    assert service.session_busy == set()
    assert current(client).json()["state"] == "RUNNING"  # None of these closed it.


def test_the_token_is_accepted_only_in_the_authorization_header(make_client):
    client = make_client()
    with ready_session(client, connect_worker=False) as (_, session):
        for url, headers in (
            (session_url(session), {}),
            (session_url(session), {"Authorization": "Bearer wrong"}),
            (session_url(session), worker_headers()),
            (f"{session_url(session)}?token=alice-token", {}),
        ):
            with pytest.raises(WebSocketDisconnect) as rejected:
                with client.websocket_connect(url, headers=headers) as ws:
                    ws.receive_json()
            assert rejected.value.code == 1008


def test_one_command_at_a_time_until_the_result(make_client):
    client = make_client()
    service = client.app.state.scheduler
    with ready_session(client) as (worker, session):
        context, first, command = begin(client, worker, session)
        with client.websocket_connect(session_url(session), headers=user_headers()) as second:
            second.send_json(exec_request())
            assert receive_error(second) == "busy"
        while not next_message(worker)["final"]:
            pass
        worker.send_json(
            CommandResult(id=command["id"], outcome="exited", exit_code=0).model_dump()
        )
        assert first.receive_json()["type"] == "result"
        context.__exit__(None, None, None)
        wait_for(lambda: not service.session_busy)
        context, third, command = begin(client, worker, session)
        assert command["type"] == "exec"
        context.__exit__(None, None, None)


def test_a_command_rejected_as_busy_does_not_end_the_running_one(make_client):
    client = make_client(fast_lane_mode="oneshot")  # Ending a command would close the session.
    with ready_session(client) as (worker, session):
        context, first, command = begin(client, worker, session)
        with client.websocket_connect(session_url(session), headers=user_headers()) as second:
            second.send_json(exec_request())
            assert receive_error(second) == "busy"
        assert current(client).json()["state"] == "RUNNING"
        assert client.app.state.scheduler.session_busy == {session["id"]}
        context.__exit__(None, None, None)


def test_another_user_cannot_mark_a_session_busy(make_client, monkeypatch):
    client = make_client()
    service = client.app.state.scheduler
    begin_command = service.begin_command
    busy_at_begin = []

    def spy(owner, session_id):
        busy_at_begin.append((owner, session_id in service.session_busy))
        return begin_command(owner, session_id)

    monkeypatch.setattr(service, "begin_command", spy)
    with ready_session(client) as (worker, session):
        with client.websocket_connect(session_url(session), headers=user_headers("bob")) as ws:
            ws.send_json(exec_request())
            assert receive_error(ws) == "not_found"
        assert busy_at_begin == [("bob", False)]
        # Still not_found, not busy, while the owner's command runs; and the owner's command
        # is not disturbed.
        context, first, command = begin(client, worker, session)
        with client.websocket_connect(session_url(session), headers=user_headers("bob")) as ws:
            ws.send_json(exec_request())
            assert receive_error(ws) == "not_found"
        assert service.session_busy == {session["id"]}
        while not next_message(worker)["final"]:
            pass
        worker.send_json(
            CommandResult(id=command["id"], outcome="exited", exit_code=0).model_dump()
        )
        assert first.receive_json()["type"] == "result"
        context.__exit__(None, None, None)
        wait_for(lambda: not service.session_busy)


def test_snapshot_size_is_enforced_while_streaming(make_client):
    client = make_client()
    with ready_session(client) as (worker, session):
        for declared, sent in ((10, b"x" * 20), (20, b"x" * 10)):
            with client.websocket_connect(session_url(session), headers=user_headers()) as ws:
                ws.send_json(exec_request(data=sent, size=declared))
                assert next_message(worker)["type"] == "exec"
                send_snapshot(ws, sent)
                assert receive_error(ws) == "too_large"
                assert next_message(worker)["type"] == "cancel"


def test_writeback_above_its_cap_is_cut_off(make_client, monkeypatch):
    monkeypatch.setattr(relay, "WRITEBACK_OVERHEAD_BYTES", 0)
    client = make_client(session_writeback_bytes=4)
    with ready_session(client) as (worker, session):
        context, ws, command = begin(client, worker, session)
        while not next_message(worker)["final"]:
            pass
        for data in (b"abc", b"def"):
            worker.send_json(
                WritebackChunk(id=command["id"], data=encode(data), final=False).model_dump()
            )
        assert ws.receive_json()["data"] == encode(b"abc")
        assert receive_error(ws) == "too_large"
        assert next_message(worker) == {"type": "cancel", "id": command["id"]}
        context.__exit__(None, None, None)


def test_client_cancel_and_disconnect_forward_a_cancel(make_client):
    client = make_client()
    service = client.app.state.scheduler
    with ready_session(client) as (worker, session):
        context, ws, command = begin(client, worker, session)
        while not next_message(worker)["final"]:
            pass
        ws.send_json({"type": "cancel"})
        assert next_message(worker) == {"type": "cancel", "id": command["id"]}
        # Cancelled before it started, so the session stays.
        result = CommandResult(id=command["id"], outcome="cancelled", session_ended=False)
        worker.send_json(result.model_dump())
        assert ws.receive_json() == result.model_dump()
        context.__exit__(None, None, None)
        wait_for(lambda: not service.session_busy)

        context, ws, command = begin(client, worker, session)
        while not next_message(worker)["final"]:
            pass
        # The client goes away mid-command. (Leaving the TestClient context would also cancel
        # the handler, which a real server does not do.)
        ws.close()
        assert next_message(worker) == {"type": "cancel", "id": command["id"]}
        time.sleep(0.2)
        assert service.session_busy == {session["id"]}  # Busy until the Worker's result.
        worker.send_json(
            CommandResult(id=command["id"], outcome="cancelled", session_ended=True).model_dump()
        )
        wait_for(lambda: not service.session_busy)
        context.__exit__(None, None, None)


def test_malformed_worker_messages_are_dropped(make_client):
    client = make_client()
    with ready_session(client) as (worker, session):
        context, ws, command = begin(client, worker, session)
        while not next_message(worker)["final"]:
            pass
        worker.send_json({"type": "output", "id": command["id"], "stream": "tty", "data": ""})
        worker.send_json({"type": "writeback_chunk", "id": command["id"], "data": "!", "final": 1})
        worker.send_json({"type": "error", "id": command["id"], "code": "busy", "message": "x"})
        result = CommandResult(id=command["id"], outcome="exited", exit_code=0).model_dump()
        worker.send_json(result)
        assert ws.receive_json() == result
        context.__exit__(None, None, None)


def test_losing_the_worker_mid_command_closes_the_session(make_client):
    client = make_client()
    with ready_session(client) as (worker, session):
        context, ws, command = begin(client, worker, session)
        worker.close()
        assert receive_error(ws) == "worker_disconnected"
        context.__exit__(None, None, None)
    view = current(client).json()
    assert view["state"] == "STOPPING"
    assert view["reason"].startswith("relay connection lost during a command")


def test_no_result_in_time_counts_as_lost(make_client, monkeypatch):
    monkeypatch.setattr(relay, "GUARD_SLACK_S", 0)
    client = make_client(session_command_timeout_s=0.2, session_sync_timeout_s=0.2)
    with ready_session(client) as (worker, session):
        context, ws, command = begin(client, worker, session)
        assert receive_error(ws) == "lost"
        context.__exit__(None, None, None)
        view = current(client).json()
        assert view["state"] == "STOPPING"
        assert view["reason"].startswith("no command result")


def test_a_session_the_worker_ended_stops_at_once_and_the_next_open_is_new(make_client):
    client = make_client()
    service = client.app.state.scheduler
    with ready_session(client) as (worker, session):
        context, ws, command = begin(client, worker, session)
        while not next_message(worker)["final"]:
            pass
        result = CommandResult(
            id=command["id"],
            outcome="timeout",
            reason="command time limit exceeded",
            session_ended=True,
        )
        worker.send_json(result.model_dump())
        assert ws.receive_json() == result.model_dump()
        context.__exit__(None, None, None)
        wait_for(lambda: not service.session_busy)
        # Before the attempt's FINISHED event: STOPPING, resources still held.
        view = job(client, session["id"])
        assert view["state"] == "STOPPING"
        assert view["reason"].startswith("command time limit exceeded")
        fresh = open_session(client)
        assert fresh["created"] is True and fresh["id"] != session["id"]
