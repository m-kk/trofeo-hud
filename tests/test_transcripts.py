"""TranscriptLog: the one reader over ~/.claude/projects.

The dedupe test is the point of this file. Claude Code writes one API message
as several JSONL lines — one per content block — each carrying the same
`message.id`, `requestId` and an identical `usage` object. Counting lines
counts the same tokens two or three times (48% of the lines in a real log
were such repeats). ccusage dedupes on `messageId:requestId`; so do we.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trofeo_hud.collectors.transcripts import TranscriptLog, UsageEvent

T0 = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _line(
    ts: datetime,
    *,
    msg_id="msg_1",
    req="req_1",
    model="claude-fable-5",
    usage=None,
    entry_type="assistant",
    cwd="/Users/x/dev/proj",
    session="sess-1",
    **extra,
) -> str:
    usage = (
        usage
        if usage is not None
        else {
            "input_tokens": 2,
            "output_tokens": 30,
            "cache_creation_input_tokens": 400,
            "cache_read_input_tokens": 5000,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 100,
                "ephemeral_1h_input_tokens": 300,
            },
        }
    )
    entry = {
        "type": entry_type,
        "timestamp": ts.isoformat().replace("+00:00", "Z"),
        "requestId": req,
        "cwd": cwd,
        "sessionId": session,
        "message": {"id": msg_id, "model": model, "role": "assistant", "usage": usage},
        **extra,
    }
    return json.dumps(entry) + "\n"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "-Users-x-dev-proj").mkdir()
    return tmp_path


def _write(root: Path, rel: str, text: str, mode="w") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open(mode) as fh:
        fh.write(text)
    return p


def test_parses_one_assistant_line_into_a_usage_event(root):
    _write(root, "-Users-x-dev-proj/s1.jsonl", _line(T0))
    log = TranscriptLog(root)
    log.ingest(T0 + timedelta(minutes=1))
    (ev,) = log.events()
    assert ev.ts == T0
    assert ev.model == "claude-fable-5"
    assert (ev.input, ev.output, ev.cache_read) == (2, 30, 5000)
    assert (ev.cache_write_5m, ev.cache_write_1h) == (100, 300)
    assert ev.total == 2 + 30 + 400 + 5000
    assert ev.project == "proj"
    assert ev.session == "sess-1"


def test_duplicate_message_lines_are_counted_once(root):
    """Same message.id + requestId → the same API call, written twice."""
    _write(
        root, "-Users-x-dev-proj/s1.jsonl", _line(T0) + _line(T0 + timedelta(seconds=2))
    )
    log = TranscriptLog(root)
    log.ingest(T0 + timedelta(minutes=1))
    assert len(log.events()) == 1


def test_different_request_ids_are_distinct_even_with_same_message_id(root):
    _write(
        root,
        "-Users-x-dev-proj/s1.jsonl",
        _line(T0, req="req_1") + _line(T0, req="req_2"),
    )
    log = TranscriptLog(root)
    log.ingest(T0)
    assert len(log.events()) == 2


def test_lines_without_ids_are_not_deduped_against_each_other(root):
    """Synthetic / legacy entries carry no ids; two of them are two events."""
    a = _line(T0, msg_id=None, req=None, model="<synthetic>")
    b = _line(T0 + timedelta(seconds=1), msg_id=None, req=None, model="<synthetic>")
    _write(root, "-Users-x-dev-proj/s1.jsonl", a + b)
    log = TranscriptLog(root)
    log.ingest(T0)
    assert len(log.events()) == 2


def test_incremental_reads_resume_from_the_last_offset(root):
    p = _write(root, "-Users-x-dev-proj/s1.jsonl", _line(T0, msg_id="m1", req="r1"))
    log = TranscriptLog(root)
    log.ingest(T0)
    _write(
        root, "-Users-x-dev-proj/s1.jsonl", _line(T0, msg_id="m2", req="r2"), mode="a"
    )
    log.ingest(T0)
    assert sorted(e.message_id for e in log.events()) == ["m1", "m2"]
    assert log._offsets[p] == p.stat().st_size


def test_partial_trailing_line_is_left_for_the_next_tick(root):
    full = _line(T0, msg_id="m1", req="r1")
    partial = _line(T0, msg_id="m2", req="r2")[:-20]  # no newline, truncated JSON
    p = _write(root, "-Users-x-dev-proj/s1.jsonl", full + partial)
    log = TranscriptLog(root)
    log.ingest(T0)
    assert [e.message_id for e in log.events()] == ["m1"]
    assert log._offsets[p] == len(full.encode())
    # Only a partial line, no newline at all: nothing consumed.
    p2 = _write(root, "-Users-x-dev-proj/s2.jsonl", partial)
    log.ingest(T0)
    assert p2 not in log._offsets or log._offsets[p2] == 0


def test_truncated_file_is_reread_from_the_start(root):
    p = _write(root, "-Users-x-dev-proj/s1.jsonl", _line(T0, msg_id="m1", req="r1") * 3)
    log = TranscriptLog(root)
    log.ingest(T0)
    p.write_text(_line(T0, msg_id="m9", req="r9"))
    log.ingest(T0)
    assert "m9" in {e.message_id for e in log.events()}


def test_subagent_transcripts_under_the_session_directory_are_read(root):
    _write(root, "-Users-x-dev-proj/s1.jsonl", _line(T0, msg_id="m1", req="r1"))
    _write(
        root,
        "-Users-x-dev-proj/s1/subagents/agent-abc.jsonl",
        _line(T0, msg_id="m2", req="r2", isSidechain=True),
    )
    log = TranscriptLog(root)
    log.ingest(T0)
    assert len(log.events()) == 2


def test_non_assistant_malformed_and_unstamped_lines_are_skipped(root):
    text = (
        _line(T0, entry_type="user")
        + '{"type":"assistant", this is not json\n'
        + '{"type":"assistant","message":{"id":"m","usage":{}}}\n'  # no timestamp
        + '{"type":"assistant","timestamp":"not-a-date","message":{"id":"m","usage":{}}}\n'
        + '"assistant"\n'  # valid JSON, wrong shape
        + _line(T0, msg_id="ok", req="ok")
    )
    _write(root, "-Users-x-dev-proj/s1.jsonl", text)
    log = TranscriptLog(root)
    log.ingest(T0)
    assert [e.message_id for e in log.events()] == ["ok"]


def test_missing_usage_and_cwd_degrade_to_zero_and_directory_name(root):
    entry = {
        "type": "assistant",
        "timestamp": T0.isoformat(),
        "message": {"id": "m", "model": "claude-opus-5"},
    }
    _write(root, "-Users-x-dev-proj/s1.jsonl", json.dumps(entry) + "\n")
    log = TranscriptLog(root)
    log.ingest(T0)
    (ev,) = log.events()
    assert ev.total == 0
    assert ev.project == "-Users-x-dev-proj"
    assert ev.session == "s1"  # file stem when the entry has no sessionId


def test_cache_creation_without_breakdown_counts_as_five_minute_writes(root):
    usage = {
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_creation_input_tokens": 50,
        "cache_read_input_tokens": 0,
    }
    _write(root, "-Users-x-dev-proj/s1.jsonl", _line(T0, usage=usage))
    log = TranscriptLog(root)
    log.ingest(T0)
    (ev,) = log.events()
    assert (ev.cache_write_5m, ev.cache_write_1h) == (50, 0)


def test_files_older_than_the_retention_window_are_not_opened(root, monkeypatch):
    p = _write(root, "-Users-x-dev-proj/old.jsonl", _line(T0 - timedelta(days=30)))
    import os

    old = (T0 - timedelta(days=30)).timestamp()
    os.utime(p, (old, old))
    log = TranscriptLog(root, retention=timedelta(days=8))
    log.ingest(T0)
    assert log.events() == []
    assert p not in log._offsets


def test_events_older_than_retention_are_pruned_and_can_reappear_as_new(root):
    """Pruning must also forget the dedupe key, or a re-ingested old line
    (e.g. after a truncation reset) would be silently dropped forever."""
    _write(root, "-Users-x-dev-proj/s1.jsonl", _line(T0, msg_id="m1", req="r1"))
    log = TranscriptLog(root, retention=timedelta(days=1))
    log.ingest(T0)
    assert len(log.events()) == 1
    log.ingest(T0 + timedelta(days=2))
    assert log.events() == []
    assert not log._seen


def test_events_since_filters_by_timestamp(root):
    _write(
        root,
        "-Users-x-dev-proj/s1.jsonl",
        _line(T0, msg_id="a", req="a")
        + _line(T0 + timedelta(hours=1), msg_id="b", req="b"),
    )
    log = TranscriptLog(root)
    log.ingest(T0 + timedelta(hours=1))
    assert [e.message_id for e in log.events(since=T0 + timedelta(minutes=30))] == ["b"]


def test_unreadable_file_is_skipped(root, monkeypatch):
    p = _write(root, "-Users-x-dev-proj/s1.jsonl", _line(T0))
    log = TranscriptLog(root)
    real_open = Path.open

    def boom(self, *a, **k):
        if self == p:
            raise OSError("nope")
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", boom)
    log.ingest(T0)
    assert log.events() == []


def test_missing_root_yields_nothing(tmp_path):
    log = TranscriptLog(tmp_path / "absent")
    log.ingest(T0)
    assert log.events() == []


def test_usage_event_total():
    ev = UsageEvent(T0, "m", 1, 2, 3, 4, 5, Path("f"), "p", "s", "id")
    assert ev.total == 15
    assert ev.cache_write == 7


def test_advisor_iterations_become_their_own_events_at_their_own_model(root):
    """The top-level `usage` is the sum of the `message` iterations only; an
    `advisor_message` iteration is a separate billed call to (possibly) a
    different model, and Claude Code leaves it out of the top-level totals."""
    usage = {
        "input_tokens": 4,
        "output_tokens": 946,
        "cache_creation_input_tokens": 4814,
        "cache_read_input_tokens": 173_469,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 2021,
            "ephemeral_1h_input_tokens": 2793,
        },
        "iterations": [
            {
                "type": "message",
                "input_tokens": 2,
                "output_tokens": 833,
                "cache_read_input_tokens": 85_338,
                "cache_creation_input_tokens": 2793,
            },
            {
                "type": "advisor_message",
                "model": "claude-opus-5",
                "input_tokens": 90_314,
                "output_tokens": 5644,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
            {
                "type": "message",
                "input_tokens": 2,
                "output_tokens": 113,
                "cache_read_input_tokens": 88_131,
                "cache_creation_input_tokens": 2021,
            },
        ],
    }
    _write(
        root, "-Users-x-dev-proj/s1.jsonl", _line(T0, usage=usage) * 2
    )  # written twice
    log = TranscriptLog(root)
    log.ingest(T0)
    main, adv = sorted(log.events(), key=lambda e: e.advisor)
    assert not main.advisor and main.model == "claude-fable-5"
    assert (main.input, main.output, main.cache_read) == (4, 946, 173_469)
    assert (main.cache_write_5m, main.cache_write_1h) == (2021, 2793)
    assert adv.advisor and adv.model == "claude-opus-5"
    assert (adv.input, adv.output, adv.cache_read, adv.cache_write) == (
        90_314,
        5644,
        0,
        0,
    )
    assert adv.ts == main.ts and adv.session == main.session


def test_advisor_iteration_without_a_model_inherits_the_entry_model(root):
    usage = {
        "input_tokens": 1,
        "output_tokens": 1,
        "iterations": [
            {"type": "message", "input_tokens": 1, "output_tokens": 1},
            {"type": "advisor_message", "input_tokens": 7, "output_tokens": 3},
        ],
    }
    _write(root, "-Users-x-dev-proj/s1.jsonl", _line(T0, usage=usage))
    log = TranscriptLog(root)
    log.ingest(T0)
    adv = [e for e in log.events() if e.advisor]
    assert len(adv) == 1 and adv[0].model == "claude-fable-5" and adv[0].total == 10


def test_a_file_that_vanishes_between_listing_and_stat_is_skipped(root, monkeypatch):
    p = _write(root, "-Users-x-dev-proj/s1.jsonl", _line(T0))
    real_stat = Path.stat

    def stat(self, *a, **k):
        if self == p:
            raise OSError("gone")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", stat)
    log = TranscriptLog(root)
    log.ingest(T0)
    assert log.events() == []


def test_unchanged_file_is_not_reread(root, monkeypatch):
    p = _write(root, "-Users-x-dev-proj/s1.jsonl", _line(T0))
    log = TranscriptLog(root)
    log.ingest(T0)
    real_open = Path.open

    def opened(self, *a, **k):
        if self == p:
            raise AssertionError("re-read an unchanged file")
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", opened)
    log.ingest(T0)
    assert len(log.events()) == 1


def test_lines_that_cannot_be_assistant_entries_are_not_parsed(root):
    _write(
        root,
        "-Users-x-dev-proj/s1.jsonl",
        '{"type":"summary","summary":"x"}\n' + _line(T0),
    )
    log = TranscriptLog(root)
    log.ingest(T0)
    assert len(log.events()) == 1


def test_old_events_in_a_recent_file_are_dropped_at_ingest(root):
    text = _line(T0 - timedelta(days=30), msg_id="old", req="old") + _line(
        T0, msg_id="new", req="new"
    )
    _write(root, "-Users-x-dev-proj/s1.jsonl", text)
    log = TranscriptLog(root, retention=timedelta(days=8))
    log.ingest(T0)
    assert [e.message_id for e in log.events()] == ["new"]
