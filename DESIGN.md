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

LabFlux reads like a university course timetable (課表) pinned under a navy campus-portal header. Each node is one column of the sheet; each resource unit (a CPU core, a slice of RAM) is one period row, numbered in the left gutter; every reservation is a course block that occupies whole periods. The visitor reads the cluster the way a student reads a timetable: which columns are full, whose block sits where, what is left empty. The job list beside it is the class roster for the same sheet, and hovering a roster row rings its block.

The world is cool, ruled and quiet. A cool off-white ground holds white sheets drawn with thin rules; colour lives almost entirely in two places, the navy chrome and the soft per-job pastel blocks. Everything that is *not* a reservation is drawn as texture instead of colour: diagonal hatching for capacity the system holds back, a fine dot grid for capacity the node simply does not have, and red-tinted hatching for a node that has stopped answering. State is carried by form as well as colour, so a stamp reads correctly in greyscale or on a washed-out projector.

Density is desktop-first and data-dense, with a projector mode that scales the whole rem ramp from 15px to 18px instead of re-laying anything out. One workhorse sans (Noto Sans TC) with tabular numerals sets every word and number; UI text is Traditional Chinese, while state names, job IDs and API terms stay in English.

**Key Characteristics:**
- Nodes as timetable columns, resource units as numbered period rows, reservations as course blocks.
- Navy portal header over a cool off-white ground and white ruled sheets.
- One pastel hue per job, derived from its ID and identical wherever the job appears.
- Absent or withheld capacity drawn as texture (hatching, dot grid), never as a flat colour.
- State stamps that change shape (solid, dashed, dotted, struck through) as well as colour.
- Every number labelled as reserved (已預約) or live (即時), never merged.
- Flat surfaces; the only shadow is a two-ring halo marking the hovered or selected job.

## Colors

A restrained cool-slate world with one structural navy, a small semantic set for job state, and an eight-hue pastel set reserved for the viewer's own jobs.

### Primary
- **Portal Navy** (portal-navy): the campus-portal header, pressed segmented options, the selected job tab, primary buttons, the RUNNING stamp, the focus outline, and the hover/selection ring around a course block. It is the only saturated chrome colour.
- **Raised Navy** (portal-navy-raised): hover state of navy controls (primary button, ghost buttons in the header).
- **Navy Keyline** (portal-navy-line): hairline border of ghost buttons sitting on the navy bar.
- **Portal Mist** (portal-navy-ink): secondary text on the navy bar (cluster line labels, live-refresh status). Values in the cluster line step up to white at weight 500.

### Secondary: State
- **Queue Amber** (queued-amber): QUEUED stamp, always dashed.
- **Done Green** (done-green): SUCCEEDED and ONLINE stamps, outlined.
- **Fault Red** (fail-red): FAILED and OFFLINE stamps (filled), danger buttons, error notes. **Deep Fault Red** (fail-red-deep) is its hover; **Fault Blush** (fail-tint) is the background of an offline node's header and of the danger button's hover.
- **Unknown Violet** (unknown-violet): UNKNOWN stamps and the dotted keyline of a block whose state is unknown.
- **Spent Grey** (off-grey): CANCELLED stamp, struck through.
- **Live Green** (live-green) / **Stale Amber** (stale-amber): the small status dot in the header; stale also tints the status text to a light amber.

### Tertiary: Course-block hues
Eight soft fill + darker keyline pairs: **Pale Blue / Cornflower** (job-blue, job-blue-line), **Apricot** (job-apricot, job-apricot-line), **Mint** (job-mint, job-mint-line), **Lilac** (job-lilac, job-lilac-line), **Pale Teal** (job-teal, job-teal-line), **Rose** (job-rose, job-rose-line), **Pale Olive** (job-olive, job-olive-line), **Sand** (job-sand, job-sand-line). A job's pair is chosen by hashing its ID and is used as the block's fill and 1px keyline, the 0.7rem swatch beside its ID in the roster, and the swatch in its detail title. Secondary text inside a block is the keyline mixed 40% into ink, so it stays on-hue but legible.

