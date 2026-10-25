"""The rules engine: precedence, edges, summer time, overrides and the replay of the real log."""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import (
    DST_END,
    DST_START,
    NORMAL_DAY,
    TZ,
    at,
    book_of,
    load_fixture,
    make_day,
)

from shelly_control.prices import PriceBook, parse_prices
from shelly_control.rules import (
    RULES,
    Lines,
    Override,
    Settings,
    ct,
    decide,
    lines_for,
    next_changes,
    parse_settings,
    parse_until,
    plan_days,
    rule_for,
)

LINES = Lines(average=0.20, on_line=0.21, off_line=0.25, morning_line=0.25)
UTC_NOON = dt.datetime(2026, 10, 27, 11, 0, tzinfo=dt.UTC)


def flat(value: float):
    return lambda i, local: value


# -- defaults -----------------------------------------------------------------------------------


def test_defaults_are_the_factors_that_reproduce_the_relay_log():
    s = Settings()
    assert (s.on_factor, s.off_factor) == (1.05, 1.25)
    assert (s.morning_enabled, s.morning_start, s.morning_end) == (
        True,
        dt.time(5),
        dt.time(6),
    )
    assert s.morning_factor == 1.25
    assert s.negative_enabled is True
    assert s.mode == "shadow"
    assert s.average_window == "day"


# -- precedence and edges -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("price", "morning", "expected"),
    [
        (-0.01, False, "negative"),
        (-0.01, True, "negative"),
        (0.24, True, "morning"),
        (0.24, False, "between"),
        (0.20, False, "below_on"),
        (0.20, True, "morning"),
        (0.26, False, "above_off"),
        (0.26, True, "above_off"),
        # Exactly on a line keeps the state: the comparisons are strict.
        (0.21, False, "between"),
        (0.25, False, "between"),
        (0.25, True, "between"),
        (0.0, False, "below_on"),
    ],
)
def test_rule_order_is_the_scripts(price, morning, expected):
    assert rule_for(Settings(), price, LINES, morning) == expected


def test_a_price_exactly_on_a_line_built_from_prices_keeps_the_state():
    # Alternating 19 and 21 ct average 20 ct; 1.05 x 20 ct is the 21 ct switch-on
    # line. In binary floating point that line is 0.21000000000000002 and 21 ct
    # would be "under" it; in decimal, as the script's bc worked, it is on it.
    book = book_of(make_day(NORMAL_DAY, lambda i, local: 0.19 if i % 2 else 0.21))
    lines = lines_for(Settings(), book, NORMAL_DAY, TZ)
    assert (lines.average, lines.on_line) == (0.2, 0.21)
    decision = decide(Settings(), book, at(NORMAL_DAY, 0, 0, 5), False, None, TZ)
    assert decision.slot.total == 0.21
    assert decision.rule == "between" and decision.action == "keep"


def test_lines_are_cut_to_four_decimals_like_bc():
    # The average 0.20005 is 0.2000 for bc with scale=4, and so the lines.
    book = book_of(make_day(NORMAL_DAY, lambda i, local: 0.2001 if i % 2 else 0.2))
    lines = lines_for(Settings(on_factor=1.5), book, NORMAL_DAY, TZ)
    assert lines.average == 0.2
    assert lines.on_line == 0.3


def test_negative_rule_can_be_turned_off():
    settings = Settings(negative_enabled=False)
    assert rule_for(settings, -0.01, LINES, False) == "below_on"


def test_an_average_at_or_below_zero_switches_nothing_but_negative_prices_still_heat():
    lines = Lines(average=-0.01, on_line=-0.0105, off_line=-0.0125, morning_line=-0.0125)
    assert rule_for(Settings(), -0.02, lines, False) == "negative"
    assert rule_for(Settings(negative_enabled=False), -0.02, lines, False) == "no_lines"
    assert rule_for(Settings(), 0.01, lines, True) == "no_lines"


