# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **Primary for now: course demo and acceptance.** The instructor, TAs, or reviewers watching an OS course project demo, where the Dashboard is used to show what the scheduler is doing.
- **Also: lab members in daily use.** Researchers sharing machines who, after submitting jobs through the CLI, use the Dashboard to see node status and job progress.
- Priority (confirmed): demo first, but it should still behave like a monitoring page people would actually use every day.

## Product Purpose

LabFlux uses a central Scheduler and several Docker Workers to manage CPU/RAM batch jobs across heterogeneous lab machines. GPU, sessions, model services, and other features are planned in later stages; see `plan.md`.

The Dashboard's current purpose (confirmed): **a simple dashboard for monitoring status.** It shows node and job status, and it is not an analytics or control center.

The long-term acceptance target from `plan.md` M12 (not current scope): "using only the Dashboard and the CLI, you can explain who is using resources right now, why a job is waiting, and what decisions the scheduler made."

## Positioning

Undecided. The user chose to set this aside for now, so future work should not invent a positioning claim.

## Operating Context

- Jobs are submitted, cancelled, and their logs and outputs fetched mainly through the CLI (`labflux submit/status/logs/fetch/nodes/cancel`); see the README.
- The Dashboard is served by the Scheduler at `/dashboard` and reads `/api/nodes` and `/api/jobs`. It authenticates with a user Bearer Token, which is kept only in that tab's sessionStorage.
- Validation environment: three local VMs (1/2/4 vCPU, 2/3/4 GiB RAM), each running one Worker, with the Scheduler on the development host.
- Each user sees only their own jobs; `/api/nodes` shows every node.

## Capabilities and Constraints

- **Implementation approach (decided in `plan.md`):** FastAPI serves HTML and simple JavaScript, with periodic refresh and no separate frontend project. The current implementation is the single file `src/labflux/dashboard.html`.
- **Job states:** `QUEUED`, `DISPATCHED`, `STARTING`, `RUNNING`, `STOPPING`, `SUCCEEDED`, `FAILED`, `CANCELLED`, `UNKNOWN`.
- **Node states:** `ONLINE`, `OFFLINE` (no heartbeat for more than 10 seconds).
- **Resource semantics:** keep total node capacity, allocated resources, and live usage separate. A reservation is not live load, and the two must not be mixed in the display.
- **Scheduling:** in the first stage this is strict FCFS. When the head of the queue cannot run, the jobs behind it wait too.
- **Terminology:** UI text is Traditional Chinese; state names, job IDs, and API terms stay in English.
- **Open questions:** whether the Dashboard should offer cancellation or other actions; whether it must avoid external CDNs; whether it must be legible when projected. The user did not answer these, so ask when they come up.

## Evidence on Hand

- Validation records: `docs/verification.md`, `docs/evidence/midterm-smoke.json`
- Course materials: `Distributed_Job_Scheduler.pdf`, `OS簡報.pptx`
- No real usage data, user feedback, or performance comparisons exist yet (M13 is unfinished), so none of them should be fabricated.

## Product Principles

1. **Keep it simple and readable first.** For now the Dashboard monitors status, and features are added only as later stages are actually delivered.
2. **State must be truthful.** Show what the Scheduler actually records: `UNKNOWN` stays unknown, and reservations stay separate from live usage.
3. **Understandable in a demo.** Someone seeing it for the first time should be able to tell at a glance which jobs are running, which are queued, and where.
4. **The CLI is the main control interface.** The Dashboard complements the CLI rather than duplicating its features.
