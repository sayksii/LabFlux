"""Replaceable batch scheduling policies, reservations and EASY backfilling.

The Scheduler owns the transaction, the candidate search and dispatch. A policy decides only the
order in which queued jobs are considered and whether a job may start while an earlier job in
that order is blocked. The helpers here are pure functions so they can be tested without SQLite.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from labflux.config import Settings
from labflux.models import MODEL_KIND, JobSpec
from labflux.session_protocol import SESSION_KIND

# Jobs with a lower class rank are always considered first, under every policy: interactive
# work, fast-lane sessions (M9) and model services (M10), goes ahead of every batch job, and
# among itself in submission order.
INTERACTIVE_CLASS = 0
BATCH_CLASS = 1
INTERACTIVE_KINDS = {SESSION_KIND: "session", MODEL_KIND: "model service"}


def class_rank(job: dict) -> int:
    return INTERACTIVE_CLASS if job.get("kind") in INTERACTIVE_KINDS else BATCH_CLASS


@dataclass(frozen=True)
class Booking:
    """Resources one attempt holds on a node until its hard deadline (start + time limit)."""

    end: float
    cpu: float
    memory_mib: int
    gpus: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Reservation:
    """The earliest time and node at which the blocked head job is guaranteed to fit."""

    job_id: str
    node_id: str
    start: float
    request: dict
    cards: frozenset[str]  # cards on node_id that match the head job's GPU filters


@dataclass
class Ranked:
    job: dict
    spec: JobSpec
    key: tuple
    why: str


def short(job_id: str) -> str:
    return job_id[:8]


def clock_text(value: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def dominant_share(resources: dict, capacity: dict) -> float:
    """The largest fraction of cluster CPU, RAM or GPUs that one allocation takes."""
    keys = [key for key in ("cpu", "memory_mib", "gpu") if capacity[key]]
    shares = [resources.get(key, 0) / capacity[key] for key in keys]
    return min(1.0, max(shares, default=0.0))


def fits_at(
    node: dict,
    cards: frozenset[str],
    bookings: list[Booking],
    request: dict,
    at: float,
    max_jobs: int,
) -> bool:
    """Whether request fits on node once every booking that ends by `at` has been released."""
    active = [b for b in bookings if b.end > at]
    busy = frozenset().union(*(b.gpus for b in active))
    capacity = node["capacity"]
    return (
        request["cpu"] <= capacity["cpu"] - sum(b.cpu for b in active) + 1e-9
        and request["memory_mib"] <= capacity["memory_mib"] - sum(b.memory_mib for b in active)
        and len(active) < max_jobs
        and len(cards - busy) >= request["gpu"]
    )


def earliest_start(
    node: dict,
    cards: frozenset[str],
    bookings: list[Booking],
    request: dict,
    now: float,
    max_jobs: int,
) -> float | None:
    """The first booking deadline after which request fits; None if it never fits on node."""
    for at in sorted({now, *(max(now, b.end) for b in bookings)}):
        if fits_at(node, cards, bookings, request, at, max_jobs):
            return at
    return None


class FcfsPolicy:
    """Strict first come, first served: submission order, never bypass a blocked head job."""

    name = "fcfs"
    uses_usage = False
    backfill = False

    def __init__(self, settings: Settings):
        self.settings = settings

    def rank(self, job: dict, spec: JobSpec, now: float, usage: dict) -> tuple[float, str]:
        return 0.0, f"submission #{job['submission_seq']}"

    def order(self, entries: list[tuple[dict, JobSpec]], now: float, usage: dict) -> list[Ranked]:
        ranked = []
        for job, spec in entries:
            score, why = self.rank(job, spec, now, usage)
            rank = class_rank(job)
            if rank == INTERACTIVE_CLASS:
                # Scores do not apply: interactive work keeps its submission order.
                kind = INTERACTIVE_KINDS[job["kind"]]
                score, why = 0.0, f"interactive {kind}, submission #{job['submission_seq']}"
            key = (rank, -score, job["submission_seq"])
            ranked.append(Ranked(job, spec, key, why))
        return sorted(ranked, key=lambda entry: entry.key)


class PriorityPolicy(FcfsPolicy):
    """Score order (priority, waiting-time bonus, recent-usage penalty); strict at the head."""

    name = "priority"
    uses_usage = True

    def rank(self, job: dict, spec: JobSpec, now: float, usage: dict) -> tuple[float, str]:
        s = self.settings
        priority = s.default_priority if spec.priority is None else spec.priority
        waited = max(0.0, now - job["created_at"]) / 60
        used = usage.get(job["owner"], 0.0)
        score = (
            s.score_priority_weight * priority
            + s.score_wait_weight * waited
            - s.score_usage_weight * used
        )
        return score, (
            f"score {score:.0f} = priority {priority}, waited {waited:.0f} min, "
            f"recent usage {used:.0f} min"
        )


class BackfillPolicy(PriorityPolicy):
    """Score order plus EASY backfilling behind a reservation for the blocked head job."""

    name = "backfill"
    backfill = True


POLICIES = {policy.name: policy for policy in (FcfsPolicy, PriorityPolicy, BackfillPolicy)}


def make_policy(settings: Settings) -> FcfsPolicy:
    return POLICIES[settings.scheduling_policy](settings)
