"""Activity collector: today's slice of the transcript log → panel state."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from trofeo_hud.collectors import transcripts as tx
from trofeo_hud.collectors.activity import ActivityCollector, _display_model
from trofeo_hud.collectors.base import SharedState
from trofeo_hud.collectors.transcripts import UsageEvent

NOW = datetime(2026, 8, 18, 13, 59, 58).astimezone()
MIDNIGHT = NOW.replace(hour=0, minute=0, second=0, microsecond=0)


def _ev(ts, tokens=0, model="claude-fable-5", project="p", session="s1", advisor=False):
    return UsageEvent(
        ts,
        model,
        tokens,
        0,
        0,
        0,
        0,
        Path("a.jsonl"),
        project,
        session,
        None,
        advisor=advisor,
    )


class _StaticLog(tx.TranscriptLog):
    def __init__(self, events):
        super().__init__(Path("/nonexistent"))
        self._events = list(events)
        self.ingested = []

    def ingest(self, now):
        self.ingested.append(now)


def test_event_after_the_hour_rolled_over_does_not_index_error():
    """`now` is sampled before ingest; an event stamped in the next hour
    (or read from a file that grew during the scan) must not crash publish."""
    shared = SharedState()
    c = ActivityCollector(shared, log=_StaticLog([]))
    events = [_ev(NOW - timedelta(minutes=5), 100), _ev(NOW + timedelta(seconds=5), 7)]
    c._publish(events, NOW, MIDNIGHT)
    buckets = shared.snapshot().hourly_tokens
    assert len(buckets) == 15  # hours 0..14 — the late event's hour is kept
    assert buckets[13] == 100 and buckets[14] == 7


def test_bucket_count_tracks_the_current_hour_when_no_event_is_later():
    shared = SharedState()
    c = ActivityCollector(shared, log=_StaticLog([]))
    now = NOW.replace(hour=9, minute=30)
    c._publish([_ev(now - timedelta(hours=2), 5)], now, MIDNIGHT)
    assert shared.snapshot().hourly_tokens == [0, 0, 0, 0, 0, 0, 0, 5, 0, 0]


def test_refresh_ingests_then_publishes_todays_events_only(monkeypatch):
    monkeypatch.setattr(tx, "local_now", lambda: NOW)
    log = _StaticLog(
        [
            _ev(NOW - timedelta(days=1), 1_000, session="yesterday"),
            _ev(NOW - timedelta(minutes=30), 200, project="alpha", session="a"),
            _ev(
                NOW - timedelta(seconds=30),
                300,
                model="claude-opus-5",
                project="beta",
                session="b",
            ),
        ]
    )
    shared = SharedState()
    ActivityCollector(shared, log=log).refresh()
    s = shared.snapshot()
    assert log.ingested == [NOW]
    assert s.activity.project == "beta" and s.activity.model == "Opus 5"
    assert s.activity.active is True
    assert s.activity.last_event == NOW - timedelta(seconds=30)
    assert (
        s.activity.burn_rate_tpm == 300 / 10
    )  # only the 30-second-old event is in the window
    assert s.tokens.session_count == 2
    assert sum(s.hourly_tokens) == 500


def test_idle_when_the_latest_event_is_older_than_the_active_window():
    shared = SharedState()
    c = ActivityCollector(shared, log=_StaticLog([]))
    c._publish([_ev(NOW - timedelta(minutes=3), 1)], NOW, MIDNIGHT)
    assert shared.snapshot().activity.active is False


def test_advisor_events_count_tokens_but_do_not_set_the_current_model():
    shared = SharedState()
    c = ActivityCollector(shared, log=_StaticLog([]))
    events = [
        _ev(NOW - timedelta(seconds=10), 10, model="claude-fable-5"),
        _ev(NOW - timedelta(seconds=5), 90, model="claude-opus-5", advisor=True),
    ]
    c._publish(events, NOW, MIDNIGHT)
    s = shared.snapshot()
    assert s.activity.model == "Fable 5"
    assert s.activity.burn_rate_tpm == 100 / 10
    assert sum(s.hourly_tokens) == 100


def test_only_advisor_events_still_yield_an_activity_reading():
    shared = SharedState()
    c = ActivityCollector(shared, log=_StaticLog([]))
    c._publish([_ev(NOW, 5, model="claude-opus-5", advisor=True)], NOW, MIDNIGHT)
    assert shared.snapshot().activity.model == "Opus 5"


def test_no_events_publishes_an_empty_activity():
    shared = SharedState()
    c = ActivityCollector(shared, log=_StaticLog([]))
    c._publish([], NOW, MIDNIGHT)
    s = shared.snapshot()
    assert s.activity.project is None and s.activity.active is False
    assert s.hourly_tokens == [0] * 14
    assert s.tokens.session_count == 0


def test_mark_stale_flags_without_dropping_the_reading():
    shared = SharedState()
    c = ActivityCollector(shared, log=_StaticLog([]))
    c._publish([_ev(NOW, 1, project="keep")], NOW, MIDNIGHT)
    c.mark_stale()
    a = shared.snapshot().activity
    assert a.stale is True and a.project == "keep"


def test_display_model_names():
    assert _display_model("claude-fable-5") == "Fable 5"
    assert _display_model("claude-haiku-4-5-20251001") == "Haiku 4.5"
    assert _display_model("claude-something-new") == "something-new"


def test_default_log_reads_the_projects_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(tx, "PROJECTS_DIR", tmp_path)
    assert ActivityCollector(SharedState()).log.root == tmp_path
