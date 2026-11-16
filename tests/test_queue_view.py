"""M12: the Scheduler's queue view, its recent decisions and the dispatch placement note.

Scheduler.queue_view(viewer) returns the queue exactly as the last scheduling pass considered it:
one entry per queued job with the decision, why it cannot start now (blocker), its score and the
schedule note that pass wrote; plus the backfill reservation and the latest decisions.
"""

from contextlib import ExitStack

import pytest
from conftest import heartbeat, register, submit
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_preemption import advance, high, job, lapse, low, restart, run, stopped

from labflux.api import create_app
from labflux.config import Settings
from labflux.database import jobs
from labflux.explain import placement

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
RTX4090 = {
    "uuid": "GPU-00000003-0000-0000-0000-000000000000",
    "index": 0,
    "name": "NVIDIA GeForce RTX 4090",
    "memory_total_mib": 24564,
}
FAKE = {
    "image": "python:3.12-slim",
    "argv": ["python", "/datasets/fake-model/server.py"],
    "resources": {"cpu": 1, "memory_mib": 512},
    "datasets": ["fake-model"],
    "cold_wait_s": 0,
}


@pytest.fixture
def make_client(tmp_path, clock):
    """Build a Scheduler API client running the named policy; all share the test clock."""
    with ExitStack() as stack:
        built = []

        def build(policy, **settings):
            values = Settings(
                data_dir=tmp_path / f"{policy}-{len(built)}",
                users={"alice": "alice-token", "bob": "bob-token"},
                scheduling_policy=policy,
                **settings,
            )
            app = create_app(values, run_loop=False, clock=lambda: clock[0])
            built.append(app)
            return stack.enter_context(TestClient(app))

        yield build


def view(client, viewer="alice"):
    return client.app.state.scheduler.queue_view(viewer)


def entries(client, viewer="alice"):
    """The queue entries by job id; every queued job has exactly one."""
    result = {entry["job_id"]: entry for entry in view(client, viewer)["entries"]}
    with client.app.state.scheduler.db.transaction() as connection:
        queued = connection.execute(select(jobs.c.id).where(jobs.c.state == "QUEUED")).scalars()
        assert sorted(result) == sorted(queued)
    return result


def decisions(client, kind):
    return [d for d in view(client)["recent_decisions"] if d["kind"] == kind]


def note(client, value):
    return job(client, value)["schedule_note"]


# Decisions -------------------------------------------------------------------------------------


def test_fcfs_head_blocks_and_later_jobs_wait_behind_it(make_client, clock):
    client = make_client("fcfs")
    register(client, "beta", cpu=2)
    submit(client, cpu=2)
    head = submit(client, cpu=2)
    later = submit(client, cpu=1, user="bob")
    queue = view(client)
    assert (queue["policy"], queue["computed_at"], queue["reservation"]) == ("fcfs", 1000.0, None)
    assert queue["weights"] == {"priority": 100, "wait": 1, "usage": 1}
    assert queue["usage_min_by_user"] == {}
    assert [e["job_id"] for e in queue["entries"]] == [head["id"], later["id"]]
    first, second = queue["entries"]
    assert first["position"] == 1 and first["decision"] == "head_blocked"
    assert first["mine"] and first["owner"] == "alice" and first["head_job_id"] is None
    assert first["blocker"] == {"kind": "cpu", "need": 2, "best_free": 0, "best_node": "beta"}
    assert (first["kind"], first["service"], first["priority"]) == ("BATCH", None, 5)
    assert first["score"] is None and first["score_parts"] is None
    assert first["note"] == note(client, head)
    assert second["position"] == 2 and second["decision"] == "behind_head"
    assert second["head_job_id"] == head["id"] and not second["mine"]
    assert second["note"] == f"fcfs: submission #3; waiting behind job {head['id'][:8]}"
    assert view(client, "bob")["entries"][1]["mine"]


