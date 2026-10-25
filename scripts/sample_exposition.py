#!/usr/bin/env python3
"""Print what /metrics serves after a few decisions, with every series present.

CI pipes this into `promtool check metrics`, which checks every name, type,
unit suffix and help text against Prometheus' conventions. Nothing is
contacted: the device and the exporter are stand-ins in this file.
"""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from prometheus_client import generate_latest  # noqa: E402

from shelly_control.controller import Controller  # noqa: E402
from shelly_control.metrics import make_registry  # noqa: E402
from shelly_control.prices import Slot  # noqa: E402
from shelly_control.rules import Settings  # noqa: E402
from shelly_control.shelly import SwitchStatus  # noqa: E402
from shelly_control.store import Store  # noqa: E402

TZ = ZoneInfo("Europe/Amsterdam")
DAY = dt.date(2026, 10, 27)
NOON = dt.datetime.combine(DAY, dt.time(12, 0, 5), tzinfo=TZ).timestamp()


class StandInRelay:
    def __init__(self) -> None:
        self.output = False

    def get_switch_status(self, switch_id: int) -> SwitchStatus:
        return SwitchStatus(self.output, "http", 1050.0 if self.output else 0.0, (), False)

    def get_device_info(self):
        from shelly_control.shelly import DeviceInfo

        return DeviceInfo("shellyplus1pm-0123456789ab", "SNSW-001P16EU", "2", "1.7.5", True)

    def set_output(self, switch_id: int, on: bool) -> bool:
        was, self.output = self.output, on
        return was


class StandInExporter:
    def fetch(self) -> tuple[Slot, ...]:
        start = dt.datetime.combine(DAY, dt.time(0), tzinfo=TZ).astimezone(dt.UTC)
        step = dt.timedelta(minutes=15)
        return tuple(
            Slot(start + i * step, start + (i + 1) * step, 0.30 if 68 <= i < 80 else 0.18)
            for i in range(2 * 96)
        )


def exposition() -> str:
    with tempfile.TemporaryDirectory() as state:
        now = [NOON]
        controller = Controller(
            shelly=StandInRelay(),
            prices=StandInExporter(),
            store=Store(state, lambda: now[0]),
            tz=TZ,
            defaults=Settings(mode="live"),
            allow_switching=True,
            clock=lambda: now[0],
        )
        controller.tick()
        until = dt.datetime.fromtimestamp(NOON + 3600, dt.UTC)
        controller.set_override("off", until, None)
        now[0] += 120
        controller.tick()
        return generate_latest(make_registry(controller, TZ)).decode()


def main() -> None:
    sys.stdout.write(exposition())


if __name__ == "__main__":
    main()
