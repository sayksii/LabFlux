#!/usr/bin/env python3
"""Check and deploy LabFlux on the physical lab machines as the dedicated account.

  uv run python scripts/lab_deploy.py check
  uv run python scripts/lab_deploy.py firewall-instructions
  uv run python scripts/lab_deploy.py deploy [--node NAME] [--dry-run]
  uv run python scripts/lab_deploy.py status

The inventory is configs/local-lab.toml (git-ignored; see configs/lab.example.toml).
Generated tokens, TOML files and the CLI config stay in the ignored .labflux/lab/.
Workers and the Scheduler run as systemd user units of that account. The only privileged
piece is the Scheduler firewall unit, which an administrator installs by hand.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".labflux" / "lab"
INVENTORY = ROOT / "configs" / "local-lab.toml"
UV = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
UV_VERSION = "0.11.8"
BASE_IMAGE = "localhost/labflux/python-uv:3.12-uv0.11.8"
GPU_UUID = r"GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
FIREWALL_UNIT = "labflux-firewall.service"
WORKER_UNIT = "labflux-worker.service"
SCHEDULER_UNIT = "labflux-scheduler.service"
# A busy card must not even get the nvidia-smi probe container; matches gpu_external_memory_mib.
IDLE_MEMORY_MIB = 1024
LOW_DISK_GIB = 50
# Since M6.4 a stopped Worker leaves its containers running and the next instance adopts them,
# so Worker restarts no longer need idle nodes. Scheduler restart recovery (M6.6) is still
# missing, so restarting the Scheduler always requires that no attempt is outstanding.
WORKER_RESTART_REQUIRES_IDLE = False


@dataclass(frozen=True)
class Node:
    name: str
    host: str
    allocatable_cpu: float
    allocatable_memory_mib: int
    gpus: tuple
    datasets: dict


@dataclass(frozen=True)
class Inventory:
    account: str
    ssh_key: Path
    known_hosts: Path
    scheduler_node: str
    bind_host: str
    port: int
    users: tuple
    nodes: tuple

    @property
    def scheduler(self) -> Node:
        return next(node for node in self.nodes if node.name == self.scheduler_node)

    @property
    def scheduler_url(self) -> str:
        return f"http://{self.bind_host}:{self.port}"

    @property
    def public_bind(self) -> bool:
        return not ipaddress.ip_address(self.bind_host).is_loopback


def parse_inventory(values: dict, root: Path = ROOT) -> Inventory:
    def path(value: str) -> Path:
        candidate = Path(value).expanduser()
        return candidate if candidate.is_absolute() else root / candidate

    scheduler = values.get("scheduler", {})
    nodes = []
    for entry in values.get("nodes", []):
        gpus = entry.get("gpus", [])
        if any(
            isinstance(gpu, bool)
            or not (
                (isinstance(gpu, int) and gpu >= 0)
                or (isinstance(gpu, str) and re.fullmatch(GPU_UUID, gpu))
            )
            for gpu in gpus
        ):
            raise ValueError(f"{entry.get('name')}: gpus must be NVML indexes or GPU UUIDs")
        if len(set(gpus)) != len(gpus):
            raise ValueError(f"{entry.get('name')}: a GPU is listed twice")
        if not entry.get("allocatable_cpu", 0) > 0 or entry.get("allocatable_memory_mib", 0) < 16:
            raise ValueError(f"{entry.get('name')}: allocatable CPU/RAM must be set")
        ipaddress.ip_address(entry["host"])
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", entry["name"]):
            raise ValueError(f"invalid node name: {entry['name']!r}")
        nodes.append(
            Node(
                name=entry["name"],
                host=entry["host"],
                allocatable_cpu=entry["allocatable_cpu"],
                allocatable_memory_mib=entry["allocatable_memory_mib"],
                gpus=tuple(gpus),
                datasets=dict(entry.get("datasets", {})),
            )
        )
    if not nodes:
        raise ValueError("the inventory needs at least one node")
    if len({n.name for n in nodes}) != len(nodes) or len({n.host for n in nodes}) != len(nodes):
        raise ValueError("node names and hosts must be unique")
    if scheduler.get("node") not in {n.name for n in nodes}:
        raise ValueError("scheduler.node must name an inventory node")
    port = scheduler.get("port", 8770)
    if not 1024 <= port <= 65535:
        raise ValueError("scheduler.port must be an unprivileged port")
    users = tuple(scheduler.get("users", []))
    if not users or len(set(users)) != len(users):
        raise ValueError("scheduler.users needs distinct user names")
    inventory = Inventory(
        account=values.get("account", "labflux"),
        ssh_key=path(values.get("ssh_key", "~/.ssh/labflux_deploy")),
        known_hosts=path(values.get("known_hosts", ".labflux/lab/known_hosts")),
        scheduler_node=scheduler["node"],
        bind_host=str(ipaddress.ip_address(scheduler["bind_host"])),
        port=port,
        users=users,
        nodes=tuple(nodes),
    )
    if inventory.public_bind:
        firewall_rule(inventory)  # Validates that the rule can express the inventory.
    return inventory


def load_inventory(path: Path = INVENTORY) -> Inventory:
    if not path.exists():
        raise RuntimeError(f"missing {path}; copy configs/lab.example.toml and fill it in")
    return parse_inventory(tomllib.loads(path.read_text()))


# ---- Generated files ---------------------------------------------------------------------


def firewall_rule(inventory: Inventory) -> str:
    """Drop Scheduler traffic except loopback and the remote Workers, without ACCEPTing anything."""
    remote = [n.host for n in inventory.nodes if n.name != inventory.scheduler_node]
    if len(remote) > 1:
        raise ValueError("the firewall rule supports one remote Worker; add a chain for more")
    allowed = f" ! -s {remote[0]}" if remote else ""
    return (
        f"INPUT -d {inventory.bind_host} -p tcp --dport {inventory.port} ! -i lo{allowed} -j DROP"
    )


def firewall_unit(inventory: Inventory) -> str:
    rule = firewall_rule(inventory)
    remote = [n.host for n in inventory.nodes if n.name != inventory.scheduler_node]
    return (
        "[Unit]\n"
        f"Description=LabFlux Scheduler firewall: only lo and {', '.join(remote) or 'nobody'}"
        f" may reach {inventory.bind_host}:{inventory.port}\n"
        "DefaultDependencies=no\nWants=network-pre.target\n"
        "Before=network-pre.target shutdown.target\nAfter=local-fs.target\n"
        "Conflicts=shutdown.target\n\n"
        "[Service]\nType=oneshot\nRemainAfterExit=yes\n"
        f"ExecStart=/usr/sbin/iptables -w -I {rule}\n"
        f"ExecStop=/usr/sbin/iptables -w -D {rule}\n\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )


def firewall_instructions(inventory: Inventory) -> str:
    scheduler = inventory.scheduler
    return f"""\