def test_priority_entries_follow_the_score_and_split_it_into_parts(make_client, clock):
    client = make_client("priority")
    instance = register(client, "beta", cpu=1)
    submit(client, user="bob")  # bob holds the whole cluster from now on.
    older = submit(client, priority=4)
    advance(client, clock, 101 * 60, {"beta": instance})
    newer = submit(client, priority=5)
    theirs = submit(client, priority=5, user="bob")
    queue = view(client)
    assert queue["usage_min_by_user"] == pytest.approx({"bob": 60})  # The 60-minute window.
    # 400 + 101 waited beats 500, which beats bob's 500 - 60 recent usage.
    assert [e["job_id"] for e in queue["entries"]] == [older["id"], newer["id"], theirs["id"]]
    first, second, third = queue["entries"]
    assert first["score_parts"] == pytest.approx(
        {"priority": 400, "waited_min": 101, "usage_min": 0}
    )
    assert first["score"] == pytest.approx(501) and first["priority"] == 4
    assert second["score"] == pytest.approx(500)
    assert third["score_parts"] == pytest.approx(
        {"priority": 500, "waited_min": 0, "usage_min": 60}
    )
    assert third["score"] == pytest.approx(440)
    assert [e["decision"] for e in queue["entries"]] == [
        "head_blocked",
        "behind_head",
        "behind_head",
    ]
    assert "score 501 = priority 4, waited 101 min" in first["note"]


def test_backfill_reservation_and_the_jobs_around_it(make_client, clock):
    client = make_client("backfill")
    register(client, "beta", cpu=6, memory=8192)
    submit(client, cpu=4, time_limit_s=600)  # Holds 4 CPUs until t=1600.
    head = submit(client, cpu=6, user="bob")
    short = submit(client, cpu=1, time_limit_s=300)  # Ends before the reservation.
    long = submit(client, cpu=1, time_limit_s=3600)  # Fits now, but would delay it.
    big = submit(client, cpu=2, user="bob")  # One CPU is left.
    assert job(client, short)["state"] == "STARTING"
    queue = view(client)
    assert queue["reservation"] == {
        "job_id": head["id"],
        "node_id": "beta",
        "start": 1600.0,
        "cpu": 6,
        "memory_mib": 256,
        "gpu": 0,
    }
    found = entries(client)
    assert [e["job_id"] for e in queue["entries"]] == [head["id"], long["id"], big["id"]]
    assert found[head["id"]]["decision"] == "head_reserved"
    assert found[head["id"]]["blocker"]["kind"] == "cpu"
    assert found[long["id"]]["decision"] == "would_delay_reservation"
    assert found[long["id"]]["blocker"] is None
    assert found[long["id"]]["head_job_id"] == head["id"]
    assert found[big["id"]]["decision"] == "no_capacity"
    assert found[big["id"]]["blocker"] == {
        "kind": "cpu",
        "need": 2,
        "best_free": 1,
        "best_node": "beta",
    }
    [backfilled] = decisions(client, "backfilled")
    assert (backfilled["job_id"], backfilled["owner"], backfilled["node_id"]) == (
        short["id"],
        "alice",
        "beta",
    )
    assert backfilled["detail"]["ahead_of"] == head["id"]
    assert backfilled["detail"]["candidates"] == ["beta"]
    assert backfilled["text"] == f"job {short['id'][:8]}: {note(client, short)}"
    assert "backfilled on beta ahead of job" in backfilled["text"]


def test_a_queued_model_service_does_not_block_and_has_no_score(make_client, clock):
    client = make_client("priority", models={"fake": FAKE})
    service = client.app.state.scheduler
    register(client, "beta", cpu=1)
    model = service.ensure_model("fake")
    started = submit(client, cpu=1)
    assert job(client, started)["state"] == "STARTING"
    waiting = submit(client, cpu=1, user="bob")
    found = entries(client)
    entry = found[model["job_id"]]
    assert (entry["position"], entry["kind"], entry["service"]) == (1, "MODEL", "fake")
    assert entry["decision"] == "does_not_block"
    assert entry["blocker"] == {"kind": "dataset", "datasets": ["fake-model"]}
    assert entry["priority"] is None and entry["score"] is None and entry["score_parts"] is None
    assert entry["note"].endswith("model services do not hold up later jobs")
    assert found[waiting["id"]]["decision"] == "head_blocked"
    assert found[waiting["id"]]["score"] == pytest.approx(500)


