# Plan — remediate part-1 review findings

Addresses the five "will break in use" findings in
[../code-review-findings.md](../code-review-findings.md) §1, plus the §1.2
follow-through that adversarial review showed was needed.

Scope is §1 only. The §2 week-window mismatch, the §3 output issues, and the
`limits[]` merge for per-model weekly caps are deliberately **out of scope** —
they are correctness/feature work, not "this breaks in operation." They stay in
the findings doc's suggested order as items 6–7.

**Constraint from `CLAUDE.md`: strict TDD.** Every step below writes a failing
test first. That is not ceremony here — four of the five findings live in code
with zero test coverage, and two of them (§1.1, §1.2) were mis-diagnosed on
first reading precisely because nothing pinned the behavior. The tests are the
durable part of this work.

No hardware is required for any step: the panel is injected/faked throughout.

---

## Step 0 — Test scaffolding

`tests/` currently holds one file covering the renderer. Add:

- `tests/conftest.py` — fixtures: `tmp_config(text)` writing a `config.toml` to a
  tmp path, and `FakePanel` (a `TrofeoPanel` stand-in with scriptable
  `connect`/`send` behavior and a call log).
- Make `config.load()` accept an optional explicit path (or an injectable
  candidate list). Right now `_CANDIDATES` is module-level and the first entry
  resolves relative to `__file__`, so a test cannot point it at a fixture
  without monkeypatching a private. **This is a prerequisite for Step 2** and
  the only production signature change in Step 0.

Rationale for doing this first: Steps 1 and 2 are both untestable without it,
and writing it once avoids three ad-hoc monkeypatches.

---

## Step 1 — §1.1 Stop reconnecting on soft send failure

