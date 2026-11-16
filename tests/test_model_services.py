"""M10 on the Scheduler: model services (job rows of kind MODEL) and the inference Gateway."""

import json
import socket
import threading
from contextlib import ExitStack

import httpx
import pytest
import uvicorn
from conftest import finish, heartbeat, register, submit, user_headers, worker_headers
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_doorbell import socket_url, wait_for
from websockets.sync.client import connect

from labflux.api import create_app
from labflux.config import ModelConfig, Settings
from labflux.database import commands, jobs, project_results
from labflux.inference import RELAYED_PATHS, TruncatedResponse
from labflux.models import MODEL_KIND, MODEL_OWNER
from labflux.scheduler import PERSISTENT_LEASE_S, ServiceError
from labflux.session_protocol import decode, encode

FAKE = {
    "image": "python:3.12-slim",
    "argv": ["python", "/datasets/fake-model/server.py"],
    "env": {"FAKE_MODEL_NAME": "fake"},
    "resources": {"cpu": 1, "memory_mib": 512},
    "datasets": ["fake-model"],
    "idle_stop_s": 300,
    "reclaim_idle_s": 60,
    "retry_after_failure_s": 120,
    "cold_wait_s": 0,
}


@pytest.fixture
def make_client(tmp_path, clock):
    with ExitStack() as stack:

        def build(models=None, name="default", **settings):
            values = Settings(
                data_dir=tmp_path / name,
                users={"alice": "alice-token", "bob": "bob-token"},
                models=models if models is not None else {"fake": FAKE},
                **settings,
            )
            app = create_app(values, run_loop=False, clock=lambda: clock[0])
            return stack.enter_context(TestClient(app))

        yield build


def model_jobs(service, name="fake"):
    with service.db.transaction() as connection:
        return [
            dict(row._mapping)
            for row in connection.execute(
                select(jobs)
                .where(jobs.c.kind == MODEL_KIND, jobs.c.service == name)
                .order_by(jobs.c.submission_seq)
            )
        ]


def report(client, service, job_id, instance, kind="STARTED", **fields):
    """Send a Worker event for the model job's current attempt."""
    with service.db.transaction() as connection:
        attempt_id = connection.execute(
            select(jobs.c.current_attempt_id).where(jobs.c.id == job_id)
        ).scalar_one()
        node_id = connection.execute(
            select(commands.c.node_id).where(commands.c.attempt_id == attempt_id)
        ).scalar()
    body = {"event_id": f"{kind}-{attempt_id}", "instance_id": instance, "kind": kind, **fields}
    response = client.post(
        f"/internal/workers/{node_id}/attempts/{attempt_id}/events",
        headers=worker_headers(),
        json=body,
    )
    assert response.status_code == 200, response.text


def stopped(client, service, job_id, instance, outcome="CANCELLED", reason="stopped"):
    report(
        client,
        service,
        job_id,
        instance,
        kind="FINISHED",
        outcome=outcome,
        termination_confirmed=True,
        reason=reason,
    )


def serving(client, service, *, cpu=2, memory=1024):
    """Register vm-a with the weights dataset and bring the fake model to RUNNING."""
    instance = register(client, cpu=cpu, memory=memory, datasets=["fake-model"])
    view = service.ensure_model("fake")
    report(client, service, view["job_id"], instance)
    return instance, service.ensure_model("fake")


def advance(client, clock, seconds, instance, node="vm-a"):
    """Move the clock and keep the node alive, as its 2 s heartbeats would."""
    clock[0] += seconds
    heartbeat(client, node, instance)


# Lifecycle -----------------------------------------------------------------------------------


def test_requests_start_one_service_on_a_node_with_the_weights(make_client):
    client = make_client()
    service = client.app.state.scheduler
    register(client, name="vm-a", cpu=4)  # No weights here.
    instance = register(client, name="vm-b", cpu=2, datasets=["fake-model"])
    first = service.ensure_model("fake")
    # A second request while it is starting merges into the same service.
    second = service.ensure_model("fake")
    assert first["job_id"] == second["job_id"]
    assert (first["state"], first["node_id"]) == ("starting", "vm-b")
    [job] = model_jobs(service)
    assert job["owner"] == MODEL_OWNER
    [command] = service.poll_commands("vm-b", instance)
    payload = command["payload"]
    assert payload["service"] == {
        "name": "fake",
        "port": 8000,
        "health_path": "/health",
        "startup_timeout_s": 300.0,
        "shm_mib": 1024,
        "pids_limit": 4096,
    }
    assert payload["job"]["env"] == {"HOME": "/workspace", "FAKE_MODEL_NAME": "fake"}
    assert payload["job"]["datasets"] == ["fake-model"]
    # Nobody's job list shows it, and no user can cancel it.
    assert client.get("/api/jobs", headers=user_headers()).json() == []
    assert client.post(f"/api/jobs/{job['id']}/cancel", headers=user_headers()).status_code == 404