@pytest.mark.parametrize(
    ("hour", "minute", "inside"),
    [(4, 45, False), (5, 0, True), (5, 15, True), (5, 45, True), (6, 0, False)],
)
def test_morning_window_includes_its_start_and_excludes_its_end(hour, minute, inside):
    # Every quarter costs the same; only the morning line is above that price.
    book = book_of(make_day(NORMAL_DAY, flat(0.20)))
    settings = Settings(on_factor=0.9, off_factor=1.3, morning_factor=1.1)
    decision = decide(settings, book, at(NORMAL_DAY, hour, minute, 30), False, None, TZ)
    assert decision.morning is inside
    assert decision.rule == ("morning" if inside else "between")
    assert decision.target is inside


def test_morning_window_off_the_hour_works_with_hourly_prices():
    settings = Settings(morning_start=dt.time(5, 30), morning_end=dt.time(6), morning_factor=1.1)
    book = book_of(make_day(NORMAL_DAY, flat(0.20), step=3600))
    # The 05:00 to 06:00 hour overlaps the window, so the morning rule applies.
    decision = decide(settings, book, at(NORMAL_DAY, 5, 40), False, None, TZ)
    assert decision.morning and decision.rule == "morning"
    decision = decide(settings, book, at(NORMAL_DAY, 6, 10), False, None, TZ)
    assert not decision.morning


def test_morning_window_can_be_moved_and_turned_off():
    book = book_of(make_day(NORMAL_DAY, flat(0.20)))
    moved = Settings(on_factor=0.9, morning_start=dt.time(4), morning_end=dt.time(4, 30))
    assert decide(moved, book, at(NORMAL_DAY, 4, 15), False, None, TZ).rule == "morning"
    assert decide(moved, book, at(NORMAL_DAY, 5, 0), False, None, TZ).rule == "between"
    off = Settings(on_factor=0.9, morning_enabled=False)
    assert decide(off, book, at(NORMAL_DAY, 5, 0), False, None, TZ).rule == "between"


# -- decisions ----------------------------------------------------------------------------------


def test_decision_carries_every_input_and_a_reason():
    book = book_of(make_day(NORMAL_DAY))
    decision = decide(Settings(), book, at(NORMAL_DAY, 18, 7), True, None, TZ)
    assert decision.rule == "above_off"
    assert decision.action == "off"
    assert decision.target is False
    assert decision.slot.total == 0.30
    assert decision.slot.start == at(NORMAL_DAY, 18, 0)
    average = (13 * 0.18 * 4 + 3 * 0.22 * 4 + 3 * 0.30 * 4 + 5 * 0.18 * 4) / 96
    assert decision.lines.average == pytest.approx(average)
    assert decision.lines.off_line == pytest.approx(1.25 * average)
    assert decision.reason == f"30.0 ct is over the {ct(1.25 * average)} switch-off line."


@pytest.mark.parametrize("current", [True, False, None])
def test_keep_keeps_the_current_state(current):
    # A flat day: every price is the average, between a line below and one above it.
    book = book_of(make_day(NORMAL_DAY, flat(0.20)))
    decision = decide(Settings(on_factor=0.9), book, at(NORMAL_DAY, 12), current, None, TZ)
    assert decision.rule == "between"
    assert decision.action == "keep"
    assert decision.target is current


def test_no_price_for_this_quarter_decides_nothing():
    book = book_of(make_day(NORMAL_DAY))
    decision = decide(Settings(), book, at(NORMAL_DAY + dt.timedelta(days=1), 0, 5), True, None, TZ)
    assert (decision.rule, decision.action, decision.target) == ("no_prices", "keep", True)
    assert decision.slot is None


def test_an_incomplete_day_decides_nothing():
    # The average needs the whole day: half a day of prices is not enough.
    book = book_of(make_day(NORMAL_DAY)[:48])
    decision = decide(Settings(), book, at(NORMAL_DAY, 3), False, None, TZ)
    assert decision.rule == "no_prices"
    assert decision.slot is not None
    assert "48" in decision.reason


def test_an_empty_book_decides_nothing():
    decision = decide(Settings(), PriceBook(), UTC_NOON, None, None, TZ)
    assert decision.rule == "no_prices"
    assert decision.target is None


# -- overrides ----------------------------------------------------------------------------------


def override(action: str, until: dt.datetime) -> Override:
    return Override(action, until.astimezone(dt.UTC), at(NORMAL_DAY, 8), "owner")


