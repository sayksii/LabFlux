"""M13 lab operations: switch the Scheduler's settings, check the lab is free, fault injection.

Only the Scheduler on the scheduler node is touched: its ~/labflux/scheduler.toml is replaced
by the production file that scripts/lab_deploy.py generated (.labflux/lab/scheduler.toml)
plus the experiment settings, and only labflux-scheduler is restarted. The first change saves
the remote file as scheduler.toml.before-m13; `restore` puts it back. Workers are never
redeployed. Experiment accounts (m13s<N>-alice ...) and their tokens live in the git-ignored
.labflux/lab/m13-users.toml.

    uv run python scripts/experiments/lab.py check
    uv run python scripts/experiments/lab.py apply --policy backfill [--no-preemption]
    uv run python scripts/experiments/lab.py restore
"""

from __future__ import annotations

import argparse
import hashlib
import secrets
import sys
import time
import tomllib
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).parent))

import lab_deploy as ld  # noqa: E402
from common import LOGICAL_USERS, account  # noqa: E402

USER_SETS = 4
USERS_FILE = ld.STATE / "m13-users.toml"
PRODUCTION = ld.STATE / "scheduler.toml"
REMOTE = "~/labflux/scheduler.toml"
BACKUP = "~/labflux/scheduler.toml.before-m13"
DATABASE = "~/labflux/scheduler-data/scheduler.sqlite3"
FAKE_MODEL = "m13-fake"
FAKE_SERVER = (ld.ROOT / "examples" / "fake-model" / "server.py").read_text()
# Experiment settings: recent usage forgets a run after 10 minutes (runs last under 5), so
# with four rotating user sets no run inherits another run's usage.
USAGE_WINDOW_S = 600


def fake_model_config(startup_s: float = 0) -> dict:
    """The on-demand stand-in model for 13.7: examples/fake-model inlined into python:3.12-slim,
    pinned to karma's RTX 4090 by its GPU filter (it never uses the card, it only holds it)."""
    return {
        "image": "python:3.12-slim",
        "argv": ["python", "-c", FAKE_SERVER],
        "env": {"FAKE_MODEL_NAME": FAKE_MODEL, "FAKE_STARTUP_S": str(startup_s)},
        "resources": {"cpu": 1, "memory_mib": 512, "gpu": 1, "gpu_model": "4090"},
        "port": 8000,
        "health_path": "/health",
        "mode": "on_demand",
        "startup_timeout_s": 120,
        "cold_wait_s": 120,
        "idle_stop_s": 300,
        "reclaim_idle_s": 60,
    }


