---
version: 1
slug: "src-labflux-dashboard-html"
primary_target: "src/labflux/dashboard.html"
related_targets: []
---

# Dashboard surface brief

Scope: `/dashboard`, single file `src/labflux/dashboard.html`. Mode: Operate.
Audience: course demo (projected) first, lab members at their desks daily. Both must work: desktop density, projected legibility.
Job: see which jobs run where, which wait and why; drill into one job's attempts and logs; cancel own job (confirmed).
Constraints: read `/api/nodes`, `/api/jobs`, `/api/jobs/{id}`, `/api/jobs/{id}/logs`, `POST /api/jobs/{id}/cancel`; Google Fonts allowed with system fallback; reserved vs live usage never merged.
Future: GPU (M7) is the main parallel resource, allocated as whole exclusive cards; the timetable's resource axis must extend to GPU slots without redesign. Do not draw GPU UI before the API reports GPUs.

## Direction contract

THESIS: Each node is one column of a university course timetable; every reservation is a course block occupying whole resource periods (CPU cores now, GPU cards later). Refuses the category default of summary-number cards over per-node progress bars.

OWN-WORLD: Navy portal header (#1f3a5f family) over a cool off-white sheet; thin ruled grid with period numerals in the left gutter; jobs as soft-tinted course blocks with a darker same-hue keyline, each job's hue persistent everywhere; unavailable capacity as fine diagonal hatching; others' reservations as neutral grey blocks. One workhorse sans (Noto Sans TC) with tabular numerals.

STORY: The visitor sees at once which nodes are full, whose job sits in which cores, and what is queued and why (FCFS head blocking); they open a job to read attempts and logs, and can cancel it.

FIRST VIEWPORT: Navy top bar: LabFlux, cluster line (nodes online, CPU reserved/total, RAM reserved/total, queued), live-refresh status, token menu. Left ~55%: "節點課表" with resource-axis tabs (CPU 核心 | RAM), node columns side by side, rows = resource periods scaled so the largest node fits. Right ~45%: "我的工作" with state tabs + counts, table rows indexed to blocks. Job detail opens as a side panel replacing the list region, not a modal.

FORM: Course timetable (課表), my candidate 7 of 7, assigned by roll. seed:663326bb99070ce4

Raises: state carried by form too (solid running, dashed queued, struck cancelled) — from emission-line rail; every number labelled reserved vs live — from monochrome canon; persistent job hue across views — from character catalog; table row hover rings its block — from star atlas; finished/cancelled jobs stay, stamped — from ticket wallet.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
