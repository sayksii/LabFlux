"""11.9: the Worker reports when each running job last saved a checkpoint, so that preemption
counts lost work from it. Victim choice with checkpoints is in tests/test_preemption.py."""

import os
from pathlib import Path

from conftest import register, submit, user_headers, worker_headers
from sqlalchemy import update
from test_doorbell import wait_for
from test_worker import ControlledExecutor

from labflux.config import Settings
from labflux.database import SCHEMA_VERSION, Database, schema_version
from labflux.models import JobSpec
from labflux.policy import lost_seconds
from labflux.scheduler import Scheduler
from labflux.worker import Worker
from labflux.workspace import copy_generated, newest_mtime


def write(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"weights")
    os.utime(path, (mtime, mtime))


# The scan ------------------------------------------------------------------------------------


def test_a_checkpoint_file_reports_its_modification_time(tmp_path):
    write(tmp_path / "model.pt", 2000)
    assert newest_mtime(tmp_path, "model.pt") == 2000
    assert newest_mtime(tmp_path, "./model.pt") == 2000


def test_a_checkpoint_directory_reports_its_newest_file_at_any_depth(tmp_path):
    write(tmp_path / "ckpt" / "step-1" / "model.pt", 2000)
    write(tmp_path / "ckpt" / "step-2" / "model.pt", 3000)
    write(tmp_path / "ckpt" / "latest.json", 2500)
    write(tmp_path / "train.log", 9000)  # Outside the checkpoint path.
    assert newest_mtime(tmp_path, "ckpt/") == 3000
    assert newest_mtime(tmp_path, "ckpt/step-1") == 2000


def test_a_missing_or_empty_checkpoint_path_is_unknown(tmp_path):
    assert newest_mtime(tmp_path, "ckpt") is None
    assert newest_mtime(tmp_path, "ckpt/model.pt") is None
    (tmp_path / "ckpt" / "empty").mkdir(parents=True)
    assert newest_mtime(tmp_path, "ckpt") is None


def test_links_are_neither_followed_nor_counted(tmp_path):
    outside = tmp_path / "outside"
    write(outside / "new.pt", 9000)
    workspace = tmp_path / "workspace"
    write(workspace / "ckpt" / "model.pt", 2000)
    (workspace / "ckpt" / "file-link").symlink_to(outside / "new.pt")
    (workspace / "ckpt" / "dir-link").symlink_to(outside)
    (workspace / "elsewhere").symlink_to(outside)
    assert newest_mtime(workspace, "ckpt") == 2000
    assert newest_mtime(workspace, "elsewhere") is None  # The path itself is a link.
    assert newest_mtime(workspace, "elsewhere/new.pt") is None  # Reached through a link.


def test_the_scan_looks_at_a_bounded_number_of_entries(tmp_path):
    for index in range(5):
        write(tmp_path / "ckpt" / f"{index}.pt", 1000 + index)
    assert newest_mtime(tmp_path, "ckpt", limit=5) == 1004
    assert newest_mtime(tmp_path, "ckpt", limit=0) is None  # Stopped before the first file.


def test_a_checkpoint_copied_from_an_earlier_attempt_keeps_its_old_time(tmp_path):
    """6.1 P3 copies the stopped attempt's files with their times kept; until this attempt
    saves, its checkpoint is older than its start and saves none of its work."""
    previous, workspace = tmp_path / "previous", tmp_path / "workspace"
    write(previous / "ckpt" / "model.pt", 2000)
    workspace.mkdir()
    copy_generated(previous, workspace, [])
    assert newest_mtime(workspace, "ckpt") == 2000
    # Started at 5000 by the Scheduler's clock, considered for preemption at 5600.
    assert lost_seconds(5000, 5600, newest_mtime(workspace, "ckpt")) == 600
    write(workspace / "ckpt" / "model.pt", 5500)  # This attempt saves.
    assert lost_seconds(5000, 5600, newest_mtime(workspace, "ckpt")) == 100


