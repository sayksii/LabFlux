"""M8: replaceable policies, priority/fairness score, reservations and EASY backfilling."""

from contextlib import ExitStack

import pytest
from conftest import finish, heartbeat, register, submit, user_headers
from fastapi.testclient import TestClient
from pydantic import ValidationError

from labflux.api import create_app
from labflux.config import Settings
from labflux.policy import Booking, clock_text, dominant_share, earliest_start

RTX3090 = {
    "uuid": "GPU-00000001-0000-0000-0000-000000000000",
    "index": 0,
    "name": "NVIDIA GeForce RTX 3090",
    "memory_total_mib": 24576,
}
A5000 = {
    "uuid": "GPU-00000002-0000-0000-0000-000000000000",
    "index": 1,
    "name": "NVIDIA RTX A5000",
    "memory_total_mib": 24564,
}


@pytest.fixture
def make_client(tmp_path, clock):
    """Build a Scheduler API client running the named policy; all share the test clock."""
    with ExitStack() as stack:

        def build(policy, **settings):
            values = Settings(
                data_dir=tmp_path / policy,
                users={"alice": "alice-token", "bob": "bob-token"},
                scheduling_policy=policy,
                **settings,
            )
            app = create_app(values, run_loop=False, clock=lambda: clock[0])
            return stack.enter_context(TestClient(app))

        yield build


def job(client, value):
    return client.get(f"/api/jobs/{value['id']}", headers=user_headers(value["owner"])).json()


def advance(client, clock, seconds, name, instance, gpus=()):
    """Move the clock and keep the node online (heartbeats expire after 10 seconds)."""
    clock[0] += seconds
    heartbeat(client, name, instance, gpus)


def run_shared_workload(client):
    """One input for every policy: A runs; B needs the whole node; C is short; D is urgent."""
    register(client, "kayle", cpu=5, memory=8192)
    jobs = {
        "A": submit(client, cpu=3, user="alice", time_limit_s=3600),
        "B": submit(client, cpu=5, user="bob", time_limit_s=3600),
        "C": submit(client, cpu=1, user="alice", time_limit_s=600),
        "D": submit(client, cpu=1, user="bob", time_limit_s=7200, priority=9),
    }
    return {name: job(client, value) for name, value in jobs.items()}


def test_same_workload_under_three_policies_is_explained(make_client, clock):
    started = {}
    results = {}
    for policy in ("fcfs", "priority", "backfill"):
        clock[0] = 1000.0
        results[policy] = run_shared_workload(make_client(policy))
        started[policy] = {
            name for name, value in results[policy].items() if value["state"] == "STARTING"
        }
    assert started == {"fcfs": {"A"}, "priority": {"A", "D"}, "backfill": {"A", "C", "D"}}

    fcfs, priority, backfill = results["fcfs"], results["priority"], results["backfill"]
    b_id = fcfs["B"]["id"][:8]
    assert fcfs["B"]["schedule_note"].startswith("fcfs: first in line (submission #2)")
    assert fcfs["D"]["schedule_note"] == f"fcfs: submission #4; waiting behind job {b_id}"
    # The urgent job overtakes; B and C tie on score and keep submission order.
    assert priority["D"]["schedule_note"].startswith("priority: started on kayle (score 900")
    b_id = priority["B"]["id"][:8]
    assert priority["C"]["schedule_note"].endswith(f"waiting behind job {b_id}")
    # B is reserved where it fits first: when A (t=4600) and D (t=8200) have both ended.
    assert backfill["B"]["schedule_note"].endswith(f"reserved on kayle at {clock_text(8200)}")
    assert "backfilled on kayle ahead of job" in backfill["C"]["schedule_note"]
    assert "its time limit ends before the reservation" in backfill["C"]["schedule_note"]
    assert backfill["B"]["reason"] == "waiting for matching CPU/RAM/GPU capacity or datasets"


@pytest.mark.parametrize("policy", ["fcfs", "priority"])
def test_priority_overtakes_only_when_the_policy_uses_it(make_client, clock, policy):
    client = make_client(policy)
    instance = register(client, cpu=1)
    blocker = submit(client, user="bob")
    low = submit(client, priority=1)
    high = submit(client, priority=9)
    assert finish(client, blocker, instance).status_code == 200
    winner, loser = (low, high) if policy == "fcfs" else (high, low)
    assert job(client, winner)["state"] == "STARTING"
    assert job(client, loser)["state"] == "QUEUED"


