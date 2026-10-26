import uuid

import pytest
from fastapi.testclient import TestClient

from labflux.api import create_app
from labflux.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, users={"alice": "alice-token", "bob": "bob-token"})


@pytest.fixture
def clock():
    return [1000.0]


@pytest.fixture
def client(settings, clock):
    app = create_app(settings, run_loop=False, clock=lambda: clock[0])
    with TestClient(app) as client:
        yield client


@pytest.fixture
def service(client):
    return client.app.state.scheduler


def user_headers(user="alice"):
    return {"Authorization": f"Bearer {user}-token"}


def worker_headers():
    return {"Authorization": "Bearer dev-worker-token"}


def register(client, name="vm-a", cpu=2, memory=1024, datasets=None, gpus=None):
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
                "datasets": datasets or [],
                "gpus": gpus or [],
            },
        },
    )
    assert response.status_code == 200, response.text
    return instance


def heartbeat(client, name, instance, gpus=(), external=()):
    """Report each card in gpus; UUIDs listed in external are in use outside LabFlux."""
    response = client.post(
        f"/internal/workers/{name}/heartbeat",
        headers=worker_headers(),
        json={
            "instance_id": instance,
            "gpus": [
                {
                    "uuid": gpu["uuid"],
                    "memory_used_mib": 0,
                    "utilization_percent": 0,
                    "external_use": gpu["uuid"] in external,
                }
                for gpu in gpus
            ],
        },
    )
    assert response.status_code == 200, response.text


def submit(client, cpu=1, memory=256, user="alice", **fields):
    response = client.post(
        "/api/jobs",
        headers=user_headers(user),
        json={
            "image": "python:3.12-slim",
            "argv": ["python", "-c", "print('hello')"],
            "resources": {"cpu": cpu, "memory_mib": memory},
            **fields,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def finish(
    client, job, instance, *, outcome="SUCCEEDED", exit_code=0, confirmed=True, event_id=None
):
    attempt = job["attempts"][-1]
    return client.post(
        f"/internal/workers/{attempt['node_id']}/attempts/{attempt['id']}/events",
        headers=worker_headers(),
        json={
            "event_id": event_id or f"finish-{attempt['id']}",
            "instance_id": instance,
            "kind": "FINISHED",
            "outcome": outcome,
            "exit_code": exit_code,
            "termination_confirmed": confirmed,
            "reason": "test completion",
        },
    )
