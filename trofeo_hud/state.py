"""HudState — everything the renderer needs, with no knowledge of sources.

Every section is optional: a collector that hasn't produced data yet (or has
gone stale) leaves its section None / stale=True and the renderer degrades
gracefully instead of crashing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class LimitGauge:
    """One rate-limit window (session or weekly)."""

    label: str
    used_pct: float | None  # 0..100, or None when the server says null
    resets_at: datetime | None = None
    # Full length of the window, when it is a fixed span anchored to its
    # reset — verified true for both the 5-hour and 7-day windows. Left None
    # for any window whose reset moves with use, where "fraction elapsed"
    # isn't a meaningful quantity; the pace marker is then omitted.
    window_s: float | None = None

    def elapsed_pct(self, now: datetime) -> float | None:
        """How far through the window we are, 0..100, or None if unknowable."""
        if self.resets_at is None or not self.window_s:
            return None
        remaining = (self.resets_at - now).total_seconds()
        return min(100.0, max(0.0, 100.0 * (1 - remaining / self.window_s)))


@dataclass
class Limits:
    session: LimitGauge | None = None
    weekly: LimitGauge | None = None
    weekly_fable: LimitGauge | None = None  # per-model cap; often absent
    plan: str | None = None  # e.g. "Max (5x)"
    stale: bool = False
    # The OAuth token expired and only Claude Code can refresh it. Distinct
    # from `stale`: the numbers aren't late, they're unreachable until the user
    # runs Claude Code again.
    auth_expired: bool = False


@dataclass
class TokenStats:
    today_cost_usd: float = 0.0  # hypothetical API cost
    today_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    session_count: int = 0
    week_tokens: int = 0  # trailing 7 calendar days, incl. today
    week_cost_usd: float = 0.0
    stale: bool = False


@dataclass
class Activity:
    project: str | None = None
    model: str | None = None
    active: bool = False
    last_event: datetime | None = None
    burn_rate_tpm: float = 0.0  # tokens per minute, trailing window
    stale: bool = False


@dataclass
class HudState:
    now: datetime = field(default_factory=datetime.now)
    limits: Limits = field(default_factory=Limits)
    tokens: TokenStats = field(default_factory=TokenStats)
    activity: Activity = field(default_factory=Activity)
    # Hourly token buckets since midnight, for the sparkline (24 slots).
    hourly_tokens: list[int] = field(default_factory=list)


def mock_state(now: datetime | None = None) -> HudState:
    """A believable state for previews and layout tests."""
    now = now or datetime.now()
    return HudState(
        now=now,
        limits=Limits(
            session=LimitGauge(
                "Current session",
                41.0,
                now + timedelta(hours=4, minutes=29),
                window_s=5 * 3600,
            ),
            weekly=LimitGauge(
                "All models",
                33.0,
                now + timedelta(days=3, hours=16),
                window_s=7 * 86400,
            ),
            weekly_fable=LimitGauge(
                "Fable only",
                10.0,
                now + timedelta(days=3, hours=16),
                window_s=7 * 86400,
            ),
            plan="Max (5x)",
        ),
        tokens=TokenStats(
            today_cost_usd=215.75,
            today_tokens=229_200_000,
            input_tokens=521_800,
            output_tokens=404_000,
            cache_tokens=228_700_000,
            session_count=38,
            week_tokens=612_000_000,
            week_cost_usd=581.20,
        ),
        activity=Activity(
            project="trofeo-hud",
            model="Fable 5",
            active=True,
            last_event=now - timedelta(seconds=8),
            burn_rate_tpm=1_240_000,
        ),
        hourly_tokens=[
            0,
            0,
            0,
            0,
            0,
            0,
            2,
            9,
            14,
            8,
            3,
            11,
            18,
            24,
            9,
            4,
            16,
            22,
            0,
            0,
            0,
            0,
            0,
            0,
        ][: now.hour + 1],
    )
