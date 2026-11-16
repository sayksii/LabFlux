#!/usr/bin/env python3
"""Run the M14 demo (docs/demo.md) against the lab Scheduler as the users alice and bob.

  check            Scheduler, policy, nodes, free GPU cards, gemma4-31b, tokens, recent usage
                   and leftover alice/bob jobs
  scene [ID]       play one scene of examples/demo/ (no ID: list the scenes in run order);
                   --dry-run prints the steps without contacting the Scheduler
  reset            cancel every unfinished alice/bob job and wait until the stops are confirmed
  keepalive        bob's request to gemma4-31b every 20 s (--once: a single request)

The Scheduler URL and the tokens come from --config, a TOML file with scheduler_url and a
[users] table (the Scheduler config that scripts/lab_deploy.py writes has both), or from
LABFLUX_SCHEDULER_URL, LABFLUX_ALICE_TOKEN and LABFLUX_BOB_TOKEN, which take precedence.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import tomllib
from pathlib import Path

import httpx

from labflux.models import JobSpec

ROOT = Path(__file__).resolve().parents[1]
SCENES = ROOT / "examples" / "demo"
USERS = ("alice", "bob")
MODEL = "gemma4-31b"
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
# The node shapes the scenes were rehearsed on (tests/test_storyboard.py): CPU, MiB, GPU models.
LAB = [
    (16.0, 24576, ("4090",)),
    (16.0, 49152, ("3090", "A5000")),
    (32.0, 65536, ("A40", "A40")),
]
ACTIONS = ("submit", "wait", "cancel", "pause", "wait_model", "model_idle", "chat")
STEP_KEYS = {*ACTIONS, "say", "user", "label", "count", "every", "index", "timeout"}
CHAT_PROMPT = "請用大約 300 字說明實驗室共用 GPU 叢集為什麼需要排程器。"


def connect(url: str, token: str, timeout: float = 30) -> httpx.Client:
    """An API client acting as the user that token belongs to (replaced in tests)."""
    return httpx.Client(
        base_url=url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
        trust_env=False,
    )


# Scenes -----------------------------------------------------------------------------------------


def scene_id(path: Path) -> str:
    return path.name.split("-", 1)[0]


def load_scene(path: Path) -> dict:
    """A scene folder's scene.toml, checked, with each submit step's job file loaded."""
    scene = tomllib.loads((path / "scene.toml").read_text())
    scene["id"], scene["path"] = scene_id(path), path
    labels = set()
    for number, step in enumerate(scene["steps"], 1):
        where = f"{path.name} step {number}"
        if unknown := set(step) - STEP_KEYS:
            raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
        actions = [key for key in ACTIONS if key in step]
        if len(actions) > 1 or not (actions or "say" in step):
            raise ValueError(f"{where}: needs exactly one of {', '.join(ACTIONS)}, or only say")
        step["action"] = actions[0] if actions else "say"
        if step["action"] == "submit":
            if step.get("user") not in USERS:
                raise ValueError(f"{where}: user must be one of {USERS}")
            step["spec"] = json.loads((path / step["submit"]).read_text())
            JobSpec.model_validate(step["spec"])
            step.setdefault("label", Path(step["submit"]).stem)
            labels.add(step["label"])
        elif step["action"] == "cancel" and step["cancel"] not in labels:
            raise ValueError(f"{where}: no earlier submit has the label {step['cancel']!r}")
        elif step["action"] == "chat" and not set(step["chat"]) <= set(USERS):
            raise ValueError(f"{where}: chat users must be among {USERS}")
    return scene


def all_scenes() -> list[dict]:
    """Every scene of examples/demo/, in run order."""
    paths = [path for path in SCENES.iterdir() if (path / "scene.toml").is_file()]
    return sorted((load_scene(path) for path in paths), key=lambda scene: scene["run_order"])


def find_scene(wanted: str) -> dict:
    for scene in all_scenes():
        if scene["id"] == wanted:
            return scene
    raise SystemExit(f"demo: no scene {wanted!r}; `demo.py scene` lists them")


def describe(spec: dict) -> str:
    resources = spec.get("resources", {})
    parts = [f"{resources.get('cpu', 1):g} CPU", f"{resources.get('memory_mib', 256) / 1024:g} GiB"]
    if resources.get("gpu"):
        card = resources.get("gpu_model")
        if card is None and resources.get("min_vram_mib"):
            card = f">= {resources['min_vram_mib'] / 1024:g} GiB VRAM"
        parts.append(f"{resources['gpu']} GPU ({card or 'any'})")
    if spec.get("priority") is not None:
        parts.append(f"priority {spec['priority']}")
    if spec.get("preemptible"):
        parts.append("preemptible")
    if spec.get("retries_on_node_failure"):
        parts.append(f"{spec['retries_on_node_failure']} retry on node failure")
    parts.append(f"limit {spec['time_limit_s']:g} s, runs {' '.join(spec['argv'])}")
    return ", ".join(parts)


def describe_step(step: dict) -> str:
    action = step["action"]
    if action == "submit":
        count = step.get("count", 1)
        times = f" x{count}" + (f", one every {step['every']} s" if step.get("every") else "")
        return f"{step['user']} submits {step['submit']}{times if count > 1 else ''}: " + describe(
            step["spec"]
        )
    if action == "wait":
        return f"wait {step['wait']} s"
    if action == "cancel":
        which = f" #{step['index']}" if "index" in step else ""
        return f"cancel {step['cancel']}{which} (its owner cancels it)"
    if action == "pause":
        return "PAUSE (do this by hand):"
    if action == "wait_model":
        states = " or ".join(step["wait_model"])
        return f"wait until {MODEL} is {states} (up to {step.get('timeout', 300)} s)"
    if action == "model_idle":
        return f"wait until {MODEL} has been idle for {step['model_idle']} s"
    if action == "chat":
        return f"{' and '.join(step['chat'])} each send a streaming chat to {MODEL} at once"
    return "note"


def say(step: dict) -> None:
    for line in step.get("say", "").strip().splitlines():
        print(f"     > {line}", flush=True)


def indent(text: str) -> str:
    return "\n".join(f"     {line.strip()}" for line in text.strip().splitlines())


# The Scheduler ----------------------------------------------------------------------------------


class Demo:
    """The Scheduler as alice and bob see it."""

    def __init__(self, url: str | None, tokens: dict):
        self.url, self.tokens, self.clients = url, tokens, {}

    def client(self, user: str, timeout: float = 30) -> httpx.Client:
        if (user, timeout) not in self.clients:
            if not self.url:
                raise SystemExit("demo: no Scheduler URL; give --config or LABFLUX_SCHEDULER_URL")
            if not self.tokens.get(user):
                raise SystemExit(
                    f"demo: no token for {user}; give --config with [users] {user} = ... or "
                    f"LABFLUX_{user.upper()}_TOKEN"
                )
            self.clients[(user, timeout)] = connect(self.url, self.tokens[user], timeout)
        return self.clients[(user, timeout)]

    def call(self, user: str, method: str, path: str, **kwargs):
        response = self.client(user).request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    def model(self) -> dict | None:
        return next(
            (m for m in self.call("alice", "GET", "/api/models") if m["name"] == MODEL), None
        )

    def unfinished(self) -> list[tuple[str, dict]]:
        return [
            (user, job)
            for user in USERS
            for job in self.call(user, "GET", "/api/jobs")
            if job["state"] not in TERMINAL
        ]

    def cards(self) -> dict:
        """GPU UUID -> (node, short card name)."""
        return {
            gpu["uuid"]: (node["id"], gpu["name"].split()[-1])
            for node in self.call("alice", "GET", "/api/nodes")
            for gpu in node["capacity"]["gpus"]
        }

    def where(self, job: dict) -> str:
        if not job["attempts"]:
            return "not started"
        attempt, cards = job["attempts"][-1], self.cards()
        names = [cards.get(uuid, ("", uuid[:12]))[1] for uuid in attempt.get("gpu_uuids") or []]
        return attempt["node_id"] + (f" {'+'.join(names)}" if names else "")


# Commands ---------------------------------------------------------------------------------------


def check(demo: Demo) -> int:
    failed = []

    def report(level: str, text: str) -> None:
        print(f"{level:4} {text}", flush=True)
        if level == "FAIL":
            failed.append(text)

    try:
        health = demo.client("alice").get("/health").json()
    except httpx.HTTPError as exc:
        report("FAIL", f"Scheduler not reachable at {demo.url}: {exc}")
        return 1
    report("OK", f"Scheduler {demo.url} (version {health.get('version')})")
    for user in USERS:
        try:
            who = demo.call(user, "GET", "/api/whoami")["user"]
            report("OK" if who == user else "FAIL", f"{user}'s token belongs to {who}")
        except httpx.HTTPStatusError as exc:
            report("FAIL", f"{user}'s token is rejected (HTTP {exc.response.status_code})")
    if failed:
        return 1

    queue = demo.call("alice", "GET", "/api/queue")
    policy = queue.get("policy")
    report("OK" if policy == "backfill" else "FAIL", f"policy {policy} (the demo needs backfill)")

    nodes = demo.call("alice", "GET", "/api/nodes")
    shapes = []
    for node in nodes:
        capacity, allocated = node["capacity"], node["allocated"]
        used = set(allocated.get("gpu_uuids") or [])
        used |= {
            g["uuid"] for g in (node.get("observed") or {}).get("gpus", []) if g["external_use"]
        }
        free = [g["name"].split()[-1] for g in capacity["gpus"] if g["uuid"] not in used]
        report(
            "OK" if node["status"] == "ONLINE" else "FAIL",
            f"{node['id']}: {node['status']}, free {capacity['cpu'] - allocated['cpu']:g}/"
            f"{capacity['cpu']:g} CPU, {(capacity['memory_mib'] - allocated['memory_mib']) / 1024:g}/"
            f"{capacity['memory_mib'] / 1024:g} GiB, GPU cards free {len(free)}/"
            f"{len(capacity['gpus'])} {' '.join(free)}",
        )
        names = tuple(sorted(g["name"].split()[-1] for g in capacity["gpus"]))
        shapes.append((float(capacity["cpu"]), capacity["memory_mib"], names))
    if sorted(shapes) != sorted(LAB):
        report(
            "WARN", "node capacities differ from the rehearsed storyboard: placements may change"
        )

    model = demo.model()
    if model is None:
        report("FAIL", f"{MODEL} is not configured")
    else:
        idle = ""
        if None not in (model["last_activity_at"], queue["computed_at"]):
            idle = f", last request {queue['computed_at'] - model['last_activity_at']:.0f} s ago"
        report(
            "OK" if model["mode"] == "on_demand" else "WARN",
            f"{MODEL}: {model['mode']}, {model['state']}"
            + (f" on {model['node_id']}" if model["node_id"] else "")
            + f", {model['inflight']} in flight, served {model['served']}{idle}",
        )
        if model["mode"] != "on_demand":
            report("WARN", f"{MODEL} should be on_demand for the cold start (docs/demo.md)")

    usage = queue.get("usage_min_by_user", {})
    for user in USERS:
        minutes = usage.get(user, 0)
        level = "WARN" if user == "bob" and minutes > 0.5 else "OK"
        note = " (scene 2 needs bob near 0: wait until it leaves the 60-minute window)"
        report(
            level, f"{user}'s recent usage {minutes:.1f} min" + (note if level == "WARN" else "")
        )

    left = demo.unfinished()
    for user, job in left:
        report("WARN", f"leftover {user} job {job['id'][:8]} {job['state']}: run demo.py reset")
    if not left:
        report("OK", "no unfinished alice/bob jobs")
    print("check failed" if failed else "check passed", flush=True)
    return 1 if failed else 0


def reset(demo: Demo, timeout: float = 180) -> int:
    pending = {}
    for user, job in demo.unfinished():
        demo.call(user, "POST", f"/api/jobs/{job['id']}/cancel")
        pending[job["id"]] = user
        print(f"cancel {user} {job['id'][:8]} ({job['state']})", flush=True)
    if not pending:
        print("No unfinished alice/bob jobs.", flush=True)
        return 0
    deadline = time.monotonic() + timeout
    shown = None
    while True:
        states = {}
        for job_id, user in list(pending.items()):
            state = demo.call(user, "GET", f"/api/jobs/{job_id}")["state"]
            if state in TERMINAL:
                del pending[job_id]
            else:
                states[job_id] = state
        if not pending:
            print("All stops confirmed: alice and bob hold no resources.", flush=True)
            return 0
        if len(pending) != shown:
            print(f"waiting for {len(pending)} confirmed stops ...", flush=True)
            shown = len(pending)
        if time.monotonic() >= deadline:
            for job_id, state in states.items():
                print(f"still {state}: {pending[job_id]} {job_id[:8]}", flush=True)
            print("Not all stops were confirmed (an UNKNOWN job waits for its Worker).")
            return 1
        time.sleep(1)


def keepalive(demo: Demo, once: bool = False, interval: float = 20) -> int:
    """bob's request loop: keeps an on-demand gemma serving (and cold-starts it if stopped)."""
    client = demo.client("bob", timeout=900)  # The first request may wait out a cold start.
    body = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16}
    while True:
        started = time.monotonic()
        try:
            response = client.post("/v1/chat/completions", json=body)
            status = str(response.status_code)
            if response.status_code != 200:
                status += f" {response.text[:160]}"
        except httpx.HTTPError as exc:
            status = type(exc).__name__
        elapsed = time.monotonic() - started
        print(f"{time.strftime('%H:%M:%S')} bob -> {MODEL}: {status} ({elapsed:.1f} s)", flush=True)
        if once:
            return 0 if status == "200" else 1
        time.sleep(interval)


