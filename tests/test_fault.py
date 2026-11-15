"""M6.5-M6.7: retries after infrastructure loss, Scheduler restarts and late or repeated
events, at the Scheduler API level and with a Worker over a fake container runtime."""

import base64
from contextlib import contextmanager

import httpx
import pytest
from conftest import finish, heartbeat, register, submit, user_headers, worker_headers
from fastapi.testclient import TestClient
from test_reconnect import allocated, container, deliver, get_job, restart, wait
from test_worker import ControlledExecutor

from labflux.api import create_app
from labflux.config import Settings
from labflux.worker import Worker


def event(client, attempt_id, instance, event_id, name="vm-a", **fields):
    body = {"event_id": event_id, "instance_id": instance, "kind": "FINISHED", **fields}
    return client.post(
        f"/internal/workers/{name}/attempts/{attempt_id}/events",
        headers=worker_headers(),
        json=body,
    )


def test_lost_execution_gets_new_attempts_until_its_retries_run_out(client, service):
    old = register(client, cpu=1)
    job = submit(client, max_retries=2)
    first = deliver(client, service, old)["attempt_id"]
    middle, response = restart(client, {})
    assert response.json()["closed"] == [first]
    value = get_job(client, job)
    # Requeued and, with the node free again, dispatched in the same transaction.
    assert value["state"] == "STARTING" and len(value["attempts"]) == 2
    lost, retry = value["attempts"]
    assert lost["id"] == first and lost["state"] == "FAILED" and lost["released"]
    assert lost["reason"] == "container missing after worker restart"
    assert retry["state"] == "STARTING" and value["current_attempt_id"] == retry["id"]
    assert allocated(client)["cpu"] == 1
    second = deliver(client, service, middle)["attempt_id"]
    last, _ = restart(client, {})
    third = deliver(client, service, last)["attempt_id"]
    _, response = restart(client, {})
    assert response.json()["closed"] == [third]
    value = get_job(client, job)
    assert (
        value["state"] == "FAILED" and value["reason"] == "container missing after worker restart"
    )
    assert [a["id"] for a in value["attempts"]] == [first, second, third]
    assert allocated(client)["cpu"] == 0


def test_job_without_retries_fails_on_a_lost_execution(client, service):
    old = register(client, cpu=1)
    job = submit(client)
    deliver(client, service, old)
    restart(client, {})
    value = get_job(client, job)
    assert value["state"] == "FAILED" and len(value["attempts"]) == 1


def test_retried_job_keeps_its_place_ahead_of_later_submissions(client, service):
    old = register(client, cpu=1)
    job = submit(client, max_retries=1)
    later = submit(client)
    deliver(client, service, old)
    restart(client, {})
    assert get_job(client, job)["state"] == "STARTING"
    assert get_job(client, later)["state"] == "QUEUED"


@pytest.mark.parametrize(
    "outcome, exit_code, reason",
    [
        ("SUCCEEDED", 0, "completed"),
        ("FAILED", 3, "completed"),
        ("FAILED", 137, "time limit exceeded"),
        ("FAILED", 137, "memory limit exceeded (OOM)"),
    ],
)
def test_program_exits_and_time_limits_are_never_retried(
    client, service, outcome, exit_code, reason
):
    instance = register(client, cpu=1)
    job = submit(client, max_retries=3)
    attempt_id = deliver(client, service, instance)["attempt_id"]
    body = dict(outcome=outcome, exit_code=exit_code, reason=reason, termination_confirmed=True)
    assert event(client, attempt_id, instance, "done", **body).status_code == 200
    value = get_job(client, job)
    assert value["state"] == outcome and len(value["attempts"]) == 1
    assert allocated(client)["cpu"] == 0


def test_cancelled_job_whose_container_is_lost_is_not_retried(client, service):
    old = register(client, cpu=1)
    job = submit(client, max_retries=3)
    deliver(client, service, old)
    client.post(f"/api/jobs/{job['id']}/cancel", headers=user_headers())
    restart(client, {})
    value = get_job(client, job)
    assert value["state"] == "CANCELLED" and len(value["attempts"]) == 1
    assert allocated(client)["cpu"] == 0


