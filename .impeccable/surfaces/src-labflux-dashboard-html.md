---
version: 1
slug: "src-labflux-dashboard-html"
primary_target: "src/labflux/dashboard.html"
related_targets: []
---

# Dashboard surface brief

Scope: `/dashboard`, single file `src/labflux/dashboard.html`. Mode: Operate.
Audience: course demo (projected) first, lab members at their desks daily. Both must work: desktop density, projected legibility.
Job: see who holds which resources where, which jobs wait and why, and what the Scheduler decided (M12: queue order with scores, the backfill reservation, placements, preemptions, node loss and retries, model reclaims); drill into one of your own jobs' attempts and logs; cancel your own job (confirmed). Every logged-in user sees everyone's job summaries; another user's job opens as a read-only summary.
Constraints: every 3 s read `/api/nodes` (with `allocations`), `/api/jobs`, `/api/jobs?scope=all`, `/api/queue` and `/api/models` in parallel; on demand `/api/whoami`, `/api/jobs/{id}`, `/api/jobs/{id}/logs` and `POST /api/jobs/{id}/cancel` (owner only). Explanations (queue order, scores, decisions, blockers, the reservation, recent decisions) come from `/api/queue` and are rendered, never recomputed in the browser; Chinese text is parsed from the Scheduler's English notes and the raw note stays beside it. Google Fonts allowed with system fallback; reserved vs live usage never merged; one 24-hour zh-TW clock whatever the browser locale; projector mode (18px root) must keep the reservation time and model names whole at 1280x720.
GPU (M7) is the main parallel resource, allocated as whole exclusive cards; GPU 卡 is a third axis option, shown only when a node reports cards, and then the default axis. Do not draw GPU UI when the API reports no GPUs.

## Direction contract

THESIS: Each node is one column of a university course timetable; every reservation is a course block occupying whole resource periods (CPU cores now, GPU cards later). Refuses the category default of summary-number cards over per-node progress bars.

OWN-WORLD: Navy portal header (#1f3a5f family) over a cool off-white sheet; thin ruled grid with period numerals in the left gutter; jobs as soft-tinted course blocks with a darker same-hue keyline, each job's hue persistent everywhere; unavailable capacity as fine diagonal hatching; under 所有人 every block names its owner, under 我的 others' reservations are neutral grey blocks. One workhorse sans (Noto Sans TC) with tabular numerals.

STORY: The visitor sees at once which nodes are full, whose job sits in which cores, and what is queued and why (the Scheduler's own decision: head blocked, reserved, backfilled, waiting for a preempted job to stop); they open a job to read attempts and logs, and can cancel it.

FIRST VIEWPORT: Navy top bar: LabFlux with the policy chip (策略 fcfs | priority | backfill), cluster line (nodes online, CPU / RAM / GPU reserved of total, running, queued), live-refresh status, the viewer's name, 投影模式 and 登出. Left ~55% (~60% projected above 1080px): "節點課表" with resource-axis tabs (CPU 核心 | RAM | GPU 卡, GPU 卡 the default when cards are reported) and the scope toggle (所有人, the default | 我的), node columns side by side, rows = resource periods scaled so the largest node fits; the backfill reservation chip on its node head; SESSION / MODEL / backfill tags on blocks. Under it, "模型服務" (state, in-flight, served, idle or cold-start time) and "誰在用" (reserved now, running / queued, recent usage per user). Right ~45%: "工作" with tabs (排隊順序 first and default, then 全部, 執行中, 完成, 失敗/未知, 已取消) and counts; 排隊順序 is the Scheduler's last pass (position, owner, score breakdown, 排程器的決定) with "最近的排程決定" below it; rows are indexed to blocks. Job detail opens as a side panel replacing the list region, not a modal. At 1080px and below one column: timetable, then the jobs panel (or the open detail), then 模型服務 and 誰在用.

FORM: Course timetable (課表), my candidate 7 of 7, assigned by roll. seed:663326bb99070ce4

Raises: state carried by form too (solid running, dashed queued, struck cancelled) — from emission-line rail; every number labelled reserved vs live — from monochrome canon; persistent job hue across views — from character catalog; table row hover rings its block — from star atlas; finished/cancelled jobs stay, stamped — from ticket wallet.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
