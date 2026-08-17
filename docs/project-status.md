# Project status

_Updated 2026-08-17._

## What this repo is

A desk HUD rendering Claude Code usage to a Thermalright Trofeo Vision 6.86"
LCD (1280×480, USB HID) from macOS. Upstream is
[`christensen143/claude-trofeo-hud`](https://github.com/christensen143/claude-trofeo-hud);
milestones M1–M4 are complete there (pixels on glass → static layout → live
collectors → launchd daemon).

**We have READ access only.** Contributions go through a fork at
`m-kk/claude-trofeo-hud` (git remote `fork`) and PRs against upstream `main`.

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

### Submitted upstream

Four independent PRs, all test-first, all reporting `MERGEABLE`:

- [#3](https://github.com/christensen143/claude-trofeo-hud/pull/3) — don't
  reconnect when the panel declines a frame (risked wedging the panel until a
  physical replug)
- [#4](https://github.com/christensen143/claude-trofeo-hud/pull/4) — config
  falls back to defaults instead of crash-looping the launchd agent
- [#5](https://github.com/christensen143/claude-trofeo-hud/pull/5) — pin
  ccusage instead of executing `@latest` every 60 s
- [#6](https://github.com/christensen143/claude-trofeo-hud/pull/6) — surface
  auth expiry; never forward the OAuth token off-host

Test count went from 5 (renderer only) to 21 on the branch with the widest
coverage. Collectors, config, and the main loop had **zero** tests before this.

## Open, not addressed

Deliberately out of scope for the remediation PRs — these are correctness and
feature work, not operational failures:

1. **Week-window mismatch.** `tokens.py` sums a Monday-anchored calendar week;
   the weekly gauge shows the server's rolling 7-day window. They are rendered
   adjacently and disagree.
2. **§3 output issues:** `jpeg_quality` is documented but hardcoded;
   `progress_bar` overstates values under ~5.5% (the minimum fill is a full
   pill cap); `activity.py`'s hourly bucket list can `IndexError` at an hour
   boundary.
3. **`severity` not adopted** for warn/critical colours — only `normal` has ever
   been observed and it is absent from Claude Code's own schema.

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

- Working branch `explore` holds the three docs commits. Not pushed — no write
  access upstream, and the docs carry account telemetry.
- Four fix branches pushed to `fork`.
- `uv.lock` was already modified before this work began; left untouched and kept
  out of every commit.
- `src/claude_trofeo_hud/__init__.py` is dead `uv init` scaffolding upstream
  (the real package is at the repo root via `module-root = ""`). Not worth a PR
  on its own; fold into the next one.

## Verification gaps

- **No hardware access this session.** Every test runs against a fake panel. The
  #3 fix follows the trcc driver author's explicit instruction and its tests pin
  the control flow, but recovery from a real soft failure is not field-proven —
  there is no way to induce one on demand. `TASKS.md` Phase 4's unplug/replug
  field test is still open and now matters more.
- **#6's collector was never run against the live endpoint** — the account was
  rate-limited (see above). Unit-tested with faked HTTP only.
