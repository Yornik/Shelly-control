"""What the page says in each situation: the hero, the banners, the log and the status."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest
from conftest import DST_END, NORMAL_DAY, TZ, at, ts

from shelly_control.rules import Override, Settings
from shelly_control.shelly import DeviceInfo
from shelly_control.view import build_view, relay_history

LIVE = Settings(mode="live")


@pytest.fixture
def snapshot(make_controller, fake_shelly, clock):
    """A snapshot after one decision, at a time the test picks."""

    def take(hour: int, minute: int = 0, *, settings: Settings = LIVE, relay_on: bool = True):
        fake_shelly.output = relay_on
        fake_shelly.apower = 1050.0 if relay_on else 0.0
        clock.set(ts(NORMAL_DAY, hour, minute, 5))
        controller = make_controller(settings=settings)
        controller.tick()
        return controller.snapshot()

    return take


@pytest.mark.parametrize(
    ("hour", "relay_on", "sentence"),
    [
        (12, True, "Power costs 18.0 ct per kWh now, under the 21.0 ct switch-on line."),
        (18, False, "Power costs 30.0 ct per kWh now, over the 25.0 ct switch-off line."),
        (
            8,
            True,
            "Power costs 22.0 ct per kWh now, between the 21.0 ct and 25.0 ct lines, so the "
            "relay stays as it is.",
        ),
        (
            5,
            True,
            "Morning top-up: power costs 18.0 ct per kWh, under the 25.0 ct morning line.",
        ),
    ],
)
def test_the_live_sentence_names_the_rule(snapshot, hour, relay_on, sentence):
    view = build_view(snapshot(hour, relay_on=relay_on), TZ, "Boiler")
    assert view.hero.why == sentence
    assert view.hero.word == ("Heating" if relay_on else "Off")


def test_on_but_drawing_nothing_means_up_to_temperature(snapshot):
    snap = snapshot(12)
    snap = replace(snap, relay=replace(snap.relay, apower=2.0))
    hero = build_view(snap, TZ, "Boiler").hero
    assert hero.word == "On"
    assert "up to temperature" in hero.detail


def test_an_override_is_explained(snapshot, make_controller):
    snap = snapshot(12)
    held = Override("off", at(NORMAL_DAY, 18).astimezone(dt.UTC), at(NORMAL_DAY, 12), "me")
    decision = replace(snap.decision, rule="override", action="off", target=False, override=held)
    view = build_view(replace(snap, decision=decision, override=held), TZ, "Boiler")
    assert view.hero.why == "Set by hand to stay off until 18:00. Then the rules decide again."
    assert view.override_text == "Set to keep off until 18:00."


def test_a_vacation_and_its_heat_up_are_explained(snapshot):
    snap = snapshot(12)
    until = at(NORMAL_DAY, 18).astimezone(dt.UTC) + dt.timedelta(days=7)
    held = Override("off", until, at(NORMAL_DAY, 12), None, "vacation")
    decision = replace(snap.decision, rule="override", action="off", target=False, override=held)
    view = build_view(replace(snap, decision=decision, override=held), TZ, "Boiler")
    assert view.override_kind == "vacation"
    assert (
        view.override_text
        == "Vacation: the boiler is switched off completely until Tue 3 Nov 18:00."
    )
    assert "Then the boiler heats for 2 hours whatever the price" in view.override_after
    assert view.hero.why.startswith(
        "Vacation: the boiler is switched off completely until Tue 3 Nov"
    )
    heat_up = Override(
        "on", at(NORMAL_DAY, 14).astimezone(dt.UTC), at(NORMAL_DAY, 12), None, "heat_up"
    )
    decision = replace(snap.decision, rule="override", action="on", target=True, override=heat_up)
    view = build_view(replace(snap, decision=decision, override=heat_up), TZ, "Boiler")
    assert view.override_kind == "heat_up"
    assert view.override_text == "Heating after the vacation until 14:00, whatever the price."
    assert view.hero.why == (
        "Heating after the vacation until 14:00, whatever the price. Then the rules decide again."
    )


def test_the_vacation_days_run_from_today_to_the_limit(snapshot):
    view = build_view(snapshot(12), TZ, "Boiler")
    days = [value for value, _ in view.vacation_days]
    assert days[0] == "2026-10-27"
    assert days[-1] == "2026-12-26"
    assert len(days) == 61
    assert view.vacation_days[0][1] == "Today"
    assert view.vacation_days[1][1] == "Tomorrow"
    assert view.vacation_days[2][1] == "Thu 29 Oct"
    assert view.vacation_default_day == "2026-11-03"
    assert view.override_kind == ""


def test_a_failed_switch_is_shown_under_the_sentence(snapshot):
    snap = snapshot(12)
    entry = dict(snap.last_entry, outcome="failed", error="Sent on, but the relay is still off.")
    hero = build_view(replace(snap, last_entry=entry), TZ, "Boiler").hero
    assert hero.detail == "Sent on, but the relay is still off."


def test_banners(snapshot):
    snap = snapshot(12, settings=Settings(mode="live"))
    snap = replace(
        snap,
        store_problem="The saved settings could not be used.",
        store_error="state.json could not be written (Read-only file system)",
        price_error="the exporter answered HTTP 503",
        relay=replace(snap.relay, errors=("overtemp",)),
        switching_allowed=False,
        device=DeviceInfo("shellyplus1pm-x", "SNSW-001P16EU", "2", "1.7.5", False),
    )
    texts = [(b.level, b.text) for b in build_view(snap, TZ, "Boiler").banners]
    assert texts[0][0] == "error" and "kept in memory only" in texts[0][1]
    assert ("warning", "The saved settings could not be used.") in texts
    assert any(level == "warning" and "Deciding with the prices read at" in t for level, t in texts)
    assert any("reports overtemp" in t for _, t in texts)
    assert any("Live mode, but switching is turned off" in t for _, t in texts)
    assert any("Authentication is off on the device" in t for _, t in texts)


def test_log_rows_say_what_happened(snapshot):
    snap = snapshot(12)
    base = dict(snap.last_entry)
    entries = (
        dict(base, outcome="switched", target=True),
        dict(base, outcome="failed", target=True, error="boom"),
        dict(base, outcome="would_switch", target=False),
        dict(base, outcome="held_back", target=True, note="Waiting 30 s"),
        dict(base, outcome="kept", target=None),
        dict(base, outcome="no_action", target=None),
        dict(base, outcome="unchanged", target=False),
        {"kind": "change", "ts": base["ts"], "what": "override", "summary": "Override", "by": "me"},
        {"kind": "evaluation", "ts": "not a time"},
    )
    rows = build_view(replace(snap, entries=entries), TZ, "Boiler").log
    labels = [r.label for r in rows]
    assert labels == [
        "Override",
        "Off",
        "No price",
        "Keep",
        "Held back",
        "Would switch off",
        "Failed",
        "Switched on",
    ]
    assert rows[0].who == "me"
    assert rows[4].text.endswith("Waiting 30 s")
    assert rows[6].text.endswith("boom")


def test_entries_from_another_day_show_the_weekday(snapshot):
    snap = snapshot(12)
    entry = dict(snap.last_entry, ts="2026-10-26T11:00:05+00:00")
    (row,) = build_view(replace(snap, entries=(entry,)), TZ, "Boiler").log
    assert row.time == "Mon 12:00"


def test_status_rows(snapshot):
    snap = snapshot(12)
    snap = replace(
        snap,
        updated_at="2026-10-27T10:00:00+00:00",
        updated_by="me",
        device=DeviceInfo("shellyplus1pm-x", "SNSW-001P16EU", "2", "1.7.5", True),
    )
    status = {row.name: row.value for row in build_view(snap, TZ, "Boiler").status}
    assert status["Prices"].startswith("known until the end of tomorrow")
    assert status["Relay"].startswith("on, 1050 W, last changed by init")
    assert status["Device"] == "SNSW-001P16EU, firmware 1.7.5"
    assert status["Switching"] == "allowed for this installation"
    # The name is the proxy's header: shown, and said to be no more than that.
    assert status["Last change"] == "11:00, by me (as named by the Tailscale proxy)"


def test_override_times_cover_the_next_day_in_quarters(snapshot):
    view = build_view(snapshot(12), TZ, "Boiler")
    values = [value for value, _ in view.override_options]
    # Full times with their offset: a choice left standing on an open page
    # is refused once it has passed, never moved to tomorrow.
    assert values[0] == "2026-10-27T12:15:00+01:00" and len(values) == 96
    assert view.override_options[-1] == ("2026-10-28T12:00:00+01:00", "12:00 tomorrow")
    assert view.override_default == "2026-10-27T14:00:00+01:00"


def test_override_times_offer_the_repeated_hour_twice(make_controller, clock):
    clock.set(at(DST_END, 1, 50))
    view = build_view(make_controller().snapshot(), TZ, "Boiler")
    labels = [label for _, label in view.override_options[:8]]
    assert labels[:6] == [
        "02:00 summer time",
        "02:15 summer time",
        "02:30 summer time",
        "02:45 summer time",
        "02:00 winter time",
        "02:15 winter time",
    ]


@pytest.mark.parametrize(
    ("hour", "relay_on", "sentence"),
    [
        (12, True, "The rules want it on, and it already is: 18.0 ct is under the 21.0 ct"),
        (12, False, "The rules would switch it on: 18.0 ct is under the 21.0 ct"),
        (18, False, "The rules want it off, and it already is: 30.0 ct is over"),
        (8, True, "The rules would leave the relay as it is: 22.0 ct is between"),
    ],
)
def test_the_shadow_sentence_compares_the_rules_with_the_relay(snapshot, hour, relay_on, sentence):
    view = build_view(snapshot(hour, settings=Settings(), relay_on=relay_on), TZ, "Boiler")
    assert view.hero.why.startswith(sentence)
    assert view.hero.watching.startswith("Shadow mode: this service decides and logs")
    assert view.hero.plan is None or view.hero.plan.startswith("The rules would")
    assert (view.mode_label, view.mode_cls) == ("Shadow mode", "shadow")


def test_the_shadow_plan_says_would(snapshot):
    view = build_view(snapshot(12, settings=Settings()), TZ, "Boiler")
    assert view.hero.plan == "The rules would switch off at 17:00, then on at 20:00."


def test_live_without_switching_has_its_own_pill(snapshot):
    snap = replace(snapshot(12), switching_allowed=False)
    view = build_view(snap, TZ, "Boiler")
    assert (view.mode_label, view.mode_cls) == ("Live, switching off", "live-blocked")
    view = build_view(snapshot(12), TZ, "Boiler")
    assert (view.mode_label, view.mode_cls) == ("Live", "live")
    assert view.hero.watching is None


def test_an_override_in_shadow_mode_says_the_relay_stays(snapshot):
    snap = snapshot(12, settings=Settings())
    held = Override("on", at(NORMAL_DAY, 18).astimezone(dt.UTC), at(NORMAL_DAY, 12), "me")
    decision = replace(snap.decision, rule="override", action="on", target=True, override=held)
    view = build_view(replace(snap, decision=decision, override=held), TZ, "Boiler")
    assert view.hero.why == (
        "Set by hand to heat until 18:00. Then the relay stays as the override left it: "
        "shadow mode does not switch it back."
    )
    assert "does not switch it back" in view.override_after


def test_an_override_ending_tomorrow_names_the_day(snapshot):
    snap = snapshot(12)
    held = Override(
        "on", at(NORMAL_DAY, 11).astimezone(dt.UTC) + dt.timedelta(days=1), at(NORMAL_DAY, 12)
    )
    decision = replace(snap.decision, rule="override", action="on", target=True, override=held)
    view = build_view(replace(snap, decision=decision, override=held), TZ, "Boiler")
    assert view.hero.why.startswith("Set by hand to heat until Wed 11:00.")
    assert view.override_text == "Set to heat until Wed 11:00."


def test_a_silent_device_is_said_once_and_kept_in_status(snapshot):
    snap = snapshot(12)
    message = "Switch.GetStatus failed (ConnectionError)"
    entry = dict(
        snap.last_entry,
        outcome="held_back",
        note="The relay's state is unknown, so nothing is sent.",
        error=message,
    )
    snap = replace(snap, relay_error=message, last_entry=entry, now=snap.now + 600)
    view = build_view(snap, TZ, "Boiler")
    hero = view.hero
    assert hero.word == "Unknown"
    assert message not in hero.detail
    assert hero.detail.startswith("The Shelly does not answer")
    assert hero.detail.count("unknown") == 1
    assert "At the last read, 12:00, it was on." in hero.detail
    assert sum(message in b.text for b in view.banners) == 1
    status = {row.name: row.value for row in view.status}
    assert status["Relay"] == "not answering, last read 10 min ago"


def test_empty_days_say_what_is_missing(make_controller, fake_exporter, clock):
    fake_exporter.http_status = 503
    clock.set(at(NORMAL_DAY, 14, 22))
    controller = make_controller()
    controller.tick()
    today, tomorrow = build_view(controller.snapshot(), TZ, "Boiler").days
    assert today.empty.startswith("There are no prices for today yet.")
    assert tomorrow.empty.startswith("No prices for tomorrow either.")


# -- v1.1.0: why nothing switches, and the relay's history -------------------------------------


def flat_peak(peak: float):
    return lambda i, local: (
        peak if 17 <= local.hour < 20 else 0.22 if 7 <= local.hour < 10 else 0.18
    )


def test_no_planned_change_says_how_close_it_came(snapshot, fake_exporter):
    from conftest import make_day

    fake_exporter.prices = make_day(NORMAL_DAY, flat_peak(0.235)) + make_day(
        NORMAL_DAY + dt.timedelta(days=1), flat_peak(0.235)
    )
    view = build_view(snapshot(12), TZ, "Boiler")
    assert view.hero.plan == (
        "Plan: no change while prices are known. The closest it comes: 23.5 ct at 17:00, "
        "0.5 ct under the 24.0 ct switch-off line."
    )
    shadow = build_view(snapshot(12, settings=Settings()), TZ, "Boiler")
    assert shadow.hero.plan.startswith("The rules would change nothing while prices are known.")
    assert shadow.hero.plan.endswith("0.5 ct under the 24.0 ct switch-off line.")


def test_the_history_comes_from_the_decision_log(snapshot):
    snap = snapshot(12)
    history = relay_history(snap.entries)
    ((state, reason),) = [v for k, v in history.items()]
    assert state is True and reason.startswith("18.0 ct is under")
    view = build_view(snap, TZ, "Boiler")
    assert view.key.history
    today = view.days[0].chart
    # Before the first decision nothing was logged; the quarter of the decision was on.
    noon = [p for p in today.pins if p.cls.endswith("done")]
    assert noon and all(p.cls == "pin unknown done" for p in noon)
    # Entries that are not decisions, and broken ones, are skipped.
    assert (
        relay_history(
            [
                {"kind": "change", "ts": "2026-10-27T11:00:00+00:00"},
                {"kind": "evaluation", "ts": "x"},
            ]
        )
        == {}
    )
