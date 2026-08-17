"""Collector scaffolding: each collector refreshes on its own thread at its
own cadence and publishes into a shared, locked HudState. A failed refresh
keeps the last-good value and flips the section's stale flag."""

from __future__ import annotations

import logging
import threading

from ..state import HudState

log = logging.getLogger(__name__)


class SharedState:
    def __init__(self) -> None:
        self._state = HudState()
        self._lock = threading.Lock()

    def snapshot(self) -> HudState:
        """A shallow-ish copy safe for rendering (sections are replaced
        wholesale by collectors, never mutated in place)."""
        import copy
        from datetime import datetime

        with self._lock:
            snap = copy.copy(self._state)
        snap.now = datetime.now()
        return snap

    def update(self, **sections) -> None:
        with self._lock:
            for name, value in sections.items():
                setattr(self._state, name, value)

    def mutate(self, fn) -> None:
        """Run `fn(state)` under the lock — for read-modify-write updates
        that span sections (plain `update` would race between collectors)."""
        with self._lock:
            fn(self._state)


class Collector(threading.Thread):
    """Runs `refresh()` every `cadence_s`, marks its section stale on error.

    A failing collector backs off instead of keeping cadence: a source that is
    down stays down for a while, and one that is *rate-limiting us* is only
    made worse by knocking every 60s. An exception may carry a
    `retry_after_s` attribute (from a server's Retry-After) to set the wait.
    """

    name_: str = "collector"
    cadence_s: float = 60.0
    backoff_max_s: float = 900.0

    def __init__(self, shared: SharedState) -> None:
        super().__init__(daemon=True, name=self.name_)
        self.shared = shared
        self._stop = threading.Event()

    def refresh(self) -> None:  # writes via self.shared.update(...)
        raise NotImplementedError

    def mark_stale(self) -> None:
        """Best-effort: flip this collector's section(s) stale."""

    def run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                self.refresh()
                failures = 0
                wait = self.cadence_s
            except Exception as exc:
                failures += 1
                wait = self._retry_wait(exc, failures)
                # One line per failure; the traceback is a debug-level detail.
                # A source that stays down for hours shouldn't fill the log
                # with identical stacks.
                log.warning(
                    "%s refresh failed (%s) — retry in %.0fs", self.name_, exc, wait
                )
                log.debug("%s refresh traceback", self.name_, exc_info=True)
                self.mark_stale()
            self._stop.wait(wait)

    def _retry_wait(self, exc: Exception, failures: int) -> float:
        hint = getattr(exc, "retry_after_s", None)
        backoff = self.cadence_s * 2 ** (failures - 1)
        if hint:
            backoff = max(float(hint), self.cadence_s)
        return min(backoff, self.backoff_max_s)

    def stop(self) -> None:
        self._stop.set()