def chat(demo: Demo, users: list[str]) -> None:
    """Each user streams one long answer from the model, all at the same moment."""

    def stream(user: str) -> None:
        body = {
            "model": MODEL,
            "messages": [{"role": "user", "content": CHAT_PROMPT}],
            "max_tokens": 512,
            "stream": True,
        }
        started, chunks, first = time.monotonic(), 0, None
        try:
            with demo.client(user, timeout=900).stream(
                "POST", "/v1/chat/completions", json=body
            ) as response:
                print(f"     {user}: HTTP {response.status_code}", flush=True)
                for line in response.iter_lines():
                    if line.startswith("data:") and line.strip() != "data: [DONE]":
                        chunks += 1
                        first = first or time.monotonic() - started
        except httpx.HTTPError as exc:
            print(f"     {user}: {type(exc).__name__}: {exc}", flush=True)
        took = time.monotonic() - started
        print(
            f"     {user}: {chunks} chunks in {took:.1f} s (first after {first or 0:.1f} s)",
            flush=True,
        )

    threads = [threading.Thread(target=stream, args=(user,)) for user in users]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def preflight(demo: Demo, scene: dict) -> list[str]:
    """Why this scene would not play as rehearsed now."""
    problems, needs = [], scene.get("requires", {})
    if "model" in needs:
        model = demo.model()
        state = model["state"] if model else "not configured"
        if state not in needs["model"]:
            problems.append(
                f"{MODEL} is {state}; this scene needs it {' or '.join(needs['model'])}"
            )
    if needs.get("no_jobs") and (left := demo.unfinished()):
        problems.append(f"alice/bob still have {len(left)} unfinished jobs: run demo.py reset")
    usage = demo.call("alice", "GET", "/api/queue")["usage_min_by_user"]
    for user, limit in needs.get("max_usage_min", {}).items():
        if usage.get(user, 0) > limit:
            problems.append(
                f"{user}'s recent usage is {usage[user]:.1f} min (over {limit}); the scores will "
                "differ from the rehearsal until it leaves the 60-minute window"
            )
    return problems


