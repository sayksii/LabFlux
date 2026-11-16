"""M12 acceptance: storyboard scene 6, the on-demand model gemma4-31b on gamma's two A40s.

The demo runs gemma4-31b in mode "on_demand" so that the audience sees its cold start, which
takes 4-5 minutes on two A40s. Like tests/test_storyboard.py, each test plays the scene under
the backfill policy on the three lab-shaped nodes, with a fake clock and fake Workers, and checks
what the Dashboard shows: GET /api/models (the model table), GET /api/queue (queue entries and
recent decisions) and the allocations in GET /api/nodes (the MODEL block on gamma).

No Gateway runs here. A request that reaches the Gateway calls ensure_model, again every few
seconds while the model is not serving (up to cold_wait_s); once it serves, the Gateway relays
the request between begin_request and end_request. The Worker reports STARTED only when the
model server's health check passes, so the tests hold Lab.work() back for the startup time.
"""

import pytest
from conftest import user_headers
from fastapi.testclient import TestClient
from test_storyboard import (
    A40S,
    GEMMA,
    PREEMPTIBLE,
    T0,
    Lab,
    assert_running,
    decisions,
    entry,
    order,
    pick,
)

from labflux.api import create_app
from labflux.config import Settings
from labflux.models import MODEL_OWNER

NAME = "gemma4-31b"
# The demo's timings. startup_timeout_s and cold_wait_s cover the 4-5 minute cold start, so the
# first request waits for it instead of getting a 503. While any job waits, a minute without
# requests releases the model (reclaim_idle_s); otherwise it stays for an hour (idle_stop_s),
# so that during the other scenes its backfill booking ends after their reservations, as a
# persistent model's would.
DEMO = {"startup_timeout_s": 600, "cold_wait_s": 600, "idle_stop_s": 3600, "reclaim_idle_s": 60}
ON_DEMAND = {**GEMMA, "mode": "on_demand", **DEMO}
STARTUP_S = 270  # 4.5 minutes from dispatch to a passing health check
A40 = {"cpu": 4, "memory_gib": 8, "gpu": 1, "gpu_model": "A40"}
A40_BLOCKER = {
    "kind": "gpu",
    "need": 1,
    "model": "A40",
    "min_vram_mib": None,
    "best_node": "gamma",
    "free": 0,
    "held": 2,
    "external": 0,
    "unreported": 0,
}


@pytest.fixture
def lab(tmp_path):
    clock = [T0]
    settings = Settings(
        data_dir=tmp_path,
        users={"alice": "alice-token", "bob": "bob-token"},
        scheduling_policy="backfill",
        models={NAME: ON_DEMAND},
    )
    app = create_app(settings, run_loop=False, clock=lambda: clock[0])
    with TestClient(app) as client:
        yield Lab(client, clock)


def model(lab, viewer="alice"):
    """gemma4-31b's row in the model table."""
    response = lab.client.get("/api/models", headers=user_headers(viewer))
    assert response.status_code == 200, response.text
    [view] = response.json()
    return view


def ask(lab):
    """The Gateway's call for a request to gemma4-31b: starts it if needed, else polls it."""
    return lab.service.ensure_model(NAME)


def model_block(lab, viewer="alice"):
    """The MODEL allocation on gamma, or None."""
    blocks = [item for item in lab.allocations("gamma", viewer).values() if item["kind"] == "MODEL"]
    return blocks[0] if blocks else None