# The heartbeat -------------------------------------------------------------------------------


def test_the_worker_reports_checkpoints_with_its_heartbeat(client, tmp_path):
    instance = register(client, cpu=2)
    executor = ControlledExecutor()
    worker = Worker(Settings(data_dir=tmp_path / "worker", worker_id="vm-a"), executor=executor)
    worker.instance_id = instance
    worker.client.close()
    worker.client = client
    worker.client.headers["Authorization"] = "Bearer dev-worker-token"
    saving = submit(client, checkpoint="ckpt/")
    plain = submit(client)
    try:
        worker.process(client.app.state.scheduler.poll_commands("vm-a", instance))
        attempt_id, other_id = saving["current_attempt_id"], plain["current_attempt_id"]
        wait_for(lambda: len(executor.containers) == 2)
        write(worker.root / other_id / "workspace" / "ckpt" / "model.pt", 4000)
        assert worker.heartbeat_payload()["checkpoints"] == {}  # Nothing saved where named.
        write(worker.root / attempt_id / "workspace" / "ckpt" / "model.pt", 4000)
        payload = worker.heartbeat_payload()
        assert payload["checkpoints"] == {attempt_id: 4000}
        response = client.post(
            "/internal/workers/vm-a/heartbeat", headers=worker_headers(), json=payload
        )
        assert response.status_code == 200, response.text
        for job, expected in ((saving, 4000), (plain, None)):
            value = client.get(f"/api/jobs/{job['id']}", headers=user_headers()).json()
            assert value["attempts"][0]["checkpoint_at"] == expected
            # The Dashboard can show checkpoint age for either viewer without a private spec.
            for viewer in ("alice", "bob"):
                nodes = client.get("/api/nodes", headers=user_headers(viewer)).json()
                allocation = next(
                    a for n in nodes for a in n["allocations"] if a["job_id"] == job["id"]
                )
                assert allocation["checkpoint_at"] == expected
                assert "spec" not in allocation and "checkpoint" not in allocation
    finally:
        executor.release.set()
        worker.pool.shutdown(wait=True)
    assert worker.heartbeat_payload()["checkpoints"] == {}  # Ended: no longer scanned.


def test_a_heartbeat_does_not_set_the_checkpoint_of_another_nodes_attempt(client):
    register(client, "vm-a")
    other = register(client, "vm-b", cpu=1)
    job = submit(client, cpu=2, checkpoint="ckpt/")  # Only vm-a fits it.
    response = client.post(
        "/internal/workers/vm-b/heartbeat",
        headers=worker_headers(),
        json={"instance_id": other, "checkpoints": {job["current_attempt_id"]: 4000}},
    )
    assert response.status_code == 200, response.text
    value = client.get(f"/api/jobs/{job['id']}", headers=user_headers()).json()
    assert value["attempts"][0]["node_id"] == "vm-a"
    assert value["attempts"][0]["checkpoint_at"] is None


# Migration -----------------------------------------------------------------------------------


def test_a_version_8_database_gains_checkpoint_at(settings):
    first = Scheduler(settings)
    spec = JobSpec(image="python:3.12-slim", argv=["true"], checkpoint="ckpt/")
    job = first.submit("alice", spec)
    with first.db.transaction() as connection:
        connection.exec_driver_sql("ALTER TABLE attempts DROP COLUMN checkpoint_at")
        connection.execute(update(schema_version).values(version=8))
    first.db.close()
    database = Database(settings.data_dir)
    assert database.version == SCHEMA_VERSION == 9
    with database.transaction() as connection:
        connection.exec_driver_sql("SELECT checkpoint_at FROM attempts")
    database.close()
    value = Scheduler(settings).get_job(job["id"], "alice")
    assert value["state"] == "QUEUED" and value["spec"]["checkpoint"] == "ckpt/"
