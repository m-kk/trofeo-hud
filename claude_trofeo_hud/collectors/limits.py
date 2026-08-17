"""Usage limits (session / weekly %) via Anthropic's OAuth usage endpoint.

Reads the Claude Code OAuth token fresh from the macOS Keychain each refresh
(Claude Code rotates it; the Keychain always has the current one) and makes a
read-only GET. The token goes nowhere except api.anthropic.com over HTTPS.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import subprocess
import urllib.error
import urllib.request
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from ..state import LimitGauge, Limits
from .base import Collector

log = logging.getLogger(__name__)

_KEYCHAIN_SERVICE = "Claude Code-credentials"
_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_BETA_HEADER = "oauth-2025-04-20"
_TIMEOUT_S = 15

_WEEK_WINDOW_S = 7 * 86400
# Measured 2026-08-17: five_hour is *anchored*, not rolling. Two samples 12
# minutes apart while actively working moved utilization 22.0 -> 31.0 while
# resets_at stayed at 2026-08-18T00:19:59Z. A window whose reset doesn't move
# with use has a meaningful elapsed fraction, so the marker is honest here.
_SESSION_WINDOW_S = 5 * 3600
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
            used_pct=float(section.get(pct_key) or 0.0),
            resets_at=_local_naive(section.get("resets_at")),
            window_s=window_s,
        )

    return Limits(
        session=gauge(data.get("five_hour"), "Current session", _SESSION_WINDOW_S),
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


class LimitsCollector(Collector):
    name_ = "limits"
    # The endpoint admits roughly one call per two minutes (measured: at a 60s
    # cadence, every other poll 429s — docs/usage-endpoint.md). Utilization
    # moves slowly and the countdowns tick client-side off resets_at, so a
    # five-minute cadence costs the panel nothing and stays clear of the limit.
    cadence_s = 300.0

    def refresh(self) -> None:
        oauth = _oauth()
        req = urllib.request.Request(
            _USAGE_URL,
            headers={
                "Authorization": f"Bearer {oauth['accessToken']}",
                "anthropic-beta": _BETA_HEADER,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as err:
            if err.code == 429:
                raise Throttled(retry_after_s(err)) from None
            raise

        limits = parse_usage(
            data,
            plan=plan_label(oauth.get("subscriptionType"), oauth.get("rateLimitTier")),
        )
        self.shared.update(limits=limits)
        log.debug(
            "limits: session=%s weekly=%s fable=%s",
            limits.session and limits.session.used_pct,
            limits.weekly and limits.weekly.used_pct,
            limits.weekly_fable and limits.weekly_fable.used_pct,
        )

    def mark_stale(self) -> None:
        def apply(state) -> None:
            state.limits = dataclasses.replace(state.limits, stale=True)

        self.shared.mutate(apply)
