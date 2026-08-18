"""Limits collector: parsing, throttling, token expiry, redirect safety.

Parsing shapes mirror docs/usage-endpoint.md, including the account where the
top-level per-model keys are null and the Fable cap exists only in limits[].

Expiry: the Keychain token is rotated by Claude Code, not by us. When Claude
Code has not run for a while the token simply expires, and an unattended daemon
would otherwise show frozen gauges behind a small "stale" indefinitely. The
expiry timestamp sits in the same Keychain blob, so the collector can say
"AUTH EXPIRED" instead of implying the numbers are merely late.

Redirects: urllib's redirect handler copies every header — including
Authorization — onto the new request with no host comparison, so a redirect
off api.anthropic.com would carry the bearer token to whatever host answered.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from trofeo_hud.collectors import limits as mod
from trofeo_hud.collectors.base import SharedState
from trofeo_hud.collectors.limits import (
    _WEEK_WINDOW_S,
    Throttled,
    parse_usage,
    plan_label,
    retry_after_s,
)


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    def __init__(self, payload=None, error=None):
        self.payload, self.error = payload, error
        self.calls = 0

    def open(self, req, timeout=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.payload)


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


def test_null_utilization_is_unknown_not_zero():
    """A confident "0%" for "no idea" would defeat a gauge whose job is to warn."""
    lim = parse_usage({"five_hour": {"utilization": None, "resets_at": None}})
    assert lim.session is not None
    assert lim.session.used_pct is None
    assert lim.session.resets_at is None


def test_genuine_zero_is_preserved():
    lim = parse_usage({"five_hour": {"utilization": 0.0, "resets_at": None}})
    assert lim.session.used_pct == 0.0


def test_session_window_carries_its_span():
    """Measured anchored, not rolling — usage moved while resets_at held."""
    assert parse_usage(_SAMPLE).session.window_s == 5 * 3600


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

    monkeypatch.setattr(mod, "_OPENER", _FakeOpener(error=error))


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


def test_cadence_stays_clear_of_the_endpoints_rate_limit():
    """Measured allowance is ~1 request/2 min; 60s guarantees 429s (see
    docs/usage-endpoint.md). Poll slower than the limiter, don't absorb it."""
    assert mod.LimitsCollector.cadence_s >= 180.0


# ── Collector against a faked Keychain and transport ─────────────────────

# Trimmed from a real response. `utilization` is 0-100; `resets_at` is
# offset-aware ISO 8601.
RESPONSE = {
    "five_hour": {"utilization": 41.0, "resets_at": "2026-08-17T19:10:00.084456+00:00"},
    "seven_day": {"utilization": 33.0, "resets_at": "2026-08-21T12:00:00.084474+00:00"},
}


def _keychain(expires_at: datetime | None, token: str = "sk-ant-oat01-x"):
    """Fake the `security find-generic-password` call."""
    blob = {"claudeAiOauth": {"accessToken": token}}
    if expires_at is not None:
        blob["claudeAiOauth"]["expiresAt"] = int(
            expires_at.timestamp() * 1000
        )  # the real field is epoch millis

    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(blob))

    return run


@pytest.fixture
def collector():
    return mod.LimitsCollector(SharedState())


def _wire(monkeypatch, expires_at, payload=None, error=None):
    monkeypatch.setattr(subprocess, "run", _keychain(expires_at))
    opener = _FakeOpener(payload, error)
    monkeypatch.setattr(mod, "_OPENER", opener)
    return opener


# ── Happy path ───────────────────────────────────────────────────────────


def test_populates_both_gauges(monkeypatch, collector):
    _wire(monkeypatch, datetime.now() + timedelta(hours=1), RESPONSE)

    collector.refresh()
    lim = collector.shared.snapshot().limits

    assert lim.session.used_pct == 41.0
    assert lim.weekly.used_pct == 33.0
    assert lim.session.label == "Current session"
    assert lim.stale is False
    assert lim.auth_expired is False


