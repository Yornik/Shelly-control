"""The controller against the fake relay and the fake exporter: when it switches, and when not."""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import threading
import time

import pytest
from conftest import NORMAL_DAY, PASSWORD, TZ, Clock, ts
from fake_exporter import HOME_ID

from shelly_control.controller import OUTCOMES, TRIGGERS, Controller
from shelly_control.logs import JsonFormatter
from shelly_control.prices import PriceClient
from shelly_control.rules import Settings, parse_until
from shelly_control.shelly import ShellyClient
from shelly_control.store import Store, StoreError

LIVE = Settings(mode="live")

# The fake exporter's default day: 18 ct at night and midday, 22 ct from 07:00
# to 10:00 and 30 ct from 17:00 to 20:00, so the average is 20 ct. The lines:
# on below 21 ct, off above 25 ct, morning (05:00 to 06:00) below 25 ct.


def at_time(clock: Clock, hour: int, minute: int = 0, second: int = 5) -> float:
    clock.set(ts(NORMAL_DAY, hour, minute, second))
    return clock.now


# -- shadow mode --------------------------------------------------------------------------------


def test_shadow_mode_decides_and_logs_but_never_switches(make_controller, fake_shelly, clock):
    controller = make_controller()
    entry = controller.evaluate("quarter")
    assert entry["rule"] == "below_on"
    assert entry["action"] == "on"
    assert entry["outcome"] == "would_switch"
    assert entry["sent"] is False
    assert fake_shelly.sets == []
    assert fake_shelly.output is False


def test_shadow_mode_counts_a_disagreement_only_if_it_lasts(make_controller, fake_shelly, clock):
    controller = make_controller()
    controller.tick()
    assert controller.snapshot().counters.shadow_disagreements == 0
    # A minute later the relay is still off: the old controller disagrees.
    clock.now += 60
    assert controller.tick() == "recheck"
    assert controller.snapshot().counters.shadow_disagreements == 1
    assert fake_shelly.sets == []


def test_shadow_mode_does_not_count_when_the_old_controller_catches_up(
    make_controller, fake_shelly, clock
):
    controller = make_controller()
    controller.tick()
    fake_shelly.output = True
    clock.now += 60
    assert controller.tick() == "recheck"
    assert controller.snapshot().counters.shadow_disagreements == 0


def test_shadow_mode_keeps_the_state_it_would_have_set(make_controller, fake_shelly, clock):
    controller = make_controller()
    at_time(clock, 6)
    controller.evaluate("quarter")
    at_time(clock, 7)
    entry = controller.evaluate("quarter")
    # Between the lines: keep what this service would have set (on), not the relay (off).
    assert entry["rule"] == "between"
    assert entry["target"] is True
    assert entry["relay_before"] is False
    assert entry["outcome"] == "kept"


# -- live mode ----------------------------------------------------------------------------------


def test_live_mode_switches_only_when_the_relay_differs(make_controller, fake_shelly):
    controller = make_controller(settings=LIVE)
    first = controller.evaluate("quarter")
    assert (first["outcome"], first["sent"], first["relay_after"]) == ("switched", True, True)
    second = controller.evaluate("quarter")
    assert second["outcome"] == "unchanged"
    assert fake_shelly.sets == ["id=0&on=true"]
    assert controller.snapshot().counters.switch_commands == {"on": 1, "off": 0}


def test_live_mode_switches_off_over_the_line(make_controller, fake_shelly, clock):
    fake_shelly.output = True
    controller = make_controller(settings=LIVE)
    at_time(clock, 18)
    entry = controller.evaluate("quarter")
    assert entry["rule"] == "above_off"
    assert fake_shelly.sets == ["id=0&on=false"]
    assert fake_shelly.output is False


def test_keep_sends_nothing(make_controller, fake_shelly, clock):
    controller = make_controller(settings=LIVE)
    at_time(clock, 8)
    entry = controller.evaluate("quarter")
    assert (entry["action"], entry["outcome"]) == ("keep", "kept")
    assert fake_shelly.sets == []


