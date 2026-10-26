from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import finish, heartbeat, register, submit, user_headers, worker_headers
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from labflux.database import Database, attempt_gpus, schema_version
from labflux.models import Capacity, GpuDevice, JobSpec, ResourceRequest
from labflux.scheduler import Scheduler


def card(number, name="NVIDIA GeForce RTX 3090", memory=24576, index=None):
    return {
        "uuid": f"GPU-{number:08x}-0000-0000-0000-000000000000",
        "index": number if index is None else index,
        "name": name,
        "memory_total_mib": memory,
    }


RTX3090 = card(1, "NVIDIA GeForce RTX 3090", 24576, index=0)
A5000 = card(2, "NVIDIA RTX A5000", 24564, index=1)
RTX4090 = card(3, "NVIDIA GeForce RTX 4090", 24564, index=0)


def gpu_node(client, name, cards, cpu=8, memory=8192, external=()):
    instance = register(client, name, cpu=cpu, memory=memory, gpus=cards)
    heartbeat(client, name, instance, cards, external)
    return instance


def get_job(client, job):
    return client.get(f"/api/jobs/{job['id']}", headers=user_headers()).json()


def held(service, node_id):
    node = next(n for n in service.list_nodes() if n["id"] == node_id)
    return node["allocated"]["gpu_uuids"]


def test_count_model_and_vram_select_matching_cards(client, service):
    kayle = gpu_node(client, "kayle", [RTX3090, A5000])
    gpu_node(client, "karma", [RTX4090])
    on_4090 = submit(client, resources={"gpu": 1, "gpu_model": "4090"})
    on_a5000 = submit(client, resources={"gpu": 1, "gpu_model": "a5000"})
    large = submit(client, resources={"gpu": 1, "min_vram_mib": 24570})
    assert on_4090["attempts"][0]["node_id"] == "karma"
    assert on_4090["attempts"][0]["gpu_uuids"] == [RTX4090["uuid"]]
    assert on_a5000["attempts"][0]["gpu_uuids"] == [A5000["uuid"]]
    assert large["attempts"][0]["gpu_uuids"] == [RTX3090["uuid"]]
    assert large["attempts"][0]["resources"]["min_vram_mib"] == 24570
    start = {c["attempt_id"]: c for c in service.poll_commands("kayle", kayle)}
    assert start[large["current_attempt_id"]]["payload"]["gpu_uuids"] == [RTX3090["uuid"]]
    assert submit(client, resources={"gpu": 1})["state"] == "QUEUED"


def test_multi_card_job_stays_on_one_node(client, service):
    gpu_node(client, "kayle", [RTX3090, A5000])
    gpu_node(client, "karma", [RTX4090])
    pair = submit(client, resources={"gpu": 2})
    assert pair["attempts"][0]["node_id"] == "kayle"
    assert sorted(pair["attempts"][0]["gpu_uuids"]) == sorted([RTX3090["uuid"], A5000["uuid"]])
    # One free card remains on karma, but a job is never split across nodes.
    assert submit(client, resources={"gpu": 2})["state"] == "QUEUED"
    assert held(service, "karma") == []


def test_cpu_jobs_never_take_cards_and_gpu_jobs_skip_gpu_less_nodes(client, service):
    register(client, "cpu-only", cpu=32, memory=65536)
    gpu_node(client, "kayle", [RTX3090], cpu=2)
    cpu_job = submit(client, cpu=1)
    assert cpu_job["attempts"][0]["gpu_uuids"] == []
    gpu_job = submit(client, cpu=1, resources={"cpu": 1, "gpu": 1})
    assert gpu_job["attempts"][0]["node_id"] == "kayle"
    assert held(service, "cpu-only") == [] and held(service, "kayle") == [RTX3090["uuid"]]
    # The only card is taken; plenty of CPU on cpu-only does not make it eligible.
    assert submit(client, resources={"cpu": 1, "gpu": 1})["state"] == "QUEUED"


