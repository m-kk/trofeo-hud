"""Live activity + sparkline from the transcript log. 5s cadence.

This collector is the one that drives ingestion (it ticks fastest); the
others read what it has already pulled in. Everything here is today's slice
of the shared log: latest project/model, active flag, trailing burn rate,
hourly buckets for the sparkline, and the session count.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timedelta

from ..state import Activity
from . import transcripts as tx
from .base import Collector

log = logging.getLogger(__name__)

ACTIVE_WINDOW_S = 120  # "active" = assistant event in the last 2 min
BURN_WINDOW_MIN = 10

# Model id → short display name; unknown ids fall back to the raw id tail.
_MODEL_NAMES = {
    "claude-fable-5": "Fable 5",
    "claude-opus-5": "Opus 5",
    "claude-sonnet-5": "Sonnet 5",
    "claude-haiku-4-5": "Haiku 4.5",
}


def _display_model(model_id: str) -> str:
    for prefix, name in _MODEL_NAMES.items():
        if model_id.startswith(prefix):
            return name
    return model_id.removeprefix("claude-")


class ActivityCollector(Collector):
    name_ = "activity"
    cadence_s = 5.0

    def __init__(self, shared, log: tx.TranscriptLog | None = None) -> None:
        super().__init__(shared)
        self.log = log if log is not None else tx.TranscriptLog(tx.PROJECTS_DIR)

    def refresh(self) -> None:
        now = tx.local_now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.log.ingest(now)
        self._publish(self.log.events(since=midnight), now, midnight)

    def _publish(
        self, events: list[tx.UsageEvent], now: datetime, midnight: datetime
    ) -> None:
        act = Activity()
        # An advisor call is a side request inside a turn; the turn's own
        # model is what the user is "on".
        replies = [e for e in events if not e.advisor] or events
        if replies:
            latest = max(replies, key=lambda e: e.ts)
            act.project = latest.project
            act.model = _display_model(latest.model)
            act.last_event = latest.ts
            act.active = (now - latest.ts).total_seconds() < ACTIVE_WINDOW_S

            burn_start = now - timedelta(minutes=BURN_WINDOW_MIN)
            burned = sum(e.total for e in events if e.ts >= burn_start)
            act.burn_rate_tpm = burned / BURN_WINDOW_MIN

        # `now` was sampled before the scan; an event stamped in the hour that
        # began during it must widen the list rather than fall off its end.
        hours = max([now.hour] + [e.ts.hour for e in events]) + 1
        buckets = [0] * hours
        for e in events:
            buckets[e.ts.hour] += e.total

        sessions_today = len({e.session for e in events})

        def apply(state) -> None:
            state.activity = act
            state.hourly_tokens = buckets
            state.tokens = dataclasses.replace(
                state.tokens, session_count=sessions_today
            )

        self.shared.mutate(apply)

    def mark_stale(self) -> None:
        def apply(state) -> None:
            state.activity = dataclasses.replace(state.activity, stale=True)

        self.shared.mutate(apply)