def test_no_worker(make_client, clock):
    client = make_client("fcfs")
    value = submit(client)
    [entry] = view(client)["entries"]
    assert entry["job_id"] == value["id"]
    assert entry["decision"] == "no_worker" and entry["blocker"] == {"kind": "no_worker"}


def test_waiting_for_release_names_the_preempted_jobs(make_client, clock):
    client = make_client("priority")
    instance = register(client, "beta", cpu=2)
    victim = run(client, low(client), instance)
    advance(client, clock, 60, {"beta": instance})
    preemptor = high(client)
    entry = entries(client)[preemptor["id"]]
    assert entry["decision"] == "waiting_for_release"
    assert entry["waiting_for"] == [victim["id"]]
    assert f"waiting for preempted job {victim['id'][:8]} on beta" in entry["note"]
    [preempting] = decisions(client, "preempting")
    assert preempting["job_id"] == preemptor["id"] and preempting["owner"] == "bob"
    assert preempting["detail"]["victims"] == [victim["id"]]
    assert preempting["detail"]["node_id"] == "beta"

    stopped(client, victim, instance)
    [requeued] = decisions(client, "requeued_after_preemption")
    assert requeued["job_id"] == victim["id"] and requeued["node_id"] == "beta"
    assert "queued to run again (preemption 1 of 3)" in requeued["text"]
    started = view(client)["recent_decisions"][0]
    assert (started["kind"], started["job_id"]) == ("started", preemptor["id"])
    # The victim is queued again, behind nothing: the preemptor holds the node.
    assert entries(client)[victim["id"]]["decision"] == "head_blocked"


# Blockers --------------------------------------------------------------------------------------


def test_blockers_name_the_missing_dataset_memory_or_combination(make_client, clock):
    client = make_client("backfill")
    register(client, "beta", cpu=4, memory=512)
    register(client, "delta", cpu=1, memory=4096)
    dataset = submit(client, datasets=["imagenet"])
    memory = submit(client, memory=8192)
    both = submit(client, cpu=2, memory=1024)
    found = entries(client)
    assert found[dataset["id"]]["blocker"] == {"kind": "dataset", "datasets": ["imagenet"]}
    assert found[memory["id"]]["blocker"] == {
        "kind": "memory",
        "need_mib": 8192,
        "best_free_mib": 4096,
        "best_node": "delta",
    }
    assert found[both["id"]]["blocker"] == {"kind": "combination"}
    # None of them can ever fit on an online node, so none holds up the others.
    assert {e["decision"] for e in found.values()} == {"does_not_block"}


def test_gpu_blocker_counts_held_external_and_missing_cards(make_client, clock):
    client = make_client("fcfs")
    instance = register(client, "delta", cpu=8, gpus=[RTX3090, A5000])
    heartbeat(client, "delta", instance, [RTX3090, A5000], external=[A5000["uuid"]])
    holder = submit(client, resources={"gpu": 1})
    assert job(client, holder)["attempts"][0]["gpu_uuids"] == [RTX3090["uuid"]]
    blocked = submit(client, resources={"gpu": 1})
    missing = submit(client, resources={"gpu": 1, "gpu_model": "4090", "min_vram_mib": 20000})
    found = entries(client)
    assert found[blocked["id"]]["blocker"] == {
        "kind": "gpu",
        "need": 1,
        "model": None,
        "min_vram_mib": None,
        "best_node": "delta",
        "free": 0,
        "held": 1,
        "external": 1,
        "unreported": 0,
    }
    assert found[missing["id"]]["decision"] == "behind_head"
    assert found[missing["id"]]["blocker"] == {
        "kind": "gpu",
        "need": 1,
        "model": "4090",
        "min_vram_mib": 20000,
        "best_node": None,
        "free": 0,
        "held": 0,
        "external": 0,
        "unreported": 0,
    }


# Recent decisions ------------------------------------------------------------------------------