def wait(seconds: float) -> None:
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        if left < seconds:
            print(f"     ... {left:.0f} s left", flush=True)
        time.sleep(min(30, left))


def wait_model(demo: Demo, states: list[str], timeout: float) -> None:
    deadline, seen = time.monotonic() + timeout, None
    while True:
        model = demo.model()
        if model["state"] != seen:
            seen = model["state"]
            where = f" on {model['node_id']}" if model["node_id"] else ""
            print(f"     {MODEL}: {seen}{where}", flush=True)
        if seen in states:
            return
        if time.monotonic() >= deadline:
            raise SystemExit(
                f"demo: {MODEL} is still {seen} after {timeout} s (docs/demo.md fallbacks)"
            )
        time.sleep(2)


def model_idle(demo: Demo, seconds: float) -> None:
    """Until the model has had no request for `seconds`, by the Scheduler's own clock."""
    last = None
    while True:
        model, now = demo.model(), demo.call("alice", "GET", "/api/queue")["computed_at"]
        if model["state"] != "serving":
            raise SystemExit(f"demo: {MODEL} is {model['state']}, not serving")
        if model["last_activity_at"] is None:
            raise SystemExit(
                f"demo: no request has reached {MODEL} since the Scheduler started; send one "
                "with `demo.py keepalive --once` first"
            )
        # computed_at (the last scheduling pass) is None only right after a Scheduler restart.
        idle = 0 if model["inflight"] or now is None else now - model["last_activity_at"]
        if idle >= seconds:
            print(f"     idle {idle:.0f} s", flush=True)
            return
        if last is None or idle < last or idle - last >= 10:
            print(f"     idle {idle:.0f} s (in flight {model['inflight']})", flush=True)
            last = idle
        time.sleep(2)