def test_cancel_while_queued_for_retry_finishes_without_a_stop(client, service):
    old = register(client, cpu=2)
    job = submit(client, cpu=2, max_retries=1)
    deliver(client, service, old)
    new, _ = restart(client, {}, cpu=1)  # Came back smaller: the retry has to wait.
    value = get_job(client, job)
    assert value["state"] == "QUEUED" and value["current_attempt_id"] is not None
    cancelled = client.post(f"/api/jobs/{job['id']}/cancel", headers=user_headers()).json()
    assert cancelled["state"] == "CANCELLED" and len(cancelled["attempts"]) == 1
    assert service.poll_commands("vm-a", new) == []


def test_stale_attempt_events_after_a_retry_are_ignored(client, service):
    old = register(client, cpu=1)
    job = submit(client, max_retries=1)
    stale = deliver(client, service, old)["attempt_id"]
    new, _ = restart(client, {})
    current = get_job(client, job)["current_attempt_id"]
    assert current != stale
    done = dict(outcome="SUCCEEDED", exit_code=0, termination_confirmed=True)
    # The old instance's outbox drains late: rejected by the instance check.
    assert event(client, stale, old, "old-finish", **done).status_code == 409
    # A delayed copy under the current instance: recorded, but the attempt is released.
    assert event(client, stale, new, "late-finish", **done).json() == {"ignored": True}
    late_start = {"event_id": "late-start", "instance_id": new, "kind": "STARTED"}
    path = f"/internal/workers/vm-a/attempts/{stale}/events"
    assert client.post(path, headers=worker_headers(), json=late_start).json() == {"ignored": True}
    log = {"instance_id": new, "seq": 99, "stream": "stdout", "data_base64": "eA=="}
    client.post(f"/internal/workers/vm-a/attempts/{stale}/logs", headers=worker_headers(), json=log)
    value = get_job(client, job)
    assert value["state"] == "STARTING" and value["current_attempt_id"] == current
    assert allocated(client)["cpu"] == 1
    # The retry's own events, out of order and repeated.
    command = next(c for c in service.poll_commands("vm-a", new) if c["kind"] == "START")
    assert command["attempt_id"] == current
    assert event(client, current, new, "finish", **done).json() == {"accepted": True}
    assert event(client, current, new, "finish", **done).json() == {"duplicate": True}
    assert event(client, current, new, "finish", **{**done, "exit_code": 1}).status_code == 409
    start = {"event_id": "start", "instance_id": new, "kind": "STARTED"}
    path = f"/internal/workers/vm-a/attempts/{current}/events"
    assert client.post(path, headers=worker_headers(), json=start).json() == {"ignored": True}
    value = get_job(client, job)
    assert value["state"] == "SUCCEEDED" and len(value["attempts"]) == 2
    assert [a["state"] for a in value["attempts"]] == ["FAILED", "SUCCEEDED"]
    assert allocated(client)["cpu"] == 0


@contextmanager
def scheduler(settings, clock):
    """One Scheduler process lifetime over the shared data directory."""
    with TestClient(create_app(settings, run_loop=False, clock=lambda: clock[0])) as client:
        yield client, client.app.state.scheduler


def node(client, name="vm-a"):
    return next(
        n for n in client.get("/api/nodes", headers=user_headers()).json() if n["id"] == name
    )


def running_job(client, service):
    instance = register(client, cpu=2)
    job = submit(client)
    deliver(client, service, instance)
    assert get_job(client, job)["state"] == "RUNNING"
    return instance, job


def test_scheduler_restart_keeps_work_and_dispatches_only_after_the_worker_returns(settings, clock):
    with scheduler(settings, clock) as (client, service):
        instance, job = running_job(client, service)
    clock[0] += 3600  # A long outage must not expire the node on the first tick.
    with scheduler(settings, clock) as (client, service):
        service.tick()
        assert node(client)["status"] == "RECONCILING"
        assert node(client)["allocated"]["cpu"] == 1
        value = get_job(client, job)
        assert value["state"] == "RUNNING" and len(value["attempts"]) == 1
        waiting = submit(client)
        assert waiting["state"] == "QUEUED"
        assert service.poll_commands("vm-a", instance) == []
        clock[0] += settings.offline_timeout_s - 1
        service.tick()
        heartbeat(client, "vm-a", instance)
        assert node(client)["status"] == "ONLINE"
        assert get_job(client, waiting)["state"] == "STARTING"
        value = get_job(client, job)
        assert value["state"] == "RUNNING" and value["attempts"][0]["reason"] == "dispatched"
        assert finish(client, value, instance).status_code == 200
        assert get_job(client, job)["state"] == "SUCCEEDED"


