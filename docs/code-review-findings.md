# Code review — 2026-08-17

Reviewed the whole package (~1,230 lines) plus `spike/`. Baseline is green:
5/5 tests pass, `ruff check` clean.

Findings are ordered by whether they will actually bite. Everything is either
reproduced, measured, or read directly out of the relevant source; assumptions
I could not settle are marked as such.

---

## 1. Will break in use

### 1.1 On send failure, `app.py` reconnects — the one thing the driver forbids for this firmware

> **Revised after adversarial review.** My first version of this finding called
> it a CPU-burning hot loop that shreds its own logs. That severity was wrong,
> and the fix I proposed (add backoff to the send path) would have preserved the
> actual bug. Corrected reasoning below; the original claim is retained in §1.1a
> so the error is legible.

The quirk row for our exact fingerprint `(0x0416, 0x5302, 0x0407)` sets
`keepalive_stream=True` (`trcc/core/models.py:360-368`). That flag changes
trcc's send contract, in `trcc/core/ports.py:337-352`:

```python
except Exception as e:
    if attempt == 0 and not self._quirks.keepalive_stream:
        ...  # reconnect and retry
    if self._quirks.keepalive_stream:
        # Single-session firmware: a close→reopen wedges the panel
        # until a physical replug (#228), so NEVER reconnect — soft-
        # fail and let the next keepalive tick resend the frame.
        log.debug(...)
        return False
```

Two consequences the review originally missed:

1. **`app.py:46`'s `except Exception` around `send` is effectively dead code.**
   For this panel trcc converts transport errors into `return False` and never
   raises them. The live failure path is `if not ok` at `app.py:50`.
2. **`app.py:50-53` answers `not ok` with `panel.close(); continue`** — a full
   teardown and re-handshake. That is precisely the close→reopen the driver
   says "wedges the panel until a physical replug (#228)."

So the daemon's response to a transient soft failure is the action most likely
to turn it into a hard one. Each attempt also costs a **≥3 s blackout**:
`_connect_streaming_firmware` opens with an unconditional
`time.sleep(_QUIRK_BOOT_SETTLE_S)` where `_QUIRK_BOOT_SETTLE_S = 3.0`
(`trcc/adapters/device/hid_lcd.py:57,161`). The README's "Unplug/replug is
handled automatically with backoff" is the opposite of what this firmware wants.

**Fix:** on `not ok`, skip the frame and fall through to the normal pacing
sleep — do not close. Let the next keepalive tick resend, which is what trcc's
comment instructs. Track consecutive soft failures and log at intervals rather
than per frame. Reserve `panel.close()` + reconnect for a raised
`DeviceDisconnectedError` (genuine unplug), not for a `False` return.

#### 1.1a What I originally claimed, and why it was wrong

I claimed that a present-but-wedged panel produces a sleepless loop —
connect → render → send fails → close → connect — burning a core and churning
through all four rotating log files in seconds, erasing the evidence.

The control-flow reading was accurate: there genuinely is no sleep on either
send-failure path, `close()` sets `_dev = None` so the next pass re-enters
`connect()`, and `backoff` is re-reset to 1.0 on every successful connect
(`app.py:34`). What I never traced was `connect()` itself. The 3.0 s boot-settle
sleep hard-caps the loop at roughly one iteration per 3 seconds, so there is no
CPU burn and, at a few log lines per cycle, filling 4 MB of logs takes hours,
not seconds.

The trigger was also narrower than I implied: "connect keeps succeeding while
send keeps failing" requires a panel that answers a `DA DB DC DD` handshake but
rejects frames. Silence instead falls through to `_handshake_retry` and finally
`HandshakeError`, which *does* land in the backoff branch.

Lesson worth keeping: I reasoned about the app's loop without reading the
driver it calls into, and got both the severity and the remedy wrong.

### 1.2 A malformed `config.toml` crash-loops the launchd agent every 10 s

`config.py:48-69` catches only `OSError` and `TOMLDecodeError`. The conversions
below that are unguarded:

```python
cfg.fps = float(raw.get("fps", cfg.fps))                       # ValueError
cfg.jpeg_quality = int(raw.get("jpeg_quality", cfg.jpeg_quality))  # ValueError
start = dtime.fromisoformat(n.get("start", "00:00"))            # ValueError
```

