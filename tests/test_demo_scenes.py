"""M14.3: the demo's scene files (examples/demo/) and scripts/demo.py, against the storyboard.

Every scene file and job file must load. The scenes are then played by scripts/demo.py itself,
in-process, against the Lab of tests/test_storyboard.py (fake clock, fake Workers, the users
alice and bob, the backfill policy). demo.py's sleeps move the fake clock, the Workers answer
between steps, and a `sleep N` container exits N seconds after it started. Each test checks the
decision the scene exists to show, so the demo files cannot drift from the verified storyboard
(tests/test_storyboard.py, test_storyboard_gpu.py, test_storyboard_model.py).
"""

import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import user_headers
from fastapi.testclient import TestClient
from test_storyboard import (
    A40S,
    A5000,
    NODES,
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
from test_storyboard_gpu import GEMMA_ON_DEMAND, gpu_blocker, uuids
from test_storyboard_model import ON_DEMAND

from labflux.api import create_app
from labflux.config import Settings
from labflux.models import MODEL_OWNER, JobSpec

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("demo", ROOT / "scripts" / "demo.py")
demo = importlib.util.module_from_spec(_spec)
sys.modules["demo"] = demo
_spec.loader.exec_module(demo)

USERS = {"alice": "alice-token", "bob": "bob-token"}


def make_lab(tmp_path, model):
    clock = [T0]
    settings = Settings(
        data_dir=tmp_path,
        users=USERS,
        scheduling_policy="backfill",
        models={"gemma4-31b": model},
    )
    app = create_app(settings, run_loop=False, clock=lambda: clock[0])
    return app, clock


@pytest.fixture
def gpu_lab(tmp_path):
    """GPU scenes 2-4: gemma4-31b on demand and stopped, all five cards free."""
    app, clock = make_lab(tmp_path, GEMMA_ON_DEMAND)
    with TestClient(app) as client:
        yield Lab(client, clock)


@pytest.fixture
def model_lab(tmp_path):
    """The model scenes: gemma4-31b with the demo's on-demand timings, stopped."""
    app, clock = make_lab(tmp_path, ON_DEMAND)
    with TestClient(app) as client:
        yield Lab(client, clock)


class World:
    """What happens while demo.py sleeps: the clock moves, the Workers answer, and each
    `sleep N` container exits N seconds after it started."""

    def __init__(self, lab):
        self.lab = lab

    def exits(self):
        for user in USERS:
            for job in self.lab.client.get("/api/jobs", headers=user_headers(user)).json():
                argv = job["spec"]["argv"]
                if job["state"] == "RUNNING" and argv[0] == "sleep":
                    started = self.lab.job(job)["attempts"][-1]["started_at"]
                    yield started + float(argv[1]), job

    def sleep(self, seconds):
        lab = self.lab
        lab.settle()  # The Workers answer at once, as the lab's do within a second.
        target = lab.clock[0] + seconds
        while due := sorted((end, job["id"], job) for end, job in self.exits() if end <= target):
            end, _, job = due[0]
            if end > lab.clock[0]:
                lab.advance(end - lab.clock[0])
            lab.done(job)
            lab.settle()
        if target > lab.clock[0]:
            lab.advance(target - lab.clock[0])
        lab.settle()


@pytest.fixture
def drive(monkeypatch):
    """demo.py talks to the lab's app as alice and bob, sleeping on the fake clock; Enter is
    pressed at every pause."""

    def start(lab):
        world = World(lab)
        fake_time = SimpleNamespace(
            sleep=world.sleep, monotonic=lambda: lab.clock[0], strftime=time.strftime
        )
        monkeypatch.setattr(demo, "time", fake_time)
        monkeypatch.setattr(
            demo,
            "connect",
            lambda url, token, timeout=30: TestClient(
                lab.client.app, headers={"Authorization": f"Bearer {token}"}
            ),
        )
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        return demo.Demo("http://testserver", dict(USERS))

    return start


def play(runner, scene_id, on_step, checks=()):
    """demo.py plays the scene; on_step checks the steps named in checks (labels or actions),
    which must still exist in the scene file, so that no check is skipped silently."""
    scene = demo.find_scene(scene_id)
    keys = {step.get("label") for step in scene["steps"]} | {s["action"] for s in scene["steps"]}
    assert set(checks) <= keys, f"scene {scene_id} has no step {set(checks) - keys}"
    assert demo.run_scene(runner, scene, on_step=on_step) == 0


def jobs(labels, label):
    return [{"id": job_id, "owner": user} for user, job_id in labels[label]]


def gemma(lab):
    [view] = lab.client.get("/api/models", headers=user_headers()).json()
    return view


def request(lab):
    """One short request relayed to the serving model, as bob's keep-alive loop sends."""
    view = lab.service.ensure_model("gemma4-31b")
    assert view["state"] == "serving", view
    lab.service.begin_request(view["job_id"])
    lab.advance(1)
    lab.service.end_request(view["job_id"])


# The files ------------------------------------------------------------------------------------


def test_every_scene_and_job_file_loads():
    scenes = demo.all_scenes()
    ids = [scene["id"] for scene in scenes]
    assert sorted(ids) == sorted(["1", "2", "3", "4", "5", "6a", "6b", "6c", "7"])
    assert [scene["run_order"] for scene in scenes] == list(range(1, len(scenes) + 1))
    for scene in scenes:
        used = set()
        for step in scene["steps"]:
            if step["action"] != "submit":
                continue
            used.add(step["submit"])
            spec = JobSpec.model_validate(step["spec"])
            assert spec.image == "python:3.12-slim", step["submit"]
            # Reserve, don't burn: the container sleeps, and exits by itself before its limit.
            assert spec.argv[0] == "sleep" and len(spec.argv) == 2, step["submit"]
            assert 0 < float(spec.argv[1]) < spec.time_limit_s, step["submit"]
        files = {path.name for path in scene["path"].glob("*.json")}
        assert files == used, f"{scene['path'].name}: unused job files {files - used}"
        for path in scene["path"].glob("*.json"):
            JobSpec.model_validate(json.loads(path.read_text()))


def test_the_run_order_keeps_gemma_away_from_the_gpu_scenes():
    """GPU scenes 2-4 need every card free, so they come before the cold start; the scenes that
    assume gemma holds kindred's A40s come after it, and the idle release comes last."""
    needs = [(scene["id"], scene["requires"]["model"]) for scene in demo.all_scenes()]
    assert needs == [
        ("2", ["stopped"]),
        ("3", ["stopped"]),
        ("4", ["stopped"]),
        ("6c", ["stopped"]),
        ("1", ["starting", "serving"]),
        ("5", ["starting", "serving"]),
        ("7", ["starting", "serving"]),
        ("6a", ["serving"]),
        ("6b", ["serving"]),
    ]


def test_the_checked_lab_shape_is_the_storyboards():
    shapes = [
        (
            float(node["cpu"]),
            node["memory"],
            tuple(sorted(g["name"].split()[-1] for g in node["gpus"])),
        )
        for node in NODES.values()
    ]
    assert sorted(shapes) == sorted(demo.LAB)


# The GPU scenes, gemma stopped ----------------------------------------------------------------


def test_scene_2_priority_jump_and_fairness(gpu_lab, drive):
    lab, runner = gpu_lab, drive(gpu_lab)
    cards = (("beta", RTX4090), ("delta", A5000), ("delta", RTX3090), ("gamma", A40S[0]))
    cards += (("gamma", A40S[1]),)

    def on_step(step, labels):
        if step["action"] == "cancel":
            lab.service.tick()  # Before the Worker confirms the stop.
            want = {"decision": "waiting_for_release", "waiting_for": [labels["sweep"][1][1]]}
            assert pick(entry(lab.queue(), jobs(labels, "urgent")[0]), want) == want
        lab.settle()
        if step.get("label") == "sweep":
            for value, (node, gpu) in zip(jobs(labels, "sweep"), cards):
                assert_running(lab, value, node, [gpu])
        if step.get("label") == "bob-p5":
            [urgent], [mine], [theirs] = (jobs(labels, k) for k in ("urgent", "alice-p5", "bob-p5"))
            queue = lab.queue()
            assert order(queue) == [urgent["id"], theirs["id"], mine["id"]]
            assert [item["decision"] for item in queue["entries"]] == [
                "head_reserved",
                "no_capacity",
                "no_capacity",
            ]
            assert [round(item["score"]) for item in queue["entries"]] == [902, 500, 495]
            assert entry(queue, urgent)["blocker"] == gpu_blocker(1, "beta", free=0, held=1)
            want = {"job_id": urgent["id"], "node_id": "beta", "start": T0 + 1800}
            assert pick(queue["reservation"], want) == want
            assert queue["usage_min_by_user"] == {"alice": pytest.approx(6)}
        if step["action"] == "cancel":
            [urgent], [mine], [theirs] = (jobs(labels, k) for k in ("urgent", "alice-p5", "bob-p5"))
            assert_running(lab, urgent, "delta", [A5000])
            assert [(item["job_id"], item["decision"]) for item in lab.queue()["entries"]] == [
                (theirs["id"], "head_reserved"),
                (mine["id"], "no_capacity"),
            ]

    play(runner, "2", on_step, checks=("sweep", "bob-p5", "cancel"))


def test_scene_3_backfill_a_short_job_before_the_two_a40_reservation(gpu_lab, drive):
    lab, runner = gpu_lab, drive(gpu_lab)

    def on_step(step, labels):
        lab.settle()
        label = step.get("label")
        if label == "prep":
            for value, gpu in zip(jobs(labels, "held"), (RTX4090, A5000, RTX3090)):
                assert_running(lab, value, "beta" if gpu is RTX4090 else "delta", [gpu])
            assert_running(lab, jobs(labels, "prep")[0], "gamma")
        if label == "head":
            queue = lab.queue()
            want = {"job_id": jobs(labels, "head")[0]["id"], "node_id": "gamma", "start": T0 + 300}
            assert pick(queue["reservation"], want) == want
            want = {"kind": "cpu", "need": 16, "best_free": 14, "best_node": "gamma"}
            assert entry(queue, jobs(labels, "head")[0])["blocker"] == want
        if label == "short":
            [head], [long], [short] = (jobs(labels, k) for k in ("head", "long", "short"))
            assert_running(lab, short, "gamma", [A40S[0]])
            assert lab.job(short)["schedule_note"].startswith(
                f"backfill: backfilled on gamma ahead of job {head['id'][:8]} reserved on gamma"
            )
            queue = lab.queue()
            assert [(item["job_id"], item["decision"]) for item in queue["entries"]] == [
                (head["id"], "head_reserved"),
                (long["id"], "would_delay_reservation"),
            ]
            blocker = gpu_blocker(2, "gamma", free=1, held=1, model="A40")
            assert entry(queue, head)["blocker"] == blocker
            assert (queue["reservation"]["node_id"], queue["reservation"]["start"]) == (
                "gamma",
                T0 + 300,
            )
            [told] = decisions(queue, "backfilled", short)
            assert told["detail"]["ahead_of"] == head["id"]
        if step["action"] == "say":
            # The countdown ran out: the 18-core job exited at 290 s, the head took both A40s.
            [head], [long] = jobs(labels, "head"), jobs(labels, "long")
            job = assert_running(lab, head, "gamma", A40S)
            started = job["attempts"][-1]["started_at"]
            assert started == T0 + 290
            queue = lab.queue()
            assert [(item["job_id"], item["decision"]) for item in queue["entries"]] == [
                (long["id"], "head_reserved")
            ]
            assert queue["reservation"]["start"] == started + 600

    play(runner, "3", on_step, checks=("prep", "head", "short", "say"))


def test_scene_4_preemption_stops_the_latest_started_job(gpu_lab, drive):
    lab, runner = gpu_lab, drive(gpu_lab)
    cards = (("beta", RTX4090), ("delta", A5000), ("delta", RTX3090), ("gamma", A40S[0]))
    cards += (("gamma", A40S[1]),)

    def on_step(step, labels):
        if step.get("label") == "urgent":
            [urgent], low = jobs(labels, "urgent"), jobs(labels, "low")
            lab.service.tick()  # Before the Worker confirms the stop.
            victim = lab.job(low[4])
            assert (victim["state"], victim["reason"]) == (
                "STOPPING",
                f"preempted by job {urgent['id'][:8]}; waiting for confirmed container termination",
            )
            queue = lab.queue()
            want = {"decision": "waiting_for_release", "waiting_for": [low[4]["id"]]}
            assert pick(entry(queue, urgent), want) == want
            [preempting] = decisions(queue, "preempting", urgent)
            assert preempting["detail"]["victims"] == [low[4]["id"]]
        lab.settle()
        if step.get("label") == "low":
            for value, (node, gpu) in zip(jobs(labels, "low"), cards):
                assert_running(lab, value, node, [gpu])
        if step.get("label") == "urgent":
            [urgent], low = jobs(labels, "urgent"), jobs(labels, "low")
            assert_running(lab, urgent, "gamma", [A40S[1]])
            job = lab.job(low[4])
            assert job["reason"].endswith("queued to run again (preemption 1 of 3)")
            assert [attempt["state"] for attempt in job["attempts"]] == ["PREEMPTED"]
            queue = lab.queue()
            assert [(item["job_id"], item["decision"]) for item in queue["entries"]] == [
                (low[4]["id"], "head_reserved")
            ]

    play(runner, "4", on_step, checks=("low", "urgent"))


# The model scenes, in the demo's run order ----------------------------------------------------


def test_the_model_scenes_in_run_order(model_lab, drive, monkeypatch):
    """6c (cold start preempting alice's A40 jobs), then 1, 5 and 7 while gemma holds kindred's
    A40s, then 6a (sharing) and 6b (idle release), with demo.py reset between the scenes."""
    lab, runner = model_lab, drive(model_lab)

    def scene_6c(step, labels):
        if step["action"] == "pause":  # bob's keep-alive loop sends its first request.
            view = lab.service.ensure_model("gemma4-31b")
            assert view["state"] == "waiting"
            sweep = [value["id"] for value in jobs(labels, "sweep")]
            queue = lab.queue()
            [item] = queue["entries"]
            want = {"owner": MODEL_OWNER, "kind": "MODEL", "decision": "waiting_for_release"}
            assert pick(item, want) == want
            assert sorted(item["waiting_for"]) == sorted(sweep)
            [preempting] = decisions(queue, "preempting")
            assert sorted(preempting["detail"]["victims"]) == sorted(sweep)
            return
        lab.settle()
        if step.get("label") == "sweep":
            for value, gpu in zip(jobs(labels, "sweep"), A40S):
                assert_running(lab, value, "gamma", [gpu])
        if step["action"] == "cancel":
            for value in jobs(labels, "sweep"):
                job = lab.job(value)
                assert job["state"] == "CANCELLED"
                assert [attempt["state"] for attempt in job["attempts"]] == ["PREEMPTED"]
            assert lab.queue()["entries"] == []
            block = next(
                item for item in lab.allocations("gamma").values() if item["kind"] == "MODEL"
            )
            assert sorted(block["gpu_uuids"]) == uuids(*A40S)

    play(runner, "6c", scene_6c, checks=("sweep", "pause", "cancel"))
    request(lab)  # The first request is served once the model is healthy.

    def scene_1(step, labels):
        lab.settle()
        if step.get("label") == "cpu8":
            placed = {
                "gpu-4090": ("beta", [RTX4090]),
                "vram20": ("delta", [A5000]),
                "ram40": ("delta", []),
            }
            for label, (node, cards) in placed.items():
                assert_running(lab, jobs(labels, label)[0], node, cards)
            for value, node in zip(jobs(labels, "cpu8"), ("beta", "gamma", "gamma")):
                assert_running(lab, value, node)

    assert demo.reset(runner) == 0
    play(runner, "1", scene_1, checks=("cpu8",))
    assert demo.reset(runner) == 0
    request(lab)

    def scene_5(step, labels):
        if step["action"] == "pause":  # bob's `labflux shell` leases a session.
            response = lab.client.post(
                "/api/sessions",
                headers=user_headers("bob"),
                json={"resources": {"cpu": 1, "memory_mib": 512}},  # fast-lane-demo's
            )
            assert response.status_code == 200, response.text
            session = {"id": response.json()["id"], "owner": "bob"}
            lab.service.tick()  # Before the Worker confirms the stop.
            queue = lab.queue()
            [waiting] = jobs(labels, "waiting")
            assert order(queue) == [session["id"], waiting["id"]]
            want = {"kind": "SESSION", "decision": "waiting_for_release"}
            assert pick(entry(queue, session), want) == want
            assert entry(queue, session)["waiting_for"] == [jobs(labels, "sweep")[0]["id"]]
            lab.settle()
            assert_running(lab, session, "delta")
            assert lab.job(jobs(labels, "sweep")[0])["reason"].endswith("(preemption 1 of 3)")
            return
        lab.settle()
        if step.get("label") == "waiting":
            trains = (("train24", "gamma"), ("train16", "beta"), ("train12", "delta"))
            for label, node in (*trains, ("sweep", "delta")):
                assert_running(lab, jobs(labels, label)[0], node)

    play(runner, "5", scene_5, checks=("waiting", "pause"))
    assert demo.reset(runner) == 0  # Also ends bob's session.
    request(lab)
    pauses = []

    def scene_7(step, labels):
        lab.settle()
        [retry] = jobs(labels, "retry")  # The scene's first step.
        if step.get("label") == "wants":
            assert_running(lab, retry, "beta", [RTX4090])
            assert entry(lab.queue(), jobs(labels, "wants")[0])["decision"] == "head_reserved"
        if step["action"] != "pause":
            return
        [wants] = jobs(labels, "wants")
        pauses.append(step)
        if len(pauses) == 1:  # (a) karma's Worker stops for about 20 seconds.
            lab.advance(11, skip=("beta",))
            assert lab.job(retry)["state"] == "UNKNOWN"
            assert entry(lab.queue(), wants)["decision"] == "does_not_block"
            lab.advance(10, skip=("beta",))
            [first] = lab.job(retry)["attempts"]
            container = {"attempt_id": first["id"], "worker_id": "beta", "status": "running"}
            lab.register(
                "beta",
                inventory={"containers": [container], "attempts": [{"attempt_id": first["id"]}]},
            )
            lab.beat()
            lab.settle()
            assert_running(lab, retry, "beta", [RTX4090])
        else:  # (b) it stops again, and the container is gone when it restarts.
            lab.advance(11, skip=("beta",))
            lab.register("beta", inventory={})
            lab.beat()
            lab.settle()
            job = assert_running(lab, retry, "delta", [A5000])
            assert [a["state"] for a in job["attempts"]] == ["FAILED", "RUNNING"]
            assert_running(lab, wants, "beta", [RTX4090])

    play(runner, "7", scene_7, checks=("wants", "pause"))
    assert len(pauses) == 2
    assert demo.reset(runner) == 0
    request(lab)

    shared = []

    def chat(_, users):  # Both streaming requests are in flight at the same time.
        view = lab.service.ensure_model("gemma4-31b")
        for _ in users:
            lab.service.begin_request(view["job_id"])
        shared.append(gemma(lab)["inflight"])
        lab.advance(20)
        for _ in users:
            lab.service.end_request(view["job_id"])

    monkeypatch.setattr(demo, "chat", chat)  # No model server answers in-process.
    served = gemma(lab)["served"]
    play(runner, "6a", lambda step, labels: lab.settle())
    assert shared == [2] and gemma(lab)["served"] == served + 2

    def scene_6b(step, labels):
        if step.get("label") == "a40":
            # Sent after 65 s idle: the model is reclaimed on the next pass.
            [a40] = jobs(labels, "a40")
            item = entry(lab.queue(), a40)
            assert item["decision"] == "waiting_for_release"
            assert "waiting for idle model gemma4-31b on gamma to be reclaimed" in item["note"]
            lab.service.tick()
            view = gemma(lab)
            assert view["state"] == "stopping"
            assert view["reason"].startswith("idle for ")
        lab.settle()
        if step["action"] == "wait_model":
            assert_running(lab, jobs(labels, "a40")[0], "gamma", A40S[:1])
            assert lab.queue()["entries"] == []

    play(runner, "6b", scene_6b, checks=("a40", "wait_model"))
    assert gemma(lab)["state"] == "stopped"


# demo.py itself -------------------------------------------------------------------------------


def test_dry_run_prints_the_steps_without_contacting_the_scheduler(monkeypatch, capsys):
    def no_network(*args, **kwargs):
        raise AssertionError("a dry run must not contact the Scheduler")

    monkeypatch.setattr(demo, "connect", no_network)
    for name in ("LABFLUX_SCHEDULER_URL", "LABFLUX_ALICE_TOKEN", "LABFLUX_BOB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert demo.main(["scene", "3", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Scene 3: EASY backfill")
    lines = [line for line in out.splitlines() if line.startswith("  [")]
    assert [line.split("] ", 1)[1].split(":")[0] for line in lines] == [
        "alice submits held-4090.json",
        "alice submits held-a5000.json",
        "alice submits held-3090.json",
        "bob submits prep.json",
        "wait 120 s",
        "bob submits head.json",
        "alice submits long.json",
        "alice submits short.json",
        "wait 175 s",
        "note",
    ]
    assert "2 GPU (A40), priority 9, limit 600 s, runs sleep 590" in out
    assert "would_delay_reservation" in out  # What the audience should see.
    assert demo.main(["scene"]) == 0
    listed = capsys.readouterr().out.splitlines()
    assert [line.split()[2] for line in listed] == ["2", "3", "4", "6c", "1", "5", "7", "6a", "6b"]


def test_reset_cancels_only_alice_and_bob_and_waits_for_confirmed_stops(
    tmp_path, monkeypatch, capsys
):
    clock = [T0]
    settings = Settings(
        data_dir=tmp_path,
        users={**USERS, "carol": "carol-token"},
        scheduling_policy="backfill",
        models={"gemma4-31b": ON_DEMAND},
    )
    app = create_app(settings, run_loop=False, clock=lambda: clock[0])
    with TestClient(app) as client:
        lab = Lab(client, clock)
        running = lab.submit("alice", cpu=4, memory_gib=8, gpu=1, limit=600)
        queued = lab.submit("bob", cpu=2, memory_gib=2, gpu=2, gpu_model="4090", limit=600)
        carols = lab.submit("carol", cpu=2, memory_gib=2, limit=600)
        lab.settle()
        assert lab.job(running)["state"] == "RUNNING" and lab.job(queued)["state"] == "QUEUED"
        stops = []

        def sleep(seconds):  # The Worker confirms the stop while reset waits.
            stops.append(lab.job(running)["state"])
            lab.settle()

        monkeypatch.setattr(demo, "time", SimpleNamespace(sleep=sleep, monotonic=time.monotonic))
        monkeypatch.setattr(
            demo,
            "connect",
            lambda url, token, timeout=30: TestClient(
                app, headers={"Authorization": f"Bearer {token}"}
            ),
        )
        monkeypatch.setenv("LABFLUX_SCHEDULER_URL", "http://testserver")
        monkeypatch.setenv("LABFLUX_ALICE_TOKEN", "alice-token")
        monkeypatch.setenv("LABFLUX_BOB_TOKEN", "bob-token")
        assert demo.main(["reset"]) == 0
        assert stops == ["STOPPING"]  # It waited for the confirmation instead of returning.
        assert lab.job(running)["state"] == lab.job(queued)["state"] == "CANCELLED"
        assert lab.job(carols)["state"] == "RUNNING"
        assert all(item["owner"] not in USERS for item in lab.allocations("beta").values())
        out = capsys.readouterr().out
        assert "All stops confirmed" in out
        assert demo.main(["reset"]) == 0
        assert "No unfinished alice/bob jobs." in capsys.readouterr().out


def test_check_reports_a_ready_lab_and_bobs_usage(gpu_lab, drive, capsys):
    lab, runner = gpu_lab, drive(gpu_lab)
    assert demo.check(runner) == 0
    out = capsys.readouterr().out
    assert "OK   policy backfill" in out
    assert "OK   gamma: ONLINE, free 32/32 CPU, 64/64 GiB, GPU cards free 2/2 A40 A40" in out
    assert "OK   gemma4-31b: on_demand, stopped" in out
    assert "differ from the rehearsed storyboard" not in out
    assert out.rstrip().endswith("check passed")

    lab.submit("bob", cpu=16, memory_gib=8, gpu=2, gpu_model="A40", limit=600)
    lab.settle()
    lab.advance(600)
    assert demo.check(runner) == 0  # Warnings only.
    out = capsys.readouterr().out
    assert "WARN bob's recent usage" in out and "scene 2 needs bob near 0" in out
    assert "WARN leftover bob job" in out
    assert "GPU cards free 0/2" in out
    # Scene 2 refuses to start in that state unless forced.
    assert demo.run_scene(runner, demo.find_scene("2")) == 1
    assert "NOT READY" in capsys.readouterr().out
