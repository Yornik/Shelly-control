"""Quarter-hour prices from a tibber-exporter in the same cluster. GET only.

The exporter (github.com/Yornik/tibber-exporter) serves the prices it already
fetched from Tibber at `GET /homes/<home id>/prices`: a JSON array of
`{startsAt, total, energy, tax, level}`, today's slots followed by tomorrow's
once Tibber has published them. This service never talks to Tibber itself and
holds no Tibber token.

What the exporter does that a client has to handle:

- It answers an unknown path, a wrong home id included, with HTTP 200 and the
  text body "Tibber exporter". So a 200 alone proves nothing.
- Before its first price query it encodes the empty list as JSON `null`.
- When Tibber's 15-minute query fails it falls back to hourly prices until a
  later query works, so the spacing of `startsAt` says which one a reply is.
- Its lists are read without a lock, so in theory a reply can hold a slot
  twice. Slots are de-duplicated by their start time and sorted.

A slot is found by its `startsAt` time, never by its position in the list:
days with a summer-time change have 92 or 100 quarters, not 96.
"""

from __future__ import annotations

import bisect
import datetime as dt
import json
import math
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter

from .transport import (
    READ_CHUNK,
    BodyTimeout,
    BodyTooLarge,
    normalize_base_url,
    origin_of,
    read_bounded,
)

UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

# The only request that may leave for the exporter, checked again at send time.
_WIRE_PATH = re.compile(r"/homes/[0-9a-fA-F-]{36}/prices")

# Two days of quarters are about 20 KiB. A bigger reply is not a price list.
MAX_REPLY_BYTES = 1024 * 1024

QUARTER = 900
HOUR = 3600
RESOLUTIONS = (QUARTER, HOUR)

# The exporter's catch-all route: what a wrong home id gets, with HTTP 200.
_CATCH_ALL = b"Tibber exporter"


class PriceError(Exception):
    """A price read failed. The controller keeps the prices it has."""

    # Bounded label value for shelly_control_price_read_errors_total.
    reason = "invalid_response"


class PriceTimeout(PriceError):
    reason = "timeout"


class PriceUnreachable(PriceError):
    reason = "connection"


class PriceHTTPError(PriceError):
    reason = "http"


class PriceNotServed(PriceError):
    """HTTP 200 with the exporter's catch-all text: wrong home id, or no subscription."""

    reason = "not_served"


PRICE_ERROR_REASONS = ("timeout", "connection", "http", "not_served", "invalid_response")


class ExporterViolation(RuntimeError):
    """Code tried to send something other than GET /homes/<id>/prices to the exporter.

    Not a PriceError on purpose: those are retried, this is a bug.
    """


@dataclass(frozen=True)
class Slot:
    """One price slot. `start` and `end` are timezone-aware and in UTC."""

    start: dt.datetime
    end: dt.datetime
    total: float

    @property
    def seconds(self) -> int:
        return int((self.end - self.start).total_seconds())


def _resolution(starts: list[dt.datetime]) -> int:
    gaps = {int((b - a).total_seconds()) for a, b in zip(starts, starts[1:], strict=False)}
    if not gaps:
        # One slot alone says nothing about the grid. Assume the shorter one:
        # a slot that ends too early means "no price" later, never a wrong one.
        return QUARTER
    step = min(gaps)
    if step not in RESOLUTIONS or any(gap % step for gap in gaps):
        raise PriceError(f"the slots are {step} s apart; expected 15 or 60 minutes")
    return step


def parse_prices(data: object) -> tuple[Slot, ...]:
    """Turn the exporter's JSON into sorted, de-duplicated slots. Raises PriceError."""
    if data is None:
        return ()
    if not isinstance(data, list):
        raise PriceError("the price list is not a JSON array")
    found: dict[dt.datetime, float] = {}
    for item in data:
        if not isinstance(item, dict):
            raise PriceError("a price entry is not a JSON object")
        starts_at = item.get("startsAt")
        if not isinstance(starts_at, str):
            raise PriceError("a price entry has no startsAt")
        try:
            start = dt.datetime.fromisoformat(starts_at)
        except ValueError:
            raise PriceError("a price entry has an unreadable startsAt") from None
        if start.tzinfo is None:
            raise PriceError("a price entry's startsAt has no UTC offset")
        total = item.get("total")
        if total is None:
            continue
        if isinstance(total, bool) or not isinstance(total, int | float):
            raise PriceError("a price entry's total is not a number")
        if not math.isfinite(total):
            raise PriceError("a price entry's total is not a finite number")
        found.setdefault(start.astimezone(dt.UTC), float(total))
    starts = sorted(found)
    step = dt.timedelta(seconds=_resolution(starts))
    return tuple(Slot(start, start + step, found[start]) for start in starts)


