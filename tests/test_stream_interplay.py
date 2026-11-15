"""M6 retries, M8 policies and the M9 doorbell together: a job requeued after a lost execution
is ordered by the active policy, and every dispatch rings the node's doorbell."""

from contextlib import ExitStack

import pytest
from conftest import register, submit
from fastapi.testclient import TestClient
from test_reconnect import deliver, get_job, restart

from labflux.api import create_app
from labflux.config import Settings


@pytest.fixture
def make_client(tmp_path, clock):
    """Build a Scheduler API client running the named policy, with a doorbell spy."""
    with ExitStack() as stack:

        def build(policy):
            values = Settings(
                data_dir=tmp_path / policy,
                users={"alice": "alice-token", "bob": "bob-token"},
                scheduling_policy=policy,
            )
            app = create_app(values, run_loop=False, clock=lambda: clock[0])
            client = stack.enter_context(TestClient(app))
            service = client.app.state.scheduler
            client.rings, ring = [], service.doorbell
            service.doorbell = lambda node: (client.rings.append(node), ring(node))
            return client, service

        yield build


@pytest.mark.parametrize("policy", ["fcfs", "priority", "backfill"])
def test_retry_is_dispatched_by_every_policy_and_rings_the_doorbell(make_client, policy):
    client, service = make_client(policy)
    old = register(client, cpu=1)
    job = submit(client, retries_on_node_failure=1, safe_to_rerun=True, time_limit_s=600)
    first = deliver(client, service, old)["attempt_id"]
    client.rings.clear()
    new, response = restart(client, {})
    assert response.json()["closed"] == [first]
    value = get_job(client, job)
    assert value["state"] == "STARTING" and len(value["attempts"]) == 2
    assert value["schedule_note"].startswith(f"{policy}: started on vm-a")
    assert client.rings == ["vm-a"]
    [command] = service.poll_commands("vm-a", new)
    assert command["kind"] == "START" and command["attempt_id"] == value["current_attempt_id"]


@pytest.mark.parametrize("policy, winner", [("fcfs", "retried"), ("priority", "urgent")])
def test_requeued_job_competes_under_the_active_policy(make_client, policy, winner):
    client, service = make_client(policy)
    old = register(client, cpu=1)
    jobs = {"retried": submit(client, retries_on_node_failure=1, safe_to_rerun=True, priority=1)}
    jobs["urgent"] = submit(client, priority=9)
    deliver(client, service, old)
    restart(client, {})
    states = {name: get_job(client, value)["state"] for name, value in jobs.items()}
    assert states == {
        name: "STARTING" if name == winner else "QUEUED" for name in ("retried", "urgent")
    }


def test_backfilled_dispatch_rings_the_doorbell(make_client):
    client, service = make_client("backfill")
    register(client, "kayle", cpu=4)
    submit(client, cpu=3, time_limit_s=3600)
    head = submit(client, cpu=4, time_limit_s=3600)
    client.rings.clear()
    short = submit(client, cpu=1, time_limit_s=600)
    assert get_job(client, head)["state"] == "QUEUED"
    assert "backfilled on kayle" in get_job(client, short)["schedule_note"]
    assert client.rings == ["kayle"]