def test_switching_turned_off_for_the_installation_holds_back(make_controller, fake_shelly):
    controller = make_controller(settings=LIVE, allow_switching=False)
    entry = controller.evaluate("quarter")
    assert entry["outcome"] == "held_back"
    assert "ALLOW_SWITCHING" in entry["note"]
    assert fake_shelly.sets == []


def test_a_protection_error_holds_back(make_controller, fake_shelly):
    fake_shelly.errors = ["overtemp"]
    controller = make_controller(settings=LIVE)
    entry = controller.evaluate("quarter")
    assert entry["outcome"] == "held_back"
    assert "overtemp" in entry["note"]
    assert fake_shelly.sets == []


def test_a_relay_that_does_not_follow_is_a_failure_and_retried(make_controller, fake_shelly, clock):
    fake_shelly.stuck = True
    controller = make_controller(settings=LIVE)
    assert controller.tick() == "start"
    entry = controller.snapshot().last_entry
    assert entry["outcome"] == "failed"
    assert "still off" in entry["error"]
    assert controller.snapshot().counters.switch_failures["not_confirmed"] == 1
    fake_shelly.stuck = False
    clock.now += 60
    assert controller.tick() == "retry"
    assert fake_shelly.output is True


def test_the_relay_is_spared_rapid_switching(make_controller, fake_shelly, clock):
    controller = make_controller(settings=LIVE)
    assert controller.tick() == "start"
    assert fake_shelly.output is True
    clock.now += 5
    controller.set_override("off", controller.snapshot().book.slots[-1].start, "me")
    assert controller.tick() == "change"
    entry = controller.snapshot().last_entry
    assert entry["outcome"] == "held_back"
    assert entry["note"].startswith("Waiting 55 s")
    assert fake_shelly.output is True
    clock.now += 55
    assert controller.tick() == "retry"
    assert fake_shelly.output is False
    assert len(fake_shelly.sets) == 2


def test_a_clock_stepping_back_over_the_quarter_does_not_switch_back(
    make_controller, fake_shelly, clock
):
    """An NTP step of 10 s back, just after the quarter that switched off."""
    monotonic = Clock(1000.0)
    fake_shelly.output = True
    controller = make_controller(settings=LIVE, monotonic=monotonic)
    at_time(clock, 16, 45, 5)
    assert controller.tick() == "start"
    at_time(clock, 17, 0, 5)
    monotonic.now += 900
    assert controller.tick() == "quarter"
    assert fake_shelly.sets == ["id=0&on=false"]
    # One real second later the wall clock reads 16:59:56.
    at_time(clock, 16, 59, 56)
    monotonic.now += 1
    assert controller.tick() is None
    # ...and 17:00:05 again is the same quarter, already done.
    at_time(clock, 17, 0, 5)
    monotonic.now += 9
    assert controller.tick() is None
    assert fake_shelly.sets == ["id=0&on=false"]


def test_the_switch_interval_holds_when_the_wall_clock_steps_back(
    make_controller, fake_shelly, clock
):
    monotonic = Clock(1000.0)
    controller = make_controller(settings=LIVE, monotonic=monotonic)
    assert controller.tick() == "start"
    assert fake_shelly.sets == ["id=0&on=true"]
    # The wall clock jumps back an hour, a second later: a corrected clock,
    # whose quarter is evaluated. It wants the relay off, but only a second
    # has passed on the monotonic clock.
    clock.now -= 3600
    monotonic.now += 1
    until = dt.datetime.fromtimestamp(clock.now + 1800, dt.UTC)
    controller.set_override("off", until, "me")
    assert controller.tick() == "change"
    entry = controller.snapshot().last_entry
    assert entry["outcome"] == "held_back" and entry["note"].startswith("Waiting 59 s")
    assert fake_shelly.sets == ["id=0&on=true"]


def test_a_large_step_back_evaluates_the_quarter_the_clock_now_shows(make_controller, clock):
    controller = make_controller()
    assert controller.tick() == "start"
    clock.now -= 3600
    assert controller.tick() == "quarter"


