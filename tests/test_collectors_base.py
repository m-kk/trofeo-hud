"""Collector retry pacing. A failing collector must not keep its cadence —
re-hitting a throttled endpoint every 60s is what sustains the throttle."""

from claude_trofeo_hud.collectors.base import Collector, SharedState


class _FakeStop:
    """Stands in for the stop Event: records waits, halts after `runs` loops."""

    def __init__(self, runs: int) -> None:
        self.runs = runs
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return self.runs <= 0

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        self.runs -= 1
        return False


class _Flaky(Collector):
    name_ = "flaky"
    cadence_s = 60.0

    def __init__(self, outcomes) -> None:
        super().__init__(SharedState())
        self.outcomes = list(outcomes)
        self.stale_calls = 0

    def refresh(self) -> None:
        outcome = self.outcomes.pop(0)
        if outcome is not None:
            raise outcome

    def mark_stale(self) -> None:
        self.stale_calls += 1


def _waits(outcomes) -> list[float]:
    c = _Flaky(outcomes)
    c._stop = _FakeStop(len(outcomes))
    c.run()
    return c._stop.waits


def test_healthy_collector_keeps_its_cadence():
    assert _waits([None, None, None]) == [60.0, 60.0, 60.0]


def test_repeated_failures_back_off_exponentially():
    fail = [RuntimeError("boom")] * 4
    assert _waits(fail) == [60.0, 120.0, 240.0, 480.0]


def test_backoff_is_capped():
    assert _waits([RuntimeError("boom")] * 8)[-1] == Collector.backoff_max_s


def test_success_resets_the_backoff():
    assert _waits([RuntimeError("boom"), RuntimeError("boom"), None, None]) == [
        60.0,
        120.0,
        60.0,
        60.0,
    ]


def test_server_retry_hint_wins_over_the_schedule():
    slow_down = RuntimeError("429")
    slow_down.retry_after_s = 300.0
    assert _waits([slow_down]) == [300.0]


def test_retry_hint_never_shortens_the_wait_below_cadence():
    hurry = RuntimeError("429")
    hurry.retry_after_s = 1.0
    assert _waits([hurry]) == [60.0]


def test_retry_hint_is_capped_too():
    forever = RuntimeError("429")
    forever.retry_after_s = 99_999.0
    assert _waits([forever]) == [Collector.backoff_max_s]


def test_failure_marks_the_section_stale():
    c = _Flaky([RuntimeError("boom"), None])
    c._stop = _FakeStop(2)
    c.run()
    assert c.stale_calls == 1