def test_states_from_stopped_to_serving_and_back(make_client, clock):
    client = make_client()
    service = client.app.state.scheduler
    [view] = client.get("/api/models", headers=user_headers()).json()
    assert (view["state"], view["mode"]) == ("stopped", "on_demand")
    instance = register(client, cpu=1, datasets=["fake-model"])
    submit(client, cpu=1)  # Takes the only CPU, so the model waits for resources.
    assert service.ensure_model("fake")["state"] == "waiting"
    batch = client.get("/api/jobs", headers=user_headers()).json()[0]
    batch = client.get(f"/api/jobs/{batch['id']}", headers=user_headers()).json()
    assert finish(client, batch, instance).status_code == 200
    view = service.ensure_model("fake")
    assert view["state"] == "starting"
    report(client, service, view["job_id"], instance)
    view = service.ensure_model("fake")
    assert view["state"] == "serving"
    assert view["idle_stop_at"] == clock[0] + 300
    listed = client.get("/v1/models", headers=user_headers()).json()
    assert listed["object"] == "list"
    assert [(m["id"], m["labflux"]["state"]) for m in listed["data"]] == [("fake", "serving")]
    advance(client, clock, 301, instance)
    service.tick()
    view = service.list_models()[0]
    assert view["state"] == "stopping" and view["reason"].startswith("idle for 301s")
    stopped(client, service, view["job_id"], instance)
    assert service.list_models()[0]["state"] == "stopped"


def test_idle_stop_waits_for_requests_in_flight_and_restarts_after_each(make_client, clock):
    client = make_client()
    service = client.app.state.scheduler
    instance, view = serving(client, service)
    job_id = view["job_id"]
    service.begin_request(job_id)
    advance(client, clock, 1000, instance)
    service.tick()
    assert service.list_models()[0]["state"] == "serving"  # A long request keeps it.
    service.end_request(job_id)
    assert service.list_models()[0]["idle_stop_at"] == clock[0] + 300
    advance(client, clock, 299, instance)
    service.tick()
    assert service.list_models()[0]["state"] == "serving"
    advance(client, clock, 2, instance)
    service.tick()
    assert service.list_models()[0]["state"] == "stopping"
    stopped(client, service, job_id, instance)
    # The next request starts a new service.
    assert service.ensure_model("fake")["job_id"] != job_id


def test_an_idle_model_is_reclaimed_early_when_jobs_wait(make_client, clock):
    client = make_client()
    service = client.app.state.scheduler
    instance, view = serving(client, service, cpu=1)
    submit(client, cpu=1)  # Does not fit while the model holds the CPU.
    advance(client, clock, 59, instance)
    service.tick()
    assert service.list_models()[0]["state"] == "serving"
    advance(client, clock, 2, instance)
    service.tick()
    view = service.list_models()[0]
    assert view["state"] == "stopping"
    assert "while other jobs wait for resources" in view["reason"]
    stopped(client, service, view["job_id"], instance)
    [batch] = client.get("/api/jobs", headers=user_headers()).json()
    assert batch["state"] == "STARTING"


def test_a_persistent_model_starts_by_itself_and_never_idles_out(make_client, clock):
    client = make_client(models={"fake": {**FAKE, "mode": "persistent"}})
    service = client.app.state.scheduler
    instance = register(client, cpu=2, datasets=["fake-model"])
    service.tick()
    [job] = model_jobs(service)
    assert job["state"] == "STARTING"
    report(client, service, job["id"], instance)
    assert service.list_models()[0]["idle_stop_at"] is None
    assert model_jobs(service)[0]["lease_expires_at"] == clock[0] + PERSISTENT_LEASE_S
    submit(client, cpu=2)  # Waiting jobs do not reclaim it either.
    advance(client, clock, 10 * 3600, instance)
    service.tick()
    assert service.list_models()[0]["state"] == "serving"


