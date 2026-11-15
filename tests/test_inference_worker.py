"""M10 Worker side: model readiness and relayed inference requests, against the fake model
server (examples/fake-model/server.py) running in this process instead of in a container."""

import importlib.util
import io
import json
import threading
import time
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from test_doorbell import wait_for

from labflux.config import Settings
from labflux.inference import InferBody, InferCancel, InferRequest
from labflux.models import JobSpec, ServiceSpec
from labflux.runtime import ExecutionResult
from labflux.session_protocol import decode, iter_chunks
from labflux.worker import Worker

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "fake_model", ROOT / "examples" / "fake-model" / "server.py"
)
fake_model = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_model)


class ServiceExecutor:
    """A fake model container: run() reports the launch and stays up until cancelled; the
    server it points at is the in-process fake model."""

    def __init__(self, address):
        self.address = address
        self.alive: dict[str, bool] = {}

    def check(self):
        return {}

    def run(self, spec, workspace, outputs, on_log, on_started, cancel, **options):
        self.alive[spec.attempt_id] = True
        on_started()
        cancel.wait()
        self.alive[spec.attempt_id] = False
        return ExecutionResult("CANCELLED", 137, "cancelled", True)

    def service_address(self, spec):
        return self.address


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(fake_model, "NAME", "fake")
    monkeypatch.setattr(fake_model, "TOKEN_DELAY_S", 0.01)
    monkeypatch.setattr(fake_model, "STARTUP_S", 0)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), fake_model.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def lab(tmp_path, server):
    executor = ServiceExecutor(f"http://127.0.0.1:{server.server_address[1]}")
    worker = Worker(Settings(data_dir=tmp_path / "worker", worker_id="vm-a"), executor=executor)
    worker.sent = []
    worker.send = lambda message: worker.sent.append(message) or True
    yield worker, executor
    for handle in worker.handles.values():
        handle["cancel"].set()
    worker.pool.shutdown(wait=True)
    worker.client.close()
    worker.local.close()


def start_model(worker, startup_timeout_s=30):
    attempt_id = uuid.uuid4().hex
    spec = JobSpec(image="python:3.12-slim", argv=["python", "/datasets/fake-model/server.py"])
    payload = {
        "job_id": uuid.uuid4().hex,
        "attempt_id": attempt_id,
        "worker_id": "vm-a",
        "job": spec.model_dump(),
        "gpu_uuids": [],
        "service": ServiceSpec(name="fake", startup_timeout_s=startup_timeout_s).model_dump(),
    }
    worker.accept({"id": f"start-{attempt_id}", "kind": "START", "payload": payload})
    return attempt_id


def send_request(worker, attempt_id, body: dict, path="chat/completions"):
    data = json.dumps(body).encode()
    request = InferRequest(
        id=uuid.uuid4().hex,
        attempt_id=attempt_id,
        path=path,
        body_size=len(data),
        read_timeout_s=10,
    )
    worker._on_message(json.dumps(request.model_dump()))
    for text, final in iter_chunks(io.BytesIO(data)):
        worker._on_message(
            json.dumps(InferBody(id=request.id, data=text, final=final).model_dump())
        )
    return request.id


def replies(worker, request_id, done=True):
    def ended():
        found = [m for m in worker.sent if m.get("id") == request_id]
        return found if found and found[-1]["type"] == "infer_end" else None

    return wait_for(ended) if done else [m for m in worker.sent if m.get("id") == request_id]


def body_of(found):
    return b"".join(decode(m["data"]) for m in found if m["type"] == "infer_chunk")


def test_started_is_reported_only_once_the_health_check_passes(lab, monkeypatch):
    worker, _ = lab
    monkeypatch.setattr(fake_model, "STARTED", fake_model.time.monotonic())
    monkeypatch.setattr(fake_model, "STARTUP_S", 1.5)
    attempt_id = start_model(worker)
    wait_for(lambda: attempt_id in worker.active)
    threading.Event().wait(0.5)
    assert worker._read(attempt_id)["started"] is False  # /health still answers 503.
    wait_for(lambda: worker._read(attempt_id)["started"], timeout=5)


def test_a_model_that_never_gets_healthy_is_stopped_as_failed(lab, monkeypatch):
    worker, _ = lab
    monkeypatch.setattr(fake_model, "STARTED", fake_model.time.monotonic())
    monkeypatch.setattr(fake_model, "STARTUP_S", 3600)
    attempt_id = start_model(worker, startup_timeout_s=1)
    result = wait_for(lambda: worker._read(attempt_id)["result"], timeout=5)
    assert result["outcome"] == "FAILED"
    assert result["reason"] == "model server not healthy within 1s"
    assert result["termination_confirmed"] is True


def test_a_streamed_chat_request_is_relayed_as_it_arrives(lab):
    worker, _ = lab
    attempt_id = start_model(worker)
    wait_for(lambda: worker._read(attempt_id)["started"])
    message = {"role": "user", "content": "hello there"}
    found = replies(
        worker,
        send_request(worker, attempt_id, {"model": "fake", "stream": True, "messages": [message]}),
    )
    head, end = found[0], found[-1]
    assert head["type"] == "infer_head" and head["status"] == 200
    assert head["content_type"] == "text/event-stream"
    assert end["error"] is None
    events = [line for line in body_of(found).decode().split("\n\n") if line]
    assert events[-1] == "data: [DONE]"
    words = [json.loads(e[6:])["choices"][0]["delta"]["content"] for e in events[:-1]]
    assert "".join(words) == "echo: hello there"
    assert len([m for m in found if m["type"] == "infer_chunk"]) > 1  # Not buffered.