### Neutral
- **Timetable Ink** (ink): all primary text, including text on course blocks.
- **Muted Slate** (muted-slate): subtitles, table headers, explanations, notes, unselected tabs.
- **Faint Slate** (faint-slate): period numerals in the gutter and the unit label above it. Used on white only.
- **Cool Ground** (cool-ground): page background, hover fill of light controls, inline code chips, skeletons.
- **Sheet** (sheet): panels, the timetable, table headers, light buttons.
- **Rule** (rule): period rules, column dividers, row dividers inside a panel.
- **Strong Rule** (rule-strong): panel borders, header underline of the timetable, borders of light controls and inputs, the edge where usable capacity ends.
- **Others Grey** (others-fill, others-line): other users' reservations, drawn as anonymous neutral blocks.
- **Roster Hover** (row-hover): hovered or linked roster row.
- **Console Night / Console Text / Console Error** (console-night, console-text, console-error): the job log, the one dark surface on the light sheet; stderr runs in the error tint.

### Named Rules
**The Your-Hue-Only Rule.** Pastel course hues belong to the viewer's own jobs that currently hold resources. Other users' reservations are always Others Grey and unlabelled beyond 其他使用者; a job that holds nothing shows an empty swatch slot, not a hue.

**The Persistent Hue Rule.** A job's hue is a function of its ID and never of its position, state or view. The same pair appears on its block, its roster swatch and its detail title.

**The Navy Is Structure Rule.** Navy marks chrome and the current choice (header, pressed option, selected tab, primary action, running, focus). Do not use it as decoration or for a job's hue.

## Typography

**Display Font:** Noto Sans TC (with PingFang TC, Microsoft JhengHei, system-ui)
**Body Font:** Noto Sans TC, the same family
**Label/Mono Font:** ui-monospace stack (Cascadia Mono, SFMono-Regular, Consolas) for IDs in facts, commands and logs

**Character:** One sober CJK workhorse sans at three weights (400, 500, 700) does everything; hierarchy comes from size steps and weight, never from a second face. Tabular numerals are on at the body, so counts, capacities and timers align in columns.

The root size is 15px (18px in projector mode); every step is in rem so projector mode scales the whole ramp.

### Hierarchy
- **Display** (700, 1.47rem, 1.3, +0.01em): the LabFlux wordmark in the portal bar. The login card uses 1.6rem for the same wordmark.
- **Headline** (700, 1.2rem, 1.3): panel titles (節點課表, 我的工作) and the job ID in the detail header.
- **Title** (700, 1rem, 1.3): node names in column heads and detail subsections (執行紀錄, 日誌). Course-block job IDs use 700 at 0.933rem.
- **Body** (400, 1rem, 1.5, tabular): base text.
- **Data** (400, 0.933rem, 1.5, tabular): roster cells, detail facts, buttons, the cluster line.
- **Note** (400, 0.867rem, 1.35): panel subtitles, queue explanations (clamped to three lines), tabs, segmented options, log status.
- **Label** (500 in table headers, 400 elsewhere; 0.8rem): table headers, period numerals, legend, node live-usage line, secondary lines inside course blocks.
- **Stamp** (700, 0.75rem, +0.03em): the state stamp. Its text is the English state name exactly as the API reports it.

### Named Rules
**The Labelled Number Rule.** Every resource figure says what it is: 已預約 (reserved) or 即時 (live). Reserved and live values sit on separate lines or in separate phrases and are never summed, averaged or drawn on the same bar.

**The Tabular Rule.** Numerals are tabular everywhere. Units are spelled in the UI's words (核, MiB, GiB) after a space.

## Layout

The page is a navy bar followed by a two-panel grid on the cool ground: the timetable (about 1.2fr) on the left and the roster (minmax 26rem, 1fr) on the right, with a 1.33rem gap and 1.6rem side padding. Above 1080px the timetable panel is sticky at 1rem from the top while the roster scrolls in its own region (max height 100vh − 13rem). Job detail replaces the roster in place, as a panel, not a modal; Escape or the back button returns to the roster.