So `start = "25:00"`, `fps = "fast"`, or a `[night]` table typo raises out of
`load()` — which `__main__.py:42` calls before anything else. This directly
contradicts the module docstring: *"a broken file logs and uses defaults rather
than refusing to start (the HUD is an appliance)."*

The sharp end is `agent.py:32-33`: `KeepAlive: True` with
`ThrottleInterval: 10`. A typo in `config.toml` doesn't fail once — it
crash-loops the agent **every 10 seconds, indefinitely**, with the traceback
going to `agent-stderr.log` where nobody is looking.

Two further escapes, found in adversarial review and verified empirically — they
matter because they change the *kind* of fix needed, not just its degree:

| `config.toml` content | Raises |
|---|---|
| `fps = "fast"` | `ValueError: could not convert string to float` |
| `start = "25:00"` | `ValueError: hour must be in 0..23` |
| `start = 22:00:00` (TOML **native time literal** — a plausible thing to write) | `TypeError: fromisoformat: argument must be str` |
| `night = "off"` (as a value rather than a `[night]` table — an easy confusion, since `mode` takes `"off"`) | `AttributeError: 'str' object has no attribute 'get'` |

Fix: widening the `except` to `ValueError` — my original recommendation — is
**insufficient**; it still crash-loops on the last two. Catch
`(ValueError, TypeError, AttributeError)` around the whole parse block, or
validate per field. Clamp `fps` and `jpeg_quality` to sane ranges while you're
there: `jpeg_quality` above 95 is counterproductive in Pillow, and `fps = 0`
divides by zero at `app.py:54`.

### 1.3 The OAuth token expires and nobody refreshes it — by design, unattended

`limits.py:27-32` reads the token fresh from the Keychain each refresh, on the
documented assumption that *"Claude Code rotates it; the Keychain always has the
current one."* That assumption holds **only while Claude Code is running.**

Measured: `expiresAt` was ~56 minutes in the future when sampled. The Keychain
item also carries a `refreshToken`, and Claude Code's own `fetchUtilization`
calls the endpoint with `refreshOAuth: true` and logs
`"401 → refresh → retry succeeded"`. The HUD has no refresh path.

So a launchd agent that is *supposed* to run unattended goes permanently stale
some hours after the user's last interactive Claude Code session — exactly the
overnight/weekend case the daemon exists for. The two limit gauges freeze at
their last-good values behind a small "(stale)" label.

It compounds with §1.4 of the endpoint doc: an expired token returns **429
`rate_limit_error`, not 401**, so the log entry blames rate limiting.

**Do not implement token refresh.** A second process writing
`Claude Code-credentials` races Claude Code's own rotation and can invalidate
the user's live session. The right fix is cheap and read-only: compare
`expiresAt` against now *before* the request and render a distinct
"AUTH EXPIRED" state instead of a stale percentage. Also update the README
troubleshooting entry, which currently offers only "Keychain access not
granted, or you're logged out."

### 1.4 `npx -y ccusage@latest` runs unpinned third-party code every 60 s

`collectors/tokens.py:20` — `["npx", "-y", "ccusage@latest", …]`, on a 60-second
cadence, in a process that reads the OAuth access token from the Keychain every
60 seconds, under a launchd agent the README tells the user to grant
**"Always Allow"** Keychain access. (To be precise about the blast radius: the
token is fetched transiently by the *limits* collector rather than held for the
process lifetime. The standing Keychain grant is the real exposure — any code
running in this process can re-read it on demand.)

`-y` suppresses the install confirmation and `@latest` never pins a version, so
whenever npm's cached dist-tag metadata does refresh, the process
auto-downloads and executes whatever was most recently published to that npm
name — with no review step, no lockfile, and no integrity pin. The blast radius
is the standing Keychain grant.

**Correction to my initial read:** I assumed `@latest` re-contacts the registry
on every invocation. It does not — `npx --offline -y ccusage@latest daily
--json` succeeded (exit 0), so a warm cache serves the run with no network.
Resolution hits the registry only when the cached metadata goes stale. That
softens the *egress* claim but not the supply-chain one, and it means the
README's "nothing leaves your machine except the usage query to
api.anthropic.com" is *usually* true rather than reliably true.

Fix: pin an exact version (`ccusage@X.Y.Z`) so upgrades are a reviewed commit,
or install it as a real project dependency. Phase 2's native JSONL parser
(already on the TASKS list) removes the exposure entirely.

### 1.5 `urllib` forwards the `Authorization` header across redirects

