import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from labflux.config import Settings

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("lab_deploy", ROOT / "scripts" / "lab_deploy.py")
lab = importlib.util.module_from_spec(_spec)
sys.modules["lab_deploy"] = lab  # dataclasses resolve annotations through sys.modules.
_spec.loader.exec_module(lab)

KAYLE_UUID = "GPU-11111111-2222-3333-4444-555555555555"


def inventory_values(**scheduler):
    return {
        "account": "labflux",
        "ssh_key": "~/.ssh/labflux_deploy",
        "known_hosts": ".labflux/lab/known_hosts",
        "scheduler": {
            "node": "kayle",
            "bind_host": "192.0.2.10",
            "port": 8770,
            "users": ["alice"],
            **scheduler,
        },
        "nodes": [
            {
                "name": "kayle",
                "host": "192.0.2.10",
                "allocatable_cpu": 16,
                "allocatable_memory_mib": 49152,
                "gpus": [0, KAYLE_UUID],
            },
            {
                "name": "karma",
                "host": "192.0.2.20",
                "allocatable_cpu": 16,
                "allocatable_memory_mib": 24576,
                "gpus": [0],
                "datasets": {"mnist": "/srv/datasets/mnist"},
            },
        ],
    }


@pytest.fixture
def inventory():
    return lab.parse_inventory(inventory_values(), root=ROOT)


def tokens():
    return {"worker": "worker-secret", "users": {"alice": "user-secret"}}


def test_inventory_files_parse():
    example = lab.parse_inventory(tomllib.loads((ROOT / "configs/lab.example.toml").read_text()))
    assert example.scheduler.name == "node-a" and example.public_bind
    assert example.known_hosts == ROOT / ".labflux/lab/known_hosts"
    assert example.models
    lab.validate_generated(example, tokens())


def test_inventory_fields(inventory):
    assert inventory.scheduler_url == "http://192.0.2.10:8770"
    assert inventory.ssh_key == Path("~/.ssh/labflux_deploy").expanduser()
    assert [node.name for node in inventory.nodes] == ["kayle", "karma"]


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda v: v["nodes"][0].update(gpus=[0, 0]), "listed twice"),
        (lambda v: v["nodes"][0].update(gpus=[True]), "NVML indexes"),
        (lambda v: v["nodes"][0].update(gpus=["GPU-0"]), "NVML indexes"),
        (lambda v: v["nodes"][1].update(name="kayle"), "unique"),
        (lambda v: v["nodes"][1].update(allocatable_cpu=0), "allocatable"),
        (lambda v: v["scheduler"].update(node="missing"), "scheduler.node"),
        (lambda v: v["scheduler"].update(port=80), "unprivileged"),
        (lambda v: v["scheduler"].update(users=[]), "users"),
        (lambda v: v["scheduler"].update(policy="easy"), "scheduler.policy must be one of"),
        (
            lambda v: v.update(models={"m": {"image": "x", "argv": ["y"], "datasets": ["hf"]}}),
            "model 'm': no inventory node provides dataset 'hf'",
        ),
    ],
)
def test_inventory_rejects_invalid_values(change, message):
    values = inventory_values()
    change(values)
    with pytest.raises(ValueError, match=message):
        lab.parse_inventory(values)