The timetable is a CSS grid: a 2.6rem gutter column carrying period numerals, then one column per node at minmax(9.5rem, 1fr) (6.5rem on phones), scrolling horizontally inside its panel when nodes outnumber the width. Row height is computed so the largest node fits the viewport, clamped between 14px and 72px (scaled in projector mode); numerals thin out to every 2nd or 5th period when rows get short. On the RAM axis the period unit is chosen from 512 MiB to 16 GiB so a node never exceeds about 16 periods, and numerals mark boundaries rather than cells. Course blocks inset 0.4rem from their column edges and progressively drop secondary lines as they get shorter (short, tight, micro).

At 1080px and below the grid collapses to one column. At 640px and below the bar wraps, the resource column of the roster hides, block insets tighten to 0.25rem, and long node names truncate from the start so the distinguishing suffix stays visible.

Spacing runs on a small rem rhythm: 0.25 / 0.4 / 0.8 / 1.1 / 1.33 / 1.6rem, with panel heads and bodies inset 1.1rem and table cells padded 0.55rem × 0.8rem.

## Elevation & Depth

Flat. Panels are separated from the ground by tone and a 1px strong rule, not by shadow. Depth inside the timetable is expressed by texture layering: the ruled period grid, then hatched or dotted unavailable regions, then course blocks above them.

### Shadow Vocabulary
- **Linked-job ring** (`box-shadow: 0 0 0 2px #ffffff, 0 0 0 4px #1f3a5f`): a course block that is hovered, selected, or linked from a hovered roster row. It is a ring, not a lift, and it raises z-index so the ring overlaps neighbouring blocks.

### Named Rules
**The One Ring Rule.** The linked-job ring is the only box-shadow in the system. Nothing floats; nothing casts an ambient shadow.

**The Texture Means Absent Rule.** Capacity a job cannot use is drawn as texture, never as a flat fill: diagonal grey hatching (135°, 5px bands) for system-reserved capacity, a 9px dot grid with a 2px strong-rule edge for capacity the node does not have, and red-tinted hatching (135°, 6px bands) over the period grid for an offline node.

## Shapes

Corners are small and graded by role: 2px for legend chips and hue swatches, 3px for stamps and inline code chips, 4px for course blocks, 6px for panels, buttons, tabs, segmented groups, the log and inputs. Borders are 1px hairlines throughout. Border *style* is semantic: solid for settled or active, dashed for waiting or stopping (STOPPING blocks use a 2px dashed keyline), dotted 2px for unknown, and a strike-through on cancelled. Segmented groups clip their options inside one 6px outline with hairline dividers between options.

## Components

### Portal Bar
Navy band carrying the wordmark, the cluster line, the live-refresh status and two ghost buttons.
- **Cluster line:** a definition list; labels in Portal Mist, values in white at 500 (節點 n / m 上線, CPU 已預約, RAM 已預約, 執行中, 排隊).
- **Live status:** an 0.5rem dot (Live Green) plus 即時 · time; on failure the dot turns Stale Amber and the text states which time's data is shown.
- **Ghost buttons:** transparent with a Navy Keyline border, white text, 16px inline SVG icon; hover fills Raised Navy; pressed (projector mode on) inverts to a white fill with navy text.

### Buttons
- **Shape:** gently rounded (6px), 1px border, 0.4rem × 0.9rem padding, data-size text.
- **Primary:** navy fill, white text; hover Raised Navy.
- **Secondary:** white with Strong Rule border; hover fills Cool Ground.
- **Danger:** white with Fault Red border and text; hover fills Fault Blush. The confirm step swaps to **Danger Solid** (Fault Red fill, white text, Deep Fault Red on hover) beside a secondary 不要, preceded by a sentence saying what cancellation will do.
- **Disabled:** 55% opacity, not-allowed cursor.
- **Focus:** 2px navy outline offset 2px, shared by every focusable element.

### Segmented Control and Tabs
- **Segmented (resource axis):** white options in a shared 6px outline, muted text; the pressed option fills navy with white text; hover on unpressed options fills Cool Ground. Each resource the API reports becomes one option.
- **Job tabs:** borderless pills, muted text with the count in a 75% opacity numeral; selected fills navy, white text.