# -- overrides ----------------------------------------------------------------------------------


def test_an_override_switches_in_shadow_mode_and_the_rules_return_after(
    make_controller, fake_shelly, clock
):
    fake_shelly.output = True
    controller = make_controller()
    until = parse_until("13:00", dt.datetime.fromtimestamp(clock.now, dt.UTC), TZ)
    controller.set_override("off", until, "me")
    assert controller.tick() == "change"
    assert fake_shelly.sets == ["id=0&on=false"]
    assert controller.snapshot().last_entry["rule"] == "override"
    # The override ends at 13:00; in shadow mode the rules then only watch.
    at_time(clock, 13, 0, 1)
    assert controller.tick() == "override_end"
    snap = controller.snapshot()
    assert snap.override is None
    assert snap.last_entry["rule"] == "below_on"
    assert snap.last_entry["outcome"] == "would_switch"
    assert fake_shelly.sets == ["id=0&on=false"]
    # Shadow mode does not switch the relay back, and the log says so.
    assert any(
        e.get("summary") == "Override ended at 13:00; in shadow mode the relay stays as the "
        "override left it"
        for e in snap.entries
    )


def test_an_override_switches_back_in_live_mode(make_controller, fake_shelly, clock):
    controller = make_controller(settings=LIVE)
    assert controller.tick() == "start"
    assert fake_shelly.output is True
    clock.now += 120
    until = dt.datetime.fromtimestamp(ts(NORMAL_DAY, 12, 30), dt.UTC)
    controller.set_override("off", until, "me")
    controller.tick()
    assert fake_shelly.output is False
    at_time(clock, 12, 30, 1)
    assert controller.tick() == "override_end"
    assert fake_shelly.output is True


def test_an_override_ending_tomorrow_names_the_day_in_the_log(make_controller, clock):
    controller = make_controller(settings=LIVE)
    until = dt.datetime.fromtimestamp(ts(NORMAL_DAY, 11) + 86400, dt.UTC)
    controller.set_override("on", until, "me")
    assert controller.snapshot().entries[-1]["summary"] == "Override: heat until Wed 11:00"


def test_clearing_an_override(make_controller, clock):
    controller = make_controller()
    assert controller.clear_override("me") is False
    until = dt.datetime.fromtimestamp(clock.now + 3600, dt.UTC)
    controller.set_override("on", until, "me")
    assert controller.clear_override("me") is True
    assert controller.snapshot().override is None


# -- missing prices and a missing device --------------------------------------------------------


def test_no_prices_means_no_action_and_says_why(make_controller, fake_exporter, fake_shelly):
    fake_exporter.http_status = 503
    controller = make_controller(settings=LIVE)
    entry = controller.evaluate("quarter")
    assert (entry["rule"], entry["outcome"]) == ("no_prices", "no_action")
    assert "HTTP 503" in entry["reason"]
    assert fake_shelly.sets == []
    assert controller.snapshot().counters.price_read_errors["http"] == 1


def test_a_wrong_home_id_is_no_action(make_controller, fake_exporter, fake_shelly):
    fake_exporter.home_id = "22222222-2222-4222-8222-222222222222"
    controller = make_controller(settings=LIVE)
    entry = controller.evaluate("quarter")
    assert entry["outcome"] == "no_action"
    assert "TIBBER_HOME_ID" in entry["reason"]
    assert fake_shelly.sets == []


def test_the_price_cache_carries_an_exporter_outage(make_controller, fake_exporter, tmp_path):
    make_controller().evaluate("quarter")
    assert (tmp_path / "state" / "prices.json").exists()
    fake_exporter.http_status = 503
    restarted = make_controller()
    entry = restarted.evaluate("quarter")
    assert entry["rule"] == "below_on"
    assert restarted.snapshot().price_error is not None