def test_firewall_chain_passes_loopback_and_every_remote_worker_then_drops():
    values = inventory_values()
    values["nodes"].append(
        {
            "name": "third",
            "host": "192.0.2.30",
            "allocatable_cpu": 1,
            "allocatable_memory_mib": 1024,
        }
    )
    inventory = lab.parse_inventory(values, root=ROOT)
    jump = "INPUT -d 192.0.2.10 -p tcp --dport 8770 -j LABFLUX-SCHEDULER"
    install, remove = lab.firewall_rules(inventory)
    assert install == [
        "-N LABFLUX-SCHEDULER",
        "-A LABFLUX-SCHEDULER -i lo -j RETURN",
        "-A LABFLUX-SCHEDULER -s 192.0.2.20 -j RETURN",
        "-A LABFLUX-SCHEDULER -s 192.0.2.30 -j RETURN",
        "-A LABFLUX-SCHEDULER -j DROP",
        f"-I {jump}",  # Last: the chain is complete before traffic reaches it.
    ]
    assert remove == [f"-D {jump}", "-F LABFLUX-SCHEDULER", "-X LABFLUX-SCHEDULER"]
    unit = lab.firewall_unit(inventory)
    iptables = "/usr/sbin/iptables -w"
    assert unit.count("ExecStart=") == len(install)
    assert all(f"ExecStart={iptables} {rule}\n" in unit for rule in install)
    # Leftovers of an interrupted start, or an already removed rule, must not fail the unit.
    assert all(f"ExecStartPre=-{iptables} {rule}\n" in unit for rule in remove)
    assert all(f"ExecStop=-{iptables} {rule}\n" in unit for rule in remove)
    assert "ACCEPT" not in unit and "ufw" not in unit
    instructions = lab.firewall_instructions(inventory)
    assert "sudo systemctl restart labflux-firewall.service" in instructions
    assert "sudo iptables -S LABFLUX-SCHEDULER" in instructions and "disable --now" in instructions


def test_firewall_chain_without_remote_workers_allows_only_loopback():
    values = inventory_values()
    del values["nodes"][1]
    install, _ = lab.firewall_rules(lab.parse_inventory(values, root=ROOT))
    assert install[1:3] == ["-A LABFLUX-SCHEDULER -i lo -j RETURN", "-A LABFLUX-SCHEDULER -j DROP"]


def test_generated_tomls_are_valid_settings(inventory):
    kayle, karma = inventory.nodes
    worker = Settings.model_validate(
        tomllib.loads(lab.worker_toml(inventory, kayle, "worker-secret"))
    )
    assert worker.worker_id == "kayle" and worker.gpus == [0, KAYLE_UUID]
    assert worker.allocatable_cpu == 16 and worker.allocatable_memory_mib == 49152
    assert worker.scheduler_url == inventory.scheduler_url
    assert worker.uv_cache_dir == Path("~/labflux/uv-cache").expanduser()
    assert worker.data_dir == Path("~/labflux/worker-data").expanduser()
    remote = Settings.model_validate(
        tomllib.loads(lab.worker_toml(inventory, karma, "worker-secret"))
    )
    assert remote.datasets == {"mnist": Path("/srv/datasets/mnist")}
    scheduler = Settings.model_validate(tomllib.loads(lab.scheduler_toml(inventory, tokens())))
    assert (scheduler.bind_host, scheduler.bind_port) == ("192.0.2.10", 8770)
    assert scheduler.users == {"alice": "user-secret"}
    assert scheduler.worker_token == "worker-secret"
    cli = Settings.model_validate(tomllib.loads(lab.cli_toml(inventory, "user-secret")))
    assert cli.user_token == "user-secret" and cli.scheduler_url == inventory.scheduler_url
    lab.validate_generated(inventory, tokens())


@pytest.mark.parametrize("policy", ["fcfs", "priority", "backfill"])
def test_scheduling_policy_is_written_to_scheduler_toml(policy):
    inventory = lab.parse_inventory(inventory_values(policy=policy), root=ROOT)
    generated = lab.scheduler_toml(inventory, tokens())
    assert Settings.model_validate(tomllib.loads(generated)).scheduling_policy == policy
    lab.validate_generated(inventory, tokens())


def test_scheduler_toml_without_a_policy_is_unchanged(inventory):
    """Without the key no line is written, so the Scheduler keeps its default fcfs."""
    default = lab.scheduler_toml(inventory, tokens())
    assert inventory.policy is None and "scheduling_policy" not in default
    assert Settings.model_validate(tomllib.loads(default)).scheduling_policy == "fcfs"
    chosen = lab.parse_inventory(inventory_values(policy="backfill"), root=ROOT)
    generated = lab.scheduler_toml(chosen, tokens())
    assert generated.replace('scheduling_policy = "backfill"\n', "") == default