@pytest.mark.parametrize("action", ["on", "off"])
def test_override_goes_before_every_rule(action):
    book = book_of(make_day(NORMAL_DAY, flat(-0.05)))
    held = override(action, at(NORMAL_DAY, 18))
    decision = decide(Settings(), book, at(NORMAL_DAY, 12), None, held, TZ)
    assert (decision.rule, decision.action, decision.target) == ("override", action, action == "on")
    assert "18:00" in decision.reason


def test_override_works_without_prices():
    decision = decide(
        Settings(), PriceBook(), UTC_NOON, None, override("on", at(NORMAL_DAY, 18)), TZ
    )
    assert decision.rule == "override"
    assert decision.target is True


def test_an_expired_override_is_ignored():
    book = book_of(make_day(NORMAL_DAY))
    held = override("on", at(NORMAL_DAY, 18))
    decision = decide(Settings(), book, at(NORMAL_DAY, 18, 0, 1), True, held, TZ)
    assert decision.rule == "above_off"


# -- summer time --------------------------------------------------------------------------------


def test_the_day_summer_time_ends_has_100_quarters():
    book = book_of(make_day(DST_END))
    assert len(book.day(DST_END, TZ)) == 100
    assert book.complete(DST_END, TZ)
    assert lines_for(Settings(), book, DST_END, TZ) is not None


def test_the_day_summer_time_starts_has_92_quarters():
    book = book_of(make_day(DST_START))
    assert len(book.day(DST_START, TZ)) == 92
    assert book.complete(DST_START, TZ)
    # 02:00 to 03:00 does not exist that night; 03:00 follows 01:45.
    decision = decide(Settings(), book, at(DST_START, 3, 5), False, None, TZ)
    assert decision.slot.start.astimezone(TZ).strftime("%H:%M") == "03:00"


def test_the_repeated_hour_finds_each_quarter_by_its_start_time():
    # Each quarter gets its own price, so the slot found shows which one it was.
    book = book_of(make_day(DST_END, lambda i, local: 0.10 + i / 1000))
    first = decide(Settings(), book, at(DST_END, 2, 20, fold=0), False, None, TZ)
    second = decide(Settings(), book, at(DST_END, 2, 20, fold=1), False, None, TZ)
    assert first.slot.total == pytest.approx(0.10 + 9 / 1000)
    assert second.slot.total == pytest.approx(0.10 + 13 / 1000)
    assert first.slot.start.astimezone(TZ).utcoffset() == dt.timedelta(hours=2)
    assert second.slot.start.astimezone(TZ).utcoffset() == dt.timedelta(hours=1)
    # Reading the list by position (hour * 4) would have taken the first one twice.
    last = decide(Settings(), book, at(DST_END, 23, 50), False, None, TZ)
    assert last.slot.total == pytest.approx(0.10 + 99 / 1000)


def test_hourly_fallback_prices_still_decide():
    book = book_of(make_day(NORMAL_DAY, step=3600))
    assert len(book.day(NORMAL_DAY, TZ)) == 24
    decision = decide(Settings(), book, at(NORMAL_DAY, 18, 40), True, None, TZ)
    assert decision.rule == "above_off"
    assert decision.slot.end - decision.slot.start == dt.timedelta(hours=1)


# -- the average --------------------------------------------------------------------------------


def test_average_over_today_and_tomorrow_once_tomorrow_is_known():
    tomorrow = NORMAL_DAY + dt.timedelta(days=1)
    today_only = book_of(make_day(NORMAL_DAY, flat(0.20)))
    both = book_of(make_day(NORMAL_DAY, flat(0.20)), make_day(tomorrow, flat(0.40)))
    settings = Settings(average_window="day_and_next")
    assert lines_for(settings, today_only, NORMAL_DAY, TZ).average == pytest.approx(0.20)
    assert lines_for(settings, both, NORMAL_DAY, TZ).average == pytest.approx(0.30)
    assert lines_for(Settings(), both, NORMAL_DAY, TZ).average == pytest.approx(0.20)


# -- settings validation ------------------------------------------------------------------------


def test_settings_accept_form_strings_and_json_values():
    settings, errors = parse_settings(
        {
            "on_factor": "1,10",
            "off_factor": 1.4,
            "morning_start": "04:30",
            "morning_end": "05:45",
            "morning_enabled": False,
            "mode": "live",
            "average_window": "day_and_next",
        },
        Settings(),
    )
    assert errors == {}
    assert settings.on_factor == 1.1
    assert settings.off_factor == 1.4
    assert settings.morning_start == dt.time(4, 30)
    assert settings.morning_enabled is False
    assert settings.mode == "live"