def test_resets_at_is_converted_from_utc(monkeypatch, collector):
    """The endpoint sends an offset; the HUD renders naive local time."""
    _wire(monkeypatch, datetime.now() + timedelta(hours=1), RESPONSE)

    collector.refresh()
    resets = collector.shared.snapshot().limits.session.resets_at

    expected = (
        datetime.fromisoformat(RESPONSE["five_hour"]["resets_at"])
        .astimezone()
        .replace(tzinfo=None)
    )
    assert resets == expected
    assert resets.tzinfo is None


def test_absent_section_leaves_gauge_none(monkeypatch, collector):
    _wire(
        monkeypatch,
        datetime.now() + timedelta(hours=1),
        {"five_hour": RESPONSE["five_hour"]},
    )

    collector.refresh()

    assert collector.shared.snapshot().limits.weekly is None


# ── Expiry is detected locally, before any request ───────────────────────


def test_expired_token_does_not_hit_the_network(monkeypatch, collector):
    opener = _wire(monkeypatch, datetime.now() - timedelta(minutes=1), RESPONSE)

    collector.refresh()

    assert opener.calls == 0, "an expired token must not be sent"
    assert collector.shared.snapshot().limits.auth_expired is True


def test_expired_token_keeps_last_good_gauges(monkeypatch, collector):
    _wire(monkeypatch, datetime.now() + timedelta(hours=1), RESPONSE)
    collector.refresh()

    _wire(monkeypatch, datetime.now() - timedelta(minutes=1), RESPONSE)
    collector.refresh()
    lim = collector.shared.snapshot().limits

    assert lim.auth_expired is True
    assert lim.session.used_pct == 41.0, "keep the last-good reading"


def test_recovering_a_fresh_token_clears_the_flag(monkeypatch, collector):
    _wire(monkeypatch, datetime.now() - timedelta(minutes=1), RESPONSE)
    collector.refresh()

    _wire(monkeypatch, datetime.now() + timedelta(hours=1), RESPONSE)
    collector.refresh()

    assert collector.shared.snapshot().limits.auth_expired is False


def test_absent_expiry_field_is_not_treated_as_expired(monkeypatch, collector):
    """Older credential blobs may not carry expiresAt; don't invent a failure."""
    opener = _wire(monkeypatch, None, RESPONSE)

    collector.refresh()

    assert opener.calls == 1
    assert collector.shared.snapshot().limits.auth_expired is False


# ── Failures keep last-good values ───────────────────────────────────────


def test_http_error_marks_stale_and_keeps_values(monkeypatch, collector):
    _wire(monkeypatch, datetime.now() + timedelta(hours=1), RESPONSE)
    collector.refresh()

    _wire(
        monkeypatch,
        datetime.now() + timedelta(hours=1),
        error=urllib.error.HTTPError("u", 503, "unavailable", {}, None),
    )
    with pytest.raises(urllib.error.HTTPError):
        collector.refresh()
    collector.mark_stale()
    lim = collector.shared.snapshot().limits

    assert lim.stale is True
    assert lim.session.used_pct == 41.0


# ── The redirect handler must not forward the bearer token ───────────────


def test_cross_host_redirect_is_refused():
    handler = mod._NoCrossHostRedirect()
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={"Authorization": "Bearer secret"},
    )

    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(
            req, None, 302, "Found", {}, "https://evil.example.com/collect"
        )


def test_same_host_redirect_is_allowed():
    handler = mod._NoCrossHostRedirect()
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={"Authorization": "Bearer secret"},
    )

    new = handler.redirect_request(
        req, None, 302, "Found", {}, "https://api.anthropic.com/api/oauth/usage2"
    )

    assert new is not None
    assert new.full_url.endswith("usage2")


def test_collector_uses_the_guarded_opener():
    """A bare urlopen() would bypass the redirect guard entirely."""
    src = inspect.getsource(mod)

    assert "_OPENER.open(" in src
    assert "urllib.request.urlopen(" not in src


# ── Fallback: estimate the 5-hour block from the transcripts ─────────────
#
# When the endpoint is unreachable (429s, expired token) the panel would
# otherwise hold a frozen percentage until — and past — its reset. The
# transcripts on disk know when this block began (the first request after the
# previous block ended), so the countdown at least can be honest; and while
# the last good sample's window is still live, its percentage can be scaled
# by the cost accrued since. Both are labelled as estimates.

