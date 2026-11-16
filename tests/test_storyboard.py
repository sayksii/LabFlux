"""M12 acceptance: the M14 demo storyboard, replayed scene by scene through the HTTP API.

Each test plays one scene of the demo (scenes 1-5 and 7 of the M12 design proposal) under the
backfill policy, on three fake nodes shaped like the lab machines, with a fake clock and fake
Workers, and checks what the Dashboard shows the audience: GET /api/queue (decisions, blockers,
the reservation, scores, recent decisions), the allocations in GET /api/nodes and the jobs'
reasons. Each scene checks its placements with the job attempts first, so a scheduling change
that moves the demo's jobs fails at the setup instead of at the explanation: rehearse the demo
again when that happens.

The nodes sort like the lab nodes, because best-fit ties fall back to the node id:
    beta   16 CPU, 24 GiB, RTX 4090
    delta  16 CPU, 48 GiB, RTX 3090 + RTX A5000
    gamma  32 CPU, 64 GiB, two A40s and the dataset hf-models; the persistent model gemma4-31b
           holds 8 CPU, 48 GiB and both A40s there
"""

import uuid

import pytest
from conftest import finish, heartbeat, user_headers, worker_headers
from fastapi.testclient import TestClient

from labflux.api import create_app
from labflux.config import Settings

T0 = 1_794_808_800.0  # 2026-11-16 14:00:00 +08, the storyboard's clock


def card(number, name, memory_mib, index):
    return {
        "uuid": f"GPU-0000000{number}-0000-0000-0000-000000000000",
        "index": index,
        "name": name,
        "memory_total_mib": memory_mib,
    }


RTX3090 = card(1, "NVIDIA GeForce RTX 3090", 24576, 0)
A5000 = card(2, "NVIDIA RTX A5000", 24564, 1)
RTX4090 = card(3, "NVIDIA GeForce RTX 4090", 24564, 0)
A40S = [card(4, "NVIDIA A40", 46068, 3), card(5, "NVIDIA A40", 46068, 4)]
NODES = {
    "beta": {"cpu": 16, "memory": 24576, "gpus": [RTX4090], "datasets": []},
    "delta": {"cpu": 16, "memory": 49152, "gpus": [RTX3090, A5000], "datasets": []},
    "gamma": {"cpu": 32, "memory": 65536, "gpus": A40S, "datasets": ["hf-models"]},
}
GEMMA = {
    "image": "vllm/vllm-openai:v0.30.0",
    "argv": ["--model", "google/gemma-4-31b"],
    "mode": "persistent",
    "resources": {"cpu": 8, "memory_mib": 49152, "gpu": 2, "gpu_model": "A40"},
    "datasets": ["hf-models"],
    "startup_timeout_s": 600,
}
PREEMPTIBLE = {"safe_to_rerun": True, "preemptible": True}