Run these on {scheduler.name} as the administrator account (the personal account has no sudo),
for example after `su - mvladmin`. They only add one DROP rule for {inventory.bind_host}:{inventory.port};
nothing else on the machine is filtered, and ufw stays disabled.

1. Install the unit:
sudo tee /etc/systemd/system/{FIREWALL_UNIT} > /dev/null <<'EOF'
{firewall_unit(inventory)}EOF

2. Load it, start it now and at every boot:
sudo systemctl daemon-reload
sudo systemctl enable --now {FIREWALL_UNIT}

3. Verify (expect "active", then one rule with "--dport {inventory.port}" and "-j DROP"):
systemctl is-active {FIREWALL_UNIT}
sudo iptables -S INPUT | grep -- '--dport {inventory.port}'

To remove it later:
sudo systemctl disable --now {FIREWALL_UNIT}
sudo rm /etc/systemd/system/{FIREWALL_UNIT}
sudo systemctl daemon-reload
"""


def toml_value(value) -> str:
    # JSON strings, numbers and flat arrays are valid TOML values.
    return json.dumps(value)


def worker_toml(inventory: Inventory, node: Node, worker_token: str) -> str:
    lines = [
        "# Generated by scripts/lab_deploy.py; contains the Worker token.",
        f"worker_id = {toml_value(node.name)}",
        f"scheduler_url = {toml_value(inventory.scheduler_url)}",
        f"worker_token = {toml_value(worker_token)}",
        'data_dir = "~/labflux/worker-data"',
        'uv_cache_dir = "~/labflux/uv-cache"',
        f"allocatable_cpu = {toml_value(node.allocatable_cpu)}",
        f"allocatable_memory_mib = {toml_value(node.allocatable_memory_mib)}",
        f"gpus = {toml_value(list(node.gpus))}",
    ]
    if node.datasets:
        lines.append("[datasets]")
        lines += [f"{toml_value(k)} = {toml_value(v)}" for k, v in sorted(node.datasets.items())]
    return "\n".join(lines) + "\n"


def scheduler_toml(inventory: Inventory, tokens: dict) -> str:
    lines = [
        "# Generated by scripts/lab_deploy.py; contains every LabFlux token.",
        'data_dir = "~/labflux/scheduler-data"',
        f"bind_host = {toml_value(inventory.bind_host)}",
        f"bind_port = {inventory.port}",
        f"scheduler_url = {toml_value(inventory.scheduler_url)}",
        f"worker_token = {toml_value(tokens['worker'])}",
        "[users]",
    ]
    lines += [
        f"{toml_value(user)} = {toml_value(token)}" for user, token in tokens["users"].items()
    ]
    return "\n".join(lines) + "\n"


def cli_toml(inventory: Inventory, user_token: str) -> str:
    return (
        "# LabFlux CLI configuration for this user; keep it private.\n"
        f"scheduler_url = {toml_value(inventory.scheduler_url)}\n"
        f"user_token = {toml_value(user_token)}\n"
    )


def worker_unit() -> str:
    return (
        "[Unit]\nDescription=LabFlux Worker\n\n"
        "[Service]\nWorkingDirectory=%h/labflux\n"
        "ExecStart=%h/labflux/.venv/bin/labflux --config %h/labflux/worker.toml worker\n"
        "Restart=on-failure\nRestartSec=5\n"
        # SIGTERM leaves job containers running; the timeout covers the final outbox flush.
        "TimeoutStopSec=60\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def scheduler_unit(inventory: Inventory) -> str:
    guard = (
        # Never listen on a public address unless the system firewall unit is active.
        f"ExecStartPre=/usr/bin/systemctl is-active --quiet {FIREWALL_UNIT}\n"
        if inventory.public_bind
        else ""
    )
    return (
        "[Unit]\nDescription=LabFlux Scheduler\n\n"
        f"[Service]\nWorkingDirectory=%h/labflux\n{guard}"
        "ExecStart=%h/labflux/.venv/bin/labflux --config %h/labflux/scheduler.toml scheduler\n"
        "Restart=on-failure\nRestartSec=5\nTimeoutStopSec=20\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def load_tokens(inventory: Inventory, state: Path = STATE, *, create: bool) -> dict:
    """Reuse tokens from the last deploy; users removed from the inventory lose their token."""
    path = state / "scheduler.toml"
    existing = tomllib.loads(path.read_text()) if path.exists() else {}

    def new(label: str) -> str:
        return secrets.token_urlsafe(32) if create else f"<generated {label}>"

    users = existing.get("users", {})
    return {
        "worker": existing.get("worker_token") or new("worker token"),
        "users": {user: users.get(user) or new(f"token for {user}") for user in inventory.users},
    }


def validate_generated(inventory: Inventory, tokens: dict) -> None:
    from labflux.config import Settings

    Settings.model_validate(tomllib.loads(scheduler_toml(inventory, tokens)))
    for node in inventory.nodes:
        Settings.model_validate(tomllib.loads(worker_toml(inventory, node, tokens["worker"])))


# ---- Restart guard -----------------------------------------------------------------------


def restart_blockers(
    name: str, *, worker_active: bool, containers: list[str], scheduler_nodes: list[dict] | None
) -> list[str]:
    """Reasons why restarting this Worker would strand work (when WORKER_RESTART_REQUIRES_IDLE)."""
    reasons = []
    if containers:
        reasons.append(f"{name}: LabFlux job containers exist: {', '.join(containers)}")
    if scheduler_nodes is None:
        if worker_active:
            reasons.append(f"{name}: the Worker is running but the Scheduler cannot be queried")
    else:
        node = next((item for item in scheduler_nodes if item["id"] == name), None)
        if node and node["allocated"]["jobs"]:
            jobs = node["allocated"]["jobs"]
            reasons.append(f"{name}: the Scheduler still holds {jobs} unreleased attempt(s)")
    return reasons


def scheduler_restart_blockers(
    *, scheduler_active: bool, scheduler_nodes: list[dict] | None
) -> list[str]:
    """Scheduler restart recovery (M6.6) is missing, so no attempt may be outstanding."""
    if scheduler_nodes is None:
        return ["the Scheduler is running but cannot be queried"] if scheduler_active else []
    busy = [
        f"{n['id']} ({n['allocated']['jobs']})" for n in scheduler_nodes if n["allocated"]["jobs"]
    ]
    return [f"the Scheduler holds unreleased attempts on: {', '.join(busy)}"] if busy else []


# ---- Execution ---------------------------------------------------------------------------


class Runner:
    def __init__(self, inventory: Inventory, *, dry_run: bool = False):
        self.inventory = inventory
        self.dry_run = dry_run

    def _options(self) -> list[str]:
        return [
            "-i",
            str(self.inventory.ssh_key),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.inventory.known_hosts}",
        ]

    def target(self, node: Node) -> str:
        return f"{self.inventory.account}@{node.host}"

    def _run(self, args, *, capture, check, stdin=None, show):
        if self.dry_run:
            print(f"[dry-run] {show}", flush=True)
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.run(
            [str(arg) for arg in args],
            check=check,
            text=True,
            input=stdin,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            cwd=ROOT,
        )

    def ssh(self, node: Node, command: str, *, capture=False, check=True, stdin=None):
        show = f"ssh {self.target(node)} {shlex.quote(command)}"
        if stdin is not None:
            show += f"  <<< ({len(stdin.splitlines())}-line script)"
        return self._run(
            ["ssh", *self._options(), self.target(node), command],
            capture=capture,
            check=check,
            stdin=stdin,
            show=show,
        )

    def scp(self, node: Node, source: Path, target: str):
        return self._run(
            ["scp", "-q", *self._options(), source, f"{self.target(node)}:{target}"],
            capture=False,
            check=True,
            show=f"scp {source} {self.target(node)}:{target}",
        )

    def local(self, args, *, capture=False):
        return self._run(args, capture=capture, check=True, show=" ".join(map(str, args)))


def write_private(path: Path, content: str, *, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] write {path} (mode 600, {len(content)} bytes)", flush=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(content)
    path.chmod(0o600)


def local_uv() -> str:
    version = subprocess.run([UV, "--version"], check=True, text=True, capture_output=True).stdout
    if version.split()[1] != UV_VERSION:
        raise RuntimeError(f"expected uv {UV_VERSION} on this machine, found: {version.strip()}")
    return str(Path(UV).resolve())


def scheduler_nodes(inventory: Inventory, tokens: dict | None = None) -> list[dict] | None:
    """The Scheduler's node view, or None when it cannot be queried."""
    import httpx

    token = None
    cli = STATE / "cli.toml"
    if cli.exists():
        token = tomllib.loads(cli.read_text()).get("user_token")
    if token is None and tokens:
        token = next(iter(tokens["users"].values()), None)
    if not token or token.startswith("<"):
        return None
    try:
        response = httpx.get(
            inventory.scheduler_url + "/api/nodes",
            headers={"Authorization": f"Bearer {token}"},
            timeout=3,
            trust_env=False,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError:
        return None


def healthy(inventory: Inventory) -> bool:
    import httpx

    try:
        return (
            httpx.get(inventory.scheduler_url + "/health", timeout=2, trust_env=False).status_code
            == 200
        )
    except httpx.HTTPError:
        return False


def wait_for(predicate, timeout: float, description: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for {description}")


def remote_services(run: Runner, node: Node, units: list[str]) -> tuple[dict, list[str]]:
    """Unit states and LabFlux job containers on one node."""
    command = (
        "; ".join(f"echo {unit}=$(systemctl --user is-active {unit})" for unit in units)
        + "; docker ps -a --filter label=labflux.managed=true --format 'container={{.Names}}'"
    )
    output = run.ssh(node, command, capture=True).stdout
    states, containers = {}, []
    for line in output.splitlines():
        key, _, value = line.partition("=")
        if key == "container":
            containers.append(value)
        elif key:
            states[key] = value
    return states, containers


def ensure_restart_is_safe(run: Runner, inventory: Inventory, nodes: list[Node], tokens) -> None:
    checked = [
        n for n in nodes if WORKER_RESTART_REQUIRES_IDLE or n.name == inventory.scheduler_node
    ]
    if not checked:
        return
    view = None if run.dry_run else scheduler_nodes(inventory, tokens)
    reasons = []
    for node in checked:
        is_scheduler = node.name == inventory.scheduler_node
        units = [WORKER_UNIT] + ([SCHEDULER_UNIT] if is_scheduler else [])
        states, containers = remote_services(run, node, units)
        if WORKER_RESTART_REQUIRES_IDLE:
            reasons += restart_blockers(
                node.name,
                worker_active=states.get(WORKER_UNIT) == "active",
                containers=containers,
                scheduler_nodes=view,
            )
        if is_scheduler:
            reasons += scheduler_restart_blockers(
                scheduler_active=states.get(SCHEDULER_UNIT) == "active", scheduler_nodes=view
            )
    if reasons:
        raise RuntimeError(
            "refusing to restart LabFlux services while work is outstanding "
            "(Scheduler restart recovery, M6.6, is not implemented yet):\n  " + "\n  ".join(reasons)
        )


def build_artifacts(run: Runner) -> tuple[Path, Path]:
    build = STATE / "build"
    if not run.dry_run:
        shutil.rmtree(build, ignore_errors=True)
        build.mkdir(parents=True)
    run.local([UV, "build", "--quiet", "--wheel", "--out-dir", build])
    requirements = build / "requirements.txt"
    # Hashes stay in the export, so remote installs verify every locked dependency.
    run.local(
        [UV, "export", "--quiet", "--frozen", "--no-dev", "--no-emit-project", "-o", requirements]
    )
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    wheel = build / f"labflux-{version}-py3-none-any.whl"
    if not run.dry_run and not wheel.exists():
        raise RuntimeError(f"uv build did not produce {wheel.name}")
    return wheel, requirements


def install_node(run, inventory, node, wheel, requirements, uv_binary, files) -> None:
    is_scheduler = node.name == inventory.scheduler_node
    units = [WORKER_UNIT] + ([SCHEDULER_UNIT] if is_scheduler else [])
    hostname = run.ssh(node, "hostname -s", capture=True).stdout.strip()
    if not run.dry_run and hostname != node.name:
        raise RuntimeError(f"{node.host} reports hostname {hostname!r}, expected {node.name!r}")
    run.ssh(
        node,
        "mkdir -p ~/.local/bin ~/.config/systemd/user ~/labflux/images/python-uv"
        " && chmod 700 ~/labflux && rm -f ~/labflux/*.whl",
    )
    run.scp(node, Path(uv_binary), ".local/bin/uv")
    run.scp(node, wheel, f"labflux/{wheel.name}")
    run.scp(node, requirements, "labflux/requirements.txt")
    run.scp(node, ROOT / "images/python-uv/Dockerfile", "labflux/images/python-uv/Dockerfile")
    for remote, local in files[node.name].items():
        run.scp(node, local, remote)
    run.ssh(node, "chmod 600 ~/labflux/*.toml")
    run.ssh(node, f"docker build --quiet -t {BASE_IMAGE} ~/labflux/images/python-uv")
    # Re-check right before stopping: the Scheduler may have dispatched since the first check.
    ensure_restart_is_safe(run, inventory, [node], None)
    run.ssh(node, f"systemctl --user stop {' '.join(units)}", check=False)
    python = "~/labflux/.venv/bin/python"
    # The deploy uses the account's default uv cache, never the job-writable ~/labflux/uv-cache.
    run.ssh(
        node,
        "set -e; uv=~/.local/bin/uv; "
        "$uv venv --quiet --clear --python 3.12 --python-preference only-managed ~/labflux/.venv; "
        f"$uv pip install --quiet --python {python} -r ~/labflux/requirements.txt; "
        f"$uv pip install --quiet --python {python} --no-deps --reinstall ~/labflux/{wheel.name}; "
        "~/labflux/.venv/bin/labflux version",
    )
    run.ssh(node, f"systemctl --user daemon-reload && systemctl --user enable {' '.join(units)}")
    if is_scheduler:
        run.ssh(node, f"systemctl --user restart {SCHEDULER_UNIT}")
        if not run.dry_run:
            wait_for(lambda: healthy(inventory), 30, "Scheduler health")
    run.ssh(node, f"systemctl --user restart {WORKER_UNIT}")


def deploy(inventory: Inventory, names: list[str], *, dry_run: bool) -> None:
    run = Runner(inventory, dry_run=dry_run)
    unknown = set(names) - {n.name for n in inventory.nodes}
    if unknown:
        raise RuntimeError(f"unknown node(s): {', '.join(sorted(unknown))}")
    # The Scheduler node goes first so that Workers can register.
    selected = sorted(
        (n for n in inventory.nodes if not names or n.name in names),
        key=lambda n: n.name != inventory.scheduler_node,
    )
    uv_binary = local_uv()
    tokens = load_tokens(inventory, create=not dry_run)
    validate_generated(inventory, tokens)
    with_scheduler = any(n.name == inventory.scheduler_node for n in selected)
    if with_scheduler and inventory.public_bind:
        state = run.ssh(
            inventory.scheduler,
            f"systemctl is-active {FIREWALL_UNIT}",
            capture=True,
            check=False,
        )
        if not dry_run and state.stdout.strip() != "active":
            raise RuntimeError(
                f"{FIREWALL_UNIT} is not active on {inventory.scheduler_node}; the Scheduler "
                "would listen on a public address. Run `firewall-instructions` first."
            )
    if not with_scheduler and not dry_run and not healthy(inventory):
        raise RuntimeError("the Scheduler is not reachable; deploy its node first")
    ensure_restart_is_safe(run, inventory, selected, tokens)

    wheel, requirements = build_artifacts(run)
    files = {}
    for node in selected:
        local = STATE / f"{node.name}-worker.toml"
        write_private(local, worker_toml(inventory, node, tokens["worker"]), dry_run=dry_run)
        units = STATE / "units" / node.name
        write_private(units / WORKER_UNIT, worker_unit(), dry_run=dry_run)
        files[node.name] = {
            "labflux/worker.toml": local,
            f".config/systemd/user/{WORKER_UNIT}": units / WORKER_UNIT,
        }
        if node.name == inventory.scheduler_node:
            config = STATE / "scheduler.toml"
            write_private(config, scheduler_toml(inventory, tokens), dry_run=dry_run)
            write_private(units / SCHEDULER_UNIT, scheduler_unit(inventory), dry_run=dry_run)
            files[node.name]["labflux/scheduler.toml"] = config
            files[node.name][f".config/systemd/user/{SCHEDULER_UNIT}"] = units / SCHEDULER_UNIT
    me = getpass.getuser()
    if me in tokens["users"]:
        write_private(STATE / "cli.toml", cli_toml(inventory, tokens["users"][me]), dry_run=dry_run)

    for node in selected:
        print(f"== Deploying {node.name} ({run.target(node)})", flush=True)
        install_node(run, inventory, node, wheel, requirements, uv_binary, files)
        if not dry_run:
            registered = wait_for(
                lambda: next(
                    (
                        item
                        for item in scheduler_nodes(inventory, tokens) or []
                        if item["id"] == node.name and item["status"] == "ONLINE"
                    ),
                    None,
                ),
                60,
                f"{node.name} to register",
            )
            cards = ", ".join(gpu["name"] for gpu in registered["capacity"].get("gpus", []))
            print(f"{node.name} is ONLINE; GPUs: {cards or 'none'}", flush=True)
    print(f"Scheduler: {inventory.scheduler_url}", flush=True)
    if me in tokens["users"]:
        print(f"CLI config: {STATE / 'cli.toml'}", flush=True)


# ---- Check (7.1) -------------------------------------------------------------------------

PROBE = r"""
put() { printf '%s\t%s\n' "$1" "$(printf '%s' "$2" | tr '\n\t' ';;')"; }
put user "$(id -un)"
put hostname "$(hostname -s)"
put linger "$(loginctl show-user "$(id -un)" -p Linger --value 2>&1)"
put user_manager "$(systemctl --user is-system-running 2>&1)"
put docker_info "$(docker info --format '{{json .}}' 2>&1)"
root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)"
put disk_home "$(df -BG --output=avail "$HOME" 2>/dev/null | tail -1 | tr -dc '0-9')"
[ -n "$root" ] && put disk_docker "$(df -BG --output=avail "$root" 2>/dev/null | tail -1 | tr -dc '0-9')"
if command -v nvidia-smi >/dev/null 2>&1; then
  put gpus "$(nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used,driver_version --format=csv,noheader,nounits 2>&1)"
  put cuda "$(nvidia-smi 2>/dev/null | grep -o 'CUDA Version: [0-9.]*' | awk '{print $3}')"
  put compute_apps "$(nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory --format=csv,noheader,nounits 2>&1)"
fi
put nvidia_ctk "$(nvidia-ctk --version 2>/dev/null | head -1)"
put uv "$("$HOME/.local/bin/uv" --version 2>/dev/null)"
put worker_unit "$(systemctl --user is-active labflux-worker.service 2>&1)"
put firewall "$(systemctl is-active labflux-firewall.service 2>&1)"
timeout 3 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null
case $? in 0) put scheduler_tcp open;; 124) put scheduler_tcp timeout;; *) put scheduler_tcp closed;; esac
"""

GPU_PROBE = (
    "docker run --rm --label labflux.check=true --gpus all ubuntu nvidia-smi -L 2>&1; "
    "echo '---'; "
    "docker run --rm --label labflux.check=true ubuntu sh -c 'ls /dev | grep -c ^nvidia' 2>&1"
)


def parse_probe(output: str) -> dict:
    facts = {}
    for line in output.splitlines():
        key, _, value = line.partition("\t")
        if key:
            facts[key] = value
    return facts


def parse_gpus(value: str) -> list[dict]:
    cards = []
    for row in filter(None, value.split(";")):
        fields = [field.strip() for field in row.split(",")]
        if len(fields) == 6 and fields[0].isdigit():
            index, uuid, name, total, used, driver = fields
            cards.append(
                dict(
                    index=int(index),
                    uuid=uuid,
                    name=name,
                    total=int(total),
                    used=int(used),
                    driver=driver,
                )
            )
    return cards


def busy_cards(facts: dict, cards: list[dict]) -> list[str]:
    busy = {
        row.split(",")[0].strip() for row in filter(None, facts.get("compute_apps", "").split(";"))
    }
    return [
        f"GPU {card['index']} ({card['used']} MiB used"
        + (", compute process" if card["uuid"] in busy else "")
        + ")"
        for card in cards
        if card["uuid"] in busy or card["used"] >= IDLE_MEMORY_MIB
    ]


def evaluate(inventory: Inventory, node: Node, facts: dict, gpu_output: str | None) -> list:
    rows = []

    def row(name, ok, detail, *, warn=False, skip=False):
        status = "SKIP" if skip else ("OK" if ok else ("WARN" if warn else "FAIL"))
        rows.append((name, status, detail))

    row(
        "SSH login",
        facts.get("user") == inventory.account and facts.get("hostname") == node.name,
        f"{facts.get('user')}@{facts.get('hostname')}",
        warn=facts.get("user") == inventory.account,
    )
    row("linger", facts.get("linger") == "yes", facts.get("linger", "?"))
    row(
        "user systemd",
        facts.get("user_manager") == "running",
        facts.get("user_manager", "?"),
        warn=True,
    )
    try:
        info = json.loads(facts.get("docker_info", ""))
    except json.JSONDecodeError:
        info = None
    if info is None:
        row("Docker", False, facts.get("docker_info", "no output")[:120])
    else:
        row("Docker", True, f"server {info.get('ServerVersion')}")
        row("cgroup v2", str(info.get("CgroupVersion")) == "2", f"v{info.get('CgroupVersion')}")
        missing = [k for k in ("MemoryLimit", "SwapLimit", "CpuCfsQuota") if not info.get(k)]
        row(
            "Docker limits",
            not missing,
            "missing: " + ", ".join(missing) if missing else "memory, swap, CPU quota",
        )
        runtimes = sorted(info.get("Runtimes", {}))
        row(
            "default runtime",
            info.get("DefaultRuntime") != "nvidia",
            f"{info.get('DefaultRuntime')} (registered: {', '.join(runtimes)})",
        )
    cards = parse_gpus(facts.get("gpus", ""))
    if "gpus" not in facts:
        row("NVIDIA driver", not node.gpus, "nvidia-smi missing", skip=not node.gpus)
    else:
        row(
            "NVIDIA driver",
            bool(cards),
            f"{cards[0]['driver']}, CUDA {facts.get('cuda') or '?'}" if cards else facts["gpus"],
        )
        row(
            "GPUs",
            bool(cards),
            "; ".join(f"{c['index']} {c['name']} {c['total']} MiB" for c in cards) or "none",
        )
    wanted = {str(gpu) for gpu in node.gpus}
    present = {str(c["index"]) for c in cards} | {c["uuid"] for c in cards}
    row(
        "pooled GPUs",
        wanted <= present,
        f"inventory {list(node.gpus)}" + ("" if wanted <= present else " not all present"),
        skip=not node.gpus,
    )
    row(
        "nvidia-ctk",
        bool(facts.get("nvidia_ctk")),
        facts.get("nvidia_ctk") or "missing",
        skip=not node.gpus and not facts.get("nvidia_ctk"),
    )
    busy = busy_cards(facts, cards)
    if cards:
        row("GPUs idle", not busy, "; ".join(busy) or "no compute process, < 1 GiB used", warn=True)
    if gpu_output is None:
        row("GPU container", False, "skipped: a card is in use" if busy else "skipped", skip=True)
    else:
        listed, _, cpu_devices = gpu_output.partition("---")
        seen = set(re.findall(GPU_UUID, listed))
        row(
            "GPU container",
            seen == {c["uuid"] for c in cards},
            f"--gpus all sees {len(seen)}/{len(cards)} card(s)",
        )
        count = cpu_devices.strip().splitlines()[-1] if cpu_devices.strip() else "?"
        row("CPU container", count == "0", f"{count} /dev/nvidia* device(s) without --gpus")
    row(
        "uv (account)",
        facts.get("uv", "").split()[1:2] == [UV_VERSION],
        facts.get("uv") or f"missing (deploy installs uv {UV_VERSION})",
        warn=True,
    )
    disks = [
        (label, facts.get(key, ""))
        for label, key in (("home", "disk_home"), ("docker", "disk_docker"))
    ]
    low = [label for label, value in disks if value.isdigit() and int(value) < LOW_DISK_GIB]
    row(
        "free disk",
        not low,
        ", ".join(f"{label} {value or '?'} GiB" for label, value in disks),
        warn=True,
    )
    row("Worker unit", True, facts.get("worker_unit", "?"))
    tcp = facts.get("scheduler_tcp", "?")
    row(
        f"to {inventory.bind_host}:{inventory.port}",
        tcp == "open",
        {
            "open": "Scheduler port open",
            "closed": "route OK, nothing listening yet (connection refused)",
            "timeout": "no answer: blocked or unreachable",
        }.get(tcp, tcp),
        warn=tcp == "closed",
    )
    if node.name == inventory.scheduler_node and inventory.public_bind:
        row(
            "firewall unit",
            facts.get("firewall") == "active",
            facts.get("firewall", "?")
            + ("" if facts.get("firewall") == "active" else " (required before deploy)"),
            warn=True,
        )
    return rows


def print_table(title: str, rows: list) -> None:
    width = max(len(name) for name, _, _ in rows)
    print(f"\n== {title}")
    print(f"{'CHECK'.ljust(width)}  STATUS  DETAIL")
    for name, status, detail in rows:
        print(f"{name.ljust(width)}  {status.ljust(6)}  {detail}")


def check(inventory: Inventory) -> bool:
    run = Runner(inventory)
    healthy_overall = True
    for node in inventory.nodes:
        try:
            output = run.ssh(
                node,
                f"bash -s -- {inventory.bind_host} {inventory.port}",
                capture=True,
                stdin=PROBE,
            ).stdout
        except subprocess.CalledProcessError as exc:
            print_table(f"{node.name} ({run.target(node)})", [("SSH login", "FAIL", exc.stderr)])
            healthy_overall = False
            continue
        facts = parse_probe(output)
        cards = parse_gpus(facts.get("gpus", ""))
        gpu_output = None
        if cards and not busy_cards(facts, cards):
            gpu_output = run.ssh(node, GPU_PROBE, capture=True, check=False).stdout
        rows = evaluate(inventory, node, facts, gpu_output)
        print_table(f"{node.name} ({run.target(node)})", rows)
        healthy_overall &= all(status != "FAIL" for _, status, _ in rows)
    return healthy_overall


def status(inventory: Inventory) -> None:
    run = Runner(inventory)
    for node in inventory.nodes:
        units = [WORKER_UNIT] + ([SCHEDULER_UNIT] if node.name == inventory.scheduler_node else [])
        command = (
            "; ".join(f"echo {u}: $(systemctl --user is-active {u})" for u in units)
            + "; echo labflux: $(~/labflux/.venv/bin/labflux version 2>/dev/null || echo missing)"
            + "; echo uv: $(~/.local/bin/uv --version 2>/dev/null || echo missing)"
        )
        if node.name == inventory.scheduler_node:
            command += f"; echo {FIREWALL_UNIT}: $(systemctl is-active {FIREWALL_UNIT})"
        result = run.ssh(node, command, capture=True, check=False)
        print(f"\n== {node.name} ({run.target(node)})")
        print(result.stdout.rstrip() or result.stderr.rstrip())
    print(
        f"\n== Scheduler {inventory.scheduler_url}: {'healthy' if healthy(inventory) else 'down'}"
    )
    for item in scheduler_nodes(inventory) or []:
        allocated = item["allocated"]
        cards = item["capacity"].get("gpus", [])
        print(
            f"{item['id']}: {item['status']}, reserved {allocated['cpu']}/{item['capacity']['cpu']}"
            f" CPU, {allocated['memory_mib']}/{item['capacity']['memory_mib']} MiB,"
            f" {allocated.get('gpu', 0)}/{len(cards)} GPU"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("action", choices=["check", "deploy", "status", "firewall-instructions"])
    parser.add_argument("--inventory", type=Path, default=INVENTORY)
    parser.add_argument("--node", action="append", default=[], help="deploy only this node")
    parser.add_argument("--dry-run", action="store_true", help="print commands, run nothing")
    options = parser.parse_args()
    try:
        lab = load_inventory(options.inventory)
        if options.action == "check":
            raise SystemExit(0 if check(lab) else 1)
        if options.action == "firewall-instructions":
            print(firewall_instructions(lab), end="")
        elif options.action == "deploy":
            deploy(lab, options.node, dry_run=options.dry_run)
        else:
            status(lab)
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Lab deployment failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
