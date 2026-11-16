"""M12 acceptance: storyboard scenes 2 and 3 played with GPU jobs, as the GPU-focused demo shows
them, through the HTTP API.

The Lab, the fake clock and the fake Workers of test_storyboard.py, under the backfill policy
with the users alice and bob, but gemma4-31b is configured on demand and has not been asked
for, so all five cards are free for batch jobs when a scene starts:
    beta   16 CPU, 24 GiB, RTX 4090
    delta  16 CPU, 48 GiB, RTX 3090 + RTX A5000
    gamma  32 CPU, 64 GiB, two A40s and the dataset hf-models (gemma4-31b starts there only
           when a request asks for it)
Each scene checks its placements with the job attempts first, then what the Dashboard's GPU
axis and queue table show: GET /api/queue (decisions, GPU blockers, the reservation, scores,
recent decisions) and the allocations in GET /api/nodes.
"""

import pytest
from conftest import user_headers
from fastapi.testclient import TestClient
from test_storyboard import (
    A40S,
    A5000,
    GEMMA,
    RTX3090,
    RTX4090,
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

# Started by the first request; about 4-5 minutes to start on two A40s in the lab.
GEMMA_ON_DEMAND = {**GEMMA, "mode": "on_demand", "startup_timeout_s": 600, "cold_wait_s": 600}


@pytest.fixture
def lab(tmp_path):
    clock = [T0]
    settings = Settings(
        data_dir=tmp_path,
        users={"alice": "alice-token", "bob": "bob-token"},
        scheduling_policy="backfill",
        models={"gemma4-31b": GEMMA_ON_DEMAND},
    )
    app = create_app(settings, run_loop=False, clock=lambda: clock[0])
    with TestClient(app) as client:
        lab = Lab(client, clock)
        models = client.get("/api/models", headers=user_headers()).json()
        assert [(m["name"], m["mode"], m["state"]) for m in models] == [
            ("gemma4-31b", "on_demand", "stopped")
        ]
        assert all(not node["allocations"] for node in lab.nodes().values())
        yield lab


def uuids(*cards):
    return [gpu["uuid"] for gpu in cards]


def gpu_blocker(need, best_node, free, held, model=None):
    """A GPU blocker on cards that are all reported and free of outside use."""
    return {
        "kind": "gpu",
        "need": need,
        "model": model,
        "min_vram_mib": None,
        "best_node": best_node,
        "free": free,
        "held": held,
        "external": 0,
        "unreported": 0,
    }


def test_gpu_scene_2_priority_jumps_the_queue_and_gpu_usage_reorders_equal_priorities(lab):
    """Scene 2 on GPUs, priority jump and fairness: alice's five one-card jobs hold every card;
    four minutes later bob sends a priority 9 one-card job, then alice and bob each send a
    priority 5 one-card job, alice a minute first.

    The audience sees bob's priority 9 job first in line with a reservation, and bob's priority
    5 job ahead of alice's earlier one: her extra minute of waiting is outweighed by six minutes
    of recent usage (five cards, a fifth of the cluster each, for six minutes), 500 + 1 - 6
    against 500 + 0 - 0. When alice cancels one of her jobs, the freed card goes to bob's
    priority 9 job, and alice's priority 5 job still waits behind bob's.
    """
    sweep = [lab.submit("alice", cpu=4, memory_gib=8, gpu=1, limit=1800) for _ in range(5)]
    lab.settle()
    # Best fit: beta's single card first, then delta's two (less spare CPU than gamma), the
    # smaller A5000 before the 3090, then gamma's A40s.
    cards = (
        ("beta", RTX4090),
        ("delta", A5000),
        ("delta", RTX3090),
        ("gamma", A40S[0]),
        ("gamma", A40S[1]),
    )
    for value, (node, gpu) in zip(sweep, cards):
        assert_running(lab, value, node, [gpu])
    lab.advance(240)
    lab.work()
    urgent = lab.submit("bob", cpu=8, memory_gib=16, gpu=1, priority=9, limit=1800)
    lab.settle()
    lab.advance(60)
    alice = lab.submit("alice", cpu=4, memory_gib=8, gpu=1, priority=5, limit=600)
    lab.advance(60)
    bob = lab.submit("bob", cpu=4, memory_gib=8, gpu=1, priority=5, limit=600)
    lab.settle()
    for value in (urgent, alice, bob):
        job = lab.job(value)
        assert (job["state"], job["attempts"]) == ("QUEUED", []), job["schedule_note"]
    assert (
        "score 495 = priority 5, waited 1 min, recent usage 6 min"
        in lab.job(alice)["schedule_note"]
    )
    assert (
        "score 500 = priority 5, waited 0 min, recent usage 0 min" in lab.job(bob)["schedule_note"]
    )

    queue = lab.queue()
    assert (queue["policy"], queue["computed_at"]) == ("backfill", lab.clock[0])
    assert order(queue) == [urgent["id"], bob["id"], alice["id"]]
    every_card_held = gpu_blocker(1, "beta", free=0, held=1)
    want = {
        "position": 1,
        "owner": "bob",
        "mine": False,
        "kind": "BATCH",
        "priority": 9,
        "score": 902,
        "score_parts": {"priority": 900, "waited_min": 2, "usage_min": 0},
        "decision": "head_reserved",
        "blocker": every_card_held,
        "head_job_id": None,
        "waiting_for": [],
    }
    assert pick(entry(queue, urgent), want) == want
    # Every card ends at the same time limit; ties go to the node id.
    assert queue["reservation"] == {
        "job_id": urgent["id"],
        "node_id": "beta",
        "start": T0 + 1800,
        "cpu": 8,
        "memory_mib": 16384,
        "gpu": 1,
    }
    mine, theirs = entry(queue, alice), entry(queue, bob)
    for item, owner, position in ((theirs, "bob", 2), (mine, "alice", 3)):
        want = {
            "position": position,
            "owner": owner,
            "mine": owner == "alice",
            "priority": 5,
            "decision": "no_capacity",
            "blocker": every_card_held,
            "head_job_id": urgent["id"],
        }
        assert pick(item, want) == want
    assert (theirs["score_parts"], theirs["score"]) == (
        {"priority": 500, "waited_min": 0, "usage_min": 0},
        500,
    )
    assert mine["score_parts"] == {
        "priority": 500,
        "waited_min": 1,
        "usage_min": pytest.approx(6),
    }
    assert mine["score"] == pytest.approx(495)
    assert queue["usage_min_by_user"] == {"alice": pytest.approx(6)}

    # The GPU axis: every card holds one of alice's blocks; bob sees them too (decision 1A).
    nodes = lab.nodes(viewer="bob")
    held = {
        name: sorted(gpu for item in node["allocations"] for gpu in item["gpu_uuids"])
        for name, node in nodes.items()
    }
    assert held == {
        "beta": uuids(RTX4090),
        "delta": uuids(RTX3090, A5000),
        "gamma": uuids(*A40S),
    }
    want = {
        "owner": "alice",
        "mine": False,
        "kind": "BATCH",
        "state": "RUNNING",
        "ends_at": T0 + 1800,
        "placement": "started",
        "preempted_by": None,
    }
    for node in nodes.values():
        for item in node["allocations"]:
            assert pick(item, want) == want
    started = {item["job_id"]: item for item in decisions(queue, "started")}
    for value, (node, gpu) in zip(sweep, cards):
        told = started[value["id"]]
        assert (told["owner"], told["node_id"], told["detail"]["gpu_uuids"]) == (
            "alice",
            node,
            [gpu["uuid"]],
        )

    # alice cancels her job on delta's A5000: bob's priority 9 job waits for it to stop, then
    # gets the card; alice's priority 5 job, sent before bob's, still waits behind it.
    lab.advance(60)
    response = lab.client.post(f"/api/jobs/{sweep[1]['id']}/cancel", headers=user_headers())
    assert response.status_code == 200, response.text
    lab.service.tick()  # before the Worker confirms the stop
    want = {"decision": "waiting_for_release", "waiting_for": [sweep[1]["id"]]}
    assert pick(entry(lab.queue(), urgent), want) == want
    lab.settle()
    assert_running(lab, urgent, "delta", [A5000])
    assert lab.job(sweep[1])["state"] == "CANCELLED"
    queue = lab.queue()
    assert [(item["job_id"], item["decision"]) for item in queue["entries"]] == [
        (bob["id"], "head_reserved"),
        (alice["id"], "no_capacity"),
    ]
    assert (queue["reservation"]["job_id"], queue["reservation"]["node_id"]) == (bob["id"], "beta")
    [told] = decisions(queue, "started", urgent)
    assert (told["owner"], told["node_id"], told["detail"]["gpu_uuids"]) == (
        "bob",
        "delta",
        uuids(A5000),
    )
    block = lab.allocations("delta")[urgent["id"]]
    assert (block["owner"], block["gpu_uuids"], block["placement"]) == (
        "bob",
        uuids(A5000),
        "started",
    )


def test_gpu_scene_3_backfill_runs_a_short_one_card_job_before_a_two_a40_reservation(lab):
    """Scene 3 on GPUs, EASY backfill: alice's hour-long jobs hold the three 24 GiB cards, and
    bob's 18-core job with a 5-minute limit leaves gamma 14 free cores. bob sends a priority 9
    job that needs both A40s and 16 cores; alice then sends a one-card job with a 30-minute
    limit, and a one-card job with a 2-minute limit.

    The audience sees gamma reserved for bob's two-A40 job from 14:05, when the 18-core job
    reaches its limit; the 2-minute job marked as backfilled on an A40 because it ends first;
    the 30-minute job, which also fits on the other A40 now, waiting with the decision "would
    delay the reservation"; and bob's job landing on both A40s once the countdown runs out.
    """
    held = [
        lab.submit("alice", cpu=4, memory_gib=8, gpu=1, gpu_model=model, limit=3600)
        for model in ("4090", "A5000", "3090")
    ]
    prep = lab.submit("bob", cpu=18, memory_gib=8, limit=300)  # only gamma has 18 cores
    lab.settle()
    for value, node, gpu in zip(held, ("beta", "delta", "delta"), (RTX4090, A5000, RTX3090)):
        assert_running(lab, value, node, [gpu])
    assert_running(lab, prep, "gamma")
    lab.advance(120)
    lab.work()
    head = lab.submit("bob", cpu=16, memory_gib=32, gpu=2, gpu_model="A40", priority=9, limit=600)
    lab.settle()
    reservation = {
        "job_id": head["id"],
        "node_id": "gamma",
        "start": T0 + 300,
        "cpu": 16,
        "memory_mib": 32768,
        "gpu": 2,
    }
    queue = lab.queue()
    assert queue["reservation"] == reservation
    # Both A40s are free; gamma's cores are what the head waits for.
    want = {
        "decision": "head_reserved",
        "blocker": {"kind": "cpu", "need": 16, "best_free": 14, "best_node": "gamma"},
    }
    assert pick(entry(queue, head), want) == want

    long = lab.submit("alice", cpu=2, memory_gib=4, gpu=1, limit=1800)  # sent before the short one
    lab.settle()
    assert entry(lab.queue(), long)["decision"] == "would_delay_reservation"
    short = lab.submit("alice", cpu=2, memory_gib=2, gpu=1, limit=120)
    lab.settle()
    # The one-card jobs fit only on gamma's A40s; the short one takes the first.
    assert_running(lab, short, "gamma", [A40S[0]])
    note = lab.job(short)["schedule_note"]
    assert note.startswith(
        f"backfill: backfilled on gamma ahead of job {head['id'][:8]} reserved on gamma at "
    )
    assert "its time limit ends before the reservation" in note
    for value in (head, long):
        assert lab.job(value)["state"] == "QUEUED"
    assert "1800s time limit would delay job" in lab.job(long)["schedule_note"]

    # Both decisions at once, and they stay: the long job still fits on the other A40.
    queue = lab.queue()
    assert order(queue) == [head["id"], long["id"]]
    assert queue["reservation"] == reservation
    want = {
        "position": 1,
        "owner": "bob",
        "mine": False,
        "priority": 9,
        "decision": "head_reserved",
        "blocker": gpu_blocker(2, "gamma", free=1, held=1, model="A40"),
        "head_job_id": None,
    }
    assert pick(entry(queue, head), want) == want
    assert entry(queue, head)["score_parts"]["priority"] == 900
    want = {
        "position": 2,
        "owner": "alice",
        "mine": True,
        "priority": 5,
        "decision": "would_delay_reservation",
        "head_job_id": head["id"],
        "blocker": None,
        "note": lab.job(long)["schedule_note"],
    }
    assert pick(entry(queue, long), want) == want
    [backfilled] = decisions(queue, "backfilled", short)
    want = {"owner": "alice", "node_id": "gamma"}
    assert pick(backfilled, want) == want
    assert backfilled["detail"]["ahead_of"] == head["id"]
    assert backfilled["detail"]["gpu_uuids"] == uuids(A40S[0])
    gamma = lab.allocations("gamma", viewer="bob")
    assert {
        label: pick(gamma[value["id"]], ("placement", "gpu_uuids", "ends_at"))
        for label, value in (("short", short), ("prep", prep))
    } == {
        # Ends before the reservation at T0 + 300.
        "short": {"placement": "backfilled", "gpu_uuids": uuids(A40S[0]), "ends_at": T0 + 240},
        "prep": {"placement": "started", "gpu_uuids": [], "ends_at": T0 + 300},
    }
    assert long["id"] not in gamma

    # The countdown runs out: the short job ends at 14:04, the 18-core job at 14:05.
    lab.advance(120)
    lab.done(short)
    lab.advance(60)
    assert entry(lab.queue(), long)["decision"] == "would_delay_reservation"
    lab.done(prep)
    lab.settle()
    job = assert_running(lab, head, "gamma", A40S)
    assert job["schedule_note"].startswith("backfill: started on gamma (score ")
    queue = lab.queue()
    [started] = decisions(queue, "started", head)
    assert (started["node_id"], started["detail"]["gpu_uuids"]) == ("gamma", uuids(*A40S))
    # The 30-minute job is first in line now, reserved for when bob's job reaches its limit.
    assert [(item["job_id"], item["decision"]) for item in queue["entries"]] == [
        (long["id"], "head_reserved")
    ]
    assert (queue["reservation"]["node_id"], queue["reservation"]["start"]) == (
        "gamma",
        T0 + 300 + 600,
    )