`limits.py:49-55` sets `Authorization` on the `Request` and calls `urlopen`.
CPython's `HTTPRedirectHandler.redirect_request` — verified by reading the
source in this venv — strips exactly two headers when following a redirect:

```python
CONTENT_HEADERS = ("content-length", "content-type")
newheaders = {k: v for k, v in req.headers.items()
              if k.lower() not in CONTENT_HEADERS}
```

`Authorization` is preserved and re-sent to the redirect target, **including a
different host**. (`requests` strips auth on cross-host redirect; `urllib` does
not.) Any 301/302 off `api.anthropic.com` — misconfiguration, or a hostile
response on a compromised network path — walks the OAuth bearer token to a
third party.

No redirect is currently returned (direct 200), so this is latent, not active.
It is also cheap to close: install an opener whose redirect handler refuses to
follow, or drop the header on host change. Given the token's scopes
(`user:inference`, `user:sessions:claude_code`), it is worth closing.

---

## 2. Assumptions verified

The endpoint and ccusage assumptions baked into the collectors, checked against
live data. Full detail in [usage-endpoint.md](usage-endpoint.md).

| Assumption | Verdict |
|---|---|
| `utilization` is a 0–100 percentage | ✅ **Confirmed** — live values `41.0` / `33.0`, matching `/usage`. A 0–1 fraction would have made the gauges silently useless and never warn. |
| `resets_at` is offset-aware ISO 8601 | ✅ **Confirmed** — `2026-08-17T19:10:00.084456+00:00`. `fromisoformat().astimezone()` is correct *because* the offset is present. Fragile if the server ever emits naive timestamps: it would be read as local time, silently skewing every countdown by the UTC offset. |
| `five_hour` / `seven_day` are the right top-level keys | ✅ **Confirmed** — both present and populated. |
| ccusage daily field names (`period`, `totalTokens`, `totalCost`, `inputTokens`, `outputTokens`, `cacheReadTokens`, `cacheCreationTokens`) | ✅ **Confirmed all seven.** I suspected `period` was wrong (that ccusage emits `date`, which would have zeroed every "today" figure on the panel) — it is not. `period` is correct. |
| One ccusage row per day (no double-counting in the week sum) | ✅ **Confirmed** — 8 periods, 8 rows, all `agent: "all"`. ccusage can split by agent; the default does not. |
| `anthropic-beta: oauth-2025-04-20` is required | ❌ **False, harmlessly.** 200 without it and with a bogus value; Claude Code doesn't send it. Cargo-cult, not a bug. |
| "week" tokens and the WEEK gauge describe the same window | ❌ **False — and they are rendered adjacently.** `tokens.py:29` uses a **Monday-anchored calendar week** (`date.today().weekday()`). The gauge uses the server's **rolling 7-day window**, which reset at `2026-08-21T12:00:00+00:00` — a Friday, at noon UTC. The footer's `week <tokens> · $<cost>` and the `WEEK 33%` bar measure different periods, and on a Monday the token figure resets to ~0 while the gauge keeps climbing. Either anchor the local sum to `seven_day.resets_at − 7 days`, or relabel it "this calendar week" so the mismatch is explicit. |
| `PROJECTS_DIR.glob("*/*.jsonl")` every 5 s is cheap enough | ✅ **Confirmed** — 491 project dirs, 205 transcripts, 3 modified today. Wasteful in syscalls, immaterial in practice. |
| Render + encode is cheap enough to stream at 2 fps | ✅ **Confirmed by measurement** — 4.7 ms render + 1.1 ms JPEG encode (63 KB) = **1.2% of one core** at 2 fps. No optimization warranted. Likewise the activity collector's in-memory event list: 1,235 assistant events today makes the unbounded-list and O(n) rescan concerns non-issues at real volumes. |
| `snapshot()`'s shallow `copy.copy` is safe | ✅ **Confirmed** — every collector replaces sections wholesale (`dataclasses.replace` or a fresh instance) rather than mutating in place, including `hourly_tokens`. Correct as written, but it is a load-bearing invariant with nothing enforcing it: one in-place `state.tokens.today_tokens += …` introduces a torn read. Worth a comment at the mutation sites, and the one thing a collector test should pin. |
| Panel handshake is authoritative over the trcc registry resolution | ✅ Consistent with `PLANNING.md` and the spike; not re-tested (no hardware access this session). |

---

## 3. Smaller correctness and output issues