@pytest.mark.parametrize(
    ("raw", "field"),
    [
        ({"on_factor": "abc"}, "on_factor"),
        ({"on_factor": "0.4"}, "on_factor"),
        ({"on_factor": "3.01"}, "on_factor"),
        ({"on_factor": "nan"}, "on_factor"),
        ({"on_factor": True}, "on_factor"),
        ({"off_factor": 1.0}, "off_factor"),
        ({"morning_start": "05:10"}, "morning_start"),
        ({"morning_start": "5:00"}, "morning_start"),
        ({"morning_start": "07:00"}, "morning_end"),
        ({"morning_enabled": "yes"}, "morning_enabled"),
        ({"mode": "auto"}, "mode"),
        ({"average_window": "week"}, "average_window"),
        ({"toggle": 1}, "toggle"),
    ],
)
def test_settings_refuse_bad_values_with_a_message(raw, field):
    base = Settings()
    settings, errors = parse_settings(raw, base)
    assert settings is base
    assert field in errors
    assert errors[field].endswith(".")


def test_equal_lines_are_allowed():
    settings, errors = parse_settings({"on_factor": 1.2, "off_factor": 1.2}, Settings())
    assert errors == {}
    assert settings.on_factor == settings.off_factor


def test_settings_round_trip_through_json():
    original = Settings(on_factor=1.1, morning_start=dt.time(4, 15), mode="live")
    settings, errors = parse_settings(original.to_json(), Settings())
    assert errors == {}
    assert settings == original


# -- override times -----------------------------------------------------------------------------


def test_until_is_the_next_time_the_clock_shows_it():
    now = at(NORMAL_DAY, 14, 10).astimezone(dt.UTC)
    assert parse_until("18:00", now, TZ) == at(NORMAL_DAY, 18).astimezone(dt.UTC)
    assert parse_until("07:00", now, TZ) == at(NORMAL_DAY + dt.timedelta(days=1), 7).astimezone(
        dt.UTC
    )


def test_until_in_the_repeated_hour_is_the_next_time_the_clock_shows_it():
    # 02:10 winter time on the day summer time ends: 02:15 summer time has
    # passed, but 02:15 winter time is five minutes away.
    now = at(DST_END, 2, 10, fold=1)
    until = parse_until("02:15", now, TZ)
    assert until - now.astimezone(dt.UTC) == dt.timedelta(minutes=5)
    # And before the change, 02:15 is the first, summer-time one.
    now = at(DST_END, 1, 50)
    assert parse_until("02:15", now, TZ) - now.astimezone(dt.UTC) == dt.timedelta(minutes=25)


def test_until_takes_an_iso_time():
    now = at(NORMAL_DAY, 14).astimezone(dt.UTC)
    assert parse_until("2026-10-27T17:30:00+01:00", now, TZ) == at(NORMAL_DAY, 17, 30)


@pytest.mark.parametrize(
    "value", ["", None, "25:00", "noon", "2026-10-27T13:00:00+01:00", "2026-10-29T12:00:00+01:00"]
)
def test_until_refuses_bad_or_out_of_range_times(value):
    now = at(NORMAL_DAY, 14).astimezone(dt.UTC)
    with pytest.raises(ValueError, match=r"\w"):
        parse_until(value, now, TZ)


# -- the plan -----------------------------------------------------------------------------------


def test_plan_covers_every_quarter_and_marks_past_and_current():
    book = book_of(make_day(NORMAL_DAY))
    now = at(NORMAL_DAY, 12, 7)
    (plan,) = plan_days(Settings(), book, now, True, None, TZ, [NORMAL_DAY])
    assert len(plan.slots) == 96
    assert [p.current for p in plan.slots].index(True) == 48
    assert all(p.past for p in plan.slots[:48])
    assert not any(p.past for p in plan.slots[48:])
    on = {p.slot.start.astimezone(TZ).strftime("%H:%M") for p in plan.slots if p.target}
    assert "12:00" in on and "18:00" not in on


