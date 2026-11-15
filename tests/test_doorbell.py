"""Doorbell connection (T4), its in-memory relay (T6 plumbing) and outbox wake-ups (T8)."""

import socket
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from conftest import register, submit, user_headers, worker_headers
from starlette.websockets import WebSocketDisconnect
from test_worker import ControlledExecutor

from labflux.api import create_app
from labflux.config import Settings
from labflux.database import Database
from labflux.worker import Worker

DEMO_USER = {"Authorization": "Bearer dev-user-token"}  # Settings() default user
SPEC = {"image": "python:3.12-slim", "argv": ["true"], "resources": {"cpu": 1}}


def socket_url(instance, node="vm-a"):
    return f"/internal/workers/{node}/ws?instance_id={instance}"


def wait_for(predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition timed out")


def test_worker_socket_requires_the_worker_token(client):
    instance = register(client)
    for headers in ({}, user_headers(), {"Authorization": "Bearer wrong"}):
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(socket_url(instance), headers=headers):
                pass
        assert rejected.value.code == 1008
    assert client.app.state.hub.workers == {}


def test_worker_socket_accepts_only_the_current_instance(client):
    stale = register(client)
    current = register(client)  # A restart registers a new instance for the same node.
    for url in (socket_url(stale), socket_url(current, node="vm-unknown"), socket_url("")):
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(url, headers=worker_headers()):
                pass
        assert rejected.value.code == 1008
    with client.websocket_connect(socket_url(current), headers=worker_headers()):
        hub = client.app.state.hub
        wait_for(lambda: "vm-a" in hub.workers)
        assert hub.workers["vm-a"].instance_id == current
    wait_for(lambda: hub.workers == {})


def test_doorbell_rings_after_start_and_stop_commands_commit(client, service):
    instance = register(client, cpu=1)
    other = register(client, name="vm-b", cpu=1)
    with (
        client.websocket_connect(socket_url(instance), headers=worker_headers()) as ws,
        client.websocket_connect(socket_url(other, node="vm-b"), headers=worker_headers()) as idle,
    ):
        wait_for(lambda: len(client.app.state.hub.workers) == 2)
        job = submit(client)  # Scheduled onto vm-a, the first fitting node.
        assert ws.receive_json() == {"type": "commands"}
        assert [c["kind"] for c in service.poll_commands("vm-a", instance)] == ["START"]
        client.post(f"/api/jobs/{job['id']}/cancel", headers=user_headers())
        assert ws.receive_json() == {"type": "commands"}
        assert [c["kind"] for c in service.poll_commands("vm-a", instance)] == ["STOP"]
        # The relay is ordered, so vm-b saw nothing before this probe.
        client.app.state.hub.send("vm-b", {"type": "probe"})
        assert idle.receive_json() == {"type": "probe"}


def test_after_commit_callbacks_run_once_after_commit_and_never_after_rollback(tmp_path):
    db = Database(tmp_path)
    calls = []
    with db.transaction():
        db.after_commit(calls.append, "vm-a")
        db.after_commit(calls.append, "vm-a")
        db.after_commit(calls.append, "vm-b")
        assert calls == []  # Nothing runs while the transaction holds the lock.
    assert calls == ["vm-a", "vm-b"]
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.after_commit(calls.append, "vm-c")
            raise RuntimeError("rolled back")
    assert calls == ["vm-a", "vm-b"]
    db.close()


def test_hub_relays_messages_both_ways_by_type_and_id(client):
    instance = register(client)
    hub = client.app.state.hub
    by_type, by_id = [], []
    hub.on("output", lambda node, message: by_type.append((node, message)))
    unsubscribe = hub.subscribe("run-1", lambda node, message: by_id.append(message))
    with client.websocket_connect(socket_url(instance), headers=worker_headers()) as ws:
        wait_for(lambda: "vm-a" in hub.workers)
        hub.send("vm-a", {"type": "exec", "id": "run-1", "argv": ["pytest"]})
        assert ws.receive_json() == {"type": "exec", "id": "run-1", "argv": ["pytest"]}
        ws.send_json({"type": "output", "id": "run-1", "data": "a"})
        ws.send_json({"type": "output", "id": "run-2", "data": "b"})
        wait_for(lambda: by_id and by_type)
        unsubscribe()
        ws.send_json({"type": "output", "id": "run-1", "data": "c"})
        wait_for(lambda: len(by_type) == 2)
    assert [m["data"] for m in by_id] == ["a"]
    assert [(node, m["data"]) for node, m in by_type] == [("vm-a", "b"), ("vm-a", "c")]


@contextmanager
def live_lab(tmp_path, **worker_settings):
    """A Scheduler over HTTP and a running Worker whose fake containers finish at once."""
    app = create_app(Settings(data_dir=tmp_path / "scheduler", scheduler_interval_s=0.05))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    address = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    wait_for(lambda: server.started)
    executor = ControlledExecutor()
    executor.release.set()
    worker = Worker(
        Settings(
            data_dir=tmp_path / "worker",
            scheduler_url=address,
            worker_id="vm-a",
            allocatable_cpu=1,
            allocatable_memory_mib=256,
            heartbeat_interval_s=0.2,
            **worker_settings,
        ),
        executor=executor,
    )
    accepted = {}
    original = worker.accept

    def timed_accept(command):
        accepted.setdefault(command["id"], time.monotonic())
        return original(command)

    worker.accept = timed_accept
    worker_thread = threading.Thread(target=worker.run, daemon=True)
    try:
        worker_thread.start()
        with httpx.Client(base_url=address, headers=DEMO_USER, trust_env=False) as client:
            wait_for(lambda: app.state.scheduler.list_nodes())
            yield app, worker, client, accepted
    finally:
        worker.stop_event.set()
        worker_thread.join(timeout=20)
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def test_a_ring_makes_the_worker_poll_long_before_its_fallback(tmp_path):
    with live_lab(tmp_path, poll_interval_s=30) as (app, worker, client, accepted):
        wait_for(lambda: "vm-a" in app.state.hub.workers)
        time.sleep(0.2)  # Let the catch-up poll after connecting finish.
        before = time.monotonic()
        job = client.post("/api/jobs", json=SPEC).json()
        wait_for(lambda: accepted.get(f"start-{job['current_attempt_id']}"), timeout=5)
        assert accepted[f"start-{job['current_attempt_id']}"] - before < 2
        # The outbox is woken by the result too, so the job finishes without any fallback.
        wait_for(
            lambda: client.get(f"/api/jobs/{job['id']}").json()["state"] == "SUCCEEDED",
            timeout=5,
        )
        # Fast-lane plumbing on the Worker side: messages both ways over the same socket.
        received, replies = [], []
        worker.handlers["ping"] = received.append
        app.state.hub.on("pong", lambda node, message: replies.append((node, message)))
        app.state.hub.send("vm-a", {"type": "ping", "id": "p1"})
        wait_for(lambda: received)
        assert worker.send({"type": "pong", "id": received[0]["id"]})
        wait_for(lambda: replies)
        assert replies == [("vm-a", {"type": "pong", "id": "p1"})]


def test_fallback_poll_delivers_commands_while_the_socket_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr(Worker, "_doorbell", lambda self: None)  # Never connects.
    with live_lab(tmp_path, poll_interval_s=0.5) as (app, worker, client, accepted):
        job = client.post("/api/jobs", json=SPEC).json()
        wait_for(lambda: accepted.get(f"start-{job['current_attempt_id']}"), timeout=5)
        wait_for(
            lambda: client.get(f"/api/jobs/{job['id']}").json()["state"] == "SUCCEEDED",
            timeout=5,
        )
        assert app.state.hub.workers == {}


def test_outbox_writes_wake_the_flush_thread(client, tmp_path):
    instance = register(client, cpu=1)
    executor = ControlledExecutor()
    worker = Worker(
        Settings(data_dir=tmp_path / "worker", worker_id="vm-a", poll_interval_s=30),
        executor=executor,
    )
    worker.instance_id = instance
    worker.client.close()
    worker.client = client
    worker.client.headers["Authorization"] = "Bearer dev-worker-token"
    outbox = threading.Thread(target=worker._outbox, daemon=True)
    outbox.start()
    try:
        job = submit(client)
        [command] = client.app.state.scheduler.poll_commands("vm-a", instance)
        time.sleep(0.1)  # The outbox has made its first pass and now waits 30 s.
        worker.accept(command)

        def state():
            return client.get(f"/api/jobs/{job['id']}", headers=user_headers()).json()["state"]

        wait_for(lambda: state() == "RUNNING", timeout=3)
        logs = f"/api/jobs/{job['id']}/logs"
        wait_for(lambda: client.get(logs, headers=user_headers()).json()["chunks"], timeout=3)
        executor.release.set()
        wait_for(lambda: state() == "SUCCEEDED", timeout=3)
    finally:
        executor.release.set()
        worker.stop_event.set()
        outbox.join(timeout=5)
        worker.pool.shutdown(wait=True)
    assert not outbox.is_alive()  # stop_event also wakes the outbox wait.


def test_outbox_backs_off_while_the_scheduler_is_unreachable(tmp_path):
    worker = Worker(
        Settings(data_dir=tmp_path, worker_id="vm-a", poll_interval_s=30),
        executor=ControlledExecutor(),
    )
    calls = []

    def unreachable():
        calls.append(time.monotonic())
        raise httpx.ConnectError("scheduler down")

    worker.flush = unreachable
    outbox = threading.Thread(target=worker._outbox, daemon=True)
    outbox.start()
    end = time.monotonic() + 1.2
    while time.monotonic() < end:
        worker.outbox_ready.set()  # Busy job logs keep writing to the outbox.
        time.sleep(0.01)
    worker.stop_event.set()
    outbox.join(timeout=5)
    worker.pool.shutdown(wait=True)
    assert len(calls) == 2  # At 0 s and 0.5 s; the next retry waits another second.
