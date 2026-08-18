"""Config discovery: renamed locations first, pre-rename location as fallback."""
from __future__ import annotations

from pathlib import Path

from trofeo_hud import config


def test_candidate_order_prefers_new_name_then_legacy():
    names = [str(p) for p in config._CANDIDATES]
    assert names[0].endswith("/config.toml")
    assert Path(names[0]).parent == Path(config.__file__).resolve().parent.parent
    assert names[1].endswith("/.config/trofeo-hud/config.toml")
    assert names[2].endswith("/.config/claude-trofeo-hud/config.toml")


def test_default_log_dir_uses_new_name():
    assert config.Config().log_dir == Path.home() / "Library" / "Logs" / "trofeo-hud"


def test_legacy_config_is_read_when_new_absent(tmp_path, monkeypatch):
    legacy = tmp_path / "claude-trofeo-hud" / "config.toml"
    legacy.parent.mkdir()
    legacy.write_text("fps = 7\n")
    monkeypatch.setattr(config, "_CANDIDATES", [
        tmp_path / "missing.toml",
        tmp_path / "trofeo-hud" / "config.toml",
        legacy,
    ])
    assert config.load().fps == 7.0


def test_new_config_wins_over_legacy(tmp_path, monkeypatch):
    new = tmp_path / "trofeo-hud" / "config.toml"
    legacy = tmp_path / "claude-trofeo-hud" / "config.toml"
    for p, fps in ((new, 3), (legacy, 7)):
        p.parent.mkdir()
        p.write_text(f"fps = {fps}\n")
    monkeypatch.setattr(config, "_CANDIDATES", [new, legacy])
    assert config.load().fps == 3.0


def test_bad_config_logs_and_falls_back(tmp_path, monkeypatch, caplog):
    bad = tmp_path / "config.toml"
    bad.write_text("fps = [oops\n")
    monkeypatch.setattr(config, "_CANDIDATES", [bad])
    with caplog.at_level("WARNING"):
        cfg = config.load()
    assert cfg.fps == config.Config().fps
    assert "ignoring bad config" in caplog.text


def test_night_active_windows():
    from datetime import time as t
    assert not config.Night(mode="on").active(t(3, 0))
    same_day = config.Night(mode="dim", start=t(1, 0), end=t(5, 0))
    assert same_day.active(t(3, 0)) and not same_day.active(t(6, 0))
    wraps = config.Night(mode="off", start=t(23, 0), end=t(6, 0))
    assert wraps.active(t(23, 30)) and wraps.active(t(2, 0))
    assert not wraps.active(t(12, 0))
