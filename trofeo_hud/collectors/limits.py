"""Usage limits (session / weekly %) via Anthropic's OAuth usage endpoint.

Reads the Claude Code OAuth credentials fresh from the macOS Keychain each
refresh (Claude Code rotates the token; the Keychain always has the current one)
and makes a read-only GET. The token goes nowhere except api.anthropic.com over
HTTPS — see `_NoCrossHostRedirect`, which is what enforces that.

We deliberately do **not** refresh the token ourselves. The Keychain item is
owned by Claude Code, and a second writer would race its rotation. Instead we
read the expiry that ships alongside the token and say so on the panel.

When the endpoint can't be read (throttled, token expired) the session gauge
falls back to the transcripts on disk: the block's reset is estimated from
event timestamps, and while the last good sample's window is still live its
percentage is scaled by the cost accrued since. Labelled `(est.)`. Usage on
other machines is invisible to this, so it is an estimate, not a reading.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import subprocess
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from .. import pricing
from ..state import LimitGauge, Limits
from . import transcripts as tx
from .base import Collector

log = logging.getLogger(__name__)

_KEYCHAIN_SERVICE = "Claude Code-credentials"
_USAGE_HOST = "api.anthropic.com"
_USAGE_URL = f"https://{_USAGE_HOST}/api/oauth/usage"
_BETA_HEADER = "oauth-2025-04-20"
_TIMEOUT_S = 15

_WEEK_WINDOW_S = 7 * 86400
# Measured 2026-08-17: five_hour is *anchored*, not rolling. Two samples 12
# minutes apart while actively working moved utilization 22.0 -> 31.0 while
# resets_at stayed at 2026-08-18T00:19:59Z. A window whose reset doesn't move
# with use has a meaningful elapsed fraction, so the marker is honest here.
_SESSION_WINDOW_S = 5 * 3600
_SESSION_LABEL = "Current session"
_EST_LABEL = f"{_SESSION_LABEL} (est.)"
_FABLE = "Fable"


class Throttled(Exception):
    """HTTP 429 from the usage endpoint.

    The endpoint answers 429 both for genuine throttling and for an expired
    or malformed token (docs/usage-endpoint.md), so this deliberately doesn't
    claim to know which. `retry_after_s` carries the server's own hint when
    it sends one; the collector's backoff uses it.
    """

    def __init__(self, retry_after: float | None) -> None:
        self.retry_after_s = retry_after
        super().__init__(
            "429 from the usage endpoint — throttled, or the OAuth token "
            "expired; the response can't tell us which"
        )


def retry_after_s(err: urllib.error.HTTPError) -> float | None:
    """Seconds from a Retry-After header, in either RFC 7231 form."""
    raw = (err.headers or {}).get("Retry-After")
    if not raw:
        return None
    try:
        return float(int(str(raw).strip()))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class _NoCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects that leave the original host.

    `HTTPRedirectHandler.redirect_request` copies every header except
    content-length/content-type onto the redirected request, and compares
    nothing about the target host. Our request carries an `Authorization:
    Bearer` header, so an off-host redirect would hand the OAuth token to
    whoever answered. (`requests` strips auth on cross-host redirect; urllib
    does not.) The endpoint does not redirect today — this keeps it that way.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).netloc.lower() != urlsplit(req.full_url).netloc.lower():
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                f"refusing cross-host redirect to {urlsplit(newurl).netloc} "
                f"— it would forward the OAuth bearer token",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_NoCrossHostRedirect)


def _oauth() -> dict:
    """The whole claudeAiOauth blob from the Keychain — token plus plan info."""
    out = subprocess.run(
        ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout
    return json.loads(out)["claudeAiOauth"]


def _expired(creds: dict) -> bool:
    """True when the access token's own expiry has passed.

    `expiresAt` is epoch milliseconds. Absent on older credential blobs, in
    which case we say nothing and let the request decide — an expired token
    comes back as HTTP 429 `rate_limit_error` rather than 401, so the response
    alone cannot tell us this.
    """
    expires_at = creds.get("expiresAt")
    if not expires_at:
        return False
    return datetime.fromtimestamp(expires_at / 1000) <= datetime.now()


def _utilization(section: dict, key: str) -> float | None:
    """0-100, or None when the server reports it as null.

    Coercing null to 0.0 would render a confident "0%" for "unknown", which is
    indistinguishable from genuinely-unused — so keep the distinction and let
    the renderer show its placeholder.
    """
    value = section.get(key)
    return None if value is None else float(value)


def plan_label(
    subscription_type: str | None, rate_limit_tier: str | None
) -> str | None:
    """ "max" + "default_claude_max_5x" -> "Max (5x)".

    rateLimitTier's full vocabulary is unconfirmed, so anything without a
    plain NNx multiplier degrades to the subscription name alone.
    """
    if not subscription_type:
        return None
    label = subscription_type.replace("_", " ").title()
    m = re.search(r"(\d+)x", rate_limit_tier or "")
    return f"{label} ({m.group(1)}x)" if m else label


def _scoped_window(limits: list | None, display_name: str) -> dict | None:
    """The weekly per-model window, which lives only in `limits[]`."""
    for entry in limits or []:
        if entry.get("kind") != "weekly_scoped":
            continue
        model = (entry.get("scope") or {}).get("model") or {}
        if (model.get("display_name") or "").lower() == display_name.lower():
            return entry
    return None


def parse_usage(data: dict, plan: str | None = None) -> Limits:
    """Usage-endpoint JSON -> Limits. Pure; every section is optional."""

    def gauge(
        section: dict | None,
        label: str,
        window_s: float | None,
        pct_key: str = "utilization",
    ) -> LimitGauge | None:
        if not section:
            return None
        return LimitGauge(
            label=label,
            used_pct=_utilization(section, pct_key),
            resets_at=_local_naive(section.get("resets_at")),
            window_s=window_s,
        )

    return Limits(
        session=gauge(data.get("five_hour"), _SESSION_LABEL, _SESSION_WINDOW_S),
        weekly=gauge(data.get("seven_day"), "All models", _WEEK_WINDOW_S),
        weekly_fable=gauge(
            _scoped_window(data.get("limits"), _FABLE),
            f"{_FABLE} only",
            _WEEK_WINDOW_S,
            pct_key="percent",
        ),
        plan=plan,
    )


def _local_naive(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).astimezone().replace(tzinfo=None)
    except ValueError:
        return None


def estimate_session(events: list[tx.UsageEvent], now: datetime) -> LimitGauge | None:
    """The current 5-hour block, from transcript timestamps alone.

    A block starts at the first request after the previous block ended, so
    the chain is walked from the oldest event we hold; it re-anchors after
    any gap of five hours or more, which makes a week of history plenty.
    Returns None between blocks (the next request would start one) and never
    guesses a percentage.
    """
    window = timedelta(seconds=_SESSION_WINDOW_S)
    start = None
    for ev in sorted(events, key=lambda e: e.ts):
        if start is None or ev.ts >= start + window:
            start = ev.ts
    if start is None or now >= start + window:
        return None
    return LimitGauge(
        label=_EST_LABEL,
        used_pct=None,
        resets_at=(start + window).astimezone().replace(tzinfo=None),
        window_s=_SESSION_WINDOW_S,
    )


@dataclasses.dataclass(frozen=True)
class _Sample:
    """The last good session reading, with the local cost in its window then."""

    resets_at: datetime  # local naive, as on the gauge
    used_pct: float
    cost_usd: float


class LimitsCollector(Collector):
    name_ = "limits"
    # The endpoint admits roughly one call per two minutes (measured: at a 60s
    # cadence, every other poll 429s — docs/usage-endpoint.md). Utilization
    # moves slowly and the countdowns tick client-side off resets_at, so a
    # five-minute cadence costs the panel nothing and stays clear of the limit.
    cadence_s = 300.0

    def __init__(self, shared, log: tx.TranscriptLog | None = None) -> None:
        super().__init__(shared)
        self.log = log
        self._sample: _Sample | None = None

    def refresh(self) -> None:
        oauth = _oauth()
        if _expired(oauth):
            log.warning(
                "OAuth token expired — run Claude Code to refresh it "
                "(we must not rotate it ourselves)"
            )
            self._flag(auth_expired=True)
            return

        req = urllib.request.Request(
            _USAGE_URL,
            headers={
                "Authorization": f"Bearer {oauth['accessToken']}",
                "anthropic-beta": _BETA_HEADER,
                "Content-Type": "application/json",
            },
        )
        try:
            with _OPENER.open(req, timeout=_TIMEOUT_S) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as err:
            if err.code == 429:
                raise Throttled(retry_after_s(err)) from None
            raise

        limits = parse_usage(
            data,
            plan=plan_label(oauth.get("subscriptionType"), oauth.get("rateLimitTier")),
        )
        self._remember(limits.session)
        self.shared.update(limits=limits)
        log.debug(
            "limits: session=%s weekly=%s fable=%s",
            limits.session and limits.session.used_pct,
            limits.weekly and limits.weekly.used_pct,
            limits.weekly_fable and limits.weekly_fable.used_pct,
        )

    def mark_stale(self) -> None:
        self._flag(stale=True)

    def _flag(self, **fields) -> None:
        """Set flags while keeping the last-good gauges — except the session
        gauge, which is re-estimated from the transcripts when we have them."""

        def apply(state) -> None:
            state.limits = dataclasses.replace(state.limits, **fields)
            if self.log is not None:
                state.limits = dataclasses.replace(
                    state.limits, session=self._fallback(state.limits.session)
                )

        self.shared.mutate(apply)

    # ── transcript fallback ──────────────────────────────────────────────

    def _remember(self, session: LimitGauge | None) -> None:
        self._sample = None
        if self.log is None or session is None:
            return
        if session.resets_at is None or session.used_pct is None:
            return
        self._sample = _Sample(
            session.resets_at, session.used_pct, self._window_cost(session.resets_at)
        )

    def _window_cost(self, resets_at: datetime) -> float:
        now = tx.local_now()
        self.log.ingest(now)
        start = (resets_at - timedelta(seconds=_SESSION_WINDOW_S)).astimezone()
        return sum(pricing.cost_usd(e) for e in self.log.events(since=start))

    def _fallback(self, current: LimitGauge | None) -> LimitGauge | None:
        now = tx.local_now()
        live = (
            current is not None
            and current.resets_at is not None
            and now.replace(tzinfo=None) < current.resets_at
        )
        if live:
            sample = self._sample
            if sample is None or sample.resets_at != current.resets_at:
                return current  # nothing to scale from; the reading stands
            if sample.cost_usd > 0:
                pct = (
                    sample.used_pct
                    * self._window_cost(current.resets_at)
                    / sample.cost_usd
                )
            else:
                pct = sample.used_pct
            return dataclasses.replace(
                current, label=_EST_LABEL, used_pct=min(100.0, pct)
            )
        self.log.ingest(now)
        return estimate_session(self.log.events(), now)