def test_node_offline_online_and_retry_decisions_newest_first(make_client, clock):
    client = make_client("fcfs")
    instance = register(client, "beta", cpu=2)
    value = run(client, submit(client, safe_to_rerun=True, retries_on_node_failure=1), instance)
    lapse(client, clock)
    latest = view(client)["recent_decisions"][0]
    assert (latest["kind"], latest["node_id"]) == ("node_offline", "beta")
    assert latest["detail"]["jobs"] == [value["id"]]
    assert "no heartbeat for 10s" in latest["text"]

    heartbeat(client, "beta", instance)
    latest = view(client)["recent_decisions"][0]
    assert (latest["kind"], latest["node_id"]) == ("node_online", "beta")
    assert "is online again" in latest["text"]

    lapse(client, clock)
    restart(client, "beta")  # A new instance whose container is gone: the job is retried.
    kinds = [d["kind"] for d in view(client)["recent_decisions"]]
    assert kinds[:4] == ["started", "retried_after_node_failure", "node_online", "node_offline"]
    retried = decisions(client, "retried_after_node_failure")[0]
    assert retried["job_id"] == value["id"] and retried["detail"]["retry"] == 1
    assert "queued for retry 1 of 1" in retried["text"]


def test_recent_decisions_keep_the_latest_fifty(make_client, clock):
    client = make_client("fcfs")
    instance = register(client, "beta", cpu=1)
    for _ in range(60):
        lapse(client, clock)
        heartbeat(client, "beta", instance)
    recent = view(client)["recent_decisions"]
    assert len(recent) == 50
    assert [d["at"] for d in recent] == sorted((d["at"] for d in recent), reverse=True)


# Placement note --------------------------------------------------------------------------------


def test_dispatch_note_names_the_candidates_and_why_the_chosen_node_won(make_client, clock):
    client = make_client("fcfs")
    register(client, "beta", cpu=4)
    register(client, "delta", cpu=8)
    cpu = submit(client, cpu=2)
    assert note(client, cpu) == (
        "fcfs: started on beta (submission #1); fits on beta, delta; chose beta: least spare CPU"
    )
    started = decisions(client, "started")[0]
    assert started["detail"]["candidates"] == ["beta", "delta"]

    client = make_client("fcfs")
    for name, cards in (("beta", [RTX4090]), ("delta", [RTX3090, A5000])):
        instance = register(client, name, cpu=4, gpus=cards)
        heartbeat(client, name, instance, cards)
    gpu = submit(client, resources={"gpu": 1})
    assert note(client, gpu).endswith("fits on beta, delta; chose beta: fewest spare matching GPUs")
    alone = submit(client, cpu=4)
    assert note(client, alone).endswith("; only delta fits")


def test_placement_says_when_the_project_files_picked_the_node():
    ranked = [((0, 0, 6, 8, 0, "delta"), "delta"), ((1, 0, 2, 4, 0, "beta"), "beta")]
    assert placement(ranked, gpu=False, project=True) == (
        "fits on beta, delta; chose delta: it holds the project's files"
    )
    tied = [((1, 0, 2, 4, 0, "beta"), "beta"), ((1, 0, 2, 8, 0, "delta"), "delta")]
    assert placement(tied, gpu=True, project=True) == (
        "fits on beta, delta; chose beta: fewest spare matching GPUs, then least spare CPU, "
        "then smallest CPU capacity"
    )


# Model services --------------------------------------------------------------------------------


def test_models_count_served_requests_and_last_activity(make_client, clock):
    client = make_client("fcfs", models={"fake": FAKE, "other": FAKE})
    service = client.app.state.scheduler
    assert [(m["served"], m["last_activity_at"]) for m in service.list_models()] == [
        (0, None),
        (0, None),
    ]
    first = service.ensure_model("fake")["job_id"]
    service.begin_request(first)
    clock[0] += 5
    service.end_request(first)
    clock[0] += 5
    service.begin_request(first)
    fake, other = service.list_models()
    assert (fake["served"], fake["last_activity_at"]) == (2, 1010.0)
    assert (other["served"], other["last_activity_at"]) == (0, None)
    assert fake["inflight"] == 1
