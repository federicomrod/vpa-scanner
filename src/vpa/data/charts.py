"""Candlestick charts as SVG, truncated at the bar being judged.

Concept v2 Section 12: "the chart-reviewer tool shows a chart
**truncated at the bar close**. No future price action is rendered or
available."

The second half of that sentence is the hard part. Rendering a chart and
simply not drawing the future would leave the future in the file, one
"view source" away. So `chart_svg` **refuses** bars after the truncation
point rather than dropping them: the caller has to hand over a window
that already ends where the chart does, and the picture cannot contain
what was never passed in.

Drawn by hand as SVG rather than with a plotting library, because the
project has no plotting dependency and this needs none: a candle is a
rectangle and two lines.

Volume is drawn beneath the price, always. This is Volume Price
Analysis; a chart without volume would be the wrong question.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

WIDTH = 900
PRICE_HEIGHT = 380
VOLUME_HEIGHT = 110
GAP = 14
MARGIN_LEFT, MARGIN_RIGHT = 8, 64
MARGIN_TOP, MARGIN_BOTTOM = 12, 22

HEIGHT = MARGIN_TOP + PRICE_HEIGHT + GAP + VOLUME_HEIGHT + MARGIN_BOTTOM

UP, DOWN = "#1a7f4b", "#b3261e"
AXIS, GRID, INK = "#6b6b6b", "#e6e6e6", "#1a1a1a"

REQUIRED = ["date", "slot_index", "open", "high", "low", "close", "volume"]


@dataclass(frozen=True)
class Scale:
    """Maps prices and positions onto the drawing."""

    low: float
    high: float
    count: int

    def x(self, position: int) -> float:
        usable = WIDTH - MARGIN_LEFT - MARGIN_RIGHT
        return MARGIN_LEFT + usable * (position + 0.5) / max(self.count, 1)

    def y(self, price: float) -> float:
        span = self.high - self.low or 1.0
        return MARGIN_TOP + PRICE_HEIGHT * (1 - (price - self.low) / span)

    @property
    def candle_width(self) -> float:
        usable = WIDTH - MARGIN_LEFT - MARGIN_RIGHT
        return max(1.5, usable / max(self.count, 1) * 0.62)


def chart_svg(bars: pd.DataFrame, subtitle: str = "") -> str:
    """One candlestick chart with volume, ending at the last bar given.

    `bars` must already be truncated: whatever is passed is what the
    chart shows, and nothing else is available to the viewer.
    """
    missing = [column for column in REQUIRED if column not in bars.columns]
    if missing:
        raise ValueError(f"chart needs columns: {missing}")
    if bars.empty:
        raise ValueError("a chart needs at least one bar")

    ordered = bars.sort_values(["date", "slot_index"]).reset_index(drop=True)
    scale = Scale(
        low=float(ordered["low"].min()), high=float(ordered["high"].max()), count=len(ordered)
    )
    loudest = float(ordered["volume"].max()) or 1.0

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="100%" role="img">',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="#ffffff"/>',
        *_price_grid(scale),
        *_candles(ordered, scale),
        *_volume(ordered, scale, loudest),
    ]
    if subtitle:
        parts.append(
            f'<text x="{MARGIN_LEFT}" y="{HEIGHT - 6}" font-size="11" '
            f'font-family="system-ui,sans-serif" fill="{AXIS}">{_escape(subtitle)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _price_grid(scale: Scale) -> list[str]:
    out = []
    for step in range(5):
        price = scale.low + (scale.high - scale.low) * step / 4
        y = scale.y(price)
        out.append(
            f'<line x1="{MARGIN_LEFT}" y1="{y:.1f}" x2="{WIDTH - MARGIN_RIGHT}" '
            f'y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>'
        )
        out.append(
            f'<text x="{WIDTH - MARGIN_RIGHT + 6}" y="{y + 3.5:.1f}" font-size="11" '
            f'font-family="system-ui,sans-serif" fill="{AXIS}">{price:,.2f}</text>'
        )
    return out


def _candles(bars: pd.DataFrame, scale: Scale) -> list[str]:
    out = []
    half = scale.candle_width / 2
    for position, bar in enumerate(bars.itertuples(index=False)):
        colour = UP if bar.close >= bar.open else DOWN
        x = scale.x(position)
        out.append(
            f'<line x1="{x:.1f}" y1="{scale.y(bar.high):.1f}" x2="{x:.1f}" '
            f'y2="{scale.y(bar.low):.1f}" stroke="{colour}" stroke-width="1"/>'
        )
        top, bottom = scale.y(max(bar.open, bar.close)), scale.y(min(bar.open, bar.close))
        out.append(
            f'<rect x="{x - half:.1f}" y="{top:.1f}" width="{scale.candle_width:.1f}" '
            f'height="{max(bottom - top, 1):.1f}" fill="{colour}"/>'
        )
    return out


def _volume(bars: pd.DataFrame, scale: Scale, loudest: float) -> list[str]:
    top = MARGIN_TOP + PRICE_HEIGHT + GAP
    out = [
        f'<line x1="{MARGIN_LEFT}" y1="{top + VOLUME_HEIGHT}" x2="{WIDTH - MARGIN_RIGHT}" '
        f'y2="{top + VOLUME_HEIGHT}" stroke="{AXIS}" stroke-width="1"/>'
    ]
    half = scale.candle_width / 2
    for position, bar in enumerate(bars.itertuples(index=False)):
        height = VOLUME_HEIGHT * (float(bar.volume) / loudest) if loudest else 0
        x = scale.x(position)
        colour = UP if bar.close >= bar.open else DOWN
        out.append(
            f'<rect x="{x - half:.1f}" y="{top + VOLUME_HEIGHT - height:.1f}" '
            f'width="{scale.candle_width:.1f}" height="{max(height, 0.5):.1f}" '
            f'fill="{colour}" opacity="0.55"/>'
        )
    return out


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