from pathlib import Path  # noqa: E402

from trofeo_hud.collectors import transcripts as tx  # noqa: E402
from trofeo_hud.collectors.limits import (  # noqa: E402
    _SESSION_WINDOW_S,
    estimate_session,
)
from trofeo_hud.collectors.transcripts import TranscriptLog, UsageEvent  # noqa: E402

T = datetime(2026, 8, 18, 14, 0).astimezone()
H = timedelta(hours=1)


def _ev(ts, inp=0):
    return UsageEvent(ts, "claude-opus-5", inp, 0, 0, 0, 0, Path("f"), "p", "s", None)


class _StaticLog(TranscriptLog):
    def __init__(self, events):
        super().__init__(Path("/nonexistent"))
        self._events = list(events)

    def ingest(self, now):
        pass


def test_estimate_session_chains_blocks_from_the_first_event():
    # 08:00 starts a block (→13:00); 12:59 is inside it; 13:01 starts the next.
    events = [
        _ev(T - 6 * H),
        _ev(T - H - timedelta(minutes=1)),
        _ev(T - timedelta(minutes=59)),
    ]
    g = estimate_session(events, T)
    assert g.label == "Current session (est.)"
    assert g.used_pct is None
    assert g.window_s == _SESSION_WINDOW_S
    assert g.resets_at == (T - timedelta(minutes=59) + 5 * H).replace(tzinfo=None)


def test_estimate_session_uses_time_order_not_file_order():
    events = [_ev(T - timedelta(minutes=59)), _ev(T - 6 * H)]
    assert estimate_session(events, T).resets_at == (
        T - timedelta(minutes=59) + 5 * H
    ).replace(tzinfo=None)


def test_estimate_session_is_none_between_blocks_or_without_events():
    assert estimate_session([], T) is None
    assert estimate_session([_ev(T - 6 * H)], T) is None  # that block ended at 13:00


def _sample(monkeypatch, collector, resets_at_utc: str, pct: float):
    payload = {"five_hour": {"utilization": pct, "resets_at": resets_at_utc}}
    _wire(monkeypatch, datetime.now() + timedelta(hours=1), payload)
    collector.refresh()


def test_no_log_means_no_fallback(monkeypatch, collector):
    """The collector without a transcript log behaves exactly as before."""
    _wire(monkeypatch, datetime.now() - timedelta(minutes=1), RESPONSE)
    collector.refresh()
    assert collector.shared.snapshot().limits.session is None


def test_fallback_estimates_the_block_when_there_is_no_gauge_yet(monkeypatch):
    monkeypatch.setattr(tx, "local_now", lambda: T)
    log = _StaticLog([_ev(T - 2 * H)])
    c = mod.LimitsCollector(SharedState(), log=log)
    _wire(monkeypatch, datetime.now() - timedelta(minutes=1), RESPONSE)  # token expired
    c.refresh()
    lim = c.shared.snapshot().limits
    assert lim.auth_expired is True
    assert lim.session.label == "Current session (est.)"
    assert lim.session.used_pct is None
    assert lim.session.resets_at == (T + 3 * H).replace(tzinfo=None)


def test_fallback_replaces_a_gauge_whose_reset_has_passed(monkeypatch):
    # Local blocks: T-8h..T-3h, then a fresh one from T-1h.
    log = _StaticLog([_ev(T - 8 * H, inp=100), _ev(T - H, inp=100)])
    c = mod.LimitsCollector(SharedState(), log=log)
    # Good sample taken in a window the server says ends at T-2h.
    monkeypatch.setattr(tx, "local_now", lambda: T - 3 * H)
    _sample(monkeypatch, c, (T - 2 * H).astimezone(UTC).isoformat(), 60.0)
    assert c.shared.snapshot().limits.session.used_pct == 60.0

    monkeypatch.setattr(tx, "local_now", lambda: T)
    c.mark_stale()
    g = c.shared.snapshot().limits.session
    assert g.label == "Current session (est.)"
    assert g.used_pct is None, "no sample in this block — don't invent a percentage"
    assert g.resets_at == (T - H + 5 * H).replace(tzinfo=None)