### State Stamps
Small bordered labels (3px corners, 0.75rem bold, English state name). Active states (RUNNING, DISPATCHED, STARTING) and alarms (FAILED, OFFLINE) are filled; others are outlined in their colour. QUEUED and STOPPING are dashed, UNKNOWN is dotted 2px, CANCELLED is struck through. Finished jobs keep their stamp in the roster; they are not removed.

### Course Block (signature)
The timetable's unit of meaning: a button positioned absolutely in its node column, its height exactly the periods it reserves.
- **Fill / keyline:** the job's hue pair, 1px keyline, 4px corners.
- **Content:** short job ID (6 characters, 700), then 核 · MiB, then elapsed time or state; lines drop away as the block shortens.
- **States:** STOPPING gets a 2px dashed keyline; UNKNOWN a 2px dotted violet keyline; hover/selected/linked gets the linked-job ring.
- **Arrival:** a new block lands with a 0.24s scale-from-top (0.88 to 1) and fade on cubic-bezier(0.16, 1, 0.3, 1); disabled under reduced motion.
- **Others:** anonymous Others Grey block labelled 其他使用者 and an amount, not interactive.

### Node Column Head
Node name (title weight, wraps anywhere) with its ONLINE/OFFLINE stamp, then 已預約 x / y on the active axis, then a muted line of live readings (即時 CPU %, 可用 RAM). An offline head turns Fault Blush and replaces live readings with the last heartbeat time.

### Roster Table
Full-width, collapsed borders, row dividers in Rule, sticky header in muted 500 label text. Rows are fully clickable; hover or a hovered block fills Roster Hover. The first cell pairs the job's hue swatch with its short ID; the explanation cell wraps and clamps at three lines, naming queue position and the head-of-line blocker for QUEUED jobs.

### Detail Panel
Replaces the roster region. Header holds a bordered back button with a chevron SVG, the hue swatch, the job ID headline and its stamp. Body is a single column with 1.4rem gaps: actions, a two-column facts list (6rem term column), an attempts table at note size, the log console, and a copy line (Cool Ground strip with the CLI command and a 複製 button).

### Log Console
Console Night block, Console Text monospace, 6px corners, max 22rem tall, wrapping long lines; stderr in Console Error; auto-sticks to the bottom only when the reader is already there.

### Inputs / Fields
Token input on the login card: white, Strong Rule border, 6px corners, inherited font; focus is the navy outline at 0 offset. The login card sits centred on a full navy ground.

### Legend
A strip under the timetable, label-size muted text, each entry a 1.1rem × 0.75rem chip (2px corners) showing the exact fill or texture it names.

## Do's and Don'ts

### Do:
- **Do** give every new resource (GPU cards included) its own axis option and draw its units as whole periods in the same node columns.
- **Do** derive a job's hue from its ID and reuse the same pair on every surface that shows that job.
- **Do** draw unavailable capacity as texture: grey hatching for system-reserved, dot grid for not present, red hatching for offline.
- **Do** carry state in border style as well as colour: solid, dashed (waiting/stopping), dotted (unknown), struck (cancelled).
- **Do** label every resource number 已預約 or 即時, and keep the two on separate lines.
- **Do** size everything in rem from the 15px root so projector mode (18px) scales the whole page.
- **Do** keep the linked-job ring as the single link between a roster row and its block.

### Don't:
- **Don't** summarise the cluster as big-number cards or per-node progress bars; capacity is read off the timetable.
- **Don't** give other users' reservations a hue or an identity; they stay Others Grey.
- **Don't** merge reserved and live usage into one figure, bar or chart.
- **Don't** add drop or ambient shadows; the linked-job ring is the only shadow.
- **Don't** open job detail in a modal; it replaces the roster panel in place.
- **Don't** introduce a second typeface for headings; hierarchy is size and weight within Noto Sans TC.
- **Don't** translate state names or job IDs; they stay in English as the API reports them.