def test_a_model_that_fails_is_retried_only_after_its_backoff(make_client, clock):
    client = make_client(models={"fake": {**FAKE, "mode": "persistent"}})
    service = client.app.state.scheduler
    instance = register(client, cpu=2, datasets=["fake-model"])
    service.tick()
    [job] = model_jobs(service)
    stopped(client, service, job["id"], instance, "FAILED", "model server not healthy within 300s")
    advance(client, clock, 60, instance)
    service.tick()
    assert len(model_jobs(service)) == 1
    view = service.ensure_model("fake")  # A request does not bypass the backoff.
    assert (view["state"], view["retry_at"]) == ("failed", job["updated_at"] + 120)
    assert view["reason"] == "model server not healthy within 300s"
    advance(client, clock, 61, instance)
    service.tick()
    assert [j["state"] for j in model_jobs(service)] == ["FAILED", "STARTING"]


def test_a_model_removed_from_the_configuration_is_stopped(make_client):
    client = make_client(models={"fake": FAKE, "other": {**FAKE, "resources": {"cpu": 8}}})
    service = client.app.state.scheduler
    register(client, cpu=2, datasets=["fake-model"])
    running = service.ensure_model("fake")["job_id"]
    service.ensure_model("other")  # Never fits: stays QUEUED.
    service.settings.models = {}
    service.tick()
    [fake] = model_jobs(service)
    [other] = model_jobs(service, "other")
    assert (fake["id"], fake["state"]) == (running, "STOPPING")
    assert fake["reason"].startswith("model removed from the configuration")
    assert (other["state"], other["reason"]) == (
        "CANCELLED",
        "model removed from the configuration",
    )


def test_model_services_are_scheduled_ahead_of_batch_jobs(make_client):
    client = make_client()
    service = client.app.state.scheduler
    instance = register(client, cpu=1, datasets=["fake-model"])
    first = submit(client, cpu=1)
    queued = submit(client, cpu=1)
    service.ensure_model("fake")  # Asked after the batch job, still goes first.
    first = client.get(f"/api/jobs/{first['id']}", headers=user_headers()).json()
    assert finish(client, first, instance).status_code == 200
    assert service.list_models()[0]["state"] == "starting"
    queued = client.get(f"/api/jobs/{queued['id']}", headers=user_headers()).json()
    assert queued["state"] == "QUEUED"


def test_unknown_model_and_invalid_configuration():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="invalid model name"):
        Settings(models={"bad name": FAKE})
    with pytest.raises(ValidationError, match="NVIDIA_VISIBLE_DEVICES"):
        Settings(models={"fake": {**FAKE, "env": {"NVIDIA_VISIBLE_DEVICES": "all"}}})
    with pytest.raises(ValidationError):
        Settings(models={"fake": {**FAKE, "mode": "sometimes"}})


def test_ensure_model_rejects_unconfigured_names(make_client):
    service = make_client().app.state.scheduler
    with pytest.raises(ServiceError) as error:
        service.ensure_model("missing")
    assert error.value.status == 404


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
@pytest.mark.parametrize(
    "field",
    ["startup_timeout_s", "cold_wait_s", "idle_stop_s", "reclaim_idle_s", "retry_after_failure_s"],
)
def test_model_times_must_be_finite(field, value):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(models={"fake": {**FAKE, field: value}})
    with pytest.raises(ValidationError):
        Settings(gateway_read_timeout_s=value)


def test_a_queued_model_does_not_block_later_jobs_and_is_cancelled_once_unwanted(
    make_client, clock
):
    client = make_client(models={"fake": FAKE, "keep": {**FAKE, "mode": "persistent"}})
    service = client.app.state.scheduler
    weights = register(client, name="vm-a", cpu=1, datasets=["fake-model"])
    plain = register(client, name="vm-b", cpu=1)
    submit(client, cpu=1, datasets=["fake-model"])  # Takes vm-a, the only node with weights.
    assert service.ensure_model("fake")["state"] == "waiting"
    service.tick()  # Queues the persistent model too.
    later = submit(client, cpu=1)  # fcfs, yet neither queued model holds it up.
    assert (later["state"], later["attempts"][-1]["node_id"]) == ("STARTING", "vm-b")
    assert model_jobs(service)[0]["schedule_note"].endswith(
        "model services do not hold up later jobs"
    )

    def wait(seconds):
        clock[0] += seconds
        heartbeat(client, "vm-a", weights)
        heartbeat(client, "vm-b", plain)
        service.tick()

    wait(20)
    service.ensure_model("fake")  # A request still waits for it.
    wait(29)
    assert model_jobs(service)[0]["state"] == "QUEUED"
    wait(2)  # More than cold_wait_s (0) + 30 s since the last request.
    [fake] = model_jobs(service)
    assert (fake["state"], fake["reason"]) == (
        "CANCELLED",
        "cancelled while queued: no request is waiting for it",
    )
    assert [job["state"] for job in model_jobs(service, "keep")] == ["QUEUED"]