def test_plain_completion_and_embedding_requests(lab):
    worker, _ = lab
    attempt_id = start_model(worker)
    wait_for(lambda: worker._read(attempt_id)["started"])
    found = replies(
        worker,
        send_request(worker, attempt_id, {"model": "fake", "prompt": "hi"}, path="completions"),
    )
    assert json.loads(body_of(found))["choices"][0]["text"] == "echo: hi"
    found = replies(
        worker, send_request(worker, attempt_id, {"model": "fake", "input": "x"}, path="embeddings")
    )
    assert len(json.loads(body_of(found))["data"][0]["embedding"]) == 8
    found = replies(
        worker,
        send_request(worker, attempt_id, {"model": "other", "prompt": "hi"}, path="completions"),
    )
    assert found[0]["status"] == 404 and found[-1]["error"] is None  # The server's own answer.


def test_cancel_stops_a_stream_and_requests_need_a_serving_model(lab, monkeypatch):
    worker, _ = lab
    monkeypatch.setattr(fake_model, "TOKEN_DELAY_S", 0.5)
    attempt_id = start_model(worker)
    wait_for(lambda: worker._read(attempt_id)["started"])
    long = " ".join(["word"] * 100)
    request_id = send_request(
        worker,
        attempt_id,
        {"model": "fake", "stream": True, "messages": [{"role": "user", "content": long}]},
    )
    wait_for(lambda: any(m["type"] == "infer_chunk" for m in replies(worker, request_id, False)))
    worker._on_message(json.dumps(InferCancel(id=request_id).model_dump()))
    found = replies(worker, request_id)
    assert found[-1]["error"] == "cancelled by the client"
    assert len([m for m in found if m["type"] == "infer_chunk"]) < 10
    assert request_id not in worker.inference
    # A request for an attempt that is not a serving model on this worker is refused.
    missing = replies(worker, send_request(worker, uuid.uuid4().hex, {"model": "fake"}))
    assert missing == [
        {
            "type": "infer_end",
            "id": missing[0]["id"],
            "error": "the model is not serving on this worker",
        }
    ]


def test_a_stopped_model_ends_its_requests_in_flight(lab, monkeypatch):
    worker, executor = lab
    monkeypatch.setattr(fake_model, "TOKEN_DELAY_S", 0.5)
    attempt_id = start_model(worker)
    wait_for(lambda: worker._read(attempt_id)["started"])
    long = " ".join(["word"] * 100)
    request_id = send_request(
        worker,
        attempt_id,
        {"model": "fake", "stream": True, "messages": [{"role": "user", "content": long}]},
    )
    wait_for(lambda: any(m["type"] == "infer_chunk" for m in replies(worker, request_id, False)))
    worker.handles[attempt_id]["cancel"].set()  # The Scheduler's STOP.
    found = replies(worker, request_id)
    assert found[-1]["error"] == "the model service ended"
    wait_for(lambda: not executor.alive[attempt_id])


def test_a_cancel_does_not_wait_for_the_next_token(lab, monkeypatch):
    """The cancel shuts the model connection down, which wakes the thread blocked on it (and
    tells the model server to stop) instead of waiting for the next token's chunk check."""
    worker, _ = lab
    monkeypatch.setattr(fake_model, "TOKEN_DELAY_S", 5)
    attempt_id = start_model(worker)
    wait_for(lambda: worker._read(attempt_id)["started"])
    request_id = send_request(
        worker,
        attempt_id,
        {"model": "fake", "stream": True, "messages": [{"role": "user", "content": "a b c"}]},
    )
    wait_for(lambda: any(m["type"] == "infer_chunk" for m in replies(worker, request_id, False)))
    cancelled = time.monotonic()
    worker._on_message(json.dumps(InferCancel(id=request_id).model_dump()))
    found = replies(worker, request_id)
    assert time.monotonic() - cancelled < 1.5
    assert found[-1]["error"] == "cancelled by the client"
    assert body_of(found).count(b"data: ") == 1  # Only the first token.
    assert request_id not in worker.inference


def test_a_cancel_after_the_whole_response_still_ends_it_as_complete(lab, monkeypatch):
    worker, _ = lab
    attempt_id = start_model(worker)
    wait_for(lambda: worker._read(attempt_id)["started"])
    drop = worker._drop_infer

    def cancel_then_drop(run):
        run.reason = "cancelled by the client"  # The client left just as the response ended.
        run.cancel.set()
        drop(run)

    monkeypatch.setattr(worker, "_drop_infer", cancel_then_drop)
    found = replies(
        worker,
        send_request(worker, attempt_id, {"model": "fake", "prompt": "hi"}, path="completions"),
    )
    assert json.loads(body_of(found))["choices"][0]["text"] == "echo: hi"
    assert found[-1]["error"] is None


def test_a_request_body_that_does_not_match_its_size_is_refused(lab):
    worker, _ = lab
    attempt_id = start_model(worker)
    wait_for(lambda: worker._read(attempt_id)["started"])
    request = InferRequest(
        id=uuid.uuid4().hex,
        attempt_id=attempt_id,
        path="completions",
        body_size=3,
        read_timeout_s=5,
    )
    worker._on_message(json.dumps(request.model_dump()))
    chunk = InferBody(id=request.id, data="aGVsbG8=", final=True)  # 5 bytes.
    worker._on_message(json.dumps(chunk.model_dump()))
    assert (
        replies(worker, request.id)[-1]["error"] == "request body does not match its declared size"
    )
