"""FCFS scheduling and durable job/worker lifecycle transitions."""

from __future__ import annotations

import base64
import time
import uuid
from collections.abc import Callable

from sqlalchemy import func, select

from labflux.config import Settings
from labflux.database import Database, artifacts, attempts, commands, events, jobs, logs, nodes
from labflux.models import Heartbeat, JobSpec, LogUpload, WorkerEvent, WorkerRegistration

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


class ServiceError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


def row_dict(row) -> dict:
    return dict(row._mapping)


class Scheduler:
    def __init__(self, settings: Settings, clock: Callable[[], float] = time.time):
        self.settings = settings
        self.clock = clock
        self.db = Database(settings.data_dir)

    def _node(self, connection, node_id: str, instance_id: str) -> dict:
        row = connection.execute(select(nodes).where(nodes.c.id == node_id)).first()
        if row is None:
            raise ServiceError(404, "worker is not registered")
        value = row_dict(row)
        if value["instance_id"] != instance_id:
            raise ServiceError(409, "worker instance changed; reconciliation is required")
        return value

    def _job(self, connection, job_id: str, owner: str | None = None) -> dict:
        row = connection.execute(select(jobs).where(jobs.c.id == job_id)).first()
        if row is None or (owner is not None and row._mapping["owner"] != owner):
            raise ServiceError(404, "job not found")
        return row_dict(row)

    def _attempt(self, connection, attempt_id: str, node_id: str, instance_id: str) -> dict:
        self._node(connection, node_id, instance_id)
        row = connection.execute(select(attempts).where(attempts.c.id == attempt_id)).first()
        if row is None or row._mapping["node_id"] != node_id:
            raise ServiceError(404, "attempt not found")
        return row_dict(row)

    @staticmethod
    def _allocations(connection, node_id: str) -> dict:
        assigned = (
            connection.execute(
                select(attempts.c.resources).where(
                    attempts.c.node_id == node_id, attempts.c.released.is_(False)
                )
            )
            .scalars()
            .all()
        )
        return {
            "cpu": sum(item["cpu"] for item in assigned),
            "memory_mib": sum(item["memory_mib"] for item in assigned),
            "jobs": len(assigned),
        }

    def register(self, payload: WorkerRegistration) -> dict:
        capacity = payload.capacity.model_dump()
        if (
            capacity["cpu"] > capacity["total_cpu"]
            or capacity["memory_mib"] > capacity["total_memory_mib"]
        ):
            raise ServiceError(422, "allocatable capacity cannot exceed physical capacity")
        with self.db.transaction() as connection:
            existing = connection.execute(
                select(nodes).where(nodes.c.id == payload.worker_id)
            ).first()
            allocation = self._allocations(connection, payload.worker_id)
            if (
                existing
                and existing._mapping["instance_id"] != payload.instance_id
                and allocation["jobs"]
            ):
                raise ServiceError(
                    409, "worker has unreleased attempts; reconcile them before restarting"
                )
            status = "ONLINE"
            if existing and existing._mapping["status"] == "OFFLINE" and allocation["jobs"]:
                status = "OFFLINE"
            values = dict(
                instance_id=payload.instance_id,
                capacity=capacity,
                observed={},
                status=status,
                last_seen=self.clock(),
            )
            if existing:
                connection.execute(
                    nodes.update().where(nodes.c.id == payload.worker_id).values(**values)
                )
            else:
                connection.execute(nodes.insert().values(id=payload.worker_id, **values))
            self._schedule(connection)
        return {"worker_id": payload.worker_id, "status": status}

    def heartbeat(self, node_id: str, payload: Heartbeat) -> dict:
        with self.db.transaction() as connection:
            node = self._node(connection, node_id, payload.instance_id)
            status = node["status"]
            if status == "OFFLINE" and not self._allocations(connection, node_id)["jobs"]:
                status = "ONLINE"
            connection.execute(
                nodes.update()
                .where(nodes.c.id == node_id)
                .values(last_seen=self.clock(), observed=payload.observed, status=status)
            )
            self._schedule(connection)
        return {"status": status}

    def submit(self, owner: str, spec: JobSpec) -> dict:
        if spec.time_limit_s is None:
            spec = spec.model_copy(update={"time_limit_s": self.settings.job_time_limit_s})
        job_id = uuid.uuid4().hex
        now = self.clock()
        with self.db.transaction() as connection:
            if spec.source_id:
                source = connection.execute(
                    select(artifacts).where(
                        artifacts.c.id == spec.source_id,
                        artifacts.c.owner == owner,
                        artifacts.c.kind == "SOURCE",
                    )
                ).first()
                if not source:
                    raise ServiceError(404, "source artifact not found")
            sequence = (
                connection.execute(
                    select(func.coalesce(func.max(jobs.c.submission_seq), 0))
                ).scalar_one()
                + 1
            )
            connection.execute(
                jobs.insert().values(
                    id=job_id,
                    submission_seq=sequence,
                    owner=owner,
                    spec=spec.model_dump(),
                    state="QUEUED",
                    reason="waiting for scheduling",
                    current_attempt_id=None,
                    cancel_requested=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            self._schedule(connection)
        return self.get_job(job_id, owner)

    def _schedule(self, connection) -> None:
        now = self.clock()
        available = [
            row_dict(row)
            for row in connection.execute(
                select(nodes).where(
                    nodes.c.status == "ONLINE",
                    nodes.c.last_seen >= now - self.settings.offline_timeout_s,
                )
            )
        ]
        allocated = {node["id"]: self._allocations(connection, node["id"]) for node in available}
        queued = connection.execute(
            select(jobs).where(jobs.c.state == "QUEUED").order_by(jobs.c.submission_seq)
        ).all()
        for row in queued:
            job = row_dict(row)
            spec = JobSpec.model_validate(job["spec"])
            request = spec.resources.model_dump()
            candidates = []
            for node in available:
                cap, used = node["capacity"], allocated[node["id"]]
                if not set(spec.datasets).issubset(cap["datasets"]):
                    continue
                if (
                    request["cpu"] <= cap["cpu"] - used["cpu"] + 1e-9
                    and request["memory_mib"] <= cap["memory_mib"] - used["memory_mib"]
                    and used["jobs"] < self.settings.max_parallel_jobs
                ):
                    candidates.append(node)
            if not candidates:
                reason = (
                    "no online worker"
                    if not available
                    else "waiting for matching CPU/RAM capacity or datasets"
                )
                connection.execute(
                    jobs.update().where(jobs.c.id == job["id"]).values(reason=reason)
                )
                break  # Strict FCFS: never bypass an unschedulable head job.
            node = min(
                candidates,
                key=lambda n: (
                    n["capacity"]["cpu"] - allocated[n["id"]]["cpu"] - request["cpu"],
                    n["capacity"]["cpu"],
                    n["capacity"]["memory_mib"]
                    - allocated[n["id"]]["memory_mib"]
                    - request["memory_mib"],
                    n["id"],
                ),
            )
            attempt_id = uuid.uuid4().hex
            connection.execute(
                attempts.insert().values(
                    id=attempt_id,
                    job_id=job["id"],
                    node_id=node["id"],
                    state="STARTING",
                    resources=request,
                    released=False,
                    created_at=now,
                    reason="dispatched",
                    oom_killed=False,
                )
            )
            payload = {
                "job_id": job["id"],
                "attempt_id": attempt_id,
                "worker_id": node["id"],
                "job": spec.model_dump(),
            }
            connection.execute(
                commands.insert().values(
                    id=f"start-{attempt_id}",
                    node_id=node["id"],
                    attempt_id=attempt_id,
                    kind="START",
                    payload=payload,
                    acked=False,
                    created_at=now,
                )
            )
            connection.execute(
                jobs.update()
                .where(jobs.c.id == job["id"])
                .values(
                    state="STARTING",
                    current_attempt_id=attempt_id,
                    reason=f"assigned to {node['id']}",
                    updated_at=now,
                )
            )
            allocated[node["id"]]["cpu"] += request["cpu"]
            allocated[node["id"]]["memory_mib"] += request["memory_mib"]
            allocated[node["id"]]["jobs"] += 1

    def tick(self) -> None:
        with self.db.transaction() as connection:
            expired = (
                connection.execute(
                    select(nodes.c.id).where(
                        nodes.c.status == "ONLINE",
                        nodes.c.last_seen < self.clock() - self.settings.offline_timeout_s,
                    )
                )
                .scalars()
                .all()
            )
            for node_id in expired:
                connection.execute(
                    nodes.update().where(nodes.c.id == node_id).values(status="OFFLINE")
                )
                assigned = connection.execute(
                    select(attempts).where(
                        attempts.c.node_id == node_id, attempts.c.released.is_(False)
                    )
                ).all()
                for attempt in assigned:
                    connection.execute(
                        attempts.update()
                        .where(attempts.c.id == attempt.id)
                        .values(
                            state="UNKNOWN",
                            reason="worker heartbeat expired; termination unconfirmed",
                        )
                    )
                    connection.execute(
                        jobs.update()
                        .where(jobs.c.id == attempt.job_id)
                        .values(
                            state="UNKNOWN",
                            reason="worker heartbeat expired; resources remain reserved",
                            updated_at=self.clock(),
                        )
                    )
            self._schedule(connection)

    def get_job(self, job_id: str, owner: str) -> dict:
        with self.db.transaction() as connection:
            job = self._job(connection, job_id, owner)
            job["attempts"] = [
                row_dict(row)
                for row in connection.execute(
                    select(attempts)
                    .where(attempts.c.job_id == job_id)
                    .order_by(attempts.c.created_at)
                )
            ]
            job["outputs"] = [
                self._artifact_view(row_dict(row))
                for row in connection.execute(
                    select(artifacts).where(
                        artifacts.c.attempt_id.in_([a["id"] for a in job["attempts"]]),
                        artifacts.c.kind == "OUTPUT",
                    )
                )
            ]
            return job

    def list_jobs(self, owner: str) -> list[dict]:
        with self.db.transaction() as connection:
            return [
                row_dict(row)
                for row in connection.execute(
                    select(jobs).where(jobs.c.owner == owner).order_by(jobs.c.submission_seq)
                )
            ]

    def list_nodes(self) -> list[dict]:
        with self.db.transaction() as connection:
            result = []
            for row in connection.execute(select(nodes).order_by(nodes.c.id)):
                value = row_dict(row)
                value.pop("instance_id")
                value["allocated"] = self._allocations(connection, value["id"])
                result.append(value)
            return result

    def poll_commands(self, node_id: str, instance_id: str) -> list[dict]:
        with self.db.transaction() as connection:
            node = self._node(connection, node_id, instance_id)
            statement = select(commands).where(
                commands.c.node_id == node_id, commands.c.acked.is_(False)
            )
            if node["status"] != "ONLINE":
                statement = statement.where(commands.c.kind == "STOP")
            return [
                row_dict(row)
                for row in connection.execute(
                    statement.order_by(commands.c.kind.desc(), commands.c.created_at)
                )
            ]

    def ack(self, node_id: str, instance_id: str, command_id: str) -> dict:
        with self.db.transaction() as connection:
            self._node(connection, node_id, instance_id)
            command = connection.execute(
                select(commands).where(commands.c.id == command_id, commands.c.node_id == node_id)
            ).first()
            if command is None:
                raise ServiceError(404, "command not found")
            connection.execute(
                commands.update().where(commands.c.id == command_id).values(acked=True)
            )
        return {"acked": True}

    def handle_event(self, node_id: str, attempt_id: str, event: WorkerEvent) -> dict:
        with self.db.transaction() as connection:
            attempt = self._attempt(connection, attempt_id, node_id, event.instance_id)
            existing = connection.execute(
                select(events).where(events.c.id == event.event_id)
            ).first()
            payload = event.model_dump()
            if existing:
                if existing.attempt_id != attempt_id or existing.payload != payload:
                    raise ServiceError(409, "event ID was reused with different contents")
                return {"duplicate": True}
            connection.execute(
                events.insert().values(
                    id=event.event_id,
                    attempt_id=attempt_id,
                    payload=payload,
                    created_at=self.clock(),
                )
            )
            job = self._job(connection, attempt["job_id"])
            if attempt["released"] or job["current_attempt_id"] != attempt_id:
                return {"ignored": True}
            if event.kind == "STARTED":
                if job["state"] == "STARTING":
                    connection.execute(
                        jobs.update()
                        .where(jobs.c.id == job["id"])
                        .values(state="RUNNING", reason="executing", updated_at=self.clock())
                    )
                    connection.execute(
                        attempts.update()
                        .where(attempts.c.id == attempt_id)
                        .values(state="RUNNING", started_at=self.clock())
                    )
            elif not event.termination_confirmed or event.outcome == "UNKNOWN":
                connection.execute(
                    attempts.update()
                    .where(attempts.c.id == attempt_id)
                    .values(state="UNKNOWN", reason=event.reason)
                )
                connection.execute(
                    jobs.update()
                    .where(jobs.c.id == job["id"])
                    .values(state="UNKNOWN", reason=event.reason, updated_at=self.clock())
                )
            else:
                if event.outcome not in TERMINAL:
                    raise ServiceError(422, "a terminal outcome is required")
                outcome = "CANCELLED" if job["cancel_requested"] else event.outcome
                connection.execute(
                    attempts.update()
                    .where(attempts.c.id == attempt_id)
                    .values(
                        state=outcome,
                        released=True,
                        finished_at=self.clock(),
                        exit_code=event.exit_code,
                        reason=event.reason,
                        oom_killed=event.oom_killed,
                    )
                )
                connection.execute(
                    jobs.update()
                    .where(jobs.c.id == job["id"])
                    .values(state=outcome, reason=event.reason, updated_at=self.clock())
                )
                connection.execute(
                    commands.update().where(commands.c.attempt_id == attempt_id).values(acked=True)
                )
                self._schedule(connection)
        return {"accepted": True}

    def cancel(self, job_id: str, owner: str) -> dict:
        with self.db.transaction() as connection:
            job = self._job(connection, job_id, owner)
            if job["state"] not in TERMINAL:
                if job["current_attempt_id"] is None:
                    connection.execute(
                        jobs.update()
                        .where(jobs.c.id == job_id)
                        .values(
                            state="CANCELLED",
                            cancel_requested=True,
                            reason="cancelled while queued",
                            updated_at=self.clock(),
                        )
                    )
                    self._schedule(connection)
                else:
                    attempt = connection.execute(
                        select(attempts).where(attempts.c.id == job["current_attempt_id"])
                    ).one()
                    connection.execute(
                        jobs.update()
                        .where(jobs.c.id == job_id)
                        .values(
                            state="STOPPING",
                            cancel_requested=True,
                            reason="waiting for confirmed container termination",
                            updated_at=self.clock(),
                        )
                    )
                    connection.execute(
                        attempts.update()
                        .where(attempts.c.id == attempt.id)
                        .values(state="STOPPING")
                    )
                    connection.execute(
                        commands.update()
                        .where(commands.c.attempt_id == attempt.id, commands.c.kind == "START")
                        .values(acked=True)
                    )
                    stop_id = f"stop-{attempt.id}"
                    if not connection.execute(
                        select(commands.c.id).where(commands.c.id == stop_id)
                    ).first():
                        connection.execute(
                            commands.insert().values(
                                id=stop_id,
                                node_id=attempt.node_id,
                                attempt_id=attempt.id,
                                kind="STOP",
                                payload={
                                    "job_id": job_id,
                                    "attempt_id": attempt.id,
                                    "worker_id": attempt.node_id,
                                    "job": job["spec"],
                                },
                                acked=False,
                                created_at=self.clock(),
                            )
                        )
        return self.get_job(job_id, owner)

    def upload_log(self, node_id: str, attempt_id: str, payload: LogUpload) -> dict:
        try:
            data = base64.b64decode(payload.data_base64, validate=True)
        except ValueError as exc:
            raise ServiceError(422, "invalid base64 log payload") from exc
        if len(data) > 65536:
            raise ServiceError(413, "log chunk exceeds 64 KiB")
        with self.db.transaction() as connection:
            self._attempt(connection, attempt_id, node_id, payload.instance_id)
            existing = connection.execute(
                select(logs).where(logs.c.attempt_id == attempt_id, logs.c.seq == payload.seq)
            ).first()
            if existing:
                if existing.data != data or existing.stream != payload.stream:
                    raise ServiceError(409, "log sequence reused with different contents")
                return {"duplicate": True}
            used = connection.execute(
                select(func.coalesce(func.sum(func.length(logs.c.data)), 0)).where(
                    logs.c.attempt_id == attempt_id
                )
            ).scalar_one()
            if used + len(data) > self.settings.max_log_bytes:
                raise ServiceError(413, "job log limit reached")
            connection.execute(
                logs.insert().values(
                    attempt_id=attempt_id, seq=payload.seq, stream=payload.stream, data=data
                )
            )
        return {"accepted": True}

    def get_logs(
        self, job_id: str, owner: str, after: int = -1, attempt_id: str | None = None
    ) -> dict:
        with self.db.transaction() as connection:
            job = self._job(connection, job_id, owner)
            selected = attempt_id or job["current_attempt_id"]
            if selected:
                attempt = connection.execute(
                    select(attempts.c.id).where(
                        attempts.c.id == selected, attempts.c.job_id == job_id
                    )
                ).first()
                if not attempt:
                    raise ServiceError(404, "attempt not found")
            chunks = (
                [
                    row_dict(row)
                    for row in connection.execute(
                        select(logs)
                        .where(logs.c.attempt_id == selected, logs.c.seq > after)
                        .order_by(logs.c.seq)
                        .limit(64)
                    )
                ]
                if selected
                else []
            )
            for chunk in chunks:
                chunk["data_base64"] = base64.b64encode(chunk.pop("data")).decode()
            return {
                "attempt_id": selected,
                "chunks": chunks,
                "next_seq": chunks[-1]["seq"] if chunks else after,
                "terminal": job["state"] in TERMINAL,
            }

    @staticmethod
    def _artifact_view(value: dict) -> dict:
        value.pop("relative_path", None)
        return value
