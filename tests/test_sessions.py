"""M9 sessions on the Scheduler: job rows of kind SESSION, the interactive queue, leases."""

from contextlib import ExitStack

import pytest
from conftest import finish, heartbeat, register, submit, user_headers
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from labflux.api import create_app
from labflux.config import Settings
from labflux.database import Database, commands, jobs, schema_version
from labflux.models import JobSpec
from labflux.policy import clock_text
from labflux.scheduler import Scheduler, ServiceError
from labflux.session_protocol import KEEPALIVE_ARGV, SESSION_ENV


@pytest.fixture
def make_client(tmp_path, clock):
    with ExitStack() as stack:

        def build(name="default", **settings):
            values = Settings(
                data_dir=tmp_path / name,
                users={"alice": "alice-token", "bob": "bob-token"},
                **settings,
            )
            app = create_app(values, run_loop=False, clock=lambda: clock[0])
            return stack.enter_context(TestClient(app))

        yield build


def open_session(client, user="alice", status=200, **body):
    response = client.post("/api/sessions", headers=user_headers(user), json=body)
    assert response.status_code == status, response.text
    return response.json()


def current(client, user="alice"):
    return client.get("/api/sessions/current", headers=user_headers(user))


def job(client, job_id, user="alice"):
    return client.get(f"/api/jobs/{job_id}", headers=user_headers(user)).json()


def start(client, session, instance):
    """Report the session container as started, as the Worker's outbox would."""
    attempt = job(client, session["id"])["attempts"][-1]
    response = client.post(
        f"/internal/workers/{attempt['node_id']}/attempts/{attempt['id']}/events",
        headers={"Authorization": "Bearer dev-worker-token"},
        json={"event_id": f"start-{attempt['id']}", "instance_id": instance, "kind": "STARTED"},
    )
    assert response.status_code == 200, response.text


def stop_commands(service):
    with service.db.transaction() as connection:
        return [
            row._mapping["id"]
            for row in connection.execute(select(commands).where(commands.c.kind == "STOP"))
        ]


def test_open_creates_a_session_job_with_a_keepalive_start(client, service):
    instance = register(client, cpu=2)
    session = open_session(client, resources={"cpu": 1, "memory_mib": 512})
    assert session["created"] is True
    assert session["state"] == "STARTING"
    assert session["node_id"] == "vm-a"
    assert session["mode"] == "session"
    assert session["image"] == service.settings.default_image
    assert session["lease_expires_at"] is None  # No lease until it is ready.
    [command] = service.poll_commands("vm-a", instance)
    payload = command["payload"]
    assert payload["session"] is True
    assert payload["job"]["argv"] == list(KEEPALIVE_ARGV)
    assert payload["job"]["env"] == SESSION_ENV
    assert "PATH" not in SESSION_ENV  # The Worker sets it from the image.
    assert payload["job"]["time_limit_s"] == service.settings.session_lease_s
    assert payload["job"]["retries_on_node_failure"] == 0
    listed = client.get("/api/jobs", headers=user_headers()).json()
    assert [(item["id"], item["kind"]) for item in listed] == [(session["id"], "SESSION")]
    assert submit(client)["kind"] == "BATCH"


@pytest.mark.parametrize("policy", ["fcfs", "priority", "backfill"])
def test_a_session_goes_ahead_of_an_earlier_batch_job(make_client, clock, policy):
    client = make_client(policy, scheduling_policy=policy)
    instance = register(client, cpu=2)
    running = submit(client, cpu=2, time_limit_s=600)
    batch = submit(client, cpu=2, priority=9)
    session = open_session(client, user="bob", resources={"cpu": 1})
    assert job(client, session["id"], "bob")["state"] == "QUEUED"
    clock[0] += 5
    heartbeat(client, "vm-a", instance)
    assert finish(client, job(client, running["id"]), instance).status_code == 200
    started = job(client, session["id"], "bob")
    assert started["state"] == "STARTING"
    assert "interactive session" in started["schedule_note"]
    assert job(client, batch["id"])["state"] == "QUEUED"  # One CPU left; it needs two.