def test_fallback_scales_the_last_sample_by_cost_accrued_since(monkeypatch):
    # Block T-1h..T+4h. Sample at T with $X of usage in the block, 20%.
    e1 = _ev(T - timedelta(minutes=30), inp=1_000_000)  # $5
    log = _StaticLog([e1])
    c = mod.LimitsCollector(SharedState(), log=log)
    monkeypatch.setattr(tx, "local_now", lambda: T)
    _sample(monkeypatch, c, (T + 4 * H).astimezone(UTC).isoformat(), 20.0)

    # Then $10 more, endpoint fails.
    log._events.append(_ev(T + timedelta(minutes=10), inp=2_000_000))
    monkeypatch.setattr(tx, "local_now", lambda: T + timedelta(minutes=20))
    c.mark_stale()
    g = c.shared.snapshot().limits.session
    assert g.label == "Current session (est.)"
    assert g.used_pct == pytest.approx(60.0)  # 20% × (15/5)
    assert g.resets_at == (T + 4 * H).replace(tzinfo=None), "the server's reset is kept"
    assert c.shared.snapshot().limits.stale is True


def test_fallback_caps_the_extrapolation_at_100(monkeypatch):
    log = _StaticLog([_ev(T, inp=1_000_000)])
    c = mod.LimitsCollector(SharedState(), log=log)
    monkeypatch.setattr(tx, "local_now", lambda: T)
    _sample(monkeypatch, c, (T + 4 * H).astimezone(UTC).isoformat(), 90.0)
    log._events.append(_ev(T + H, inp=9_000_000))
    monkeypatch.setattr(tx, "local_now", lambda: T + H)
    c.mark_stale()
    assert c.shared.snapshot().limits.session.used_pct == 100.0


def test_fallback_holds_the_sample_when_it_had_no_local_cost_to_scale_from(monkeypatch):
    """A sample taken with nothing in the local logs (usage from another
    machine) has no ratio to scale by; keep its percentage rather than zeroing."""
    log = _StaticLog([])
    c = mod.LimitsCollector(SharedState(), log=log)
    monkeypatch.setattr(tx, "local_now", lambda: T)
    _sample(monkeypatch, c, (T + 4 * H).astimezone(UTC).isoformat(), 35.0)
    log._events.append(_ev(T + H, inp=1))
    monkeypatch.setattr(tx, "local_now", lambda: T + H)
    c.mark_stale()
    g = c.shared.snapshot().limits.session
    assert g.used_pct == 35.0 and g.label == "Current session (est.)"


def test_fallback_leaves_a_live_null_gauge_alone_when_no_sample_exists(monkeypatch):
    """Server said null for the live window: still unknown, still its reset."""
    log = _StaticLog([_ev(T)])
    c = mod.LimitsCollector(SharedState(), log=log)
    monkeypatch.setattr(tx, "local_now", lambda: T)
    payload = {
        "five_hour": {
            "utilization": None,
            "resets_at": (T + 4 * H).astimezone(UTC).isoformat(),
        }
    }
    _wire(monkeypatch, datetime.now() + timedelta(hours=1), payload)
    c.refresh()
    monkeypatch.setattr(tx, "local_now", lambda: T + H)
    c.mark_stale()
    g = c.shared.snapshot().limits.session
    assert g.used_pct is None and g.label == "Current session"


def test_a_fresh_sample_restores_the_servers_label(monkeypatch):
    log = _StaticLog([_ev(T)])
    c = mod.LimitsCollector(SharedState(), log=log)
    monkeypatch.setattr(tx, "local_now", lambda: T)
    _sample(monkeypatch, c, (T + 4 * H).astimezone(UTC).isoformat(), 20.0)
    c.mark_stale()
    assert c.shared.snapshot().limits.session.label == "Current session (est.)"
    _sample(monkeypatch, c, (T + 4 * H).astimezone(UTC).isoformat(), 25.0)
    g = c.shared.snapshot().limits.session
    assert g.label == "Current session" and g.used_pct == 25.0


def test_unparseable_reset_timestamp_becomes_none():
    lim = parse_usage({"five_hour": {"utilization": 1.0, "resets_at": "soon"}})
    assert lim.session.used_pct == 1.0 and lim.session.resets_at is None
