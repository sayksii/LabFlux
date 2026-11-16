# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **Primary for now: course demo and acceptance.** The instructor, TAs, or reviewers watching the OS course project's final demo (plan M14) on a projector, where the Dashboard is used to show what the scheduler is doing and why.
- **Also: lab members in daily use.** Researchers sharing the lab's machines who, after submitting jobs through the CLI, use the Dashboard to see node status, job progress, and why their jobs wait.
- Priority (confirmed, re-confirmed 2026-11-16): demo first, but it should still behave like a monitoring page people would actually use every day.

## Product Purpose

LabFlux uses a central Scheduler and several Docker Workers to run work across heterogeneous lab machines: batch jobs on CPU, RAM and whole GPUs, fast-lane test sessions, and shared model services behind an OpenAI-compatible inference Gateway. The scheduling policy is configurable (`fcfs`, `priority`, `backfill`), and jobs whose owners marked them preemptible can be stopped and rerun for more important work. See `plan.md`.

The Dashboard's purpose (confirmed 2026-11-16): **the M12 acceptance.** Using only the Dashboard and the CLI, anyone can explain who is using resources right now, why a job is waiting, and what decisions the scheduler made. It shows nodes, jobs, the scheduler's queue in its real order, and the scheduler's recent decisions. It is still not an analytics or control center.

## Positioning

Undecided. The user chose to set this aside for now, so future work should not invent a positioning claim.

## Operating Context

- Jobs are submitted, cancelled, and their logs and outputs fetched mainly through the CLI (`labflux submit/status/logs/fetch/nodes/models/cancel`, `labflux run` and `labflux shell` for the fast lane); see the README.
- The Dashboard is served by the Scheduler at `/dashboard`. It authenticates with a user Bearer Token, kept only in that tab's sessionStorage. Tokens come from the Scheduler's configuration or from `labflux login` with the user's Unix account on a LabFlux node.
- Environments: three physical lab machines with different CPU, RAM and GPUs, each running one Worker (one also runs a persistent shared model service); a local three-VM lab for validation; local Docker for development.
- Visibility (decided 2026-11-16): every logged-in user sees a summary of every user's jobs (owner, kind, state, resources, node, priority, score, scheduling note). The command, environment variables, logs, outputs, and cancellation stay with the job's owner. Model services belong to `@models` and are visible to everyone.
- Scheduling explanations (queue order, scores, reasons a job waits, backfill reservations, recent decisions) come from the Scheduler's own scheduling passes, never recomputed in the browser.
- One Scheduler runs one policy at a time; changing it means editing the configuration and restarting. The Dashboard shows the current policy and never switches it.

## Capabilities and Constraints

- **Implementation approach (decided in `plan.md`):** FastAPI serves HTML and simple JavaScript, with periodic refresh and no separate frontend project. The current implementation is the single file `src/labflux/dashboard.html`.
- **Job kinds:** `BATCH`, `SESSION` (a fast-lane session), `MODEL` (a shared model service).
- **Job states:** `QUEUED`, `DISPATCHED`, `STARTING`, `RUNNING`, `STOPPING`, `SUCCEEDED`, `FAILED`, `CANCELLED`, `UNKNOWN`. An attempt can also end `PREEMPTED`: it was stopped for a preemption and its job queued again.
- **Model service states:** `waiting`, `starting`, `serving`, `stopping`, `failed`, `stopped`; each model is `persistent` or `on_demand`.
- **Node states:** `ONLINE`, `OFFLINE` (no heartbeat for more than 10 seconds), `RECONCILING` (after a Scheduler restart, until its Worker reconciles).
- **Resource semantics:** keep total node capacity, allocated resources, and live usage separate. A reservation is not live load, and the two must not be mixed in the display. GPUs are allocated as whole, exclusive cards.
- **Scheduling:** `fcfs` is strict first come, first served: when the head of the queue cannot run, the jobs behind it wait too. `priority` orders batch jobs by score (priority × 100 + minutes waited − recent usage minutes). `backfill` adds EASY backfilling: the blocked head job gets a reservation from hard time limits, and later jobs start only if they cannot delay it. Interactive work (sessions, model services) goes ahead of batch jobs under every policy. Preemption stops and reruns from the start only jobs marked `preemptible`.
- **Terminology:** UI text is Traditional Chinese; state names, job IDs, and API terms stay in English.
- **Actions (decided 2026-11-16):** the Dashboard can cancel only the viewer's own job, behind a confirmation step; every other action stays in the CLI.
- **Fonts (decided 2026-11-16):** Google Fonts (Noto Sans TC) are allowed, with a system font fallback.
- **Projection (decided 2026-11-16):** the Dashboard must stay legible on a projector; projector mode scales the whole page.

## Evidence on Hand

- Validation records: `docs/verification.md` (through M11, including runs on the three lab machines), `docs/evidence/midterm-smoke.json`, `docs/demo-fast-lane.md`
- Course materials: `Distributed_Job_Scheduler.pdf`, `OS簡報.pptx` (the final demo shows live jobs, a fast-lane test, an on-demand model service, and a worker failure)
- No real usage data, user feedback, or policy comparisons exist yet (M13 is unfinished), so none of them should be fabricated.

## Product Principles

1. **Keep it simple and readable first.** Features are added only as stages are actually delivered; the Dashboard explains what the scheduler did, it does not become an analytics tool.
2. **State must be truthful.** Show what the Scheduler actually records and decided: `UNKNOWN` stays unknown, reservations stay separate from live usage, and explanations come from the Scheduler rather than being guessed in the browser.
3. **Understandable in a demo.** Someone seeing it for the first time should be able to tell at a glance which jobs are running, which are queued, where, and why.
4. **The CLI is the main control interface.** The Dashboard complements the CLI rather than duplicating its features.
