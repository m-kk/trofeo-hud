"""Config discovery: renamed locations first, pre-rename location as fallback."""
from __future__ import annotations

from datetime import time as dtime
from pathlib import Path

import pytest

from trofeo_hud import config
from trofeo_hud.config import Config, Night, load


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


# ── Loading must never raise ─────────────────────────────────────────────
# `agent.py` installs the daemon with KeepAlive + ThrottleInterval 10, so an
# exception escaping load() is a crash-loop every ten seconds.

def _write(tmp_path, text: str):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


# ── Every malformed input falls back to defaults ─────────────────────────


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("unparsable toml", "fps = = 2"),
        ("fps not a number", 'fps = "fast"'),
        ("fps not convertible", "fps = [1, 2]"),
        ("quality not a number", 'jpeg_quality = "high"'),
        ("impossible clock time", '[night]\nstart = "25:00"'),
        # Easy confusion, since `mode` itself takes the string "off".
        ("night as a value not a table", 'night = "off"'),
        ("dim_factor not a number", '[night]\ndim_factor = "half"'),
    ],
)
def test_malformed_config_falls_back_to_defaults(tmp_path, caplog, label, text):
    path = _write(tmp_path, text)

    with caplog.at_level("WARNING"):
        cfg = load(path)

    assert cfg == Config(), f"{label}: must return untouched defaults"
    assert caplog.records, f"{label}: must log why the file was ignored"


def test_absent_file_uses_defaults(tmp_path):
    assert load(tmp_path / "nope.toml") == Config()


def test_partial_config_keeps_defaults_for_absent_fields(tmp_path):
    cfg = load(_write(tmp_path, "fps = 4.0"))

    assert cfg.fps == 4.0
    assert cfg.jpeg_quality == Config().jpeg_quality
    assert cfg.night == Night()


# ── Valid input round-trips ──────────────────────────────────────────────


def test_toml_native_time_literal_is_accepted(tmp_path):
    """TOML has a real time type, so `start = 22:00:00` (no quotes) is a
    reasonable thing to write. It used to raise TypeError out of
    `dtime.fromisoformat()` and crash-loop the daemon; honour it instead of
    discarding the whole file."""
    cfg = load(_write(tmp_path, "[night]\nstart = 22:00:00\nend = 06:00:00"))

    assert cfg.night.start == dtime(22, 0)
    assert cfg.night.end == dtime(6, 0)


def test_full_config_round_trips(tmp_path):
    cfg = load(
        _write(
            tmp_path,
            """
        fps = 3.0
        jpeg_quality = 80
        [night]
        mode = "dim"
        start = "22:30"
        end = "06:15"
        dim_factor = 0.5
    """,
        )
    )

    assert cfg.fps == 3.0
    assert cfg.jpeg_quality == 80
    assert cfg.night == Night(
        mode="dim", start=dtime(22, 30), end=dtime(6, 15), dim_factor=0.5
    )


# ── Values that parse but would break the render loop are clamped ────────


def test_zero_fps_is_clamped(tmp_path):
    """`1.0 / cfg.fps` in app.py divides by zero."""
    assert load(_write(tmp_path, "fps = 0")).fps > 0


def test_negative_fps_is_clamped(tmp_path):
    assert load(_write(tmp_path, "fps = -5")).fps > 0


@pytest.mark.parametrize("quality", [0, -10, 300])
def test_jpeg_quality_is_clamped_to_pillows_useful_range(tmp_path, quality):
    cfg = load(_write(tmp_path, f"jpeg_quality = {quality}"))

    assert 1 <= cfg.jpeg_quality <= 95


def test_unknown_night_mode_falls_back_rather_than_blanking_the_panel(tmp_path):
    """An unrecognised mode must not be treated as "not on" — that would put
    the panel into dim/off behaviour for a typo."""
    cfg = load(_write(tmp_path, '[night]\nmode = "sometimes"'))

    assert cfg.night.mode in {"on", "dim", "off"}


# ── Night.active ─────────────────────────────────────────────────────────


def test_night_active_within_window():
    n = Night(mode="off", start=dtime(22, 0), end=dtime(7, 0))

    assert n.active(dtime(23, 0)) is True
    assert n.active(dtime(3, 0)) is True, "window crosses midnight"
    assert n.active(dtime(12, 0)) is False


def test_night_mode_on_is_never_active():
    n = Night(mode="on", start=dtime(0, 0), end=dtime(23, 59))

    assert n.active(dtime(12, 0)) is False


def test_equal_bounds_mean_no_quiet_hours():
    """Pinning today's behaviour explicitly: an empty window is never active,
    rather than silently meaning "always"."""
    n = Night(mode="off", start=dtime(0, 0), end=dtime(0, 0))

    assert n.active(dtime(0, 0)) is False
    assert n.active(dtime(12, 0)) is False
