"""The day chart: x on real time, gaps shown as gaps, no style attributes, escaped text."""

from __future__ import annotations

import datetime as dt
import re
import xml.etree.ElementTree as ET

import pytest
from conftest import DST_END, DST_START, NORMAL_DAY, TZ, at, book_of, make_day
from flask import Flask, render_template_string

from shelly_control.chart import build_chart, y_scale
from shelly_control.rules import DayPlan, PlannedSlot, Settings, plan_days
from shelly_control.view import Key

SVG = "{http://www.w3.org/2000/svg}"


def chart_for(day: dt.date, now: dt.datetime, price=None, step=900, entries=None, settings=None):
    settings = settings or Settings()
    book = book_of(entries if entries is not None else make_day(day, price, step))
    (plan,) = plan_days(settings, book, now, True, None, TZ, [day])
    scale = y_scale([plan], morning=settings.morning_enabled)
    return plan, build_chart(plan, scale, now, TZ, "today", f"Today, {day}", settings)


def render(model) -> str:
    app = Flask("shelly_control")
    with app.app_context():
        source = '{% from "_chart.html" import day_chart %}{{ day_chart(c, key=key) }}'
        key = Key(zero=True, morning=True, unknown=True, nodata=True)
        return render_template_string(source, c=model, key=key)


def svg_of(html: str) -> ET.Element:
    start = html.index("<svg")
    end = html.index("</figure>")
    # The chart's outer <svg> and the gutter's, without the HTML around them.
    fragment = re.sub(r"</?(div|p)\b[^>]*>", "", html[start:end])
    # Our own rendered output, not untrusted input.
    return ET.fromstring(f'<root xmlns="http://www.w3.org/2000/svg">{fragment}</root>')  # noqa: S314


def minutes(day: dt.date, hour: int, minute: int = 0, fold: int = 0) -> float:
    return (at(day, hour, minute, fold=fold) - at(day, 0)).total_seconds() / 60


@pytest.mark.parametrize(
    ("day", "slots", "span"), [(NORMAL_DAY, 96, 1440), (DST_END, 100, 1500), (DST_START, 92, 1380)]
)
def test_one_hit_area_and_one_table_row_per_quarter(day, slots, span):
    plan, model = chart_for(day, at(day, 12, 7))
    assert model.n == slots
    assert model.span == span
    assert len(model.hits) == slots
    assert len(model.rows) == slots
    root = svg_of(render(model))
    assert len(root.findall(f".//{SVG}rect[@class='hit']")) == slots
    assert model.nodata == ()


def test_hour_labels_fall_on_real_time_on_the_long_day():
    plan, model = chart_for(DST_END, at(DST_END, 12))
    hours = {label.text: label.x for label in model.labels if label.cls == "hour"}
    assert hours["00"] == "0.000%"
    # 03:00 is four hours after midnight, not three: 02:00 repeats.
    assert hours["03"] == "16.000%"
    times = [row.time for row in model.rows]
    # The repeated hour says which one it is, in the table and the readout.
    assert times.count("02:00 summer time") == 1 and times.count("02:00 winter time") == 1
    assert "02:00" not in times
    assert model.hits[11].time == "02:45 summer time\N{EN DASH}02:00 winter time"


def test_x_is_time_so_a_missing_hour_leaves_a_gap():
    entries = [e for e in make_day(NORMAL_DAY) if not e["startsAt"].startswith("2026-10-27T11:")]
    plan, model = chart_for(NORMAL_DAY, at(NORMAL_DAY, 14), entries=entries)
    # Hours after the gap stay where the clock puts them.
    hours = {label.text: label.x for label in model.labels if label.cls == "hour"}
    assert hours["12"] == "50.000%" and hours["15"] == "62.500%"
    by_time = {round(h.x): h for h in model.hits}
    assert minutes(NORMAL_DAY, 12) in by_time and minutes(NORMAL_DAY, 11) not in by_time
    # The line breaks: a new subpath starts at 12:00.
    assert f"M{minutes(NORMAL_DAY, 12)} " in model.path
    assert model.nodata == (("45.833%", "4.167%"),)
    # Not all prices of the day: no lines, and the plan is unknown, never "on" or "off".
    assert plan.lines is None
    assert {p.cls for p in model.pins} == {"pin unknown", "pin unknown done"}
    assert model.unknown


def test_a_partial_day_is_drawn_on_the_whole_day():
    entries = [e for e in make_day(NORMAL_DAY) if e["startsAt"] < "2026-10-27T10:00"]
    plan, model = chart_for(NORMAL_DAY, at(NORMAL_DAY, 14, 30), entries=entries)
    hours = {label.text: label.x for label in model.labels if label.cls == "hour"}
    assert hours["06"] == "25.000%" and hours["21"] == "87.500%"
    assert model.now_x == f"{(14.5 * 60) / 1440 * 100:.3f}%"
    assert model.nodata == (("41.667%", "58.333%"),)
    assert [label.text for label in model.nodata_labels] == ["no price"]


