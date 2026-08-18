"""Renderer tests. Pixel-exact golden images would be font-dependent across
machines; we assert geometry and the few pixels that carry meaning."""

from datetime import datetime, timedelta

from PIL import Image, ImageColor, ImageDraw

from trofeo_hud import theme
from trofeo_hud.render import widgets as w
from trofeo_hud.render.layout import (
    HEIGHT,
    WIDTH,
    gauge_rows,
    render,
)
from trofeo_hud.state import HudState, LimitGauge, mock_state

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


# ── Pace marker ──────────────────────────────────────────────────────────

_BAR = (2, 3, 202, 27)  # x0, y0, x1, y1 — midpoint at x=102


def _bar(pct: float, marker_pct: float | None) -> Image.Image:
    img = Image.new("RGB", (204, 40), theme.BG)
    w.progress_bar(ImageDraw.Draw(img), _BAR, pct, theme.ACCENT, marker_pct=marker_pct)
    return img


def _xs_of(img: Image.Image, color: str, y: int) -> list[int]:
    """x of every pixel of `color` on row `y`, scanning inside the bar only."""
    return [
        x for x in range(3, 202) if img.getpixel((x, y)) == ImageColor.getrgb(color)
    ]


def test_marker_lands_at_the_elapsed_fraction():
    xs = _xs_of(_bar(10.0, 50.0), theme.FG, 15)
    assert xs, "no marker drawn"
    assert abs(sum(xs) / len(xs) - 102) <= 2


def test_marker_reads_the_same_where_it_crosses_the_fill():
    """One treatment on fill or bare track — legibility beats subtlety here."""
    on_track = _xs_of(_bar(10.0, 50.0), theme.FG, 15)
    through_fill = _xs_of(_bar(90.0, 50.0), theme.FG, 15)
    assert on_track and through_fill == on_track


def test_marker_stands_proud_of_the_pill_top_and_bottom():
    img = _bar(50.0, 50.0)
    above = _BAR[1] - w.MARKER_OVERHANG
    below = _BAR[3] + w.MARKER_OVERHANG
    assert _xs_of(img, theme.FG, above), "no overhang above the pill"
    assert _xs_of(img, theme.FG, below), "no overhang below the pill"
    # …and nothing beyond it, so rows can't collide.
    assert _xs_of(img, theme.FG, above - 1) == []


def test_marker_omitted_when_the_window_length_is_unknown():
    assert _bar(10.0, None) != _bar(10.0, 50.0)
    assert _xs_of(_bar(10.0, None), theme.FG, 15) == []


def test_marker_stays_inside_the_track_at_the_extremes():
    for marker_pct in (0.0, 100.0):
        xs = _xs_of(_bar(0.0, marker_pct), theme.FG, 15)
        assert xs and all(_BAR[0] <= x <= _BAR[2] for x in xs)


def test_every_gauge_row_gets_a_marker():
    """All three windows are anchored spans, so none renders bare."""
    limits = mock_state(NOW).limits
    for _, gauge in gauge_rows(limits):
        assert gauge.elapsed_pct(NOW) is not None, gauge.label


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


# ── Small fills are drawn true to size, not inflated to a full cap ───────


def _fill_extent(pct: float) -> int:
    xs = _xs_of(_bar(pct, None), theme.ACCENT, 15)
    return max(xs) if xs else _BAR[0]


def test_small_fill_is_not_inflated_to_a_full_pill_cap():
    """2% of a 200px track is 4px; it used to draw a 24px cap regardless."""
    assert _BAR[0] < _fill_extent(2.0) <= _BAR[0] + 5


def test_fill_extent_grows_with_the_percentage_through_the_cap_region():
    extents = [_fill_extent(p) for p in (1.0, 2.0, 4.0, 8.0, 12.0, 20.0)]
    assert extents == sorted(extents) and len(set(extents)) == len(extents)


def test_zero_fill_draws_nothing():
    assert _xs_of(_bar(0.0, None), theme.ACCENT, 15) == []
