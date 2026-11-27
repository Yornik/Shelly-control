"""The cost tiles and the column chart: exact euros per day, honest about what was not measured."""

from __future__ import annotations

import datetime as dt
import re

import pytest
from conftest import TZ, at, ts
from flask import Flask, render_template_string

from shelly_control.cost import PLOT_H, TOP, build_cost
from shelly_control.power import Sample, intervals

# Friday 27 November 2026: the week began on Monday the 23rd.
TODAY = dt.date(2026, 11, 27)


def record(days: dict[dt.date, float], price=0.25, now_hour=15, partial=None):
    """One reading a minute, with 1 kW drawn for `hours` hours from midnight on each day."""
    samples, wh = [], 0.0
    start = at(min(days), 0).timestamp()
    end = at(TODAY, now_hour).timestamp()
    t = start
    while t <= end:
        day = dt.datetime.fromtimestamp(t, TZ).date()
        samples.append(Sample(t, wh, price))
        hours = days.get(day, 0)
        local_hour = (t - at(day, 0).timestamp()) / 3600
        if local_hour < hours:
            wh += 1000 / 60
        t += 60
    return intervals(samples), at(TODAY, now_hour)


def test_the_tiles_give_today_yesterday_and_the_week_in_euros():
    days = {TODAY - dt.timedelta(days=n): 4.0 for n in range(5)}  # Mon..Fri, 4 kWh each
    stretches, now = record(days)
    cost = build_cost(stretches, now, TZ)
    today, yesterday, week = cost.tiles
    assert today.label == "Today so far" and today.value == "\N{EURO SIGN}1.00"
    assert yesterday.value == "\N{EURO SIGN}1.00"
    assert week.value == "\N{EURO SIGN}5.00" and week.sub == "since Monday"
    assert "20.0 kWh at 25.0 ct per kWh on average" == week.detail
    assert today.note is None and week.note is None


def test_the_columns_are_the_last_seven_days_with_today_lighter_and_labelled():
    days = {TODAY - dt.timedelta(days=n): 2.0 + n for n in range(7)}
    stretches, now = record(days)
    chart = build_cost(stretches, now, TZ).chart
    assert [c.label for c in chart.columns] == ["Sat", "Sun", "Mon", "Tue", "Wed", "Thu", "Today"]
    assert chart.columns[-1].is_today and not chart.columns[0].is_today
    # Today, yesterday and the dearest day carry a value; the rest do not.
    labelled = [c.date for c in chart.columns if c.labelled]
    assert len(labelled) == 3
    assert chart.columns[-1].labelled and chart.columns[-2].labelled
    # A column never rises above the plot, and rests on the baseline.
    assert all(c.path for c in chart.columns)
    for c in chart.columns:
        tops = [float(v) for v in re.findall(r"V(\d+\.?\d*)", c.path)]
        assert min(tops) >= TOP - 0.01
    assert chart.base == TOP + PLOT_H


def test_a_week_without_readings_says_so_instead_of_drawing_zeros():
    cost = build_cost([], at(TODAY, 15), TZ)
    assert cost.chart is None and "No readings yet" in cost.empty
    assert [t.value for t in cost.tiles] == ["\N{EN DASH}"] * 3


def test_a_day_the_readings_only_partly_cover_says_so():
    # The service was down until 12:00: only three hours are measured.
    days = {TODAY: 5.0}
    stretches, now = record(days, now_hour=15)
    stretches = [s for s in stretches if s.t0 >= ts(TODAY, 12)]
    cost = build_cost(stretches, now, TZ)
    assert cost.tiles[0].note == "Readings cover 3.0 h of 15 h."
    assert "Readings cover 3.0 h of 15 h." in cost.chart.columns[-1].text


def test_energy_without_a_price_is_counted_in_kwh_but_not_in_euros():
    samples = [
        Sample(ts(TODAY, 8) + i * 60, i * 16.667, 0.3 if i < 30 else None) for i in range(60)
    ]
    cost = build_cost(intervals(samples), at(TODAY, 9), TZ)
    today = cost.tiles[0]
    assert today.value == "\N{EURO SIGN}0.15"
    assert "had no price" in today.note
    nothing_priced = [Sample(ts(TODAY, 8) + i * 60, i * 16.667, None) for i in range(60)]
    tile = build_cost(intervals(nothing_priced), at(TODAY, 9), TZ).tiles[0]
    assert tile.value == "\N{EN DASH}" and "no price known" in tile.detail


def test_each_past_day_is_priced_at_the_price_it_was_used_at():
    cheap = Sample(ts(TODAY - dt.timedelta(days=2), 3), 0, 0.10)
    cheap_end = Sample(ts(TODAY - dt.timedelta(days=2), 3, 1), 1000 / 60 * 60, 0.10)
    dear = Sample(ts(TODAY - dt.timedelta(days=1), 18), 5000, 0.50)
    dear_end = Sample(ts(TODAY - dt.timedelta(days=1), 18, 1), 5000 + 1000, 0.50)
    stretches = intervals([cheap, cheap_end]) + intervals([dear, dear_end])
    chart = build_cost(stretches, at(TODAY, 9), TZ).chart
    by_label = {c.date: c for c in chart.columns}
    assert by_label["Wed 25 Nov"].euros == pytest.approx(1.0 * 0.10)
    assert by_label["Thu 26 Nov"].euros == pytest.approx(1.0 * 0.50)


def render_cost(cost) -> str:
    app = Flask("shelly_control")
    with app.app_context():
        source = '{% from "_cost.html" import cost_section %}{{ cost_section(c) }}'
        return render_template_string(source, c=cost)


def test_the_section_is_valid_markup_without_style_attributes():
    days = {TODAY - dt.timedelta(days=n): 3.0 for n in range(7)}
    stretches, now = record(days)
    html = render_cost(build_cost(stretches, now, TZ))
    assert " style=" not in html
    assert html.count('class="cday') == 7
    assert "Cost per day, last 7 days" in html
    assert "Show the cost per day as a table" in html
    assert html.count("<tr") == 8  # a header and seven days
    assert "\N{EURO SIGN}0.75" in html  # 3 kWh at 25 ct


def test_text_from_the_data_is_escaped():
    cost = build_cost([], at(TODAY, 15), TZ)
    assert "<script" not in render_cost(cost)