def test_worker_absent_after_scheduler_restart_expires_after_the_normal_window(settings, clock):
    with scheduler(settings, clock) as (client, service):
        _, job = running_job(client, service)
    clock[0] += 600
    with scheduler(settings, clock) as (client, service):
        clock[0] += settings.offline_timeout_s - 1
        service.tick()
        assert node(client)["status"] == "RECONCILING"
        clock[0] += 2
        service.tick()
        assert node(client)["status"] == "OFFLINE"
        assert get_job(client, job)["state"] == "UNKNOWN"
        assert node(client)["allocated"]["cpu"] == 1


def test_worker_restarted_during_scheduler_outage_reconciles_by_registering(settings, clock):
    with scheduler(settings, clock) as (client, service):
        old, job = running_job(client, service)
        attempt_id = get_job(client, job)["current_attempt_id"]
    with scheduler(settings, clock) as (client, service):
        assert node(client)["status"] == "RECONCILING"
        assert submit(client)["state"] == "QUEUED"
        _, response = restart(client, {"containers": [container(attempt_id)]}, cpu=2)
        assert [a["attempt_id"] for a in response.json()["adopt"]] == [attempt_id]
        assert node(client)["status"] == "ONLINE"
        stale = client.post(
            "/internal/workers/vm-a/heartbeat", headers=worker_headers(), json={"instance_id": old}
        )
        assert stale.status_code == 409
        assert get_job(client, job)["state"] == "RUNNING"


def test_outbox_results_during_reconciliation_release_without_dispatching(settings, clock):
    with scheduler(settings, clock) as (client, service):
        instance = register(client, cpu=1)
        job = submit(client)
        deliver(client, service, instance)
        waiting = submit(client)
    with scheduler(settings, clock) as (client, service):
        assert finish(client, get_job(client, job), instance).status_code == 200
        assert get_job(client, job)["state"] == "SUCCEEDED"
        assert node(client)["allocated"]["cpu"] == 0
        assert get_job(client, waiting)["state"] == "QUEUED"
        heartbeat(client, "vm-a", instance)
        assert get_job(client, waiting)["state"] == "STARTING"


class Partition:
    """The Worker's link to the Scheduler; while cut, every request fails to connect."""

    def __init__(self, client):
        self.client = client
        self.cut = False

    def __getattr__(self, name):
        if self.cut and name in {"get", "post", "put", "stream"}:

            def unreachable(*args, **kwargs):
                raise httpx.ConnectError("network is unreachable")

            return unreachable
        return getattr(self.client, name)


def worker_for(client, tmp_path, executor, instance=None):
    worker = Worker(Settings(data_dir=tmp_path / "worker", worker_id="vm-a"), executor=executor)
    worker.client.close()
    client.headers["Authorization"] = "Bearer dev-worker-token"
    worker.client = Partition(client)
    if instance:
        worker.instance_id = instance
    return worker


def beat(worker):
    """One iteration of the Worker's heartbeat thread, which tolerates failures."""
    try:
        worker.client.post(worker._url("heartbeat"), json=worker.heartbeat_payload())
    except httpx.HTTPError:
        pass


def logs(client, job):
    chunks = client.get(f"/api/jobs/{job['id']}/logs", headers=user_headers()).json()["chunks"]
    return b"".join(base64.b64decode(c["data_base64"]) for c in chunks)


def test_network_partition_keeps_the_outbox_and_settles_once_reconnected(
    client, service, clock, tmp_path
):
    executor = ControlledExecutor()
    instance = register(client, cpu=1)
    worker = worker_for(client, tmp_path, executor, instance)
    job = submit(client)
    waiting = submit(client)
    try:
        worker.process(service.poll_commands("vm-a", instance))
        attempt_id = get_job(client, job)["current_attempt_id"]
        wait(lambda: worker._read(attempt_id)["started"])
        worker.flush()
        assert get_job(client, job)["state"] == "RUNNING"
        worker.client.cut = True
        clock[0] += service.settings.offline_timeout_s + 1
        beat(worker)
        service.tick()
        assert node(client)["status"] == "OFFLINE"
        assert get_job(client, job)["state"] == "UNKNOWN"
        executor.release.set()  # The job ends while the node is unreachable.
        worker.handles[attempt_id]["future"].result(timeout=5)
        with pytest.raises(httpx.ConnectError):
            worker.flush()
        state = worker._read(attempt_id)
        assert state["result"]["outcome"] == "SUCCEEDED" and not state["result_sent"]
        assert node(client)["allocated"]["cpu"] == 1
        assert get_job(client, waiting)["state"] == "QUEUED"
        worker.client.cut = False
        beat(worker)
        assert node(client)["status"] == "ONLINE"
        assert get_job(client, job)["state"] == "UNKNOWN"  # Not active, so not resumed.
        worker.flush()
        value = get_job(client, job)
        assert value["state"] == "SUCCEEDED" and len(value["attempts"]) == 1
        assert logs(client, job) == b"live output\n"
        assert get_job(client, waiting)["state"] == "STARTING"
        assert executor.calls == 1
    finally:
        executor.release.set()
        worker.pool.shutdown(wait=True)


