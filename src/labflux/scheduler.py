"""Policy-driven scheduling and durable job/worker lifecycle transitions."""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from functools import partial

from pydantic import ValidationError
from sqlalchemy import and_, func, literal_column, or_, select

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
    project_results,
    user_tokens,
)
from labflux.explain import blocker, placement, who
from labflux.models import (
    MODEL_KIND,
    MODEL_OWNER,
    Heartbeat,
    JobSpec,
    LogUpload,
    ServiceSpec,
    WorkerEvent,
    WorkerRegistration,
)
from labflux.policy import (
    INTERACTIVE_CLASS,
    Booking,
    Ranked,
    Release,
    Reservation,
    Victim,
    cheapest_preemption,
    choose_victims,
    class_rank,
    clock_text,
    dominant_share,
    earliest_start,
    fits_at,
    lost_seconds,
    make_policy,
    short,
)
from labflux.session_protocol import (
    KEEPALIVE_ARGV,
    SESSION_ENV,
    SESSION_KIND,
    SESSION_LIVE_STATES,
    SESSION_READY_STATE,
    SessionRequest,
    SessionView,
)

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
EXPIRED_ATTEMPT = "worker heartbeat expired; termination unconfirmed"
# A node restored after a Scheduler restart: it receives no new work until its Worker proves
# its instance (heartbeat) or registers a new one with an inventory.
RECONCILING = "RECONCILING"
STOP_WAIT = "waiting for confirmed container termination"
# Model services (M10): states in which a service counts as the model's one live service. A
# STOPPING one does not: a request then starts the next service, which waits for its resources.
MODEL_LIVE_STATES = frozenset({"QUEUED", "STARTING", "RUNNING", "UNKNOWN"})
# Ended without LabFlux stopping it: started again only after retry_after_failure_s.
MODEL_ENDED = frozenset({"FAILED", "SUCCEEDED"})
MODEL_STATES = {
    "QUEUED": "waiting",
    "STARTING": "starting",
    "RUNNING": "serving",
    "STOPPING": "stopping",
    "UNKNOWN": "unknown",
    "FAILED": "failed",
    "SUCCEEDED": "failed",
    "CANCELLED": "stopped",
}
# A persistent model's lease, so backfill bookings treat it as never ending.
PERSISTENT_LEASE_S = 365 * 24 * 3600.0
# The state of an attempt whose termination was confirmed after a preemption stop (M11).
PREEMPTED = "PREEMPTED"
# Scheduling decisions kept in memory for the queue view (M12).
RECENT_DECISIONS = 50


def stop_reason(attempt: dict) -> str:
    """Job reason while an attempt is STOPPING: a cancel or session end, or a preemption."""
    if attempt.get("preempted_by"):
        return f"preempted by job {short(attempt['preempted_by'])}; {STOP_WAIT}"
    return STOP_WAIT


class ServiceError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