def test_external_and_unobserved_cards_are_not_allocated(client, service):
    instance = register(client, "kayle", cpu=8, memory=8192, gpus=[RTX3090, A5000])
    waiting = submit(client, resources={"gpu": 1})
    assert waiting["state"] == "QUEUED"  # Registered but not yet reported by a heartbeat.
    heartbeat(client, "kayle", instance, [RTX3090, A5000], external=[RTX3090["uuid"]])
    assert get_job(client, waiting)["attempts"][0]["gpu_uuids"] == [A5000["uuid"]]
    second = submit(client, resources={"gpu": 1})
    assert second["state"] == "QUEUED"
    heartbeat(client, "kayle", instance, [A5000])  # The 3090 is no longer reported at all.
    assert get_job(client, second)["state"] == "QUEUED"
    heartbeat(client, "kayle", instance, [RTX3090, A5000])
    assert get_job(client, second)["attempts"][0]["gpu_uuids"] == [RTX3090["uuid"]]
    node = service.list_nodes()[0]
    assert [g["external_use"] for g in node["observed"]["gpus"]] == [False, False]


def test_smallest_card_and_fewest_leftover_cards_are_preferred(client):
    big = card(10, "NVIDIA RTX A6000", 49140, index=0)
    small = card(11, "NVIDIA GeForce RTX 3090", 24576, index=1)
    gpu_node(client, "two-cards", [big, small])
    gpu_node(client, "one-card", [card(12, index=0)])
    assert submit(client, resources={"gpu": 1})["attempts"][0]["node_id"] == "one-card"
    assert submit(client, resources={"gpu": 1})["attempts"][0]["gpu_uuids"] == [small["uuid"]]
    assert submit(client, resources={"gpu": 1})["attempts"][0]["gpu_uuids"] == [big["uuid"]]


def test_gpu_head_job_blocks_fcfs_until_its_card_is_released(client, service):
    instance = gpu_node(client, "kayle", [RTX3090], cpu=8)
    active = submit(client, resources={"gpu": 1})
    head = submit(client, resources={"gpu": 1})
    tail = submit(client)
    assert head["state"] == tail["state"] == "QUEUED"
    assert (
        get_job(client, head)["reason"] == "waiting for matching CPU/RAM/GPU capacity or datasets"
    )
    assert finish(client, active, instance).status_code == 200
    assert get_job(client, head)["attempts"][0]["gpu_uuids"] == [RTX3090["uuid"]]
    assert get_job(client, tail)["state"] == "STARTING"


def test_concurrent_gpu_submissions_never_share_a_card(client, service):
    gpu_node(client, "kayle", [RTX3090, A5000], cpu=32, memory=65536)
    gpu_node(client, "karma", [RTX4090], cpu=32, memory=65536)
    spec = JobSpec(image="python:3.12-slim", argv=["true"], resources={"gpu": 1})
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: service.submit("alice", spec), range(24)))
    started = [job for job in results if job["state"] == "STARTING"]
    assigned = [uuid for job in started for uuid in job["attempts"][0]["gpu_uuids"]]
    assert len(started) == 3
    assert sorted(assigned) == sorted(c["uuid"] for c in (RTX3090, A5000, RTX4090))
    with service.db.transaction() as connection:
        active = connection.execute(
            select(func.count()).where(attempt_gpus.c.released.is_(False))
        ).scalar_one()
    assert active == 3


def test_late_or_duplicate_finish_cannot_release_the_next_jobs_card(client, service):
    instance = gpu_node(client, "kayle", [RTX3090])
    first = submit(client, resources={"gpu": 1})
    second = submit(client, resources={"gpu": 1})
    third = submit(client, resources={"gpu": 1})
    assert finish(client, first, instance).status_code == 200
    second = get_job(client, second)
    assert second["attempts"][0]["gpu_uuids"] == [RTX3090["uuid"]]
    assert finish(client, first, instance).json() == {"duplicate": True}
    late = finish(client, first, instance, event_id="late-finish-with-new-id")
    assert late.json() == {"ignored": True}
    assert held(service, "kayle") == [RTX3090["uuid"]]
    assert get_job(client, third)["state"] == "QUEUED"
    with service.db.transaction() as connection:
        rows = connection.execute(select(attempt_gpus)).all()
    by_attempt = {row.attempt_id: row.released for row in rows}
    assert by_attempt == {first["current_attempt_id"]: True, second["current_attempt_id"]: False}


def test_unconfirmed_termination_keeps_the_card_reserved(client, service):
    instance = gpu_node(client, "kayle", [RTX3090])
    job = submit(client, resources={"gpu": 1})
    queued = submit(client, resources={"gpu": 1})
    finish(client, job, instance, outcome="UNKNOWN", exit_code=None, confirmed=False)
    assert get_job(client, job)["state"] == "UNKNOWN"
    assert held(service, "kayle") == [RTX3090["uuid"]]
    assert get_job(client, queued)["state"] == "QUEUED"


