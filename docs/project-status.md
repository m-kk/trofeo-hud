# Project status

_Updated 2026-08-18._

## What this repo is

A desk HUD rendering Claude Code usage to a Thermalright Trofeo Vision 6.86"
LCD (1280×480, USB HID) from macOS. Now a standalone project at
[`m-kk/trofeo-hud`](https://github.com/m-kk/trofeo-hud) (git remote `origin`;
`main` = the former `explore` branch). It began as a fork of
[`christensen143/claude-trofeo-hud`](https://github.com/christensen143/claude-trofeo-hud)
(remote `upstream`), where milestones M1–M4 were done (pixels on glass →
static layout → live collectors → launchd daemon).

This is now an independent project: it does not track upstream, and changes
are not shaped for upstream mergeability. The old fork
`m-kk/claude-trofeo-hud` (remote `fork`) is kept only while the upstream PRs
below are open; delete it once they resolve.

Renamed 2026-08-18: package `trofeo_hud`, CLI `trofeo-hud`, logs in
`~/Library/Logs/trofeo-hud/`, config in `~/.config/trofeo-hud/` (the old
`claude-trofeo-hud` config dir is still read as a fallback), launchd label
`io.github.m-kk.trofeo-hud` (install/uninstall also retire the old
`com.varlogchris.claude-trofeo-hud` agent). The dead `src/claude_trofeo_hud`
scaffold is gone.

## Open issues (2026-08-18)

No GitHub issues on `m-kk/trofeo-hud`. What is actually open:

- **Field-test USB reconnect** (TASKS.md Phase 4) — unplug/replug the panel
  and confirm the daemon recovers. Never done; the reconnect path and the #3
  soft-failure path are unit-tested only.
- **Live soak of the #6 limits collector.** The daemon was restarted at 15:16
  today onto current `main` (it had been running pre-fix code since 14:58);
  the collector has run against the live endpoint only since then. Check
  `~/Library/Logs/trofeo-hud/hud.log` for 429s / `AUTH EXPIRED` after a day.
- **Upstream PRs #3–#7** are still open at `christensen143/claude-trofeo-hud`.
  Nothing here waits on them; delete `fork` and the local `fix/*` branches
  once they close.
- **Phase 5 stretch** (screen cycling, calendar countdown, theming) —
  unstarted; features, not fixes.

Closed later the same day (see "Native transcripts" below): the JSONL
fallback for the 5-hour window and the native parser that retires ccusage.

Fixed today, outside git: the `.venv` had been carried over from a checkout
at `~/Downloads/display/claude-trofeo-hud`, so every console script's shebang
(`pytest`, `ruff`, `trofeo-hud`) pointed at a path that no longer existed and
`uv run pytest` failed with `ModuleNotFoundError`. `uv sync --reinstall`
rewrote them; 129 tests pass, ruff clean. The launchd agent itself was
unaffected (it invokes `.venv/bin/python3 -m trofeo_hud`).

## Native transcripts + session fallback (2026-08-18)

Plan: [plans/archive/native-transcripts.md](plans/archive/native-transcripts.md).

- **`collectors/transcripts.py`** — one incremental, thread-safe
  `TranscriptLog` over `~/.claude/projects/**/*.jsonl` shared by all three
  collectors. Dedupes on `message.id`+`requestId` (48% of assistant lines in
  this account's logs were repeats — the old `activity.py` double-counted burn
  rate and the sparkline), reads subagent transcripts (the old glob was one
  level deep), and emits `advisor_message` iterations as their own events —
  Claude Code leaves those out of the top-level `usage`, ccusage counts them.
  Validated against ccusage 20.0.20: cache read/write per day match exactly,
  input within 0.02% once advisor iterations are included. First pass over a
  week of logs: 0.17 s; incremental tick: ~30 ms.
- **`pricing.py`** — Anthropic list rates by model prefix (dated in the file);
  cache write ×1.25 / ×2 (5m / 1h), read ×0.1. Unknown model → $0, logged once.
- **`tokens.py`** no longer shells out. Node is not a requirement any more;
  the launchd plist no longer bakes an `npx` path; first-frame wait dropped
  from 90 s to 20 s.
- **`limits.py` fallback** — on refresh failure or auth expiry the session
  gauge is re-estimated from transcripts: block reset from timestamps
  (`estimate_session`, chained blocks, re-anchors after any ≥5 h gap), and
  while the last good sample's window is live its percentage is scaled by
  cost accrued since (`_Sample`). Labelled `Current session (est.)`; a fresh
  sample restores the server's label. Without a log the collector behaves as
  before (tests exercise both).

Tests: 185. New modules at 100% line coverage.

## Current work

A review of the codebase plus an exploration of Anthropic's OAuth usage
endpoint, followed by remediation of everything the review classed as "will
break in use".

### Documents

| File | What it is |
|---|---|
| [code-review-findings.md](code-review-findings.md) | The review. §1 operational failures, §2 assumption verification, §3 smaller issues. Corrections from adversarial review are marked inline, including one finding whose severity and fix were both wrong (§1.1a). |
| [usage-endpoint.md](usage-endpoint.md) | Field reference for `GET /api/oauth/usage`, verified live and cross-checked against Claude Code 2.1.233's own schemas. Contains this account's live utilisation figures and plan tier — **kept out of every upstream PR for that reason.** |
| [plans/archive/review-remediation.md](plans/archive/review-remediation.md) | The remediation plan, with outcomes and PR links. |

### Submitted upstream — and now on our `main` (2026-08-18)

Four independent PRs, all test-first, all reporting `MERGEABLE`. Upstream has
not merged them; each is now cherry-picked onto `m-kk/trofeo-hud` `main`
(#6 was ported by hand onto the rebuilt gauge-row layout):

- [#3](https://github.com/christensen143/claude-trofeo-hud/pull/3) — don't
  reconnect when the panel declines a frame (risked wedging the panel until a
  physical replug); every loop path now paces
- [#4](https://github.com/christensen143/claude-trofeo-hud/pull/4) — config
  falls back to defaults instead of crash-looping the launchd agent; native
  TOML time literals accepted; fps/quality/dim clamped; `preview` mkdirs
- [#5](https://github.com/christensen143/claude-trofeo-hud/pull/5) — pin
  ccusage (`20.0.20`) instead of executing `@latest` every 60 s
- [#6](https://github.com/christensen143/claude-trofeo-hud/pull/6) — `AUTH
  EXPIRED` state from the Keychain `expiresAt` (no request made, no token
  refresh attempted); urllib opener refuses cross-host redirects so the bearer
  token can't be forwarded; null utilization renders `—%`, not `0%`

[#7](https://github.com/christensen143/claude-trofeo-hud/pull/7) (5-minute
poll cadence) was already on `main` as its own commit.

Tests: 129 at that point.

## Review follow-ups — closed 2026-08-18

The items the remediation PRs left open, all now resolved on `main`:

1. **Week-window mismatch** — `tokens.py` now sums the trailing seven
   calendar days and the panel labels it `7 DAYS`, so it sits honestly beside
   the rolling 7-day gauge. Residual: the sum is by local calendar day, so
   the two can still differ by the partial day at the window's start.
2. **`jpeg_quality`** is threaded from `Config` through `run_loop` to
   `TrofeoPanel.send(quality=…)` instead of being hardcoded at 90.
3. **`progress_bar` small fills** are drawn true to size — inside the left cap
   the fill is the circle segment left of the fill edge (`ImageDraw.chord`),
   not a whole cap. 1% no longer reads as ~4%.
4. **`activity.py` hour-boundary `IndexError`** — the bucket list is sized to
   the latest hour seen across `now` *and* the events, so an event stamped in
   the hour that began mid-scan widens the list instead of falling off it.
5. **`severity`** — closed as *won't adopt for now*: only `normal` has ever
   been observed and it is absent from Claude Code's own schema
   (usage-endpoint.md). Colour stays a function of percentage. Revisit only
   after a non-`normal` value is seen in the wild.

Tests after this pass: 75 (new `test_app`, `test_panel`, `test_tokens`, `test_activity`).

## Done in the layout redesign (2026-08-17)

Branch `explore`, pushed to `fork`. Closes four items from the list above:

- **Gauge rows.** Label + value on one line, that row's bar directly beneath,
  laid out in sequence — `gauge_rows()` is the single source of the left
  column's geometry, so an absent window closes up instead of drawing an empty
  bar.
- **Per-model weekly cap** (Fable), read from `limits[]` — the only place it
  appears when `seven_day_opus`/`seven_day_sonnet` are null.
- **Plan tier** from the Keychain: `subscriptionType` + `rateLimitTier`.
- **Pace marker** on every bar: where even-pace usage would be. The 5-hour
  window turned out to be anchored, not rolling (measured — see
  usage-endpoint.md), so the session bar carries one too. Drawn in the more
  legible treatment from upstream PR #1: a white mark standing proud of the
  pill rather than an inset notch. Window length is carried on the gauge by the
  collector, not hardcoded in the renderer as PR #1 does, so a scoped window
  can state its own span and an unknown one renders bare.
- **Activity stale indicator**, and a weekly reset that names the weekday.
- **429 backoff** in `base.py`: exponential from cadence to a 15-minute cap,
  honouring `Retry-After`, reset on success — with one warning line per
  failure instead of a traceback a minute.
- **Limits cadence 60s → 300s.** The endpoint admits ~1 call/2 min, so the
  inherited 60s cadence 429s on every other poll. Measured from 145 failures
  over a 5-hour run; see usage-endpoint.md. **Affects upstream `main`
  identically** — worth a fifth PR.

## Local repo state

- `main` on `origin` (m-kk/trofeo-hud) is the working branch; the local
  checkout still calls it `explore` and tracks `origin/main`. Local `main` is
  kept fast-forwarded to `origin/main` but is not the checked-out branch.
- The five `fix/*` branches exist on `origin` and `fork` while the upstream
  PRs are open.
- `docs/usage-endpoint.md` carries account telemetry — never fold it into an
  upstream PR.

## Verification gaps

- **No hardware access this session.** Every test runs against a fake panel. The
  #3 fix follows the trcc driver author's explicit instruction and its tests pin
  the control flow, but recovery from a real soft failure is not field-proven —
  there is no way to induce one on demand. `TASKS.md` Phase 4's unplug/replug
  field test is still open and now matters more.
- **#6's collector has run live only since 15:16 today** — before that the
  account was rate-limited and the daemon was on pre-fix code. Unit-tested
  with faked HTTP; live behaviour beyond a clean startup is not yet observed.