def test_models_pass_through_to_scheduler_toml():
    values = inventory_values()
    values["nodes"][0]["datasets"] = {"hf-models": "/srv/models/hf"}
    values["models"] = {
        "Qwen/Qwen2.5-7B:v1.5": {
            "image": "vllm/vllm-openai",
            "argv": ["Qwen/Qwen2.5-7B", "--served-model-name=Qwen/Qwen2.5-7B:v1.5"],
            "env": {"HF_HUB_OFFLINE": "1", "HF_HOME": "/datasets/hf-models", "Q": 'a "b" \\c'},
            "resources": {"cpu": 8, "memory_mib": 32768, "gpu": 2, "gpu_model": "RTX A5000"},
            "datasets": ["hf-models", "mnist"],
            "mode": "persistent",
            "startup_timeout_s": 600,
        },
        "tiny": {"image": "example/tiny", "argv": ["serve"]},
    }
    inventory = lab.parse_inventory(values)
    generated = lab.scheduler_toml(inventory, tokens())
    assert '[models."Qwen/Qwen2.5-7B:v1.5"]' in generated and "[models.tiny]" in generated
    models = Settings.model_validate(tomllib.loads(generated)).models
    qwen = models["Qwen/Qwen2.5-7B:v1.5"]
    assert qwen.argv == values["models"]["Qwen/Qwen2.5-7B:v1.5"]["argv"]
    assert qwen.env == {"HF_HUB_OFFLINE": "1", "HF_HOME": "/datasets/hf-models", "Q": 'a "b" \\c'}
    assert (qwen.resources.gpu, qwen.resources.gpu_model) == (2, "RTX A5000")
    assert (qwen.resources.cpu, qwen.resources.memory_mib) == (8, 32768)
    assert qwen.datasets == ["hf-models", "mnist"]
    assert (qwen.mode, qwen.startup_timeout_s) == ("persistent", 600)
    assert models["tiny"].mode == "on_demand" and models["tiny"].argv == ["serve"]
    lab.validate_generated(inventory, tokens())


def test_units_restart_on_failure_and_scheduler_requires_firewall(inventory):
    worker = lab.worker_unit()
    assert "ExecStart=%h/labflux/.venv/bin/labflux --config %h/labflux/worker.toml worker" in worker
    assert "Restart=on-failure" in worker and "TimeoutStopSec=60" in worker
    assert "WantedBy=default.target" in worker
    guard = "ExecStartPre=/usr/bin/systemctl is-active --quiet labflux-firewall.service"
    assert guard in lab.scheduler_unit(inventory)
    local = lab.parse_inventory(inventory_values(bind_host="127.0.0.1"))
    assert guard not in lab.scheduler_unit(local)


def test_tokens_are_reused_and_removed_users_lose_theirs(inventory, tmp_path):
    created = lab.load_tokens(inventory, tmp_path, create=True)
    assert len(created["worker"]) >= 40 and len(created["users"]["alice"]) >= 40
    previous = {**created, "users": {**created["users"], "former": "old-token"}}
    (tmp_path / "scheduler.toml").write_text(lab.scheduler_toml(inventory, previous))
    reused = lab.load_tokens(inventory, tmp_path, create=True)
    assert reused == created
    placeholder = lab.load_tokens(inventory, tmp_path / "empty", create=False)
    assert placeholder["worker"].startswith("<")


