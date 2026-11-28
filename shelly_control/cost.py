"""What the boiler cost: three numbers and seven columns, from the power record.

Stat tiles for today so far, yesterday and this week (since Monday), and under
them one column per day for the last seven days. The column chart is drawn as
seven small SVGs side by side, each with its own piece of the gridlines, so a
column keeps its width (22 px, round at the top, square on the baseline) at any
page width instead of being stretched with the plot. Heights are plain numbers
in the SVG attributes: no style attributes, so the page works under a
Content-Security-Policy without 'unsafe-inline'.

Euros are the energy that had a price, at the price in force when it was used,
so a day's cost is exact even though the price service only keeps today's and
tomorrow's prices. A period whose readings do not cover all of it says so.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .power import Interval, Totals, totals
from .prices import day_bounds
from .rules import ct

DAYS = 7
COL_W = 22
RADIUS = 4
PLOT_H = 84
# Room above the tallest column for its label.
TOP = 20
AXIS_H = 24
GUTTER_W = 40
# A period counts as fully measured from this share of its time on.
FULL_COVER = 0.95
# Shorter than this, a period is too young to say anything about its coverage.
MIN_EXPECTED = 300.0
STEPS = (0.1, 0.2, 0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100)


def euro(value: float) -> str:
    return f"\N{EURO SIGN}{value:.2f}"


def kwh(value: float) -> str:
    return f"{value:.1f} kWh" if value >= 10 else f"{value:.2f} kWh"


def _duration(seconds: float) -> str:
    if seconds < 7200:
        return f"{round(seconds / 60)} min"
    hours = seconds / 3600
    return f"{hours:.0f} h" if hours >= 10 else f"{hours:.1f} h"


@dataclass(frozen=True)
class Tile:
    label: str
    # "since Monday", under the label.
    sub: str
    # The euros, or an en dash without any.
    value: str
    # "6.1 kWh at 23.3 ct/kWh on average".
    detail: str
    # A caveat: readings that do not cover the period, energy without a price.
    note: str | None


@dataclass(frozen=True)
class Column:
    """One day of the column chart."""

    # "Mon", or "Today".
    label: str
    # "Thu 26 Nov": for the tooltip and the table.
    date: str
    is_today: bool
    euros: float | None
    kwh: float
    average: float | None
    # What the readout and the screen reader say.
    text: str
    # The column as a path, in the cell's own coordinates (x from the cell's middle).
    path: str
    # Where the value label sits, and whether it is shown (today, yesterday, the dearest day).
    value_y: float
    value: str
    labelled: bool
    # Table cells.
    kwh_cell: str
    euros_cell: str
    average_cell: str
    covered_cell: str


@dataclass(frozen=True)
class CostChart:
    columns: tuple[Column, ...]
    # Gridlines (y in the cell) and their gutter labels.
    grid: tuple[float, ...]
    gutter: tuple[tuple[float, str], ...]
    plot_h: int = PLOT_H
    top: int = TOP
    col_w: int = COL_W
    gutter_w: int = GUTTER_W
    height: int = TOP + PLOT_H + AXIS_H
    base: int = TOP + PLOT_H
    aria: str = ""


@dataclass(frozen=True)
class CostView:
    tiles: tuple[Tile, ...]
    chart: CostChart | None
    # Shown instead of everything while there are no readings.
    empty: str | None


def _nice(value: float) -> float:
    """The smallest of a few round steps that is at least `value`."""
    for step in STEPS:
        if step >= value:
            return step
    return math.ceil(value / 100) * 100


def _scale(peak: float) -> tuple[float, float]:
    """(step between gridlines, euros at the top of the plot): about three lines."""
    step = _nice(max(peak, 0.01) / 3)
    return step, step * max(1, math.ceil(peak / step - 1e-9))


def _column_path(height: float) -> str:
    """A column `height` px tall on the baseline: round at the top, square at the bottom."""
    if height <= 0:
        return ""
    base = TOP + PLOT_H
    half = COL_W / 2
    top = round(base - height, 2)
    r = min(RADIUS, height, half)
    return (
        f"M{-half} {base} V{round(top + r, 2)} A{r} {r} 0 0 1 {round(-half + r, 2)} {top} "
        f"H{round(half - r, 2)} A{r} {r} 0 0 1 {half} {round(top + r, 2)} V{base} Z"
    )


def _tile(
    label: str,
    sub: str,
    stretches: Sequence[Interval],
    start: dt.datetime,
    end: dt.datetime,
    now: dt.datetime,
) -> Tile:
    t = totals(stretches, start.timestamp(), end.timestamp())
    expected = (min(end, now) - start).total_seconds()
    note = _coverage_note(t, expected)
    if t.covered == 0:
        return Tile(label, sub, "\N{EN DASH}", "No readings yet", note)
    if _nothing_used(t):
        return Tile(label, sub, euro(0), "Nothing used", note)
    if t.priced_kwh == 0:
        return Tile(label, sub, "\N{EN DASH}", f"{kwh(t.kwh)}, no price known", note)
    detail = f"{kwh(t.kwh)} at {ct(t.average_price)} per kWh on average"
    unpriced = t.kwh - t.priced_kwh
    if unpriced > 0.005:
        extra = f"{kwh(unpriced)} had no price and is not in the euros."
        note = f"{note} {extra}" if note else extra
    return Tile(label, sub, euro(t.euros), detail, note)


def _nothing_used(t: Totals) -> bool:
    """The readings cover time in which the boiler used no energy: it cost nothing."""
    return t.covered > 0 and t.kwh < 0.005


def _coverage_note(t: Totals, expected: float) -> str | None:
    if expected < MIN_EXPECTED or t.covered >= expected * FULL_COVER:
        return None
    return f"Readings cover {_duration(t.covered)} of {_duration(expected)}."


def build_cost(stretches: Sequence[Interval], now: dt.datetime, tz: ZoneInfo) -> CostView:
    """The tiles and the column chart as of `now`."""
    local_now = now.astimezone(tz)
    today = local_now.date()
    monday = today - dt.timedelta(days=today.weekday())

    def span_of(first: dt.date, last: dt.date) -> tuple[dt.datetime, dt.datetime]:
        return day_bounds(first, tz)[0], day_bounds(last, tz)[1]

    tiles = (
        _tile("Today so far", "", stretches, *span_of(today, today), now),
        _tile(
            "Yesterday",
            "",
            stretches,
            *span_of(today - dt.timedelta(days=1), today - dt.timedelta(days=1)),
            now,
        ),
        _tile("This week", "since Monday", stretches, *span_of(monday, today), now),
    )
    if not stretches:
        return CostView(
            tiles,
            None,
            "No readings yet. The boiler's energy counter is read once a minute, so the "
            "first numbers appear shortly after this service has been running for a minute.",
        )

    days = [today - dt.timedelta(days=n) for n in range(DAYS - 1, -1, -1)]
    per_day = []
    for day in days:
        start, end = day_bounds(day, tz)
        t = totals(stretches, start.timestamp(), end.timestamp())
        expected = (min(end, now) - start).total_seconds()
        per_day.append((day, t, expected))
    euros_list = [t.euros for _, t, _ in per_day if t.priced_kwh > 0]
    peak = max(euros_list, default=0.0)
    step, top = _scale(peak)
    dearest = euros_list.index(peak) if euros_list else -1
    dearest_day = [d for d, t, _ in per_day if t.priced_kwh > 0][dearest] if euros_list else None

    columns = []
    for day, t, expected in per_day:
        is_today = day == today
        date_text = f"{day:%a} {day.day} {day:%b}"
        label = "Today" if is_today else f"{day:%a}"
        idle = _nothing_used(t)
        priced = t.priced_kwh > 0 or idle
        height = round(t.euros / top * PLOT_H, 2) if priced else 0.0
        average = t.average_price
        if t.covered == 0:
            text = f"{date_text}: no readings."
        elif idle:
            text = f"{date_text}: nothing used."
        elif not priced:
            text = f"{date_text}: {kwh(t.kwh)}, no price known."
        else:
            text = (
                f"{date_text}{' so far' if is_today else ''}: {euro(t.euros)}, {kwh(t.kwh)} "
                f"at {ct(average)} per kWh on average."
            )
        note = _coverage_note(t, expected)
        if note and t.covered:
            text += f" {note}"
        # The label of a short column sits just above it; of a taller one, also above it.
        value_y = round(TOP + PLOT_H - height - 5, 2)
        labelled = priced and (
            is_today or day == today - dt.timedelta(days=1) or day == dearest_day
        )
        columns.append(
            Column(
                label=label,
                date=date_text,
                is_today=is_today,
                euros=t.euros if priced else None,
                kwh=t.kwh,
                average=average,
                text=text,
                path=_column_path(height),
                value_y=value_y,
                value=euro(t.euros) if priced else "",
                labelled=labelled,
                kwh_cell=f"{t.kwh:.2f}" if t.covered else "\N{EN DASH}",
                euros_cell=f"{t.euros:.2f}" if priced else "\N{EN DASH}",
                average_cell=f"{(average or 0) * 100:.1f}"
                if average is not None
                else "\N{EN DASH}",
                covered_cell=_duration(t.covered) if t.covered else "\N{EN DASH}",
            )
        )

    lines = round(top / step)
    grid = tuple(round(TOP + PLOT_H - i * step / top * PLOT_H, 2) for i in range(1, lines + 1))
    gutter = tuple(
        (round(TOP + PLOT_H - i * step / top * PLOT_H, 2), _axis(i * step))
        for i in range(0, lines + 1)
    )
    low = euro(min(euros_list, default=0))
    aria = f"The boiler's cost per day for the last {DAYS} days, from {low} to {euro(peak)}."
    return CostView(tiles, CostChart(tuple(columns), grid, gutter, aria=aria), None)


def _axis(euros: float) -> str:
    if euros == 0:
        return "0"
    text = f"{euros:.2f}".rstrip("0").rstrip(".")
    return f"\N{EURO SIGN}{text}"