class Lab:
    def __init__(self):
        self.inventory = ld.load_inventory()
        self.runner = ld.Runner(self.inventory)
        self.url = self.inventory.scheduler_url
        self.scheduler_node = next(
            n for n in self.inventory.nodes if n.name == self.inventory.scheduler_node
        )
        self.admin_token = tomllib.loads((ld.STATE / "cli.toml").read_text())["user_token"]
        self.tokens = self._tokens()

    # ---- accounts ----
    def _tokens(self) -> dict[str, str]:
        if USERS_FILE.exists():
            return tomllib.loads(USERS_FILE.read_text())["users"]
        tokens = {
            account(user, n): secrets.token_urlsafe(32)
            for n in range(1, USER_SETS + 1)
            for user in LOGICAL_USERS
        }
        body = "# M13 experiment accounts (git-ignored).\n[users]\n" + "".join(
            f"{ld.toml_value(k)} = {ld.toml_value(v)}\n" for k, v in tokens.items()
        )
        ld.write_private(USERS_FILE, body, dry_run=False)
        return tokens

    def headers(self, owner: str | None = None) -> dict:
        token = self.tokens[owner] if owner else self.admin_token
        return {"Authorization": f"Bearer {token}"}

    def get(self, path: str, owner: str | None = None, timeout: float = 5):
        response = httpx.get(
            self.url + path, headers=self.headers(owner), timeout=timeout, trust_env=False
        )
        response.raise_for_status()
        return response.json()

    # ---- remote helpers ----
    def ssh(self, node_name: str, command: str, stdin: str | None = None, check: bool = True):
        node = next(n for n in self.inventory.nodes if n.name == node_name)
        result = self.runner.ssh(node, command, capture=True, check=False, stdin=stdin)
        if check and result.returncode != 0:
            raise RuntimeError(f"{node_name}: {command!r} failed: {result.stderr.strip()[-500:]}")
        return result

    def healthy(self) -> bool:
        return ld.healthy(self.inventory)

    # ---- checks ----
    def foreign_jobs(self) -> list[tuple]:
        """Unfinished jobs of anyone but the experiment accounts and the model services, read
        from the Scheduler database without writing to it (the API lists only one's own)."""
        script = (
            "import sqlite3, os\n"
            f"path = os.path.expanduser({DATABASE!r})\n"
            "db = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)\n"
            'rows = db.execute("SELECT substr(id,1,8), owner, kind, state FROM jobs WHERE state '
            "NOT IN ('SUCCEEDED','FAILED','CANCELLED') AND kind != 'MODEL' AND owner NOT LIKE "
            "'m13s%'\").fetchall()\n"
            "for r in rows: print('|'.join(map(str, r)))\n"
        )
        out = self.ssh(self.scheduler_node.name, "python3 -", stdin=script).stdout
        return [tuple(line.split("|")) for line in out.splitlines() if line.strip()]

    def gpu_state(self) -> dict[str, dict]:
        result = {}
        for node in self.get("/api/nodes"):
            names = {g["uuid"]: g["name"] for g in node["capacity"].get("gpus", [])}
            for card in node["observed"].get("gpus", []):
                label = f"{node['id']}/{names.get(card['uuid'], '?').removeprefix('NVIDIA ').removeprefix('GeForce ')}"
                result[label] = card
        return result

    def check(self) -> list[str]:
        """Reasons the lab is not free for an experiment run (empty when it is)."""
        problems = []
        if not self.healthy():
            return ["Scheduler is not healthy"]
        nodes = self.get("/api/nodes")
        for node in nodes:
            if node["status"] != "ONLINE":
                problems.append(f"{node['id']} is {node['status']}")
        foreign = self.foreign_jobs()
        if foreign:
            problems.append("other users' LabFlux jobs: " + ", ".join("/".join(f) for f in foreign))
        for label, card in self.gpu_state().items():
            if card["external_use"] and not label.startswith("kindred/A40"):
                problems.append(f"{label} is used outside LabFlux ({card['memory_used_mib']} MiB)")
        return problems

    def experiment_jobs(self) -> list[dict]:
        live = []
        for owner in self.tokens:
            for job in self.get("/api/jobs", owner):
                if job["state"] not in ("SUCCEEDED", "FAILED", "CANCELLED"):
                    live.append(job)
        return live

    def cancel_experiment_jobs(self) -> int:
        count = 0
        for job in self.experiment_jobs():
            httpx.post(
                f"{self.url}/api/jobs/{job['id']}/cancel",
                headers=self.headers(job["owner"]),
                timeout=5,
                trust_env=False,
            ).raise_for_status()
            count += 1
        return count

    def gemma_inflight(self) -> int:
        return sum(m.get("inflight", 0) for m in self.get("/api/models") if m["name"] != FAKE_MODEL)

    # ---- Scheduler configuration ----
    def render(self, policy: str, preemption: bool = True) -> str:
        values = tomllib.loads(PRODUCTION.read_text())
        values["scheduling_policy"] = policy
        values["preemption_enabled"] = preemption
        values["usage_window_s"] = USAGE_WINDOW_S
        values["users"] = {**values["users"], **self.tokens}
        values.setdefault("models", {})[FAKE_MODEL] = fake_model_config()
        lines = ["# M13 experiment configuration written by scripts/experiments/lab.py."]
        lines += [
            f"{ld.toml_key(k)} = {ld.toml_value(v)}"
            for k, v in values.items()
            if not isinstance(v, dict)
        ]
        lines.append("[users]")
        lines += [f"{ld.toml_key(k)} = {ld.toml_value(v)}" for k, v in values["users"].items()]
        for name, model in values["models"].items():
            lines.append(f"[models.{ld.toml_key(name)}]")
            lines += [f"{ld.toml_key(k)} = {ld.toml_value(v)}" for k, v in model.items()]
        text = "\n".join(lines) + "\n"
        tomllib.loads(text)  # must parse
        return text

    def backup_remote(self) -> None:
        node = self.scheduler_node.name
        remote = self.ssh(node, f"sha256sum {REMOTE} | cut -d' ' -f1").stdout.strip()
        local = hashlib.sha256(PRODUCTION.read_bytes()).hexdigest()
        exists = self.ssh(node, f"test -e {BACKUP} && echo yes || echo no").stdout.strip() == "yes"
        if not exists:
            if remote != local:
                raise RuntimeError(
                    "the Scheduler's scheduler.toml differs from .labflux/lab/scheduler.toml; "
                    "refusing to replace a file this machine did not generate"
                )
            self.ssh(node, f"cp -p {REMOTE} {BACKUP}")

    def wait_ready(self, timeout: float = 90) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.healthy():
                    nodes = self.get("/api/nodes")
                    models = {m["name"]: m for m in self.get("/api/models")}
                    if (
                        len(nodes) == 3
                        and all(n["status"] == "ONLINE" for n in nodes)
                        and models.get("gemma4-31b", {}).get("state") == "serving"
                    ):
                        return
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise RuntimeError("Scheduler or nodes not ready after the restart")

    def restart_with(self, text: str) -> None:
        if self.healthy():
            for _ in range(60):
                if self.gemma_inflight() == 0:
                    break
                print("waiting for gemma requests in flight to finish", flush=True)
                time.sleep(5)
        node = self.scheduler_node.name
        self.ssh(
            node,
            f"umask 077 && cat > {REMOTE}.m13new && mv {REMOTE}.m13new {REMOTE}",
            stdin=text,
        )
        self.ssh(node, "systemctl --user restart labflux-scheduler.service")
        self.wait_ready()

    def apply(self, policy: str, preemption: bool = True) -> None:
        self.backup_remote()
        self.restart_with(self.render(policy, preemption))
        print(f"Scheduler restarted: policy={policy} preemption_enabled={preemption}", flush=True)

    def restore(self) -> None:
        node = self.scheduler_node.name
        exists = self.ssh(node, f"test -e {BACKUP} && echo yes || echo no").stdout.strip() == "yes"
        if not exists:
            print("no backup: the Scheduler was never switched", flush=True)
            return
        self.restart_with(self.ssh(node, f"cat {BACKUP}").stdout)
        remote = self.ssh(node, f"sha256sum {REMOTE} | cut -d' ' -f1").stdout.strip()
        if remote != hashlib.sha256(PRODUCTION.read_bytes()).hexdigest():
            raise RuntimeError("restored scheduler.toml does not match the production file")
        self.ssh(node, f"rm -f {BACKUP}")
        print("Scheduler restored to the production configuration", flush=True)

    # ---- fault injection (13.7): only karma or kayle Workers ----
    def worker(self, node: str, action: str) -> None:
        if node not in ("karma", "kayle"):
            raise ValueError("fault injection is limited to karma and kayle")
        self.ssh(node, f"systemctl --user {action} labflux-worker.service")

    def remove_container(self, node: str, attempt_id: str) -> str:
        """Remove one experiment job's container, as if the machine had crashed under it."""
        if node not in ("karma", "kayle"):
            raise ValueError("fault injection is limited to karma and kayle")
        return self.ssh(
            node,
            f"docker ps -aq --filter label=labflux.attempt_id={attempt_id} | xargs -r docker rm -f",
        ).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    apply = sub.add_parser("apply")
    apply.add_argument("--policy", required=True, choices=ld.POLICIES)
    apply.add_argument("--no-preemption", action="store_true")
    sub.add_parser("restore")
    sub.add_parser("render")
    sub.add_parser("cancel")
    args = parser.parse_args()
    lab = Lab()
    if args.command == "check":
        problems = lab.check()
        print("lab is free" if not problems else "\n".join(problems))
    elif args.command == "apply":
        lab.apply(args.policy, not args.no_preemption)
    elif args.command == "restore":
        lab.restore()
    elif args.command == "render":
        print(lab.render("fcfs").count("\n"), "lines (tokens not printed)")
    elif args.command == "cancel":
        print(lab.cancel_experiment_jobs(), "experiment jobs cancelled")


if __name__ == "__main__":
    main()
