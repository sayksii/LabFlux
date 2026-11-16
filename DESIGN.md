---
name: LabFlux
description: A cluster monitor drawn as a university course timetable; nodes are columns, reservations are course blocks.
colors:
  portal-navy: "#1f3a5f"
  portal-navy-raised: "#2a4b75"
  portal-navy-line: "#36577f"
  portal-navy-ink: "#c9d6ea"
  ink: "#1d2433"
  muted-slate: "#5b6577"
  faint-slate: "#6b7486"
  cool-ground: "#eef1f5"
  sheet: "#ffffff"
  rule: "#e6e9ef"
  rule-strong: "#cfd5df"
  queued-amber: "#9a5b00"
  done-green: "#2e7d4f"
  fail-red: "#b3261e"
  fail-red-deep: "#93201a"
  fail-tint: "#fbeceb"
  unknown-violet: "#7a3e9d"
  off-grey: "#6b7280"
  others-fill: "#e9ecf1"
  others-line: "#c5cad3"
  row-hover: "#f3f6fb"
  console-night: "#17202e"
  console-text: "#dfe6f0"
  console-error: "#ffb4a9"
  live-green: "#6fd39a"
  stale-amber: "#ffb020"
  job-blue: "#dbe7fb"
  job-blue-line: "#7fa6e3"
  job-apricot: "#fde3d3"
  job-apricot-line: "#e9a17a"
  job-mint: "#d9f0e0"
  job-mint-line: "#7cc195"
  job-lilac: "#ebdff8"
  job-lilac-line: "#b493dd"
  job-teal: "#d5efef"
  job-teal-line: "#6fbcbc"
  job-rose: "#f9dde6"
  job-rose-line: "#de8aa5"
  job-olive: "#ebefd2"
  job-olive-line: "#a9b45f"
  job-sand: "#f3e6cf"
  job-sand-line: "#c9a46a"
typography:
  display:
    fontFamily: "Noto Sans TC, PingFang TC, Microsoft JhengHei, system-ui, sans-serif"
    fontSize: "1.47rem"
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: "0.01em"
  headline:
    fontFamily: "Noto Sans TC, PingFang TC, Microsoft JhengHei, system-ui, sans-serif"
    fontSize: "1.2rem"
    fontWeight: 700
    lineHeight: 1.3
  title:
    fontFamily: "Noto Sans TC, PingFang TC, Microsoft JhengHei, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 700
    lineHeight: 1.3
  body:
    fontFamily: "Noto Sans TC, PingFang TC, Microsoft JhengHei, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
    fontFeature: "tnum"
  data:
    fontFamily: "Noto Sans TC, PingFang TC, Microsoft JhengHei, system-ui, sans-serif"
    fontSize: "0.933rem"
    fontWeight: 400
    lineHeight: 1.5
    fontFeature: "tnum"
  note:
    fontFamily: "Noto Sans TC, PingFang TC, Microsoft JhengHei, system-ui, sans-serif"
    fontSize: "0.867rem"
    fontWeight: 400
    lineHeight: 1.35
  label:
    fontFamily: "Noto Sans TC, PingFang TC, Microsoft JhengHei, system-ui, sans-serif"
    fontSize: "0.8rem"
    fontWeight: 500
    lineHeight: 1.3
    fontFeature: "tnum"
  stamp:
    fontFamily: "Noto Sans TC, PingFang TC, Microsoft JhengHei, system-ui, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 700
    lineHeight: 1.4
    letterSpacing: "0.03em"
  mono:
    fontFamily: "ui-monospace, Cascadia Mono, SFMono-Regular, Consolas, monospace"
    fontSize: "0.867rem"
    fontWeight: 400
    lineHeight: 1.5
rounded:
  hairline: "2px"
  stamp: "3px"
  block: "4px"
  control: "6px"
spacing:
  xs: "0.25rem"
  sm: "0.4rem"
  md: "0.8rem"
  lg: "1.1rem"
  gap: "1.33rem"
  page: "1.6rem"
