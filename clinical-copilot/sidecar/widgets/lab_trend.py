"""Lab trend chart widget — Phase 11 extension.

Given a list of historical observations for one test (HbA1c, LDL,
etc.), render a sparkline plus a small data table. Output is a
self-contained HTML fragment so the chat UI can drop it into a
response without bundling external assets.

Why HTML rather than PNG:

- The trend is small (3-12 data points typical). Inline SVG is the
  simplest renderable form.
- An HTML fragment lets the chat UI link each data point to its
  citation chip without a server round-trip.

The widget does NOT call the chart layer to fetch data — the caller
provides the points it has already retrieved. Keeping the widget pure
makes it trivially testable.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import date
from typing import Final


SVG_VIEWBOX_WIDTH: Final[int] = 320
SVG_VIEWBOX_HEIGHT: Final[int] = 80


@dataclass(frozen=True)
class TrendPoint:
    """One observation point in a trend.

    ``citation_id`` is the row id from the ``citations`` table; the
    chat UI renders this as a clickable chip on the corresponding
    sparkline marker.
    """

    when: date
    value: float
    unit: str
    citation_id: str


def render_trend(
    *,
    test_name: str,
    points: list[TrendPoint],
) -> str:
    """Return a self-contained HTML fragment.

    Empty input returns the "no data" placeholder so the caller does
    not have to special-case it. Single-point inputs render a single
    dot (no line).
    """
    if not points:
        return f"<div class='trend trend-empty'>No {html.escape(test_name)} data on file.</div>"

    sorted_points = sorted(points, key=lambda p: p.when)
    if len(sorted_points) == 1:
        return _render_single_point(test_name, sorted_points[0])

    values = [p.value for p in sorted_points]
    min_v, max_v = min(values), max(values)
    span = (max_v - min_v) or 1.0
    pad_x = 10
    pad_y = 10
    width = SVG_VIEWBOX_WIDTH - 2 * pad_x
    height = SVG_VIEWBOX_HEIGHT - 2 * pad_y

    coords: list[tuple[float, float]] = []
    n = len(sorted_points)
    for i, point in enumerate(sorted_points):
        x = pad_x + (width * i / max(1, n - 1))
        y = pad_y + height * (1.0 - (point.value - min_v) / span)
        coords.append((x, y))

    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)

    markers = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" '
        f'data-citation-id="{html.escape(p.citation_id)}" '
        f'tabindex="0" role="button" '
        f'aria-label="{html.escape(test_name)} {p.value} {html.escape(p.unit)} on {p.when.isoformat()}" />'
        for (x, y), p in zip(coords, sorted_points)
    )

    rows = "".join(
        "<tr>"
        f"<td>{p.when.isoformat()}</td>"
        f"<td>{p.value:g} {html.escape(p.unit)}</td>"
        f"<td><a href='#' data-citation-id='{html.escape(p.citation_id)}'>citation</a></td>"
        "</tr>"
        for p in sorted_points
    )

    return (
        f"<div class='trend trend-multi' data-test-name='{html.escape(test_name)}'>"
        f"<svg viewBox='0 0 {SVG_VIEWBOX_WIDTH} {SVG_VIEWBOX_HEIGHT}' "
        f"role='img' aria-label='{html.escape(test_name)} trend'>"
        f"<polyline fill='none' stroke='#1a78c2' stroke-width='2' points='{polyline}' />"
        f"{markers}</svg>"
        f"<table class='trend-table'>"
        f"<thead><tr><th>Date</th><th>Value</th><th>Source</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )


def _render_single_point(test_name: str, point: TrendPoint) -> str:
    return (
        f"<div class='trend trend-single' data-test-name='{html.escape(test_name)}'>"
        f"<span class='trend-value'>{point.value:g} {html.escape(point.unit)}</span>"
        f" <span class='trend-when'>({point.when.isoformat()})</span>"
        f" <a href='#' data-citation-id='{html.escape(point.citation_id)}'>citation</a>"
        f"</div>"
    )


__all__ = ["SVG_VIEWBOX_HEIGHT", "SVG_VIEWBOX_WIDTH", "TrendPoint", "render_trend"]
