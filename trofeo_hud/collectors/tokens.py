"""Tokens & hypothetical cost from the transcript log. 60s cadence.

Reads the shared `TranscriptLog` (deduped, subagents and advisor calls
included) and prices each event at Anthropic list rates. Days are local
calendar days; the week is the trailing seven of them, so the figure sits
honestly beside the rolling 7-day gauge.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import timedelta

from .. import pricing
from ..state import TokenStats
from . import transcripts as tx
from .base import Collector

log = logging.getLogger(__name__)


class TokensCollector(Collector):
    name_ = "tokens"
    cadence_s = 60.0

    def __init__(self, shared, log: tx.TranscriptLog | None = None) -> None:
        super().__init__(shared)
        self.log = log if log is not None else tx.TranscriptLog(tx.PROJECTS_DIR)

    def refresh(self) -> None:
        now = tx.local_now()
        self.log.ingest(now)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = midnight - timedelta(days=6)

        stats = TokenStats()
        for ev in self.log.events(since=week_start):
            cost = pricing.cost_usd(ev)
            stats.week_tokens += ev.total
            stats.week_cost_usd += cost
            if ev.ts >= midnight:
                stats.today_tokens += ev.total
                stats.today_cost_usd += cost
                stats.input_tokens += ev.input
                stats.output_tokens += ev.output
                stats.cache_tokens += ev.cache_read + ev.cache_write

        def apply(state) -> None:
            stats.session_count = state.tokens.session_count  # activity owns it
            state.tokens = stats

        self.shared.mutate(apply)
        log.debug("tokens: today=%s week=%s", stats.today_tokens, stats.week_tokens)

    def mark_stale(self) -> None:
        def apply(state) -> None:
            state.tokens = dataclasses.replace(state.tokens, stale=True)

        self.shared.mutate(apply)