class Lab:
    """The three nodes, their Workers and the users alice and bob, all through the HTTP API."""

    def __init__(self, client, clock):
        self.client, self.clock = client, clock
        self.service = client.app.state.scheduler
        self.instances = {}
        for name in NODES:
            self.register(name)
        self.beat()
        self.settle()  # gemma4-31b starts on gamma

    # The Workers ------------------------------------------------------------------------------

    def register(self, name, inventory=None):
        """A Worker instance registers; a restarted one reports what it found locally."""
        node = NODES[name]
        self.instances[name] = uuid.uuid4().hex
        body = {
            "worker_id": name,
            "instance_id": self.instances[name],
            "capacity": {
                "total_cpu": node["cpu"],
                "total_memory_mib": node["memory"] + 512,
                "cpu": node["cpu"],
                "memory_mib": node["memory"],
                "datasets": node["datasets"],
                "gpus": node["gpus"],
            },
        }
        if inventory is not None:
            body["inventory"] = inventory
        response = self.client.post(
            "/internal/workers/register", headers=worker_headers(), json=body
        )
        assert response.status_code == 200, response.text

    def beat(self, skip=()):
        for name, instance in self.instances.items():
            if name not in skip:
                heartbeat(self.client, name, instance, NODES[name]["gpus"])

    def advance(self, seconds, skip=()):
        """Move the clock; every node not skipped keeps sending heartbeats."""
        self.clock[0] += seconds
        self.beat(skip)
        self.service.tick()

    def work(self):
        """Each Worker carries out its commands: a START is reported STARTED, a STOP confirmed."""
        for name, instance in self.instances.items():
            response = self.client.get(
                f"/internal/workers/{name}/commands",
                params={"instance_id": instance},
                headers=worker_headers(),
            )
            assert response.status_code == 200, response.text
            for command in response.json():
                if command["kind"] == "START":
                    event = {"kind": "STARTED"}
                elif command["kind"] == "STOP":
                    event = {
                        "kind": "FINISHED",
                        "outcome": "CANCELLED",
                        "exit_code": 137,
                        "termination_confirmed": True,
                        "reason": "stopped",
                    }
                else:
                    continue
                attempt = command["attempt_id"]
                response = self.client.post(
                    f"/internal/workers/{name}/commands/{command['id']}/ack",
                    headers=worker_headers(),
                    json={"instance_id": instance},
                )
                assert response.status_code == 200, response.text
                response = self.client.post(
                    f"/internal/workers/{name}/attempts/{attempt}/events",
                    headers=worker_headers(),
                    json={"event_id": f"{command['id']}-done", "instance_id": instance, **event},
                )
                assert response.status_code == 200, response.text

    def settle(self):
        """A few Scheduler loops, each followed by the Workers' replies."""
        for _ in range(3):
            self.service.tick()
            self.work()

    def done(self, value):
        """The job's container exits successfully."""
        job = self.job(value)
        response = finish(self.client, job, self.instances[job["attempts"][-1]["node_id"]])
        assert response.status_code == 200, response.text

    # The users --------------------------------------------------------------------------------

    def submit(self, user="alice", cpu=1, memory_gib=1, gpu=0, limit=300, **fields):
        resources = {"cpu": cpu, "memory_mib": memory_gib * 1024, "gpu": gpu}
        for key in ("gpu_model", "min_vram_mib"):
            if key in fields:
                resources[key] = fields.pop(key)
        response = self.client.post(
            "/api/jobs",
            headers=user_headers(user),
            json={
                "image": "python:3.12-slim",
                "argv": ["sleep", "infinity"],
                "resources": resources,
                "time_limit_s": limit,
                **fields,
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    def job(self, value):
        response = self.client.get(f"/api/jobs/{value['id']}", headers=user_headers(value["owner"]))
        assert response.status_code == 200, response.text
        return response.json()

    def queue(self, viewer="alice"):
        response = self.client.get("/api/queue", headers=user_headers(viewer))
        assert response.status_code == 200, response.text
        return response.json()

    def nodes(self, viewer="alice"):
        response = self.client.get("/api/nodes", headers=user_headers(viewer))
        assert response.status_code == 200, response.text
        return {node["id"]: node for node in response.json()}

    def allocations(self, node, viewer="alice"):
        return {item["job_id"]: item for item in self.nodes(viewer)[node]["allocations"]}


@pytest.fixture
def lab(tmp_path):
    clock = [T0]
    settings = Settings(
        data_dir=tmp_path,
        users={"alice": "alice-token", "bob": "bob-token"},
        scheduling_policy="backfill",
        models={"gemma4-31b": GEMMA},
    )
    app = create_app(settings, run_loop=False, clock=lambda: clock[0])
    with TestClient(app) as client:
        yield Lab(client, clock)


def assert_running(lab, value, node, cards=()):
    """The job runs on node with exactly these cards; returns the job."""
    job = lab.job(value)
    attempt = job["attempts"][-1] if job["attempts"] else {}
    placed = (job["state"], attempt.get("node_id"), attempt.get("gpu_uuids"))
    assert placed == ("RUNNING", node, [gpu["uuid"] for gpu in cards]), job["schedule_note"]
    return job


def order(queue):
    return [item["job_id"] for item in queue["entries"]]


def entry(queue, value):
    return next(item for item in queue["entries"] if item["job_id"] == value["id"])


def decisions(queue, kind, value=None):
    """The recent decisions of one kind, newest first, optionally about one job only."""
    return [
        item
        for item in queue["recent_decisions"]
        if item["kind"] == kind and (value is None or item["job_id"] == value["id"])
    ]


def pick(value, expected):
    """The fields of value that expected names, for a readable comparison."""
    return {key: value.get(key) for key in expected}


def arrivals(lab):
    """Scene 1's six jobs from alice, in the order the demo script sends them."""
    jobs = {}
    jobs["4090"] = lab.submit(cpu=4, memory_gib=8, gpu=1, gpu_model="4090", limit=1800)
    jobs["vram20"] = lab.submit(cpu=4, memory_gib=8, gpu=1, min_vram_mib=20480, limit=1800)
    jobs["ram40"] = lab.submit(cpu=4, memory_gib=40, limit=1800)
    for index in range(3):
        jobs[f"cpu8-{index}"] = lab.submit(cpu=8, memory_gib=4, limit=1800)
    lab.settle()
    return jobs


def test_scene_1_arrivals_land_on_the_nodes_that_fit_them(lab):
    """Scene 1, job arrivals and heterogeneous placement: alice sends six jobs at once.

    The audience sees each block land in the column that can hold it: the RTX 4090 job on beta
    (the only 4090), the job needing 20 GiB of VRAM on delta's smaller A5000, the 40 GiB RAM job
    on delta (gamma has 64 GiB, but gemma holds 48), and the 8-core jobs spread by spare
    capacity, while gemma4-31b keeps both A40s on gamma. Opening a job shows which nodes could
    take it and why the chosen one won; bob sees alice's blocks too, without her details.
    """
    models = lab.client.get("/api/models", headers=user_headers()).json()
    assert [(m["name"], m["state"], m["node_id"]) for m in models] == [
        ("gemma4-31b", "serving", "gamma")
    ]
    jobs = arrivals(lab)
    placed = {
        "4090": ("beta", [RTX4090]),
        "vram20": ("delta", [A5000]),
        "ram40": ("delta", []),
        "cpu8-0": ("beta", []),
        "cpu8-1": ("gamma", []),
        "cpu8-2": ("gamma", []),
    }
    for label, (node, cards) in placed.items():
        assert_running(lab, jobs[label], node, cards)

    # Why each node won (M12 placement note).
    note = lab.job(jobs["cpu8-0"])["schedule_note"]
    assert note.startswith("backfill: started on beta (score 500 ")
    assert "fits on beta, gamma; chose beta" in note
    note = lab.job(jobs["ram40"])["schedule_note"]
    assert note.startswith("backfill: started on delta (score 500 ")
    assert "only delta fits" in note

    queue = lab.queue()
    assert (queue["policy"], queue["entries"], queue["reservation"]) == ("backfill", [], None)
    started = {item["job_id"]: item for item in decisions(queue, "started")}
    for label, (node, _) in placed.items():
        told = started[jobs[label]["id"]]
        assert (told["owner"], told["node_id"], bool(told["text"])) == ("alice", node, True)

    # Every logged-in user sees every allocation (decision 1A).
    gamma = lab.allocations("gamma", viewer="bob")
    [gemma] = [item for item in gamma.values() if item["kind"] == "MODEL"]
    want = {"service": "gemma4-31b", "mine": False, "cpu": 8, "memory_mib": 49152}
    assert pick(gemma, want) == want
    assert sorted(gemma["gpu_uuids"]) == [gpu["uuid"] for gpu in A40S]
    want = {
        "owner": "alice",
        "mine": False,
        "kind": "BATCH",
        "service": None,
        "state": "RUNNING",
        "attempt_state": "RUNNING",
        "cpu": 4,
        "memory_mib": 40960,
        "gpu_uuids": [],
        "started_at": T0,
        "ends_at": T0 + 1800,
        "placement": "started",
        "preempted_by": None,
    }
    assert pick(lab.allocations("delta", viewer="bob")[jobs["ram40"]["id"]], want) == want
    vram20 = lab.allocations("delta")[jobs["vram20"]["id"]]
    assert (vram20["mine"], vram20["gpu_uuids"]) == (True, [A5000["uuid"]])


def test_scene_2_priority_jumps_the_queue_and_usage_reorders_equal_priorities(lab):
    """Scene 2, priority jump and fairness: with the cluster loaded by scene 1, bob sends a
    priority 9 job of 16 cores; then alice and bob each send a priority 5 job of 12 cores that
    fits nowhere, alice a minute first.

    The audience sees bob's priority 9 job first in line with a reservation, and bob's priority
    5 job ahead of alice's earlier one: the score breakdown shows alice's extra minute of
    waiting outweighed by her recent usage (500 + 1 - 6 against 500 + 0 - 0), and the usage
    table shows that alice used more of the last 60 minutes.
    """
    arrivals(lab)
    lab.advance(240)
    lab.work()
    urgent = lab.submit("bob", cpu=16, memory_gib=8, priority=9)
    lab.settle()
    lab.advance(30)
    alice = lab.submit("alice", cpu=12, memory_gib=4, priority=5, limit=600)
    lab.advance(60)
    bob = lab.submit("bob", cpu=12, memory_gib=4, priority=5, limit=600)
    lab.settle()
    for value in (urgent, alice, bob):
        job = lab.job(value)
        assert (job["state"], job["attempts"]) == ("QUEUED", []), job["schedule_note"]
    assert (
        "score 495 = priority 5, waited 1 min, recent usage 6 min"
        in (lab.job(alice)["schedule_note"])
    )
    assert (
        "score 500 = priority 5, waited 0 min, recent usage 0 min"
        in (lab.job(bob)["schedule_note"])
    )

    queue = lab.queue()
    assert queue["computed_at"] == lab.clock[0]
    assert queue["weights"] == {"priority": 100, "wait": 1, "usage": 1}
    assert order(queue) == [urgent["id"], bob["id"], alice["id"]]
    assert [item["position"] for item in queue["entries"]] == [1, 2, 3]
    want = {
        "owner": "bob",
        "mine": False,
        "kind": "BATCH",
        "service": None,
        "priority": 9,
        "decision": "head_reserved",
        "blocker": {"kind": "cpu", "need": 16, "best_free": 8, "best_node": "delta"},
        "waiting_for": [],
    }
    assert pick(entry(queue, urgent), want) == want
    assert entry(queue, urgent)["score_parts"]["priority"] == 900
    assert queue["reservation"] == {
        "job_id": urgent["id"],
        "node_id": "beta",
        "start": T0 + 1800,
        "cpu": 16,
        "memory_mib": 8192,
        "gpu": 0,
    }
    mine, theirs = entry(queue, alice), entry(queue, bob)
    for item, owner in ((mine, "alice"), (theirs, "bob")):
        want = {
            "owner": owner,
            "mine": owner == "alice",
            "priority": 5,
            "decision": "no_capacity",
            "head_job_id": urgent["id"],
            "blocker": {"kind": "cpu", "need": 12, "best_free": 8, "best_node": "delta"},
        }
        assert pick(item, want) == want
    assert theirs["score_parts"] == {"priority": 500, "waited_min": 0, "usage_min": 0}
    assert theirs["score"] == 500
    parts = mine["score_parts"]
    assert (parts["priority"], parts["waited_min"]) == (500, pytest.approx(1))
    assert parts["usage_min"] > parts["waited_min"]
    assert mine["score"] == pytest.approx(500 + 1 - parts["usage_min"])
    assert mine["score"] < theirs["score"]
    usage = queue["usage_min_by_user"]
    assert usage["alice"] == pytest.approx(parts["usage_min"])
    assert usage.get("bob", 0) == 0


def test_scene_3_backfill_runs_the_short_job_before_the_reservation(lab):
    """Scene 3, backfill: jobs sent so that delta keeps 4 free cores while beta and gamma are
    full; bob's priority 9 job of 16 cores reserves delta for when delta's two 5-minute jobs
    reach their time limit. alice then sends a 2-core job with a 30-minute limit, and a 2-core
    job with a 2-minute limit; bob a 4-core job.

    The audience sees delta reserved for bob's job from 14:05, the 2-minute job marked as
    backfilled because it ends first, the 30-minute job waiting with the decision "would delay
    the reservation", the 4-core job with no room, and bob's job landing on delta once the
    countdown runs out.
    """
    bob24 = lab.submit("bob", cpu=24, memory_gib=8, limit=3600)  # only gamma fits
    lab.settle()
    bob12 = lab.submit("bob", cpu=12, memory_gib=6, limit=2400)  # beta: less spare RAM
    lab.settle()
    alice4 = lab.submit("alice", cpu=4, memory_gib=2, limit=3600)  # fills beta
    lab.settle()
    alice8 = lab.submit("alice", cpu=8, memory_gib=8, limit=300)
    bob4 = lab.submit("bob", cpu=4, memory_gib=4, limit=300)  # delta keeps 4 cores
    lab.settle()
    for value, node in (
        (bob24, "gamma"),
        (bob12, "beta"),
        (alice4, "beta"),
        (alice8, "delta"),
        (bob4, "delta"),
    ):
        assert_running(lab, value, node)
    lab.advance(120)
    lab.work()
    head = lab.submit("bob", cpu=16, memory_gib=8, priority=9, limit=600)
    lab.settle()
    long = lab.submit("alice", cpu=2, memory_gib=4, limit=1800)  # sent before the short one
    lab.settle()
    short = lab.submit("alice", cpu=2, memory_gib=2, limit=120)
    four = lab.submit("bob", cpu=4, memory_gib=4, limit=600)
    lab.settle()
    assert_running(lab, short, "delta")
    assert lab.job(short)["schedule_note"].startswith(
        f"backfill: backfilled on delta ahead of job {head['id'][:8]} reserved on delta at "
    )
    for value in (head, long, four):
        assert lab.job(value)["state"] == "QUEUED"
    assert "1800s time limit would delay job" in lab.job(long)["schedule_note"]
    assert "no matching capacity now" in lab.job(four)["schedule_note"]

    queue = lab.queue()
    assert order(queue) == [head["id"], long["id"], four["id"]]
    assert queue["reservation"] == {
        "job_id": head["id"],
        "node_id": "delta",
        "start": T0 + 300,
        "cpu": 16,
        "memory_mib": 8192,
        "gpu": 0,
    }
    want = {
        "decision": "head_reserved",
        "blocker": {"kind": "cpu", "need": 16, "best_free": 2, "best_node": "delta"},
    }
    assert pick(entry(queue, head), want) == want
    want = {
        "mine": True,
        "decision": "would_delay_reservation",
        "head_job_id": head["id"],
        "blocker": None,
        "note": lab.job(long)["schedule_note"],
    }
    assert pick(entry(queue, long), want) == want
    want = {
        "mine": False,
        "decision": "no_capacity",
        "head_job_id": head["id"],
        "blocker": {"kind": "cpu", "need": 4, "best_free": 2, "best_node": "delta"},
    }
    assert pick(entry(queue, four), want) == want
    [backfilled] = decisions(queue, "backfilled", short)
    assert (backfilled["owner"], backfilled["node_id"]) == ("alice", "delta")
    delta = lab.allocations("delta", viewer="bob")
    blocks = {value["id"]: label for value, label in ((short, "short"), (alice8, "8"), (bob4, "4"))}
    assert {
        blocks[job_id]: (item["placement"], item["ends_at"])
        for job_id, item in delta.items()
        if job_id in blocks
    } == {
        "short": ("backfilled", T0 + 240),  # ends before the reservation at T0 + 300
        "8": ("started", T0 + 300),
        "4": ("started", T0 + 300),
    }

    # The countdown runs out: the short job ends at 14:04, delta's 5-minute jobs at 14:05.
    lab.advance(120)
    lab.done(short)
    lab.advance(60)
    lab.done(alice8)
    lab.done(bob4)
    lab.settle()
    job = assert_running(lab, head, "delta")
    assert job["schedule_note"].startswith("backfill: started on delta (score ")
    queue = lab.queue()
    assert [item["node_id"] for item in decisions(queue, "started", head)] == ["delta"]
    # The 30-minute job is first in line now, reserved for when bob's job reaches its limit.
    assert order(queue) == [long["id"], four["id"]]
    assert (queue["reservation"]["job_id"], queue["reservation"]["start"]) == (
        long["id"],
        T0 + 300 + 600,
    )


def test_scene_4_preemption_stops_the_cheapest_job_then_requeues_it(lab):
    """Scene 4, preemption: alice's preemptible priority 2 GPU jobs hold every free card (beta's
    4090 first by best fit, then delta's two); bob sends a priority 8 job that needs a card with
    20 GiB of VRAM.

    The audience sees the latest-started of alice's jobs, which loses the least work, drawn as
    "preempted by bob's job, stopping" and bob's decision "waiting for it to stop"; once the
    Worker confirms the stop, bob's block lands on the freed card and alice's job returns to
    the queue ("preempted 1 of 3 times") with a PREEMPTED attempt in its history.
    """
    low = []
    for _ in range(3):
        low.append(lab.submit(cpu=4, memory_gib=8, gpu=1, priority=2, limit=86400, **PREEMPTIBLE))
        lab.settle()
        lab.advance(30)
        lab.work()
    for value, node, gpu in zip(low, ("beta", "delta", "delta"), (RTX4090, A5000, RTX3090)):
        assert_running(lab, value, node, [gpu])
    victim = low[2]
    urgent = lab.submit(
        "bob", cpu=4, memory_gib=8, gpu=1, min_vram_mib=20480, priority=8, limit=1800
    )
    lab.service.tick()  # before the Worker confirms the stop
    job = lab.job(victim)
    assert (job["state"], job["reason"]) == (
        "STOPPING",
        f"preempted by job {urgent['id'][:8]}; waiting for confirmed container termination",
    )
    assert lab.job(urgent)["state"] == "QUEUED"
    for value in low[:2]:
        assert lab.job(value)["state"] == "RUNNING"

    queue = lab.queue()
    assert order(queue) == [urgent["id"]]
    want = {
        "decision": "waiting_for_release",
        "waiting_for": [victim["id"]],
        "blocker": {
            "kind": "gpu",
            "need": 1,
            "model": None,
            "min_vram_mib": 20480,
            "best_node": "beta",
            "free": 0,
            "held": 1,
            "external": 0,
            "unreported": 0,
        },
    }
    assert pick(entry(queue, urgent), want) == want
    [preempting] = decisions(queue, "preempting", urgent)
    assert preempting["detail"]["victims"] == [victim["id"]]
    assert preempting["detail"]["node_id"] == "delta"
    delta = lab.allocations("delta")
    want = {
        "owner": "alice",
        "mine": True,
        "state": "STOPPING",
        "attempt_state": "STOPPING",
        "gpu_uuids": [RTX3090["uuid"]],
        "preempted_by": urgent["id"],
    }
    assert pick(delta[victim["id"]], want) == want
    assert delta[low[1]["id"]]["preempted_by"] is None

    lab.settle()  # the Worker confirms the stop
    assert_running(lab, urgent, "delta", [RTX3090])
    job = lab.job(victim)
    assert (job["state"], job["reason"]) == (
        "QUEUED",
        f"preempted by job {urgent['id'][:8]}; queued to run again (preemption 1 of 3)",
    )
    assert [attempt["state"] for attempt in job["attempts"]] == ["PREEMPTED"]
    for value in low[:2]:
        assert lab.job(value)["state"] == "RUNNING"

    queue = lab.queue()
    assert [(item["job_id"], item["decision"]) for item in queue["entries"]] == [
        (victim["id"], "head_reserved")
    ]
    assert queue["reservation"] == {
        "job_id": victim["id"],
        "node_id": "delta",
        "start": T0 + 90 + 1800,  # when bob's job reaches its time limit
        "cpu": 4,
        "memory_mib": 8192,
        "gpu": 1,
    }
    told = [(item["kind"], item["job_id"]) for item in queue["recent_decisions"]]
    assert (
        told.index(("started", urgent["id"]))
        < told.index(("requeued_after_preemption", victim["id"]))
        < told.index(("preempting", urgent["id"]))
    )
    assert lab.allocations("delta")[urgent["id"]]["placement"] == "started"


def test_scene_5_a_session_goes_first_and_preempts_a_small_batch_job(lab):
    """Scene 5, fast lane: the CPUs are full, mostly with bob's jobs that are not preemptible,
    plus one small preemptible 4-core job of alice's; alice's 8-core job waits. bob's first
    `labflux shell` command leases a 2-core session.

    The audience sees the session first in the queue ("interactive work first"), ahead of the
    waiting batch job, waiting for alice's small job to stop; then the SESSION block on delta
    with its owner and lease countdown, while bob's jobs that are not preemptible keep running
    and alice's job queues to run again.
    """
    train24 = lab.submit("bob", cpu=24, memory_gib=8, limit=86400)
    train16 = lab.submit("bob", cpu=16, memory_gib=8, limit=86400)
    train12 = lab.submit("bob", cpu=12, memory_gib=8, limit=86400)
    sweep = lab.submit("alice", cpu=4, memory_gib=4, priority=3, limit=86400, **PREEMPTIBLE)
    lab.settle()
    trains = ((train24, "gamma"), (train16, "beta"), (train12, "delta"))
    for value, node in (*trains, (sweep, "delta")):
        assert_running(lab, value, node)
    waiting = lab.submit("alice", cpu=8, memory_gib=4, limit=600)
    response = lab.client.post(
        "/api/sessions",
        headers=user_headers("bob"),
        json={"resources": {"cpu": 2, "memory_mib": 2048}},
    )
    assert response.status_code == 200, response.text
    session = {"id": response.json()["id"], "owner": "bob"}
    lab.service.tick()  # before the Worker confirms the stop
    assert lab.job(session)["state"] == "QUEUED"
    assert lab.job(sweep)["state"] == "STOPPING"
    assert lab.job(waiting)["state"] == "QUEUED"

    queue = lab.queue()
    assert order(queue) == [session["id"], waiting["id"]]
    want = {
        "position": 1,
        "owner": "bob",
        "mine": False,
        "kind": "SESSION",
        "priority": None,
        "score": None,
        "score_parts": None,
        "decision": "waiting_for_release",
        "waiting_for": [sweep["id"]],
    }
    assert pick(entry(queue, session), want) == want
    want = {
        "position": 2,
        "mine": True,
        "kind": "BATCH",
        "decision": "no_capacity",
        "head_job_id": session["id"],
        "blocker": {"kind": "cpu", "need": 8, "best_free": 0, "best_node": "beta"},
    }
    assert pick(entry(queue, waiting), want) == want
    [preempting] = decisions(queue, "preempting", session)
    assert (preempting["detail"]["victims"], preempting["detail"]["node_id"]) == (
        [sweep["id"]],
        "delta",
    )

    lab.settle()  # the Worker confirms the stop
    assert_running(lab, session, "delta")
    for value, node in trains:
        assert_running(lab, value, node)
    job = lab.job(sweep)
    assert (job["state"], job["reason"]) == (
        "QUEUED",
        f"preempted by job {session['id'][:8]}; queued to run again (preemption 1 of 3)",
    )
    delta = lab.allocations("delta")
    block = delta[session["id"]]
    want = {
        "owner": "bob",
        "mine": False,
        "kind": "SESSION",
        "state": "RUNNING",
        "cpu": 2,
        "placement": "started",
        "preempted_by": None,
        "lease_expires_at": T0 + lab.service.settings.session_lease_s,
        "ends_at": T0 + lab.service.settings.session_lease_s,
    }
    assert pick(block, want) == want
    assert delta[train12["id"]]["preempted_by"] is None
    assert sweep["id"] not in delta
    assert [item["node_id"] for item in decisions(lab.queue(), "started", session)] == ["delta"]


def test_scene_7_a_lost_worker_holds_its_job_then_a_lost_container_retries_elsewhere(lab):
    """Scene 7, Worker failure and recovery: bob's GPU job (any card with 20 GiB of VRAM, one
    retry on node failure) runs on beta; alice's job waits for beta's RTX 4090.

    (a) beta's Worker stops: after 10 seconds without heartbeats the audience sees beta offline,
    bob's job UNKNOWN with its resources still reserved, and alice's job explained as "no online
    node has a 4090", so it does not block later jobs. The Worker restarts about 20 seconds
    later, finds the container still running, and everything returns to how it was.
    (b) beta's Worker stops again and the container is lost with it: on restart the attempt
    fails and is released, bob's job is retried on delta's A5000 and alice's job finally gets
    the 4090; bob's job history shows FAILED, then RUNNING.
    """
    retry = lab.submit(
        "bob",
        cpu=4,
        memory_gib=4,
        gpu=1,
        min_vram_mib=20480,
        limit=3600,
        safe_to_rerun=True,
        retries_on_node_failure=1,
    )
    lab.settle()
    wants = lab.submit("alice", cpu=2, memory_gib=2, gpu=1, gpu_model="4090", limit=600)
    lab.settle()
    [first] = assert_running(lab, retry, "beta", [RTX4090])["attempts"]
    assert lab.job(wants)["state"] == "QUEUED"
    assert entry(lab.queue(), wants)["decision"] == "head_reserved"

    # (a) beta's Worker process stops and its heartbeats lapse.
    lab.advance(11, skip=("beta",))
    job = lab.job(retry)
    assert (job["state"], job["reason"]) == (
        "UNKNOWN",
        "worker heartbeat expired; resources remain reserved",
    )
    assert "no online node can fit it right now" in lab.job(wants)["schedule_note"]
    nodes = lab.nodes()
    assert nodes["beta"]["status"] == "OFFLINE"
    beta = {item["job_id"]: item for item in nodes["beta"]["allocations"]}
    want = {
        "owner": "bob",
        "state": "UNKNOWN",
        "attempt_state": "UNKNOWN",
        "gpu_uuids": [RTX4090["uuid"]],
    }
    assert pick(beta[retry["id"]], want) == want
    queue = lab.queue()
    want = {
        "decision": "does_not_block",
        "blocker": {
            "kind": "gpu",
            "need": 1,
            "model": "4090",
            "min_vram_mib": None,
            "best_node": None,
            "free": 0,
            "held": 0,
            "external": 0,
            "unreported": 0,
        },
    }
    assert pick(entry(queue, wants), want) == want
    assert [item["node_id"] for item in decisions(queue, "node_offline")] == ["beta"]

    # The Worker restarts about 20 seconds later and finds the container still running.
    lab.advance(10, skip=("beta",))
    container = {"attempt_id": first["id"], "worker_id": "beta", "status": "running"}
    lab.register(
        "beta", inventory={"containers": [container], "attempts": [{"attempt_id": first["id"]}]}
    )
    lab.beat()
    lab.settle()
    job = assert_running(lab, retry, "beta", [RTX4090])
    assert [attempt["id"] for attempt in job["attempts"]] == [first["id"]]
    assert lab.nodes()["beta"]["status"] == "ONLINE"
    queue = lab.queue()
    assert entry(queue, wants)["decision"] == "head_reserved"
    assert decisions(queue, "node_online")[0]["node_id"] == "beta"

    # (b) beta's Worker stops again, and the container is gone when it restarts.
    lab.advance(11, skip=("beta",))
    lab.register("beta", inventory={})
    lab.beat()
    lab.settle()
    job = assert_running(lab, retry, "delta", [A5000])
    assert [(attempt["node_id"], attempt["state"]) for attempt in job["attempts"]] == [
        ("beta", "FAILED"),
        ("delta", "RUNNING"),
    ]
    assert_running(lab, wants, "beta", [RTX4090])
    queue = lab.queue()
    assert queue["entries"] == []
    assert decisions(queue, "retried_after_node_failure", retry)
    assert [item["node_id"] for item in decisions(queue, "node_offline")] == ["beta", "beta"]
    assert lab.allocations("delta")[retry["id"]]["placement"] == "started"