def test_a_waiting_session_blocks_batch_under_fcfs_and_is_reserved_under_backfill(
    make_client, clock
):
    client = make_client("fcfs")
    register(client, cpu=2)
    submit(client, cpu=2, time_limit_s=600)
    session = open_session(client, resources={"cpu": 1})
    later = submit(client, cpu=1, user="bob")
    assert job(client, session["id"])["state"] == "QUEUED"
    assert (
        f"waiting behind job {session['id'][:8]}"
        in job(client, later["id"], "bob")["schedule_note"]
    )

    client = make_client("backfill", scheduling_policy="backfill")
    register(client, cpu=3)
    submit(client, cpu=2, time_limit_s=600)  # Holds two of three CPUs until t=1600.
    session = open_session(client, resources={"cpu": 3})
    note = job(client, session["id"])["schedule_note"]
    assert "first in line (interactive session" in note
    assert note.endswith(f"reserved on vm-a at {clock_text(1600)}")
    long = submit(client, cpu=1, user="bob", time_limit_s=3600)
    assert job(client, long["id"], "bob")["state"] == "QUEUED"
    assert "would delay job" in job(client, long["id"], "bob")["schedule_note"]


def test_backfill_books_a_session_until_its_renewed_lease(make_client, clock):
    client = make_client("backfill", scheduling_policy="backfill")
    service = client.app.state.scheduler
    instance = register(client, cpu=8, memory=8192)
    session = open_session(client, resources={"cpu": 3})
    start(client, session, instance)  # Lease until t=1600.
    clock[0] += 500
    service.begin_command("alice", session["id"])
    service.end_command(session["id"])  # Lease until t=2100.
    clock[0] += 200  # t=1700: past the first lease, and the session still holds 3 CPUs.
    heartbeat(client, "vm-a", instance)
    submit(client, cpu=2, time_limit_s=100)  # Holds 2 CPUs until t=1800.
    head = submit(client, cpu=4, user="bob")
    filler = submit(client, cpu=2, time_limit_s=3600)
    # The head fits at t=1800 next to the session, unless the filler takes 2 of those 5 CPUs.
    note = job(client, head["id"], "bob")["schedule_note"]
    assert note.endswith(f"reserved on vm-a at {clock_text(1800)}")
    assert job(client, filler["id"])["state"] == "QUEUED"
    assert "would delay job" in job(client, filler["id"])["schedule_note"]


def test_a_queued_session_is_cancelled_once_its_client_stops_polling(client, service, clock):
    instance = register(client, cpu=1)
    running = submit(client, cpu=1, time_limit_s=3600)
    session = open_session(client, resources={"cpu": 1})
    batch = submit(client, cpu=1, user="bob")
    assert "waiting behind job" in job(client, batch["id"], "bob")["schedule_note"]
    timeout = service.settings.session_queue_timeout_s
    for _ in range(3):  # A waiting client keeps it queued for as long as it polls.
        clock[0] += timeout - 1
        heartbeat(client, "vm-a", instance)
        assert current(client).json()["state"] == "QUEUED"
        service.tick()
    assert job(client, session["id"])["state"] == "QUEUED"
    clock[0] += timeout + 1
    heartbeat(client, "vm-a", instance)
    service.tick()
    abandoned = job(client, session["id"])
    assert abandoned["state"] == "CANCELLED"
    assert abandoned["reason"] == "cancelled while queued: its client stopped waiting for it"
    assert abandoned["attempts"] == []
    assert current(client).status_code == 404
    assert service.session_polled == {}
    # It no longer holds the head of the queue.
    assert "first in line" in job(client, batch["id"], "bob")["schedule_note"]
    assert finish(client, job(client, running["id"]), instance).status_code == 200
    assert job(client, batch["id"], "bob")["state"] == "STARTING"


