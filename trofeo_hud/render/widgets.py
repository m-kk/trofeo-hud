"""Reusable drawing primitives. All take a Draw + a box, return nothing."""

from __future__ import annotations

from PIL import ImageDraw

from .. import theme

Box = tuple[int, int, int, int]  # x0, y0, x1, y1

BAR_TOP_OFFSET = 44  # a gauge row's bar sits this far below its label line
BAR_H = 24
MARKER_W = 3
MARKER_OVERHANG = 3  # how far the marker stands proud of the pill, each side


def progress_bar(
    d: ImageDraw.ImageDraw,
    box: Box,
    pct: float,
    color: str,
    track: str = theme.PANEL,
    marker_pct: float | None = None,
) -> None:
    """A pill gauge, optionally marked with where even-pace usage would be.

    `marker_pct` is the percentage of the window already elapsed. Fill short
    of the marker means you're under pace; past it means you'll exhaust the
    window early. Omitted for windows whose elapsed fraction isn't knowable.

    The marker stands proud of the pill top and bottom rather than sitting
    inside it: at a desk's viewing distance an inset notch disappears, and
    the overhang reads at a glance whether it crosses fill or bare track.
    """
    x0, y0, x1, y1 = box
    r = (y1 - y0) // 2
    d.rounded_rectangle(box, radius=r, fill=track, outline=theme.BORDER)
    pct = max(0.0, min(100.0, pct))
    fill_w = int((x1 - x0) * pct / 100)
    if fill_w > 2 * r:
        d.rounded_rectangle((x0, y0, x0 + fill_w, y1), radius=r, fill=color)
    elif fill_w > 0:
        fill_w = max(fill_w, 2 * r)
        d.ellipse((x0, y0, x0 + fill_w, y1), fill=color)

    if marker_pct is None:
        return
    marker_pct = max(0.0, min(100.0, marker_pct))
    mx = x0 + int((x1 - x0) * marker_pct / 100)
    mx = max(x0 + 1, min(mx, x1 - MARKER_W - 1))
    d.rectangle(
        (mx, y0 - MARKER_OVERHANG, mx + MARKER_W - 1, y1 + MARKER_OVERHANG),
        fill=theme.FG,
    )


def sparkline(
    d: ImageDraw.ImageDraw,
    box: Box,
    values: list[int],
    color: str = theme.ACCENT,
    slots: int | None = None,
) -> None:
    """Bar sparkline; `slots` fixes the x-scale (e.g. 24 for a full day)."""
    x0, y0, x1, y1 = box
    n = slots or max(len(values), 1)
    peak = max(values) if values else 0
    if peak == 0:
        d.line((x0, y1 - 1, x1, y1 - 1), fill=theme.FAINT)
        return
    gap = 2
    bar_w = max(2, ((x1 - x0) - gap * (n - 1)) // n)
    h = y1 - y0
    for i, v in enumerate(values):
        bx = x0 + i * (bar_w + gap)
        bh = max(2, int(h * v / peak))
        d.rectangle((bx, y1 - bh, bx + bar_w, y1), fill=color if v else theme.FAINT)


def status_dot(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int, active: bool) -> None:
    color = theme.GOOD if active else theme.FAINT
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)


def fmt_tokens(n: int | float) -> str:
    n = float(n)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= div:
            return f"{n / div:.1f}{suffix}"
    return f"{int(n)}"


def fmt_reset_clock(now, resets_at) -> str:
    """Absolute reset time, qualified only as far as it needs to be."""
    if resets_at.date() == now.date():
        return f"{resets_at:%-I:%M %p}"
    if (resets_at - now).days < 7:
        return f"{resets_at:%a %-I:%M %p}"
    return f"{resets_at:%b %-d %-I:%M %p}"


def fmt_countdown(seconds: float) -> str:
    seconds = max(0, int(seconds))
    d_, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d_:
        return f"{d_}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"