def test_a_configuration_change_restarts_a_live_model_with_the_new_spec(make_client):
    persistent = {**FAKE, "mode": "persistent"}
    client = make_client(models={"fake": persistent})
    service = client.app.state.scheduler
    instance = register(client, cpu=1, datasets=["fake-model"])
    service.tick()
    [old] = model_jobs(service)
    report(client, service, old["id"], instance)
    service.tick()  # An unchanged configuration leaves it serving.
    assert model_jobs(service)[0]["state"] == "RUNNING"
    # Only the ServiceSpec of its START command differs.
    service.settings.models = {"fake": ModelConfig(**persistent, port=9000)}
    service.tick()
    old, new = model_jobs(service)
    assert old["state"] == "STOPPING"
    assert old["reason"].startswith("model configuration changed; ")
    assert new["state"] == "QUEUED"  # No backoff: a stop is not a failure.
    # The queued service picks up a further change through its stored JobSpec.
    argv = [*FAKE["argv"], "--v2"]
    service.settings.models = {"fake": ModelConfig(**{**persistent, "argv": argv}, port=9000)}
    service.tick()
    assert model_jobs(service)[1]["spec"]["argv"] == argv
    stopped(client, service, old["id"], instance)
    [command] = service.poll_commands("vm-a", instance)
    assert command["payload"]["job"]["argv"] == argv
    assert command["payload"]["service"]["port"] == 9000


def test_every_user_can_read_a_model_servers_logs(make_client):
    client = make_client()
    service = client.app.state.scheduler
    instance, view = serving(client, service)
    upload = {"instance_id": instance, "seq": 0, "stream": "stderr", "data_base64": encode(b"up\n")}
    response = client.post(
        f"/internal/workers/vm-a/attempts/{view['attempt_id']}/logs",
        headers=worker_headers(),
        json=upload,
    )
    assert response.status_code == 200, response.text
    [listed] = client.get("/api/models", headers=user_headers("bob")).json()
    for user in ("alice", "bob"):
        response = client.get(f"/api/jobs/{listed['job_id']}/logs", headers=user_headers(user))
        assert response.status_code == 200, response.text
        assert [decode(chunk["data_base64"]) for chunk in response.json()["chunks"]] == [b"up\n"]
    batch = submit(client, cpu=1)  # Other jobs stay private to their owner.
    assert client.get(f"/api/jobs/{batch['id']}/logs", headers=user_headers()).status_code == 200
    path = f"/api/jobs/{batch['id']}/logs"
    assert client.get(path, headers=user_headers("bob")).status_code == 404


def test_an_adopted_model_serves_only_after_its_started_event(make_client):
    client = make_client()
    service = client.app.state.scheduler
    register(client, cpu=2, datasets=["fake-model"])
    view = service.ensure_model("fake")
    attempt_id, new = view["attempt_id"], "1" * 32
    # The Worker restarts while the model loads; its running container is adopted.
    response = client.post(
        "/internal/workers/register",
        headers=worker_headers(),
        json={
            "worker_id": "vm-a",
            "instance_id": new,
            "capacity": {
                "total_cpu": 2,
                "total_memory_mib": 1536,
                "cpu": 2,
                "memory_mib": 1024,
                "datasets": ["fake-model"],
            },
            "inventory": {
                "attempts": [{"attempt_id": attempt_id}],
                "containers": [
                    {"attempt_id": attempt_id, "worker_id": "vm-a", "status": "running"}
                ],
            },
        },
    )
    assert [item["attempt_id"] for item in response.json()["adopt"]] == [attempt_id]
    service.tick()
    assert service.list_models()[0]["state"] == "starting"
    report(client, service, view["job_id"], new)
    assert service.list_models()[0]["state"] == "serving"