def test_waiting_time_bonus_lets_an_older_lower_priority_job_win(make_client, clock):
    client = make_client("priority")
    instance = register(client, cpu=1)
    blocker = submit(client, user="bob")
    older = submit(client, priority=4)
    advance(client, clock, 101 * 60, "vm-a", instance)
    newer = submit(client, priority=5)
    # 4 * 100 + 101 queued minutes beats 5 * 100 + 0.
    assert "score 501 = priority 4, waited 101 min" in job(client, older)["schedule_note"]
    assert finish(client, blocker, instance).status_code == 200
    assert job(client, older)["state"] == "STARTING"
    assert job(client, newer)["state"] == "QUEUED"


def test_recent_heavy_usage_is_penalized_and_ages_out(make_client, clock):
    client = make_client("priority")
    service = client.app.state.scheduler
    instance = register(client, cpu=2, memory=1024)
    heavy = submit(client, cpu=2, user="alice")  # The whole cluster's CPU for an hour.
    advance(client, clock, 3600, "vm-a", instance)
    mine = submit(client, cpu=2, user="alice")
    theirs = submit(client, cpu=2, user="bob")
    assert "recent usage 60 min" in job(client, mine)["schedule_note"]
    assert "recent usage 0 min" in job(client, theirs)["schedule_note"]
    assert finish(client, heavy, instance).status_code == 200
    assert job(client, theirs)["state"] == "STARTING"
    assert job(client, mine)["state"] == "QUEUED"

    def usage(now):
        available = service.list_nodes()
        with service.db.transaction() as connection:
            return service._recent_usage(connection, now, available)

    # The finished hour slides out of the 60-minute window; bob's running job now counts.
    assert usage(4600 + 1800)["alice"] == pytest.approx(30)
    assert usage(4600 + 1800)["bob"] == pytest.approx(30)
    assert usage(4600 + 3600).get("alice", 0) == 0


def test_recent_usage_is_normalized_by_the_cluster_gpu_count(make_client, clock):
    client = make_client("priority")
    cards = [RTX3090, A5000]
    instance = register(client, cpu=8, memory=8192, gpus=cards)
    heartbeat(client, "vm-a", instance, cards)
    # One of two cards for an hour: half the cluster by GPU, an eighth of it by CPU.
    submit(client, cpu=1, user="alice", resources={"gpu": 1})
    advance(client, clock, 3600, "vm-a", instance, cards)
    mine = submit(client, user="alice", resources={"gpu": 2})
    theirs = submit(client, user="bob", resources={"gpu": 2})
    assert "recent usage 30 min" in job(client, mine)["schedule_note"]
    assert "recent usage 0 min" in job(client, theirs)["schedule_note"]


def test_backfill_never_delays_the_reserved_job(make_client, clock):
    client = make_client("backfill")
    instance = register(client, "kayle", cpu=4, memory=8192)
    running = submit(client, cpu=2, time_limit_s=600)  # Holds 2 CPUs until t=1600.
    head = submit(client, cpu=4, user="bob")
    long = submit(client, cpu=2, time_limit_s=3600, estimated_runtime_s=60)
    short = submit(client, cpu=2, time_limit_s=300)
    assert job(client, head)["schedule_note"].endswith(f"reserved on kayle at {clock_text(1600)}")
    # Its estimate would fit, but only the hard limit counts.
    assert "its 3600s time limit would delay job" in job(client, long)["schedule_note"]
    assert "estimate 60s is advisory only" in job(client, long)["schedule_note"]
    assert job(client, long)["state"] == "QUEUED"
    assert job(client, short)["state"] == "STARTING"

    advance(client, clock, 300, "kayle", instance)
    assert finish(client, job(client, short), instance).status_code == 200
    assert job(client, long)["state"] == "QUEUED"  # Two CPUs are free but still promised.
    advance(client, clock, 300, "kayle", instance)
    assert finish(client, job(client, running), instance).status_code == 200
    assert job(client, head)["state"] == "STARTING"
    assert job(client, head)["attempts"][0]["created_at"] == 1600
    assert job(client, long)["state"] == "QUEUED"


