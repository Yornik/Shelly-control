"""The dashboard is only useful if it asks for metrics the service publishes."""

from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.dashboard_rules import expressions
from scripts.sample_exposition import exposition

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboards" / "shelly-control.json"

# Queried from other exporters on purpose: the relay as shelly-bridge reads it.
EXTERNAL_METRICS = {"shelly_switch_output"}
EXTERNAL_PREFIXES = ("tibber_",)


def load():
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))


def exported_names() -> set[str]:
    """Every sample name /metrics serves, from the same stand-ins CI feeds promtool."""
    return {
        line.split("{")[0].split(" ")[0]
        for line in exposition().splitlines()
        if line and not line.startswith("#")
    }


def test_every_queried_metric_is_exported():
    queried = set()
    for _, expr in expressions(load()):
        queried |= set(re.findall(r"\bshelly_control_[a-z0-9_]+", expr))
    assert queried, "dashboard has no shelly_control queries"
    assert queried - exported_names() == set()


def test_only_known_external_metrics_are_queried():
    for where, expr in expressions(load()):
        for name in re.findall(r"\b(shelly_[a-z0-9_]+|tibber_[a-z0-9_]+)", expr):
            assert (
                name.startswith("shelly_control_")
                or name in EXTERNAL_METRICS
                or name.startswith(EXTERNAL_PREFIXES)
            ), (where, name)


def test_every_query_variable_is_replaced():
    for where, expr in expressions(load()):
        assert "$" not in expr, where


def test_stat_tiles_show_current_values_only():
    for panel in load()["panels"]:
        if panel["type"] == "stat":
            for target in panel["targets"]:
                assert target.get("instant") is True, panel["title"]


def test_no_value_text_never_looks_healthy():
    for panel in load()["panels"]:
        no_value = panel.get("fieldConfig", {}).get("defaults", {}).get("noValue")
        assert no_value in (None, "No data"), panel["title"]


def test_gaps_stay_gaps():
    for panel in load()["panels"]:
        custom = panel.get("fieldConfig", {}).get("defaults", {}).get("custom", {})
        if panel["type"] in ("timeseries", "state-timeline"):
            assert custom.get("spanNulls") is False, panel["title"]


def test_bars_count_each_event_once():
    # $__rate_interval is wider than the step, so bars built on it overlap.
    for panel in load()["panels"]:
        custom = panel.get("fieldConfig", {}).get("defaults", {}).get("custom", {})
        if custom.get("drawStyle") == "bars":
            assert panel.get("interval") == "1h", panel["title"]
            for target in panel["targets"]:
                assert "$__rate_interval" not in target["expr"], panel["title"]
                assert "[1h]" in target["expr"], panel["title"]


def test_no_home_id_device_id_or_address_is_hardcoded():
    text = DASHBOARD.read_text(encoding="utf-8")
    assert "home_id" not in text
    assert "shellyplus" not in text
    assert not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", text)


def test_panel_ids_are_unique_and_layout_does_not_overlap():
    panels = load()["panels"]
    ids = [p["id"] for p in panels]
    assert len(ids) == len(set(ids))
    cells = set()
    for p in panels:
        g = p["gridPos"]
        assert g["x"] + g["w"] <= 24, p["title"]
        for x in range(g["x"], g["x"] + g["w"]):
            for y in range(g["y"], g["y"] + g["h"]):
                assert (x, y) not in cells, f"{p['title']} overlaps another panel"
                cells.add((x, y))


def test_dashboard_is_portable():
    dashboard = load()
    assert dashboard["id"] is None
    assert dashboard["uid"] == "shelly-control"
    variables = {v["name"]: v for v in dashboard["templating"]["list"]}
    assert variables["datasource"]["type"] == "datasource"
    for panel in dashboard["panels"]:
        if panel["type"] != "row":
            assert panel["datasource"]["uid"] == "${datasource}", panel["title"]
            for target in panel["targets"]:
                assert target["datasource"]["uid"] == "${datasource}", panel["title"]


def test_the_dashboard_says_it_is_unofficial():
    assert "Unofficial" in load()["description"]
