"""render(state) -> 1280x480 PIL image. Pure function of HudState."""

from __future__ import annotations

from PIL import Image, ImageDraw

from .. import theme
from ..state import HudState, LimitGauge, Limits
from . import widgets as w

WIDTH, HEIGHT = 1280, 480

# Zone x-boundaries (three columns + full-width footer strip)
_COL1 = 620  # limits
_COL2 = 960  # tokens / cost
_PAD = 28
_FOOTER_H = 74

# Left column: every gauge is one row — label and value on a line, that row's
# bar directly beneath it — so a label can never be read against a neighbour's
# bar. Rows are laid out in sequence, so an absent window (the per-model cap
# is null on most accounts) closes up instead of leaving an empty bar.
_ROW_Y = 80  # first row's label line
_ROW_H = 72  # label line + bar
_RESET_H = 34  # the "resets …" line that closes a group
_HEADING_H = 44


def render(state: HudState) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), theme.BG)
    d = ImageDraw.Draw(img)

    _limits_zone(d, state, x0=0, x1=_COL1)
    _tokens_zone(d, state, x0=_COL1, x1=_COL2)
    _activity_zone(d, state, x0=_COL2, x1=WIDTH)
    _footer(d, state)

    # Column separators
    body_y1 = HEIGHT - _FOOTER_H
    for x in (_COL1, _COL2):
        d.line((x, 24, x, body_y1 - 12), fill=theme.BORDER)
    d.line((_PAD, body_y1, WIDTH - _PAD, body_y1), fill=theme.BORDER)
    return img


def gauge_rows(limits: Limits) -> list[tuple[int, LimitGauge]]:
    """(y of the label line, gauge) for each window that exists, top to bottom.

    The single source of the left column's vertical geometry — the renderer
    and the layout tests read the same numbers.
    """
    rows: list[tuple[int, LimitGauge]] = []
    y = _ROW_Y
    if limits.session:
        rows.append((y, limits.session))
        y += _ROW_H + _RESET_H  # session's own reset line closes the group
    y += _HEADING_H  # "Weekly limits"
    for gauge in (limits.weekly, limits.weekly_fable):
        if gauge:
            rows.append((y, gauge))
            y += _ROW_H
    return rows


# ── Zones ────────────────────────────────────────────────────────────────


def _limits_zone(d: ImageDraw.ImageDraw, state: HudState, x0: int, x1: int) -> None:
    x, xr = x0 + _PAD, x1 - _PAD
    lim = state.limits
    d.text((x, 22), "USAGE", font=theme.sans(36), fill=theme.FG)
    if lim.plan:
        d.text((x + 148, 34), lim.plan, font=theme.sans(22), fill=theme.MUTED)
    if lim.stale:
        d.text((xr, 34), "stale", font=theme.sans(20), fill=theme.STALE, anchor="ra")

    rows = gauge_rows(lim)
    if not rows:
        d.text((x, _ROW_Y), "no usage data", font=theme.mono(22), fill=theme.FAINT)
        return

    weekly = [row for row in rows if row[1] is not lim.session]
    for y, gauge in rows:
        if weekly and gauge is weekly[0][1]:
            d.text(
                (x, y - _HEADING_H + 2),
                "Weekly limits",
                font=theme.sans(24),
                fill=theme.MUTED,
            )
        _gauge_row(d, state, gauge, y, x, xr)
        if gauge is lim.session:
            _reset_line(d, state, gauge, y + _ROW_H, x, xr)

    if weekly:
        # Both weekly windows share one boundary; one reset line closes them.
        _reset_line(d, state, weekly[-1][1], weekly[-1][0] + _ROW_H, x, xr)


def _gauge_row(
    d: ImageDraw.ImageDraw, state: HudState, g: LimitGauge, y: int, x: int, xr: int
) -> None:
    color = theme.limit_color(g.used_pct)  # severity, never model identity
    d.text((x, y + 8), g.label, font=theme.sans(24), fill=theme.FG)
    d.text(
        (xr, y - 6), f"{g.used_pct:.0f}%", font=theme.sans(42), fill=color, anchor="ra"
    )
    w.progress_bar(
        d,
        (x, y + w.BAR_TOP_OFFSET, xr, y + w.BAR_TOP_OFFSET + w.BAR_H),
        g.used_pct,
        color,
        pace=g.elapsed_pct(state.now),
    )


