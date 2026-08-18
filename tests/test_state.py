"""LimitGauge pace maths — the input to the pace tick on the bars."""

from datetime import datetime, timedelta

from trofeo_hud.state import LimitGauge

_WEEK_S = 7 * 86400
NOW = datetime(2026, 8, 17, 12, 0)


def test_elapsed_pct_is_none_without_a_window():
    g = LimitGauge("WEEK", 10.0, resets_at=NOW + timedelta(days=3))
    assert g.elapsed_pct(NOW) is None


def test_elapsed_pct_is_none_without_a_reset_time():
    g = LimitGauge("WEEK", 10.0, window_s=_WEEK_S)
    assert g.elapsed_pct(NOW) is None


def test_elapsed_pct_halfway_through_the_window():
    g = LimitGauge("WEEK", 10.0, resets_at=NOW + timedelta(days=3.5), window_s=_WEEK_S)
    assert g.elapsed_pct(NOW) == 50.0


def test_elapsed_pct_clamps_at_both_ends():
    fresh = LimitGauge("WEEK", 0.0, resets_at=NOW + timedelta(days=9), window_s=_WEEK_S)
    overdue = LimitGauge(
        "WEEK", 0.0, resets_at=NOW - timedelta(hours=2), window_s=_WEEK_S
    )
    assert fresh.elapsed_pct(NOW) == 0.0
    assert overdue.elapsed_pct(NOW) == 100.0
