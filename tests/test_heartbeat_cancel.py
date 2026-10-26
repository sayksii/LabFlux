from conftest import finish, register, submit, user_headers, worker_headers


def get_job(client, job):
    return client.get(f"/api/jobs/{job['id']}", headers=user_headers()).json()


def test_heartbeat_renews_deadline_then_expiry_retains_allocations(client, service, clock):
    instance = register(client, cpu=1)
    job = submit(client)
    clock[0] += 9
    response = client.post(
        "/internal/workers/vm-a/heartbeat",
        headers=worker_headers(),
        json={"instance_id": instance, "observed": {"cpu_percent": 50}},
    )
    assert response.status_code == 200
    clock[0] += 9
    service.tick()
    assert service.list_nodes()[0]["status"] == "ONLINE"
    clock[0] += 2
    service.tick()
    node = service.list_nodes()[0]
    assert node["status"] == "OFFLINE" and node["allocated"]["cpu"] == 1
    assert get_job(client, job)["state"] == "UNKNOWN"
    assert submit(client)["state"] == "QUEUED"
    assert service.poll_commands("vm-a", instance) == []


def test_queued_cancel_never_allocates_and_is_idempotent(client):
    register(client, cpu=1)
    submit(client)
    queued = submit(client)
    path = f"/api/jobs/{queued['id']}/cancel"
    assert client.post(path, headers=user_headers()).json()["state"] == "CANCELLED"
    again = client.post(path, headers=user_headers()).json()
    assert again["state"] == "CANCELLED" and again["attempts"] == []
    assert client.get("/api/nodes", headers=user_headers()).json()[0]["allocated"]["cpu"] == 1


def test_running_cancel_waits_for_proof_and_delayed_start_does_not_override(client, service):
    instance = register(client, cpu=1)
    job = submit(client)
    queued = submit(client)
    attempt_id = job["current_attempt_id"]
    event_path = f"/internal/workers/vm-a/attempts/{attempt_id}/events"
    started = {"instance_id": instance, "event_id": "started", "kind": "STARTED"}
    assert client.post(event_path, headers=worker_headers(), json=started).status_code == 200
    cancelled = client.post(f"/api/jobs/{job['id']}/cancel", headers=user_headers()).json()
    assert cancelled["state"] == "STOPPING"
    assert get_job(client, queued)["state"] == "QUEUED"
    commands = service.poll_commands("vm-a", instance)
    assert [command["kind"] for command in commands] == ["STOP"]
    client.post(event_path, headers=worker_headers(), json={**started, "event_id": "delayed-start"})
    assert get_job(client, job)["state"] == "STOPPING"
    assert service.list_nodes()[0]["allocated"]["cpu"] == 1
    finish(client, job, instance, outcome="CANCELLED", exit_code=137)
    assert get_job(client, job)["state"] == "CANCELLED"
    assert get_job(client, queued)["state"] == "STARTING"
    assert service.list_nodes()[0]["allocated"]["cpu"] == 1


def test_cancel_before_command_delivery_only_sends_stop(client, service):
    instance = register(client, cpu=1)
    job = submit(client)
    for _ in range(3):
        response = client.post(f"/api/jobs/{job['id']}/cancel", headers=user_headers())
        assert response.json()["state"] == "STOPPING"
    assert [command["kind"] for command in service.poll_commands("vm-a", instance)] == ["STOP"]


def test_offline_cancel_does_not_claim_success_until_worker_confirms(client, service, clock):
    instance = register(client, cpu=1)
    job = submit(client)
    clock[0] += 11
    service.tick()
    assert get_job(client, job)["state"] == "UNKNOWN"
    response = client.post(f"/api/jobs/{job['id']}/cancel", headers=user_headers())
    assert response.json()["state"] == "STOPPING"
    assert service.list_nodes()[0]["allocated"]["cpu"] == 1
    assert [command["kind"] for command in service.poll_commands("vm-a", instance)] == ["STOP"]
    finish(client, job, instance, outcome="CANCELLED", exit_code=137)
    assert get_job(client, job)["state"] == "CANCELLED"
    assert service.list_nodes()[0]["allocated"]["cpu"] == 0


def test_expired_worker_cannot_re_register_new_instance_while_execution_is_unknown(
    client, service, clock
):
    register(client, cpu=1)
    submit(client)
    clock[0] += 11
    service.tick()
    response = client.post(
        "/internal/workers/register",
        headers=worker_headers(),
        json={
            "worker_id": "vm-a",
            "instance_id": "f" * 32,
            "capacity": {"total_cpu": 1, "total_memory_mib": 2048, "cpu": 1, "memory_mib": 1024},
        },
    )
    assert response.status_code == 409
    assert service.list_nodes()[0]["allocated"]["cpu"] == 1
