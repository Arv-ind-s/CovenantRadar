"""Accessible inline SVG fan chart: do-nothing against a remediation package.

Presentation only.  It plots quantiles the remediation simulator already
produced — the filed starting value, then the 10th, 50th and 90th percentile
of each projected quarter — for two plans on one axis, with the covenant
threshold ruled across.  It projects nothing itself.

Every quarter carries a hit target with a ``<title>``, so hovering reads the
figures, and the chart's ``<desc>`` states them for a screen reader; the
page also prints them in a table beside the chart.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape
from typing import Final

from markupsafe import Markup

_WIDTH: Final[float] = 360.0
_HEIGHT: Final[float] = 176.0
_LEFT: Final[float] = 44.0
_RIGHT: Final[float] = 14.0
_TOP: Final[float] = 14.0
_BOTTOM: Final[float] = 30.0


@dataclass(frozen=True, slots=True)
class FanSeries:
    """One plan: the filed (or pro-forma) start, then (p10, p50, p90) per quarter."""

    key: str
    label: str
    start: float | None
    quantiles: Sequence[tuple[float, float, float]]


def render_fan_svg(
    chart_id: str,
    *,
    title: str,
    quarter_labels: Sequence[str],
    threshold: float,
    threshold_label: str,
    breach_above: bool,
    series: Sequence[FanSeries],
    value_format: str = "{:.2f}x",
    reset_threshold: float | None = None,
    reset_label: str = "",
) -> Markup:
    if not series or not quarter_labels:
        return Markup('<p class="fan-state">No projection is available to chart.</p>')
    columns = len(quarter_labels)
    finite: list[float] = [threshold]
    if reset_threshold is not None:
        finite.append(reset_threshold)
    for item in series:
        if item.start is not None and math.isfinite(item.start):
            finite.append(item.start)
        for low, middle, high in item.quantiles:
            finite.extend(value for value in (low, middle, high) if math.isfinite(value))
    medians = [
        middle for item in series for _low, middle, _high in item.quantiles if math.isfinite(middle)
    ]
    starts = [item.start for item in series if item.start is not None and math.isfinite(item.start)]
    # The band can run to extreme multiples (a coverage ratio with almost no
    # finance cost); the axis is sized to what a reader compares — threshold,
    # starts and medians — and the band is clipped to it.
    anchor = [threshold, *medians, *starts]
    if reset_threshold is not None:
        anchor.append(reset_threshold)
    top_value = max(anchor) if anchor else threshold
    bottom_value = min(anchor) if anchor else threshold
    spread = max(top_value - bottom_value, abs(threshold) * 0.25, 0.1)
    upper = min(max(finite), top_value + spread * 0.6)
    lower = max(min(finite), bottom_value - spread * 0.6)
    if lower > 0 and lower < spread * 0.3:
        lower = 0.0
    if upper <= lower:
        upper = lower + 1.0

    plot_width = _WIDTH - _LEFT - _RIGHT
    plot_height = _HEIGHT - _TOP - _BOTTOM

    def x(index: int) -> float:
        return _LEFT + plot_width * index / max(columns - 1, 1)

    def y(value: float) -> float:
        clipped = min(max(value, lower), upper)
        return _TOP + plot_height * (1.0 - (clipped - lower) / (upper - lower))

    parts: list[str] = []
    safe_id = escape(chart_id)
    parts.append(
        f'<svg class="fan" id="{safe_id}" viewBox="0 0 {_WIDTH:.0f} {_HEIGHT:.0f}" '
        f'role="img" aria-labelledby="{safe_id}-title {safe_id}-desc" '
        'preserveAspectRatio="xMidYMid meet">'
    )
    parts.append(f'<title id="{safe_id}-title">{escape(title)}</title>')
    descriptions = []
    for item in series:
        medians_text = ", ".join(
            f"{label} {value_format.format(middle)}"
            for label, (_low, middle, _high) in zip(
                quarter_labels[1:], item.quantiles, strict=False
            )
            if math.isfinite(middle)
        )
        start_text = (
            value_format.format(item.start)
            if item.start is not None and math.isfinite(item.start)
            else "not computable"
        )
        descriptions.append(f"{item.label}: starts {start_text}; median {medians_text}.")
    parts.append(
        f'<desc id="{safe_id}-desc">Threshold {escape(threshold_label)}. '
        f"{escape(' '.join(descriptions))}</desc>"
    )

    # Breach side, then a recessive grid.
    threshold_y = y(threshold)
    if breach_above:
        parts.append(
            f'<rect class="fan__breach-zone" x="{_LEFT:.1f}" y="{_TOP:.1f}" '
            f'width="{plot_width:.1f}" height="{max(threshold_y - _TOP, 0):.1f}"/>'
        )
    else:
        parts.append(
            f'<rect class="fan__breach-zone" x="{_LEFT:.1f}" y="{threshold_y:.1f}" '
            f'width="{plot_width:.1f}" height="{max(_TOP + plot_height - threshold_y, 0):.1f}"/>'
        )
    for tick in _ticks(lower, upper):
        tick_y = y(tick)
        parts.append(
            f'<line class="fan__grid" x1="{_LEFT:.1f}" x2="{_WIDTH - _RIGHT:.1f}" '
            f'y1="{tick_y:.1f}" y2="{tick_y:.1f}"/>'
            f'<text class="fan__axis" x="{_LEFT - 6:.1f}" y="{tick_y + 3.5:.1f}" text-anchor="end">'
            f"{escape(_tick_label(tick))}</text>"
        )
    for index, label in enumerate(quarter_labels):
        parts.append(
            f'<text class="fan__axis" x="{x(index):.1f}" y="{_HEIGHT - 10:.1f}" '
            'text-anchor="middle">'
            f"{escape(label)}</text>"
        )

    for item in series:
        key = escape(item.key)
        upper_points = []
        lower_points = []
        if item.start is not None and math.isfinite(item.start):
            upper_points.append((x(0), y(item.start)))
            lower_points.append((x(0), y(item.start)))
        for index, (low, _middle, high) in enumerate(item.quantiles, start=1):
            if index >= columns:
                break
            upper_points.append((x(index), y(high if math.isfinite(high) else upper)))
            lower_points.append((x(index), y(low if math.isfinite(low) else upper)))
        if len(upper_points) >= 2:
            polygon = upper_points + list(reversed(lower_points))
            parts.append(
                f'<polygon class="fan__band fan__band--{key}" points="{_points(polygon)}"/>'
            )

    label_offset = -4.0 if breach_above else 11.0
    parts.append(
        f'<line class="fan__threshold" x1="{_LEFT:.1f}" x2="{_WIDTH - _RIGHT:.1f}" '
        f'y1="{threshold_y:.1f}" y2="{threshold_y:.1f}"/>'
        f'<text class="fan__threshold-label" x="{_LEFT + 4:.1f}" '
        f'y="{threshold_y + label_offset:.1f}" text-anchor="start">'
        f"{escape(threshold_label)}</text>"
    )
    if reset_threshold is not None:
        reset_y = y(reset_threshold)
        parts.append(
            f'<line class="fan__reset" x1="{_LEFT:.1f}" x2="{_WIDTH - _RIGHT:.1f}" '
            f'y1="{reset_y:.1f}" y2="{reset_y:.1f}"/>'
            f'<text class="fan__reset-label" x="{_LEFT + 4:.1f}" '
            f'y="{reset_y + label_offset:.1f}" text-anchor="start">'
            f"{escape(reset_label)}</text>"
        )

    for item in series:
        key = escape(item.key)
        line: list[tuple[float, float]] = []
        if item.start is not None and math.isfinite(item.start):
            line.append((x(0), y(item.start)))
        for index, (_low, middle, _high) in enumerate(item.quantiles, start=1):
            if index < columns and math.isfinite(middle):
                line.append((x(index), y(middle)))
        if len(line) >= 2:
            parts.append(
                f'<polyline class="fan__median fan__median--{key}" points="{_points(line)}"/>'
            )
        if line:
            end_x, end_y = line[-1]
            parts.append(
                f'<circle class="fan__end fan__end--{key}" '
                f'cx="{end_x:.1f}" cy="{end_y:.1f}" r="3.5"/>'
            )

    # Hover targets: one column per quarter, reading every plan's figures.
    step = plot_width / max(columns - 1, 1)
    for index, label in enumerate(quarter_labels):
        lines = [label]
        for item in series:
            if index == 0:
                value = item.start
                text = (
                    value_format.format(value)
                    if value is not None and math.isfinite(value)
                    else "n/a"
                )
                lines.append(f"{item.label}: {text}")
            elif index - 1 < len(item.quantiles):
                low, middle, high = item.quantiles[index - 1]
                lines.append(
                    f"{item.label}: median {_fmt(value_format, middle)} "
                    f"(80% range {_fmt(value_format, low)}–{_fmt(value_format, high)})"
                )
        left = max(x(index) - step / 2, _LEFT)
        right = min(x(index) + step / 2, _WIDTH - _RIGHT)
        parts.append(
            f'<rect class="fan__hit" x="{left:.1f}" y="{_TOP:.1f}" width="{right - left:.1f}" '
            f'height="{plot_height:.1f}"><title>{escape(chr(10).join(lines))}</title></rect>'
        )
    parts.append("</svg>")
    return Markup("".join(parts))


def _fmt(pattern: str, value: float) -> str:
    return pattern.format(value) if math.isfinite(value) else "n/a"


def _points(points: Sequence[tuple[float, float]]) -> str:
    return " ".join(f"{px:.1f},{py:.1f}" for px, py in points)


def _ticks(lower: float, upper: float) -> list[float]:
    span = upper - lower
    raw = span / 4.0
    magnitude = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    step = magnitude
    for multiple in (1, 2, 2.5, 5, 10):
        if raw <= multiple * magnitude:
            step = multiple * magnitude
            break
    first = math.ceil(lower / step) * step
    ticks: list[float] = []
    value = first
    while value <= upper + 1e-9 and len(ticks) < 8:
        ticks.append(round(value, 6))
        value += step
    return ticks


def _tick_label(value: float) -> str:
    if abs(value) >= 100:
        return f"{value:,.0f}"
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text


__all__ = ["FanSeries", "render_fan_svg"]
