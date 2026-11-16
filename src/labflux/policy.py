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


@dataclass(frozen=True)
class Victim:
    """A running preemptible attempt that could be stopped to make room on its node (M11)."""

    attempt_id: str
    job_id: str
    priority: int
    lost_s: float
    cpu: float
    memory_mib: int
    cards: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Release:
    """Resources already on their way back on a node (M11): an attempt stopping for a cancel, a
    session end or a model stop, or an idle model service about to be reclaimed. A blocked head
    job waits for these instead of preempting for them."""

    what: str  # for the head job's schedule note, e.g. "job 1a2b3c4d on beta to stop"
    cpu: float
    memory_mib: int
    cards: frozenset[str] = frozenset()
    job_id: str = ""  # the job whose attempt frees them, for the queue view (M12)


def lost_seconds(started_at: float | None, now: float) -> float:
    """Work a stop now throws away. Without checkpoints that is everything since the attempt
    reported STARTED; a later step can measure from its last checkpoint instead."""
    return 0.0 if started_at is None else max(0.0, now - started_at)


def victim_key(victim: Victim) -> tuple:
    """Least lost work first, then lower priority, then job id."""
    return (victim.lost_s, victim.priority, victim.job_id)


def fits_after_stopping(
    node: dict,
    used: dict,
    held: frozenset[str],
    matching: frozenset[str],
    request: dict,
    stopped: list[Victim],
    max_jobs: int,
    released: tuple[Release, ...] | list[Release] = (),
) -> bool:
    """Whether request fits on node once the stopped attempts, and the released resources, are
    free again.

    used: the CPU, RAM and job count allocated on node now, the stopped attempts and released
    resources included. held: allocated cards. matching: the node's cards that suit the request
    and are not used outside LabFlux, allocated or not.
    """
    capacity = node["capacity"]
    freeing = [*released, *stopped]
    freed = frozenset().union(*(item.cards for item in freeing))
    cpu = capacity["cpu"] - used["cpu"] + sum(item.cpu for item in freeing)
    memory = capacity["memory_mib"] - used["memory_mib"] + sum(i.memory_mib for i in freeing)
    return (
        request["cpu"] <= cpu + 1e-9
        and request["memory_mib"] <= memory
        and used["jobs"] - len(freeing) < max_jobs
        and len(matching - (held - freed)) >= request["gpu"]
    )


def choose_victims(
    node: dict,
    used: dict,
    held: frozenset[str],
    matching: frozenset[str],
    request: dict,
    candidates: list[Victim],
    max_jobs: int,
    released: tuple[Release, ...] | list[Release] = (),
) -> list[Victim] | None:
    """The victims to stop on one node so that request fits there.

    Released resources count as free from the start. Candidates are added in victim_key order
    until the request fits; then every victim that is no longer needed is dropped, trying the
    last added first. None when the request does not fit even with every candidate stopped;
    empty when it already fits, or fits once the released resources are free.
    """

    def fits(stopped: list[Victim]) -> bool:
        return fits_after_stopping(node, used, held, matching, request, stopped, max_jobs, released)

    chosen: list[Victim] = []
    for victim in sorted(candidates, key=victim_key):
        if fits(chosen):
            break
        chosen.append(victim)
    if not fits(chosen):
        return None
    for victim in reversed(chosen.copy()):
        rest = [other for other in chosen if other is not victim]
        if fits(rest):
            chosen = rest
    return chosen


def cheapest_preemption(options: dict[str, list[Victim]]) -> tuple[str, list[Victim]] | None:
    """Among nodes where stopping victims makes room: the smallest total lost work, then the
    fewest victims, then the node id."""
    choices = [(node_id, victims) for node_id, victims in options.items() if victims]
    if not choices:
        return None
    return min(
        choices,
        key=lambda choice: (sum(v.lost_s for v in choice[1]), len(choice[1]), choice[0]),
    )


class FcfsPolicy:
    """Strict first come, first served: submission order, never bypass a blocked head job."""

    name = "fcfs"
    uses_usage = False
    backfill = False
    # Whether a blocked batch job may preempt (M11); a blocked session may under every policy.
    batch_preemption = False

    def __init__(self, settings: Settings):
        self.settings = settings

    def rank(self, job: dict, spec: JobSpec, now: float, usage: dict) -> tuple[float, str]:
        return 0.0, f"submission #{job['submission_seq']}"

    def score_parts(self, job: dict, spec: JobSpec, now: float, usage: dict) -> dict | None:
        """The weighted terms of a batch job's score, score = priority + waited_min -
        usage_min (M12); None when the policy does not score jobs."""
        return None

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

    def before(
        self, first: tuple[dict, JobSpec], second: tuple[dict, JobSpec], now: float, usage: dict
    ) -> bool:
        """Whether this policy, ordering both as queued jobs now, considers first before second."""
        return self.order([first, second], now, usage)[0].job is first[0]


class PriorityPolicy(FcfsPolicy):
    """Score order (priority, waiting-time bonus, recent-usage penalty); strict at the head."""

    name = "priority"
    uses_usage = True
    batch_preemption = True

    def score_parts(self, job: dict, spec: JobSpec, now: float, usage: dict) -> dict:
        s = self.settings
        priority = s.default_priority if spec.priority is None else spec.priority
        return {
            "priority": s.score_priority_weight * priority,
            "waited_min": s.score_wait_weight * (max(0.0, now - job["created_at"]) / 60),
            "usage_min": s.score_usage_weight * usage.get(job["owner"], 0.0),
        }

    def rank(self, job: dict, spec: JobSpec, now: float, usage: dict) -> tuple[float, str]:
        s = self.settings
        priority = s.default_priority if spec.priority is None else spec.priority
        waited = max(0.0, now - job["created_at"]) / 60
        used = usage.get(job["owner"], 0.0)
        parts = self.score_parts(job, spec, now, usage)
        score = parts["priority"] + parts["waited_min"] - parts["usage_min"]
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