def _reset_line(
    d: ImageDraw.ImageDraw, state: HudState, g: LimitGauge, y: int, x: int, xr: int
) -> None:
    if not g.resets_at:
        return
    secs = (g.resets_at - state.now).total_seconds()
    d.text(
        (x, y),
        f"resets {w.fmt_reset_clock(state.now, g.resets_at)}",
        font=theme.mono(19),
        fill=theme.FAINT,
    )
    d.text(
        (xr, y),
        f"in {w.fmt_countdown(secs)}",
        font=theme.mono(19),
        fill=theme.FAINT,
        anchor="ra",
    )


def _tokens_zone(d: ImageDraw.ImageDraw, state: HudState, x0: int, x1: int) -> None:
    x = x0 + _PAD
    t = state.tokens
    d.text(
        (x, 24),
        "TODAY" + (" (stale)" if t.stale else ""),
        font=theme.sans(20),
        fill=theme.STALE if t.stale else theme.MUTED,
    )
    d.text(
        (x, 54),
        f"{w.fmt_tokens(t.today_tokens)} tokens",
        font=theme.sans(34),
        fill=theme.FG,
    )
    d.text((x, 100), f"${t.today_cost_usd:,.2f}", font=theme.sans(56), fill=theme.FG)
    d.text((x, 170), "est. API cost", font=theme.sans(17), fill=theme.FAINT)
    d.text(
        (x, 200), f"{t.session_count} sessions", font=theme.mono(19), fill=theme.MUTED
    )

    y = 240
    for label, val in (
        ("IN", t.input_tokens),
        ("OUT", t.output_tokens),
        ("CACHE", t.cache_tokens),
    ):
        d.text((x, y), label, font=theme.mono(21), fill=theme.MUTED)
        d.text((x + 100, y), w.fmt_tokens(val), font=theme.mono(21), fill=theme.FG)
        y += 32

    d.text(
        (x, 348),
        f"WEEK  {w.fmt_tokens(t.week_tokens)}",
        font=theme.mono(21),
        fill=theme.MUTED,
    )
    d.text(
        (x, 378), f"${t.week_cost_usd:,.2f} est.", font=theme.mono(19), fill=theme.FAINT
    )


def _activity_zone(d: ImageDraw.ImageDraw, state: HudState, x0: int, x1: int) -> None:
    x, xr = x0 + _PAD, x1 - _PAD
    a = state.activity
    d.text(
        (xr, 22),
        f"{state.now:%a %b %-d}",
        font=theme.sans(22),
        fill=theme.MUTED,
        anchor="ra",
    )
    d.text(
        (xr, 50),
        f"{state.now:%-I:%M %p}",
        font=theme.sans(56),
        fill=theme.FG,
        anchor="ra",
    )
    d.text(
        (xr, 122), "CLAUDE CODE", font=theme.sans(24), fill=theme.ACCENT, anchor="ra"
    )
    d.line((x, 168, xr, 168), fill=theme.BORDER)

    # A stale reading must not pass for a live one: the collector has gone
    # quiet, so we can't claim ACTIVE, and the session below it is dimmed.
    live = a.active and not a.stale
    w.status_dot(d, x + 9, 200, 9, live)
    if a.stale:
        status, status_color = "STALE", theme.STALE
    elif a.active:
        status, status_color = "ACTIVE", theme.GOOD
    else:
        status, status_color = "IDLE", theme.MUTED
    d.text((x + 32, 186), status, font=theme.sans(26), fill=status_color)

    fg = theme.STALE if a.stale else theme.FG
    accent = theme.STALE if a.stale else theme.ACCENT
    muted = theme.STALE if a.stale else theme.MUTED

    y = 240
    if a.project:
        d.text((x, y), a.project, font=theme.mono(22), fill=fg)
        y += 36
    if a.model:
        d.text((x, y), a.model, font=theme.sans(22), fill=accent)
        y += 34
    if live and a.burn_rate_tpm:
        d.text(
            (x, y),
            f"{w.fmt_tokens(a.burn_rate_tpm)} tok/min",
            font=theme.mono(20),
            fill=muted,
        )


def _footer(d: ImageDraw.ImageDraw, state: HudState) -> None:
    y0 = HEIGHT - _FOOTER_H
    d.text((_PAD, y0 + 12), "TOKENS TODAY", font=theme.sans(15), fill=theme.FAINT)
    w.sparkline(
        d,
        (_PAD + 150, y0 + 14, WIDTH - _PAD, HEIGHT - 16),
        state.hourly_tokens,
        slots=24,
    )
