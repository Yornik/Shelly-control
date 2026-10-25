"""/metrics: what the controller decided, why, and how healthy its inputs are.

Every series is built at scrape time from one controller snapshot, so the
numbers on one scrape always belong together. The device's own readings
(power, energy, voltage, temperature) are not here: shelly-bridge exports
those as shelly_*. This service exports only what a decision needs, under
shelly_control_*.
"""

from __future__ import annotations

from collections.abc import Iterator
from zoneinfo import ZoneInfo

from prometheus_client import CollectorRegistry, disable_created_metrics
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.gc_collector import GCCollector
from prometheus_client.platform_collector import PlatformCollector
from prometheus_client.process_collector import ProcessCollector

from .controller import Controller, Snapshot, prices_usable
from .rules import ACTIONS, RULES

# A *_created series next to every counter only doubles the series count.
disable_created_metrics()

PRICE_KINDS = ("slot", "average", "on_line", "off_line", "morning_line")


def _gauge(name: str, doc: str, value: float | None) -> Iterator[Metric]:
    if value is not None:
        metric = GaugeMetricFamily(name, doc)
        metric.add_metric([], float(value))
        yield metric


def _flag(value: bool | None) -> float | None:
    return None if value is None else float(value)


class ControlCollector:
    def __init__(self, controller: Controller, tz: ZoneInfo) -> None:
        self.controller = controller
        self.tz = tz

    def collect(self) -> Iterator[Metric]:
        snap = self.controller.snapshot()
        yield from self._mode(snap)
        yield from self._decision(snap)
        yield from self._relay(snap)
        yield from self._prices(snap)
        yield from self._counters(snap)

    def _mode(self, snap: Snapshot) -> Iterator[Metric]:
        yield from _gauge(
            "shelly_control_live",
            "1 in live mode (the rules switch the relay), 0 in shadow mode (decide and log only).",
            float(snap.settings.mode == "live"),
        )
        yield from _gauge(
            "shelly_control_switching_allowed",
            "1 if ALLOW_SWITCHING lets this installation switch the relay at all.",
            float(snap.switching_allowed),
        )
        active = snap.override is not None and snap.override.until.timestamp() > snap.now
        yield from _gauge(
            "shelly_control_override_active",
            "1 while a manual override is in force.",
            float(active),
        )
        if active:
            yield from _gauge(
                "shelly_control_override_until_timestamp_seconds",
                "Unix time the active override ends.",
                snap.override.until.timestamp(),
            )
        yield from _gauge(
            "shelly_control_state_saved",
            "1 if the last write to the state directory worked, 0 while writes fail.",
            float(snap.store_error is None),
        )

    def _decision(self, snap: Snapshot) -> Iterator[Metric]:
        decision = snap.decision
        action = GaugeMetricFamily(
            "shelly_control_decision_action",
            "1 for the action of the latest decision (on, off or keep), 0 for the others.",
            labels=["action"],
        )
        rule = GaugeMetricFamily(
            "shelly_control_decision_rule",
            "1 for the rule that made the latest decision, 0 for the others.",
            labels=["rule"],
        )
        for name in ACTIONS:
            action.add_metric([name], float(decision is not None and decision.action == name))
        for name in RULES:
            rule.add_metric([name], float(decision is not None and decision.rule == name))
        yield action
        yield rule
        if decision is None:
            return
        yield from _gauge(
            "shelly_control_target_on",
            "1 if the latest decision wants the relay on, 0 if off. Absent while unknown.",
            _flag(decision.target),
        )
        yield from _gauge(
            "shelly_control_morning_window",
            "1 if the latest decision's quarter is inside the enabled morning window.",
            float(decision.morning),
        )
        prices = GaugeMetricFamily(
            "shelly_control_price_per_kwh",
            "Inputs of the latest decision, per kWh in the price's currency: the quarter's "
            "price (slot), the day's average and the three lines derived from it.",
            labels=["kind"],
        )
        values = {"slot": decision.slot.total if decision.slot else None}
        if decision.lines is not None:
            values.update(
                average=decision.lines.average,
                on_line=decision.lines.on_line,
                off_line=decision.lines.off_line,
                morning_line=decision.lines.morning_line,
            )
        for kind in PRICE_KINDS:
            if values.get(kind) is not None:
                prices.add_metric([kind], values[kind])
        yield prices
        yield from _gauge(
            "shelly_control_last_evaluation_timestamp_seconds",
            "Unix time of the latest evaluation.",
            snap.last_evaluation_at,
        )

    def _relay(self, snap: Snapshot) -> Iterator[Metric]:
        if snap.relay is not None:
            yield from _gauge(
                "shelly_control_relay_on",
                "1 if the relay was on when last read, 0 if off.",
                float(snap.relay.output),
            )
        yield from _gauge(
            "shelly_control_relay_read_success",
            "1 if the latest read of the relay worked.",
            float(snap.relay is not None and snap.relay_error is None),
        )
        yield from _gauge(
            "shelly_control_relay_last_read_timestamp_seconds",
            "Unix time of the latest successful read of the relay.",
            snap.relay_read_at,
        )

    def _prices(self, snap: Snapshot) -> Iterator[Metric]:
        yield from _gauge(
            "shelly_control_prices_up",
            "1 if the latest read from the tibber-exporter worked.",
            float(snap.price_attempt_at is not None and snap.price_error is None),
        )
        yield from _gauge(
            "shelly_control_prices_usable",
            "1 if the known prices cover this quarter and the whole of its day, so the "
            "rules can decide.",
            float(prices_usable(snap, self.tz)),
        )
        yield from _gauge(
            "shelly_control_prices_last_success_timestamp_seconds",
            "Unix time of the latest successful read from the tibber-exporter.",
            snap.price_success_at,
        )
        if snap.book.slots:
            yield from _gauge(
                "shelly_control_prices_known_until_timestamp_seconds",
                "Unix time the last known price slot ends.",
                snap.book.slots[-1].end.timestamp(),
            )

    def _counters(self, snap: Snapshot) -> Iterator[Metric]:
        counters = snap.counters
        yield _counter(
            "shelly_control_evaluations",
            "Evaluations of the rules: every quarter, after a change, and retries.",
            {(): counters.evaluations},
        )
        yield _counter(
            "shelly_control_switch_commands",
            "Switch commands the device accepted, by target state.",
            {(k,): v for k, v in counters.switch_commands.items()},
            ["target"],
        )
        yield _counter(
            "shelly_control_switch_failures",
            "Switch commands that failed or that the relay did not follow, by reason.",
            {(k,): v for k, v in counters.switch_failures.items()},
            ["reason"],
        )
        yield _counter(
            "shelly_control_relay_read_errors",
            "Failed reads of the relay's state, by reason.",
            {(k,): v for k, v in counters.device_read_errors.items()},
            ["reason"],
        )
        yield _counter(
            "shelly_control_price_read_errors",
            "Failed reads from the tibber-exporter, by reason.",
            {(k,): v for k, v in counters.price_read_errors.items()},
            ["reason"],
        )
        yield _counter(
            "shelly_control_shadow_disagreements",
            "Quarters in shadow mode where the relay still differed from the rules a minute "
            "after the quarter started.",
            {(): counters.shadow_disagreements},
        )
        yield _counter(
            "shelly_control_state_write_errors",
            "Failed writes to the state directory.",
            {(): counters.state_write_errors},
        )


def _counter(
    name: str, doc: str, values: dict[tuple[str, ...], int], labels: list[str] | None = None
) -> Metric:
    metric = CounterMetricFamily(name, doc, labels=labels or [])
    for key, value in values.items():
        metric.add_metric(list(key), value)
    return metric


def make_registry(controller: Controller, tz: ZoneInfo) -> CollectorRegistry:
    registry = CollectorRegistry()
    registry.register(ControlCollector(controller, tz))
    ProcessCollector(registry=registry)
    PlatformCollector(registry=registry)
    GCCollector(registry=registry)
    return registry
