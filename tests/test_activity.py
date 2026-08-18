"""Activity collector: hourly buckets survive an hour boundary mid-refresh."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from trofeo_hud.collectors.activity import ActivityCollector, _Event
from trofeo_hud.collectors.base import SharedState


def test_event_after_the_hour_rolled_over_does_not_index_error():
    """`now` is sampled before ingest; an event stamped in the next hour
    (or read from a file that grew during the scan) must not crash publish."""
    shared = SharedState()
    c = ActivityCollector(shared)
    now = datetime(2026, 8, 18, 13, 59, 58).astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    c._events = [
        _Event(now - timedelta(minutes=5), 100, "claude-fable-5", Path("a.jsonl"), "p"),
        _Event(now + timedelta(seconds=5), 7, "claude-fable-5", Path("a.jsonl"), "p"),
    ]
    c._publish(now, midnight)
    buckets = shared.snapshot().hourly_tokens
    assert len(buckets) == 15  # hours 0..14 — the late event's hour is kept
    assert buckets[13] == 100 and buckets[14] == 7


def test_bucket_count_tracks_the_current_hour_when_no_event_is_later():
    shared = SharedState()
    c = ActivityCollector(shared)
    now = datetime(2026, 8, 18, 9, 30).astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    c._events = [_Event(now - timedelta(hours=2), 5, "m", Path("a.jsonl"), "p")]
    c._publish(now, midnight)
    assert shared.snapshot().hourly_tokens == [0, 0, 0, 0, 0, 0, 0, 5, 0, 0]
