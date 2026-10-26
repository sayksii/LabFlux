import base64
from concurrent.futures import ThreadPoolExecutor

from conftest import finish, register, submit, user_headers, worker_headers

from labflux.database import Database
from labflux.models import JobSpec


def test_authentication_and_ownership(client):
    assert client.get("/health").status_code == 200
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/jobs", headers=worker_headers()).status_code == 401
    job = submit(client)
    assert client.get(f"/api/jobs/{job['id']}", headers=user_headers("bob")).status_code == 404
    assert (
        client.post(f"/api/jobs/{job['id']}/cancel", headers=user_headers("bob")).status_code == 404
    )
    assert client.get("/api/jobs", headers=user_headers("bob")).json() == []


def test_worker_auth_is_separate_from_user_auth(client):
    response = client.post("/internal/workers/register", headers=user_headers(), json={})
    assert response.status_code == 401


def test_heterogeneous_placement(client):
    register(client, "small", cpu=1)
    register(client, "large", cpu=4)
    big = submit(client, cpu=3)
    small = submit(client, cpu=1)
    assert big["attempts"][0]["node_id"] == "large"
    assert small["attempts"][0]["node_id"] == "small"


def test_strict_fcfs_and_resource_release(client):
    register(client, "small", cpu=1)
    large_instance = register(client, "large", cpu=4)
    active = submit(client, cpu=4)
    head = submit(client, cpu=4)
    tail = submit(client, cpu=1)
    assert head["state"] == tail["state"] == "QUEUED"
    assert finish(client, active, large_instance).status_code == 200
    assert (
        client.get(f"/api/jobs/{head['id']}", headers=user_headers()).json()["state"] == "STARTING"
    )
    assert (
        client.get(f"/api/jobs/{tail['id']}", headers=user_headers()).json()["state"] == "STARTING"
    )


def test_memory_is_reserved_independently_from_cpu(client):
    register(client, cpu=8, memory=512)
    assert submit(client)["state"] == "STARTING"
    assert submit(client)["state"] == "STARTING"
    assert submit(client)["state"] == "QUEUED"
    node = client.get("/api/nodes", headers=user_headers()).json()[0]
    assert node["allocated"] == {"cpu": 2, "memory_mib": 512, "jobs": 2, "gpu": 0, "gpu_uuids": []}


def test_concurrent_submission_does_not_overallocate(client, service):
    register(client, cpu=4, memory=2048)
    spec = JobSpec(image="python:3.12-slim", argv=["true"])
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: service.submit("alice", spec), range(24)))
    assert len({job["id"] for job in results}) == 24
    assert sum(job["state"] == "STARTING" for job in results) == 4
    assert service.list_nodes()[0]["allocated"] == {
        "cpu": 4,
        "memory_mib": 1024,
        "jobs": 4,
        "gpu": 0,
        "gpu_uuids": [],
    }


def test_commands_are_stable_until_acknowledged(client):
    instance = register(client)
    submit(client)
    path = "/internal/workers/vm-a/commands"
    first = client.get(path, headers=worker_headers(), params={"instance_id": instance}).json()
    second = client.get(path, headers=worker_headers(), params={"instance_id": instance}).json()
    assert first == second and len(first) == 1
    for _ in range(2):
        assert (
            client.post(
                f"{path}/{first[0]['id']}/ack",
                headers=worker_headers(),
                json={"instance_id": instance},
            ).status_code
            == 200
        )
    assert client.get(path, headers=worker_headers(), params={"instance_id": instance}).json() == []