def test_the_morning_window_and_its_own_line_are_drawn():
    settings = Settings(morning_factor=1.10)
    plan, model = chart_for(NORMAL_DAY, at(NORMAL_DAY, 12), settings=settings)
    assert model.morning.x == minutes(NORMAL_DAY, 5) and model.morning.width == 60
    assert model.morning_line[:2] == (300, 360)
    assert "morn" in [r.name for r in model.refs]
    assert any(label.text == "morning" for label in model.labels)
    # With the morning line on the switch-off line, there is no second line to draw.
    plan, model = chart_for(NORMAL_DAY, at(NORMAL_DAY, 12))
    assert model.morning is not None and model.morning_line is None
    assert "morn" not in [r.name for r in model.refs]
    plan, model = chart_for(
        NORMAL_DAY, at(NORMAL_DAY, 12), settings=Settings(morning_enabled=False)
    )
    assert model.morning is None


def test_the_unit_and_round_gridlines_are_shown():
    plan, model = chart_for(NORMAL_DAY, at(NORMAL_DAY, 12))
    html = render(model)
    assert ">ct/kWh</text>" in html
    assert [m.cls for m in model.grid] and {m.cls for m in model.grid} == {"grid"}
    grid_labels = [r.value for r in model.refs if r.cls == "grid-label"]
    assert grid_labels and all(v.isdigit() for v in grid_labels)
    # A grid label never sits on a line label.
    ys = sorted(r.text_y for r in model.refs)
    assert all(b - a >= 14 for a, b in zip(ys, ys[1:], strict=False))


def test_the_short_day_skips_the_missing_hour():
    plan, model = chart_for(DST_START, at(DST_START, 12))
    times = [row.time for row in model.rows]
    assert "02:00" not in times and times[8] == "03:00"


def test_pins_follow_the_plan_and_past_pins_are_marked():
    plan, model = chart_for(NORMAL_DAY, at(NORMAL_DAY, 12, 7))
    on = [p for p in model.pins if "on" in p.cls.split()]
    off = [p for p in model.pins if "off" in p.cls.split()]
    assert len(on) + len(off) == 96
    assert len(off) == 12  # 17:00 to 20:00 is over the switch-off line
    assert all("done" in p.cls for p in model.pins if p.x < 12 * 60)
    assert not any("done" in p.cls for p in model.pins if p.x > 12 * 60)
    assert model.now_x == f"{(12 * 60 + 7) / 1440 * 100:.3f}%"
    assert model.past == 12 * 60 + 7


def test_an_hour_of_hourly_prices_gets_four_pins():
    plan, model = chart_for(NORMAL_DAY, at(NORMAL_DAY, 12), step=3600)
    assert model.n == 24 and len(model.pins) == 96


def test_no_style_attributes_anywhere():
    plan, model = chart_for(DST_END, at(DST_END, 12))
    html = render(model)
    assert " style=" not in html
    for element in svg_of(html).iter():
        assert "style" not in element.attrib


def test_text_is_escaped():
    plan, model = chart_for(NORMAL_DAY, at(NORMAL_DAY, 12))
    planned = plan.slots[0]
    evil = PlannedSlot(planned.slot, planned.rule, True, "<script>alert(1)</script>", False, False)
    model = build_chart(
        DayPlan(plan.day, (evil, *plan.slots[1:]), plan.lines),
        y_scale([plan]),
        at(NORMAL_DAY, 12),
        TZ,
        "today",
        'Today <b>"x"</b>',
    )
    html = render(model)
    assert "<script>" not in html and "<b>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_a_negative_price_brings_zero_into_view():
    plan, model = chart_for(
        NORMAL_DAY, at(NORMAL_DAY, 12), lambda i, local: -0.02 if i == 20 else 0.2
    )
    scale = y_scale([plan])
    assert scale.low < 0
    assert any(mark.cls == "zero" for mark in model.marks)
    # Zero is named in the gutter, and is not drawn as a gridline.
    assert "0" in [r.value for r in model.refs if r.cls == "grid-label"]
    assert all(abs(m.y - scale.y(0)) > 0.5 for m in model.grid)


def test_the_lowest_price_label_stays_clear_of_the_pin_rail():
    plan, model = chart_for(
        NORMAL_DAY, at(NORMAL_DAY, 12), lambda i, local: -0.04 if i == 50 else 0.2
    )
    low = next(label for label in model.labels if label.cls == "ext ext-min")
    assert low.y <= model.top + model.plot_h - 2


def test_the_gutter_labels_never_overlap():
    plan, model = chart_for(NORMAL_DAY, at(NORMAL_DAY, 12), lambda i, local: 0.2)
    lines = [r for r in model.refs if r.name]
    assert [r.name for r in lines] == ["off", "on", "avg"]
    ys = sorted(r.text_y for r in model.refs)
    assert all(b - a >= 14 for a, b in zip(ys, ys[1:], strict=False))


def test_the_chart_is_a_labelled_group_with_a_live_readout():
    plan, model = chart_for(NORMAL_DAY, at(NORMAL_DAY, 12, 7))
    html = render(model)
    assert 'role="group" aria-roledescription="chart"' in html
    assert 'aria-live="polite" aria-atomic="true"' in html
    assert 'aria-current="time"' in html and ", now</span>" in html
    assert model.start_hit == 48
    assert "ct per kWh" in model.aria


def test_hourly_days_draw_24_slots():
    plan, model = chart_for(NORMAL_DAY, at(NORMAL_DAY, 12), step=3600)
    assert model.n == 24 and model.span == 1440
    assert [h.width for h in model.hits] == [60] * 24
