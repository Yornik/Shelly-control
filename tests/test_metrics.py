"""/metrics: one consistent set of series from one controller snapshot."""

from __future__ import annotations

from conftest import TZ
from prometheus_client.parser import text_string_to_metric_families

from scripts.sample_exposition import exposition
from shelly_control.metrics import make_registry
from shelly_control.rules import ACTIONS, RULES, Settings


def samples(registry) -> dict:
    from prometheus_client import generate_latest

    text = generate_latest(registry).decode()
    found = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            found[(sample.name, tuple(sorted(sample.labels.items())))] = sample.value
    return found


def test_before_the_first_decision_only_health_series_exist(make_controller):
    controller = make_controller()
    found = samples(make_registry(controller, TZ))
    assert found[("shelly_control_live", ())] == 0
    assert all(found[("shelly_control_decision_rule", (("rule", r),))] == 0 for r in RULES)
    assert ("shelly_control_target_on", ()) not in found
    assert ("shelly_control_relay_on", ()) not in found


def test_after_a_decision(make_controller):
    controller = make_controller(settings=Settings(mode="live"))
    controller.tick()
    found = samples(make_registry(controller, TZ))
    assert found[("shelly_control_live", ())] == 1
    assert found[("shelly_control_decision_action", (("action", "on"),))] == 1
    assert sum(found[("shelly_control_decision_action", (("action", a),))] for a in ACTIONS) == 1
    assert found[("shelly_control_decision_rule", (("rule", "below_on"),))] == 1
    assert found[("shelly_control_price_per_kwh", (("kind", "slot"),))] == 0.18
    assert found[("shelly_control_price_per_kwh", (("kind", "average"),))] == 0.20
    assert found[("shelly_control_relay_on", ())] == 1
    assert found[("shelly_control_switch_commands_total", (("target", "on"),))] == 1
    assert found[("shelly_control_prices_usable", ())] == 1
    assert found[("shelly_control_override_active", ())] == 0


def test_every_error_reason_exists_from_the_start(make_controller):
    found = samples(make_registry(make_controller(), TZ))
    reasons = {
        dict(labels)["reason"]
        for (name, labels) in found
        if name == "shelly_control_switch_failures_total"
    }
    assert reasons == {"timeout", "connection", "http", "auth", "invalid_response", "not_confirmed"}


def test_no_device_readings_are_duplicated_from_shelly_bridge():
    names = {
        line.split("{")[0].split(" ")[0]
        for line in exposition().splitlines()
        if line.startswith("shelly")
    }
    assert all(name.startswith("shelly_control_") for name in names)
    assert not any(
        word in name for name in names for word in ("power", "energy", "voltage", "temperature")
    )
