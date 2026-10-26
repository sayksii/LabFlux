"""FCFS scheduling and durable job/worker lifecycle transitions."""

from __future__ import annotations

import base64
import time
import uuid
from collections.abc import Callable

from sqlalchemy import func, select

from labflux.config import Settings
from labflux.database import (
    Database,
    artifacts,
    attempt_gpus,
    attempts,
    commands,
    events,
    jobs,
    logs,
    nodes,
)
from labflux.models import Heartbeat, JobSpec, LogUpload, WorkerEvent, WorkerRegistration

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
EXPIRED_ATTEMPT = "worker heartbeat expired; termination unconfirmed"


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
        gpu_uuids = (
            connection.execute(
                select(attempt_gpus.c.gpu_uuid)
                .where(attempt_gpus.c.node_id == node_id, attempt_gpus.c.released.is_(False))
                .order_by(attempt_gpus.c.gpu_uuid)
            )
            .scalars()
            .all()
        )
        return {
            "cpu": sum(item["cpu"] for item in assigned),
            "memory_mib": sum(item["memory_mib"] for item in assigned),
            "jobs": len(assigned),
            "gpu": len(gpu_uuids),
            "gpu_uuids": list(gpu_uuids),
        }

    @staticmethod
    def _held_gpus(connection) -> set[str]:
        return set(
            connection.execute(
                select(attempt_gpus.c.gpu_uuid).where(attempt_gpus.c.released.is_(False))
            ).scalars()
        )

    @staticmethod
    def _eligible_gpus(node: dict, request: dict, held: set[str]) -> list[dict]:
        """Pooled, unallocated cards whose latest heartbeat shows no external use."""
        observed = {gpu["uuid"]: gpu for gpu in node["observed"].get("gpus", [])}
        model = (request["gpu_model"] or "").lower()
        cards = [
            gpu
            for gpu in node["capacity"].get("gpus", [])
            if gpu["uuid"] not in held
            and gpu["uuid"] in observed
            and not observed[gpu["uuid"]]["external_use"]
            and model in gpu["name"].lower()
            and gpu["memory_total_mib"] >= (request["min_vram_mib"] or 0)
        ]
        # Smaller cards first, keeping large cards for jobs that require them.
        return sorted(cards, key=lambda gpu: (gpu["memory_total_mib"], gpu["index"]))

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
            pooled = {gpu["uuid"] for gpu in capacity["gpus"]}
            for other in connection.execute(select(nodes).where(nodes.c.id != payload.worker_id)):
                if pooled & {gpu["uuid"] for gpu in other.capacity.get("gpus", [])}:
                    raise ServiceError(409, f"GPU is already pooled by worker {other.id}")
            allocation = self._allocations(connection, payload.worker_id)
            legacy = payload.inventory is None
            if (
                legacy
                and existing
                and existing._mapping["instance_id"] != payload.instance_id
                and allocation["jobs"]
            ):
                raise ServiceError(
                    409, "worker has unreleased attempts; reconcile them before restarting"
                )
            status = "ONLINE"
            if (
                legacy
                and existing
                and existing._mapping["status"] == "OFFLINE"
                and allocation["jobs"]
            ):
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
            # Reconciled in this transaction, so nothing is dispatched to an unreconciled node.
            reconciled = {} if legacy else self._reconcile(connection, payload)
            self._schedule(connection)
        return {"worker_id": payload.worker_id, "status": status, **reconciled}

    def _reconcile(self, connection, payload: WorkerRegistration) -> dict:
        """Settle a restarted worker's unreleased attempts against what it found locally.

        A surviving container or an undelivered result is adopted; a record that never reached
        container creation is started again; anything else has nothing left running, which
        confirms termination, so it is closed and released.
        """
        worker_id, inventory = payload.worker_id, payload.inventory
        if any(item.worker_id != worker_id for item in inventory.containers):
            raise ServiceError(422, "inventory lists containers of another worker")
        containers = {item.attempt_id: item for item in inventory.containers}
        records = {item.attempt_id: item for item in inventory.attempts}
        known = {
            row.id: row_dict(row)
            for row in connection.execute(
                select(attempts).where(attempts.c.id.in_(set(containers) | set(records)))
            )
        }
        if any(attempt["node_id"] != worker_id for attempt in known.values()):
            raise ServiceError(409, "inventory lists attempts of another worker")
        result = {"adopt": [], "restart": [], "closed": [], "remove": []}
        unreleased = connection.execute(
            select(attempts)
            .where(attempts.c.node_id == worker_id, attempts.c.released.is_(False))
            .order_by(attempts.c.created_at)
        ).all()
        for row in unreleased:
            attempt = row_dict(row)
            job = self._job(connection, attempt["job_id"])
            current = job["current_attempt_id"] == attempt["id"]
            container = containers.get(attempt["id"])
            record = records.get(attempt["id"])
            start = connection.execute(
                select(commands).where(commands.c.id == f"start-{attempt['id']}")
            ).first()
            if container or (record and record.unsent_result):
                started_at = attempt["started_at"]
                if started_at is None and container and container.status != "created":
                    started_at = container.started_at or self.clock()
                if job["cancel_requested"]:
                    state = "STOPPING"
                elif started_at or (record and record.unsent_result):
                    state = "RUNNING"
                else:
                    state = "STARTING"
                connection.execute(
                    attempts.update()
                    .where(attempts.c.id == attempt["id"])
                    .values(state=state, started_at=started_at, reason="adopted by new instance")
                )
                if current:
                    connection.execute(
                        jobs.update()
                        .where(jobs.c.id == job["id"])
                        .values(
                            state=state,
                            reason=(
                                "waiting for confirmed container termination"
                                if state == "STOPPING"
                                else "executing"
                            ),
                            updated_at=self.clock(),
                        )
                    )
                logged = {"stdout": 0, "stderr": 0}
                next_seq = 0
                for stream, size, last in connection.execute(
                    select(logs.c.stream, func.sum(func.length(logs.c.data)), func.max(logs.c.seq))
                    .where(logs.c.attempt_id == attempt["id"])
                    .group_by(logs.c.stream)
                ):
                    logged[stream] = size
                    next_seq = max(next_seq, last + 1)
                result["adopt"].append(
                    {
                        "attempt_id": attempt["id"],
                        "payload": start.payload,
                        "cancel": job["cancel_requested"],
                        # Used only when the worker lost its local record of the attempt.
                        "log_bytes": logged,
                        "next_seq": next_seq,
                    }
                )
            elif record and not record.launched:
                if job["cancel_requested"]:
                    self._finish(connection, attempt, job, "CANCELLED", "cancelled before start")
                    result["closed"].append(attempt["id"])
                    continue
                # No container was ever created, so running it from the start is safe.
                connection.execute(
                    commands.update()
                    .where(commands.c.id == f"start-{attempt['id']}")
                    .values(acked=False)
                )
                connection.execute(
                    attempts.update()
                    .where(attempts.c.id == attempt["id"])
                    .values(state="STARTING", reason="restarted after worker restart")
                )
                if current:
                    connection.execute(
                        jobs.update()
                        .where(jobs.c.id == job["id"])
                        .values(
                            state="STARTING",
                            reason=f"assigned to {worker_id}",
                            updated_at=self.clock(),
                        )
                    )
                result["restart"].append(attempt["id"])
            elif record is None and start is not None and not start.acked:
                continue  # Never delivered; normal command polling hands it to the new instance.
            else:
                self._finish(
                    connection, attempt, job, "FAILED", "container missing after worker restart"
                )
                result["closed"].append(attempt["id"])
        for item in inventory.containers:
            attempt = known.get(item.attempt_id)
            if attempt is None or attempt["released"]:
                result["remove"].append(item.attempt_id)
        return result

    def _finish(
        self,
        connection,
        attempt: dict,
        job: dict,
        outcome: str,
        reason: str,
        exit_code: int | None = None,
        oom_killed: bool = False,
    ) -> None:
        """Record confirmed termination; CPU, RAM and GPUs are released together."""
        outcome = "CANCELLED" if job["cancel_requested"] else outcome
        connection.execute(
            attempts.update()
            .where(attempts.c.id == attempt["id"])
            .values(
                state=outcome,
                released=True,
                finished_at=self.clock(),
                exit_code=exit_code,
                reason=reason,
                oom_killed=oom_killed,
            )
        )
        connection.execute(
            attempt_gpus.update()
            .where(attempt_gpus.c.attempt_id == attempt["id"])
            .values(released=True)
        )
        if job["current_attempt_id"] == attempt["id"]:
            connection.execute(
                jobs.update()
                .where(jobs.c.id == job["id"])
                .values(state=outcome, reason=reason, updated_at=self.clock())
            )
        connection.execute(
            commands.update().where(commands.c.attempt_id == attempt["id"]).values(acked=True)
        )

    def heartbeat(self, node_id: str, payload: Heartbeat) -> dict:
        with self.db.transaction() as connection:
            node = self._node(connection, node_id, payload.instance_id)
            status = node["status"]
            if status == "OFFLINE":
                # The same instance is back after its heartbeats lapsed; its records are intact.
                status = "ONLINE"
                self._resume(connection, node_id, set(payload.active_attempts))
            connection.execute(
                nodes.update()
                .where(nodes.c.id == node_id)
                .values(
                    last_seen=self.clock(),
                    observed={
                        **payload.observed,
                        "gpus": [gpu.model_dump() for gpu in payload.gpus],
                    },
                    status=status,
                )
            )
            self._schedule(connection)
        return {"status": status}

    def _resume(self, connection, node_id: str, active: set[str]) -> None:
        """Attempts that went UNKNOWN only through lapsed heartbeats and that the same instance
        still executes are running again; the rest stay UNKNOWN until their FINISHED event."""
        lapsed = connection.execute(
            select(attempts).where(
                attempts.c.node_id == node_id,
                attempts.c.released.is_(False),
                attempts.c.state == "UNKNOWN",
                attempts.c.reason == EXPIRED_ATTEMPT,
            )
        ).all()
        for row in lapsed:
            if row.id not in active:
                continue
            job = self._job(connection, row.job_id)
            if job["cancel_requested"]:
                state, reason = "STOPPING", "waiting for confirmed container termination"
            elif row.started_at:
                state, reason = "RUNNING", "executing"
            else:
                state, reason = "STARTING", f"assigned to {node_id}"
            connection.execute(
                attempts.update()
                .where(attempts.c.id == row.id)
                .values(state=state, reason="worker heartbeats resumed")
            )
            if job["current_attempt_id"] == row.id and job["state"] == "UNKNOWN":
                connection.execute(
                    jobs.update()
                    .where(jobs.c.id == job["id"])
                    .values(state=state, reason=reason, updated_at=self.clock())
                )

    def submit(self, owner: str, spec: JobSpec) -> dict:
        if spec.time_limit_s is None:
            spec = spec.model_copy(update={"time_limit_s": self.settings.job_time_limit_s})
        if spec.image is None:
            spec = spec.model_copy(update={"image": self.settings.default_image})
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
        held = self._held_gpus(connection)
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
                cards = self._eligible_gpus(node, request, held) if request["gpu"] else []
                if (
                    request["cpu"] <= cap["cpu"] - used["cpu"] + 1e-9
                    and request["memory_mib"] <= cap["memory_mib"] - used["memory_mib"]
                    and used["jobs"] < self.settings.max_parallel_jobs
                    and len(cards) >= request["gpu"]
                ):
                    candidates.append((node, cards))
            if not candidates:
                reason = (
                    "no online worker"
                    if not available
                    else "waiting for matching CPU/RAM/GPU capacity or datasets"
                )
                connection.execute(
                    jobs.update().where(jobs.c.id == job["id"]).values(reason=reason)
                )
                break  # Strict FCFS: never bypass an unschedulable head job.
            node, cards = min(
                candidates,
                key=lambda c: (
                    len(c[1]) - request["gpu"],
                    c[0]["capacity"]["cpu"] - allocated[c[0]["id"]]["cpu"] - request["cpu"],
                    c[0]["capacity"]["cpu"],
                    c[0]["capacity"]["memory_mib"]
                    - allocated[c[0]["id"]]["memory_mib"]
                    - request["memory_mib"],
                    c[0]["id"],
                ),
            )
            gpu_uuids = [gpu["uuid"] for gpu in cards[: request["gpu"]]]
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
            for gpu_uuid in gpu_uuids:
                connection.execute(
                    attempt_gpus.insert().values(
                        attempt_id=attempt_id,
                        gpu_uuid=gpu_uuid,
                        node_id=node["id"],
                        released=False,
                    )
                )
            held.update(gpu_uuids)
            payload = {
                "job_id": job["id"],
                "attempt_id": attempt_id,
                "worker_id": node["id"],
                "job": spec.model_dump(),
                "gpu_uuids": gpu_uuids,
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
                            reason=EXPIRED_ATTEMPT,
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
            assigned: dict[str, list[str]] = {}
            for row in connection.execute(
                select(attempt_gpus)
                .where(attempt_gpus.c.attempt_id.in_([a["id"] for a in job["attempts"]]))
                .order_by(attempt_gpus.c.gpu_uuid)
            ):
                assigned.setdefault(row.attempt_id, []).append(row.gpu_uuid)
            for attempt in job["attempts"]:
                attempt["gpu_uuids"] = assigned.get(attempt["id"], [])
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
                self._finish(
                    connection,
                    attempt,
                    job,
                    event.outcome,
                    event.reason,
                    event.exit_code,
                    event.oom_killed,
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
