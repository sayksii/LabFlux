"""M11 on top of M10, at the Scheduler API level.

A blocked head job does not preempt for resources that are already on their way back: an
attempt stopping for a cancel, a session end or a model stop, or an idle on-demand model that M10
reclaims as soon as a job waits. Queued model services may preempt preemptible batch jobs under
every policy, like sessions, and are never chosen as victims.
"""

from contextlib import ExitStack

import pytest
from conftest import register, submit, user_headers
from fastapi.testclient import TestClient
from test_model_services import FAKE, model_jobs, report
from test_model_services import stopped as model_stopped
from test_preemption import (
    advance,
    high,
    job,
    low,
    open_session,
    run,
    short,
    stopped,
    stops,
)

from labflux.api import create_app
from labflux.config import Settings


@pytest.fixture
def make_client(tmp_path, clock):
    """A Scheduler API client with the named policy and model services; all share the clock."""
    with ExitStack() as stack:
        built = []

        def build(policy, models=None, **settings):
            values = Settings(
                data_dir=tmp_path / f"{policy}-{len(built)}",
                users={"alice": "alice-token", "bob": "bob-token"},
                scheduling_policy=policy,
                models=models or {},
                **settings,
            )
            app = create_app(values, run_loop=False, clock=lambda: clock[0])
            built.append(app)
            return stack.enter_context(TestClient(app))

        yield build


def serve(client, instance):
    """A request starts the on-demand model; its Worker reports it healthy, so it serves."""
    service = client.app.state.scheduler
    report(client, service, service.ensure_model("fake")["job_id"], instance)
    view = service.ensure_model("fake")
    assert view["state"] == "serving"
    return view


def cancel(client, value):
    response = client.post(f"/api/jobs/{value['id']}/cancel", headers=user_headers(value["owner"]))
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "STOPPING"


# Resources already on their way back (review finding 1) ---------------------------------------


@pytest.mark.parametrize("ending", ["cancel", "session end"])
def test_head_waits_for_a_stop_in_progress_instead_of_preempting(make_client, clock, ending):
    """Bob cancels his job and submits it again at once, or his session ends and the next
    command opens a new one: what the old one held is already being released, so the hour of
    work of alice's preemptible job is not thrown away for it."""
    client = make_client("priority")
    instance = register(client, "kayle", cpu=2)
    victim = run(client, low(client, cpu=1), instance)
    if ending == "cancel":
        old = run(client, high(client, cpu=1, time_limit_s=86400), instance)
        cancel(client, old)
    else:
        old = run(client, open_session(client, resources={"cpu": 1}), instance)
        response = client.post("/api/sessions/current/close", headers=user_headers("bob"))
        assert response.status_code == 200, response.text
        assert job(client, old)["state"] == "STOPPING"
    advance(client, clock, 3600, {"kayle": instance})
    new = high(client, cpu=1) if ending == "cancel" else open_session(client, resources={"cpu": 1})
    assert stops(client, instance) == [old["current_attempt_id"]]  # Only its own stop.
    assert job(client, victim)["state"] == "RUNNING"
    waiting = job(client, new)
    assert waiting["state"] == "QUEUED" and waiting["attempts"] == []
    assert f"waiting for job {short(old)} on kayle to stop" in waiting["schedule_note"]
    stopped(client, old, instance)
    assert job(client, new)["state"] == "STARTING"
    assert job(client, victim)["state"] == "RUNNING"


def test_a_stop_in_progress_that_is_not_enough_means_fewer_victims(make_client, clock):
    client = make_client("priority")
    instance = register(client, "kayle", cpu=3)
    nodes = {"kayle": instance}
    older = run(client, low(client, cpu=1), instance)
    advance(client, clock, 600, nodes)
    newer = run(client, low(client, cpu=1), instance)
    old = run(client, high(client, cpu=1, time_limit_s=86400), instance)
    cancel(client, old)
    advance(client, clock, 60, nodes)
    head = high(client, cpu=2)
    # The cancelled job frees one CPU; one victim, the one with less lost work, frees the other.
    assert sorted(stops(client, instance)) == sorted(
        [old["current_attempt_id"], newer["current_attempt_id"]]
    )
    assert job(client, older)["state"] == "RUNNING"
    assert f"preempted job {short(newer)} on kayle" in job(client, head)["schedule_note"]
    stopped(client, newer, instance)
    waiting = job(client, head)
    assert waiting["state"] == "QUEUED"
    assert f"waiting for job {short(old)} on kayle to stop" in waiting["schedule_note"]
    assert stops(client, instance) == [old["current_attempt_id"]]
    stopped(client, old, instance)
    assert job(client, head)["state"] == "STARTING"
    assert job(client, older)["state"] == "RUNNING"


def test_a_stop_on_an_unavailable_node_does_not_hold_back_preemption_elsewhere(make_client, clock):
    client = make_client("priority", models={"fake": FAKE})
    service = client.app.state.scheduler
    nodes = {
        "karma": register(client, "karma", cpu=1, datasets=["fake-model"]),
        "kayle": register(client, "kayle", cpu=1),
    }
    serve(client, nodes["karma"])  # karma is the only node with the weights.
    victim = run(client, low(client, cpu=1), nodes["kayle"])
    advance(client, clock, 301, nodes)
    service.tick()  # Idle for idle_stop_s: M10 stops the model.
    assert model_jobs(service)[0]["state"] == "STOPPING"
    # karma's heartbeats stop before the stop is confirmed. No tick has marked it OFFLINE yet,
    # so its attempt is still STOPPING, but karma is no longer a node anything can start on.
    advance(client, clock, service.settings.offline_timeout_s + 1, {"kayle": nodes["kayle"]})
    head = high(client, cpu=1)
    assert stops(client, nodes["kayle"], "kayle") == [victim["current_attempt_id"]]
    assert f"preempted job {short(victim)} on kayle" in job(client, head)["schedule_note"]


