"""ccusage collector: the 7-day figure covers the trailing week, not a
Monday-anchored calendar week that disagrees with the rolling weekly gauge."""
from __future__ import annotations

import json
import subprocess
from datetime import date, timedelta

from trofeo_hud.collectors import tokens as mod
from trofeo_hud.collectors.base import SharedState


def _fake_run(calls):
    def run(cmd, **kw):
        calls.append(cmd)
        today = date.today()
        days = [
            {"period": (today - timedelta(days=i)).isoformat(),
             "totalTokens": 10, "totalCost": 1.0}
            for i in range(7)
        ]
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"daily": days}), "")
    return run


def test_since_is_six_days_ago_so_the_window_is_a_trailing_seven_days(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(mod.subprocess, "run", _fake_run(calls))
    shared = SharedState()
    mod.TokensCollector(shared).refresh()
    since = calls[0][calls[0].index("--since") + 1]
    assert since == (date.today() - timedelta(days=6)).strftime("%Y%m%d")
    snap = shared.snapshot()
    assert snap.tokens.week_tokens == 70
    assert snap.tokens.week_cost_usd == 7.0
    assert snap.tokens.today_tokens == 10