def test_node_lost_with_its_containers_retries_on_the_returning_worker(
    client, service, clock, tmp_path, monkeypatch
):
    """A VM shutdown: the Worker and its containers die together, later the Worker returns.

    Docker keeps the stopped container across the reboot; it started before the current boot.
    """
    monkeypatch.setattr("labflux.worker.psutil.boot_time", lambda: 5000.0)
    executor = ControlledExecutor()
    instance = register(client, cpu=1)
    old = worker_for(client, tmp_path, executor, instance)
    job = submit(client, max_retries=1)
    try:
        old.process(service.poll_commands("vm-a", instance))
        first = get_job(client, job)["current_attempt_id"]
        wait(lambda: old._read(first)["started"])
        old.flush()
        old.stop_event.set()
        old.handles[first]["future"].result(timeout=5)
        # The container went down with the machine; Docker keeps it as exited.
        executor.containers[first].update(status="exited", exit_code=143, started_at=1000.0)
        clock[0] += service.settings.offline_timeout_s + 1
        service.tick()
        assert get_job(client, job)["state"] == "UNKNOWN"
        new = worker_for(client, tmp_path, executor)
        assert new.register()["closed"] == [first]
        value = get_job(client, job)
        assert value["state"] == "STARTING" and len(value["attempts"]) == 2
        retry = value["current_attempt_id"]
        new.process(service.poll_commands("vm-a", new.instance_id))
        wait(lambda: new._read(retry)["started"])
        executor.release.set()
        new.handles[retry]["future"].result(timeout=5)
        new.flush()
        # A late report from the old instance can no longer change anything.
        late = dict(outcome="FAILED", exit_code=1, termination_confirmed=True)
        assert event(client, first, instance, "late", **late).status_code == 409
        value = get_job(client, job)
        assert value["state"] == "SUCCEEDED"
        assert [a["state"] for a in value["attempts"]] == ["FAILED", "SUCCEEDED"]
        assert node(client)["allocated"]["cpu"] == 0
        assert new.inventory()["attempts"] == []
        assert first in executor.removed
    finally:
        executor.release.set()
        old.pool.shutdown(wait=True)


def test_only_containers_stopped_by_a_host_restart_are_dropped_from_the_inventory(
    client, service, tmp_path, monkeypatch
):
    """A stop after the current boot, a clean exit or an OOM kill is a real outcome to adopt."""
    monkeypatch.setattr("labflux.worker.psutil.boot_time", lambda: 5000.0)
    executor = ControlledExecutor()
    instance = register(client, cpu=4)
    old = worker_for(client, tmp_path, executor, instance)
    jobs = [submit(client, max_retries=1) for _ in range(4)]
    try:
        old.process(service.poll_commands("vm-a", instance))
        ids = [get_job(client, job)["current_attempt_id"] for job in jobs]
        for attempt_id in ids:
            wait(lambda attempt_id=attempt_id: old._read(attempt_id)["started"])
        old.stop_event.set()
        for attempt_id in ids:
            old.handles[attempt_id]["future"].result(timeout=5)
        interrupted, recent, clean, oom = ids
        executor.containers[interrupted].update(status="exited", exit_code=255, started_at=10.0)
        executor.containers[recent].update(status="exited", exit_code=143, started_at=6000.0)
        executor.containers[clean].update(status="exited", exit_code=0, started_at=10.0)
        executor.containers[oom].update(
            status="exited", exit_code=137, oom_killed=True, started_at=10.0
        )
        executor.containers["f" * 32] = {"worker_id": "vm-a", "status": "exited", "started_at": 1.0}
        new = worker_for(client, tmp_path, executor)
        found = {item["attempt_id"] for item in new.inventory()["containers"]}
        # Without a local record a container is never touched here.
        assert found == {recent, clean, oom, "f" * 32}
        assert executor.removed == [interrupted]
    finally:
        executor.release.set()
        old.pool.shutdown(wait=True)