def test_a_queued_session_gets_a_full_window_after_a_scheduler_restart(make_client, clock):
    session = open_session(make_client())  # No worker: it stays queued.
    clock[0] += 3600
    # A second Scheduler over the same data directory.
    client = make_client(session_queue_timeout_s=5)
    service = client.app.state.scheduler
    service.tick()
    assert job(client, session["id"])["state"] == "QUEUED"
    clock[0] += 6
    service.tick()
    assert job(client, session["id"])["state"] == "CANCELLED"


def test_reuse_conflicts_and_one_session_per_user(client, service):
    register(client, cpu=4, memory=4096, datasets=["mnist"])
    first = open_session(client, resources={"cpu": 1})
    again = open_session(client, resources={"cpu": 1}, image=service.settings.default_image)
    assert again["id"] == first["id"] and again["created"] is False
    for body in (
        {"resources": {"cpu": 2}},
        {"resources": {"cpu": 1}, "image": "python:3.12-slim"},
        {"resources": {"cpu": 1}, "datasets": ["mnist"]},
    ):
        conflict = client.post("/api/sessions", headers=user_headers(), json=body)
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == (
            f"you already have session {first['id'][:8]} (cpu=1, memory_mib=256, gpu=0); "
            "close it with `labflux session close`"
        )
    other = open_session(client, user="bob", resources={"cpu": 1})
    assert other["created"] is True and other["id"] != first["id"]
    bad = client.post("/api/sessions", headers=user_headers("bob"), json={"datasets": ["../x"]})
    assert bad.status_code == 422


def test_started_sets_the_lease_and_commands_renew_it(client, service, clock):
    instance = register(client, cpu=2)
    session = open_session(client)
    start(client, session, instance)
    view = current(client).json()
    assert view["state"] == "RUNNING"
    assert view["lease_expires_at"] == 1000 + 600
    clock[0] += 100
    attempt_id, node_id = service.begin_command("alice", session["id"])
    assert (attempt_id, node_id) == (job(client, session["id"])["current_attempt_id"], "vm-a")
    assert current(client).json()["lease_expires_at"] == 1100 + 600
    clock[0] += 50
    service.end_command(session["id"])
    assert current(client).json()["lease_expires_at"] == 1150 + 600


def test_an_idle_lease_expires_but_a_busy_session_does_not(client, service, clock):
    instance = register(client, cpu=2)
    session = open_session(client, resources={"cpu": 1})
    start(client, session, instance)
    clock[0] += 601
    heartbeat(client, "vm-a", instance)
    service.session_busy.add(session["id"])
    service.tick()
    assert current(client).json()["state"] == "RUNNING"
    service.session_busy.discard(session["id"])
    rings = []
    service.doorbell = rings.append
    service.tick()
    view = current(client).json()
    assert view["state"] == "STOPPING"
    assert view["reason"].startswith("session lease expired")
    attempt_id = job(client, session["id"])["current_attempt_id"]
    assert stop_commands(service) == [f"stop-{attempt_id}"]
    assert rings == ["vm-a"]
    # Resources stay held until the Worker confirms termination.
    assert service.list_nodes()[0]["allocated"]["cpu"] == 1
    response = finish(client, job(client, session["id"]), instance, outcome="CANCELLED")
    assert response.status_code == 200
    assert service.list_nodes()[0]["allocated"]["cpu"] == 0
    assert job(client, session["id"])["state"] == "CANCELLED"
    assert current(client).status_code == 404