components:
  portal-bar:
    backgroundColor: "{colors.portal-navy}"
    textColor: "{colors.sheet}"
    typography: "{typography.display}"
    padding: "0.8rem 1.6rem"
  panel:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
  button-primary:
    backgroundColor: "{colors.portal-navy}"
    textColor: "{colors.sheet}"
    typography: "{typography.data}"
    rounded: "{rounded.control}"
    padding: "0.4rem 0.9rem"
  button-primary-hover:
    backgroundColor: "{colors.portal-navy-raised}"
  button-secondary:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.ink}"
    typography: "{typography.data}"
    rounded: "{rounded.control}"
    padding: "0.4rem 0.9rem"
  button-secondary-hover:
    backgroundColor: "{colors.cool-ground}"
  button-danger:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.fail-red}"
    typography: "{typography.data}"
    rounded: "{rounded.control}"
    padding: "0.4rem 0.9rem"
  button-danger-hover:
    backgroundColor: "{colors.fail-tint}"
  button-danger-solid:
    backgroundColor: "{colors.fail-red}"
    textColor: "{colors.sheet}"
    rounded: "{rounded.control}"
    padding: "0.4rem 0.9rem"
  button-danger-solid-hover:
    backgroundColor: "{colors.fail-red-deep}"
  button-ghost:
    backgroundColor: "{colors.portal-navy}"
    textColor: "{colors.sheet}"
    typography: "{typography.note}"
    rounded: "{rounded.control}"
    padding: "0.3rem 0.75rem"
  button-ghost-hover:
    backgroundColor: "{colors.portal-navy-raised}"
  button-ghost-pressed:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.portal-navy}"
  segmented-option:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.muted-slate}"
    typography: "{typography.note}"
    padding: "0.3rem 0.8rem"
  segmented-option-pressed:
    backgroundColor: "{colors.portal-navy}"
    textColor: "{colors.sheet}"
  tab:
    textColor: "{colors.muted-slate}"
    typography: "{typography.note}"
    rounded: "{rounded.control}"
    padding: "0.3rem 0.7rem"
  tab-selected:
    backgroundColor: "{colors.portal-navy}"
    textColor: "{colors.sheet}"
  stamp-running:
    backgroundColor: "{colors.portal-navy}"
    textColor: "{colors.sheet}"
    typography: "{typography.stamp}"
    rounded: "{rounded.stamp}"
    padding: "0.1rem 0.45rem"
  stamp-queued:
    textColor: "{colors.queued-amber}"
    typography: "{typography.stamp}"
    rounded: "{rounded.stamp}"
    padding: "0.1rem 0.45rem"
  stamp-succeeded:
    textColor: "{colors.done-green}"
    typography: "{typography.stamp}"
    rounded: "{rounded.stamp}"
    padding: "0.1rem 0.45rem"
  stamp-failed:
    backgroundColor: "{colors.fail-red}"
    textColor: "{colors.sheet}"
    typography: "{typography.stamp}"
    rounded: "{rounded.stamp}"
    padding: "0.1rem 0.45rem"
  stamp-unknown:
    textColor: "{colors.unknown-violet}"
    typography: "{typography.stamp}"
    rounded: "{rounded.stamp}"
    padding: "0.1rem 0.45rem"
  stamp-cancelled:
    textColor: "{colors.off-grey}"
    typography: "{typography.stamp}"
    rounded: "{rounded.stamp}"
    padding: "0.1rem 0.45rem"
  course-block:
    backgroundColor: "{colors.job-blue}"
    textColor: "{colors.ink}"
    rounded: "{rounded.block}"
    padding: "0.3rem 0.55rem"
  course-block-others:
    backgroundColor: "{colors.others-fill}"
    textColor: "{colors.ink}"
    rounded: "{rounded.block}"
    padding: "0.3rem 0.55rem"
  job-table-row-hover:
    backgroundColor: "{colors.row-hover}"
  log-console:
    backgroundColor: "{colors.console-night}"
    textColor: "{colors.console-text}"
    typography: "{typography.mono}"
    rounded: "{rounded.control}"
    padding: "0.8rem 0.9rem"
  input-token:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "0.55rem 0.7rem"
---

# Design System: LabFlux

## Overview

**Creative North Star: "The Course Timetable"**

LabFlux reads like a university course timetable (課表) pinned under a navy campus-portal header. Each node is one column of the sheet; each resource unit (a CPU core, a slice of RAM) is one period row, numbered in the left gutter; every reservation is a course block that occupies whole periods. The visitor reads the cluster the way a student reads a timetable: which columns are full, whose block sits where, what is left empty. The job list beside it is the class roster for the same sheet, opened on the queue in the order the Scheduler last considered it, and hovering a roster row rings its block. Under the timetable, two smaller ruled tables say who holds the sheet right now (誰在用) and what the shared model services are doing (模型服務).

The world is cool, ruled and quiet. A cool off-white ground holds white sheets drawn with thin rules; colour lives almost entirely in two places, the navy chrome and the soft per-job pastel blocks. Everything that is *not* a reservation is drawn as texture instead of colour: diagonal hatching for capacity the system holds back, a fine dot grid for capacity the node simply does not have, and red-tinted hatching for a node that has stopped answering. State is carried by form as well as colour, so a stamp reads correctly in greyscale or on a washed-out projector.

Density is desktop-first and data-dense, with a projector mode that scales the whole rem ramp from 15px to 18px and changes only two proportions: a wider timetable column and no nested scroll region. One workhorse sans (Noto Sans TC) with tabular numerals sets every word and number; UI text is Traditional Chinese, while state names, job IDs and API terms stay in English. Every time on the page reads on one 24-hour clock.

