# trofeo-hud

**Repo:** <https://github.com/m-kk/trofeo-hud>

A desk HUD that shows live Claude usage on a Thermalright Trofeo Vision 6.86"
LCD (1280×480, USB-C, ~$38), driven from macOS. Inspired by the r/ClaudeAI
"$38 Claude LCD Table Display" post. Started as a fork of
[christensen143/claude-trofeo-hud](https://github.com/christensen143/claude-trofeo-hud)
and now developed independently here.

![Live HUD render — session/weekly limit gauges, today's tokens and cost, current session activity, hourly burn sparkline](docs/hud.png)

What it shows: Pro/Max session + weekly limit bars with reset countdowns
(from Anthropic's usage endpoint), today's tokens and estimated API cost
(parsed natively from Claude Code's transcripts), the live session
(project, model, burn rate), a clock, and an hourly token sparkline.

### Reading the limit gauges

Each window is one row — label and percentage on a line, that row's bar
directly beneath it. Bar colour tracks the percentage only (amber past 80%,
red past 95%); it never encodes which model a window belongs to. The plan tier
sits next to the `USAGE` heading.

The white vertical mark standing proud of each bar is the **pace marker**: how
far through the window you already are. Fill short of the marker means you're
using the window slower than the clock is spending it; fill past it means
you'll run out before the reset.

It needs the window's full length, which is only meaningful if the reset is
anchored rather than sliding with use. Both are: sampled 12 minutes apart while
working, session utilization moved 22% → 31% while `resets_at` held at the same
second. A gauge whose window length is unknown renders without a marker rather
than guessing. One caveat on the 7-day bars: the span is assumed full, so if an
account's *first* weekly window is short, the marker is optimistic for that
window only.

The per-model weekly cap (`Fable only`) is absent on most accounts; when the
API doesn't report it, the row is dropped and the column closes up rather than
showing an empty bar.

## Requirements

- macOS, Python 3.12+, [uv](https://docs.astral.sh/uv/)
- `brew install hidapi` (C library behind the `hidapi` Python package)
- Claude Code installed and logged in (the HUD reads its local logs and its
  OAuth token from the Keychain — read-only, and the only thing sent anywhere
  is the usage query to api.anthropic.com)

### Where the numbers come from

Token counts and the "est. API cost" are computed natively from Claude Code's
own transcripts (`~/.claude/projects/**/*.jsonl`, including subagent
transcripts), deduplicated the way ccusage does — one API message is written
as several lines with identical `usage` — and priced at Anthropic's list rates
(`trofeo_hud/pricing.py`; the table is dated, bump it when prices move).
Advisor-tool calls that Claude Code itemises under `usage.iterations` are
counted at their own model's rate. Cost is what the usage *would* have cost
pay-as-you-go; on a subscription it is a proxy for how hard the plan is being
worked. Nothing is executed from npm and no network is used for this.

When the usage endpoint can't be read (throttled, or the token has expired) the
session gauge falls back to the transcripts: the block's reset time is
estimated from request timestamps and, while the last good reading's window is
still live, its percentage is scaled by the cost accrued since. Such gauges are
labelled `(est.)` — usage on other machines is invisible to the estimate.

## Setup

```bash
git clone https://github.com/m-kk/trofeo-hud.git && cd trofeo-hud
uv sync
uv run trofeo-hud preview         # render mock layout to out/preview.png
uv run trofeo-hud run             # live HUD on the LCD (Ctrl-C stops)
uv run trofeo-hud install-agent   # start at login via launchd
```

(`python -m trofeo_hud …` is equivalent.)

On the first `run`, macOS asks for Keychain access to "Claude Code-credentials"
— choose **Always Allow** so the daemon can run unattended.

`uninstall-agent` stops and removes the launchd agent. Config lives in
[config.toml](config.toml) next to the project or `~/.config/trofeo-hud/config.toml`
(fps, JPEG quality, night dim/off hours; the pre-rename
`~/.config/claude-trofeo-hud/` location is still read as a fallback). Logs go to
`~/Library/Logs/trofeo-hud/`.

Upgrading from a `claude-trofeo-hud` checkout: run `install-agent` again — it
also stops and removes the old `com.varlogchris.claude-trofeo-hud` launchd
agent so two daemons don't fight over the panel.

## How it drives the display

The panel is not a monitor — it's a USB HID device (VID:PID `0416:5302`) that
accepts JPEG frames over a reverse-engineered protocol. We use the device
classes from [thermalright-trcc-linux](https://github.com/Lexonight1/thermalright-trcc-linux)
with its `HidApiTransport` (IOHIDManager), bypassing its CLI — trcc's default
transport routes through libusb, which macOS blocks for HID devices. The
firmware blanks when idle, so the HUD streams continuously (default 2 fps).
See [PLANNING.md](PLANNING.md) for the full protocol notes.

## Troubleshooting

- **"Access denied (insufficient permissions)"** — something is opening the
  device via libusb instead of hidapi; make sure you're running our CLI, not
  `trcc` directly.
- **Panel shows boot logo / blanks** — no frames arriving; check
  `~/Library/Logs/trofeo-hud/hud.log`. Unplug/replug is handled
  automatically with backoff.
- **Empty cost/tokens** — the HUD reads `~/.claude/projects/**/*.jsonl`;
  make sure Claude Code has been used on this machine today and that the
  files are readable. Only files modified in the last 8 days are opened.
- **Panel shows "AUTH EXPIRED"** — the OAuth token in the Keychain has expired.
  Only Claude Code can refresh it (the HUD deliberately won't write to that
  Keychain item — a second writer would race Claude Code's own rotation), so
  run Claude Code once and the HUD recovers on its next refresh. This is the
  expected state after the daemon has been running unattended for a while with
  no interactive Claude Code sessions.
- **Limits stale** — Keychain access not granted, you're logged out of Claude
  Code, or the usage endpoint is rate-limiting. Note that an expired token also
  comes back as HTTP 429 rather than 401, which is why the HUD checks the
  expiry timestamp locally rather than trusting the status code.
