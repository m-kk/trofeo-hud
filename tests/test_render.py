"""Renderer tests. Pixel-exact golden images would be font-dependent across
machines; we assert geometry and the few pixels that carry meaning."""

from datetime import datetime, timedelta

from PIL import Image, ImageDraw

from claude_trofeo_hud import theme
from claude_trofeo_hud.render import widgets as w
from claude_trofeo_hud.render.layout import (
    HEIGHT,
    WIDTH,
    gauge_rows,
    render,
)
from claude_trofeo_hud.state import HudState, LimitGauge, mock_state

NOW = datetime(2026, 8, 17, 14, 51)


def test_render_size_and_content():
    img = render(mock_state(NOW))
    assert img.size == (WIDTH, HEIGHT)
    assert len(img.getcolors(maxcolors=1 << 20)) > 10  # actually drew things


def test_render_empty_state_does_not_crash():
    img = render(HudState(now=datetime(2026, 8, 15, 3, 0)))
    assert img.size == (WIDTH, HEIGHT)


def _count(img: Image.Image, color: str, band: tuple[int, int]) -> int:
    """Pixels of `color` in the right column between two y bounds."""
    rgb = tuple(int(color[i : i + 2], 16) for i in (1, 3, 5))
    return sum(
        img.getpixel((x, y)) == rgb for y in range(*band) for x in range(960, WIDTH)
    )


def test_stale_activity_is_not_shown_as_live():
    """A quiet collector must not keep claiming ACTIVE with a live-looking dot."""
    live = mock_state(NOW)
    stale = mock_state(NOW)
    stale.activity.stale = True

    assert _count(render(live), theme.GOOD, (180, 340)) > 0
    assert _count(render(stale), theme.GOOD, (180, 340)) == 0


def test_stale_activity_dims_the_session_block():
    stale = mock_state(NOW)
    stale.activity.stale = True
    # The model line is the only ACCENT text below the rule.
    assert _count(render(mock_state(NOW)), theme.ACCENT, (230, 300)) > 0
    assert _count(render(stale), theme.ACCENT, (230, 300)) == 0
    assert _count(render(stale), theme.STALE, (180, 300)) > 0


def test_render_stale_sections_do_not_crash():
    state = mock_state(NOW)
    state.limits.stale = True
    state.tokens.stale = True
    state.activity.stale = True
    render(state)


# ── Gauge rows: label/value/bar binding and reflow ───────────────────────


def test_three_gauge_rows_when_the_fable_cap_exists():
    rows = gauge_rows(mock_state(NOW).limits)
    assert [g.label for _, g in rows] == ["Current session", "All models", "Fable only"]


def test_rows_reflow_when_the_fable_cap_is_absent():
    limits = mock_state(NOW).limits
    limits.weekly_fable = None
    rows = gauge_rows(limits)
    assert [g.label for _, g in rows] == ["Current session", "All models"]


def test_absent_fable_row_draws_no_bar_in_its_band():
    """Reflow, not an empty third bar: the band must be bare background."""
    full = mock_state(NOW)
    y = [y for y, g in gauge_rows(full.limits) if g.label == "Fable only"][0]
    bar_y = y + w.BAR_TOP_OFFSET + 6

    full.limits.weekly_fable = None
    img = render(full)
    row = [img.getpixel((x, bar_y)) for x in range(20, 560)]
    assert set(row) == {tuple(int(theme.BG[i : i + 2], 16) for i in (1, 3, 5))}


def test_every_row_has_its_bar_below_its_label():
    """Binding rule: label and value on one line, that row's bar beneath it."""
    rows = gauge_rows(mock_state(NOW).limits)
    ys = [y for y, _ in rows]
    assert ys == sorted(ys)
    for (y0, _), (y1, _) in zip(rows, rows[1:], strict=False):
        assert y0 + w.BAR_TOP_OFFSET < y1  # bar sits inside its own row


# ── Pace tick ────────────────────────────────────────────────────────────


def _bar(pct: float, pace: float | None) -> Image.Image:
    img = Image.new("RGB", (204, 30), theme.BG)
    w.progress_bar(ImageDraw.Draw(img), (2, 3, 202, 27), pct, theme.ACCENT, pace=pace)
    return img


def _xs_of(img: Image.Image, color: str, y: int) -> list[int]:
    """x of every pixel of `color` on row `y`, scanning inside the bar only."""
    rgb = tuple(int(color[i : i + 2], 16) for i in (1, 3, 5))
    return [x for x in range(3, 202) if img.getpixel((x, y)) == rgb]


def test_pace_tick_lands_at_the_elapsed_fraction():
    xs = _xs_of(_bar(10.0, 50.0), theme.MUTED, 15)
    assert xs, "no tick drawn on the track"
    assert abs(sum(xs) / len(xs) - 102) <= 2  # midpoint of a 2..202 bar


def test_pace_tick_reads_as_a_notch_when_inside_the_fill():
    xs = _xs_of(_bar(90.0, 50.0), theme.BG, 15)
    assert xs, "no notch cut into the fill"
    assert abs(sum(xs) / len(xs) - 102) <= 2


def test_pace_tick_omitted_when_pace_is_unknown():
    assert _bar(10.0, None) != _bar(10.0, 50.0)
    assert _xs_of(_bar(10.0, None), theme.MUTED, 15) == []


def test_pace_tick_stays_inside_the_track_at_the_extremes():
    for pace in (0.0, 100.0):
        xs = _xs_of(_bar(0.0, pace), theme.MUTED, 15)
        assert xs and all(2 <= x <= 202 for x in xs)


# ── Formatters ───────────────────────────────────────────────────────────


def test_fmt_tokens():
    assert w.fmt_tokens(999) == "999"
    assert w.fmt_tokens(1_500) == "1.5k"
    assert w.fmt_tokens(229_200_000) == "229.2M"
    assert w.fmt_tokens(1_100_000_000) == "1.1B"


def test_fmt_countdown():
    assert w.fmt_countdown(59) == "0m"
    assert w.fmt_countdown(4 * 3600 + 45 * 60) == "4h 45m"
    assert w.fmt_countdown(2 * 86400 + 9 * 3600) == "2d 9h"
    assert w.fmt_countdown(-5) == "0m"


def test_fmt_reset_clock_drops_the_day_when_it_is_today():
    assert w.fmt_reset_clock(NOW, NOW + timedelta(hours=4, minutes=29)) == "7:20 PM"


def test_fmt_reset_clock_names_the_weekday_within_a_week():
    assert w.fmt_reset_clock(NOW, datetime(2026, 8, 21, 7, 0)) == "Fri 7:00 AM"


def test_fmt_reset_clock_dates_anything_further_out():
    assert w.fmt_reset_clock(NOW, datetime(2026, 8, 28, 7, 0)) == "Aug 28 7:00 AM"


# ── Limit colour is severity, never model identity ───────────────────────


def test_limit_colour_maps_to_percentage():
    assert theme.limit_color(31.0) == theme.ACCENT
    assert theme.limit_color(85.0) == theme.WARN
    assert theme.limit_color(97.0) == theme.CRIT


def test_same_percentage_renders_the_same_colour_on_every_gauge():
    session = LimitGauge("Current session", 85.0)
    fable = LimitGauge("Fable only", 85.0)
    assert theme.limit_color(session.used_pct) == theme.limit_color(fable.used_pct)