def day_bounds(day: dt.date, tz: ZoneInfo) -> tuple[dt.datetime, dt.datetime]:
    """Local midnight to the next local midnight, in UTC: 23, 24 or 25 hours."""
    start = dt.datetime.combine(day, dt.time(0), tzinfo=tz)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), tzinfo=tz)
    return start.astimezone(dt.UTC), end.astimezone(dt.UTC)


def local_day(moment: dt.datetime, tz: ZoneInfo) -> dt.date:
    return moment.astimezone(tz).date()


def is_complete(slots: tuple[Slot, ...], day: dt.date, tz: ZoneInfo) -> bool:
    """Whether `slots` cover `day` from local midnight to local midnight without a gap."""
    if not slots:
        return False
    start, end = day_bounds(day, tz)
    if slots[0].start != start or slots[-1].end != end:
        return False
    return all(a.end == b.start for a, b in zip(slots, slots[1:], strict=False))


@dataclass(frozen=True)
class PriceBook:
    """The prices the controller decides with, and when they were read."""

    slots: tuple[Slot, ...] = ()
    # Unix time of the successful read the newest of these slots came from.
    fetched_at: float | None = None

    @cached_property
    def _starts(self) -> list[dt.datetime]:
        return [s.start for s in self.slots]

    def slot_at(self, moment: dt.datetime) -> Slot | None:
        """The slot whose start is at or before `moment` and whose end is after it."""
        i = bisect.bisect_right(self._starts, moment) - 1
        if i >= 0 and moment < self.slots[i].end:
            return self.slots[i]
        return None

    def day(self, day: dt.date, tz: ZoneInfo) -> tuple[Slot, ...]:
        return tuple(s for s in self.slots if local_day(s.start, tz) == day)

    def days(self, tz: ZoneInfo) -> list[dt.date]:
        return sorted({local_day(s.start, tz) for s in self.slots})

    def complete(self, day: dt.date, tz: ZoneInfo) -> bool:
        return is_complete(self.day(day, tz), day, tz)

    # -- the cache file --------------------------------------------------------------------------

    def to_json(self) -> dict:
        return {
            "fetched_at": self.fetched_at,
            "slots": [
                {"start": s.start.isoformat(), "end": s.end.isoformat(), "total": s.total}
                for s in self.slots
            ],
        }

    @classmethod
    def from_json(cls, data: object) -> PriceBook:
        """Read the cache file's content back. Raises ValueError if it is not one."""
        if not isinstance(data, dict) or not isinstance(data.get("slots"), list):
            raise ValueError("not a price cache")
        fetched_at = data.get("fetched_at")
        if fetched_at is not None and (
            isinstance(fetched_at, bool) or not isinstance(fetched_at, int | float)
        ):
            raise ValueError("fetched_at is not a number")
        slots = []
        for item in data["slots"]:
            if not isinstance(item, dict):
                raise ValueError("a cached slot is not an object")
            start = dt.datetime.fromisoformat(str(item.get("start"))).astimezone(dt.UTC)
            end = dt.datetime.fromisoformat(str(item.get("end"))).astimezone(dt.UTC)
            total = item.get("total")
            if isinstance(total, bool) or not isinstance(total, int | float):
                raise ValueError("a cached total is not a number")
            if int((end - start).total_seconds()) not in RESOLUTIONS or not math.isfinite(total):
                raise ValueError("a cached slot is not 15 or 60 minutes long")
            slots.append(Slot(start, end, float(total)))
        slots.sort(key=lambda s: s.start)
        if any(a.end > b.start for a, b in zip(slots, slots[1:], strict=False)):
            raise ValueError("cached slots overlap")
        return cls(tuple(slots), None if fetched_at is None else float(fetched_at))