def test_plan_includes_an_active_override_then_resumes_the_rules():
    book = book_of(make_day(NORMAL_DAY))
    now = at(NORMAL_DAY, 16)
    held = override("on", at(NORMAL_DAY, 18, 30))
    (plan,) = plan_days(Settings(), book, now, True, held, TZ, [NORMAL_DAY])
    by_time = {p.slot.start.astimezone(TZ).strftime("%H:%M"): p for p in plan.slots}
    assert by_time["18:15"].rule == "override" and by_time["18:15"].target is True
    assert by_time["18:30"].rule == "above_off" and by_time["18:30"].target is False


def test_next_changes_reads_the_plan_ahead():
    tomorrow = NORMAL_DAY + dt.timedelta(days=1)
    book = book_of(make_day(NORMAL_DAY), make_day(tomorrow))
    now = at(NORMAL_DAY, 12, 7)
    plans = plan_days(Settings(), book, now, True, None, TZ, [NORMAL_DAY, tomorrow])
    changes = next_changes(plans, now, True)
    assert [(m.astimezone(TZ).strftime("%H:%M"), on) for m, on in changes] == [
        ("17:00", False),
        ("20:00", True),
    ]


def test_ct_uses_a_real_minus_sign():
    assert ct(-0.004) == "\N{MINUS SIGN}0.4 ct"
    assert ct(0.1296) == "13.0 ct"


def test_every_rule_name_is_in_the_bounded_list():
    names = {"override", "no_prices", "negative", "no_lines", "morning", "below_on"}
    assert names | {"above_off", "between"} == set(RULES)


# -- the replay: the real prices of 20 to 24 October 2026 and the relay's own log ---------------


def replay_switches(settings: Settings) -> list[tuple[str, bool]]:
    slots = parse_prices(load_fixture("replay_2026-10-20_to_24.json"))
    book = PriceBook(slots, 0)
    days = book.days(TZ)
    before = dt.datetime(2026, 10, 19, tzinfo=dt.UTC)
    switches, state = [], None
    for plan in plan_days(settings, book, before, None, None, TZ, days):
        for planned in plan.slots:
            if state is not None and planned.target != state:
                switches.append(
                    (planned.slot.start.astimezone(TZ).strftime("%m-%d %H:%M"), planned.target)
                )
            state = planned.target
    return switches


def test_default_rules_reproduce_the_relay_log_at_the_exact_quarter():
    switches = replay_switches(Settings())
    # The five switches the Shelly logged...
    logged = [
        ("10-23 02:15", True),
        ("10-23 08:00", False),
        ("10-23 10:30", True),
        ("10-24 17:45", False),
        ("10-24 21:30", True),
    ]
    assert switches[-5:] == logged
    # ...and the three before the log starts, which the power readings confirm:
    # the boiler starting at 21:45 after hours cold, and no heating from 17:30.
    assert switches[:3] == [("10-20 18:45", False), ("10-20 21:45", True), ("10-22 17:30", False)]
    assert len(switches) == 8


LOGGED = [
    ("10-23 02:15", True),
    ("10-23 08:00", False),
    ("10-23 10:30", True),
    ("10-24 17:45", False),
    ("10-24 21:30", True),
]


def test_the_published_scripts_factors_do_not_reproduce_the_log():
    switches = replay_switches(Settings(on_factor=1.2, off_factor=1.5, morning_factor=1.5))
    assert not [switch for switch in LOGGED if switch in switches]


@pytest.mark.parametrize("on", [1.02, 1.03, 1.04, 1.05, 1.06])
@pytest.mark.parametrize("off", [1.23, 1.24, 1.25, 1.26, 1.27, 1.28, 1.29])
def test_which_factors_reproduce_the_log(on, off):
    """The ranges the README states: the log alone, and the log with the power readings."""
    switches = replay_switches(Settings(on_factor=on, off_factor=off, morning_factor=off))
    five = switches[-5:] == LOGGED
    assert five == (1.03 <= on <= 1.05 and 1.24 <= off <= 1.28)
    # The boiler starting at 21:45 on 20 October after hours cold needs a
    # switch-off earlier that evening.
    cold_start = ("10-20 21:45", True) in switches
    assert (five and cold_start) == (1.03 <= on <= 1.05 and 1.24 <= off <= 1.26)
