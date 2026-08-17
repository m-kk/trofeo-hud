# `GET /api/oauth/usage` — field reference

Explored 2026-08-17 against a live **Max** subscription. Everything below is
either **observed** in a real response, **corroborated** by the Claude Code
2.1.233 binary (zod schemas and consumer code extracted with `strings`), or
explicitly marked **unconfirmed**.

Example payloads here use synthetic values — the real capture contains the
account's organization/workspace UUIDs and spend figures and is deliberately
not committed.

> This endpoint is undocumented and not part of Anthropic's public API. It can
> change without notice. `collectors/limits.py` is the only place that touches
> it; keep it that way.

---

## Request

```http
GET https://api.anthropic.com/api/oauth/usage
Authorization: Bearer <claudeAiOauth.accessToken>
```

| Aspect | Observed behavior |
|---|---|
| `anthropic-beta: oauth-2025-04-20` | **Not required.** 200 with it, without it, and with a bogus beta value. Claude Code itself sends only `Content-Type: application/json`. Harmless to keep, but it is not load-bearing. |
| `anthropic-version` | Not required. |
| `x-api-key` auth | Rejected. This endpoint is OAuth-only — a plain API key does not work. |
| Methods | `GET` only. `HEAD` → **405**. |
| Invalid / absent / expired token | **HTTP 429 `rate_limit_error`** — *not* 401. See [Auth failure looks like rate limiting](#auth-failure-looks-like-rate-limiting). |
| Rate-limit headers | None exposed (no `RateLimit-*`, no `Retry-After`). |
| Timeout used by Claude Code | 5 s, with 401 → token-refresh → retry. Our collector uses 15 s and cannot refresh. |
| Redirects | None observed (direct 200). Relevant anyway — see the finding on `urllib` and `Authorization` forwarding. |

Useful **response headers**: `anthropic-organization-id`, `anthropic-workspace-id`,
`request-id` (quote the latter if you ever need to ask Anthropic about a response).

---

## Response: top-level keys

17 keys observed. Claude Code parses the body with a `.passthrough()` zod schema
and consumes only this allowlist:

```js
["five_hour", "seven_day", "seven_day_oauth_apps",
 "seven_day_opus", "seven_day_sonnet", "cinder_cove",
 "extra_usage", "limits"]
```

Everything outside that list is server-emitted and has **no consumer in the
CLI** — treat it as unstable.

| Key | Type | This account | Consumed by CLI | Notes |
|---|---|---|---|---|
| `five_hour` | LimitWindow | `41.0` | ✅ | The rolling session window. `/usage` labels it "Current session". |
| `seven_day` | LimitWindow | `33.0` | ✅ | `/usage`: "Current week (all models)". |
| `seven_day_opus` | LimitWindow | `null` | ✅ | Per-model weekly cap. Null here. |
| `seven_day_sonnet` | LimitWindow | `null` | ✅ | `/usage`: "Current week (Sonnet only)", rendered only when plan ∈ {`max`, `team`, `null`}. Null here. |
| `seven_day_oauth_apps` | LimitWindow | `null` | ✅ | Separate window for OAuth-app (non-Claude-Code) traffic. |
| `cinder_cove` | LimitWindow | `null` | ✅ | Codename bucket that *is* in the allowlist — so it is a real, currently-unused window, not noise. |
| `extra_usage` | object | see below | ✅ | Usage-credit ("extra usage") state. |
| `limits` | array | 3 entries | ✅ | **The forward-compatible view.** See below. |
| `seven_day_cowork` | LimitWindow | `null` | ❌ | 0 hits in the binary. |
| `seven_day_omelette` | LimitWindow | `null` | ❌ | 0 hits. |
| `omelette_promotional` | LimitWindow | `null` | ❌ | 0 hits. |
| `tangelo` | LimitWindow | `null` | ❌ | 0 hits. |
| `iguana_necktie` | LimitWindow | `null` | ❌ | 0 hits. |
| `amber_ladder` | LimitWindow | `null` | ❌ | 0 hits. |
| **`nimbus_quill`** | LimitWindow | **`{utilization: 0.0, resets_at: null}`** | ❌ | The one anomaly: the only unknown bucket returned as a **populated object** rather than `null`, yet 0 hits in the binary. Meaning unknown. Do not render it. |
| `spend` | object | see below | ❌ (this path) | Consumed elsewhere — `/usage-credits`. |
| `member_dashboard_available` | bool | `false` | ❌ | 0 hits. Presumably a team/enterprise UI affordance. |

### `LimitWindow`

```json
{
  "utilization": 41.0,
  "resets_at": "2026-08-17T19:10:00.084456+00:00",
  "limit_dollars": null,
  "used_dollars": null,
  "remaining_dollars": null
}
```

- **`utilization` is on a 0–100 scale**, as a float. Confirmed against `/usage`,
  which showed the same percentages. Our `used_pct` treatment is correct.
- **`utilization` is nullable** in Claude Code's schema
  (`z.number().nullable()`). `limits.py` coerces null → `0.0`, which renders a
  confident "0%" for "unknown". Prefer `None` → the `—` placeholder.
- **`resets_at` is offset-aware ISO 8601** (`+00:00`), also nullable. The
  `datetime.fromisoformat(...).astimezone()` conversion in `limits.py` is
  correct *because* the offset is present; it would silently mis-read a naive
  timestamp as local time if the server ever dropped the offset.
- `*_dollars` are `null` on this Max subscription and appear **nowhere** in the
  Claude Code binary. Most likely populated only for spend-metered plans.
  Unconfirmed.

### `limits[]` — the richer, forward-compatible view

```json
[
  {"kind": "session",       "group": "session", "percent": 41, "severity": "normal",
   "resets_at": "...", "scope": null, "is_active": true},
  {"kind": "weekly_all",    "group": "weekly",  "percent": 33, "severity": "normal",
   "resets_at": "...", "scope": null, "is_active": false},
  {"kind": "weekly_scoped", "group": "weekly",  "percent": 10, "severity": "normal",
   "resets_at": "...", "is_active": false,
   "scope": {"model": {"id": null, "display_name": "Fable"}, "surface": null}}
]
```

| Field | Notes |
|---|---|
| `kind` | Observed: `session`, `weekly_all`, `weekly_scoped`. Full enum unconfirmed. **Not** the same namespace as the `five_hour`/`seven_day_opus`/… strings, which belong to the rate-limit *header* vocabulary. |
| `group` | Observed: `session`, `weekly`. A coarser grouping for stacking bars. |
| `percent` | **Integer** here (41, 33, 10), where top-level `utilization` is a float (41.0). |
| `severity` | Observed only `normal`. The binary contains `severity: "warning"` literals, so at least one escalation level exists, but that string was not clearly in this payload's namespace — **enum unconfirmed**. If confirmed, it replaces our hardcoded 80%/95% thresholds with server-authoritative state. |
| `scope` | `null`, or `{model: {id, display_name}, surface}`. `display_name` is a server-supplied label ("Fable"); `id` was `null`. |
| `is_active` | `true` on session, `false` on both weekly entries. Plausibly "is this the currently binding window" — **semantics unconfirmed**, do not build on it. |

**The key structural fact:** on this account `seven_day_opus` and
`seven_day_sonnet` are `null`, yet `limits[]` carries a real **10% weekly
Fable cap**. That window exists *only* in `limits[]`. Reading top-level keys
alone — which is what `limits.py` does — makes the per-model weekly cap
structurally invisible.

Claude Code reads **both** paths and merges them:

```js
// top-level bars
{bar: "five_hour",         title: "Current session"}
{bar: "seven_day",         title: "Current week (all models)"}
{bar: "seven_day_sonnet",  title: "Current week (Sonnet only)"}  // plan ∈ {max, team, null}
// plus, from limits[]:
limits.filter(l => l.kind === "weekly_scoped" && l.scope?.model
                && allowlist.includes(l.scope.model.display_name.toLowerCase()))
      .map(l => ({title: `Current week (${l.scope.model.display_name})`,
                  limit: {utilization: l.percent, resets_at: l.resets_at}}))
```

The scoped bars are gated on a remote-config allowlist
(`tengu_usage_overage_included_models`), so Claude Code may hide a scoped limit
that the server is reporting. A HUD has no reason to apply that gate.

**Recommendation: merge both sources**, preferring `limits[]` for anything
scoped. Which path is populated evidently varies by account and rollout, so
reading only one is fragile in either direction.

### `extra_usage` — usage credits

```json
{
  "is_enabled": false, "monthly_limit": 10000, "used_credits": 0.0,
  "utilization": 0.0, "currency": "USD", "decimal_places": 2,
  "disabled_reason": "out_of_credits", "user_disabled": false,
  "spend_limit_reached": false, "credits_ever_enabled": true,
  "daily": null, "weekly": null
}
```

⚠️ **`monthly_limit` is in minor units.** With `decimal_places: 2`,
`10000` means **$100.00**, not $10,000. Rendering it raw would be off by 100×.

`used_credits` is a float and appears to be in the same minor units.
`daily`/`weekly` are null here — shape unconfirmed. Observed
`disabled_reason` values in the binary include `out_of_credits`,
`member_zero_credit_limit`, `seat_tier_zero_credit_limit`,
`org_spend_cap_reached`.

Claude Code's schema for this object covers only
`{is_enabled, monthly_limit, used_credits, utilization, currency, disabled_reason}`
— the rest passes through unvalidated.

### `spend` — credit balance and cap

```json
{
  "used":  {"amount_minor": 0,     "currency": "USD", "exponent": 2},
  "limit": {"amount_minor": 10000, "currency": "USD", "exponent": 2},
  "percent": 0, "severity": "normal", "enabled": false,
  "disabled_reason": "out_of_credits",
  "cap": {"money": null, "credits": {"amount_minor": 10000, "exponent": 2}},
  "balance": null, "auto_reload": null,
  "can_purchase_credits": false, "can_toggle": false,
  "disclaimer": "Usage credits cover you when you hit your plan limits. …"
}
```

Same minor-units convention, here via `exponent`: `amount_minor: 10000,
exponent: 2` → **$100.00**. `balance` and `auto_reload` are null on this
account; shapes unconfirmed. `disclaimer` contains markdown — it is copy for a
dialog, not a HUD string.

---

## Datapoints available *without* the network

`security find-generic-password -s "Claude Code-credentials" -w` returns JSON
whose `claudeAiOauth` object holds more than the access token:

| Field | This account | HUD use |
|---|---|---|
| `subscriptionType` | `"max"` | Label the panel ("MAX"); also the gate Claude Code uses to decide whether to show the Sonnet-only bar. |
| `rateLimitTier` | *(present)* | Plan tier detail. Semantics unconfirmed. |
| `scopes` | `user:profile`, `user:inference`, `user:sessions:claude_code`, `user:file_upload`, `user:mcp_servers` | `user:profile` is what makes the usage endpoint work. Its absence ⇒ no rate-limit data. |
| `expiresAt` | epoch ms, **~1 h out when sampled** | Directly renderable as an "auth expires in" state — and the root of the staleness problem below. |
| `refreshTokenExpiresAt` | epoch ms | Long-horizon session validity. |

Do not write to this Keychain item. Claude Code owns the rotation; a second
writer races it.

---

## Auth failure looks like rate limiting

An invalid, malformed, or absent bearer token returns **HTTP 429
`rate_limit_error`** — the same response as genuine throttling. Verified with
a well-formed-but-fake `sk-ant-oat01-…` token and with no `Authorization`
header at all.

*Caveat:* several unauthenticated probes were made during this exploration, so
these could in principle share a tripped anonymous bucket. Against that: the
very first unauthenticated request already returned 429, which is the signature
of a deliberate anti-enumeration response rather than an exhausted quota.

Consequence for the HUD: **the collector cannot distinguish "token expired"
from "you are being throttled."** Both surface as one 429 in the log and one
`stale` flag on the panel, and the README's troubleshooting entry ("Keychain
access not granted, or you're logged out") points at neither.

Since `expiresAt` is available locally, the collector can and should make that
distinction *before* the request: if the token is past `expiresAt`, render a
distinct "AUTH EXPIRED" state rather than a stale percentage.

---

## Alternative source: the `get_usage` control request

Claude Code exposes the same data — plus a great deal more — through its
control protocol, as subtype **`get_usage`**, described in the binary as
*"Structured /usage data: session cost/usage totals plus claude.ai plan
rate-limit utilization. **Experimental — the shape may change.**"*

```
session:
  total_cost_usd, total_api_duration_ms, total_duration_ms,
  total_lines_added, total_lines_removed, model_usage{model → {...}}
subscription_type            'pro' | 'max' | 'team' | 'enterprise' | null
rate_limits_available        false for API-key / Bedrock / Vertex / missing profile scope
rate_limits:
  five_hour, seven_day, seven_day_oauth_apps, seven_day_opus, seven_day_sonnet
  model_scoped[]             {display_name, utilization, resets_at}
                             ← "Per-model weekly windows from the server limits[]
                                array, filtered by the overage-included-models
                                allowlist. Additive — present only when the
                                server emits them."
  extra_usage                {is_enabled, monthly_limit, used_credits, utilization, currency}
behaviors:                   null for non-subscriber sessions or if the scan fails
  day / week (last 24 h / 7 d), each:
    request_count            API requests in local transcripts for this window
    session_count            distinct sessions observed
    behaviors[]              {key, pct, count}, key ∈ cache_miss | long_context
                             | subagent_heavy | high_parallel | cron
                             (categories overlap — percentages do not sum to 100)
    agents[] skills[] plugins[] mcp_servers[]   each {name, pct}
```

Sibling subtypes: `get_session_cost`, `get_context_usage`, `get_plan`.

This is the richest inventory of HUD-able datapoints found anywhere in this
exploration — per-skill / per-agent / per-plugin / per-MCP-server attribution,
behavioral breakdown of *what* is consuming the limit, lines added/removed, and
`model_scoped` already merged for us. `behaviors` is explicitly documented as
the same local-transcript scan the `/usage` dialog renders, which also means it
is approximate and machine-local.

The catch: it is a control request, so reaching it means driving a Claude Agent
SDK session rather than making an HTTP call — a much heavier dependency than
`urllib` — and it is flagged experimental. Worth knowing about; not obviously
worth adopting for two gauges.

---

## Unrendered datapoints, ranked by value to this HUD

1. **Per-model weekly cap** (`limits[]` `weekly_scoped`, 10% Fable here) — on
   Max, the per-model weekly window is often the *binding* constraint, and it
   is the one number the panel cannot currently show at all.
2. **`severity`** — server-authoritative warn/critical state, replacing our
   guessed 80%/95% thresholds.
3. **`subscriptionType`** (Keychain, free) — plan label on the panel.
4. **`expiresAt`** (Keychain, free) — turns silent staleness into an actionable
   "AUTH EXPIRED".
5. **`extra_usage` / `spend`** — credit headroom, for anyone who has credits
   enabled. Mind the minor-units conversion.
6. **`seven_day_oauth_apps`** — segregates non-Claude-Code OAuth traffic.
7. `behaviors.*` attribution (via `get_usage` only) — "what is eating my limit".

## Exists, but not reachable from this app

The `anthropic-ratelimit-unified-*` response headers —
`-status` (`allowed` / `allowed_warning` / `rejected`), `-reset`,
`-representative-claim` (`five_hour` | `seven_day` | `seven_day_opus` |
`seven_day_sonnet` | `seven_day_overage_included`), `-overage-status`,
`-overage-disabled-reason` — carry near-real-time limit state, which is how
Claude Code knows it is being throttled mid-turn.

They appear only on **inference** API responses. A read-only HUD makes no
inference calls, so it can never see them. Listed here so nobody spends time
looking for them on the usage endpoint.

---

## Reproducing this

```bash
.venv/bin/python - <<'EOF'
import json, subprocess, urllib.request
tok = json.loads(subprocess.run(
    ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
    capture_output=True, text=True, check=True).stdout)["claudeAiOauth"]["accessToken"]
req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage",
                             headers={"Authorization": f"Bearer {tok}"})
with urllib.request.urlopen(req, timeout=20) as r:
    print(json.dumps(json.loads(r.read()), indent=2))
EOF
```

Use the venv interpreter: the system `python3.14` on this machine has no CA
bundle configured and fails TLS verification against `api.anthropic.com`.

Schemas and consumer logic were recovered from the Claude Code binary at
`~/.local/share/claude/versions/2.1.233` with `strings -n 6`.