def test_duplicate_finish_and_delayed_start_cannot_regress_state(client):
    instance = register(client, cpu=1)
    job = submit(client)
    queued = submit(client)
    assert finish(client, job, instance).status_code == 200
    assert finish(client, job, instance).json() == {"duplicate": True}
    attempt = job["attempts"][0]
    response = client.post(
        f"/internal/workers/vm-a/attempts/{attempt['id']}/events",
        headers=worker_headers(),
        json={"instance_id": instance, "event_id": "late-start", "kind": "STARTED"},
    )
    assert response.json() == {"ignored": True}
    assert (
        client.get(f"/api/jobs/{job['id']}", headers=user_headers()).json()["state"] == "SUCCEEDED"
    )
    assert (
        client.get(f"/api/jobs/{queued['id']}", headers=user_headers()).json()["state"]
        == "STARTING"
    )
    assert client.get("/api/nodes", headers=user_headers()).json()[0]["allocated"]["cpu"] == 1


def test_failed_start_releases_reservation(client):
    instance = register(client, cpu=1)
    job = submit(client)
    queued = submit(client)
    assert finish(client, job, instance, outcome="FAILED", exit_code=None).status_code == 200
    assert (
        client.get(f"/api/jobs/{queued['id']}", headers=user_headers()).json()["state"]
        == "STARTING"
    )


def test_unconfirmed_termination_keeps_reservation(client):
    instance = register(client, cpu=1)
    job = submit(client)
    queued = submit(client)
    assert (
        finish(
            client, job, instance, outcome="UNKNOWN", exit_code=None, confirmed=False
        ).status_code
        == 200
    )
    assert client.get(f"/api/jobs/{job['id']}", headers=user_headers()).json()["state"] == "UNKNOWN"
    assert (
        client.get(f"/api/jobs/{queued['id']}", headers=user_headers()).json()["state"] == "QUEUED"
    )
    assert client.get("/api/nodes", headers=user_headers()).json()[0]["allocated"]["cpu"] == 1


def test_other_worker_cannot_finish_attempt(client):
    register(client)
    other = register(client, "vm-b")
    job = submit(client)
    node_id = job["attempts"][0]["node_id"]
    wrong = "vm-b" if node_id == "vm-a" else "vm-a"
    response = client.post(
        f"/internal/workers/{wrong}/attempts/{job['attempts'][0]['id']}/events",
        headers=worker_headers(),
        json={
            "instance_id": other,
            "event_id": "wrong-node",
            "kind": "FINISHED",
            "outcome": "SUCCEEDED",
            "termination_confirmed": True,
        },
    )
    assert response.status_code == 404


def test_log_upload_is_idempotent_and_owned(client):
    instance = register(client)
    job = submit(client)
    attempt = job["attempts"][0]
    path = f"/internal/workers/vm-a/attempts/{attempt['id']}/logs"
    payload = {
        "instance_id": instance,
        "seq": 0,
        "stream": "stdout",
        "data_base64": base64.b64encode(b"hello\n").decode(),
    }
    assert client.post(path, headers=worker_headers(), json=payload).status_code == 200
    assert client.post(path, headers=worker_headers(), json=payload).json() == {"duplicate": True}
    assert (
        client.post(
            path, headers=worker_headers(), json={**payload, "data_base64": "d3Jvbmc="}
        ).status_code
        == 409
    )
    response = client.get(f"/api/jobs/{job['id']}/logs", headers=user_headers()).json()
    assert response["next_seq"] == 0 and len(response["chunks"]) == 1
    assert client.get(f"/api/jobs/{job['id']}/logs", headers=user_headers("bob")).status_code == 404


def test_dataset_matching(client):
    register(client, "vm-a", datasets=["images"])
    register(client, "vm-b", cpu=4)
    job = submit(client, datasets=["images"])
    assert job["attempts"][0]["node_id"] == "vm-a"
    missing = submit(client, datasets=["unavailable"])
    assert missing["state"] == "QUEUED"


def test_schema_upgrade_and_persistence(settings):
    from labflux.scheduler import Scheduler

    first = Scheduler(settings)
    job = first.submit("alice", JobSpec(image="python:3.12-slim", argv=["true"]))
    first.db.upgrade()
    first.db.close()
    reopened = Scheduler(settings)
    assert reopened.get_job(job["id"], "alice")["state"] == "QUEUED"
    reopened.db.close()
    database = Database(settings.data_dir)
    database.upgrade()
    database.close()