def merge(
    old: PriceBook | None,
    new: tuple[Slot, ...],
    fetched_at: float,
    tz: ZoneInfo,
    keep_from: dt.date,
) -> PriceBook:
    """Combine a fresh read with the prices already known, one local day at a time.

    A published price never changes, so for each day the better version wins:
    a complete day over a partial one, quarter-hours over the exporter's
    hourly fallback, and the fresh read over the old one when they are equal.
    Days before `keep_from` are dropped.
    """
    fresh = PriceBook(new, fetched_at)
    chosen: dict[dt.date, tuple[tuple[bool, bool, bool], tuple[Slot, ...]]] = {}
    for book, is_fresh in ((fresh, True), (old, False)):
        if book is None:
            continue
        for day in book.days(tz):
            if day < keep_from:
                continue
            slots = book.day(day, tz)
            fine = all(s.seconds == QUARTER for s in slots)
            score = (is_complete(slots, day, tz), fine, is_fresh)
            if day not in chosen or score > chosen[day][0]:
                chosen[day] = (score, slots)
    merged = [s for day in sorted(chosen) for s in chosen[day][1]]
    return PriceBook(tuple(merged), fetched_at)


def _check_wire(method: str | None, url: str | None, origin: tuple[str, str]) -> None:
    """Raise ExporterViolation unless this is GET /homes/<id>/prices on the exporter."""
    parts = urlsplit(url or "")
    if (
        method != "GET"
        or (parts.scheme, parts.netloc.lower()) != origin
        or not _WIRE_PATH.fullmatch(parts.path)
        or parts.query
        or parts.fragment
    ):
        raise ExporterViolation(f"refusing to send {method} to the exporter: not the price read")


class _ExporterAdapter(HTTPAdapter):
    """Checks every request as it is sent, after requests has built the final URL."""

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self._origin = origin_of(base_url)

    def send(self, request, *args, **kwargs):
        _check_wire(request.method, request.url, self._origin)
        return super().send(request, *args, **kwargs)


class PriceClient:
    def __init__(self, base_url: str, home_id: str, *, timeout: float = 5.0) -> None:
        self.base_url = normalize_base_url(base_url)
        if not UUID.fullmatch(home_id):
            raise ValueError("the home id must be a UUID")
        self._url = f"{self.base_url}/homes/{home_id.lower()}/prices"
        self.timeout = timeout
        self.session = requests.Session()
        adapter = _ExporterAdapter(self.base_url)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        # In-cluster service: no proxy from the environment, no ~/.netrc.
        self.session.trust_env = False

    def fetch(self) -> tuple[Slot, ...]:
        """Today's and tomorrow's slots, as far as the exporter knows them."""
        return parse_prices(self._get())

    def _get(self) -> object:
        # One timeout to connect and one to read, at most, for the whole reply.
        deadline = time.monotonic() + 2 * self.timeout
        try:
            resp = self.session.get(
                self._url,
                timeout=self.timeout,
                # A redirect could point anywhere, so none is followed.
                allow_redirects=False,
                stream=True,
                headers={"Accept": "application/json"},
            )
        except requests.Timeout:
            raise PriceTimeout(f"the exporter did not answer within {self.timeout:g} s") from None
        except requests.RequestException as exc:
            raise PriceUnreachable(
                f"cannot reach the exporter ({exc.__class__.__name__})"
            ) from None
        with resp:
            if resp.status_code != 200:
                raise PriceHTTPError(f"the exporter answered HTTP {resp.status_code}")
            try:
                body = read_bounded(_chunks(resp), MAX_REPLY_BYTES, deadline)
            except BodyTooLarge:
                raise PriceError(f"the reply is larger than {MAX_REPLY_BYTES} bytes") from None
            except BodyTimeout:
                raise PriceTimeout("the exporter's reply took too long") from None
            except requests.RequestException as exc:
                raise PriceUnreachable(
                    f"the exporter's reply broke off ({exc.__class__.__name__})"
                ) from None
            content_type = resp.headers.get("Content-Type", "")
        if body.startswith(_CATCH_ALL):
            raise PriceNotServed(
                "the exporter does not serve this home: check TIBBER_HOME_ID, and that the "
                "home has an active Tibber subscription"
            )
        if not content_type.startswith("application/json"):
            raise PriceError("the exporter's reply is not JSON")
        try:
            return json.loads(body)
        except (ValueError, RecursionError):
            # RecursionError: nested deeper than the parser goes, which no price list is.
            raise PriceError("the exporter's reply is not valid JSON") from None


def _chunks(resp: requests.Response) -> Iterable[bytes]:
    return resp.iter_content(READ_CHUNK)
