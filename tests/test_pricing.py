"""Hypothetical API cost from a UsageEvent's token classes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from trofeo_hud import pricing
from trofeo_hud.collectors.transcripts import UsageEvent

T0 = datetime(2026, 8, 18, tzinfo=UTC)


def _ev(model, inp=0, out=0, w5=0, w1=0, rd=0):
    return UsageEvent(T0, model, inp, out, w5, w1, rd, Path("f"), "p", "s", "id")


def test_fable_uses_its_own_higher_rates():
    # $10 in, $50 out per MTok
    assert pricing.cost_usd(_ev("claude-fable-5", inp=1_000_000)) == pytest.approx(10.0)
    assert pricing.cost_usd(_ev("claude-fable-5", out=1_000_000)) == pytest.approx(50.0)


@pytest.mark.parametrize(
    "model, inp, out",
    [
        ("claude-opus-5", 5.0, 25.0),
        ("claude-opus-4-8", 5.0, 25.0),
        ("claude-opus-4-7", 5.0, 25.0),
        ("claude-opus-4-6", 5.0, 25.0),
        ("claude-opus-4-5-20251101", 5.0, 25.0),
        ("claude-opus-4-1-20250805", 15.0, 75.0),
        ("claude-sonnet-5", 3.0, 15.0),
        ("claude-sonnet-4-6", 3.0, 15.0),
        ("claude-sonnet-4-5-20250929", 3.0, 15.0),
        ("claude-haiku-4-5-20251001", 1.0, 5.0),
        ("claude-3-5-haiku-20241022", 0.8, 4.0),
    ],
)
def test_per_model_input_and_output_rates(model, inp, out):
    assert pricing.cost_usd(_ev(model, inp=1_000_000)) == pytest.approx(inp)
    assert pricing.cost_usd(_ev(model, out=1_000_000)) == pytest.approx(out)


def test_cache_tiers_scale_off_the_input_rate():
    # Opus 5: $5 in → 5m write $6.25, 1h write $10, read $0.50
    assert pricing.cost_usd(_ev("claude-opus-5", w5=1_000_000)) == pytest.approx(6.25)
    assert pricing.cost_usd(_ev("claude-opus-5", w1=1_000_000)) == pytest.approx(10.0)
    assert pricing.cost_usd(_ev("claude-opus-5", rd=1_000_000)) == pytest.approx(0.5)


def test_unknown_model_costs_nothing_and_is_logged_once(caplog):
    pricing._warned.clear()
    with caplog.at_level("WARNING"):
        assert pricing.cost_usd(_ev("gpt-5.6-sol", inp=10)) == 0.0
        assert pricing.cost_usd(_ev("gpt-5.6-sol", inp=10)) == 0.0
    assert sum("gpt-5.6-sol" in r.message for r in caplog.records) == 1


def test_synthetic_model_is_silently_free(caplog):
    with caplog.at_level("WARNING"):
        assert pricing.cost_usd(_ev("<synthetic>")) == 0.0
    assert not caplog.records


def test_rates_lookup_prefers_the_longest_matching_prefix():
    # "claude-opus-4-1" must not be swallowed by a shorter "claude-opus-4" rule.
    assert pricing.rates("claude-opus-4-1-20250805") == pricing.rates("claude-opus-4-1")
    assert pricing.rates("claude-opus-4-1") != pricing.rates("claude-opus-4-5")