def test_close_a_queued_or_running_session(client, service):
    assert client.post("/api/sessions/current/close", headers=user_headers()).status_code == 404
    assert current(client).status_code == 404
    queued = open_session(client)  # No worker yet.
    closed = client.post("/api/sessions/current/close", headers=user_headers()).json()
    assert (closed["id"], closed["state"]) == (queued["id"], "CANCELLED")
    instance = register(client, cpu=2)
    running = open_session(client)
    start(client, running, instance)
    closed = client.post("/api/sessions/current/close", headers=user_headers()).json()
    assert (closed["id"], closed["state"]) == (running["id"], "STOPPING")
    attempt_id = job(client, running["id"])["current_attempt_id"]
    assert stop_commands(service) == [f"stop-{attempt_id}"]
    # A stopping session no longer counts toward the limit.
    assert open_session(client)["created"] is True


def test_an_unknown_session_keeps_its_resources_and_is_stopped_by_a_new_one(client, service, clock):
    instance = register(client, cpu=2)
    lost = open_session(client, resources={"cpu": 1})
    start(client, lost, instance)
    clock[0] += 30
    service.tick()
    assert current(client).json()["state"] == "UNKNOWN"
    assert service.list_nodes()[0]["allocated"]["cpu"] == 1
    register(client, name="vm-b", cpu=1)
    fresh = open_session(client, resources={"cpu": 1})
    assert fresh["created"] is True and fresh["node_id"] == "vm-b"
    old = job(client, lost["id"])
    assert old["state"] == "STOPPING"
    assert old["reason"].startswith("replaced by a new session")
    assert stop_commands(service) == [f"stop-{old['current_attempt_id']}"]
    assert {n["id"]: n["allocated"]["cpu"] for n in service.list_nodes()} == {
        "vm-a": 1,
        "vm-b": 1,
    }
    assert current(client).json()["id"] == fresh["id"]


def test_oneshot_mode_closes_the_session_after_each_command(make_client):
    client = make_client(fast_lane_mode="oneshot")
    service = client.app.state.scheduler
    instance = register(client, cpu=2)
    session = open_session(client)
    assert session["mode"] == "oneshot"
    start(client, session, instance)
    service.begin_command("alice", session["id"])
    service.end_command(session["id"])
    view = current(client).json()
    assert view["state"] == "STOPPING"
    assert view["reason"].startswith("one-shot mode")


def test_sessions_belong_to_their_owner(client, service):
    instance = register(client, cpu=2)
    session = open_session(client)
    start(client, session, instance)
    assert current(client, "bob").status_code == 404
    assert client.post("/api/sessions/current/close", headers=user_headers("bob")).status_code == (
        404
    )
    with pytest.raises(ServiceError) as denied:
        service.begin_command("bob", session["id"])
    assert denied.value.status == 404
    batch = submit(client)
    with pytest.raises(ServiceError) as batch_job:
        service.begin_command("alice", batch["id"])
    assert batch_job.value.status == 404
    unauthenticated = client.post("/api/sessions", json={})
    assert unauthenticated.status_code == 401


def test_begin_command_requires_a_ready_session(client, service):
    queued = open_session(client)
    with pytest.raises(ServiceError) as waiting:
        service.begin_command("alice", queued["id"])
    assert waiting.value.status == 409


def test_a_version_3_database_migrates_and_existing_jobs_are_batch(settings):
    first = Scheduler(settings)
    job_id = first.submit("alice", JobSpec(image="python:3.12-slim", argv=["true"]))["id"]
    with first.db.transaction() as connection:
        connection.exec_driver_sql("ALTER TABLE jobs DROP COLUMN kind")
        connection.exec_driver_sql("ALTER TABLE jobs DROP COLUMN lease_expires_at")
        connection.exec_driver_sql("ALTER TABLE jobs DROP COLUMN service")  # Version 5 (M10).
        connection.exec_driver_sql("ALTER TABLE attempts DROP COLUMN preempted_by")  # Version 6.
        connection.execute(update(schema_version).values(version=3))
    first.db.close()
    database = Database(settings.data_dir)
    with database.transaction() as connection:
        row = connection.execute(select(jobs).where(jobs.c.id == job_id)).one()
    assert (row.kind, row.lease_expires_at, row.service) == ("BATCH", None, None)
    database.close()
