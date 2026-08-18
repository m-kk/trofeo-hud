# Native transcript parser + session-window fallback

_2026-08-18. Closes the two remaining code items from TASKS.md Phase 3. **Done** — see docs/project-status.md._

## Why

- `tokens.py` shells out to `npx ccusage` every 60 s: a Node dependency, a
  cold-start that made the first frame wait up to 90 s, and third-party code
  running in a process with standing Keychain access.
- `activity.py` reads `~/.claude/projects/*/*.jsonl` itself but (a) does not
  dedupe — one API message is written as several JSONL lines with the same
  `message.id`+`requestId` and identical `usage`, ~48% of lines in this
  account's logs — so burn rate and the sparkline double-count; and (b) only
  globs one level deep, missing `…/<session>/subagents/*.jsonl`.
- When the usage endpoint fails (429s, expired token) the session gauge
  freezes on its last value and, once its `resets_at` passes, is simply wrong.

## Design

1. `collectors/transcripts.py` — `TranscriptLog`: one incremental,
   thread-safe reader over `PROJECTS_DIR.rglob("*.jsonl")`. Byte-offset
   resume per file, complete-lines-only, dedupe on `(message.id, requestId)`,
   sliding retention window (8 days). Emits `UsageEvent` with the four token
   classes split, the 5m/1h cache-write split, model, `sessionId`, project.
2. `pricing.py` — Anthropic list prices per model prefix; cache write ×1.25
   (5m) / ×2 (1h), cache read ×0.1. Unknown model → $0, logged once.
3. `TokensCollector` reads the shared log: today + trailing 7 calendar days,
   local time. No subprocess. `ActivityCollector` reads the same log
   (deduped, subagents included, sessions counted by `sessionId`).
4. `LimitsCollector` fallback: on refresh failure / auth expiry, if the
   session gauge is absent or its `resets_at` has passed, estimate the current
   5-hour block from event timestamps (a block starts at the first event after
   the previous block ended). `used_pct` is extrapolated cost-proportionally
   from the last good sample when that sample is in the same block, else
   `None`. Label `Current session (est.)`.

## Out of scope

Field-testing USB replug (needs hands on the cable). Phase 5 stretch screens.