**Key Characteristics:**
- Nodes as timetable columns, resource units as numbered period rows, reservations as course blocks.
- Navy portal header over a cool off-white ground and white ruled sheets.
- One pastel hue per job, derived from its ID (a model service's from its name) and identical wherever the job appears.
- Every hue comes with a name: under 所有人 each block names its owner; under 我的 other users stay anonymous grey.
- The Scheduler's decisions sit beside what they are about: outlined tags on blocks, a dashed amber reservation chip on a node head, a decision sentence on each queue row.
- Absent or withheld capacity drawn as texture (hatching, dot grid), never as a flat colour.
- State stamps that change shape (solid, dashed, dotted, struck through) as well as colour.
- Every number labelled as reserved (已預約) or live (即時), never merged.
- Flat surfaces; the only shadow is a two-ring halo marking the hovered or selected job.

## Colors

A restrained cool-slate world with one structural navy, a small semantic set for job state, and an eight-hue pastel set for named course blocks.

### Primary
- **Portal Navy** (portal-navy): the campus-portal header, pressed segmented options (axis and scope), the selected job tab, primary buttons, the RUNNING stamp and the serving/starting model stamps, the focus outline (white on the navy bar itself), and the hover/selection ring around a course block. It is the only saturated chrome colour.
- **Raised Navy** (portal-navy-raised): hover state of navy controls (primary button, ghost buttons in the header).
- **Navy Keyline** (portal-navy-line): hairline border of the ghost buttons and the policy chip on the navy bar.
- **Portal Mist** (portal-navy-ink): secondary text on the navy bar (cluster line labels, the policy chip's 策略 label, live-refresh status, the viewer icon). Values in the cluster line, the policy name and the viewer's name step up to white at weight 500.

### Secondary: State
- **Queue Amber** (queued-amber): QUEUED and PREEMPTED stamps and the waiting model stamp, always dashed. The backfill reservation chip on a node head and its legend entry use the same dashed amber outline, so a reservation reads as waiting.
- **Done Green** (done-green): SUCCEEDED and ONLINE stamps, outlined.
- **Fault Red** (fail-red): FAILED and OFFLINE stamps and the failed model stamp (filled), danger buttons, error notes. **Deep Fault Red** (fail-red-deep) is its hover; **Fault Blush** (fail-tint) is the background of an offline node's header and of the danger button's hover.
- **Unknown Violet** (unknown-violet): UNKNOWN stamps (dotted), RECONCILING stamps (dashed), the unknown model stamp, and the dotted keyline of a block whose state is unknown.
- **Spent Grey** (off-grey): CANCELLED stamp, struck through; the stopped model stamp, outlined without the strike.
- **Live Green** (live-green) / **Stale Amber** (stale-amber): the small status dot in the header; stale also tints the status text to a light amber.

### Tertiary: Course-block hues
Eight soft fill + darker keyline pairs: **Pale Blue / Cornflower** (job-blue, job-blue-line), **Apricot** (job-apricot, job-apricot-line), **Mint** (job-mint, job-mint-line), **Lilac** (job-lilac, job-lilac-line), **Pale Teal** (job-teal, job-teal-line), **Rose** (job-rose, job-rose-line), **Pale Olive** (job-olive, job-olive-line), **Sand** (job-sand, job-sand-line). A job's pair is chosen by hashing its ID; a model service hashes its service name instead (see The Persistent Hue Rule). The pair is used as the block's fill and 1px keyline, the 0.7rem swatch beside its ID in the roster and the model table, and the swatch in its detail title. Secondary text inside a block is the keyline mixed 40% into ink, and a tag inside a block the keyline mixed 45% into ink, so both stay on-hue but legible. The owner that leads a block's line is plain Timetable Ink at 700, so who holds the block reads before what it holds, even on a washed-out projector.

### Neutral
- **Timetable Ink** (ink): all primary text, including the name line on course blocks and the owner that leads a block's secondary line.
- **Muted Slate** (muted-slate): subtitles, table headers, explanations, decision detail lines, score breakdowns, notes, unselected tabs, tags outside blocks.
- **Faint Slate** (faint-slate): period numerals in the gutter and the unit label above it, queue positions, and the times in the decision feed. Used on white only.
- **Cool Ground** (cool-ground): page background, hover fill of light controls, inline code chips, the raw-note strip under a translated explanation, skeletons.
- **Sheet** (sheet): panels, the timetable, table headers, light buttons.
- **Rule** (rule): period rules, column dividers, row dividers inside a panel.
- **Strong Rule** (rule-strong): panel borders, header underline of the timetable, borders of light controls and inputs, the edge where usable capacity ends.
- **Others Grey** (others-fill, others-line): other users' reservations under the 我的 scope, drawn as anonymous neutral blocks.
- **Roster Hover** (row-hover): hovered or linked roster row.
- **Console Night / Console Text / Console Error** (console-night, console-text, console-error): the job log, the one dark surface on the light sheet; stderr runs in the error tint.

### Named Rules
**The Your-Hue-Only Rule.** Pastel course hues belong to jobs the viewer may name, and only while they hold resources; a hue never appears without a name. Under 我的 that is the viewer's own jobs and the model services: every other reservation is Others Grey, merged per column into one block labelled only 其他使用者. Under 所有人 (the default) every block takes its hue, and its secondary line starts with the owner in ink at 700. A model service's block names no owner: its MODEL tag and service name already say whose it is. A job that holds nothing shows an empty swatch slot, not a hue.

**The Persistent Hue Rule.** A job's hue is a function of its ID and never of its position, state, scope or view. The same pair appears on its block, its roster swatch and its detail title. The one exception is a model service: every restart is a new job, so it hashes its service name, and its block, swatch, model-table row and detail title share that hue across restarts.

**The Navy Is Structure Rule.** Navy marks chrome and the current choice (header, pressed option, selected tab, primary action, running or serving, focus). Do not use it as decoration or for a job's hue.

## Typography

**Display Font:** Noto Sans TC (with PingFang TC, Microsoft JhengHei, system-ui)
**Body Font:** Noto Sans TC, the same family
**Label/Mono Font:** ui-monospace stack (Cascadia Mono, SFMono-Regular, Consolas) for IDs in facts, commands and logs

**Character:** One sober CJK workhorse sans at three weights (400, 500, 700) does everything; hierarchy comes from size steps and weight, never from a second face. Tabular numerals are on at the body, so counts, capacities and timers align in columns.

The root size is 15px (18px in projector mode); every step is in rem so projector mode scales the whole ramp.

### Hierarchy
- **Display** (700, 1.47rem, 1.3, +0.01em): the LabFlux wordmark in the portal bar. The login card uses 1.6rem for the same wordmark.
- **Headline** (700, 1.2rem, 1.3): panel titles (節點課表, 模型服務, 誰在用, 工作) and the job ID in the detail header.
- **Title** (700, 1rem, 1.3): node names in column heads, detail subsections (執行紀錄, 日誌) and the decision feed's heading (最近的排程決定). Course-block names (short job ID or model name) use 700 at 0.933rem.
- **Body** (400, 1rem, 1.5, tabular): base text.
- **Data** (400, 0.933rem, 1.5, tabular): roster and model-table cells, detail facts, buttons, the cluster line.
- **Note** (400, 0.867rem, 1.35): panel subtitles, list explanations (clamped to three lines), queue decision sentences, decision-feed entries, tabs, segmented options, the policy chip and the viewer's name, log status.
- **Label** (500 in table headers and the reservation chip, 400 elsewhere; 0.8rem): table headers, period numerals and queue positions, the score breakdown and the detail line under a queue decision, feed times, legend, node live-usage line, secondary lines inside course blocks, the raw note.
- **Stamp** (700, 0.75rem, +0.03em): state stamps and tags. A stamp's text is the state name exactly as the API reports it: uppercase for jobs, attempts and nodes, lowercase for model services (serving, waiting, starting, stopping, stopped, failed, unknown). Tags (SESSION, MODEL, backfill) use the same face at line-height 1.35.

### Named Rules
**The Labelled Number Rule.** Every resource figure says what it is: 已預約 (reserved) or 即時 (live). Reserved and live values sit on separate lines or in separate phrases and are never summed, averaged or drawn on the same bar.

**The Tabular Rule.** Numerals are tabular everywhere. Units are spelled in the UI's words (核, MiB, GiB) after a space. Times use one 24-hour zh-TW clock whatever the browser's locale: 14:05:09 for clocks, feed times and the reservation start, 14:05 for a block's deadline, 2026/11/16 14:05:09 for dates. Inside a course block an elapsed time or countdown is compact m:ss (h:mm:ss past an hour); everywhere else it is spelled out (13 分 9 秒).

**The Raw Note Rule.** A Chinese sentence that renders an English scheduling note keeps the note beside it: the detail's 排程說明 shows the sentence over the raw note in a Cool Ground strip (label size, Muted Slate, 3px corners), and queue decisions and feed entries carry the English text as their tooltip. A note the page cannot parse is shown raw, never guessed at.

## Layout

The page is a navy bar followed by a two-column grid on the cool ground, with a 1.33rem gap and 1.6rem side padding. The left column (1.2fr; 1.5fr in projector mode above 1080px, so block lines and model names fit at 18px on a 1280px projector) is a stack of panels 1.33rem apart: the timetable, then 模型服務 (only when models are configured), then 誰在用; it scrolls with the page. The right column (minmax 26rem, 1fr) is the jobs panel (工作), which scrolls in its own region (max height 100vh − 13rem) holding the queue table and, on the queue tab, the decision feed below it. In projector mode that region has no height limit and the page scrolls as one, so the decision feed is never squeezed into a nested scroller. Job detail replaces the jobs panel in place, as a panel, not a modal; Escape or the back button returns to the list. The timetable's head carries two segmented groups side by side at its right: the resource axis, then the scope.

The timetable is a CSS grid: a 2.6rem gutter column carrying period numerals, then one column per node at minmax(9.5rem, 1fr) (6.5rem on phones), scrolling horizontally inside its panel when nodes outnumber the width. Row height is computed so the largest node fits the viewport, clamped between 14px and 72px (scaled in projector mode); numerals thin out to every 2nd or 5th period when rows get short. On the RAM axis the period unit is chosen from 512 MiB to 16 GiB so a node never exceeds about 16 periods, and numerals mark boundaries rather than cells. Course blocks inset 0.4rem from their column edges and progressively drop lines as they get shorter: under 62px the third line goes (short); under 44px the block becomes a single row, the name followed by its first secondary line (tight); under 16px only the fill remains (micro). The 62px and 44px thresholds scale with the root size, so projector mode keeps the same lines at the same proportions.

At 1080px and below the grid collapses to one column in the order timetable, jobs panel (or the open detail), 模型服務, 誰在用, so why jobs wait sits right under the timetable; the jobs list loses its own scroll region. At 640px and below the bar wraps, table cells tighten to 0.5rem × 0.55rem, the list's resource column, the queue's 類別 column and the score breakdown hide (the score itself stays), the axis and scope groups lose their right alignment, block insets tighten to 0.25rem, and long node names truncate from the start so the distinguishing suffix stays visible.

Projector mode hides the list's resource column and the queue's 類別 column and tightens jobs-panel and model-table cells to 0.5rem inline padding (the model table's last column keeps a 7rem minimum), so the decision column and the cold-start time stay on screen; the kind tags and the score breakdown's priority term still carry what the hidden column said. Nothing that is the point of a moment truncates on a 1280px projector: the reservation chip wraps, model names and block times fit. Lower-priority block lines (an UNKNOWN block's reason, a long owner with a long card name) may still end in an ellipsis there; their full text is in the tooltip.

Spacing runs on a small rem rhythm: 0.25 / 0.4 / 0.8 / 1.1 / 1.33 / 1.6rem, with panel heads and bodies inset 1.1rem and table cells padded 0.55rem × 0.8rem (0.6rem inline in the jobs panel).

## Elevation & Depth

Flat. Panels are separated from the ground by tone and a 1px strong rule, not by shadow. Depth inside the timetable is expressed by texture layering: the ruled period grid, then hatched or dotted unavailable regions, then course blocks above them.

### Shadow Vocabulary
- **Linked-job ring** (`box-shadow: 0 0 0 2px #ffffff, 0 0 0 4px #1f3a5f`): a course block that is hovered, selected, or linked from a hovered roster row. It is a ring, not a lift, and it raises z-index so the ring overlaps neighbouring blocks.

### Named Rules
**The One Ring Rule.** The linked-job ring is the only box-shadow in the system. Nothing floats; nothing casts an ambient shadow.

**The Texture Means Absent Rule.** Capacity a job cannot use is drawn as texture, never as a flat fill: diagonal grey hatching (135°, 5px bands) for system-reserved capacity, a 9px dot grid with a 2px strong-rule edge for capacity the node does not have, and red-tinted hatching (135°, 6px bands) over the period grid for an offline node.

## Shapes

Corners are small and graded by role: 2px for legend chips and hue swatches, 3px for stamps, tags, the reservation chip, the raw-note strip and inline code chips, 4px for course blocks, 6px for panels, buttons, the policy chip, tabs, segmented groups, the log and inputs. Borders are 1px hairlines throughout. Border *style* is semantic: solid for settled or active (tags are always solid), dashed for waiting or stopping (QUEUED, PREEMPTED and waiting stamps, the reservation chip; STOPPING blocks use a 2px dashed keyline), dotted 2px for unknown, and a strike-through on cancelled. Segmented groups clip their options inside one 6px outline with hairline dividers between options.

## Components

### Portal Bar
Navy band carrying the wordmark with the policy chip, the cluster line, the live-refresh status, the viewer's name and two ghost buttons; the end group wraps on narrow screens.
- **Policy chip:** beside the wordmark, a Navy Keyline outline with 6px corners reading 策略 in Portal Mist, then the configured policy (fcfs, priority or backfill) in white at 500. Display only, with a help cursor: its tooltip states the ordering rule, the score formula (a weight is shown only when it is not 1), that sessions and model services go before batch jobs, and that switching needs a Scheduler restart. Hidden until the first queue response.
- **Cluster line:** a definition list; labels in Portal Mist, values in white at 500 (節點 n / m 上線, CPU 已預約, RAM 已預約, GPU 已預約 when cards are pooled, 執行中, 排隊). Counts cover every user's jobs.
- **Live status:** an 0.5rem dot (Live Green) plus 即時 · time; on failure the dot turns Stale Amber and the text states which time's data is shown.
- **Viewer:** a 16px person outline in Portal Mist and the logged-in name in white at 500.
- **Ghost buttons:** transparent with a Navy Keyline border, white text, 16px inline SVG icon; hover fills Raised Navy; pressed (projector mode on) inverts to a white fill with navy text.

### Buttons
- **Shape:** gently rounded (6px), 1px border, 0.4rem × 0.9rem padding, data-size text.
- **Primary:** navy fill, white text; hover Raised Navy.
- **Secondary:** white with Strong Rule border; hover fills Cool Ground.
- **Danger:** white with Fault Red border and text; hover fills Fault Blush. The confirm step swaps to **Danger Solid** (Fault Red fill, white text, Deep Fault Red on hover) beside a secondary 不要, preceded by a sentence saying what cancellation will do.
- **Disabled:** 55% opacity, not-allowed cursor.
- **Focus:** 2px navy outline offset 2px, shared by every focusable element; on the navy bar the outline is white, so focus never disappears into the bar.

### Segmented Control and Tabs
- **Segmented (resource axis):** white options in a shared 6px outline, muted text; the pressed option fills navy with white text; hover on unpressed options fills Cool Ground. Each resource the API reports becomes one option (see GPU Card Axis for the default).
- **Segmented (scope):** the same control, beside the axis group: 所有人 (the default) and 我的, remembered in the browser. 我的 filters the list, the queue, the tab counts and the decision feed to the viewer's jobs, keeps queue positions as the Scheduler numbered them, and turns other users' reservations into Others Grey.
- **Job tabs:** borderless pills, muted text with the count in a 75% opacity numeral; selected fills navy, white text. The first tab, 排隊順序, is the default and counts the queue entries in scope; then 全部, 執行中, 完成, 失敗/未知, 已取消.

### State Stamps
Small bordered labels (3px corners, 0.75rem bold, the state name as the API reports it). Active states (RUNNING, DISPATCHED, STARTING; serving, starting) and alarms (FAILED, OFFLINE; failed) are filled; others are outlined in their colour. QUEUED, PREEMPTED, STOPPING, RECONCILING, waiting and stopping are dashed, UNKNOWN and unknown are dotted 2px, CANCELLED is struck through, stopped is a plain grey outline. Model-service stamps keep the lowercase names /api/models reports, so a model's state never reads as a job's. Finished jobs keep their stamp in the roster; they are not removed.

### Tags
Small outlined labels saying what kind of work a block is or how the Scheduler placed it: SESSION and MODEL before the name, backfill after it (lowercase, as the policy is named). Stamp face at line-height 1.35, 0.28rem inline padding, 1px solid currentColor, 3px corners, on a white fill so they read on any hue: Muted Slate outside blocks, the block's keyline mixed 45% into ink inside one. They appear on course blocks, in the job cell of the list and the queue, and in the legend.

### Reservation Chip
The backfill head's reservation, in the reserved node's column head under the live line: a dashed Queue Amber outline (1px, 3px corners) around amber label text at 500, 保留給 short ID · time 起, or 保留給 short ID · 資源釋放就開始 once the start time has passed. It wraps rather than truncates, breaking only at spaces so 資源釋放就開始 stays whole, because the start time at its end is the point; its tooltip gives the reserved CPU, RAM and cards and the countdown. At most one per timetable.

### Course Block (signature)
The timetable's unit of meaning: a button positioned absolutely in its node column, its height exactly the periods it reserves. Blocks are drawn from the Scheduler's allocations, oldest first; clicking one opens its detail (a read-only summary when it is not the viewer's).
- **Fill / keyline:** the hue pair of the job (or model service), 1px keyline, 4px corners.
- **Content:** a name line (a SESSION or MODEL tag, the 6-character job ID or the model's name at 700, then a backfill tag when the Scheduler started it ahead of a reservation), then two secondary lines. The first carries the news, because it is the one a tight block keeps: owner · resources (the owner only under 所有人, leading in ink at 700), the resources alone for a model (its MODEL tag says what it is), owner · 閒置到期 m:ss for a running session, or 被 job 搶占 · 停止中 for a block being preempted. The second is compact so it fits a projected column: 執行 m:ss · 時限到 HH:MM; UNKNOWN · 資源保留中 for a job on a lost node (the node head above already says OFFLINE) (the tooltip adds 無法確認容器已停止,所以不會重派); starting · 啟動 m:ss while a model loads; the model's state · 進行中 n; or the resources.
- **States:** STOPPING gets a 2px dashed keyline; UNKNOWN a 2px dotted violet keyline over its own fill; hover/selected/linked gets the linked-job ring.
- **Arrival:** a new block lands with a 0.24s scale-from-top (0.88 to 1) and fade on cubic-bezier(0.16, 1, 0.3, 1); disabled under reduced motion.
- **Others:** under 我的, every reservation the viewer may not name merges into one anonymous Others Grey block per column, after the named blocks, labelled 其他使用者 and the amount; not interactive.

### GPU Card Axis
Shown as a third segmented option, GPU 卡, only when some node reports pooled cards; an API without GPU fields draws nothing GPU-related.
- **Default:** when any node reports pooled cards, the timetable opens on this axis and stays there until the viewer picks another axis; the choice lasts for the page and is not stored. With no cards it opens on CPU 核心.
- **Periods:** one period per pooled card, ordered by GPU index; rows equal the most cards on any node, and nodes with fewer cards show the dot grid below. Cards are whole, so a block is always exactly one period.
- **Blocks:** a named job draws one block per card in its hue (a two-card job is two same-hue blocks): the name line, then owner · model · total VRAM, then elapsed time or state. Cards held by work the viewer may not name are Others Grey blocks naming only the card. Work that holds no card (CPU-only jobs, sessions) does not appear on this axis.
- **Free cards:** labelled in place in label-size muted text: GPU index · model · total VRAM, then a separate 即時 line (VRAM used, utilization).
- **Not assignable:** a card in use outside LabFlux is grey hatching labelled 外部使用中 (the legend swaps 系統保留 for this entry on the GPU axis); a card with no report yet says 尚未回報 without texture; an offline node keeps its red hatching and names its cards without stale readings.
- **Head:** 已預約 x / y 張, then 即時 GPU n · x% per card and the count in external use.

### Node Column Head
Node name (title weight, wraps anywhere) with its ONLINE/OFFLINE stamp, then 已預約 x / y on the active axis, then a muted line of live readings (即時 CPU %, 可用 RAM), then, on the reserved node only, the reservation chip. An offline head turns Fault Blush and replaces live readings with the last heartbeat time.

### Roster Table
The jobs panel (工作), under a subtitle stating the policy's rule. Full-width, collapsed borders, row dividers in Rule, sticky header in muted 500 label text. Rows are fully clickable; hover or a hovered block fills Roster Hover. The list tabs show 工作, 擁有者 (所有人 only), 狀態, 節點, 資源 and 說明. The first cell pairs the hue swatch (only for a named job that holds resources) with the short ID and its tags; the explanation cell wraps and clamps at three lines: a queued job's position and the Scheduler's decision, 被 job 搶占 · 停止中, a running job's elapsed time, or its reason.

### Queue Table
The default tab: the queued jobs in the exact order of the Scheduler's last pass, never re-sorted by the page.
- **#:** the position, right-aligned in Faint Slate label text like the timetable's period numerals; 我的 filters rows but keeps the numbers.
- **工作, 擁有者, 類別:** the job cell as in the list; the owner under 所有人; the kind as 批次 · P5, session or 模型 · name (hidden on phones and in projector mode).
- **分數:** the rounded score over a label-size breakdown, 900 + 等 4 − 用 9, that wraps only between whole terms. The column is absent under fcfs; the breakdown hides on phones.
- **排程器的決定:** a note-size sentence (隊首, 排在 job 後面, 放得下但會延後保留時段, 等 job 停止) over a Muted Slate label-size detail line (what it needs against the best free node, the reservation time, or 互動工作排在批次工作前面); the raw note is the tooltip. A head that preempted others says so: 隊首。搶占 node 的 job,等它停止後開始。 over 選它是因為損失最少 · the blocker (它們 for several victims, grouped by node).
- **Empty:** Scheduler 剛啟動,還沒有完成第一輪排程 before the first pass; otherwise 佇列是空的 (你沒有排隊中的工作 under 我的) and a line saying that jobs that fit are dispatched without queueing.

### Decision Feed
Below the queue table, on the queue tab only: the heading 最近的排程決定 over the latest eight decisions in scope, each a two-column row of a Faint Slate time (label size) and one Chinese sentence (note size); the Scheduler's English sentence is the tooltip. A start names the node choice (… 在 delta 開始,可放 delta、gamma;選 delta:剩下符合的 GPU 最少); a model stop names why (gemma4-31b 閒置 1 分 5 秒,有工作在等資源,提前回收, or 逾時自動釋放 at its idle limit); a sentence the page cannot parse is shown in English. Under 我的 it keeps decisions about the viewer's jobs, node events and preemptions of the viewer's jobs. Empty, a note says the Scheduler keeps only the latest 50, in memory.

### Model Services Panel
模型服務, in the left stack under the timetable, shown only when models are configured. Columns: 模型 (the name with its swatch while it holds resources; a button to its summary when its job is listed), 模式 (常駐 or 自動釋放), 狀態 (a lowercase stamp), 節點, 進行中, 已服務, and 閒置, a muted three-line sentence that follows the state: 處理中 n 個請求; 閒置 time · 常駐不因閒置停止 or · countdown 後釋放; 等待資源 with the blocker; 啟動 elapsed (the 節點 column already names the node); the retry countdown after a failure; 下一個請求時啟動; otherwise the reason in Chinese, such as a stopping model's 閒置 1 分 5 秒,有工作在等資源,提前回收;等待確認容器已停止.

### Who's Using Panel
誰在用, a static table (no hover fill, default cursor) whose subtitle separates 已預約 (now) from 近 60 分占用 (the fairness penalty). One row per user, the viewer suffixed (你), and 模型服務 last in Muted Slate. Columns: 已預約 (cores · RAM · cards, summed from the allocations), 執行 / 排隊, 近 60 分占用 (minutes from the Scheduler; 不計 for model services and — under fcfs, both muted).

### Detail Panel
Replaces the jobs panel. Header holds a bordered back button with a chevron SVG, the hue swatch, the job ID headline and its stamp. Body is a single column with 1.4rem gaps.
- **The viewer's own job:** actions, a two-column facts list (6rem term column), an attempts table at note size whose last column (原因) says why an attempt ended (被 job 搶占, the failure reason), the log console, and a copy line (Cool Ground strip with the CLI command and a 複製 button).
- **Anyone else's job, or a model service:** a read-only summary, the same facts without image and command, no attempts or log, and a muted note in place of the actions: for another user's job, that only the owner sees commands, logs, attempts and outputs, and only the owner can cancel; for a model service, that the Scheduler manages it and `labflux models` shows its state.
- **Facts:** 工作 ID, 擁有者, 類別 (Chinese, then the API kind), 優先權 (noting when the default applies), 分數 with its breakdown while queued, 資源 and GPU, 時限, 閒置到期 for a running session, 資料集, 專案, 重跑 (safe_to_rerun · preemptible · times preempted), 節點, 提交, 說明, and 排程說明 under The Raw Note Rule. For a backfilled job the 排程說明 says why it cannot delay the reservation (時限在保留時段開始前結束, 剩下的資源仍夠保留的工作用, or 保留的工作等的是 X); every job ID in it is the 6-character form.

### Log Console
Console Night block, Console Text monospace, 6px corners, max 22rem tall, wrapping long lines; stderr in Console Error; auto-sticks to the bottom only when the reader is already there.

### Inputs / Fields
Token input on the login card: white, Strong Rule border, 6px corners, inherited font; focus is the navy outline at 0 offset. The login card sits centred on a full navy ground.

### Legend
A strip under the timetable, label-size muted text, each entry a 1.1rem × 0.75rem chip (2px corners) showing the exact fill, texture or outline it names, or the tag itself: job hues (每個工作一色,模型服務依名稱; under 我的 it begins 你的工作), 其他使用者 (我的 only), SESSION, MODEL, backfill, 保留時段, 可用, 系統保留 (外部使用中 on the GPU axis), 節點沒有這部分, 節點失聯.

## Do's and Don'ts

### Do:
- **Do** give every new resource (GPU cards included) its own axis option and draw its units as whole periods in the same node columns.
- **Do** derive a job's hue from its ID (a model service's from its name) and reuse the same pair on every surface that shows that job.
- **Do** name every hued block: under 所有人 its first secondary line starts with the owner.
- **Do** put a scheduling decision beside what it is about: tags on the block, the reservation chip on the node head, the decision sentence on the queue row, the raw note under its Chinese rendering.
- **Do** draw unavailable capacity as texture: grey hatching for system-reserved, dot grid for not present, red hatching for offline.
- **Do** carry state in border style as well as colour: solid, dashed (waiting/stopping), dotted (unknown), struck (cancelled).
- **Do** label every resource number 已預約 or 即時, and keep the two on separate lines.
- **Do** size everything in rem from the 15px root so projector mode (18px) scales the whole page.
- **Do** keep the linked-job ring as the single link between a roster row and its block.

### Don't:
- **Don't** summarise the cluster as big-number cards or per-node progress bars; capacity is read off the timetable.
- **Don't** show a hue without a name: under 我的 other users' reservations stay anonymous Others Grey; under 所有人 every block names its owner.
- **Don't** renumber queue positions when filtering; they are the Scheduler's order.
- **Don't** merge reserved and live usage into one figure, bar or chart.
- **Don't** add drop or ambient shadows; the linked-job ring is the only shadow.
- **Don't** open job detail in a modal; it replaces the roster panel in place.
- **Don't** introduce a second typeface for headings; hierarchy is size and weight within Noto Sans TC.
- **Don't** translate state names or job IDs; they stay in English as the API reports them.
