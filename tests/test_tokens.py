"""Tokens collector: today + trailing-7-day totals and cost from the shared
transcript log — no subprocess, no Node.

Days are *local* calendar days: the panel says TODAY and the user's day is
the one their clock shows, not UTC's.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trofeo_hud.collectors import transcripts as tx
from trofeo_hud.collectors.base import SharedState
from trofeo_hud.collectors.tokens import TokensCollector
from trofeo_hud.collectors.transcripts import TranscriptLog, UsageEvent

NOW = datetime(2026, 8, 17, 15, 30).astimezone()  # local


def _ev(
    ts,
    model="claude-opus-5",
    inp=0,
    out=0,
    w5=0,
    w1=0,
    rd=0,
    session="s",
    advisor=False,
):
    return UsageEvent(
        ts,
        model,
        inp,
        out,
        w5,
        w1,
        rd,
        Path("f"),
        "p",
        session,
        None,
        key=None,
        advisor=advisor,
    )


class _StaticLog(TranscriptLog):
    """A log with canned events; `ingest` records that it was called."""

    def __init__(self, events):
        super().__init__(Path("/nonexistent"))
        self._events = list(events)
        self.ingested = []

    def ingest(self, now):
        self.ingested.append(now)


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    monkeypatch.setattr(tx, "local_now", lambda: NOW)


def test_today_and_week_totals_are_local_calendar_days():
    events = [
        # today, main model: $5/$25 in/out; 5m write ×1.25, read ×0.1 of $5
        _ev(
            NOW - timedelta(hours=1),
            inp=1_000_000,
            out=100_000,
            w5=200_000,
            rd=3_000_000,
        ),
        # earlier today, a Fable call
        _ev(NOW.replace(hour=0, minute=5), model="claude-fable-5", out=10_000),
        # yesterday
        _ev(NOW - timedelta(days=1), inp=500_000),
        # six days ago at 00:01 local — inside the trailing 7 calendar days
        _ev((NOW - timedelta(days=6)).replace(hour=0, minute=1), out=1_000),
        # seven days ago at 23:59 — outside
        _ev((NOW - timedelta(days=7)).replace(hour=23, minute=59), inp=9_000_000),
    ]
    log = _StaticLog(events)
    c = TokensCollector(SharedState(), log=log)
    c.refresh()
    t = c.shared.snapshot().tokens

    assert log.ingested == [NOW]
    assert t.input_tokens == 1_000_000
    assert t.output_tokens == 110_000
    assert t.cache_tokens == 200_000 + 3_000_000
    assert t.today_tokens == 1_000_000 + 110_000 + 3_200_000
    assert t.today_cost_usd == pytest.approx(5.0 + 2.5 + 1.25 + 1.5 + 0.5)
    assert t.week_tokens == t.today_tokens + 500_000 + 1_000
    assert t.week_cost_usd == pytest.approx(t.today_cost_usd + 2.5 + 0.025)
    assert t.stale is False


def test_advisor_events_count_toward_tokens_and_cost():
    log = _StaticLog([_ev(NOW, model="claude-opus-5", inp=1_000_000, advisor=True)])
    c = TokensCollector(SharedState(), log=log)
    c.refresh()
    t = c.shared.snapshot().tokens
    assert t.today_tokens == 1_000_000 and t.today_cost_usd == pytest.approx(5.0)


def test_no_events_yields_zeros_not_an_error():
    c = TokensCollector(SharedState(), log=_StaticLog([]))
    c.refresh()
    t = c.shared.snapshot().tokens
    assert (t.today_tokens, t.week_tokens, t.today_cost_usd) == (0, 0, 0.0)


def test_session_count_is_left_to_the_activity_collector():
    """Both collectors write TokenStats, so tokens must preserve the field
    activity owns rather than resetting it to zero."""
    c = TokensCollector(SharedState(), log=_StaticLog([_ev(NOW, inp=1)]))

    def seed(state):
        state.tokens.session_count = 38

    c.shared.mutate(seed)
    c.refresh()
    assert c.shared.snapshot().tokens.session_count == 38


def test_mark_stale_keeps_last_good_values():
    c = TokensCollector(SharedState(), log=_StaticLog([_ev(NOW, inp=7)]))
    c.refresh()
    c.mark_stale()
    t = c.shared.snapshot().tokens
    assert t.stale is True
    assert t.today_tokens == 7, "last-good value must survive"


def test_default_log_reads_the_projects_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(tx, "PROJECTS_DIR", tmp_path)
    c = TokensCollector(SharedState())
    assert c.log.root == tmp_path


def test_local_now_is_timezone_aware(monkeypatch):
    monkeypatch.undo()
    assert tx.local_now().tzinfo is not None