**`jpeg_quality` is documented but does nothing.** `config.py:59` parses it;
`display/panel.py:56` hardcodes `quality=90`. `app.py` never passes it through.
The README states *"Config lives in config.toml (fps, JPEG quality, night
dim/off hours)"* and `PLANNING.md` treats quality as the USB-bandwidth tuning
knob from the spike — so this is a documented feature that silently has no
effect, not merely an unused field. Thread `cfg.jpeg_quality` into
`panel.send()`.

**Activity is the only zone with no stale indicator.** `layout.py` renders
`" (stale)"` for limits (line 44) and tokens (line 70), and
`ActivityCollector.mark_stale()` sets the flag — but `_activity_zone` never
reads `a.stale`. A wedged activity collector shows a frozen project/model/burn
rate with no hint that it is frozen. Given the 5 s cadence, this is the section
where a freeze is most misleading.

**`progress_bar` overstates small values by ~5×.** `widgets.py:18-21`: when the
computed fill is narrower than the bar's diameter, it draws a full circle of
width `2 * r`. At the 22 px bar height in `layout.py:55`, anything under ~5%
renders as a ~5% blob. A fresh session at 1% looks like 5%. Either draw a
clipped partial circle or accept a 1 px sliver below the rounding threshold.

**The weekly gauge's reset timestamp is a bare time-of-day for a date two days
out.** `layout.py:59` formats both gauges identically:
`f"resets {g.resets_at:%-I:%M %p}"`. So the WEEK row renders
`resets 7:00 AM · 2d 9h` — the countdown carries the real information while the
timestamp actively misleads about *which* 7:00 AM. Claude Code draws this
distinction deliberately: its `/usage` bar table carries a per-bar
`alwaysShowDateInReset` flag, `false` for `five_hour` and `true` for
`seven_day`. Include the date (and, if adopting the per-model caps, for those
too) whenever the window is longer than a day.

**`activity.py:128` can `IndexError` at every hour boundary.**

```python
now = datetime.now().astimezone()   # captured at the top of refresh()
...                                 # _ingest() then reads freshly appended lines
buckets = [0] * (now.hour + 1)
for e in self._events:
    buckets[e.ts.hour] += e.tokens
```

`now` is sampled before `_ingest`, which then parses lines that may have been
written *after* that sample. A tick starting at 10:59:59.9 that ingests an event
stamped 11:00:00.x indexes `buckets[11]` into a list of length 11. `Collector.run`
catches it, logs a traceback, and marks activity stale; the next 5 s tick
recovers. The symptom is therefore an unexplained stale flicker on the activity
zone plus periodic tracebacks in `hud.log` at hour boundaries — annoying to
diagnose, trivial to fix: size the list to 24 (the docstring in `state.py:57`
already says 24 slots) or clamp the index.

**`utilization: null` renders as a confident "0%".** `limits.py:62`,
`float(section.get("utilization") or 0.0)`. Claude Code's schema declares this
field nullable, and the gauge already has a `—` placeholder for a missing
section (`layout.py:49`). Map null → `None` so unknown reads as unknown. Note
`or` also swallows a legitimate `0.0`, which is harmless only by coincidence.

**The tokens collector's timeout exceeds its own cadence.** `tokens.py:21` sets
`_TIMEOUT_S = 120` against a 60 s `cadence_s`, and `Collector.run` only starts
`_stop.wait(60)` *after* `refresh()` returns. A slow ccusage run therefore
stretches the effective cadence past three minutes. Not a bug, but it means the
"60 s cadence" in the docstring is a floor, not a period.

**No backoff or 429 handling on the usage endpoint.** A 429 marks the section
stale and retries in exactly 60 s, forever. The endpoint exposes no
`Retry-After`, so a modest client-side backoff on repeated failures is the only
polite option. 1,440 requests/day is likely fine; hammering through a throttle
is not.

**`PATH` can get `.` injected into the daemon's environment.**
`agent.py:21` — `Path(shutil.which("npx") or "").parent` evaluates to `Path(".")`
when npx is absent, and `"."` is then joined into the plist's `PATH` ahead of
the system directories, with `WorkingDirectory` set to the project root. A
relative entry in a long-running daemon's `PATH` is a classic hijack vector.
Guard the empty case; better, fail the install loudly, since without npx the
tokens collector cannot work at all — which is precisely the "Empty cost/tokens"
symptom the README documents.