**The finding, corrected:** trcc's quirk row for `(0x0416, 0x5302, 0x0407)` sets
`keepalive_stream=True`, which makes `_send_with_recovery` return `False` on
transport errors instead of raising, with the comment *"a close→reopen wedges
the panel until a physical replug (#228), so NEVER reconnect."* `app.py:50-53`
answers `not ok` with exactly that close→reopen.

### Tests first

In `tests/test_app.py`, against `FakePanel`:

1. `send` returns `False` once, then `True` → assert `panel.close()` was
   **never called**, and that the loop kept running and sent again.
2. `send` returns `False` continuously → assert no `close()`, and that the loop
   paces at ~`1/fps` rather than spinning (assert on a fake clock / injected
   sleep, not wall time).
3. `send` raises `DeviceDisconnectedError` → assert `close()` **is** called and
   the reconnect path with backoff runs.
4. `connect` raises repeatedly → assert backoff doubles and caps at 60 s
   (pins the existing behavior, which is correct and currently untested).
5. Repeated soft failures log at intervals, not once per frame.

### Change

In `run_loop`:

- `if not ok:` → increment a soft-failure counter, log at intervals
  (first failure, then every Nth), and **fall through to the pacing sleep**
  without closing. Reset the counter on success.
- Keep `panel.close()` + reconnect **only** for a raised exception, and narrow
  that `except Exception` toward `DeviceDisconnectedError` — noting in a comment
  that for this firmware trcc converts transport errors to `False`, so the
  raising path is for genuine disconnects.
- Do not add backoff to the soft-failure path. The pacing sleep is the correct
  interval; the next keepalive tick is meant to resend.

**Risk:** if a soft failure is ever *persistent* and not recoverable by
resending, the HUD now shows a frozen frame instead of attempting recovery. That
is the trade the driver author explicitly chose (a wedged panel needs a replug
either way), and a frozen-but-recoverable panel beats one that needs physical
intervention. Log loudly enough that the user can tell.

**Verify:** cannot be verified on hardware without a way to induce a soft
failure. The `panel` subcommand streaming mock frames for a few minutes
confirms no regression on the happy path — that is the honest limit of
validation here, and the field-test item already open in `TASKS.md` Phase 4
covers the unplug/replug case.

---

## Step 2 — §1.2 Config parsing must not crash-loop the agent

**Why it matters:** `agent.py` sets `KeepAlive: True` + `ThrottleInterval: 10`,
so any exception out of `load()` is not a single failure but a permanent 10 s
crash-loop, with the traceback in `agent-stderr.log` where nobody looks. The
`config.py` docstring promises the opposite ("the HUD is an appliance").

### Tests first

In `tests/test_config.py`, one case per verified failure mode — all four confirmed
empirically, the last two found only in adversarial review:

| Fixture | Currently raises |
|---|---|
| `fps = "fast"` | `ValueError` |
| `start = "25:00"` | `ValueError` |
| `start = 22:00:00` (TOML native time literal) | `TypeError` |
| `night = "off"` (value, not `[night]` table) | `AttributeError` |
| malformed TOML | already handled |
| absent file | already handled |

Each asserts `load()` returns defaults and logs a warning. Plus: a valid config
round-trips; `fps = 0` and `jpeg_quality = 300` are clamped.

### Change

- Wrap the field-parsing block in `except (ValueError, TypeError, AttributeError)`
  alongside the existing `OSError`/`TOMLDecodeError`, logging and falling back to
  defaults. **Widening to `ValueError` alone is insufficient** — that was my
  original recommendation and it still crash-loops on the last two rows above.
- Clamp `fps` to a sane floor (`fps = 0` currently divides by zero at
  `app.py:54`) and `jpeg_quality` to 1–95.
- Accept a `datetime.time` for `start`/`end` as well as a string, since TOML has
  a native time type and writing `start = 22:00:00` is a reasonable mistake.

---

## Step 3 — §1.4 Pin ccusage

One-line change, largest security reduction per unit of effort: `ccusage@latest`
with `-y` means whatever was most recently published to that npm name executes
unreviewed, in a process with a standing "Always Allow" Keychain grant.

### Tests first

`tests/test_tokens.py`: assert the constructed argv contains a pinned
`ccusage@<exact version>` and no floating tag. A cheap regression test, and the
only thing keeping the pin from drifting back to `@latest`.

Also add, from the captured real output: parse a `daily --json` fixture and
assert today/week totals, the in/out/cache split, and that `session_count` is
preserved from the activity collector (the `mutate` interplay at
`tokens.py:47-49`). This retires the guesswork about ccusage's field names by
freezing a real payload — `period`, not `date`, verified live.

### Change

- Pin an exact version in `_CMD`. Record in a comment *why* it is pinned and
  that bumping it is a reviewed commit.
- Note in the README that ccusage is third-party code executed by the daemon,
  and correct "nothing leaves your machine except the usage query" — with a warm
  npm cache that is usually true, but resolution can hit the registry.

**Follow-through (not this plan):** the native JSONL parser already listed in
`TASKS.md` Phase 3 removes this exposure entirely and drops the Node dependency.
Pinning is the interim fix.

---

## Step 4 — §1.3 Surface auth expiry instead of silent staleness

**The failure:** the Keychain token expires (~1 h horizon observed) and only
Claude Code refreshes it. A daemon meant to run unattended goes stale hours
after the last interactive session — the overnight case the daemon exists for.
Worse, an expired token returns **429 `rate_limit_error`, not 401**, so the log
blames throttling and the README blames a missing Keychain grant. All three
point away from the real cause.

**Explicitly not doing:** implementing token refresh. A second process writing
`Claude Code-credentials` races Claude Code's own rotation and can invalidate
the user's live session. This step is read-only.

### Tests first

`tests/test_limits.py`, with the Keychain read and `urlopen` both faked:

1. `expiresAt` in the past → collector reports an `auth_expired` state and
   **makes no HTTP request** (assert the fake `urlopen` was not called).
2. `expiresAt` in the future, 200 response → gauges populate from the captured
   real payload fixture.
3. HTTP 429 with a live token → `stale`, last-good values kept.
4. `utilization: null` → gauge reads unknown, not `0.0` (this also closes the
   §3 "confident 0%" item, since it is the same line).
5. `render()` with `auth_expired` set → does not crash, and the frame differs
   from the stale rendering.

### Change

- `_access_token()` also returns `expiresAt`; check it before the request.
- Add an `auth_expired: bool` to the `Limits` dataclass (defaulting False, so
  `mock_state` and existing renderer tests are unaffected).
- `layout._limits_zone` renders a distinct "AUTH EXPIRED" treatment instead of
  a stale percentage. Use the existing `CRIT` colour — a frozen gauge that looks
  normal is the actual defect being fixed, so it must be visually obvious at a
  desk glance.
- README: replace the "Limits stale" troubleshooting entry with the real cause
  and the remedy (run Claude Code once to refresh).

Cheap adjacent win while in this file: `subscriptionType` is in the same Keychain
blob, so the plan-tier label ("MAX") costs one extra dict lookup. Optional.

---

## Step 5 — §1.5 Do not forward `Authorization` across hosts

Latent, not active — no redirect is currently returned — but verified in this
venv's CPython: `HTTPRedirectHandler.redirect_request` strips only
`content-length` and `content-type` and copies every other header, with no host
comparison. A 301/302 off `api.anthropic.com` walks the bearer token to a third
party.

### Tests first

`tests/test_limits.py`: build the opener the collector uses and assert a
cross-host redirect is refused rather than followed with the header attached.
(Test the handler directly — no network.)

### Change

Build a module-level `urllib.request.build_opener` with a redirect handler that
refuses cross-host redirects (or drops `Authorization` when the host changes),
and use it instead of the bare `urlopen`. Small enough to keep in `limits.py`.

While in this file, two adjacent items already scoped in the findings doc:
drop the unnecessary `anthropic-beta` header (verified not required) and add
modest backoff on repeated failures instead of retrying every 60 s forever.

---

## Order and rationale

Steps 1 and 2 are the two findings that can leave the user with a dead HUD —
one risks a panel that needs physical replugging, the other a permanent
crash-loop — so they lead. Step 3 is a one-line security fix gated only on
writing its regression test. Step 4 is the largest change (state, renderer, and
README) and the most likely failure in practice, but it degrades gracefully
today, so it follows the cheaper fixes. Step 5 is latent and last.

Steps are independent and individually committable; Step 0 blocks Steps 1–2.

## Definition of done

- [ ] Each step's tests written first and failing for the stated reason before
      the fix lands.
- [ ] `pytest` green, `ruff check` clean.
- [ ] Collector, config, and app-loop paths covered — the §3 test-coverage gap
      substantially closed for §1 code, per `CLAUDE.md`'s 100%-coverage rule.
- [ ] `uv run python -m claude_trofeo_hud panel --seconds 120` shows no
      regression on real hardware.
- [ ] README corrected on three points: ccusage as third-party executed code,
      the "nothing leaves your machine" claim, and the "Limits stale" cause.
- [ ] `TASKS.md` updated; this plan moved to `docs/plans/archive/`.
- [ ] `docs/project-status.md` created/updated per `CLAUDE.md`.

## Known gaps this plan does not close

- **§1.1 cannot be verified against a genuinely wedged panel.** No way to induce
  a soft send failure on demand. The fix follows the driver author's explicit
  instruction, which is the best available evidence, and the tests pin the
  intended control flow — but that is not the same as field-proving recovery.
  `TASKS.md` Phase 4's unplug/replug field test remains open.
- **429-vs-401 for an expired token** was observed after several unauthenticated
  probes, so it could in principle reflect a tripped anonymous bucket rather
  than by-design behavior. Step 4 does not depend on which it is: it checks
  `expiresAt` locally and never has to interpret the 429.
- **`severity` is not adopted** for warn/critical colours. Only `normal` has ever
  been observed and it is absent from Claude Code's own schema, so the 80/95
  thresholds stay until a non-`normal` value is seen in the wild.
