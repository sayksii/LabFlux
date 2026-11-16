"""M11: preemption with a rerun from the start, at the Scheduler API level.

Written from the M11 contract only: a blocked head job may have running batch jobs that their
owners marked preemptible stopped; the freed resources are handed over only after each stop is
confirmed, and every stopped job is queued to run again.
"""

import uuid
from contextlib import ExitStack

import pytest
from conftest import finish, heartbeat, register, submit, user_headers, worker_headers
from fastapi.testclient import TestClient

from labflux.api import create_app
from labflux.config import Settings

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
PREEMPTIBLE = {"safe_to_rerun": True, "preemptible": True}


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


# Users and the Worker, as seen through the HTTP API -------------------------------------------


def short(value):
    return value["id"][:8]


def job(client, value):
    return client.get(f"/api/jobs/{value['id']}", headers=user_headers(value["owner"])).json()


def low(client, cpu=2, priority=1, **fields):
    """A long-running preemptible batch job owned by alice."""
    fields.setdefault("time_limit_s", 86400)
    return submit(client, cpu=cpu, priority=priority, **PREEMPTIBLE, **fields)


def high(client, cpu=2, priority=9, **fields):
    """A batch job owned by bob that is not preemptible."""
    return submit(client, cpu=cpu, priority=priority, user="bob", **fields)


def open_session(client, user="bob", **body):
    response = client.post("/api/sessions", headers=user_headers(user), json=body)
    assert response.status_code == 200, response.text
    return job(client, {"id": response.json()["id"], "owner": user})