**`Night.active` is a no-op when `start == end`.** `config.py:33` — the
`start <= now < end` branch is empty for equal bounds, so `start = end = "00:00"`
disables night mode silently rather than meaning "always" or "never" explicitly.
Edge case; worth one line.

**Collectors are never stopped.** `Collector.stop()` exists and nothing calls
it. `run` catches `KeyboardInterrupt` in `__main__.py:73` and returns while three
daemon threads are mid-`refresh` — including a `subprocess` call with a 120 s
timeout. Harmless in practice (daemon threads die with the process), but it
means Ctrl-C can leave an orphaned `npx` child.

**`preview --live` always waits the full 90 s for a user with no usage today.**
`__main__.py:110-115` breaks on `s.tokens.today_tokens and s.limits.session`.
`today_tokens == 0` is a legitimate state, so a first-thing-in-the-morning
preview hangs for 90 seconds. Break on "collector has reported" rather than on a
truthy token count.

**Test coverage is the largest gap against the project's own standard.**
`CLAUDE.md` mandates strict TDD with 100% coverage before committing.
`tests/test_render.py` covers the renderer and two formatters — 5 tests. There
are **zero** tests for the three collectors, `config.py`, `app.py`'s reconnect
logic, `agent.py`, or `widgets.progress_bar`/`sparkline`. Every finding in §1 is
in untested code, and each is straightforwardly testable without hardware:
`config.py` with a bad TOML fixture, `app.py` against a fake panel that raises
on `send` (that test *is* §1.1), the collectors against captured JSON fixtures —
the endpoint capture in `usage-endpoint.md` is ready to serve as one.

**The first command in the README fails on a fresh clone.** `__main__.py:27`
defaults `preview` to `out/preview.png`, but `.gitignore:7` ignores `out/` and
nothing creates it — `git ls-files out/` is empty, so a fresh clone has no such
directory and Pillow raises `FileNotFoundError` before rendering anything. The
README's Setup section lists `preview` as the first thing a new user runs.
`mkdir -p` the parent in the preview branch, the way `_setup_logging` already
does for `log_dir`.

**Repo hygiene.** `src/claude_trofeo_hud/__init__.py` is leftover `uv init`
scaffolding (`print("Hello from claude-trofeo-hud!")`); the real package lives at
the repo root via `module-root = ""`. Delete `src/`. `uv.lock` is modified and
uncommitted. `pyproject.toml` still has `description = "Add your description
here"`.

**Spike divergence.** `spike/send_test_frame.py` encodes at `quality=95`;
`panel.py` hardcodes 90. Harmless, but it means the spike's measured
43 KB-per-frame figure in `PLANNING.md` doesn't describe production (measured
63 KB at quality 90 for the real layout). The spike's non-JPEG branch also
imports `numpy`, which is not a declared dependency — dead code for this panel,
but it would fail if ever reached. Neither spike file is imported at runtime and
neither contains secrets.

---

## Suggested order

Sequenced work plan: [docs/plans/review-remediation.md](plans/review-remediation.md).

1. **Stop reconnecting on soft send failure (§1.1)** — risks wedging the panel
   until physical replug, per the driver's own warning.
2. **Guard config parsing against `ValueError`/`TypeError`/`AttributeError`
   (§1.2)** — prevents an indefinite 10 s launchd crash-loop.
3. **Pin the ccusage version (§1.4)** — one-line change, largest security
   reduction.
4. **Pre-flight `expiresAt` → "AUTH EXPIRED" (§1.3)** — makes the most likely
   unattended failure legible.
5. Refuse cross-host redirects on the usage request (§1.5).
6. Merge `limits[]` for the per-model weekly cap (see
   [usage-endpoint.md](usage-endpoint.md)) — the one materially missing gauge.
7. Reconcile the week-window mismatch (§2); thread `jpeg_quality`, add the
   activity stale indicator, size the hourly buckets to 24, show the date on the
   weekly reset, and create `out/` for `preview` (§3).
8. Backfill collector and config tests, starting with the ones that pin §1.1
   and §1.2.

## Provenance

Written from a direct read of the package plus live verification of the usage
endpoint and ccusage output. Independently re-verified by an adversarial review
agent instructed to refute rather than agree; it confirmed most findings,
**overturned the severity and the proposed fix for §1.1**, showed §1.2's fix was
incomplete, and surfaced the fresh-clone `preview` failure. Corrections are
marked inline rather than silently folded in.
