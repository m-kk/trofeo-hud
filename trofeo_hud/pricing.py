"""Hypothetical API cost of a UsageEvent at Anthropic list prices.

The HUD shows what the usage *would* have cost pay-as-you-go; on a
subscription it is a proxy for how hard the plan is being worked, nothing
more. Rates are USD per million tokens, (input, output). Cache pricing is a
fixed multiple of the input rate on every model: 5-minute writes ×1.25,
1-hour writes ×2, reads ×0.1.
"""

from __future__ import annotations

import logging

from .collectors.transcripts import UsageEvent

log = logging.getLogger(__name__)

# Longest matching prefix wins, so `claude-opus-4-1` is not swallowed by
# `claude-opus-4`. Cached 2026-08-18 from the Anthropic pricing page.
_RATES: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4": (1.0, 5.0),
    "claude-3-5-haiku": (0.8, 4.0),
}
_CACHE_WRITE_5M = 1.25
_CACHE_WRITE_1H = 2.0
_CACHE_READ = 0.1
# Claude Code's own placeholder for locally generated messages; no tokens,
# not worth a warning.
_FREE = {"<synthetic>", ""}

_warned: set[str] = set()


def rates(model: str) -> tuple[float, float] | None:
    best = None
    for prefix, rate in _RATES.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, rate)
    return best[1] if best else None


def cost_usd(ev: UsageEvent) -> float:
    rate = rates(ev.model)
    if rate is None:
        if ev.model not in _FREE and ev.model not in _warned:
            _warned.add(ev.model)
            log.warning("no price for model %r — counting its cost as $0", ev.model)
        return 0.0
    inp, out = rate
    return (
        ev.input * inp
        + ev.output * out
        + ev.cache_write_5m * inp * _CACHE_WRITE_5M
        + ev.cache_write_1h * inp * _CACHE_WRITE_1H
        + ev.cache_read * inp * _CACHE_READ
    ) / 1_000_000
