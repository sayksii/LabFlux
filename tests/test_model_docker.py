"""M10 end to end on real Docker (CPU only): a real Scheduler (uvicorn) with the inference
Gateway, a real Worker with DockerExecutor, and the fake model server in a container."""

import json
import socket
import threading
import time
import uuid
from pathlib import Path

import docker
import httpx
import pytest
import uvicorn

from labflux.api import create_app
from labflux.config import Settings
from labflux.runtime import DockerExecutor
from labflux.worker import Worker

pytestmark = pytest.mark.docker

IMAGE = "python:3.12-slim"
FAKE_DIR = Path(__file__).resolve().parents[1] / "examples" / "fake-model"
FAKE = {
    "image": IMAGE,
    "argv": ["python", "/datasets/fake-model/server.py"],
    "env": {"FAKE_MODEL_NAME": "fake", "FAKE_STARTUP_S": "1", "FAKE_TOKEN_DELAY_S": "0.05"},
    "resources": {"cpu": 0.5, "memory_mib": 256},
    "datasets": ["fake-model"],
    "startup_timeout_s": 60,
    "cold_wait_s": 60,
    "idle_stop_s": 4,
}
BROKEN = {
    **FAKE,
    "env": {"FAKE_MODEL_NAME": "broken", "FAKE_STARTUP_S": "3600"},
    "startup_timeout_s": 3,
    "cold_wait_s": 30,
}


def wait_for(predicate, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("end-to-end condition timed out")


@pytest.fixture
def gateway(tmp_path):
    scheduler_settings = Settings(
        data_dir=tmp_path / "scheduler",
        scheduler_interval_s=0.05,
        users={"alice": "alice-token", "bob": "bob-token"},
        models={"fake": FAKE, "broken": BROKEN},
    )
    worker_id = f"m10-{uuid.uuid4().hex[:8]}"
    worker_settings = Settings(
        data_dir=tmp_path / "worker",
        worker_id=worker_id,
        allocatable_cpu=2,
        allocatable_memory_mib=1024,
        heartbeat_interval_s=0.2,
        poll_interval_s=0.05,
        stop_grace_s=1,
        datasets={"fake-model": FAKE_DIR},
    )
    executor = DockerExecutor(worker_settings)
    try:
        executor.check()
        docker.from_env().images.get(IMAGE)
    except (docker.errors.DockerException, OSError, RuntimeError) as exc:
        pytest.skip(f"Docker or {IMAGE} unavailable: {exc}")
    app = create_app(scheduler_settings)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    address = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    wait_for(lambda: server.started)
    worker = Worker(worker_settings.model_copy(update={"scheduler_url": address}))
    worker_thread = threading.Thread(target=worker.run, daemon=True)
    worker_thread.start()
    clients = {
        user: httpx.Client(
            base_url=address,
            headers={"Authorization": f"Bearer {user}-token"},
            trust_env=False,
            timeout=90,
        )
        for user in ("alice", "bob")
    }
    service = app.state.scheduler

    def containers():
        return docker.from_env().containers.list(
            all=True, filters={"label": f"labflux.worker_id={worker_id}"}
        )

    try:
        wait_for(lambda: clients["alice"].get("/api/nodes").json())
        yield clients, containers, service
    finally:
        for client in clients.values():
            client.close()
        worker.stop_event.set()
        worker_thread.join(timeout=20)
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        for container in containers():
            container.remove(force=True)
        assert not worker_thread.is_alive()
        assert not thread.is_alive()


def chat(content, stream=True, model="fake"):
    return {"model": model, "stream": stream, "messages": [{"role": "user", "content": content}]}


def streamed_text(response_text: str) -> str:
    events = [e for e in response_text.split("\n\n") if e]
    assert events[-1] == "data: [DONE]"
    return "".join(json.loads(e[6:])["choices"][0]["delta"]["content"] for e in events[:-1])


def model_view(client, name="fake"):
    return next(m for m in client.get("/api/models").json() if m["name"] == name)


def test_models_start_on_demand_are_shared_streamed_and_released(gateway):
    clients, containers, service = gateway
    alice, bob = clients["alice"], clients["bob"]

    # Two users' cold requests at once: one service starts and both are streamed back.
    results = {}

    def ask(user, client):
        response = client.post("/v1/chat/completions", json=chat(f"hello from {user}"))
        results[user] = (response.status_code, response.headers["content-type"], response.text)

    threads = [threading.Thread(target=ask, args=item) for item in clients.items()]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(90)
    cold_s = time.monotonic() - started
    for user in ("alice", "bob"):
        status, content_type, text = results[user]
        assert status == 200 and content_type.startswith("text/event-stream")
        assert streamed_text(text) == f"echo: hello from {user}"
    assert len(containers()) == 1
    view = model_view(alice)
    assert (view["state"], view["inflight"]) == ("serving", 0)
    print(f"cold start and two streamed replies: {cold_s:.1f}s")

    # Plain completions and embeddings on the warm service.
    started = time.monotonic()
    plain = bob.post("/v1/completions", json={"model": "fake", "prompt": "warm"})
    warm_s = time.monotonic() - started
    assert plain.status_code == 200 and plain.json()["choices"][0]["text"] == "echo: warm"
    embedding = alice.post("/v1/embeddings", json={"model": "fake", "input": "x"})
    assert len(embedding.json()["data"][0]["embedding"]) == 8
    print(f"warm non-streamed request: {warm_s * 1000:.0f} ms")
    listed = alice.get("/v1/models").json()["data"]
    assert {m["id"] for m in listed} == {"fake", "broken"}

    # A client that leaves mid-stream: the request ends and the model server stops writing.
    long = " ".join(["word"] * 200)
    with alice.stream("POST", "/v1/chat/completions", json=chat(long)) as response:
        assert response.status_code == 200
        next(response.iter_raw())
    wait_for(lambda: model_view(alice)["inflight"] == 0, timeout=20)
    [container] = containers()
    wait_for(lambda: b"client disconnected mid-stream" in container.logs(), timeout=20)

    # Idle for idle_stop_s: stopped, its container removed and its resources released.
    wait_for(lambda: model_view(alice)["state"] == "stopped", timeout=30)
    wait_for(lambda: not containers(), timeout=30)
    allocated = alice.get("/api/nodes").json()[0]["allocated"]
    assert (allocated["cpu"], allocated["memory_mib"], allocated["jobs"]) == (0, 0, 0)


def test_a_model_that_never_gets_healthy_fails_and_is_not_restarted_at_once(gateway):
    clients, containers, service = gateway
    alice = clients["alice"]
    response = alice.post("/v1/chat/completions", json=chat("hi", stream=False, model="broken"))
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["type"] == "model_failed"
    assert "model server not healthy within 3s" in error["message"]
    assert int(response.headers["retry-after"]) > 200
    view = model_view(alice, "broken")
    assert view["state"] == "failed" and view["retry_at"]
    wait_for(lambda: not containers(), timeout=30)
    again = alice.post("/v1/completions", json={"model": "broken", "prompt": "hi"})
    assert again.status_code == 503 and again.json()["error"]["type"] == "model_failed"
    assert not containers()  # Not started again before its retry time.