def test_an_unreachable_device_is_retried_a_few_times(
    fake_exporter, closed_port_url, tmp_path, clock
):
    controller = Controller(
        shelly=ShellyClient(closed_port_url, timeout=1),
        prices=PriceClient(fake_exporter.url, HOME_ID, timeout=2),
        store=Store(tmp_path, clock),
        tz=TZ,
        defaults=LIVE,
        allow_switching=True,
        clock=clock,
    )
    assert controller.tick() == "start"
    entry = controller.snapshot().last_entry
    assert entry["outcome"] == "held_back"
    assert entry["relay_before"] is None
    assert "Switch.GetStatus failed" in entry["error"]
    triggers = []
    for _ in range(6):
        clock.now += 60
        triggers.append(controller.tick())
    assert triggers.count("retry") == 3
    assert controller.snapshot().counters.device_read_errors["connection"] == 2 * 4


def test_a_refused_password_backs_off(make_controller, fake_shelly, clock):
    controller = make_controller(settings=LIVE)
    fake_shelly.password = PASSWORD
    entry = controller.evaluate("quarter")
    assert "requires a password" in entry["error"]
    sent = len(fake_shelly.requests)
    clock.now += 120
    entry = controller.evaluate("change")
    assert "waiting after the device refused the password" in entry["error"]
    assert len(fake_shelly.requests) == sent


# -- the scheduler ------------------------------------------------------------------------------


def test_evaluations_follow_the_quarter_hours(make_controller, fake_shelly, clock):
    # The relay agrees with the rules, so no re-check is scheduled.
    fake_shelly.output = True
    controller = make_controller()
    at_time(clock, 12, 0, 2)
    assert controller.tick() is None
    at_time(clock, 12, 0, 5)
    assert controller.tick() == "start"
    at_time(clock, 12, 7, 0)
    assert controller.tick() is None
    at_time(clock, 12, 15, 4)
    assert controller.tick() is None
    at_time(clock, 12, 15, 5)
    assert controller.tick() == "quarter"
    assert controller.tick() is None


def test_a_long_pause_gives_one_evaluation_not_one_per_missed_quarter(
    make_controller, fake_shelly, clock
):
    fake_shelly.output = True
    controller = make_controller()
    controller.tick()
    at_time(clock, 15, 47, 0)
    assert controller.tick() == "quarter"
    assert controller.tick() is None
    assert controller.snapshot().counters.evaluations == 2


def test_a_clock_step_backwards_evaluates_once(make_controller, fake_shelly, clock):
    fake_shelly.output = True
    controller = make_controller()
    controller.tick()
    at_time(clock, 11, 3, 0)
    assert controller.tick() == "quarter"
    assert controller.tick() is None


def test_a_change_is_evaluated_at_once(make_controller, clock):
    controller = make_controller()
    controller.tick()
    _, errors = controller.update_settings({"on_factor": "1.10"}, "me")
    assert errors == {}
    assert controller.tick() == "change"
    assert controller.snapshot().last_entry["on_line"] == pytest.approx(0.22)


def test_the_loop_sleeps_until_the_next_thing_but_at_most_30_s(make_controller, fake_shelly, clock):
    fake_shelly.output = True
    controller = make_controller(price_refresh=3600)
    controller.tick()
    assert controller.seconds_until_due() == 30
    at_time(clock, 12, 14, 50)
    assert controller.seconds_until_due() == pytest.approx(15)


def test_prices_are_read_again_when_idle(make_controller, fake_exporter, fake_shelly, clock):
    fake_shelly.output = True
    controller = make_controller(price_refresh=300)
    controller.tick()
    reads = len(fake_exporter.requests)
    at_time(clock, 12, 4, 0)
    assert controller.tick() is None
    assert len(fake_exporter.requests) == reads
    at_time(clock, 12, 5, 6)
    assert controller.tick() is None
    assert len(fake_exporter.requests) == reads + 1


