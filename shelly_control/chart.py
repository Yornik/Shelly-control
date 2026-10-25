"""The day chart as plain numbers and strings, for the _chart.html template to draw.

One chart per day: the price as a step line, the switch-on and switch-off
lines with the band between them where the relay keeps its state, the day's
average, the morning window, and under it a rail of quarter-hour pins (a
mechanical timer, unrolled) that shows the plan: a full pin where the relay is
planned on, a short bar where it is planned off, a dotted baseline where the
plan is unknown.

x is time. The plot is an SVG whose viewBox is one unit per minute of the
local day (1380, 1440 or 1500 minutes: summer time starts, a normal day,
summer time ends), stretched to the panel's width (preserveAspectRatio="none",
strokes that do not scale), while every text sits at a percentage x in the
outer SVG, so the chart fills any width without stretched letters. A missing
slot leaves a gap in the line and a hatched "no price" span, never a line that
runs on as if nothing were missing. No style attributes anywhere, so the page
works under a Content-Security-Policy without 'unsafe-inline'.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .prices import day_bounds
from .rules import DayPlan, Lines, Settings, ct, hhmm

PLOT_H = 176
RAIL_H = 28
STUB_H = 7
DOT_H = 2
GAP = 10
AXIS_H = 22
TOP = 22
GUTTER_W = 76
# Gutter labels closer than this would overlap.
LABEL_SPACING = 14
# A "no price" span narrower than this share of the day gets no words.
NODATA_LABEL_SHARE = 0.08

PLAN_WORDS = {True: "On", False: "Off", None: "Unknown"}


@dataclass(frozen=True)
class YScale:
    low: float
    high: float

    def y(self, value: float) -> float:
        return round(PLOT_H - (value - self.low) / (self.high - self.low) * PLOT_H, 2)


def y_scale(plans: list[DayPlan], morning: bool = False) -> YScale | None:
    """One scale for every day shown, so the days compare at a glance."""
    values = [p.slot.total for plan in plans for p in plan.slots]
    for plan in plans:
        if plan.lines is not None:
            values += [plan.lines.average, plan.lines.on_line, plan.lines.off_line]
            if morning:
                values.append(plan.lines.morning_line)
    if not values:
        return None
    low, high = min(values), max(values)
    step = 0.05 if high - low > 0.12 else 0.02
    y0 = math.floor(low / step) * step
    y1 = math.ceil(high / step) * step
    if y1 - y0 < step:
        y1 = y0 + step
    if low < 0:
        y0 = min(y0, -step)
    # Headroom for the label of the day's highest price.
    return YScale(y0, y1 + step / 2)


@dataclass(frozen=True)
class Mark:
    cls: str
    y: float


@dataclass(frozen=True)
class Span:
    """A stretch of the day, in minutes from local midnight."""

    x: float
    width: float


@dataclass(frozen=True)
class Pin:
    x: float
    y: float
    width: float
    height: float
    cls: str


@dataclass(frozen=True)
class Label:
    x: str
    y: float
    anchor: str
    text: str
    cls: str


@dataclass(frozen=True)
class Hit:
    x: float
    width: float
    title: str
    time: str
    price: str
    plan: str
    why: str


@dataclass(frozen=True)
class Ref:
    lead_y: float
    text_y: float
    name: str
    value: str
    cls: str


@dataclass(frozen=True)
class Row:
    time: str
    price: str
    plan: str
    plan_cls: str
    why: str
    past: bool
    current: bool


@dataclass(frozen=True)
class ChartModel:
    id: str
    title: str
    # Minutes in this local day: the width of the plot's viewBox.
    span: int
    # Slots with a price.
    n: int
    band: tuple[float, float] | None
    grid: tuple[Mark, ...]
    marks: tuple[Mark, ...]
    morning: Span | None
    morning_line: tuple[float, float, float] | None
    # Stretches without a price, as percentages (x, width) of the outer SVG, where
    # the hatching is not stretched with the plot.
    nodata: tuple[tuple[str, str], ...]
    nodata_labels: tuple[Label, ...]
    path: str
    past: float
    pins: tuple[Pin, ...]
    now_x: str | None
    labels: tuple[Label, ...]
    hits: tuple[Hit, ...]
    # Index of the hit the keyboard starts from: the current quarter, else the first ahead.
    start_hit: int
    refs: tuple[Ref, ...]
    unknown: bool
    rows: tuple[Row, ...]
    aria: str
    plot_h: int = PLOT_H
    rail_h: int = RAIL_H
    top: int = TOP
    gutter_w: int = GUTTER_W
    rail_y: int = TOP + PLOT_H + GAP
    hit_h: int = PLOT_H + GAP + RAIL_H
    total_h: int = TOP + PLOT_H + GAP + RAIL_H + AXIS_H
    axis_y: int = TOP + PLOT_H + GAP + RAIL_H + 16


def _pct(minute: float, span: float) -> str:
    return f"{minute / span * 100:.3f}%"


def _value(value: float) -> str:
    """A price as a bare number of cents, for labels next to a unit already shown."""
    return ct(value).removesuffix(" ct")


def _minutes(moment: dt.datetime, start: dt.datetime) -> float:
    return round((moment - start).total_seconds() / 60, 3)


def _refs(lines: Lines, scale: YScale, morning: bool) -> tuple[Ref, ...]:
    """Labels in the right-hand gutter, each pushed down only as far as it must."""
    named = [
        (lines.off_line, "off", "lead-th"),
        (lines.on_line, "on", "lead-th"),
        (lines.average, "avg", "lead-avg"),
    ]
    if morning and lines.morning_line != lines.off_line:
        named.append((lines.morning_line, "morn", "lead-th"))
    found = sorted((scale.y(value), name, value, cls) for value, name, cls in named)
    refs, last = [], -99.0
    for y, name, value, cls in found:
        text_y = max(y + 4, last + LABEL_SPACING)
        refs.append(Ref(TOP + y, TOP + text_y, name, _value(value), cls))
        last = text_y
    return tuple(refs)


def _grid(scale: YScale, refs: tuple[Ref, ...]) -> tuple[tuple[Mark, ...], tuple[Ref, ...]]:
    """Recessive gridlines at round prices, labelled where no line label sits."""
    span = scale.high - scale.low
    step = 0.10 if span > 0.25 else 0.05 if span > 0.10 else 0.02
    marks, labels = [], []
    taken = [r.text_y for r in refs]
    value = math.ceil(scale.low / step) * step
    while value <= scale.high + 1e-9:
        y = scale.y(value)
        if 6 <= y <= PLOT_H:
            is_zero = abs(value) < 1e-9
            if not is_zero:
                marks.append(Mark("grid", y))
            text_y = TOP + y + 4
            # The zero line's label goes in even when crowded; it carries meaning.
            if is_zero or all(abs(text_y - t) >= LABEL_SPACING for t in taken):
                text = "0" if is_zero else f"{value * 100:.0f}"
                labels.append(Ref(TOP + y, text_y, "", text, "grid-label"))
                taken.append(text_y)
        value += step
    return tuple(marks), tuple(labels)


def _gaps(plan: DayPlan, start: dt.datetime, end: dt.datetime) -> list[Span]:
    """The stretches of the day without a price."""
    gaps, cursor = [], start
    for planned in plan.slots:
        if planned.slot.start > cursor:
            gaps.append(Span(_minutes(cursor, start), _minutes(planned.slot.start, cursor)))
        cursor = max(cursor, planned.slot.end)
    if cursor < end:
        gaps.append(Span(_minutes(cursor, start), _minutes(end, cursor)))
    return gaps


def build_chart(
    plan: DayPlan,
    scale: YScale,
    now: dt.datetime,
    tz: ZoneInfo,
    chart_id: str,
    title: str,
    settings: Settings | None = None,
) -> ChartModel:
    slots = plan.slots
    lines = plan.lines
    start, end = day_bounds(plan.day, tz)
    span = round((end - start).total_seconds() / 60)
    show_morning = settings is not None and settings.morning_enabled and lines is not None

    marks: list[Mark] = []
    band = None
    if lines is not None:
        band = (scale.y(lines.off_line), round(scale.y(lines.on_line) - scale.y(lines.off_line), 2))
        marks.append(Mark("avg", scale.y(lines.average)))
        marks.append(Mark("th th-on", scale.y(lines.on_line)))
        marks.append(Mark("th th-off", scale.y(lines.off_line)))
    if scale.low < 0:
        marks.append(Mark("zero", scale.y(0)))

    morning = morning_line = None
    if show_morning:
        m_start = dt.datetime.combine(plan.day, settings.morning_start, tzinfo=tz)
        m_end = dt.datetime.combine(plan.day, settings.morning_end, tzinfo=tz)
        morning = Span(_minutes(m_start, start), _minutes(m_end, m_start))
        if lines.morning_line != lines.off_line:
            morning_line = (morning.x, morning.x + morning.width, scale.y(lines.morning_line))

    path: list[str] = []
    previous_end = None
    for planned in slots:
        x0 = _minutes(planned.slot.start, start)
        x1 = _minutes(planned.slot.end, start)
        y = scale.y(planned.slot.total)
        # A new subpath after a gap: the line never bridges a missing slot.
        path.append(f"M{x0} {y}" if planned.slot.start != previous_end else f"V{y}")
        path.append(f"H{x1}")
        previous_end = planned.slot.end

    pins = []
    for planned in slots:
        cls = {True: "pin on", False: "pin off", None: "pin unknown"}[planned.target]
        height = {True: RAIL_H, False: STUB_H, None: DOT_H}[planned.target]
        if planned.past:
            cls += " done"
        # One pin per quarter, also for an hour of the exporter's hourly fallback.
        quarter = planned.slot.start
        while quarter < planned.slot.end:
            x = _minutes(quarter, start)
            pins.append(Pin(round(x + 2, 3), RAIL_H - height, 11, height, cls))
            quarter += dt.timedelta(minutes=15)

    gaps = _gaps(plan, start, end)
    nodata_labels = tuple(
        Label(_pct(g.x + g.width / 2, span), TOP + PLOT_H / 2, "middle", "no price", "nodata-label")
        for g in gaps
        if g.width >= span * NODATA_LABEL_SHARE
    )

    now_x = None
    if start <= now < end:
        past = _minutes(now, start)
        now_x = _pct(past, span)
    else:
        past = float(span) if now >= end else 0.0

    labels: list[Label] = []
    if slots:
        totals = [p.slot.total for p in slots]
        high_i, low_i = totals.index(max(totals)), totals.index(min(totals))
        extremes = [(high_i, "ext ext-max", -6)]
        if low_i != high_i:
            extremes.append((low_i, "ext ext-min", 15))
        for i, cls, dy in extremes:
            mid = _minutes(slots[i].slot.start, start) + slots[i].slot.seconds / 120
            anchor = "start" if mid < span * 0.08 else "end" if mid > span * 0.92 else "middle"
            y = TOP + scale.y(totals[i]) + dy
            if dy > 0 and y > TOP + PLOT_H - 2:
                # No room under the lowest point: above it, clear of the pin rail.
                y = TOP + scale.y(totals[i]) - 6
            y = max(y, 11)
            labels.append(Label(_pct(mid, span), y, anchor, _value(totals[i]), cls))

    for hour in range(round(span / 60)):
        local = (start + dt.timedelta(hours=hour)).astimezone(tz)
        if local.minute == 0 and local.hour % 3 == 0:
            anchor = "start" if hour == 0 else "middle"
            labels.append(
                Label(
                    _pct(hour * 60, span),
                    TOP + PLOT_H + GAP + RAIL_H + 16,
                    anchor,
                    f"{local:%H}",
                    "hour",
                )
            )

    if morning is not None:
        labels.append(
            Label(
                _pct(morning.x + morning.width / 2, span), TOP + 12, "middle", "morning", "mlabel"
            )
        )

    hits, rows = [], []
    start_hit = 0
    found_start = False
    for i, planned in enumerate(slots):
        s_local = planned.slot.start.astimezone(tz)
        e_local = planned.slot.end.astimezone(tz)
        when = f"{hhmm(s_local)}\N{EN DASH}{hhmm(e_local)}"
        price = ct(planned.slot.total)
        word = PLAN_WORDS[planned.target]
        if not found_start and (planned.current or not planned.past):
            start_hit, found_start = i, True
        hits.append(
            Hit(
                _minutes(planned.slot.start, start),
                round(planned.slot.seconds / 60, 3),
                f"{when}: {price}, planned {word.lower()}. {planned.reason}",
                when,
                price,
                word,
                planned.reason,
            )
        )
        rows.append(
            Row(
                hhmm(s_local),
                _value(planned.slot.total),
                word,
                {True: "on", False: "off", None: "unknown"}[planned.target],
                planned.reason,
                planned.past,
                planned.current,
            )
        )

    refs = _refs(lines, scale, show_morning) if lines is not None else ()
    grid, grid_labels = _grid(scale, refs)

    if not slots:
        aria = f"{title}: no prices."
    else:
        totals = [p.slot.total for p in slots]
        aria = (
            f"{title}, in ct per kWh: the price per quarter hour from {_value(min(totals))} "
            f"to {_value(max(totals))}"
        )
        if lines is None:
            aria += ". Not all of this day's prices are known, so there is no plan."
        else:
            on_count = sum(1 for p in slots if p.target)
            aria += (
                f", with the switch lines; the relay is planned on for {on_count} of "
                f"{len(slots)} slots."
            )
        if gaps:
            aria += " Part of the day has no price."
    return ChartModel(
        id=chart_id,
        title=title,
        span=span,
        n=len(slots),
        band=band,
        grid=grid,
        marks=tuple(marks),
        morning=morning,
        morning_line=morning_line,
        nodata=tuple((_pct(g.x, span), _pct(g.width, span)) for g in gaps),
        nodata_labels=nodata_labels,
        path=" ".join(path),
        past=past,
        pins=tuple(pins),
        now_x=now_x,
        labels=tuple(labels),
        hits=tuple(hits),
        start_hit=start_hit,
        refs=refs + grid_labels,
        unknown=any(p.target is None for p in slots),
        rows=tuple(rows),
        aria=aria,
    )