def test_heartbeat_expiry_keeps_cards_until_stop_is_confirmed(client, service, clock):
    instance = gpu_node(client, "kayle", [RTX3090])
    job = submit(client, resources={"gpu": 1})
    clock[0] += 11
    service.tick()
    assert service.list_nodes()[0]["status"] == "OFFLINE"
    assert held(service, "kayle") == [RTX3090["uuid"]]
    client.post(f"/api/jobs/{job['id']}/cancel", headers=user_headers())
    finish(client, job, instance, outcome="CANCELLED", exit_code=137)
    assert get_job(client, job)["state"] == "CANCELLED"
    assert held(service, "kayle") == []


def test_database_rejects_a_second_unreleased_row_for_a_card(client, service):
    gpu_node(client, "kayle", [RTX3090, A5000])
    first = submit(client, resources={"gpu": 1})
    second = submit(client, resources={"gpu": 1})
    taken = first["attempts"][0]["gpu_uuids"][0]
    row = dict(attempt_id=second["current_attempt_id"], gpu_uuid=taken, node_id="kayle")
    with pytest.raises(IntegrityError):
        with service.db.transaction() as connection:
            connection.execute(attempt_gpus.insert().values(**row, released=False))
    # Released history rows for the same card are allowed.
    with service.db.transaction() as connection:
        connection.execute(attempt_gpus.insert().values(**row, released=True))


def test_card_pooled_by_another_worker_is_rejected(client):
    register(client, "kayle", gpus=[RTX3090])
    response = client.post(
        "/internal/workers/register",
        headers=worker_headers(),
        json={
            "worker_id": "duplicate",
            "instance_id": "e" * 32,
            "capacity": {
                "total_cpu": 2,
                "total_memory_mib": 2048,
                "cpu": 2,
                "memory_mib": 1024,
                "gpus": [RTX3090],
            },
        },
    )
    assert response.status_code == 409


def test_v1_database_upgrades_to_v2_and_keeps_jobs(settings):
    first = Scheduler(settings)
    job = first.submit("alice", JobSpec(image="python:3.12-slim", argv=["true"]))
    with first.db.transaction() as connection:
        attempt_gpus.drop(connection)  # Version 1 had every table except attempt_gpus.
        connection.execute(update(schema_version).values(version=1))
    first.db.close()
    database = Database(settings.data_dir)
    assert database.version == 2
    with database.transaction() as connection:
        assert connection.execute(select(func.count()).select_from(attempt_gpus)).scalar_one() == 0
    database.close()
    reopened = Scheduler(settings)
    assert reopened.get_job(job["id"], "alice")["state"] == "QUEUED"
    with reopened.db.transaction() as connection:
        connection.execute(update(schema_version).values(version=99))
    reopened.db.close()
    with pytest.raises(RuntimeError, match="unsupported schema version"):
        Database(settings.data_dir)


def test_gpu_request_and_inventory_validation(client):
    for bad in (
        {"gpu_model": "4090"},
        {"min_vram_mib": 1024},
        {"gpu": 9},
        {"gpu": -1},
        {"gpu": 1, "gpu_model": ""},
        {"gpu": 1, "min_vram_mib": 0},
    ):
        with pytest.raises(ValidationError):
            ResourceRequest(**bad)
    assert ResourceRequest(gpu=2, gpu_model="A5000", min_vram_mib=20000).gpu == 2
    response = client.post(
        "/api/jobs",
        headers=user_headers(),
        json={"image": "python:3.12-slim", "argv": ["true"], "resources": {"gpu_model": "4090"}},
    )
    assert response.status_code == 422
    with pytest.raises(ValidationError):
        GpuDevice(uuid="GPU-0", index=0, name="RTX", memory_total_mib=1)
    base = {"total_cpu": 2, "total_memory_mib": 2048, "cpu": 2, "memory_mib": 1024}
    for gpus in ([RTX3090, {**A5000, "uuid": RTX3090["uuid"]}], [RTX3090, {**A5000, "index": 0}]):
        with pytest.raises(ValidationError):
            Capacity(**base, gpus=gpus)
