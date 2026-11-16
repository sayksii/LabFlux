"""What every logged-in user may see of everyone's work (M12, decision 1A).

Node allocations and job summaries name owners, kinds, resources and scheduling notes. A job's
argv, env, image and source, its logs and outputs, and cancelling it stay with its owner; so
does any reason that could quote them (public_reason).
"""

from __future__ import annotations

import re

from sqlalchemy import select

from labflux.database import attempt_gpus, attempts, jobs, nodes
from labflux.models import MODEL_KIND
from labflux.scheduler import STOP_WAIT, TERMINAL, Scheduler, row_dict

# Ended jobs in the all-users list: those updated in this window, at most this many (newest).
SUMMARY_WINDOW_S = 24 * 3600.0
SUMMARY_TERMINAL_LIMIT = 200

# Job reasons the Scheduler and Worker build only from fixed text, node names, job IDs and
# counts. Any other reason, such as a Docker error, can quote the job's image or command.
_STOPPED_BY = (
    r"preempted by job \w{8}|session lease expired|replaced by a new session|one-shot mode"
    r"|session ended by the worker|model configuration changed"
    r"|model removed from the configuration|idle for \d+s( while other jobs wait for resources)?"
)
_PUBLIC = (
    "waiting for scheduling",
    "dispatched",
    r"assigned to \S+",
    "executing",
    "no online worker",
    "waiting for matching CPU/RAM/GPU capacity or datasets",
    "cancelled while queued(: its client stopped waiting for it|: no request is waiting for it)?",
    "worker heartbeat expired; (resources remain reserved|termination unconfirmed)",
    "model removed from the configuration",
    rf"(({_STOPPED_BY}); )?{STOP_WAIT}",
    r"preempted by job \w{8}; queued to run again \(preemption \d+ of \d+\)",
    r"container missing after worker restart(; queued for retry \d+ of \d+)?",
    # The Worker's own outcomes.
    "completed",
    "cancelled",
    "cancelled before start",
    "time limit exceeded",
    r"memory limit exceeded \(OOM\)",
    "output upload exceeds server limit",
    "termination could not be confirmed|container identity mismatch|container still running",
)
PUBLIC_REASONS = re.compile(f"({'|'.join(_PUBLIC)})(; log limit reached)?")


def public_reason(reason: str | None, state: str) -> str | None:
    """The reason as a user other than the job's owner sees it: one of PUBLIC_REASONS as it
    is, anything else as the job's state ("failed")."""
    if not reason or PUBLIC_REASONS.fullmatch(reason):
        return reason
    return state.lower()


def _allocations(connection, viewer: str, default_limit: float) -> dict[str, list[dict]]:
    """Each node's unreleased attempts, oldest first."""
    cards: dict[str, list[str]] = {}
    for row in connection.execute(
        select(attempt_gpus.c.attempt_id, attempt_gpus.c.gpu_uuid)
        .where(attempt_gpus.c.released.is_(False))
        .order_by(attempt_gpus.c.gpu_uuid)
    ):
        cards.setdefault(row.attempt_id, []).append(row.gpu_uuid)
    result: dict[str, list[dict]] = {}
    for row in connection.execute(
        select(
            attempts.c.id,
            attempts.c.job_id,
            attempts.c.node_id,
            attempts.c.state.label("attempt_state"),
            attempts.c.resources,
            attempts.c.created_at,
            attempts.c.started_at,
            attempts.c.preempted_by,
            jobs.c.owner,
            jobs.c.kind,
            jobs.c.service,
            jobs.c.state,
            jobs.c.spec,
            jobs.c.lease_expires_at,
            jobs.c.schedule_note,
        )
        .select_from(attempts.join(jobs, jobs.c.id == attempts.c.job_id))
        .where(attempts.c.released.is_(False))
        .order_by(attempts.c.created_at, attempts.c.id)
    ):
        limit = row.spec.get("time_limit_s") or default_limit
        result.setdefault(row.node_id, []).append(
            {
                "job_id": row.job_id,
                "attempt_id": row.id,
                "owner": row.owner,
                "mine": row.owner == viewer,
                "kind": row.kind,
                "service": row.service,
                "state": row.state,
                "attempt_state": row.attempt_state,
                "cpu": row.resources["cpu"],
                "memory_mib": row.resources["memory_mib"],
                "gpu_uuids": cards.get(row.id, []),
                "created_at": row.created_at,
                "started_at": row.started_at,
                # Scheduler._bookings' rule: the lease of a started session or model, else the
                # hard time limit from the start, or from dispatch before the start.
                "ends_at": row.lease_expires_at or (row.started_at or row.created_at) + limit,
                "lease_expires_at": row.lease_expires_at,
                # The dispatch note stays the job's note while this attempt runs.
                "placement": "backfilled" if " backfilled on " in row.schedule_note else "started",
                "preempted_by": row.preempted_by,
            }
        )
    return result


def list_nodes(service: Scheduler, viewer: str) -> list[dict]:
    """Scheduler.list_nodes plus each node's allocations, read in the same transaction so the
    allocations always add up to the allocated totals."""
    with service.db.transaction() as connection:
        allocations = _allocations(connection, viewer, service.settings.job_time_limit_s)
        result = []
        for row in connection.execute(select(nodes).order_by(nodes.c.id)):
            value = row_dict(row)
            value.pop("instance_id")
            value["allocated"] = service._allocations(connection, value["id"])
            value["allocations"] = allocations.get(value["id"], [])
            result.append(value)
        return result


def _summary(row, viewer: str) -> dict:
    """Only these fields: never the spec's argv, env, image or source_id. A model service's
    reason is public already (GET /api/models)."""
    spec = row.spec
    mine = row.owner == viewer
    return {
        "id": row.id,
        "owner": row.owner,
        "mine": mine,
        "kind": row.kind,
        "service": row.service,
        "state": row.state,
        "reason": row.reason
        if mine or row.kind == MODEL_KIND
        else public_reason(row.reason, row.state),
        "schedule_note": row.schedule_note,
        "priority": spec.get("priority"),
        "resources": spec["resources"],
        "time_limit_s": spec.get("time_limit_s"),
        "estimated_runtime_s": spec.get("estimated_runtime_s"),
        "safe_to_rerun": spec.get("safe_to_rerun", False),
        "preemptible": spec.get("preemptible", False),
        "project": spec.get("project"),
        "datasets": spec.get("datasets", []),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "submission_seq": row.submission_seq,
        "current_attempt_id": row.current_attempt_id,
        "node_id": row.node_id,
        "lease_expires_at": row.lease_expires_at,
        "cancel_requested": row.cancel_requested,
    }


def job_summaries(service: Scheduler, viewer: str) -> list[dict]:
    """Every user's jobs in submission order: all unfinished ones, plus the newest
    SUMMARY_TERMINAL_LIMIT of those that ended within SUMMARY_WINDOW_S."""
    statement = select(jobs, attempts.c.node_id).select_from(
        jobs.outerjoin(attempts, attempts.c.id == jobs.c.current_attempt_id)
    )
    since = service.clock() - SUMMARY_WINDOW_S
    with service.db.transaction() as connection:
        rows = connection.execute(statement.where(jobs.c.state.not_in(TERMINAL))).all()
        rows += connection.execute(
            statement.where(jobs.c.state.in_(TERMINAL), jobs.c.updated_at >= since)
            .order_by(jobs.c.updated_at.desc(), jobs.c.submission_seq.desc())
            .limit(SUMMARY_TERMINAL_LIMIT)
        ).all()
    return [_summary(row, viewer) for row in sorted(rows, key=lambda row: row.submission_seq)]