@pytest.mark.parametrize("idle_s, reclaimed", [(61, True), (30, False)])
def test_head_waits_for_an_idle_model_to_be_reclaimed_instead_of_preempting(
    make_client, clock, idle_s, reclaimed
):
    """Plan 11.2: M10 stops an on-demand model idle for reclaim_idle_s (60 s) as soon as a job
    waits, so the head waits for it. A model idle for less keeps serving, and the head preempts."""
    client = make_client("priority", models={"fake": FAKE})
    service = client.app.state.scheduler
    instance = register(client, "kayle", cpu=2, datasets=["fake-model"])
    model = serve(client, instance)
    victim = run(client, low(client, cpu=1), instance)
    advance(client, clock, idle_s, {"kayle": instance})
    head = high(client, cpu=1)
    if not reclaimed:
        assert stops(client, instance) == [victim["current_attempt_id"]]
        return
    assert stops(client, instance) == []
    waiting = job(client, head)
    assert waiting["state"] == "QUEUED"
    assert "waiting for idle model fake on kayle to be reclaimed" in waiting["schedule_note"]
    service.tick()  # M10's reclaim, because a job waits.
    [served] = model_jobs(service)
    assert served["state"] == "STOPPING"
    assert "while other jobs wait for resources" in served["reason"]
    assert stops(client, instance) == [model["attempt_id"]]
    assert "waiting for model fake on kayle to stop" in job(client, head)["schedule_note"]
    model_stopped(client, service, served["id"], instance)
    assert job(client, head)["state"] == "STARTING"
    assert job(client, victim)["state"] == "RUNNING"


# Model services as preemptors (plan 11.4, 11.8) ------------------------------------------------


@pytest.mark.parametrize("policy", ["fcfs", "priority", "backfill"])
def test_a_queued_model_preempts_a_preemptible_batch_job_and_is_never_a_victim(
    make_client, clock, policy
):
    client = make_client(policy, models={"fake": FAKE})
    service = client.app.state.scheduler
    instance = register(client, "kayle", cpu=1, datasets=["fake-model"])
    nodes = {"kayle": instance}
    victim = run(client, low(client, cpu=1, priority=9), instance)
    advance(client, clock, 60, nodes)
    assert service.ensure_model("fake")["state"] == "waiting"  # A cold request.
    [model] = model_jobs(service)
    value = job(client, victim)
    assert value["state"] == "STOPPING" and not value["cancel_requested"]
    assert f"preempted by job {model['id'][:8]}" in value["reason"]
    assert stops(client, instance) == [value["current_attempt_id"]]
    assert f"preempted job {short(victim)} on kayle" in model["schedule_note"]
    stopped(client, victim, instance)
    value = job(client, victim)
    assert value["state"] == "QUEUED" and "1 of 3" in value["reason"]
    assert service.ensure_model("fake")["state"] == "starting"
    report(client, service, model["id"], instance)
    assert service.ensure_model("fake")["state"] == "serving"
    # Neither a session nor a higher-priority batch job stops the serving model.
    session = open_session(client, resources={"cpu": 1})
    high(client, cpu=1)
    advance(client, clock, 5, nodes)
    assert stops(client, instance) == []
    assert model_jobs(service)[0]["state"] == "RUNNING"
    assert job(client, session)["state"] == "QUEUED"


def test_a_persistent_model_start_may_preempt(make_client, clock):
    client = make_client("fcfs", models={"fake": {**FAKE, "mode": "persistent"}})
    service = client.app.state.scheduler
    instance = register(client, "kayle", cpu=1, datasets=["fake-model"])
    victim = run(client, low(client, cpu=1), instance)
    advance(client, clock, 60, {"kayle": instance})
    service.tick()  # Queues the persistent model's service, which needs the whole node.
    assert model_jobs(service)[0]["state"] == "QUEUED"
    assert stops(client, instance) == [victim["current_attempt_id"]]
    stopped(client, victim, instance)
    assert model_jobs(service)[0]["state"] == "STARTING"
    assert job(client, victim)["state"] == "QUEUED"


def test_when_a_blocked_model_cannot_preempt_the_next_blocked_job_may(make_client, clock):
    client = make_client("priority", models={"fake": FAKE})
    service = client.app.state.scheduler
    nodes = {
        "karma": register(client, "karma", cpu=1, datasets=["fake-model"]),
        "kayle": register(client, "kayle", cpu=1),
    }
    # karma, the only node with the weights, runs a job its owner did not mark preemptible.
    pinned = submit(client, cpu=1, datasets=["fake-model"], time_limit_s=86400)
    pinned = run(client, pinned, nodes["karma"])
    victim = run(client, low(client, cpu=1), nodes["kayle"])
    advance(client, clock, 60, nodes)
    assert service.ensure_model("fake")["state"] == "waiting"
    assert stops(client, nodes["karma"], "karma") == []
    assert stops(client, nodes["kayle"], "kayle") == []
    head = high(client, cpu=1)
    assert stops(client, nodes["kayle"], "kayle") == [victim["current_attempt_id"]]
    assert stops(client, nodes["karma"], "karma") == []
    assert job(client, pinned)["state"] == "RUNNING"
    stopped(client, victim, nodes["kayle"])
    assert job(client, head)["state"] == "STARTING"
    assert model_jobs(service)[0]["state"] == "QUEUED"