def row_dict(row) -> dict:
    return dict(row._mapping)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Scheduler:
    def __init__(self, settings: Settings, clock: Callable[[], float] = time.time):
        self.settings = settings
        self.clock = clock
        self.db = Database(settings.data_dir)
        self.policy = make_policy(settings)
        # Wakes a node's Worker after its commands commit (the API wires it to the hub).
        self.doorbell: Callable[[str], None] = lambda node_id: None
        # Sessions with a fast-lane command in flight. Only the relay changes it, on the event
        # loop; tick() only reads it, so a lease never expires during a command.
        self.session_busy: set[str] = set()
        # Queued sessions: when their client last asked about them. In memory only, so after a
        # restart every queued session gets a full window again.
        self.session_polled: dict[str, float] = {}
        # Inference requests in flight per model service job (M10); in memory only, since a
        # Scheduler restart ends every relayed request anyway.
        self.service_inflight: dict[str, int] = {}
        self.service_lock = threading.Lock()
        # Queued model services: when a request last asked for them (ensure_model). Like
        # session_polled, in memory only: after a restart each one gets a full window again.
        self.model_polled: dict[str, float] = {}
        # Owners of the tokens issued by `labflux login`, by SHA-256 (Q14). This process is the
        # only writer, so the copy stays exact and authenticating never reads the database.
        self.token_owners: dict[str, str] = {}
        # Relayed requests begun, and when one last began or ended, per model service job (M12).
        # In memory only, like service_inflight: they count from the Scheduler's start.
        self.service_served: dict[str, int] = {}
        self.service_activity: dict[str, float] = {}
        # The result of the last scheduling pass and the latest decisions (M12), served by
        # queue_view. In memory only: empty again after a restart until the first pass. A pass
        # replaces the snapshot as a whole once its transaction commits.
        self.queue_snapshot: dict = self._queue_snapshot(None, [], None, {})
        self.decisions: deque[dict] = deque(maxlen=RECENT_DECISIONS)
        self.decisions_lock = threading.Lock()
        self.restore()

    def restore(self) -> None:
        """Resume from the database after a (re)start.

        Every committed transition is already durable, so only liveness is stale: nodes that
        were ONLINE wait for their Worker again, with a fresh offline window measured from now
        so that a long Scheduler downtime does not expire them all on the first tick.
        """
        with self.db.transaction() as connection:
            connection.execute(
                nodes.update()
                .where(nodes.c.status == "ONLINE")
                .values(status=RECONCILING, last_seen=self.clock())
            )
            self.token_owners = {
                row.token_sha256: row.owner
                for row in connection.execute(
                    select(user_tokens.c.token_sha256, user_tokens.c.owner)
                )
            }

    def issue_token(self, node_id: str, user: str) -> dict:
        """A new token for an account that node_id's Worker identified (labflux login, Q14).
        The user's newest login_tokens_per_user tokens stay valid, e.g. the copy on another
        machine; older ones are deleted."""
        token = secrets.token_urlsafe(32)
        digest = token_digest(token)
        # Holding the lock past the commit keeps token_owners in step with concurrent logins.
        with self.db.lock:
            with self.db.transaction() as connection:
                connection.execute(
                    user_tokens.insert().values(
                        token_sha256=digest, owner=user, node_id=node_id, created_at=self.clock()
                    )
                )
                # SQLite gives each insert a larger rowid than every row left, so this is
                # issue order, and the new token always survives.
                stale = list(
                    connection.execute(
                        select(user_tokens.c.token_sha256)
                        .where(user_tokens.c.owner == user)
                        .order_by(literal_column("rowid").desc())
                        .offset(self.settings.login_tokens_per_user)
                    ).scalars()
                )
                if stale:
                    connection.execute(
                        user_tokens.delete().where(user_tokens.c.token_sha256.in_(stale))
                    )
            self.token_owners[digest] = user
            for old in stale:
                self.token_owners.pop(old, None)
        return {"user": user, "token": token}

    def token_owner(self, token: str) -> str | None:
        return self.token_owners.get(token_digest(token))

    def _ring(self, node_id: str) -> None:
        """Call next to every command insert or redelivery: rings once the transaction commits.
        A missed ring only delays the command until the Worker's fallback poll."""
        self.db.after_commit(self.doorbell, node_id)

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
            if status == "ONLINE":
                self._decide(
                    "node_online",
                    f"node {payload.worker_id} is online: a Worker instance registered",
                    node_id=payload.worker_id,
                )
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
            # A cancel or a pending preemption: the attempt must stop, never run again.
            stopping = bool(job["cancel_requested"] or attempt["preempted_by"])
            if container or (record and record.unsent_result):
                # A model serves only once its STARTED event, sent after the health check
                # passes, arrives: a running container may still be loading the weights.
                model = job["kind"] == MODEL_KIND
                started_at = attempt["started_at"]
                if started_at is None and container and container.status != "created" and not model:
                    started_at = container.started_at or self.clock()
                if stopping:
                    state = "STOPPING"
                elif started_at or (record and record.unsent_result and not model):
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
                            reason=stop_reason(attempt) if state == "STOPPING" else "executing",
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
                        "cancel": stopping,
                        # Used only when the worker lost its local record of the attempt.
                        "log_bytes": logged,
                        "next_seq": next_seq,
                    }
                )
            elif record and not record.launched:
                if stopping:
                    # A preempted attempt ends PREEMPTED here and its job queues again.
                    self._finish(connection, attempt, job, "CANCELLED", "cancelled before start")
                    result["closed"].append(attempt["id"])
                    continue
                # No container was ever created, so running it from the start is safe.
                connection.execute(
                    commands.update()
                    .where(commands.c.id == f"start-{attempt['id']}")
                    .values(acked=False)
                )
                self._ring(worker_id)
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
                # The node lost the execution (crash, reboot, removed container): retryable.
                # The Worker keeps the directory of a launched run, so a retry continues there.
                self._finish(
                    connection,
                    attempt,
                    job,
                    "FAILED",
                    "container missing after worker restart",
                    lost=True,
                    kept=bool(record and record.launched),
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
        lost: bool = False,
        kept: bool = False,
        size: int | None = None,
    ) -> None:
        """Record confirmed termination; CPU, RAM and GPUs are released together.

        `lost` marks an infrastructure loss; a job with retries left is queued again. An attempt
        stopped for a preemption whose stop took effect, or whose execution was lost, becomes
        PREEMPTED and its job is queued again; an outcome it reached on its own is kept.
        `kept` means the Worker keeps the attempt's directory (`size` bytes, None when
        unknown).
        """
        preempted_by = attempt.get("preempted_by")
        if job["cancel_requested"]:
            outcome = "CANCELLED"
        elif preempted_by and (outcome == "CANCELLED" or lost):
            outcome = PREEMPTED
            reason = f"preempted by job {short(preempted_by)} ({reason})"
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
        if kept:
            self._keep_result(connection, attempt, job, size)
        if job["current_attempt_id"] == attempt["id"]:
            state = outcome
            if outcome == PREEMPTED:
                # Same submission_seq, like a retry; dispatch adds a new attempt.
                count = connection.execute(
                    select(func.count()).where(
                        attempts.c.job_id == job["id"], attempts.c.state == PREEMPTED
                    )
                ).scalar_one()
                state = "QUEUED"
                reason = (
                    f"preempted by job {short(preempted_by)}; queued to run again "
                    f"(preemption {count} of {self.settings.max_preemptions_per_job})"
                )
                self._decide(
                    "requeued_after_preemption",
                    f"{who(job)}: {reason}",
                    job=job,
                    node_id=attempt["node_id"],
                    detail={
                        "attempt_id": attempt["id"],
                        "preempted_by": preempted_by,
                        "preemptions": count,
                    },
                )
            elif lost and outcome != "CANCELLED":
                limit = JobSpec.model_validate(job["spec"]).retries_on_node_failure
                # Earlier attempts lost to the infrastructure; preemptions do not count.
                used = (
                    connection.execute(
                        select(func.count()).where(
                            attempts.c.job_id == job["id"], attempts.c.state != PREEMPTED
                        )
                    ).scalar_one()
                    - 1
                )
                if used < limit:
                    # Same submission_seq: the job returns to its original queue position,
                    # ahead of everything submitted after it. Dispatch adds a new attempt.
                    state = "QUEUED"
                    reason = f"{reason}; queued for retry {used + 1} of {limit}"
                    self._decide(
                        "retried_after_node_failure",
                        f"{who(job)}: {reason}",
                        job=job,
                        node_id=attempt["node_id"],
                        detail={"attempt_id": attempt["id"], "retry": used + 1, "limit": limit},
                    )
            connection.execute(
                jobs.update()
                .where(jobs.c.id == job["id"])
                .values(state=state, reason=reason, updated_at=self.clock())
            )
        connection.execute(
            commands.update().where(commands.c.attempt_id == attempt["id"]).values(acked=True)
        )
        # After the job's new state: a job queued for retry keeps its directories (P2).
        self._prune_results(connection, job)

    def _keep_result(self, connection, attempt: dict, job: dict, size: int | None) -> None:
        """Record a project directory kept on its node (6.1 P0a)."""
        project = job["spec"].get("project")
        if not project or job["kind"] in (SESSION_KIND, MODEL_KIND):
            return  # A session never counts as a previous run (P5); a model service has none.
        connection.execute(
            project_results.insert().values(
                attempt_id=attempt["id"],
                job_id=job["id"],
                owner=job["owner"],
                project=project,
                node_id=attempt["node_id"],
                size=size,
                ended_at=self.clock(),
            )
        )

    def _prune_results(self, connection, job: dict) -> None:
        """Delete the job's project's directories beyond the newest results_keep on each node
        (P4) with durable PRUNE commands; a row goes when its PRUNE is queued, so the table lists
        what remains. A directory of a job that can still run again (queued for a retry) stays,
        since the retry continues from it (P2); it goes when that job ends or is cancelled,
        which runs this again."""
        project = job["spec"].get("project")
        if not project or job["kind"] in (SESSION_KIND, MODEL_KIND):
            return
        rows = connection.execute(
            select(
                project_results.c.id,
                project_results.c.attempt_id,
                project_results.c.node_id,
                jobs.c.state,
            )
            .select_from(project_results.join(jobs, jobs.c.id == project_results.c.job_id))
            .where(project_results.c.owner == job["owner"], project_results.c.project == project)
            .order_by(project_results.c.node_id, project_results.c.id.desc())
        ).all()
        newer: dict[str, int] = {}
        pruned = set()
        for row in rows:
            position = newer.get(row.node_id, 0)
            newer[row.node_id] = position + 1
            if position < self.settings.results_keep or row.state not in TERMINAL:
                continue
            connection.execute(project_results.delete().where(project_results.c.id == row.id))
            connection.execute(
                commands.insert().values(
                    id=f"prune-{row.attempt_id}",
                    node_id=row.node_id,
                    attempt_id=row.attempt_id,
                    kind="PRUNE",
                    payload={"attempt_id": row.attempt_id, "worker_id": row.node_id},
                    acked=False,
                    created_at=self.clock(),
                )
            )
            pruned.add(row.node_id)
        for node_id in sorted(pruned):
            self._ring(node_id)

    @staticmethod
    def _project_results(connection, job: dict, spec: JobSpec) -> list:
        """The project's kept directories, best source first: this job's own earlier attempts
        (a retry continues from them, P2), then the most recently ended runs (P0c)."""
        if not spec.project or job["kind"] == MODEL_KIND:
            return []
        return connection.execute(
            select(
                project_results.c.attempt_id, project_results.c.job_id, project_results.c.node_id
            )
            .where(
                project_results.c.owner == job["owner"], project_results.c.project == spec.project
            )
            .order_by(project_results.c.job_id != job["id"], project_results.c.id.desc())
        ).all()

    def heartbeat(self, node_id: str, payload: Heartbeat) -> dict:
        with self.db.transaction() as connection:
            node = self._node(connection, node_id, payload.instance_id)
            status = node["status"]
            if status in ("OFFLINE", RECONCILING):
                # The same instance is back after its heartbeats lapsed or the Scheduler
                # restarted; its records are intact, so the instance check reconciles it.
                after = "a Scheduler restart" if status == RECONCILING else "its heartbeats lapsed"
                self._decide(
                    "node_online",
                    f"node {node_id} is online again: the same Worker instance is back after "
                    f"{after}",
                    node_id=node_id,
                )
                status = "ONLINE"
                self._resume(connection, node_id, set(payload.active_attempts))
                self._ring(node_id)  # START commands are handed out again.
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
            if job["cancel_requested"] or row.preempted_by:
                state, reason = "STOPPING", stop_reason(row_dict(row))
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
        if spec.priority is None:
            spec = spec.model_copy(update={"priority": self.settings.default_priority})
        if spec.estimated_runtime_s is not None and spec.estimated_runtime_s > spec.time_limit_s:
            raise ServiceError(422, "estimated_runtime_s cannot exceed time_limit_s")
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
            job_id = self._insert_job(connection, owner, spec)
            self._schedule(connection)
        return self.get_job(job_id, owner)

    def _insert_job(
        self,
        connection,
        owner: str,
        spec: JobSpec,
        kind: str = "BATCH",
        service: str | None = None,
    ) -> str:
        job_id = uuid.uuid4().hex
        now = self.clock()
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
                kind=kind,
                service=service,
            )
        )
        return job_id

    def _candidates(self, spec: JobSpec, available: list, allocated: dict, held: set) -> list:
        """Nodes where the job fits right now, each with its eligible cards (best first)."""
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
        return candidates

    def _bookings(self, connection, available: list, now: float) -> dict[str, list[Booking]]:
        """Unreleased attempts per node, each held until its start plus its hard time limit.

        An attempt that has not reported STARTED counts from dispatch, so image pulls and source
        downloads are not part of the estimate. A session has no hard limit: it is held until
        its current lease expiry, which every command moves forward. An attempt being stopped
        ends now, so the head job's reservation covers what it frees: every stop (a cancel, a
        session end, a model stop or a preemption) marks the job cancel_requested or the attempt
        preempted_by, whether the attempt is STOPPING or UNKNOWN after lapsed heartbeats.
        """
        result: dict[str, list[Booking]] = {node["id"]: [] for node in available}
        cards: dict[str, set[str]] = {}
        for row in connection.execute(
            select(attempt_gpus.c.attempt_id, attempt_gpus.c.gpu_uuid).where(
                attempt_gpus.c.released.is_(False)
            )
        ):
            cards.setdefault(row.attempt_id, set()).add(row.gpu_uuid)
        for row in connection.execute(
            select(
                attempts.c.id,
                attempts.c.node_id,
                attempts.c.resources,
                attempts.c.created_at,
                attempts.c.started_at,
                attempts.c.preempted_by,
                jobs.c.spec,
                jobs.c.lease_expires_at,
                jobs.c.cancel_requested,
            )
            .select_from(attempts.join(jobs, jobs.c.id == attempts.c.job_id))
            .where(attempts.c.released.is_(False), attempts.c.node_id.in_(list(result)))
        ):
            limit = row.spec.get("time_limit_s") or self.settings.job_time_limit_s
            # Only a session that has started has a lease.
            end = row.lease_expires_at or (row.started_at or row.created_at) + limit
            result[row.node_id].append(
                Booking(
                    end=now if row.cancel_requested or row.preempted_by else end,
                    cpu=row.resources["cpu"],
                    memory_mib=row.resources["memory_mib"],
                    gpus=frozenset(cards.get(row.id, ())),
                )
            )
        return result

    def _recent_usage(self, connection, now: float, available: list) -> dict[str, float]:
        """Per-user reserved minutes in the usage window, scaled by the dominant cluster share."""
        capacity = {
            "cpu": sum(node["capacity"]["cpu"] for node in available),
            "memory_mib": sum(node["capacity"]["memory_mib"] for node in available),
            "gpu": sum(len(node["capacity"].get("gpus", [])) for node in available),
        }
        since = now - self.settings.usage_window_s
        usage: dict[str, float] = {}
        for row in connection.execute(
            select(
                jobs.c.owner,
                attempts.c.resources,
                attempts.c.created_at,
                attempts.c.finished_at,
                attempts.c.released,
            )
            .select_from(attempts.join(jobs, jobs.c.id == attempts.c.job_id))
            .where(or_(attempts.c.released.is_(False), attempts.c.finished_at > since))
        ):
            end = (row.finished_at or now) if row.released else now
            seconds = max(0.0, min(end, now) - max(row.created_at, since))
            share = dominant_share(row.resources, capacity)
            usage[row.owner] = usage.get(row.owner, 0.0) + seconds / 60 * share
        return usage

    def _reserve(
        self, entry: Ranked, available: list, bookings: dict, now: float
    ) -> Reservation | None:
        """Earliest time and node at which the blocked head job fits, from hard time limits."""
        request = entry.spec.resources.model_dump()
        best = None
        for node in available:
            if not set(entry.spec.datasets).issubset(node["capacity"]["datasets"]):
                continue
            cards = frozenset(gpu["uuid"] for gpu in self._eligible_gpus(node, request, set()))
            at = earliest_start(
                node, cards, bookings[node["id"]], request, now, self.settings.max_parallel_jobs
            )
            if at is not None and (best is None or (at, node["id"]) < best[:2]):
                best = (at, node["id"], cards)
        if best is None:
            return None
        return Reservation(entry.job["id"], best[1], best[0], request, best[2])

    def _keeps_reservation(
        self, reservation: Reservation, bookings: dict, node: dict, cards: list, spec, now: float
    ) -> bool:
        """EASY rule: starting here must leave the head job able to start at its reservation."""
        if node["id"] != reservation.node_id:
            return True
        request = spec.resources.model_dump()
        limit = spec.time_limit_s or self.settings.job_time_limit_s
        chosen = frozenset(gpu["uuid"] for gpu in cards[: request["gpu"]])
        trial = bookings[node["id"]] + [
            Booking(now + limit, request["cpu"], request["memory_mib"], chosen)
        ]
        return fits_at(
            node,
            reservation.cards,
            trial,
            reservation.request,
            reservation.start,
            self.settings.max_parallel_jobs,
        )

    def _preempt(
        self,
        connection,
        entry: Ranked,
        available: list,
        allocated: dict,
        held: set,
        usage: dict,
        now: float,
    ) -> tuple[str, list[str]] | None:
        """Let the blocked head job stop preemptible batch jobs to make room for it (M11).

        Returns a note, and the jobs it names (M12), when the head waits for preempted jobs or
        for resources already on their way back, or None when nothing is preempted for it.
        Interactive work (sessions, model services) may preempt under every policy, a batch job
        only under policies that allow it. One round at a time: while any attempt is still
        stopping for a preemption, no new one starts. Resources already on their way back count
        as free: if the head fits once they are released it preempts nothing, otherwise it needs
        fewer victims. The resources are released, and the head dispatched, only by the victims'
        confirmed termination.
        """
        job, spec = entry.job, entry.spec
        if not self.settings.preemption_enabled or not (
            class_rank(job) == INTERACTIVE_CLASS
            or (job.get("kind") == "BATCH" and self.policy.batch_preemption)
        ):
            return None
        waiting = self._preemption_in_progress(connection, job)
        if waiting:
            return waiting
        victims = self._victims(connection, entry, available, usage, now)
        releases = self._releases(connection, available, now)
        request = spec.resources.model_dump()
        options, ready = {}, []
        for node in available:
            here, freed = victims.get(node["id"], []), releases.get(node["id"], [])
            if not (here or freed) or not set(spec.datasets).issubset(node["capacity"]["datasets"]):
                continue
            matching = frozenset(gpu["uuid"] for gpu in self._eligible_gpus(node, request, set()))
            chosen = choose_victims(
                node,
                allocated[node["id"]],
                frozenset(held),
                matching,
                request,
                here,
                self.settings.max_parallel_jobs,
                freed,
            )
            if chosen == [] and freed:
                ready.append(node["id"])
            elif chosen:
                options[node["id"]] = chosen
        if ready:
            freeing = releases[min(ready)]
            note = "waiting for " + ", ".join(release.what for release in freeing)
            return note, [release.job_id for release in freeing]
        best = cheapest_preemption(options)
        if best is None:
            return None
        node_id, chosen = best
        for victim in chosen:
            self._request_stop(
                connection,
                self._job(connection, victim.job_id),
                f"preempted by job {short(job['id'])}; {STOP_WAIT}",
                preempted_by=job["id"],
            )
        lost = sum(victim.lost_s for victim in chosen)
        self._decide(
            "preempting",
            f"{who(job)}: preempting job {', '.join(short(v.job_id) for v in chosen)} on "
            f"{node_id}, the least work lost ({lost:.0f}s); it starts once they stop",
            job=job,
            node_id=node_id,
            detail={"victims": [v.job_id for v in chosen], "node_id": node_id, "lost_s": lost},
        )
        return self._preemption_in_progress(connection, job)

    @staticmethod
    def _preemption_in_progress(connection, job: dict) -> tuple[str, list[str]] | None:
        """A note naming the attempts still stopping for a preemption, and their jobs, if any."""
        rows = connection.execute(
            select(attempts.c.job_id, attempts.c.node_id, attempts.c.preempted_by)
            .where(attempts.c.released.is_(False), attempts.c.preempted_by.is_not(None))
            .order_by(attempts.c.created_at, attempts.c.id)
        ).all()
        if not rows:
            return None
        stopping = ", ".join(f"{short(row.job_id)} on {row.node_id}" for row in rows)
        note = f"waiting for preempted job{'s' if len(rows) > 1 else ''} {stopping} to stop"
        others = sorted({short(row.preempted_by) for row in rows if row.preempted_by != job["id"]})
        if others:
            note += f" (preempted for job {', '.join(others)})"
        return note, [row.job_id for row in rows]

    def _releases(self, connection, available: list, now: float) -> dict[str, list[Release]]:
        """Resources already on their way back, per available node: attempts stopping for a
        cancel, a session end or a model stop, and idle on-demand models that M10 reclaims as
        soon as a job waits. Attempts on nodes that are not available never count."""
        result: dict[str, list[Release]] = {node["id"]: [] for node in available}
        cards: dict[str, set[str]] = {}
        for row in connection.execute(
            select(attempt_gpus.c.attempt_id, attempt_gpus.c.gpu_uuid).where(
                attempt_gpus.c.released.is_(False)
            )
        ):
            cards.setdefault(row.attempt_id, set()).add(row.gpu_uuid)
        for row in connection.execute(
            select(
                jobs,
                attempts.c.id.label("attempt_id"),
                attempts.c.node_id,
                attempts.c.resources,
                attempts.c.state.label("attempt_state"),
            )
            .select_from(attempts.join(jobs, jobs.c.id == attempts.c.job_id))
            .where(
                attempts.c.released.is_(False),
                attempts.c.preempted_by.is_(None),
                attempts.c.node_id.in_(list(result)),
                or_(
                    attempts.c.state == "STOPPING",
                    and_(jobs.c.kind == MODEL_KIND, jobs.c.state == "RUNNING"),
                ),
            )
            .order_by(attempts.c.created_at, attempts.c.id)
        ):
            job = row_dict(row)
            if job["attempt_state"] == "STOPPING":
                model = job["kind"] == MODEL_KIND
                who = f"model {job['service']}" if model else f"job {short(job['id'])}"
                what = f"{who} on {job['node_id']} to stop"
            elif self._idle_stop(job, now, waiting=True):
                what = f"idle model {job['service']} on {job['node_id']} to be reclaimed"
            else:
                continue
            result[job["node_id"]].append(
                Release(
                    what=what,
                    cpu=job["resources"]["cpu"],
                    memory_mib=job["resources"]["memory_mib"],
                    cards=frozenset(cards.get(job["attempt_id"], ())),
                    job_id=job["id"],
                )
            )
        return result

    def _victims(
        self, connection, entry: Ranked, available: list, usage: dict, now: float
    ) -> dict[str, list[Victim]]:
        """Running attempts the head job may preempt, per node.

        A victim is the current attempt of a batch job marked preemptible and safe to rerun,
        not cancelled, RUNNING or STARTING on an available node, not already stopping for a
        preemption and preempted fewer than max_preemptions_per_job times. Sessions and model
        services are never victims; as the head they may stop any such job. A batch head also
        needs a strictly higher explicit priority than the victim, and must rank ahead of it
        even if the victim were queued now: otherwise an old job's waiting bonus would put it
        back in front as soon as it is requeued, and the two would preempt each other.
        """
        job, spec = entry.job, entry.spec
        rows = connection.execute(
            select(
                attempts.c.id.label("attempt_id"),
                attempts.c.node_id,
                attempts.c.resources,
                attempts.c.started_at,
                jobs.c.id.label("job_id"),
                jobs.c.owner,
                jobs.c.spec,
                jobs.c.kind,
                jobs.c.submission_seq,
                jobs.c.created_at,
            )
            .select_from(attempts.join(jobs, jobs.c.current_attempt_id == attempts.c.id))
            .where(
                attempts.c.released.is_(False),
                attempts.c.state.in_(["RUNNING", "STARTING"]),
                attempts.c.preempted_by.is_(None),
                attempts.c.node_id.in_([node["id"] for node in available]),
                jobs.c.kind == "BATCH",
                jobs.c.cancel_requested.is_(False),
            )
        ).all()
        if not rows:
            return {}
        preempted = dict(
            connection.execute(
                select(attempts.c.job_id, func.count())
                .where(
                    attempts.c.state == PREEMPTED,
                    attempts.c.job_id.in_([row.job_id for row in rows]),
                )
                .group_by(attempts.c.job_id)
            ).all()
        )
        cards: dict[str, set[str]] = {}
        for row in connection.execute(
            select(attempt_gpus.c.attempt_id, attempt_gpus.c.gpu_uuid).where(
                attempt_gpus.c.released.is_(False),
                attempt_gpus.c.attempt_id.in_([row.attempt_id for row in rows]),
            )
        ):
            cards.setdefault(row.attempt_id, set()).add(row.gpu_uuid)
        default = self.settings.default_priority
        mine = default if spec.priority is None else spec.priority
        interactive = class_rank(job) == INTERACTIVE_CLASS
        result: dict[str, list[Victim]] = {}
        for row in rows:
            victim_spec = JobSpec.model_validate(row.spec)
            if not (victim_spec.preemptible and victim_spec.safe_to_rerun):
                continue
            if preempted.get(row.job_id, 0) >= self.settings.max_preemptions_per_job:
                continue
            priority = default if victim_spec.priority is None else victim_spec.priority
            if not interactive:
                queued = {
                    "id": row.job_id,
                    "owner": row.owner,
                    "kind": row.kind,
                    "submission_seq": row.submission_seq,
                    "created_at": row.created_at,
                }
                if priority >= mine or not self.policy.before(
                    (job, spec), (queued, victim_spec), now, usage
                ):
                    continue
            result.setdefault(row.node_id, []).append(
                Victim(
                    attempt_id=row.attempt_id,
                    job_id=row.job_id,
                    priority=priority,
                    lost_s=lost_seconds(row.started_at, now),
                    cpu=row.resources["cpu"],
                    memory_mib=row.resources["memory_mib"],
                    cards=frozenset(cards.get(row.attempt_id, ())),
                )
            )
        return result

    def _note(self, connection, job: dict, note: str) -> None:
        if job.get("schedule_note") != note:
            connection.execute(
                jobs.update().where(jobs.c.id == job["id"]).values(schedule_note=note)
            )

    def _decide(
        self,
        kind: str,
        text: str,
        job: dict | None = None,
        node_id: str | None = None,
        detail: dict | None = None,
    ) -> None:
        """Remember a scheduling decision for the queue view (M12) once the transaction
        commits, so a rolled-back one is never shown. In memory only."""
        decision = {
            "at": self.clock(),
            "kind": kind,
            "job_id": job["id"] if job else None,
            "owner": job["owner"] if job else None,
            "node_id": node_id,
            "detail": detail or {},
            "text": text,
        }
        self.db.after_commit(partial(self._remember, decision))

    def _remember(self, decision: dict) -> None:
        with self.decisions_lock:
            self.decisions.appendleft(decision)

    def _queue_snapshot(
        self,
        now: float | None,
        entries: list[dict],
        reservation: Reservation | None,
        usage: dict[str, float],
    ) -> dict:
        s = self.settings
        if reservation is not None:
            request = reservation.request
            reservation = {
                "job_id": reservation.job_id,
                "node_id": reservation.node_id,
                "start": reservation.start,
                "cpu": request["cpu"],
                "memory_mib": request["memory_mib"],
                "gpu": request["gpu"],
            }
        return {
            "policy": self.policy.name,
            "computed_at": now,
            "weights": {
                "priority": s.score_priority_weight,
                "wait": s.score_wait_weight,
                "usage": s.score_usage_weight,
            },
            "entries": entries,
            "reservation": reservation,
            "usage_min_by_user": dict(usage),
        }

    def _publish_queue(self, snapshot: dict) -> None:
        """Replace the queue snapshot as a whole once this pass's transaction commits."""
        self.db.after_commit(partial(setattr, self, "queue_snapshot", snapshot))

    def queue_view(self, viewer: str) -> dict:
        """The queue as the last scheduling pass left it, and the latest decisions, newest
        first (GET /api/queue, M12). Every user sees every entry; `mine` marks the viewer's."""
        snapshot = self.queue_snapshot
        with self.decisions_lock:
            decisions = list(self.decisions)
        return {
            **snapshot,
            "entries": [
                {**entry, "mine": entry["owner"] == viewer} for entry in snapshot["entries"]
            ],
            "recent_decisions": decisions,
        }

    def _schedule(self, connection) -> None:
        """Consider queued jobs in policy order and dispatch every job allowed to start now.

        Without backfilling, the first job that cannot start blocks every later job, unless it is a
        model service. With it, that head job gets a reservation and a later job starts only if it
        cannot delay it (EASY). Every job left queued gets a queue entry that explains the
        decision with the facts it was made from (M12).
        """
        now = self.clock()
        policy = self.policy
        available = [
            row_dict(row)
            for row in connection.execute(
                select(nodes).where(
                    nodes.c.status == "ONLINE",
                    nodes.c.last_seen >= now - self.settings.offline_timeout_s,
                )
            )
        ]
        # Also with an empty queue, so the queue view keeps showing recent usage.
        usage = self._recent_usage(connection, now, available) if policy.uses_usage else {}
        queued = connection.execute(select(jobs).where(jobs.c.state == "QUEUED")).all()
        if not queued:
            self._publish_queue(self._queue_snapshot(now, [], None, usage))
            return
        allocated = {node["id"]: self._allocations(connection, node["id"]) for node in available}
        held = self._held_gpus(connection)
        bookings = self._bookings(connection, available, now) if policy.backfill else {}
        entries = [(row_dict(row), JobSpec.model_validate(row.spec)) for row in queued]
        head: dict | None = None
        reservation: Reservation | None = None
        explained: list[dict] = []

        def explain(
            entry: Ranked,
            decision: str,
            note: str,
            behind: dict | None = None,
            waiting_for: list[str] | None = None,
        ) -> None:
            """Write the job's schedule note and add its queue entry (M12)."""
            job, spec = entry.job, entry.spec
            self._note(connection, job, note)
            batch = class_rank(job) != INTERACTIVE_CLASS
            parts = policy.score_parts(job, spec, now, usage) if batch else None
            priority = self.settings.default_priority if spec.priority is None else spec.priority
            explained.append(
                {
                    "position": len(explained) + 1,
                    "job_id": job["id"],
                    "owner": job["owner"],
                    "kind": job["kind"],
                    "service": job["service"],
                    "priority": priority if batch else None,
                    "score": None
                    if parts is None
                    else parts["priority"] + parts["waited_min"] - parts["usage_min"],
                    "score_parts": parts,
                    "decision": decision,
                    "blocker": blocker(
                        spec.resources.model_dump(),
                        spec.datasets,
                        available,
                        allocated,
                        held,
                        self.settings.max_parallel_jobs,
                    ),
                    "head_job_id": behind["id"] if behind else None,
                    "waiting_for": waiting_for or [],
                    "note": note,
                }
            )

        for entry in policy.order(entries, now, usage):
            job, spec = entry.job, entry.spec
            if job["kind"] == MODEL_KIND and job["service"] not in self.settings.models:
                continue  # Removed from the configuration; the next tick cancels it.
            request = spec.resources.model_dump()
            limit = spec.time_limit_s or self.settings.job_time_limit_s
            if head is not None and not policy.backfill:
                waiting = f"waiting behind job {short(head['id'])}"
                explain(entry, "behind_head", f"{policy.name}: {entry.why}; {waiting}", head)
                continue
            candidates = self._candidates(spec, available, allocated, held)
            fits_now = bool(candidates)
            if reservation is not None:
                # Cards the head job cannot use are taken first; a stable sort, so smaller
                # cards still lead within each group. Dispatch below uses the same order.
                reserved = reservation.cards
                candidates = [
                    (node, sorted(cards, key=lambda gpu: gpu["uuid"] in reserved))
                    for node, cards in candidates
                ]
                candidates = [
                    (node, cards)
                    for node, cards in candidates
                    if self._keeps_reservation(reservation, bookings, node, cards, spec, now)
                ]
            if not candidates:
                if head is None:
                    reason = (
                        "no online worker"
                        if not available
                        else "waiting for matching CPU/RAM/GPU capacity or datasets"
                    )
                    if job["current_attempt_id"] is None:
                        # A job queued again (retry, preemption) keeps the reason it was
                        # requeued; its schedule note says what it waits for.
                        connection.execute(
                            jobs.update().where(jobs.c.id == job["id"]).values(reason=reason)
                        )
                    note = f"{policy.name}: first in line ({entry.why}); {reason}"
                    waiting, waiting_for = self._preempt(
                        connection, entry, available, allocated, held, usage, now
                    ) or (None, [])
                    # For the queue view, no online worker explains a head job first, then
                    # waiting for resources on their way back, then what its policy does.
                    if not available:
                        decision = "no_worker"
                    elif waiting:
                        decision = "waiting_for_release"
                    else:
                        decision = "head_reserved" if policy.backfill else "head_blocked"
                    if job["kind"] == MODEL_KIND and not policy.backfill:
                        # A model service ranks ahead of every batch job, so as the head it
                        # would stop even jobs that fit on idle nodes, for as long as it waits.
                        # It may still preempt; if it cannot, the next blocked job may.
                        note = f"{policy.name}: {entry.why}; {reason}"
                        if waiting:
                            note += f"; {waiting}"
                        if decision == "head_blocked":
                            decision = "does_not_block"
                        note += "; model services do not hold up later jobs"
                        explain(entry, decision, note, waiting_for=waiting_for)
                        continue
                    if waiting:
                        note += f"; {waiting}"
                        if policy.backfill:
                            # Attempts this pass asked to stop now end now.
                            bookings = self._bookings(connection, available, now)
                    if policy.backfill:
                        reservation = self._reserve(entry, available, bookings, now)
                        if reservation is None:
                            # It cannot be reserved anywhere, so it must not leave the next
                            # blocked job unprotected: that job becomes the head instead.
                            note = (
                                f"{policy.name}: {entry.why}; {reason}; no online node can fit "
                                "it right now, so it does not block later jobs"
                            )
                            if available:
                                decision = "does_not_block"
                            explain(entry, decision, note, waiting_for=waiting_for)
                            continue
                        if reservation.start <= now and waiting:
                            note += f"; reserved on {reservation.node_id} as soon as they stop"
                        elif reservation.start <= now:
                            note += (
                                f"; reserved on {reservation.node_id} as soon as attempts past "
                                "their time limit release resources"
                            )
                        else:
                            note += (
                                f"; reserved on {reservation.node_id} at "
                                f"{clock_text(reservation.start)}"
                            )
                    explain(entry, decision, note, waiting_for=waiting_for)
                    head = job
                elif fits_now:
                    estimate = (
                        f" (estimate {spec.estimated_runtime_s:.0f}s is advisory only)"
                        if spec.estimated_runtime_s
                        else ""
                    )
                    note = (
                        f"{policy.name}: {entry.why}; its {limit:.0f}s time limit would delay "
                        f"job {short(head['id'])} reserved on {reservation.node_id} at "
                        f"{clock_text(reservation.start)}{estimate}"
                    )
                    explain(entry, "would_delay_reservation", note, head)
                else:
                    note = (
                        f"{policy.name}: {entry.why}; no matching capacity now; job "
                        f"{short(head['id'])} is first in line"
                    )
                    explain(entry, "no_capacity", note, head)
                continue
            # Nodes holding the project's files go first, the one with the best source first
            # (a retry's own attempt, then the latest run): a preference among the nodes that
            # fit now, never a wait (6.1 P0c). Then best fit; explain.PLACEMENT_CRITERIA names
            # each part of the key.
            results = self._project_results(connection, job, spec)
            rank: dict[str, int] = {}
            for position, row in enumerate(results):
                rank.setdefault(row.node_id, position)
            ranked = [
                (
                    (
                        rank.get(node["id"], len(results)),
                        len(cards) - request["gpu"],
                        node["capacity"]["cpu"] - allocated[node["id"]]["cpu"] - request["cpu"],
                        node["capacity"]["cpu"],
                        node["capacity"]["memory_mib"]
                        - allocated[node["id"]]["memory_mib"]
                        - request["memory_mib"],
                        node["id"],
                    ),
                    node,
                    cards,
                )
                for node, cards in candidates
            ]
            ranked.sort(key=lambda item: item[0])
            _, node, cards = ranked[0]
            gpu_uuids = [gpu["uuid"] for gpu in cards[: request["gpu"]]]
            chosen = placement(
                [(key, candidate["id"]) for key, candidate, _ in ranked],
                gpu=request["gpu"] > 0,
                project=bool(results),
            )
            if head is None:
                note = f"{policy.name}: started on {node['id']} ({entry.why}); {chosen}"
            else:
                if node["id"] != reservation.node_id:
                    safe = f"the reserved job waits for {reservation.node_id}"
                elif now + limit <= reservation.start:
                    safe = "its time limit ends before the reservation"
                else:
                    safe = "it leaves enough room for the reserved job"
                note = (
                    f"{policy.name}: backfilled on {node['id']} ahead of job "
                    f"{short(head['id'])} reserved on {reservation.node_id} at "
                    f"{clock_text(reservation.start)}; {safe} ({entry.why}); {chosen}"
                )
            source = next((row for row in results if row.node_id == node["id"]), None)
            if source is not None:
                note += f"; starts from the files of job {short(source.job_id)}"
            elif results:
                note += f"; no node with the project's files ({', '.join(rank)}) can run it now"
            self._dispatch(
                connection,
                job,
                spec,
                node["id"],
                gpu_uuids,
                now,
                note,
                source.attempt_id if source is not None else None,
            )
            self._decide(
                "started" if head is None else "backfilled",
                f"{who(job)}: {note}",
                job=job,
                node_id=node["id"],
                detail={
                    "candidates": sorted(candidate["id"] for _, candidate, _ in ranked),
                    "gpu_uuids": gpu_uuids,
                    **({} if head is None else {"ahead_of": head["id"]}),
                },
            )
            held.update(gpu_uuids)
            allocated[node["id"]]["cpu"] += request["cpu"]
            allocated[node["id"]]["memory_mib"] += request["memory_mib"]
            allocated[node["id"]]["jobs"] += 1
            if policy.backfill:
                bookings[node["id"]].append(
                    Booking(
                        now + limit, request["cpu"], request["memory_mib"], frozenset(gpu_uuids)
                    )
                )
        self._publish_queue(self._queue_snapshot(now, explained, reservation, usage))

    def _dispatch(
        self,
        connection,
        job: dict,
        spec: JobSpec,
        node_id: str,
        gpu_uuids: list[str],
        now: float,
        note: str,
        previous_attempt_id: str | None = None,
    ) -> None:
        """Reserve the resources in a new attempt and queue its START command."""
        attempt_id = uuid.uuid4().hex
        connection.execute(
            attempts.insert().values(
                id=attempt_id,
                job_id=job["id"],
                node_id=node_id,
                state="STARTING",
                resources=spec.resources.model_dump(),
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
                    node_id=node_id,
                    released=False,
                )
            )
        payload = {
            "job_id": job["id"],
            "attempt_id": attempt_id,
            "worker_id": node_id,
            "job": spec.model_dump(),
            "gpu_uuids": gpu_uuids,
        }
        if job.get("kind") == SESSION_KIND:
            payload["session"] = True
        if job.get("kind") == MODEL_KIND:
            payload["service"] = self._service_spec(job["service"]).model_dump()
        if previous_attempt_id:
            payload["previous_attempt_id"] = previous_attempt_id
        connection.execute(
            commands.insert().values(
                id=f"start-{attempt_id}",
                node_id=node_id,
                attempt_id=attempt_id,
                kind="START",
                payload=payload,
                acked=False,
                created_at=now,
            )
        )
        self._ring(node_id)
        connection.execute(
            jobs.update()
            .where(jobs.c.id == job["id"])
            .values(
                state="STARTING",
                current_attempt_id=attempt_id,
                reason=f"assigned to {node_id}",
                schedule_note=note,
                updated_at=now,
            )
        )

    def tick(self) -> None:
        with self.db.transaction() as connection:
            expired = (
                connection.execute(
                    select(nodes.c.id).where(
                        nodes.c.status.in_(["ONLINE", RECONCILING]),
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
                text = (
                    f"node {node_id} is offline: no heartbeat for "
                    f"{self.settings.offline_timeout_s:g}s"
                )
                if assigned:
                    names = ", ".join(short(attempt.job_id) for attempt in assigned)
                    text += f"; its jobs ({names}) are UNKNOWN and keep their resources"
                self._decide(
                    "node_offline",
                    text,
                    node_id=node_id,
                    detail={"jobs": [attempt.job_id for attempt in assigned]},
                )
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
            self._expire_leases(connection)
            self._cancel_abandoned_sessions(connection)
            self._manage_models(connection)
            self._schedule(connection)

    def _cancel_abandoned_sessions(self, connection) -> None:
        """Cancel queued sessions whose client stopped asking about them (a CLI killed while
        it waited), so that they do not hold their place in the queue forever. The CLI polls
        four times a second while it waits, so only a client that is gone stays silent."""
        now = self.clock()
        waiting = {}
        for session_id in connection.execute(
            select(jobs.c.id).where(jobs.c.kind == SESSION_KIND, jobs.c.state == "QUEUED")
        ).scalars():
            polled = self.session_polled.get(session_id, now)
            if polled < now - self.settings.session_queue_timeout_s:
                connection.execute(
                    jobs.update()
                    .where(jobs.c.id == session_id)
                    .values(
                        state="CANCELLED",
                        cancel_requested=True,
                        reason="cancelled while queued: its client stopped waiting for it",
                        updated_at=now,
                    )
                )
            else:
                waiting[session_id] = polled
        self.session_polled = waiting

    def _expire_leases(self, connection) -> None:
        """Stop idle sessions whose lease has passed; a session with a command in flight waits.
        A session that became RUNNING without a lease (adopted after a restart) gets one."""
        now = self.clock()
        for row in connection.execute(
            select(jobs).where(jobs.c.kind == SESSION_KIND, jobs.c.state == SESSION_READY_STATE)
        ).all():
            if row.lease_expires_at is None:
                connection.execute(
                    jobs.update()
                    .where(jobs.c.id == row.id)
                    .values(lease_expires_at=now + self.settings.session_lease_s)
                )
            elif row.lease_expires_at < now and row.id not in self.session_busy:
                self._request_stop(connection, row_dict(row), f"session lease expired; {STOP_WAIT}")

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

    def list_results(self, owner: str) -> list[dict]:
        """The owner's kept project directories, newest first per project and node (P4)."""
        with self.db.transaction() as connection:
            rows = connection.execute(
                select(
                    project_results,
                    attempts.c.state.label("outcome"),
                    jobs.c.state.label("job_state"),
                )
                .select_from(
                    project_results.join(
                        attempts, attempts.c.id == project_results.c.attempt_id
                    ).join(jobs, jobs.c.id == project_results.c.job_id)
                )
                .where(project_results.c.owner == owner)
                .order_by(
                    project_results.c.project,
                    project_results.c.node_id,
                    project_results.c.id.desc(),
                )
            ).all()
        result, counts = [], {}
        for row in rows:
            value = row_dict(row)
            del value["id"], value["owner"]
            job_state = value.pop("job_state")
            key = (value["project"], value["node_id"])
            counts[key] = counts.get(key, 0) + 1
            # The oldest of a full set: the next run of the project there deletes it, unless
            # its job waits for a retry (_prune_results).
            value["deleted_by_next_run"] = (
                counts[key] >= self.settings.results_keep and job_state in TERMINAL
            )
            result.append(value)
        return result

    def check_instance(self, node_id: str, instance_id: str) -> None:
        """Raise unless instance_id is the node's current registered instance."""
        with self.db.transaction() as connection:
            self._node(connection, node_id, instance_id)

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
                # Normalized, so a payload stored before a field was added (kept_bytes) still
                # matches its resent event.
                stored = WorkerEvent.model_validate(existing.payload).model_dump()
                if existing.attempt_id != attempt_id or stored != payload:
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
                # The start may arrive after a heartbeat lapse made the job UNKNOWN: the node was
                # offline, or a restarted worker ran an undelivered START.
                lapsed = job["state"] == "UNKNOWN" and attempt["reason"] == EXPIRED_ATTEMPT
                if job["state"] == "STARTING" or lapsed:
                    stopping = job["cancel_requested"] or attempt["preempted_by"]
                    state = "STOPPING" if stopping else "RUNNING"
                    lease = {}
                    if job["kind"] == SESSION_KIND and state == "RUNNING":
                        lease = {"lease_expires_at": self.clock() + self.settings.session_lease_s}
                    elif job["kind"] == MODEL_KIND and state == "RUNNING":
                        # A model reports STARTED only once healthy: it serves from now on.
                        lease = {"lease_expires_at": self._model_lease(job["service"])}
                    connection.execute(
                        jobs.update()
                        .where(jobs.c.id == job["id"])
                        .values(
                            state=state,
                            reason=stop_reason(attempt) if state == "STOPPING" else "executing",
                            updated_at=self.clock(),
                            **lease,
                        )
                    )
                    connection.execute(
                        attempts.update()
                        .where(attempts.c.id == attempt_id)
                        .values(
                            state=state,
                            started_at=attempt["started_at"] or self.clock(),
                            **({"reason": "worker reported start"} if lapsed else {}),
                        )
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
                    kept=event.kept_bytes is not None,
                    size=event.kept_bytes,
                )
                self._schedule(connection)
        return {"accepted": True}

    def cancel(self, job_id: str, owner: str) -> dict:
        with self.db.transaction() as connection:
            job = self._job(connection, job_id, owner)
            if job["state"] not in TERMINAL:
                # A job queued for retry still points at its last, already released attempt.
                if job["state"] == "QUEUED":
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
                    self._prune_results(connection, job)  # A retry's kept directories.
                    self._schedule(connection)
                else:
                    self._request_stop(connection, job, STOP_WAIT)
        return self.get_job(job_id, owner)

    def _request_stop(
        self, connection, job: dict, reason: str, preempted_by: str | None = None
    ) -> None:
        """Durably ask the Worker to stop the job's current attempt (cancel, session end).

        With preempted_by (M11) the job is not cancelled: the attempt records the job it stops
        for, and its confirmed termination queues the job again. Resources stay reserved until
        the attempt's FINISHED event confirms termination.
        """
        attempt = connection.execute(
            select(attempts).where(attempts.c.id == job["current_attempt_id"])
        ).one()
        connection.execute(
            jobs.update()
            .where(jobs.c.id == job["id"])
            .values(
                state="STOPPING",
                reason=reason,
                updated_at=self.clock(),
                **({} if preempted_by else {"cancel_requested": True}),
            )
        )
        connection.execute(
            attempts.update()
            .where(attempts.c.id == attempt.id)
            .values(state="STOPPING", **({"preempted_by": preempted_by} if preempted_by else {}))
        )
        connection.execute(
            commands.update()
            .where(commands.c.attempt_id == attempt.id, commands.c.kind == "START")
            .values(acked=True)
        )
        stop_id = f"stop-{attempt.id}"
        if not connection.execute(select(commands.c.id).where(commands.c.id == stop_id)).first():
            connection.execute(
                commands.insert().values(
                    id=stop_id,
                    node_id=attempt.node_id,
                    attempt_id=attempt.id,
                    kind="STOP",
                    payload={
                        "job_id": job["id"],
                        "attempt_id": attempt.id,
                        "worker_id": attempt.node_id,
                        "job": job["spec"],
                    },
                    acked=False,
                    created_at=self.clock(),
                )
            )
            self._ring(attempt.node_id)

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
            job = self._job(connection, job_id)
            # A model server belongs to no user, so every user may read its logs (read-only).
            if job["kind"] != MODEL_KIND and job["owner"] != owner:
                raise ServiceError(404, "job not found")
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

    # Fast-lane sessions (M9) ---------------------------------------------------------------
    # A session is a job row with kind SESSION whose container is its current attempt, so the
    # ledger, dispatch, STOP, UNKNOWN handling, reconcile and _finish apply to it unchanged.

    def _session_view(self, connection, job: dict, created: bool = False) -> dict:
        """The session as its owner sees it. Every caller is a request from the owner, which
        is the sign of life that keeps a queued session from being cancelled as abandoned."""
        if job["state"] == "QUEUED":
            self.session_polled[job["id"]] = self.clock()
        node_id = None
        if job["current_attempt_id"]:
            node_id = connection.execute(
                select(attempts.c.node_id).where(attempts.c.id == job["current_attempt_id"])
            ).scalar_one_or_none()
        spec = job["spec"]
        return SessionView(
            id=job["id"],
            state=job["state"],
            reason=job["reason"],
            node_id=node_id,
            resources=spec["resources"],
            image=spec["image"],
            datasets=spec["datasets"],
            lease_expires_at=job["lease_expires_at"],
            created_at=job["created_at"],
            mode=self.settings.fast_lane_mode,
            created=created,
        ).model_dump()

    def _user_sessions(self, connection, owner: str, states) -> list[dict]:
        return [
            row_dict(row)
            for row in connection.execute(
                select(jobs)
                .where(jobs.c.owner == owner, jobs.c.kind == SESSION_KIND, jobs.c.state.in_(states))
                .order_by(jobs.c.submission_seq.desc())
            )
        ]

    def open_session(self, owner: str, request: SessionRequest) -> dict:
        """Return the user's live session if it matches the request, or lease a new one."""
        image = request.image or self.settings.default_image
        try:
            spec = JobSpec(
                image=image,
                argv=list(KEEPALIVE_ARGV),
                env=dict(SESSION_ENV),
                resources=request.resources,
                datasets=request.datasets,
                # Used only by backfill bookings, until the session starts and has a lease;
                # the Worker never enforces it on sessions.
                time_limit_s=self.settings.session_lease_s,
                priority=self.settings.default_priority,
                retries_on_node_failure=0,
                # Starts from the project's latest result on its node (6.1 P5); never kept.
                project=request.project,
            )
        except ValidationError as exc:
            raise ServiceError(422, str(exc)) from exc
        with self.db.transaction() as connection:
            for live in self._user_sessions(connection, owner, SESSION_LIVE_STATES):
                current = live["spec"]
                if (
                    current["resources"] == spec.resources.model_dump()
                    and current["image"] == image
                    and sorted(current["datasets"]) == sorted(spec.datasets)
                ):
                    return self._session_view(connection, live)
                resources = current["resources"]
                raise ServiceError(
                    409,
                    f"you already have session {short(live['id'])} (cpu={resources['cpu']:g}, "
                    f"memory_mib={resources['memory_mib']}, gpu={resources['gpu']}); "
                    "close it with `labflux session close`",
                )
            for lost in self._user_sessions(connection, owner, ["UNKNOWN"]):
                # Stops it if its node comes back; its resources stay held until then.
                self._request_stop(connection, lost, f"replaced by a new session; {STOP_WAIT}")
            job_id = self._insert_job(connection, owner, spec, kind=SESSION_KIND)
            self._schedule(connection)
            return self._session_view(connection, self._job(connection, job_id), created=True)

    def _current_session(self, connection, owner: str) -> dict:
        found = self._user_sessions(
            connection, owner, SESSION_LIVE_STATES | {"STOPPING", "UNKNOWN"}
        )
        if not found:
            raise ServiceError(404, "no session")
        return found[0]

    def current_session(self, owner: str) -> dict:
        with self.db.transaction() as connection:
            return self._session_view(connection, self._current_session(connection, owner))

    def close_session(self, owner: str) -> dict:
        with self.db.transaction() as connection:
            job_id = self._current_session(connection, owner)["id"]
        self.cancel(job_id, owner)
        with self.db.transaction() as connection:
            return self._session_view(connection, self._job(connection, job_id))

    def begin_command(self, owner: str, session_id: str) -> tuple[str, str]:
        """Accept a fast-lane command: 404 unless the session is the user's, 409 unless it is
        ready. Renews the lease and returns (attempt_id, node_id)."""
        with self.db.transaction() as connection:
            job = self._job(connection, session_id, owner)
            if job["kind"] != SESSION_KIND:
                raise ServiceError(404, "session not found")
            attempt = None
            if job["current_attempt_id"]:
                attempt = connection.execute(
                    select(attempts).where(attempts.c.id == job["current_attempt_id"])
                ).first()
            if (
                job["state"] != SESSION_READY_STATE
                or job["cancel_requested"]
                or attempt is None
                or attempt.released
            ):
                raise ServiceError(409, f"session {short(session_id)} is {job['state']}")
            connection.execute(
                jobs.update()
                .where(jobs.c.id == session_id)
                .values(lease_expires_at=self.clock() + self.settings.session_lease_s)
            )
            return attempt.id, attempt.node_id

    def end_command(self, session_id: str, stop_reason: str | None = None) -> None:
        """After a command: renew the lease, or close the session (one-shot mode, relay loss)."""
        if stop_reason is None and self.settings.fast_lane_mode == "oneshot":
            stop_reason = "one-shot mode"
        with self.db.transaction() as connection:
            job = self._job(connection, session_id)
            if job["state"] in TERMINAL or job["state"] == "QUEUED":
                return
            if stop_reason is not None:
                if not job["cancel_requested"]:
                    self._request_stop(connection, job, f"{stop_reason}; {STOP_WAIT}")
            elif job["state"] == SESSION_READY_STATE:
                connection.execute(
                    jobs.update()
                    .where(jobs.c.id == session_id)
                    .values(lease_expires_at=self.clock() + self.settings.session_lease_s)
                )

    # Model services (M10) ------------------------------------------------------------------
    # A model service is a job row with kind MODEL, owned by MODEL_OWNER, whose container is the
    # model server; like a session it reuses the ledger, dispatch, STOP, UNKNOWN handling,
    # reconcile and _finish. Its states: QUEUED waits for resources, STARTING is loading (the
    # Worker reports STARTED only once the health check passes), RUNNING serves requests.

    def _model_spec(self, name: str) -> JobSpec:
        config = self.settings.models[name]
        return JobSpec(
            image=config.image,
            argv=config.argv,
            # Model servers write caches under HOME; the workspace is the writable place.
            env={"HOME": "/workspace", **config.env},
            resources=config.resources,
            datasets=config.datasets,
            # Used by backfill bookings until it serves; the Worker enforces startup_timeout_s.
            time_limit_s=config.startup_timeout_s,
            priority=self.settings.default_priority,
            retries_on_node_failure=0,
            # A model server keeps no project directory, starts from no earlier run and gets no
            # placement preference (6.1).
            project=None,
        )

    def _service_spec(self, name: str) -> ServiceSpec:
        config = self.settings.models[name]
        return ServiceSpec(
            name=name,
            port=config.port,
            health_path=config.health_path,
            startup_timeout_s=config.startup_timeout_s,
            shm_mib=config.shm_mib,
            pids_limit=config.pids_limit,
        )

    def _model_changed(self, connection, job: dict) -> bool:
        """Whether a live service no longer matches the configuration: its stored JobSpec or,
        once dispatched, the ServiceSpec its START carried. Priority is left out: interactive
        work ignores it, and a new default_priority must not restart every model."""
        name = job["service"]
        stored = JobSpec.model_validate(job["spec"]).model_dump(exclude={"priority"})
        if stored != self._model_spec(name).model_dump(exclude={"priority"}):
            return True
        if job["state"] == "QUEUED":
            return False
        payload = connection.execute(
            select(commands.c.payload).where(commands.c.id == f"start-{job['current_attempt_id']}")
        ).scalar_one_or_none()
        return (
            payload is not None and payload.get("service") != self._service_spec(name).model_dump()
        )

    def _model_lease(self, name: str | None) -> float:
        """When an idle model is stopped, counted from now; far ahead for a persistent one."""
        config = self.settings.models.get(name or "")
        if config is None or config.mode == "persistent":
            return self.clock() + PERSISTENT_LEASE_S
        return self.clock() + config.idle_stop_s

    def _model_job(self, connection, name: str, live: bool) -> dict | None:
        """The model's live service, or (live=False) its latest one in any state."""
        statement = select(jobs).where(jobs.c.kind == MODEL_KIND, jobs.c.service == name)
        if live:
            statement = statement.where(
                jobs.c.state.in_(MODEL_LIVE_STATES), jobs.c.cancel_requested.is_(False)
            )
        row = connection.execute(statement.order_by(jobs.c.submission_seq.desc())).first()
        return row_dict(row) if row else None

    def _retry_at(self, name: str, last: dict | None) -> float | None:
        """When a model that ended on its own may start again; None if it may start now."""
        if last is None or last["state"] not in MODEL_ENDED:
            return None
        at = last["updated_at"] + self.settings.models[name].retry_after_failure_s
        return at if at > self.clock() else None

    def _start_model(self, connection, name: str) -> dict:
        job_id = self._insert_job(
            connection, MODEL_OWNER, self._model_spec(name), kind=MODEL_KIND, service=name
        )
        return self._job(connection, job_id)

    def _inflight(self, job_id: str) -> int:
        with self.service_lock:
            return self.service_inflight.get(job_id, 0)

    def _model_view(self, connection, name: str, job: dict | None) -> dict:
        config = self.settings.models[name]
        with self.service_lock:
            served, activity = dict(self.service_served), dict(self.service_activity)
        # Counted per service job (every served one has an activity time); the model's earlier
        # services since the Scheduler started count too.
        mine = []
        if activity:
            mine = (
                connection.execute(
                    select(jobs.c.id).where(jobs.c.id.in_(list(activity)), jobs.c.service == name)
                )
                .scalars()
                .all()
            )
        view = {
            "name": name,
            "mode": config.mode,
            "state": "stopped",
            "reason": "",
            "job_id": None,
            "node_id": None,
            "attempt_id": None,
            "inflight": 0,
            "idle_stop_at": None,
            "retry_at": None,
            "resources": config.resources.model_dump(),
            "served": sum(served.get(job_id, 0) for job_id in mine),
            "last_activity_at": max((activity[job_id] for job_id in mine), default=None),
        }
        if job is None:
            return view
        view.update(
            state=MODEL_STATES.get(job["state"], "stopped"),
            reason=job["reason"],
            job_id=job["id"],
            inflight=self._inflight(job["id"]),
            retry_at=self._retry_at(name, job),
        )
        if job["state"] == "RUNNING" and config.mode == "on_demand":
            view["idle_stop_at"] = job["lease_expires_at"]
        if job["current_attempt_id"]:
            view["attempt_id"] = job["current_attempt_id"]
            view["node_id"] = connection.execute(
                select(attempts.c.node_id).where(attempts.c.id == job["current_attempt_id"])
            ).scalar_one_or_none()
        return view

    def ensure_model(self, name: str) -> dict:
        """The model's live service; when there is none, start one, unless the last one ended on
        its own less than retry_after_failure_s ago. One transaction, so concurrent requests
        for a stopped model start a single service (plan 10.4)."""
        if name not in self.settings.models:
            raise ServiceError(404, f"model {name!r} is not configured")
        with self.db.transaction() as connection:
            job = self._model_job(connection, name, live=True)
            if job is None:
                last = self._model_job(connection, name, live=False)
                if self._retry_at(name, last) is not None:
                    return self._model_view(connection, name, last)
                job = self._start_model(connection, name)
                self._schedule(connection)
                job = self._job(connection, job["id"])
            if job["state"] == "QUEUED":
                self.model_polled[job["id"]] = self.clock()
            return self._model_view(connection, name, job)

    def list_models(self) -> list[dict]:
        with self.db.transaction() as connection:
            return [
                self._model_view(
                    connection,
                    name,
                    self._model_job(connection, name, live=True)
                    or self._model_job(connection, name, live=False),
                )
                for name in self.settings.models
            ]

    def begin_request(self, job_id: str) -> None:
        with self.service_lock:
            self.service_inflight[job_id] = self.service_inflight.get(job_id, 0) + 1
            self.service_served[job_id] = self.service_served.get(job_id, 0) + 1
            self.service_activity[job_id] = self.clock()

    def end_request(self, job_id: str) -> None:
        """A relayed request ended (completed, failed or abandoned): the idle time restarts."""
        with self.service_lock:
            count = self.service_inflight.get(job_id, 0) - 1
            if count > 0:
                self.service_inflight[job_id] = count
            else:
                self.service_inflight.pop(job_id, None)
            self.service_activity[job_id] = self.clock()
        with self.db.transaction() as connection:
            row = connection.execute(
                select(jobs.c.service).where(jobs.c.id == job_id, jobs.c.state == "RUNNING")
            ).first()
            if row is not None:
                connection.execute(
                    jobs.update()
                    .where(jobs.c.id == job_id)
                    .values(lease_expires_at=self._model_lease(row.service))
                )

    def _idle_stop(self, job: dict, now: float, waiting: bool) -> str | None:
        """Why an idle model service is stopped now, or None: a RUNNING on-demand model with no
        request in flight, idle for idle_stop_s, or for reclaim_idle_s while other jobs wait
        (waiting). _manage_models stops it; a blocked head job waits for it (M11)."""
        config = self.settings.models.get(job["service"] or "")
        if (
            config is None
            or config.mode != "on_demand"
            or job["state"] != "RUNNING"
            or job["cancel_requested"]
            or job["lease_expires_at"] is None
            or self._inflight(job["id"])
        ):
            return None
        idle = now - (job["lease_expires_at"] - config.idle_stop_s)
        if job["lease_expires_at"] <= now:
            return f"idle for {idle:.0f}s"
        if waiting and idle >= config.reclaim_idle_s:
            return f"idle for {idle:.0f}s while other jobs wait for resources"
        return None

    def _manage_models(self, connection) -> None:
        """Stop idle on-demand models, models removed from the configuration and models whose
        configuration changed; cancel queued on-demand models nobody asks for any more; start
        persistent models that have no live service (plan 10.9)."""
        now = self.clock()
        models = self.settings.models
        # Any queued job, another model's service included, can reclaim an idle model early.
        waiting = (
            connection.execute(select(func.count()).where(jobs.c.state == "QUEUED")).scalar_one()
            > 0
        )
        asked = {}
        for row in connection.execute(
            select(jobs).where(
                jobs.c.kind == MODEL_KIND,
                jobs.c.state.in_(MODEL_LIVE_STATES),
                jobs.c.cancel_requested.is_(False),
            )
        ).all():
            job = row_dict(row)
            config = models.get(job["service"])
            if config is None:
                reason = "model removed from the configuration"
                if job["state"] == "QUEUED":
                    connection.execute(
                        jobs.update()
                        .where(jobs.c.id == job["id"])
                        .values(
                            state="CANCELLED",
                            cancel_requested=True,
                            reason=reason,
                            updated_at=now,
                        )
                    )
                else:
                    self._request_stop(connection, job, f"{reason}; {STOP_WAIT}")
                continue
            if job["state"] == "QUEUED":
                # Requests poll ensure_model for up to cold_wait_s; when they stop, a queued
                # on-demand model would only hold its place and later start for nobody.
                last = self.model_polled.get(job["id"], now)
                if config.mode == "on_demand" and last < now - (config.cold_wait_s + 30):
                    connection.execute(
                        jobs.update()
                        .where(jobs.c.id == job["id"])
                        .values(
                            state="CANCELLED",
                            cancel_requested=True,
                            reason="cancelled while queued: no request is waiting for it",
                            updated_at=now,
                        )
                    )
                    continue
                asked[job["id"]] = last
            if self._model_changed(connection, job):
                if job["state"] == "QUEUED":
                    # Dispatch then sends the new JobSpec together with the new ServiceSpec.
                    connection.execute(
                        jobs.update()
                        .where(jobs.c.id == job["id"])
                        .values(spec=self._model_spec(job["service"]).model_dump())
                    )
                else:
                    # A persistent model's next service is queued below without a backoff: a
                    # stop ends in CANCELLED, which is not a failure.
                    self._request_stop(connection, job, f"model configuration changed; {STOP_WAIT}")
                continue
            if job["state"] != "RUNNING":
                continue
            if job["lease_expires_at"] is None:  # Adopted after a restart without a lease.
                connection.execute(
                    jobs.update()
                    .where(jobs.c.id == job["id"])
                    .values(lease_expires_at=self._model_lease(job["service"]))
                )
                continue
            reason = self._idle_stop(job, now, waiting)
            if reason:
                self._request_stop(connection, job, f"{reason}; {STOP_WAIT}")
        self.model_polled = asked
        for name, config in models.items():
            if (
                config.mode == "persistent"
                and self._model_job(connection, name, live=True) is None
                and self._retry_at(name, self._model_job(connection, name, live=False)) is None
            ):
                self._start_model(connection, name)