def load(lab, seconds=STARTUP_S):
    """The model server loads the weights; the Gateway polls every 10 s and heartbeats go on."""
    for _ in range(int(seconds // 10)):
        lab.advance(10)
        ask(lab)
    lab.advance(seconds % 10)


def cold_start(lab):
    """A request starts gemma4-31b on gamma, which serves after its startup; returns its job."""
    view = ask(lab)
    assert (view["state"], view["node_id"]) == ("starting", "gamma"), view
    load(lab)
    lab.work()  # The health check passes: the Worker reports STARTED.
    view = model(lab)
    assert view["state"] == "serving", view
    return view["job_id"]


def chat(lab, seconds=2):
    """One relayed request that takes `seconds`; returns when it ended."""
    view = ask(lab)
    assert view["state"] == "serving", view
    lab.service.begin_request(view["job_id"])
    lab.advance(seconds)
    lab.service.end_request(view["job_id"])
    return lab.clock[0]


def notebook(lab, minutes):
    """bob's notebook sends a short request every 20 seconds; returns when the last one ended."""
    for _ in range(minutes * 3):
        ended = chat(lab)
        lab.advance(18)
    return ended


def test_scene_6a_a_cold_start_on_gamma_then_alice_and_bob_share_the_service(lab):
    """Scene 6 (a), cold start and sharing: gemma4-31b is stopped; bob sends a chat request,
    and alice sends one while it loads.

    The audience sees the model table go from "stopped" to "starting" on gamma at once (gamma's
    A40s are free, so it never waits in the queue; the decision feed says it started on gamma,
    the only node with the weights and two A40s), the MODEL block on gamma's two A40 cards for
    the 4-5 minutes of loading, then "serving": both requests are relayed to the same service
    ("2 in progress", served 2), and when they end the model shows how long it has been idle and
    when it will be released. Both users see the same row and the same block. The queue shows a
    MODEL entry only while the model waits for resources (scene 6 (c)).
    """
    view = model(lab)
    want = {
        "mode": "on_demand",
        "state": "stopped",
        "job_id": None,
        "node_id": None,
        "inflight": 0,
        "served": 0,
        "last_activity_at": None,
        "idle_stop_at": None,
    }
    assert pick(view, want) == want
    assert model_block(lab) is None

    # bob's request starts the model; it is dispatched in the same scheduling pass.
    started = ask(lab)
    job_id = started["job_id"]
    assert (started["state"], started["node_id"]) == ("starting", "gamma")
    queue = lab.queue("bob")
    assert queue["entries"] == []
    [told] = [item for item in decisions(queue, "started") if item["job_id"] == job_id]
    assert (told["owner"], told["node_id"]) == (MODEL_OWNER, "gamma")
    assert told["text"].startswith(f"model {NAME}: backfill: started on gamma (")
    assert told["text"].endswith("only gamma fits")
    # The Dashboard names the block and the decision after the service.
    summaries = lab.client.get("/api/jobs?scope=all", headers=user_headers("bob")).json()
    [summary] = [item for item in summaries if item["id"] == job_id]
    want = {"owner": MODEL_OWNER, "kind": "MODEL", "service": NAME, "node_id": "gamma"}
    assert pick(summary, want) == want
    want = {
        "owner": MODEL_OWNER,
        "mine": False,
        "kind": "MODEL",
        "service": NAME,
        "state": "STARTING",
        "attempt_state": "STARTING",
        "cpu": 8,
        "memory_mib": 49152,
        "created_at": T0,
        "started_at": None,
        "ends_at": T0 + 600,  # startup_timeout_s bounds the load in the backfill bookings
        "placement": "started",
        "preempted_by": None,
    }
    for viewer in ("alice", "bob"):
        block = model_block(lab, viewer)
        assert pick(block, want) == want
        assert sorted(block["gpu_uuids"]) == [gpu["uuid"] for gpu in A40S]

    # alice's request while it loads joins the same service.
    lab.advance(30)
    assert ask(lab)["job_id"] == job_id
    load(lab, STARTUP_S - 30)
    view = model(lab)
    assert (view["state"], view["job_id"], view["node_id"]) == ("starting", job_id, "gamma")

    lab.work()  # The health check passes after 4.5 minutes.
    healthy = lab.clock[0]
    assert healthy == T0 + STARTUP_S
    view = model(lab)
    want = {"state": "serving", "inflight": 0, "served": 0, "idle_stop_at": healthy + 3600}
    assert pick(view, want) == want
    block = model_block(lab)
    want = {"state": "RUNNING", "started_at": healthy, "ends_at": healthy + 3600}
    assert pick(block, want) == want

    # The Gateway relays both waiting requests to the one service.
    lab.service.begin_request(job_id)  # bob's
    lab.clock[0] += 1
    lab.service.begin_request(job_id)  # alice's
    view = model(lab, "bob")
    want = {"inflight": 2, "served": 2, "last_activity_at": healthy + 1}
    assert pick(view, want) == want
    listed = lab.client.get("/v1/models", headers=user_headers("bob")).json()["data"]
    assert [(item["id"], item["labflux"]["served"]) for item in listed] == [(NAME, 2)]

    lab.advance(20)
    lab.service.end_request(job_id)
    lab.advance(5)
    lab.service.end_request(job_id)
    ended = lab.clock[0]
    view = model(lab)
    want = {
        "state": "serving",
        "inflight": 0,
        "served": 2,
        "last_activity_at": ended,
        "idle_stop_at": ended + 3600,
    }
    assert pick(view, want) == want
    assert model_block(lab)["ends_at"] == ended + 3600


def test_scene_6b_requests_keep_the_model_while_a_job_waits_then_a_minute_idle_releases_it(lab):
    """Scene 6 (b), idle release with a job already waiting: while bob's notebook sends a
    request every 20 seconds, alice sends a GPU job that needs an A40; gemma4-31b holds both.

    The audience sees alice's job first in line, reserved on gamma for when the model would stop
    by itself, the model kept serving by bob's requests (served keeps growing); once bob stops,
    the model's idle time grows and 60 seconds later (reclaim_idle_s, because a job waits) the
    model is "stopping", alice's job "waiting for model gemma4-31b on gamma to stop", and her
    block lands on an A40 once the Worker confirms the stop; the model table shows "stopped",
    ready to start again on the next request.
    """
    job_id = cold_start(lab)
    chat(lab)
    waiting = lab.submit("alice", limit=1800, **A40)
    assert lab.job(waiting)["state"] == "QUEUED"
    ended = notebook(lab, minutes=5)
    view = model(lab)
    want = {"state": "serving", "served": 16, "last_activity_at": ended}
    assert pick(view, want) == want
    queue = lab.queue()
    assert order(queue) == [waiting["id"]]
    want = {"kind": "BATCH", "decision": "head_reserved", "waiting_for": [], "blocker": A40_BLOCKER}
    assert pick(entry(queue, waiting), want) == want
    assert queue["reservation"] == {
        "job_id": waiting["id"],
        "node_id": "gamma",
        "start": ended + 3600,  # when the idle model would stop by itself
        "cpu": 4,
        "memory_mib": 8192,
        "gpu": 1,
    }

    lab.advance(40)  # bob stopped: idle for 58 s
    assert model(lab)["state"] == "serving"
    lab.advance(3)  # idle for 61 s while a job waits: reclaimed
    view = model(lab)
    assert view["state"] == "stopping"
    assert view["reason"].startswith("idle for 61s while other jobs wait for resources")
    want = {"state": "STOPPING", "attempt_state": "STOPPING", "preempted_by": None}
    assert pick(model_block(lab), want) == want
    queue = lab.queue()
    want = {"decision": "waiting_for_release", "waiting_for": [job_id]}
    assert pick(entry(queue, waiting), want) == want
    assert f"waiting for model {NAME} on gamma to stop" in entry(queue, waiting)["note"]

    lab.settle()  # The Worker confirms the stop.
    assert_running(lab, waiting, "gamma", A40S[:1])
    view = model(lab)
    want = {"state": "stopped", "inflight": 0, "served": 16, "last_activity_at": ended}
    assert pick(view, want) == want
    assert model_block(lab) is None
    queue = lab.queue()
    assert queue["entries"] == []
    assert [item["node_id"] for item in decisions(queue, "started", waiting)] == ["gamma"]


def test_scene_6b_a_job_sent_after_a_minute_idle_releases_the_model_at_once(lab):
    """Scene 6 (b), idle release in the demo's order: bob's requests stop; with no job waiting
    the model keeps serving past reclaim_idle_s; alice then sends a GPU job that needs an A40.

    The audience sees alice's job explained at once as "waiting for idle model gemma4-31b on
    gamma to be reclaimed", the model "stopping" on the Scheduler's next pass, and her block on
    an A40 as soon as the Worker confirms the stop.
    """
    job_id = cold_start(lab)
    ended = notebook(lab, minutes=1)
    lab.advance(65 - (lab.clock[0] - ended))
    view = model(lab)
    want = {"state": "serving", "last_activity_at": ended, "idle_stop_at": ended + 3600}
    assert pick(view, want) == want

    waiting = lab.submit("alice", limit=1800, **A40)
    item = entry(lab.queue(), waiting)
    want = {"decision": "waiting_for_release", "waiting_for": [job_id], "blocker": A40_BLOCKER}
    assert pick(item, want) == want
    assert f"waiting for idle model {NAME} on gamma to be reclaimed" in item["note"]
    lab.service.tick()
    view = model(lab)
    assert view["state"] == "stopping"
    assert view["reason"].startswith("idle for 65s while other jobs wait for resources")
    item = entry(lab.queue(), waiting)
    assert f"waiting for model {NAME} on gamma to stop" in item["note"]
    lab.settle()  # The Worker confirms the stop.
    assert_running(lab, waiting, "gamma", A40S[:1])
    assert model(lab)["state"] == "stopped"


def test_scene_6c_a_cold_start_preempts_low_priority_a40_jobs(lab):
    """Scene 6 (c), cold start with preemption: alice's two preemptible priority 2 jobs hold
    gamma's A40s; bob sends a chat request.

    The audience sees gemma4-31b first in the queue as a MODEL entry "waiting for release",
    waiting for alice's two jobs and reserved on gamma, the decision feed "model gemma4-31b:
    preempting job ... on gamma", alice's blocks drawn as preempted and stopping; once the
    Worker confirms the stops the MODEL block takes both A40s and starts loading, alice's jobs
    return to the queue ("preemption 1 of 3"), and after the startup the model serves bob's
    request. When the model is then idle for a minute, alice's waiting jobs get the cards back.
    """
    sweep = []
    for _ in range(2):
        sweep.append(lab.submit("alice", priority=2, limit=86400, **A40, **PREEMPTIBLE))
        lab.settle()
        lab.advance(60)
    for value, gpu in zip(sweep, A40S):
        assert_running(lab, value, "gamma", [gpu])
    lab.advance(600)

    view = ask(lab)  # bob's request
    assert (view["state"], view["node_id"]) == ("waiting", None)
    job_id = view["job_id"]
    victims = {value["id"] for value in sweep}
    for value in sweep:
        job = lab.job(value)
        assert (job["state"], job["reason"]) == (
            "STOPPING",
            f"preempted by job {job_id[:8]}; waiting for confirmed container termination",
        )
    queue = lab.queue("bob")
    assert order(queue) == [job_id]
    want = {
        "position": 1,
        "owner": MODEL_OWNER,
        "mine": False,
        "kind": "MODEL",
        "service": NAME,
        "priority": None,
        "score": None,
        "score_parts": None,
        "decision": "waiting_for_release",
        "blocker": {**A40_BLOCKER, "need": 2},
    }
    item = entry(queue, {"id": job_id})
    assert pick(item, want) == want
    assert set(item["waiting_for"]) == victims
    assert item["note"].endswith("reserved on gamma as soon as they stop")
    want = {"job_id": job_id, "node_id": "gamma", "start": lab.clock[0], "gpu": 2}
    assert pick(queue["reservation"], want) == want
    [preempting] = decisions(queue, "preempting")
    assert (preempting["job_id"], preempting["owner"]) == (job_id, MODEL_OWNER)
    assert (set(preempting["detail"]["victims"]), preempting["detail"]["node_id"]) == (
        victims,
        "gamma",
    )
    assert preempting["text"].startswith(f"model {NAME}: preempting job ")
    blocks = lab.allocations("gamma")
    for value in sweep:
        want = {"owner": "alice", "state": "STOPPING", "preempted_by": job_id}
        assert pick(blocks[value["id"]], want) == want
    assert model(lab)["state"] == "waiting"
    assert ask(lab)["job_id"] == job_id  # The Gateway polls; still the one service.

    # The Worker confirms both stops; the next pass dispatches the model to gamma.
    lab.work()
    lab.service.tick()
    view = model(lab)
    assert (view["state"], view["node_id"]) == ("starting", "gamma")
    assert sorted(model_block(lab)["gpu_uuids"]) == [gpu["uuid"] for gpu in A40S]
    for value in sweep:
        job = lab.job(value)
        assert (job["state"], job["reason"]) == (
            "QUEUED",
            f"preempted by job {job_id[:8]}; queued to run again (preemption 1 of 3)",
        )
        assert [attempt["state"] for attempt in job["attempts"]] == ["PREEMPTED"]
    queue = lab.queue()
    assert [item["decision"] for item in queue["entries"]] == ["head_reserved", "no_capacity"]
    assert set(order(queue)) == victims
    told = [(item["kind"], item["job_id"]) for item in queue["recent_decisions"]]
    assert told.index(("started", job_id)) < told.index(("preempting", job_id))
    for value in sweep:
        assert told.index(("requeued_after_preemption", value["id"])) < told.index(
            ("preempting", job_id)
        )

    load(lab)
    assert model(lab)["state"] == "starting"
    lab.work()  # Healthy: bob's waiting request is relayed.
    chat(lab, seconds=10)
    assert pick(model(lab), {"state": "serving", "served": 1, "inflight": 0}) == {
        "state": "serving",
        "served": 1,
        "inflight": 0,
    }

    # alice's jobs still wait for the A40s, so a minute without requests releases the model.
    lab.advance(61)
    assert model(lab)["state"] == "stopping"
    lab.settle()
    assert model(lab)["state"] == "stopped"
    for value in sweep:
        assert lab.job(value)["state"] == "RUNNING"