def test_restart_guard():
    idle = [{"id": "kayle", "allocated": {"jobs": 0}}]
    busy = [{"id": "kayle", "allocated": {"jobs": 2}}]
    common = dict(worker_active=True, containers=[])
    assert lab.restart_blockers("kayle", scheduler_nodes=idle, **common) == []
    assert "2 unreleased" in lab.restart_blockers("kayle", scheduler_nodes=busy, **common)[0]
    assert "cannot be queried" in lab.restart_blockers("kayle", scheduler_nodes=None, **common)[0]
    assert (
        lab.restart_blockers("kayle", worker_active=False, containers=[], scheduler_nodes=None)
        == []
    )
    blocked = lab.restart_blockers(
        "kayle", worker_active=False, containers=["labflux-abc"], scheduler_nodes=idle
    )
    assert "labflux-abc" in blocked[0]
    assert lab.scheduler_restart_blockers(scheduler_active=True, scheduler_nodes=idle) == []
    assert (
        "kayle (2)"
        in lab.scheduler_restart_blockers(scheduler_active=True, scheduler_nodes=busy)[0]
    )
    assert lab.WORKER_RESTART_REQUIRES_IDLE is False  # M6.4 adoption is verified.
    assert lab.SCHEDULER_RESTART_REQUIRES_IDLE is False  # M6.6 restart recovery is verified.


def test_dry_run_executes_nothing(inventory, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run must not execute commands")

    monkeypatch.setattr(subprocess, "run", forbidden)
    run = lab.Runner(inventory, dry_run=True)
    assert run.ssh(inventory.nodes[1], "systemctl --user restart x", capture=True).stdout == ""
    run.scp(inventory.nodes[1], Path("/tmp/file"), "labflux/file")
    target = Path("/nonexistent/scheduler.toml")
    lab.write_private(target, "TOKEN-VALUE", dry_run=True)
    output = capsys.readouterr().out
    assert "[dry-run] ssh labflux@192.0.2.20" in output and "[dry-run] scp" in output
    assert "TOKEN-VALUE" not in output and not target.exists()


def test_probe_parsing_and_evaluation(inventory):
    facts = lab.parse_probe(
        "user\tlabflux\nhostname\tkarma\nlinger\tyes\nuser_manager\trunning\n"
        'docker_info\t{"ServerVersion": "29.1.3", "CgroupVersion": "2", "MemoryLimit": true,'
        ' "SwapLimit": true, "CpuCfsQuota": true, "DefaultRuntime": "runc",'
        ' "Runtimes": {"runc": {}, "nvidia": {}}}\n'
        "gpus\t0, GPU-66666666-7777-8888-9999-aaaaaaaaaaaa, NVIDIA GeForce RTX 4090,"
        " 24564, 87, 580.178.04\n"
        "cuda\t13.0\ncompute_apps\t\nnvidia_ctk\tNVIDIA Container Toolkit CLI version 1.20.1\n"
        "uv\t\ndisk_home\t3467\ndisk_docker\t3467\nworker_unit\tinactive\n"
        "scheduler_tcp\tclosed\n"
    )
    cards = lab.parse_gpus(facts["gpus"])
    assert cards[0]["name"] == "NVIDIA GeForce RTX 4090" and cards[0]["used"] == 87
    assert lab.busy_cards(facts, cards) == []
    gpu_output = (
        "GPU 0: NVIDIA GeForce RTX 4090 (UUID: GPU-66666666-7777-8888-9999-aaaaaaaaaaaa)\n---\n0\n"
    )
    rows = {
        name: status
        for name, status, _ in lab.evaluate(inventory, inventory.nodes[1], facts, gpu_output)
    }
    assert rows["GPU container"] == rows["CPU container"] == rows["default runtime"] == "OK"
    assert rows["uv (account)"] == "WARN" and rows["to 192.0.2.10:8770"] == "WARN"
    assert "FAIL" not in rows.values()
    busy = {**facts, "compute_apps": "GPU-66666666-7777-8888-9999-aaaaaaaaaaaa, 4242, 2048"}
    assert lab.busy_cards(busy, cards) == ["GPU 0 (87 MiB used, compute process)"]
    nvidia_default = {
        **facts,
        "docker_info": facts["docker_info"].replace('"runc",', '"nvidia",', 1),
    }
    rows = {n: s for n, s, _ in lab.evaluate(inventory, inventory.nodes[1], nvidia_default, None)}
    assert rows["default runtime"] == "FAIL" and rows["GPU container"] == "SKIP"