def run_scene(demo: Demo, scene: dict, *, dry_run=False, force=False, on_step=None) -> int:
    """Play the scene's steps in order. on_step(step, jobs) runs after each step (tests); jobs
    maps each label to the (user, job ID) pairs submitted under it."""
    steps = scene["steps"]
    print(f"Scene {scene['id']}: {scene['title']}  (run order {scene['run_order']})", flush=True)
    print(f"  Dashboard axis: {scene.get('axis', 'GPU 卡')}", flush=True)
    needs = scene.get("requires", {})
    if needs:
        print(f"  Requires: {json.dumps(needs, ensure_ascii=False)}", flush=True)
    if dry_run:
        for number, step in enumerate(steps, 1):
            print(f"  [{number}/{len(steps)}] {describe_step(step)}", flush=True)
            if step["action"] == "pause":
                print(indent(step["pause"]), flush=True)
            say(step)
        return 0
    if problems := preflight(demo, scene):
        for problem in problems:
            print(f"  NOT READY: {problem}", flush=True)
        if not force:
            print("  Fix these first, or pass --force to play anyway.", flush=True)
            return 1
    labels: dict[str, list[tuple[str, str]]] = {}
    for number, step in enumerate(steps, 1):
        action = step["action"]
        print(f"  [{number}/{len(steps)}] {describe_step(step)}", flush=True)
        if action in ("wait", "wait_model", "model_idle", "chat"):
            say(step)  # What the audience sees meanwhile.
        if action == "submit":
            for index in range(step.get("count", 1)):
                if index and step.get("every"):
                    time.sleep(step["every"])
                job = demo.call(step["user"], "POST", "/api/jobs", json=step["spec"])
                labels.setdefault(step["label"], []).append((step["user"], job["id"]))
                print(
                    f"     {step['label']} #{index + 1}: job {job['id'][:8]} {job['state']}",
                    flush=True,
                )
        elif action == "wait":
            wait(step["wait"])
        elif action == "cancel":
            chosen = labels[step["cancel"]]
            if "index" in step:
                chosen = [chosen[step["index"] - 1]]
            for user, job_id in chosen:
                place = demo.where(demo.call(user, "GET", f"/api/jobs/{job_id}"))
                state = demo.call(user, "POST", f"/api/jobs/{job_id}/cancel")["state"]
                print(f"     {user} cancelled {job_id[:8]} ({place}): {state}", flush=True)
        elif action == "pause":
            text = step["pause"]
            for label, jobs in labels.items():
                text = text.replace("{" + label + "}", jobs[0][1])
            print(indent(text), flush=True)
            try:
                input("  [Enter] continue, [Ctrl-C] end the scene here ")
            except EOFError:
                pass
        elif action == "wait_model":
            wait_model(demo, step["wait_model"], step.get("timeout", 300))
        elif action == "model_idle":
            model_idle(demo, step["model_idle"])
        elif action == "chat":
            chat(demo, step["chat"])
        if action not in ("wait", "wait_model", "model_idle", "chat"):
            say(step)
        if on_step:
            on_step(step, labels)
    order = [s for s in all_scenes() if s["run_order"] > scene["run_order"]]
    after = f" Next in the run order: scene {order[0]['id']}." if order else ""
    print(f"Scene {scene['id']} done. Run `demo.py reset` before the next scene.{after}")
    return 0


