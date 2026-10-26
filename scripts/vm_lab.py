#!/usr/bin/env python3
"""Provision and operate ONLY the three LabFlux development VMs.

Run with libvirt group access: sg libvirt -c 'uv run python scripts/vm_lab.py provision'.
All generated keys, credentials, disks, logs and manifests stay in ignored .labflux/vms/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".labflux" / "vms"
PRESETS = [("labflux-vm-a", 1, 2048), ("labflux-vm-b", 2, 3072), ("labflux-vm-c", 4, 4096)]
BASE_URL = "https://cloud-images.ubuntu.com/noble/current/"
IMAGE = "noble-server-cloudimg-amd64.img"
UV = shutil.which("uv") or str(Path.home() / ".local/bin/uv")


def run(args, *, capture=False, check=True):
    return subprocess.run(
        [str(arg) for arg in args],
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        cwd=ROOT,
    )


def virsh(*args, capture=False, check=True):
    return run(["virsh", "-c", "qemu:///system", *args], capture=capture, check=check)


def ip_address(name):
    output = virsh("domifaddr", name, "--source", "lease", capture=True).stdout
    for line in output.splitlines():
        fields = line.split()
        if "ipv4" in fields:
            return fields[-1].split("/")[0]
    return None


def ssh(ip, command, *, capture=False, check=True):
    return run(
        [
            "ssh",
            "-i",
            STATE / "id_ed25519",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={STATE / 'known_hosts'}",
            f"ubuntu@{ip}",
            command,
        ],
        capture=capture,
        check=check,
    )


def scp(ip, source, target):
    return run(
        [
            "scp",
            "-q",
            "-i",
            STATE / "id_ed25519",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={STATE / 'known_hosts'}",
            source,
            f"ubuntu@{ip}:{target}",
        ]
    )


def wait_for(predicate, timeout, description):
    deadline = time.monotonic() + timeout
    last_notice = 0
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        if time.monotonic() - last_notice > 30:
            print(f"Waiting for {description}...", flush=True)
            last_notice = time.monotonic()
        time.sleep(2)
    raise RuntimeError(f"timed out waiting for {description}")


def prepare_image():
    path = STATE / "base.img"
    if not path.exists():
        print("Downloading official Ubuntu 24.04 cloud image", flush=True)
        urllib.request.urlretrieve(BASE_URL + "SHA256SUMS", STATE / "SHA256SUMS")
        urllib.request.urlretrieve(BASE_URL + IMAGE, path)
    expected = next(
        line.split()[0]
        for line in (STATE / "SHA256SUMS").read_text().splitlines()
        if line.split()[-1].lstrip("*") == IMAGE
    )
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise RuntimeError("Ubuntu cloud image checksum mismatch")
    (STATE / "image.sha256").write_text(actual + "\n")
    return path


def provision():
    STATE.mkdir(parents=True, exist_ok=True)
    for binary in ("virsh", "virt-install", "qemu-img", "cloud-localds", "ssh", "scp"):
        if not shutil.which(binary):
            raise RuntimeError(f"missing host tool: {binary}")
    virsh("list", "--all", capture=True)
    if "default" not in virsh("net-list", "--name", capture=True).stdout.splitlines():
        virsh("net-start", "default")
    key = STATE / "id_ed25519"
    if not key.exists():
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "labflux-local-vms", "-f", key])
    public = key.with_suffix(".pub").read_text().strip()
    base = prepare_image()
    for name, cpu, memory in PRESETS:
        exists = virsh("dominfo", name, capture=True, check=False).returncode == 0
        if not exists:
            disk = STATE / f"{name}.qcow2"
            run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", base, disk, "12G"])
            userdata = {
                "hostname": name,
                "manage_etc_hosts": True,
                "users": [
                    {
                        "name": "ubuntu",
                        "shell": "/bin/bash",
                        "groups": ["sudo", "docker"],
                        "sudo": "ALL=(ALL) NOPASSWD:ALL",
                        "ssh_authorized_keys": [public],
                        "lock_passwd": True,
                    }
                ],
                "ssh_pwauth": False,
                "package_update": True,
                "packages": ["docker.io", "python3-venv"],
                "runcmd": [
                    ["systemctl", "enable", "--now", "docker"],
                    ["usermod", "-aG", "docker", "ubuntu"],
                    ["mkdir", "-p", "/srv/labflux-datasets/demo"],
                    [
                        "sh",
                        "-c",
                        "printf 'labflux dataset\n' > /srv/labflux-datasets/demo/message.txt",
                    ],
                ],
            }
            user_path = STATE / f"{name}-user-data"
            user_path.write_text("#cloud-config\n" + json.dumps(userdata))
            meta_path = STATE / f"{name}-meta-data"
            meta_path.write_text(
                json.dumps(
                    {"instance-id": f"{name}-{secrets.token_hex(8)}", "local-hostname": name}
                )
            )
            seed = STATE / f"{name}-seed.iso"
            run(["cloud-localds", seed, user_path, meta_path])
            run(
                [
                    "virt-install",
                    "--connect",
                    "qemu:///system",
                    "--name",
                    name,
                    "--memory",
                    memory,
                    "--vcpus",
                    cpu,
                    "--osinfo",
                    "ubuntu24.04",
                    "--import",
                    "--disk",
                    f"path={disk},format=qcow2,bus=virtio",
                    "--disk",
                    f"path={seed},device=cdrom",
                    "--network",
                    "network=default,model=virtio",
                    "--graphics",
                    "none",
                    "--noautoconsole",
                ]
            )
        elif virsh("domstate", name, capture=True).stdout.strip() == "shut off":
            virsh("start", name)
    manifest = []
    for name, cpu, memory in PRESETS:
        address = wait_for(lambda: ip_address(name), 180, f"{name} DHCP")
        wait_for(
            lambda: ssh(address, "true", capture=True, check=False).returncode == 0,
            300,
            f"{name} SSH",
        )
        wait_for(
            lambda: (
                ssh(
                    address,
                    "test -f /var/lib/cloud/instance/boot-finished && sudo docker info >/dev/null 2>&1",
                    capture=True,
                    check=False,
                ).returncode
                == 0
            ),
            600,
            f"{name} cloud-init and Docker",
        )
        manifest.append({"name": name, "ip": address, "cpu": cpu, "memory_mib": memory})
        print(f"Ready: {name} {address}, {cpu} vCPU, {memory} MiB", flush=True)
    (STATE / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def deploy(port):
    import httpx

    manifest = json.loads((STATE / "manifest.json").read_text())
    net = ET.fromstring(virsh("net-dumpxml", "default", capture=True).stdout)
    bridge_ip = net.find("ip").attrib["address"]
    config = STATE / "scheduler.toml"
    if not config.exists():
        tokens = {"user": secrets.token_urlsafe(32), "worker": secrets.token_urlsafe(32)}
        config.write_text(
            f"data_dir = {json.dumps(str(STATE / 'scheduler-data'))}\n"
            f'bind_host = "{bridge_ip}"\nbind_port = {port}\nscheduler_url = "http://{bridge_ip}:{port}"\n'
            f'user_token = "{tokens["user"]}"\nworker_token = "{tokens["worker"]}"\n'
            f'[users]\ndemo = "{tokens["user"]}"\n'
        )
        config.chmod(0o600)
    import tomllib

    settings = tomllib.loads(config.read_text())
    url = settings["scheduler_url"]
    try:
        healthy = httpx.get(url + "/health", timeout=2, trust_env=False).status_code == 200
    except httpx.HTTPError:
        healthy = False
    if healthy:
        require_idle(settings)
    if not healthy:
        with (STATE / "scheduler.log").open("ab") as output:
            process = subprocess.Popen(
                [UV, "run", "labflux", "--config", str(config), "scheduler"],
                cwd=ROOT,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        (STATE / "scheduler.pid").write_text(str(process.pid))

        def ready():
            try:
                return httpx.get(url + "/health", timeout=1, trust_env=False).status_code == 200
            except httpx.HTTPError:
                return False

        wait_for(ready, 30, "Scheduler")
    run([UV, "build", "--wheel"])
    wheel = sorted((ROOT / "dist").glob("*.whl"))[-1]
    requirements = STATE / "requirements.txt"
    run(
        [
            UV,
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "-o",
            requirements,
        ],
        capture=True,
    )
    for vm in manifest:
        address, name = vm["ip"], vm["name"]
        worker_config = STATE / f"{name}.toml"
        worker_config.write_text(
            f'data_dir = "/home/ubuntu/labflux-state"\nworker_id = "{name}"\n'
            f'scheduler_url = "{url}"\nworker_token = "{settings["worker_token"]}"\n'
            f'[datasets]\ndemo = "/srv/labflux-datasets/demo"\n'
        )
        worker_config.chmod(0o600)
        ssh(address, "mkdir -p ~/labflux && python3 -m venv ~/labflux/.venv")
        scp(address, wheel, "/home/ubuntu/labflux/package.whl")
        # Wheel filenames must retain their distribution/version tags for pip.
        ssh(address, f"mv ~/labflux/package.whl ~/labflux/{shlex.quote(wheel.name)}")
        scp(address, requirements, "/home/ubuntu/labflux/requirements.txt")
        scp(address, worker_config, "/home/ubuntu/labflux/worker.toml")
        ssh(
            address,
            "~/labflux/.venv/bin/pip -q install -r ~/labflux/requirements.txt && "
            f"~/labflux/.venv/bin/pip -q install --no-deps --force-reinstall ~/labflux/{shlex.quote(wheel.name)}",
        )
        unit = STATE / "labflux-worker.service"
        unit.write_text(
            "[Unit]\nDescription=LabFlux Worker\nAfter=network-online.target docker.service\nRequires=docker.service\n"
            "[Service]\nUser=ubuntu\nGroup=ubuntu\nSupplementaryGroups=docker\nWorkingDirectory=/home/ubuntu/labflux\n"
            "ExecStart=/home/ubuntu/labflux/.venv/bin/labflux --config /home/ubuntu/labflux/worker.toml worker\n"
            "Restart=on-failure\nRestartSec=3\nTimeoutStopSec=20\n[Install]\nWantedBy=multi-user.target\n"
        )
        scp(address, unit, "/home/ubuntu/labflux/labflux-worker.service")
        ssh(
            address,
            "sudo cp ~/labflux/labflux-worker.service /etc/systemd/system/ && "
            "sudo systemctl daemon-reload && sudo systemctl enable labflux-worker && sudo systemctl restart labflux-worker",
        )
        # Image prefetch is setup time, separate from job execution and queue timings.
        ssh(address, "sudo docker pull python:3.12-slim >/dev/null")
        print(f"Deployed Worker: {name}", flush=True)
    print(f"Scheduler: {url}\nCLI config: {config}", flush=True)


def status():
    virsh("list", "--all")
    if (STATE / "manifest.json").exists():
        for vm in json.loads((STATE / "manifest.json").read_text()):
            result = ssh(vm["ip"], "systemctl is-active labflux-worker", capture=True, check=False)
            print(vm["name"], vm["ip"], result.stdout.strip() or "unreachable")


def stop():
    # Stop only resources created by this script. Disks and evidence are retained.
    config = STATE / "scheduler.toml"
    if config.exists():
        import tomllib

        require_idle(tomllib.loads(config.read_text()))
    for name, _, _ in PRESETS:
        if virsh("dominfo", name, capture=True, check=False).returncode == 0:
            virsh("shutdown", name, check=False)
    pid = STATE / "scheduler.pid"
    if pid.exists():
        number = int(pid.read_text())
        cmdline = Path(f"/proc/{number}/cmdline")
        if cmdline.exists() and str(STATE / "scheduler.toml").encode() in cmdline.read_bytes():
            if os.getpgid(number) == number:
                os.killpg(number, signal.SIGTERM)


def require_idle(settings):
    """Do not redeploy or shut down this development lab over outstanding work."""
    import httpx

    try:
        with httpx.Client(
            base_url=settings["scheduler_url"],
            trust_env=False,
            timeout=3,
            headers={"Authorization": f"Bearer {settings['user_token']}"},
        ) as client:
            nodes = client.get("/api/nodes")
            jobs = client.get("/api/jobs")
            nodes.raise_for_status()
            jobs.raise_for_status()
            if any(node["allocated"]["jobs"] for node in nodes.json()) or any(
                job["state"] not in {"SUCCEEDED", "FAILED", "CANCELLED"} for job in jobs.json()
            ):
                raise RuntimeError(
                    "finish or cancel outstanding jobs before deploying or stopping the VM lab"
                )
    except httpx.HTTPError as exc:
        raise RuntimeError(
            "cannot verify that the VM lab is idle; check Scheduler connectivity first"
        ) from exc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["provision", "deploy", "status", "stop"])
    parser.add_argument("--port", type=int, default=8765)
    options = parser.parse_args()
    try:
        if options.action == "provision":
            provision()
        elif options.action == "deploy":
            deploy(options.port)
        elif options.action == "status":
            status()
        else:
            stop()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"VM setup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