def commands(client, instance, name="kayle"):
    """What the node's Worker receives when it polls for commands now."""
    response = client.get(
        f"/internal/workers/{name}/commands",
        params={"instance_id": instance},
        headers=worker_headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()


def stops(client, instance, name="kayle"):
    return [c["attempt_id"] for c in commands(client, instance, name) if c["kind"] == "STOP"]


def starts(client, instance, name="kayle"):
    return [c["attempt_id"] for c in commands(client, instance, name) if c["kind"] == "START"]


def run(client, value, instance):
    """Deliver the job's START and report its container as started, as the Worker would."""
    attempt = job(client, value)["attempts"][-1]
    name, command_id = attempt["node_id"], f"start-{attempt['id']}"
    assert command_id in [c["id"] for c in commands(client, instance, name)]
    response = client.post(
        f"/internal/workers/{name}/commands/{command_id}/ack",
        headers=worker_headers(),
        json={"instance_id": instance},
    )
    assert response.status_code == 200, response.text
    response = client.post(
        f"/internal/workers/{name}/attempts/{attempt['id']}/events",
        headers=worker_headers(),
        json={"event_id": f"start-{attempt['id']}", "instance_id": instance, "kind": "STARTED"},
    )
    assert response.status_code == 200, response.text
    value = job(client, value)
    assert value["state"] == "RUNNING"
    return value


def stopped(client, value, instance):
    """The Worker confirms that the STOP took effect and the container is gone."""
    response = finish(client, job(client, value), instance, outcome="CANCELLED", exit_code=137)
    assert response.status_code == 200, response.text


def advance(client, clock, seconds, nodes, gpus=()):
    """Move the clock and keep every node online (heartbeats expire after 10 seconds)."""
    clock[0] += seconds
    for name, instance in nodes.items():
        heartbeat(client, name, instance, gpus)


def beat(client, instance, active=(), name="kayle"):
    """A heartbeat listing the attempts this Worker instance still executes."""
    response = client.post(
        f"/internal/workers/{name}/heartbeat",
        headers=worker_headers(),
        json={"instance_id": instance, "active_attempts": list(active)},
    )
    assert response.status_code == 200, response.text


def lapse(client, clock):
    """Let the node's heartbeats expire and run the Scheduler loop once."""
    service = client.app.state.scheduler
    clock[0] += service.settings.offline_timeout_s + 1
    service.tick()


def restart(client, name="kayle", cpu=2, memory=1024):
    """A new Worker instance registers and finds nothing locally: its containers are gone."""
    instance = uuid.uuid4().hex
    response = client.post(
        "/internal/workers/register",
        headers=worker_headers(),
        json={
            "worker_id": name,
            "instance_id": instance,
            "capacity": {
                "total_cpu": cpu,
                "total_memory_mib": memory + 512,
                "cpu": cpu,
                "memory_mib": memory,
                "datasets": [],
                "gpus": [],
            },
            "inventory": {},
        },
    )
    assert response.status_code == 200, response.text
    return instance


def preempting(client, clock, **fields):
    """L (preemptible, priority 1) fills kayle; a minute later H (priority 9) needs it all."""
    instance = register(client, "kayle", cpu=2)
    victim = run(client, low(client, **fields), instance)
    advance(client, clock, 60, {"kayle": instance})
    return instance, victim, high(client)


def assert_not_preempted(client, instance, running, waiting):
    assert job(client, running)["state"] == "RUNNING"
    assert stops(client, instance) == []
    value = job(client, waiting)
    assert value["state"] == "QUEUED" and value["attempts"] == []


# 1. Validation ---------------------------------------------------------------------------------


def post_job(client, **fields):
    return client.post("/api/jobs", headers=user_headers(), json={"argv": ["true"], **fields})


def messages(response):
    detail = response.json()["detail"]
    return detail if isinstance(detail, str) else " ".join(error["msg"] for error in detail)


def test_rerun_fields_default_off(client):
    spec = post_job(client).json()["spec"]
    assert spec["safe_to_rerun"] is False
    assert spec["preemptible"] is False
    assert spec["retries_on_node_failure"] == 0


@pytest.mark.parametrize(
    "fields, named",
    [
        ({"preemptible": True}, "preemptible"),
        ({"preemptible": True, "safe_to_rerun": False}, "preemptible"),
        ({"retries_on_node_failure": 2}, "retries_on_node_failure"),
        # An explicit safe_to_rerun wins over the legacy mapping, so this is the same mistake.
        ({"max_retries": 2, "safe_to_rerun": False}, "retries_on_node_failure"),
    ],
)
def test_preemptible_or_node_failure_retries_require_safe_to_rerun(client, fields, named):
    response = post_job(client, **fields)
    assert response.status_code == 422
    assert named in messages(response) and "safe_to_rerun" in messages(response)


def test_rerun_fields_are_accepted_with_safe_to_rerun(client):
    response = post_job(client, safe_to_rerun=True, preemptible=True, retries_on_node_failure=10)
    assert response.status_code == 201, response.text
    spec = response.json()["spec"]
    assert (spec["safe_to_rerun"], spec["preemptible"], spec["retries_on_node_failure"]) == (
        True,
        True,
        10,
    )
    assert post_job(client, safe_to_rerun=True, retries_on_node_failure=11).status_code == 422


def test_legacy_max_retries_is_mapped_to_retries_on_node_failure(client):
    response = post_job(client, max_retries=2)
    assert response.status_code == 201, response.text
    spec = response.json()["spec"]
    assert spec["retries_on_node_failure"] == 2 and spec["safe_to_rerun"] is True
    assert spec["preemptible"] is False and "max_retries" not in spec
    spec = post_job(client, max_retries=0).json()["spec"]
    assert spec["retries_on_node_failure"] == 0 and spec["safe_to_rerun"] is False


def test_giving_both_retry_field_names_is_rejected(client):
    response = post_job(client, safe_to_rerun=True, max_retries=1, retries_on_node_failure=1)
    assert response.status_code == 422


def test_checkpoint_is_a_path_relative_to_the_workspace(client):
    for path in ("ckpt/", "ckpt/model.pt", "./runs/ckpt"):
        response = post_job(client, checkpoint=path)
        assert response.status_code == 201, response.text
        assert response.json()["spec"]["checkpoint"] == path
    assert post_job(client).json()["spec"]["checkpoint"] is None


@pytest.mark.parametrize("path", ["/workspace/ckpt", "../ckpt", "ckpt/../../etc", ".", ""])
def test_checkpoint_outside_the_workspace_is_rejected(client, path):
    response = post_job(client, checkpoint=path)
    assert response.status_code == 422
    assert "checkpoint" in messages(response)


# 2. The whole round under the priority policy --------------------------------------------------


def test_higher_priority_job_gets_the_node_only_after_the_victim_is_confirmed_stopped(
    make_client, clock
):
    client = make_client("priority")
    instance, victim, preemptor = preempting(client, clock)
    value = job(client, victim)
    [attempt] = value["attempts"]
    assert value["state"] == "STOPPING" and attempt["state"] == "STOPPING"
    assert attempt["preempted_by"] == preemptor["id"]
    assert not value["cancel_requested"]
    assert f"preempted by job {short(preemptor)}" in value["reason"]
    assert "waiting for confirmed container termination" in value["reason"]
    [command] = commands(client, instance)
    assert command["kind"] == "STOP" and command["id"] == f"stop-{attempt['id']}"
    assert command["attempt_id"] == attempt["id"]
    waiting = job(client, preemptor)
    assert waiting["state"] == "QUEUED" and waiting["attempts"] == []
    assert short(victim) in waiting["schedule_note"] and "kayle" in waiting["schedule_note"]

    # Another scheduling pass hands nothing over before termination is confirmed.
    advance(client, clock, 5, {"kayle": instance})
    assert job(client, preemptor)["state"] == "QUEUED"
    assert starts(client, instance) == []

    stopped(client, victim, instance)
    value = job(client, victim)
    [preempted] = value["attempts"]
    assert preempted["state"] == "PREEMPTED" and preempted["released"]
    assert preempted["preempted_by"] == preemptor["id"]
    assert value["state"] == "QUEUED"
    assert value["submission_seq"] == victim["submission_seq"]
    assert f"preempted by job {short(preemptor)}" in value["reason"]
    assert "1 of 3" in value["reason"]
    started = job(client, preemptor)
    assert started["state"] == "STARTING" and started["attempts"][0]["node_id"] == "kayle"
    assert starts(client, instance) == [started["current_attempt_id"]]

    # The victim runs again from the start, in a new attempt, once the node is free.
    assert finish(client, started, instance).status_code == 200
    value = job(client, victim)
    assert value["state"] == "STARTING" and len(value["attempts"]) == 2
    rerun = value["attempts"][-1]
    assert rerun["preempted_by"] is None
    assert starts(client, instance) == [rerun["id"]]


# 3. When nothing is preempted ------------------------------------------------------------------


def test_job_not_marked_preemptible_is_not_stopped(make_client, clock):
    client = make_client("priority")
    instance = register(client, "kayle", cpu=2)
    running = submit(client, cpu=2, priority=1, safe_to_rerun=True, time_limit_s=86400)
    running = run(client, running, instance)
    advance(client, clock, 60, {"kayle": instance})
    assert_not_preempted(client, instance, running, high(client))


@pytest.mark.parametrize("victim_priority", [5, 7])
def test_job_with_equal_or_higher_priority_is_not_stopped(make_client, clock, victim_priority):
    client = make_client("priority")
    instance = register(client, "kayle", cpu=2)
    running = run(client, low(client, priority=victim_priority), instance)
    advance(client, clock, 60, {"kayle": instance})
    assert_not_preempted(client, instance, running, high(client, priority=5))


def test_batch_job_does_not_preempt_under_fcfs(make_client, clock):
    client = make_client("fcfs")
    instance = register(client, "kayle", cpu=2)
    running = run(client, low(client), instance)
    advance(client, clock, 60, {"kayle": instance})
    assert_not_preempted(client, instance, running, high(client))


@pytest.mark.parametrize("preemptor", ["batch", "session"])
def test_nothing_is_preempted_when_preemption_is_disabled(make_client, clock, preemptor):
    client = make_client("priority", preemption_enabled=False)
    instance = register(client, "kayle", cpu=2)
    running = run(client, low(client), instance)
    advance(client, clock, 60, {"kayle": instance})
    waiting = high(client) if preemptor == "batch" else open_session(client, resources={"cpu": 2})
    assert_not_preempted(client, instance, running, waiting)


@pytest.mark.parametrize("waited_min, preempted", [(10, True), (200, False)])
def test_victim_that_would_rank_first_again_once_queued_is_not_stopped(
    make_client, clock, waited_min, preempted
):
    """Ping-pong rule. L's priority 4 is below H's 5, but the policy must also rank L behind H
    if L were queued now. Score = 100 * priority + minutes since submission - recent usage
    minutes, and L has used the whole cluster for min(waited, 60) minutes:
    after 10 minutes L scores 400 + 10 - 10 = 400 < 500, so it is preempted;
    after 200 minutes L scores 400 + 200 - 60 = 540 > 500, so it would take the node back."""
    client = make_client("priority")
    instance = register(client, "kayle", cpu=2)
    running = run(client, low(client, priority=4), instance)
    advance(client, clock, waited_min * 60, {"kayle": instance})
    waiting = high(client, priority=5)
    if preempted:
        assert stops(client, instance) == [running["current_attempt_id"]]
    else:
        assert_not_preempted(client, instance, running, waiting)


def test_job_preempted_the_maximum_number_of_times_is_not_chosen_again(make_client, clock):
    client = make_client("priority", max_preemptions_per_job=1)
    instance, victim, first = preempting(client, clock)
    stopped(client, victim, instance)
    assert "1 of 1" in job(client, victim)["reason"]
    assert finish(client, job(client, first), instance).status_code == 200
    rerun = run(client, victim, instance)
    advance(client, clock, 60, {"kayle": instance})
    second = high(client)
    assert_not_preempted(client, instance, rerun, second)
    # It is never failed for it: it runs to completion and the waiting job follows.
    assert finish(client, job(client, victim), instance).status_code == 200
    assert job(client, victim)["state"] == "SUCCEEDED"
    assert job(client, second)["state"] == "STARTING"


# 4. Interactive sessions -----------------------------------------------------------------------


@pytest.mark.parametrize("policy", ["fcfs", "priority", "backfill"])
def test_session_preempts_a_preemptible_batch_job_of_any_priority(make_client, clock, policy):
    client = make_client(policy)
    instance = register(client, "kayle", cpu=2)
    running = run(client, low(client, priority=9), instance)
    advance(client, clock, 60, {"kayle": instance})
    session = open_session(client, resources={"cpu": 2})
    assert session["state"] == "QUEUED"
    value = job(client, running)
    assert value["state"] == "STOPPING"
    assert f"preempted by job {short(session)}" in value["reason"]
    assert stops(client, instance) == [value["current_attempt_id"]]
    stopped(client, running, instance)
    assert job(client, running)["state"] == "QUEUED"
    session = job(client, session)
    assert session["state"] == "STARTING"
    [command] = [c for c in commands(client, instance) if c["kind"] == "START"]
    assert command["attempt_id"] == session["current_attempt_id"]
    assert command["payload"]["session"] is True


def test_batch_job_never_preempts_a_running_session(make_client, clock):
    client = make_client("priority")
    instance = register(client, "kayle", cpu=2)
    session = run(client, open_session(client, resources={"cpu": 2}), instance)
    advance(client, clock, 60, {"kayle": instance})
    assert_not_preempted(client, instance, session, high(client))


# 5. Victim choice ------------------------------------------------------------------------------


def test_of_two_candidates_the_one_with_less_lost_work_is_stopped(make_client, clock):
    client = make_client("priority")
    instance = register(client, "kayle", cpu=2)
    nodes = {"kayle": instance}
    older = run(client, low(client, cpu=1), instance)
    advance(client, clock, 600, nodes)
    newer = run(client, low(client, cpu=1), instance)
    advance(client, clock, 60, nodes)
    high(client, cpu=1)  # One victim is enough.
    assert stops(client, instance) == [newer["current_attempt_id"]]
    assert job(client, older)["state"] == "RUNNING"


def saved(client, value, instance, at, name="kayle"):
    """The Worker's heartbeat reports the newest file time under the job's checkpoint path."""
    response = client.post(
        f"/internal/workers/{name}/heartbeat",
        headers=worker_headers(),
        json={"instance_id": instance, "checkpoints": {value["current_attempt_id"]: at}},
    )
    assert response.status_code == 200, response.text


def test_a_job_that_saved_a_checkpoint_recently_is_stopped_first(make_client, clock):
    """11.9: lost work counts from the last checkpoint. The job that started ten minutes
    earlier but saved 10 seconds ago loses less than the one that never saved."""
    client = make_client("priority")
    instance = register(client, "kayle", cpu=2)
    nodes = {"kayle": instance}
    saving = run(client, low(client, cpu=1, checkpoint="ckpt/"), instance)
    advance(client, clock, 600, nodes)
    never = run(client, low(client, cpu=1), instance)
    advance(client, clock, 60, nodes)
    saved(client, saving, instance, clock[0] - 10)
    high(client, cpu=1)  # One victim is enough.
    assert stops(client, instance) == [saving["current_attempt_id"]]
    assert job(client, never)["state"] == "RUNNING"
    # labflux status shows when the stopped attempt last saved.
    assert job(client, saving)["attempts"][0]["checkpoint_at"] == clock[0] - 10
    assert job(client, never)["attempts"][0]["checkpoint_at"] is None


def test_a_checkpoint_older_than_the_attempt_saves_nothing_of_it(make_client, clock):
    """A file copied from an earlier attempt keeps its time (6.1 P3). It is older than this
    attempt's start, so lost work counts from the start, as without a checkpoint: 60 seconds,
    not the 960 since the copied file was written."""
    client = make_client("priority")
    instance = register(client, "kayle", cpu=2)
    nodes = {"kayle": instance}
    older = run(client, low(client, cpu=1), instance)
    advance(client, clock, 600, nodes)
    copied = run(client, low(client, cpu=1, checkpoint="ckpt/"), instance)
    saved(client, copied, instance, clock[0] - 900)
    advance(client, clock, 60, nodes)
    high(client, cpu=1)
    assert stops(client, instance) == [copied["current_attempt_id"]]
    assert job(client, older)["state"] == "RUNNING"


def test_node_with_less_total_lost_work_is_chosen(make_client, clock):
    client = make_client("priority")
    # Datasets pin each victim to its node; the preemptor needs none, so either node would do.
    # Node id order alone would pick karma.
    nodes = {
        "karma": register(client, "karma", cpu=1, datasets=["early"]),
        "kayle": register(client, "kayle", cpu=1, datasets=["late"]),
    }
    older = run(client, low(client, cpu=1, datasets=["early"]), nodes["karma"])
    advance(client, clock, 600, nodes)
    newer = run(client, low(client, cpu=1, datasets=["late"]), nodes["kayle"])
    advance(client, clock, 60, nodes)
    waiting = high(client, cpu=1)
    assert stops(client, nodes["karma"], "karma") == []
    assert stops(client, nodes["kayle"], "kayle") == [newer["current_attempt_id"]]
    stopped(client, newer, nodes["kayle"])
    assert job(client, waiting)["attempts"][0]["node_id"] == "kayle"
    assert job(client, older)["state"] == "RUNNING"


def test_only_a_node_with_the_preemptors_dataset_is_considered(make_client, clock):
    client = make_client("priority")
    nodes = {
        "karma": register(client, "karma", cpu=1),
        "kayle": register(client, "kayle", cpu=1, datasets=["mnist"]),
    }
    older = run(client, low(client, cpu=1, datasets=["mnist"]), nodes["kayle"])
    advance(client, clock, 600, nodes)
    # Started later on karma, so it would be chosen if datasets did not matter.
    newer = run(client, low(client, cpu=1), nodes["karma"])
    advance(client, clock, 60, nodes)
    waiting = high(client, cpu=1, datasets=["mnist"])
    assert stops(client, nodes["karma"], "karma") == []
    assert stops(client, nodes["kayle"], "kayle") == [older["current_attempt_id"]]
    assert job(client, newer)["state"] == "RUNNING"
    stopped(client, older, nodes["kayle"])
    assert job(client, waiting)["attempts"][0]["node_id"] == "kayle"


def test_gpu_request_stops_the_job_holding_a_matching_card(make_client, clock):
    client = make_client("priority")
    cards = [RTX3090, A5000]
    instance = register(client, "kayle", cpu=8, memory=8192, gpus=cards)
    heartbeat(client, "kayle", instance, cards)
    nodes = {"kayle": instance}
    on_3090 = run(client, low(client, resources={"gpu": 1, "gpu_model": "3090"}), instance)
    advance(client, clock, 600, nodes, cards)
    # Started later, so it would be chosen first if the cards did not matter.
    on_a5000 = run(client, low(client, resources={"gpu": 1, "gpu_model": "a5000"}), instance)
    advance(client, clock, 60, nodes, cards)
    waiting = high(client, resources={"gpu": 1, "gpu_model": "3090"})
    assert stops(client, instance) == [on_3090["current_attempt_id"]]
    assert job(client, on_a5000)["state"] == "RUNNING"
    stopped(client, on_3090, instance)
    started = job(client, waiting)
    assert started["state"] == "STARTING"
    assert started["attempts"][0]["gpu_uuids"] == [RTX3090["uuid"]]


# 6. One round at a time ------------------------------------------------------------------------


def test_no_second_preemption_starts_while_one_is_in_progress(make_client, clock):
    client = make_client("priority")
    nodes = {
        "karma": register(client, "karma", cpu=1, datasets=["early"]),
        "kayle": register(client, "kayle", cpu=1, datasets=["late"]),
    }
    older = run(client, low(client, cpu=1, datasets=["early"]), nodes["karma"])
    advance(client, clock, 600, nodes)
    newer = run(client, low(client, cpu=1, datasets=["late"]), nodes["kayle"])
    advance(client, clock, 60, nodes)
    high(client, cpu=1, priority=8)
    assert stops(client, nodes["kayle"], "kayle") == [newer["current_attempt_id"]]
    # The new head could preempt the older job on karma, but must wait for the round in progress.
    second = high(client, cpu=1, priority=9)
    advance(client, clock, 5, nodes)
    assert stops(client, nodes["karma"], "karma") == []
    assert job(client, older)["state"] == "RUNNING"
    waiting = job(client, second)
    assert waiting["state"] == "QUEUED" and waiting["attempts"] == []
    assert short(newer) in waiting["schedule_note"]
    # Once that round ends, the job still blocked may start the next one.
    stopped(client, newer, nodes["kayle"])
    assert job(client, older)["state"] == "STOPPING"


def test_only_the_first_job_in_line_may_preempt(make_client, clock):
    """No round is in progress here, so only the head-only rule keeps L running: H, first in
    line, cannot fit even with every preemptible job stopped, because a job that is not
    preemptible holds half of the node; M, behind H, would fit if L were stopped."""
    client = make_client("priority")
    instance = register(client, "kayle", cpu=2)
    nodes = {"kayle": instance}
    pinned = run(client, submit(client, cpu=1, priority=1, time_limit_s=86400), instance)
    victim = run(client, low(client, cpu=1), instance)
    advance(client, clock, 60, nodes)
    head = high(client, cpu=2, priority=9)
    later = high(client, cpu=1, priority=5)
    advance(client, clock, 5, nodes)
    assert stops(client, instance) == []
    assert job(client, pinned)["state"] == "RUNNING"
    value = job(client, victim)
    assert value["state"] == "RUNNING" and value["attempts"][0]["preempted_by"] is None
    for waiting in (head, later):
        waiting = job(client, waiting)
        assert waiting["state"] == "QUEUED" and waiting["attempts"] == []
    assert short(head) in job(client, later)["schedule_note"]

    # M itself may preempt L: once H is gone, M is first in line and L is stopped for it.
    response = client.post(f"/api/jobs/{head['id']}/cancel", headers=user_headers("bob"))
    assert response.status_code == 200, response.text
    value = job(client, victim)
    assert value["state"] == "STOPPING"
    assert value["attempts"][0]["preempted_by"] == later["id"]
    assert stops(client, instance) == [victim["current_attempt_id"]]
    assert job(client, pinned)["state"] == "RUNNING"


# 7. The victim ends some other way during the stop ---------------------------------------------


@pytest.mark.parametrize("outcome, exit_code", [("SUCCEEDED", 0), ("FAILED", 3)])
def test_victim_that_ends_on_its_own_during_the_stop_keeps_its_outcome(
    make_client, clock, outcome, exit_code
):
    client = make_client("priority")
    instance, victim, preemptor = preempting(client, clock)
    response = finish(client, job(client, victim), instance, outcome=outcome, exit_code=exit_code)
    assert response.status_code == 200, response.text
    value = job(client, victim)
    assert value["state"] == outcome
    assert [a["state"] for a in value["attempts"]] == [outcome]
    assert job(client, preemptor)["state"] == "STARTING"


def test_victim_cancelled_by_its_owner_during_the_stop_is_cancelled(make_client, clock):
    client = make_client("priority")
    instance, victim, preemptor = preempting(client, clock)
    response = client.post(f"/api/jobs/{victim['id']}/cancel", headers=user_headers())
    assert response.status_code == 200 and response.json()["state"] == "STOPPING"
    assert len(stops(client, instance)) == 1
    stopped(client, victim, instance)
    value = job(client, victim)
    assert value["state"] == "CANCELLED" and len(value["attempts"]) == 1
    assert job(client, preemptor)["state"] == "STARTING"


# 8. Preemptions and node-failure retries are counted separately --------------------------------


def test_preemptions_do_not_consume_node_failure_retries(make_client, clock):
    client = make_client("priority")
    instance, victim, preemptor = preempting(client, clock, retries_on_node_failure=1)
    stopped(client, victim, instance)
    assert finish(client, job(client, preemptor), instance).status_code == 200
    run(client, victim, instance)
    restart(client)  # The rerun's node is lost together with its container.
    value = job(client, victim)
    assert [a["state"] for a in value["attempts"]] == ["PREEMPTED", "FAILED", "STARTING"]
    assert value["state"] == "STARTING"


def test_node_failure_retries_do_not_count_as_preemptions(make_client, clock):
    client = make_client("priority")
    instance = register(client, "kayle", cpu=2)
    victim = run(client, low(client, retries_on_node_failure=1), instance)
    instance = restart(client)
    run(client, victim, instance)
    advance(client, clock, 60, {"kayle": instance})
    high(client)
    stopped(client, victim, instance)
    value = job(client, victim)
    assert [a["state"] for a in value["attempts"]] == ["FAILED", "PREEMPTED"]
    assert value["state"] == "QUEUED" and "1 of 3" in value["reason"]


# 9. Backfill -----------------------------------------------------------------------------------


def test_backfill_does_not_hand_resources_being_freed_to_a_later_job(make_client, clock):
    client = make_client("backfill")
    instance = register(client, "kayle", cpu=2)
    nodes = {"kayle": instance}
    first = run(client, low(client, cpu=1), instance)
    advance(client, clock, 60, nodes)
    second = run(client, low(client, cpu=1), instance)
    advance(client, clock, 60, nodes)
    preemptor = high(client, cpu=2)  # Needs both victims stopped.
    victims = {first["current_attempt_id"], second["current_attempt_id"]}
    assert set(stops(client, instance)) == victims
    # It would end long before the victims' own hard limits, so plain EASY would let it in.
    later = submit(client, cpu=1, user="bob", time_limit_s=600)
    stopped(client, first, instance)  # One CPU is free; the preemptor needs the other one too.
    value = job(client, later)
    assert value["state"] == "QUEUED" and value["attempts"] == []
    assert len(job(client, first)["attempts"]) == 1  # Nor does the requeued victim take it.
    assert job(client, preemptor)["state"] == "QUEUED"
    stopped(client, second, instance)
    assert job(client, preemptor)["state"] == "STARTING"
    assert job(client, later)["state"] == "QUEUED"


def test_backfill_does_not_hand_the_room_beside_a_cancel_in_progress_to_a_later_job(
    make_client, clock
):
    """Bob's cancelled job is stopping, so the head waits for it instead of preempting. It
    needs that CPU and the free one: a short job in the free CPU, ending long before the
    cancelled job's hard limit, would leave the head blocked again once the stop is confirmed."""
    client = make_client("backfill")
    instance = register(client, "kayle", cpu=2)
    old = run(client, high(client, cpu=1, time_limit_s=86400), instance)
    response = client.post(f"/api/jobs/{old['id']}/cancel", headers=user_headers("bob"))
    assert response.status_code == 200 and response.json()["state"] == "STOPPING"
    head = high(client, cpu=2)
    later = submit(client, cpu=1, time_limit_s=600)
    waiting = job(client, head)
    assert waiting["state"] == "QUEUED" and waiting["attempts"] == []
    assert f"waiting for job {short(old)} on kayle to stop" in waiting["schedule_note"]
    assert "reserved on kayle as soon as they stop" in waiting["schedule_note"]
    value = job(client, later)
    assert value["state"] == "QUEUED" and value["attempts"] == []
    stopped(client, old, instance)
    assert job(client, head)["state"] == "STARTING"
    assert job(client, later)["state"] == "QUEUED"


# 10. Heartbeat lapses and Worker restarts during the stop --------------------------------------


def test_heartbeat_lapse_during_the_stop_keeps_the_attempt_stopping(make_client, clock):
    client = make_client("priority")
    instance, victim, preemptor = preempting(client, clock)
    attempt_id = job(client, victim)["current_attempt_id"]
    lapse(client, clock)
    assert job(client, victim)["state"] == "UNKNOWN"
    beat(client, instance, active=[attempt_id])  # The same instance is back and still runs it.
    value = job(client, victim)
    assert value["state"] == "STOPPING"
    [attempt] = value["attempts"]
    assert attempt["state"] == "STOPPING" and attempt["preempted_by"] == preemptor["id"]
    assert stops(client, instance) == [attempt_id]
    assert job(client, preemptor)["state"] == "QUEUED"
    stopped(client, victim, instance)
    assert job(client, victim)["attempts"][0]["state"] == "PREEMPTED"
    assert job(client, preemptor)["state"] == "STARTING"


def test_start_reported_after_a_lapse_during_the_stop_does_not_resume_the_victim(
    make_client, clock
):
    client = make_client("priority")
    instance = register(client, "kayle", cpu=2)
    victim = low(client)  # Dispatched; its container has not reported STARTED yet.
    advance(client, clock, 60, {"kayle": instance})
    high(client)
    assert job(client, victim)["state"] == "STOPPING"
    attempt_id = job(client, victim)["current_attempt_id"]
    lapse(client, clock)
    beat(client, instance)  # Back, but not listing the attempt: it stays UNKNOWN.
    response = client.post(
        f"/internal/workers/kayle/attempts/{attempt_id}/events",
        headers=worker_headers(),
        json={"event_id": "late-start", "instance_id": instance, "kind": "STARTED"},
    )
    assert response.status_code == 200, response.text
    assert job(client, victim)["state"] == "STOPPING"


def test_worker_restart_with_the_victims_container_gone_counts_as_preempted(make_client, clock):
    client = make_client("priority")
    _, victim, preemptor = preempting(client, clock)
    restart(client)
    value = job(client, victim)
    assert [a["state"] for a in value["attempts"]] == ["PREEMPTED"]
    assert value["state"] == "QUEUED" and "1 of 3" in value["reason"]
    assert job(client, preemptor)["state"] == "STARTING"