def test_a_model_service_is_not_a_project_job(make_client):
    """No kept directory, no earlier run to start from (architecture 6.1)."""
    client = make_client()
    service = client.app.state.scheduler
    instance, view = serving(client, service)
    assert model_jobs(service)[0]["spec"]["project"] is None
    with service.db.transaction() as connection:
        start = connection.execute(
            select(commands.c.payload).where(commands.c.id == f"start-{view['attempt_id']}")
        ).scalar_one()
    assert "previous_attempt_id" not in start
    report(
        client,
        service,
        view["job_id"],
        instance,
        kind="FINISHED",
        outcome="FAILED",
        termination_confirmed=True,
        reason="exited",
        kept_bytes=10,
    )
    with service.db.transaction() as connection:
        assert connection.execute(select(func.count()).select_from(project_results)).scalar() == 0


# Gateway -------------------------------------------------------------------------------------


def chat(stream=False, model="fake"):
    return {"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": stream}


def next_message(worker):
    while (message := worker.receive_json()) == {"type": "commands"}:
        pass
    return message


def in_thread(function):
    """Run a blocking client call while the test plays the Worker; returns its result, or the
    exception it raised, later."""
    result = {}

    def run():
        try:
            result["value"] = function()
        except Exception as exc:
            result["value"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    def join():
        thread.join(10)
        assert not thread.is_alive(), "the request did not finish"
        return result["value"]

    return join


def connected(client, service):
    """A serving model on vm-a whose fake Worker socket is connected."""
    instance = register(client, cpu=2, datasets=["fake-model"])
    stack = ExitStack()
    worker = stack.enter_context(
        client.websocket_connect(socket_url(instance), headers=worker_headers())
    )
    wait_for(lambda: "vm-a" in client.app.state.hub.workers)
    view = service.ensure_model("fake")
    report(client, service, view["job_id"], instance)
    return stack, worker, service.ensure_model("fake")


def receive_request(worker):
    request = next_message(worker)
    assert request["type"] == "infer"
    body = b""
    while True:
        chunk = next_message(worker)
        assert chunk["type"] == "infer_body" and chunk["id"] == request["id"]
        body += decode(chunk["data"])
        if chunk["final"]:
            return request, json.loads(body)


def test_a_streamed_request_is_relayed_through_the_worker(make_client, clock):
    client = make_client()
    service = client.app.state.scheduler
    stack, worker, view = connected(client, service)
    with stack:
        join = in_thread(
            lambda: client.post("/v1/chat/completions", headers=user_headers(), json=chat(True))
        )
        request, body = receive_request(worker)
        assert (request["attempt_id"], request["path"]) == (view["attempt_id"], "chat/completions")
        assert body == chat(True)
        assert service.list_models()[0]["inflight"] == 1
        rid = request["id"]
        worker.send_json(
            {"type": "infer_head", "id": rid, "status": 200, "content_type": "text/event-stream"}
        )
        for piece in (b"data: one\n\n", b"data: two\n\n", b"data: [DONE]\n\n"):
            worker.send_json({"type": "infer_chunk", "id": rid, "data": encode(piece)})
        clock[0] += 50
        worker.send_json({"type": "infer_end", "id": rid, "error": None})
        response = join()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text == "data: one\n\ndata: two\n\ndata: [DONE]\n\n"
    view = service.list_models()[0]
    assert view["inflight"] == 0
    assert view["idle_stop_at"] == clock[0] + 300  # Idle time restarts when a request ends.


def test_gateway_errors_use_the_openai_shape(make_client):
    client = make_client()
    headers = user_headers()
    assert client.post("/v1/chat/completions", json=chat()).status_code == 401
    missing = client.post("/v1/chat/completions", headers=headers, json=chat(model="nope"))
    assert missing.status_code == 404
    assert missing.json()["error"]["type"] == "model_not_found"
    bad = client.post("/v1/completions", headers=headers, content=b"not json")
    assert bad.status_code == 400 and "model name" in bad.json()["error"]["message"]
    assert client.post("/v1/audio/speech", headers=headers, json=chat()).status_code == 404
    assert RELAYED_PATHS == {"chat/completions", "completions", "embeddings"}


def test_a_cold_request_starts_the_model_and_gets_a_retryable_503(make_client):
    client = make_client()
    service = client.app.state.scheduler
    register(client, cpu=2, datasets=["fake-model"])
    response = client.post("/v1/chat/completions", headers=user_headers(), json=chat())
    assert response.status_code == 503
    assert response.headers["retry-after"] == "10"
    assert response.json()["error"]["type"] == "model_not_ready"
    assert service.list_models()[0]["state"] == "starting"


def test_a_failed_model_answers_503_until_its_retry_time(make_client, clock):
    client = make_client()
    service = client.app.state.scheduler
    instance = register(client, cpu=2, datasets=["fake-model"])
    job_id = service.ensure_model("fake")["job_id"]
    stopped(client, service, job_id, instance, "FAILED", "exited 3")
    clock[0] += 20
    response = client.post("/v1/completions", headers=user_headers(), json=chat())
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "model_failed"
    assert response.headers["retry-after"] == "100"


def test_a_worker_error_before_the_head_is_a_502(make_client):
    client = make_client()
    service = client.app.state.scheduler
    stack, worker, _ = connected(client, service)
    with stack:
        join = in_thread(lambda: client.post("/v1/embeddings", headers=user_headers(), json=chat()))
        request, _ = receive_request(worker)
        worker.send_json({"type": "infer_end", "id": request["id"], "error": "connection refused"})
        response = join()
    assert response.status_code == 502
    assert "connection refused" in response.json()["error"]["message"]
    assert service.list_models()[0]["inflight"] == 0


def test_losing_the_worker_mid_response_aborts_the_body_and_ends_the_request(make_client):
    client = make_client()
    service = client.app.state.scheduler
    stack, worker, _ = connected(client, service)
    join = in_thread(
        lambda: client.post("/v1/chat/completions", headers=user_headers(), json=chat(True))
    )
    request, _ = receive_request(worker)
    rid = request["id"]
    worker.send_json({"type": "infer_head", "id": rid, "status": 200, "content_type": "text/plain"})
    worker.send_json({"type": "infer_chunk", "id": rid, "data": encode(b"partial")})
    stack.close()  # The Worker's connection drops before the end.
    # The body is aborted rather than ended like a complete one. The TestClient raises the
    # server's error here; what a real client sees is in the test below.
    error = join()
    assert isinstance(error, TruncatedResponse)
    assert str(error) == "model fake: lost the connection to vm-a"
    assert service.list_models()[0]["inflight"] == 0


class LiveWorker:
    """A fake Worker on a real WebSocket, with the JSON methods of the TestClient's session."""

    def __init__(self, connection):
        self.connection = connection

    def receive_json(self):
        return json.loads(self.connection.recv(timeout=10))

    def send_json(self, message):
        self.connection.send(json.dumps(message))


@pytest.fixture
def live_gateway(tmp_path):
    """The Scheduler served by a real uvicorn (no Docker), to see what a client gets on the
    wire; the TestClient raises server errors instead."""
    settings = Settings(
        data_dir=tmp_path / "live", users={"alice": "alice-token"}, models={"fake": FAKE}
    )
    app = create_app(settings, run_loop=False)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    wait_for(lambda: server.started)
    address = f"127.0.0.1:{sock.getsockname()[1]}"
    try:
        with httpx.Client(base_url=f"http://{address}", trust_env=False, timeout=10) as http:
            yield app, http, address
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive()


def test_a_real_client_sees_a_cut_off_response_as_incomplete(live_gateway):
    """A complete response ends normally; one whose Worker drops mid-stream makes the client
    raise instead of returning what looks like a complete answer."""
    app, http, address = live_gateway
    service = app.state.scheduler
    instance = register(http, cpu=2, datasets=["fake-model"])

    def ask():
        return http.post("/v1/chat/completions", headers=user_headers(), json=chat(True))

    def relay(worker, piece, end):
        request, _ = receive_request(worker)
        rid = request["id"]
        head = {"type": "infer_head", "id": rid, "status": 200, "content_type": "text/event-stream"}
        worker.send_json(head)
        worker.send_json({"type": "infer_chunk", "id": rid, "data": encode(piece)})
        if end:
            worker.send_json({"type": "infer_end", "id": rid, "error": None})

    url = f"ws://{address}{socket_url(instance)}"
    with connect(url, additional_headers=worker_headers()) as connection:
        worker = LiveWorker(connection)
        wait_for(lambda: "vm-a" in app.state.hub.workers)
        report(http, service, service.ensure_model("fake")["job_id"], instance)
        join = in_thread(ask)
        relay(worker, b"data: one\n\ndata: [DONE]\n\n", end=True)
        response = join()
        assert (response.status_code, response.text) == (200, "data: one\n\ndata: [DONE]\n\n")
        join = in_thread(ask)
        relay(worker, b"data: one\n\n", end=False)
    # The Worker's connection dropped before the second response ended.
    error = join()
    assert isinstance(error, httpx.RemoteProtocolError), repr(error)
    assert "incomplete" in str(error)
    wait_for(lambda: service.list_models()[0]["inflight"] == 0)
