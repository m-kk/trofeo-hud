"""One reader over Claude Code's transcripts, shared by every collector.

`~/.claude/projects/**/*.jsonl` is append-only, so each file is read from the
byte offset reached last time and only complete lines are consumed. Every
`assistant` line becomes a `UsageEvent` — except repeats: Claude Code writes
one API message as several lines (one per content block), each carrying the
same `message.id` + `requestId` and an identical `usage` object. Counting
lines counts the same tokens two or three times, so events are keyed on that
pair and duplicates dropped, as ccusage does.

Subagent transcripts live one level down (`<session>/subagents/*.jsonl`) and
are included; their entries carry the parent's `sessionId`.

A `usage.iterations[]` list, when present, itemises the API calls behind one
assistant turn. The top-level `usage` is the sum of the `message` iterations
only; an `advisor_message` iteration is a separate billed call — often to a
different model — that Claude Code leaves out of the top-level totals. Those
become their own events, marked `advisor`, priced at their own model.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

PROJECTS_DIR = Path.home() / ".claude" / "projects"
# A week of history plus a day's slack for the trailing-7-calendar-day sum.
DEFAULT_RETENTION = timedelta(days=8)


def local_now() -> datetime:
    """Timezone-aware local time; one seam for every collector's clock."""
    return datetime.now().astimezone()


@dataclasses.dataclass(frozen=True)
class UsageEvent:
    ts: datetime  # timezone-aware
    model: str
    input: int
    output: int
    cache_write_5m: int
    cache_write_1h: int
    cache_read: int
    file: Path
    project: str
    session: str
    message_id: str | None
    key: tuple | None = None  # dedupe key; None when the entry has no ids
    advisor: bool = False  # an advisor_message iteration, not the main reply

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_write + self.cache_read


class TranscriptLog:
    def __init__(
        self, root: Path = PROJECTS_DIR, retention: timedelta = DEFAULT_RETENTION
    ) -> None:
        self.root = root
        self.retention = retention
        self._lock = threading.Lock()
        self._offsets: dict[Path, int] = {}
        self._events: list[UsageEvent] = []
        self._seen: set[tuple] = set()

    def ingest(self, now: datetime) -> None:
        """Read whatever is new, then drop what has aged out of retention."""
        cutoff = now.astimezone() - self.retention
        with self._lock:
            for f in self._files(cutoff):
                self._ingest_file(f, cutoff)
            self._prune(cutoff)

    def events(self, since: datetime | None = None) -> list[UsageEvent]:
        with self._lock:
            if since is None:
                return list(self._events)
            return [e for e in self._events if e.ts >= since]

    # ── internals ────────────────────────────────────────────────────────

    def _files(self, cutoff: datetime) -> list[Path]:
        if not self.root.is_dir():
            return []
        out = []
        for f in self.root.rglob("*.jsonl"):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime).astimezone()
            except OSError:
                continue
            if mtime >= cutoff:
                out.append(f)
        return out

    def _ingest_file(self, f: Path, cutoff: datetime) -> None:
        offset = self._offsets.get(f, 0)
        try:
            size = f.stat().st_size
            if size < offset:
                offset = 0  # rewritten/truncated: start over (dedupe absorbs repeats)
            if size == offset:
                return
            with f.open("rb") as fh:
                fh.seek(offset)
                data = fh.read()
        except OSError:
            return
        last_nl = data.rfind(b"\n")
        if last_nl < 0:
            return  # only a partial line so far; re-read next tick
        self._offsets[f] = offset + last_nl + 1
        for line in data[:last_nl].split(b"\n"):
            if b'"assistant"' not in line:
                continue
            for ev in _parse(line, f):
                if ev.ts < cutoff:
                    continue
                if ev.key is not None:
                    if ev.key in self._seen:
                        continue
                    self._seen.add(ev.key)
                self._events.append(ev)

    def _prune(self, cutoff: datetime) -> None:
        keep = [e for e in self._events if e.ts >= cutoff]
        if len(keep) != len(self._events):
            self._events = keep
            self._seen = {e.key for e in keep if e.key is not None}


def _parse(line: bytes, f: Path) -> list[UsageEvent]:
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return []
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return []
    try:
        ts = datetime.fromisoformat(entry["timestamp"]).astimezone()
    except (KeyError, TypeError, ValueError):
        return []
    msg = entry.get("message") or {}
    usage = msg.get("usage") or {}
    msg_id, req_id = msg.get("id"), entry.get("requestId")
    key = (msg_id, req_id) if msg_id and req_id else None
    cwd = entry.get("cwd")
    common = dict(
        ts=ts,
        file=f,
        project=Path(cwd).name if cwd else f.parent.name,
        session=entry.get("sessionId") or f.stem,
        message_id=msg_id,
    )
    model = msg.get("model") or ""
    events = [UsageEvent(model=model, key=key, **_tokens(usage), **common)]
    for i, it in enumerate(usage.get("iterations") or []):
        if not isinstance(it, dict) or it.get("type") == "message":
            continue
        events.append(
            UsageEvent(
                model=it.get("model") or model,
                key=(*key, "iter", i) if key else None,
                advisor=True,
                **_tokens(it),
                **common,
            )
        )
    return events


def _tokens(usage: dict) -> dict:
    def n(key: str, src: dict = usage) -> int:
        return int(src.get(key) or 0)

    creation = usage.get("cache_creation") or {}
    if creation:
        w5 = n("ephemeral_5m_input_tokens", creation)
        w1 = n("ephemeral_1h_input_tokens", creation)
    else:
        w5, w1 = n("cache_creation_input_tokens"), 0
    return dict(
        input=n("input_tokens"),
        output=n("output_tokens"),
        cache_write_5m=w5,
        cache_write_1h=w1,
        cache_read=n("cache_read_input_tokens"),
    )