# Entry point ------------------------------------------------------------------------------------


def load_config(path: Path | None) -> Demo:
    try:
        values = tomllib.loads(path.read_text()) if path else {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"demo: cannot read --config {path}: {exc}") from exc
    users = values.get("users", {})
    tokens = {user: os.getenv(f"LABFLUX_{user.upper()}_TOKEN") or users.get(user) for user in USERS}
    return Demo(os.getenv("LABFLUX_SCHEDULER_URL") or values.get("scheduler_url"), tokens)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", type=Path, help="TOML with scheduler_url and [users]")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="is the lab ready for the demo?")
    scene = commands.add_parser("scene", help="play one scene; no ID lists them")
    scene.add_argument("id", nargs="?")
    scene.add_argument("--dry-run", action="store_true", help="print the steps only")
    scene.add_argument("--force", action="store_true", help="play even if not ready")
    reset_parser = commands.add_parser("reset", help="cancel all alice/bob jobs")
    reset_parser.add_argument("--timeout", type=float, default=180)
    keep = commands.add_parser("keepalive", help="bob's gemma request every 20 s")
    keep.add_argument("--once", action="store_true", help="send one request and exit")
    args = parser.parse_args(argv)

    if args.command == "scene" and args.id is None:
        for item in all_scenes():
            print(
                f"{item['run_order']:2}. scene {item['id']:3} {item['title']}  [{item['path'].name}]"
            )
        return 0
    demo = load_config(args.config)
    try:
        if args.command == "check":
            return check(demo)
        if args.command == "scene":
            return run_scene(demo, find_scene(args.id), dry_run=args.dry_run, force=args.force)
        if args.command == "reset":
            return reset(demo, args.timeout)
        return keepalive(demo, once=args.once)
    except KeyboardInterrupt:
        print("\nstopped" + ("" if args.command == "keepalive" else "; `demo.py reset` cleans up"))
        return 0 if args.command == "keepalive" else 130
    except httpx.HTTPStatusError as exc:
        print(f"demo: HTTP {exc.response.status_code} from {exc.request.url}: {exc.response.text}")
        return 1
    except httpx.HTTPError as exc:
        print(f"demo: API request failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
