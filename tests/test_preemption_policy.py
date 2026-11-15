"""M11: the pure victim selection behind preemption (no SQLite)."""

from labflux.config import Settings
from labflux.models import JobSpec
from labflux.policy import (
    FcfsPolicy,
    PriorityPolicy,
    Victim,
    cheapest_preemption,
    choose_victims,
    fits_after_stopping,
    lost_seconds,
    victim_key,
)

NODE = {"capacity": {"cpu": 4, "memory_mib": 4096}}
CARD_A = "GPU-00000001-0000-0000-0000-000000000000"
CARD_B = "GPU-00000002-0000-0000-0000-000000000000"


def victim(name, lost=0.0, priority=1, cpu=1.0, memory=256, cards=()):
    return Victim(f"attempt-{name}", name, priority, lost, cpu, memory, frozenset(cards))


def used(*victims, cpu=0.0, memory=0, jobs=0):
    """The node's allocation: the given victims plus other attempts that cannot be stopped."""
    return {
        "cpu": cpu + sum(v.cpu for v in victims),
        "memory_mib": memory + sum(v.memory_mib for v in victims),
        "jobs": jobs + len(victims),
    }


def request(cpu=1.0, memory=256, gpu=0):
    return {"cpu": cpu, "memory_mib": memory, "gpu": gpu}


def choose(candidates, need, allocation, held=frozenset(), matching=frozenset(), max_jobs=32):
    return choose_victims(NODE, allocation, held, matching, need, candidates, max_jobs)


def test_lost_work_counts_from_the_reported_start():
    assert lost_seconds(None, 5000) == 0
    assert lost_seconds(4000, 5000) == 1000
    assert lost_seconds(6000, 5000) == 0  # Clock skew never yields negative lost work.


def test_order_is_lost_work_then_priority_then_job_id():
    ordered = sorted(
        [
            victim("c", lost=10, priority=1),
            victim("b", lost=5, priority=3),
            victim("a", lost=5, priority=3),
            victim("d", lost=5, priority=2),
        ],
        key=victim_key,
    )
    assert [v.job_id for v in ordered] == ["d", "a", "b", "c"]


def test_one_victim_with_less_lost_work_is_enough():
    old, recent = victim("old", lost=600, cpu=2), victim("recent", lost=60, cpu=2)
    chosen = choose([old, recent], request(cpu=2), used(old, recent))
    assert chosen == [recent]


def test_victims_are_added_until_it_fits():
    a, b, c = victim("a", lost=1, cpu=1), victim("b", lost=2, cpu=1), victim("c", lost=3, cpu=1)
    # Two of four CPUs are held by a job that cannot be stopped.
    assert choose([c, b, a], request(cpu=2), used(a, b, c, cpu=1)) == [a, b]
    assert choose([a, b], request(cpu=4), used(a, b, cpu=2)) is None


def test_victims_that_are_no_longer_needed_are_dropped():
    """A small early victim does not help once a later one frees enough by itself."""
    small, big = victim("small", lost=1, cpu=0.5), victim("big", lost=2, cpu=3)
    assert choose([small, big], request(cpu=3), used(small, big, cpu=0.5)) == [big]


def test_memory_and_the_job_slot_limit_count():
    a, b = victim("a", lost=1, memory=1024), victim("b", lost=2, memory=3072)
    assert choose([a, b], request(cpu=0.5, memory=3000), used(a, b)) == [b]
    slot = victim("slot", lost=1, cpu=0.5)
    # CPU and RAM are free, but the node already runs its maximum of two jobs.
    assert choose([slot], request(cpu=0.5), used(slot, jobs=1), max_jobs=2) == [slot]


def test_a_gpu_request_frees_a_matching_card_only():
    on_a = victim("on-a", lost=100, cards=[CARD_A])
    on_b = victim("on-b", lost=1, cards=[CARD_B])
    held = frozenset({CARD_A, CARD_B})
    # Only CARD_A matches the request (model, VRAM, no external use).
    chosen = choose([on_a, on_b], request(gpu=1), used(on_a, on_b), held, frozenset({CARD_A}))
    assert chosen == [on_a]
    assert choose([on_b], request(gpu=1), used(on_b), held, frozenset({CARD_A})) is None


def test_it_already_fits_without_stopping_anything():
    a = victim("a")
    assert choose([a], request(cpu=1), used(a)) == []
    assert fits_after_stopping(NODE, used(a), frozenset(), frozenset(), request(), [], 32)


def test_across_nodes_the_least_total_lost_work_wins():
    options = {
        "kayle": [victim("a", lost=50), victim("b", lost=60)],
        "karma": [victim("c", lost=200)],
        "kindred": [],
    }
    assert cheapest_preemption(options) == ("kayle", options["kayle"])
    tie = {"kayle": [victim("a", lost=5), victim("b", lost=5)], "karma": [victim("c", lost=10)]}
    assert cheapest_preemption(tie)[0] == "karma"  # Same lost work: fewer victims.
    assert cheapest_preemption({"b": [victim("x")], "a": [victim("y")]})[0] == "a"
    assert cheapest_preemption({"kayle": []}) is None


def test_policy_order_check_used_against_ping_pong():
    settings = Settings(scheduling_policy="priority")
    priority, fcfs = PriorityPolicy(settings), FcfsPolicy(settings)
    spec = JobSpec(argv=["true"], priority=9)
    low = JobSpec(argv=["true"], priority=4)
    head = {"id": "h", "owner": "bob", "kind": "BATCH", "submission_seq": 9, "created_at": 1000}
    fresh = {"id": "l", "owner": "alice", "kind": "BATCH", "submission_seq": 1, "created_at": 1000}
    old = {**fresh, "created_at": 1000 - 600 * 60}  # Queued ten hours ago.
    assert priority.before((head, spec), (fresh, low), 1000, {})
    # 4 * 100 + 600 waited minutes outranks 9 * 100: requeued, it would win straight back.
    assert not priority.before((head, spec), (old, low), 1000, {})
    assert not fcfs.before((head, spec), (fresh, low), 1000, {})
    assert not fcfs.batch_preemption and priority.batch_preemption
