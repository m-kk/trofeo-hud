"""Parsing of GET /api/oauth/usage into Limits — no network involved.

Shapes here mirror docs/usage-endpoint.md, including the account where the
top-level per-model keys are null and the Fable cap exists only in limits[].
"""

import urllib.error
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from claude_trofeo_hud.collectors import limits as mod
from claude_trofeo_hud.collectors.base import SharedState
from claude_trofeo_hud.collectors.limits import (
    _WEEK_WINDOW_S,
    Throttled,
    parse_usage,
    plan_label,
    retry_after_s,
)

_SAMPLE = {
    "five_hour": {"utilization": 41.0, "resets_at": "2026-08-17T19:10:00.084456+00:00"},
    "seven_day": {"utilization": 33.0, "resets_at": "2026-08-21T14:00:00+00:00"},
    "seven_day_opus": None,
    "seven_day_sonnet": None,
    "limits": [
        {
            "kind": "session",
            "group": "session",
            "percent": 41,
            "resets_at": "2026-08-17T19:10:00.084456+00:00",
            "scope": None,
        },
        {
            "kind": "weekly_all",
            "group": "weekly",
            "percent": 33,
            "resets_at": "2026-08-21T14:00:00+00:00",
            "scope": None,
        },
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 10,
            "resets_at": "2026-08-21T14:00:00+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
        },
    ],
}


def test_parses_session_and_weekly_windows():
    lim = parse_usage(_SAMPLE)
    assert lim.session is not None and lim.session.used_pct == 41.0
    assert lim.weekly is not None and lim.weekly.used_pct == 33.0
    assert isinstance(lim.session.resets_at, datetime)
    assert lim.session.resets_at.tzinfo is None  # local naive, for the clock


def test_fable_cap_comes_from_the_limits_array():
    """seven_day_opus/sonnet are null here; the Fable window is scoped-only."""
    fable = parse_usage(_SAMPLE).weekly_fable
    assert fable is not None
    assert fable.used_pct == 10.0  # int percent -> float
    assert isinstance(fable.used_pct, float)
    assert fable.window_s == _WEEK_WINDOW_S


def test_fable_match_is_case_insensitive():
    data = {
        "limits": [
            {
                "kind": "weekly_scoped",
                "percent": 7,
                "scope": {"model": {"display_name": "FABLE"}},
            }
        ]
    }
    assert parse_usage(data).weekly_fable is not None


def test_no_fable_window_yields_none_not_an_empty_gauge():
    data = {
        "five_hour": {"utilization": 5.0},
        "limits": [
            {
                "kind": "weekly_scoped",
                "percent": 12,
                "scope": {"model": {"display_name": "Sonnet"}},
            }
        ],
    }
    assert parse_usage(data).weekly_fable is None


def test_absent_and_null_sections_degrade_to_none():
    lim = parse_usage({"five_hour": None, "seven_day": None, "limits": None})
    assert (lim.session, lim.weekly, lim.weekly_fable) == (None, None, None)


def test_null_utilization_reads_as_zero():
    lim = parse_usage({"five_hour": {"utilization": None, "resets_at": None}})
    assert lim.session is not None
    assert lim.session.used_pct == 0.0
    assert lim.session.resets_at is None


def test_session_window_carries_no_pace_length():
    """five_hour is documented as *rolling*; a pace tick there would lie."""
    assert parse_usage(_SAMPLE).session.window_s is None


def test_plan_label_combines_subscription_and_tier():
    assert plan_label("max", "default_claude_max_5x") == "Max (5x)"
    assert plan_label("max", "default_claude_max_20x") == "Max (20x)"


def test_plan_label_without_a_recognisable_multiplier():
    assert plan_label("pro", "default_claude_pro") == "Pro"
    assert plan_label("max", None) == "Max"


def test_plan_label_absent_subscription():
    assert plan_label(None, "default_claude_max_5x") is None


# ── Throttling ───────────────────────────────────────────────────────────


def _http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        mod._USAGE_URL, code, "Too Many Requests", headers or {}, None
    )


def test_retry_after_in_seconds():
    assert retry_after_s(_http_error(429, {"Retry-After": "30"})) == 30.0


def test_retry_after_as_an_http_date():
    when = datetime.now(UTC) + timedelta(seconds=120)
    got = retry_after_s(_http_error(429, {"Retry-After": format_datetime(when)}))
    assert got is not None and 100 <= got <= 130


def test_retry_after_absent_or_unparseable():
    assert retry_after_s(_http_error(429)) is None
    assert retry_after_s(_http_error(429, {"Retry-After": "soon"})) is None


def test_a_past_http_date_does_not_produce_a_negative_wait():
    when = datetime.now(UTC) - timedelta(seconds=60)
    assert (
        retry_after_s(_http_error(429, {"Retry-After": format_datetime(when)})) == 0.0
    )


def _stub_transport(collector, error: Exception, monkeypatch) -> None:
    monkeypatch.setattr(
        mod,
        "_oauth",
        lambda: {
            "accessToken": "sk-ant-oat01-x",
            "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_5x",
        },
    )

    def boom(*_a, **_kw):
        raise error

    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)


def test_429_becomes_a_throttled_carrying_the_servers_hint(monkeypatch):
    """The log's failure mode: 429 every minute. It must pace the next try."""
    collector = mod.LimitsCollector(SharedState())
    _stub_transport(collector, _http_error(429, {"Retry-After": "300"}), monkeypatch)
    with pytest.raises(Throttled) as excinfo:
        collector.refresh()
    assert excinfo.value.retry_after_s == 300.0
    # Documented ambiguity: a 429 is also what an expired token returns.
    assert "expired" in str(excinfo.value)


def test_429_without_a_hint_still_throttles(monkeypatch):
    collector = mod.LimitsCollector(SharedState())
    _stub_transport(collector, _http_error(429), monkeypatch)
    with pytest.raises(Throttled) as excinfo:
        collector.refresh()
    assert excinfo.value.retry_after_s is None


def test_other_http_errors_are_not_disguised_as_throttling(monkeypatch):
    collector = mod.LimitsCollector(SharedState())
    _stub_transport(collector, _http_error(500), monkeypatch)
    with pytest.raises(urllib.error.HTTPError):
        collector.refresh()
