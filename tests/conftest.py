from __future__ import annotations

import datetime as dt
import json
import socket
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fake_exporter import HOME_ID, FakeExporter
from fake_shelly import FakeShelly

from shelly_control.controller import Controller
from shelly_control.prices import PriceBook, PriceClient, parse_prices
from shelly_control.rules import Settings
from shelly_control.shelly import ShellyClient
from shelly_control.store import Store

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TZ = ZoneInfo("Europe/Amsterdam")
PASSWORD = "correct horse battery staple"  # noqa: S105 - a test password, not a secret

# 2026: summer time ends on Sunday 25 October (a 25-hour day, 100 quarters) and
# starts on Sunday 29 March (a 23-hour day, 92 quarters).
DST_END = dt.date(2026, 10, 25)
DST_START = dt.date(2026, 3, 29)
NORMAL_DAY = dt.date(2026, 10, 27)


def at(day: dt.date, hour: int, minute: int = 0, second: int = 0, fold: int = 0) -> dt.datetime:
    """A local wall-clock time in Amsterdam, as an aware datetime."""
    return dt.datetime.combine(day, dt.time(hour, minute, second, fold=fold), tzinfo=TZ)


def ts(day: dt.date, hour: int, minute: int = 0, second: int = 0, fold: int = 0) -> float:
    return at(day, hour, minute, second, fold).timestamp()


def make_day(
    day: dt.date,
    price: Callable[[int, dt.datetime], float] | None = None,
    step: int = 900,
) -> list[dict]:
    """One local day of exporter entries, shaped like the real ones, with made-up prices.

    The default curve is cheap at night, dearer in the morning and very dear
    from 17:00 to 20:00.
    """
    start = at(day, 0).astimezone(dt.UTC)
    end = at(day + dt.timedelta(days=1), 0).astimezone(dt.UTC)
    entries, moment, i = [], start, 0
    while moment < end:
        local = moment.astimezone(TZ)
        if price is None:
            total = 0.30 if 17 <= local.hour < 20 else 0.22 if 7 <= local.hour < 10 else 0.18
        else:
            total = price(i, local)
        entries.append(
            {
                "startsAt": local.isoformat(),
                "total": round(total, 4),
                "energy": round(total - 0.137, 4),
                "tax": 0.137,
                "level": 3,
            }
        )
        moment += dt.timedelta(seconds=step)
        i += 1
    return entries


def book_of(*days: list[dict], fetched_at: float = 0.0) -> PriceBook:
    return PriceBook(parse_prices([e for day in days for e in day]), fetched_at)


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def set(self, moment: dt.datetime | float) -> None:
        self.now = moment.timestamp() if isinstance(moment, dt.datetime) else moment


@pytest.fixture
def fake_shelly():
    fake = FakeShelly()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def fake_exporter():
    fake = FakeExporter(make_day(NORMAL_DAY) + make_day(NORMAL_DAY + dt.timedelta(days=1)))
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def closed_port_url():
    """A local URL where nothing listens, so connecting is refused at once."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


@pytest.fixture
def clock():
    return Clock(ts(NORMAL_DAY, 12, 0, 5))


@pytest.fixture
def make_controller(fake_shelly, fake_exporter, tmp_path, clock):
    """Build a Controller wired to the fakes, a temporary state directory and the test clock."""

    def build(*, settings: Settings | None = None, allow_switching: bool = True, **kwargs):
        defaults = settings or Settings()
        # The test clock only moves forward, so it serves as the monotonic clock
        # too, unless a test passes its own to step the wall clock back.
        kwargs.setdefault("monotonic", clock)
        return Controller(
            shelly=ShellyClient(fake_shelly.url, fake_shelly.password, timeout=2),
            prices=PriceClient(fake_exporter.url, HOME_ID, timeout=2),
            store=kwargs.pop("store", None) or Store(tmp_path / "state", clock),
            tz=TZ,
            defaults=defaults,
            allow_switching=allow_switching,
            clock=clock,
            **kwargs,
        )

    return build