def test_one_evaluation_at_a_time(make_controller, fake_shelly):
    controller = make_controller()
    original = controller.shelly.get_switch_status
    running, most = [0], [0]
    lock = threading.Lock()

    def slow(switch_id):
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
        time.sleep(0.1)
        with lock:
            running[0] -= 1
        return original(switch_id)

    controller.shelly.get_switch_status = slow
    threads = [threading.Thread(target=controller.evaluate, args=("change",)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert most[0] == 1
    assert controller.snapshot().counters.evaluations == 4


def test_the_loop_runs_and_stops(fake_shelly, fake_exporter, tmp_path):
    controller = Controller(
        shelly=ShellyClient(fake_shelly.url, timeout=2),
        prices=PriceClient(fake_exporter.url, HOME_ID, timeout=2),
        store=Store(tmp_path),
        tz=TZ,
        defaults=Settings(),
        evaluation_delay=0,
    )
    stop = threading.Event()
    thread = threading.Thread(target=controller.run, args=(stop,))
    thread.start()
    assert controller.wait_for_evaluation(0, 5)
    stop.set()
    controller.stop()
    thread.join(5)
    assert not thread.is_alive()


# -- settings, state and logs -------------------------------------------------------------------


def test_settings_changes_are_saved_and_logged(make_controller, tmp_path):
    controller = make_controller()
    settings, errors = controller.update_settings({"on_factor": 1.1, "mode": "live"}, "me")
    assert errors == {}
    saved = json.loads((tmp_path / "state" / "state.json").read_text())
    assert saved["settings"]["on_factor"] == 1.1
    assert saved["updated_by"] == "me"
    entry = controller.snapshot().entries[-1]
    assert entry["kind"] == "change"
    assert entry["summary"] == "Mode: shadow to live; Switch-on line: 1.05 to 1.10"


def test_invalid_settings_change_nothing(make_controller, tmp_path):
    controller = make_controller()
    settings, errors = controller.update_settings({"on_factor": "9"}, "me")
    assert "on_factor" in errors
    assert settings == Settings()
    assert not (tmp_path / "state" / "state.json").exists()


def test_a_failing_volume_never_stops_a_decision(make_controller):
    controller = make_controller()

    def broken(entries):
        raise StoreError("decisions.json could not be written (Read-only file system)")

    controller.store.save_decisions = broken
    entry = controller.evaluate("quarter")
    snap = controller.snapshot()
    assert entry["rule"] == "below_on"
    assert snap.last_entry is entry
    assert "Read-only" in snap.store_error
    assert snap.counters.state_write_errors == 1


def test_mode_change_to_live_starts_from_the_relay(make_controller, fake_shelly, clock):
    controller = make_controller()
    at_time(clock, 6)
    controller.evaluate("quarter")
    assert controller.snapshot().current is True
    controller.update_settings({"mode": "live"}, "me")
    assert controller.snapshot().current is False


def test_one_json_line_per_evaluation_without_secrets(make_controller, fake_shelly):
    fake_shelly.password = PASSWORD
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("shelly-control")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        controller = make_controller(settings=LIVE)
        controller.shelly = ShellyClient(fake_shelly.url, PASSWORD, timeout=2)
        controller.evaluate("quarter")
    finally:
        logger.removeHandler(handler)
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    evaluations = [line for line in lines if line["event"] == "evaluation"]
    assert len(evaluations) == 1
    required = {
        "ts", "slot_start", "price", "average", "on_line", "off_line", "rule", "action",
        "mode", "override", "sent", "relay_before", "relay_after", "reason", "error",
    }  # fmt: skip
    assert required <= set(evaluations[0])
    assert evaluations[0]["sent"] is True
    text = stream.getvalue()
    assert PASSWORD not in text
    assert HOME_ID not in text


def test_triggers_and_outcomes_stay_in_their_bounded_lists(make_controller, fake_shelly, clock):
    controller = make_controller(settings=LIVE)
    controller.tick()
    fake_shelly.stuck = True
    at_time(clock, 18)
    controller.tick()
    clock.now += 60
    controller.tick()
    entries = [e for e in controller.snapshot().entries if e["kind"] == "evaluation"]
    assert {e["trigger"] for e in entries} <= set(TRIGGERS)
    assert {e["outcome"] for e in entries} <= set(OUTCOMES)
    assert {e["outcome"] for e in entries} >= {"switched", "failed"}