def test_backfill_may_use_resources_the_reserved_job_does_not_need(make_client, clock):
    client = make_client("backfill")
    register(client, "kayle", cpu=8, memory=8192)
    register(client, "karma", cpu=1, memory=1024)
    submit(client, cpu=4, time_limit_s=600)
    head = submit(client, cpu=6, user="bob")  # Fits on kayle once the 4 CPUs return.
    spare = submit(client, cpu=2, time_limit_s=3600)
    over = submit(client, cpu=2, time_limit_s=3600)
    elsewhere = submit(client, cpu=1, time_limit_s=7200)
    assert job(client, head)["state"] == "QUEUED"
    assert "it leaves enough room for the reserved job" in job(client, spare)["schedule_note"]
    assert job(client, over)["state"] == "QUEUED"
    assert job(client, elsewhere)["attempts"][0]["node_id"] == "karma"
    assert "the reserved job waits for kayle" in job(client, elsewhere)["schedule_note"]


def test_reservation_counts_matching_gpus_per_node(make_client, clock):
    client = make_client("backfill")
    cards = [RTX3090, A5000]
    instance = register(client, "kayle", cpu=8, memory=8192, gpus=cards)
    heartbeat(client, "kayle", instance, cards)
    submit(client, resources={"gpu": 1, "gpu_model": "3090"}, time_limit_s=3600)
    short_a5000 = submit(client, resources={"gpu": 1, "gpu_model": "a5000"}, time_limit_s=600)
    head = submit(client, user="bob", resources={"gpu": 1, "gpu_model": "3090"})
    # The A5000 frees first (t=1600), but the head needs the 3090 (t=4600).
    assert job(client, head)["schedule_note"].endswith(f"reserved on kayle at {clock_text(4600)}")
    advance(client, clock, 600, "kayle", instance, cards)
    assert finish(client, job(client, short_a5000), instance).status_code == 200
    anygpu = submit(client, resources={"gpu": 1}, time_limit_s=7200)
    assert job(client, anygpu)["attempts"][0]["gpu_uuids"] == [A5000["uuid"]]
    assert "it leaves enough room" in job(client, anygpu)["schedule_note"]
    assert job(client, head)["state"] == "QUEUED"


def test_backfill_may_not_take_a_card_the_reserved_job_needs(make_client, clock):
    client = make_client("backfill")
    second_3090 = {**RTX3090, "uuid": "GPU-00000003-0000-0000-0000-000000000000", "index": 2}
    cards = [RTX3090, second_3090]
    instance = register(client, "kayle", cpu=8, memory=8192, gpus=cards)
    heartbeat(client, "kayle", instance, cards)
    submit(client, resources={"gpu": 1, "gpu_model": "3090"}, time_limit_s=600)
    head = submit(client, user="bob", resources={"gpu": 2, "gpu_model": "3090"})
    rival = submit(client, resources={"gpu": 1, "gpu_model": "3090"}, time_limit_s=3600)
    assert job(client, head)["schedule_note"].endswith(f"reserved on kayle at {clock_text(1600)}")
    assert job(client, rival)["state"] == "QUEUED"
    assert "would delay job" in job(client, rival)["schedule_note"]


def test_backfill_prefers_a_card_the_reserved_job_cannot_use(make_client, clock):
    client = make_client("backfill")
    second_3090 = {**RTX3090, "uuid": "GPU-00000003-0000-0000-0000-000000000000", "index": 2}
    a6000 = {
        "uuid": "GPU-00000004-0000-0000-0000-000000000000",
        "index": 3,
        "name": "NVIDIA RTX A6000",
        "memory_total_mib": 49140,
    }
    cards = [RTX3090, second_3090, a6000]
    instance = register(client, "kayle", cpu=8, memory=8192, gpus=cards)
    heartbeat(client, "kayle", instance, cards)
    submit(client, resources={"gpu": 1, "gpu_model": "3090"}, time_limit_s=600)
    head = submit(client, user="bob", resources={"gpu": 2, "gpu_model": "3090"})
    assert job(client, head)["schedule_note"].endswith(f"reserved on kayle at {clock_text(1600)}")
    # Smallest first would pick the free 3090, which the head job is waiting for.
    anygpu = submit(client, resources={"gpu": 1}, time_limit_s=3600)
    assert job(client, anygpu)["attempts"][0]["gpu_uuids"] == [a6000["uuid"]]
    assert "it leaves enough room" in job(client, anygpu)["schedule_note"]
    rival = submit(client, resources={"gpu": 1}, time_limit_s=3600)
    assert job(client, rival)["state"] == "QUEUED"
    assert "would delay job" in job(client, rival)["schedule_note"]


def test_backfill_without_any_reservation_lets_later_jobs_start(make_client, clock):
    client = make_client("backfill")
    register(client, cpu=2)
    impossible = submit(client, cpu=4)
    later = submit(client, cpu=1)
    assert "no online node can fit it right now" in job(client, impossible)["schedule_note"]
    assert job(client, later)["state"] == "STARTING"
    assert job(client, later)["schedule_note"].startswith("backfill: started on vm-a")


def test_unreservable_job_does_not_leave_the_next_blocked_job_unprotected(make_client, clock):
    client = make_client("backfill")
    register(client, "kayle", cpu=4, memory=8192)
    submit(client, cpu=2, time_limit_s=600)  # Holds 2 CPUs until t=1600.
    impossible = submit(client, cpu=16)
    head = submit(client, cpu=4, user="bob")
    long = submit(client, cpu=2, time_limit_s=3600)
    assert "does not block later jobs" in job(client, impossible)["schedule_note"]
    assert job(client, head)["schedule_note"].endswith(f"reserved on kayle at {clock_text(1600)}")
    assert job(client, long)["state"] == "QUEUED"
    assert "would delay job" in job(client, long)["schedule_note"]


def test_priority_estimate_and_policy_validation(make_client, client):
    assert submit(client)["spec"]["priority"] == 5
    for bad in ({"priority": 10}, {"priority": -1}, {"estimated_runtime_s": 0}):
        response = client.post("/api/jobs", headers=user_headers(), json={"argv": ["true"], **bad})
        assert response.status_code == 422
    response = client.post(
        "/api/jobs",
        headers=user_headers(),
        json={"argv": ["true"], "time_limit_s": 60, "estimated_runtime_s": 61},
    )
    assert response.status_code == 422
    assert submit(make_client("priority", default_priority=7))["spec"]["priority"] == 7
    with pytest.raises(ValidationError):
        Settings(scheduling_policy="lottery")


def test_reservation_helpers():
    node = {"capacity": {"cpu": 4, "memory_mib": 4096}}
    cards = frozenset({"a", "b"})
    bookings = [
        Booking(end=1600, cpu=2, memory_mib=1024),
        Booking(end=3000, cpu=1, memory_mib=1024, gpus=frozenset({"a"})),
    ]
    cpu = {"cpu": 3, "memory_mib": 1024, "gpu": 0}
    two_cards = {"cpu": 1, "memory_mib": 256, "gpu": 2}
    assert earliest_start(node, cards, bookings, cpu, 1000, 32) == 1600
    assert earliest_start(node, cards, bookings, two_cards, 1000, 32) == 3000
    assert earliest_start(node, cards, bookings, {**cpu, "cpu": 5}, 1000, 32) is None
    # An attempt past its deadline is expected to release at any moment.
    assert earliest_start(node, cards, bookings, cpu, 2000, 32) == 2000
    assert earliest_start(node, cards, bookings, cpu, 1000, 1) == 3000
    capacity = {"cpu": 16, "memory_mib": 32768, "gpu": 0}
    assert dominant_share({"cpu": 4, "memory_mib": 16384, "gpu": 0}, capacity) == 0.5
    assert dominant_share({"cpu": 4, "memory_mib": 16384, "gpu": 1}, capacity) == 0.5
    assert dominant_share({"cpu": 4, "memory_mib": 4096, "gpu": 3}, {**capacity, "gpu": 4}) == 0.75
