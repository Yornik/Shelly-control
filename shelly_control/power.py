"""What the boiler used, minute by minute, and what that cost.

Once a minute the controller reads the device's energy counter (Wh, the one
that only counts up) and keeps it with the price in force. The difference
between two reads is the exact energy used between them, so the average power
of that stretch and its cost follow without a power meter's sampling error: a
thermostat that switches in the middle of a minute is not missed.

The record lives in `power.jsonl` in the state directory, one JSON line per
read, `[unix seconds, Wh, price per kWh or null]`. It is appended to, and
rewritten without the old lines now and then. It is for the graph and the cost
line only: a missing, damaged or unwritable file never stops the controller,
the graph just has gaps.

A gap in the reads (the service was down, the device did not answer) is a gap in
the record, never an average spread over it. A counter that went down (the device
restarted and lost it) loses the one stretch it spans.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .store import write_atomic

POWER_FILE = "power.jsonl"
# One read of the counter per this many seconds.
SAMPLE_SECONDS = 60.0
# Reads closer together than this are one read.
MIN_SPACING = 20.0
# Two reads further apart than this leave a gap, not an average.
MAX_GAP = 180.0
# How long the record is kept: a calendar week and a day to look back.
KEEP_SECONDS = 8 * 86400
# After this many appended lines, the file is rewritten without the old ones.
COMPACT_AFTER = 2880


@dataclass(frozen=True)
class Sample:
    """One read of the energy counter."""

    ts: float
    wh: float
    # The price per kWh in force at the read, or None without a usable price.
    price: float | None = None


@dataclass(frozen=True)
class Interval:
    """The stretch between two consecutive reads, with the energy used in it."""

    t0: float
    t1: float
    wh: float
    price: float | None

    @property
    def watts(self) -> float:
        return self.wh * 3600 / (self.t1 - self.t0)

    @property
    def kwh(self) -> float:
        return self.wh / 1000

    @property
    def euros(self) -> float | None:
        return None if self.price is None else self.kwh * self.price


def intervals(samples: Iterable[Sample]) -> list[Interval]:
    """The stretches between consecutive reads that make a usable average."""
    found: list[Interval] = []
    previous: Sample | None = None
    for sample in samples:
        if previous is not None:
            span = sample.ts - previous.ts
            used = sample.wh - previous.wh
            # A longer gap, or a counter that went back, is not averaged over.
            if 0 < span <= MAX_GAP and used >= 0:
                found.append(Interval(previous.ts, sample.ts, used, previous.price))
        previous = sample
    return found


@dataclass(frozen=True)
class Totals:
    """What a period used and cost, from the reads inside it."""

    kwh: float
    # The cost of the energy that had a price, and how much that was.
    euros: float
    priced_kwh: float
    # Seconds the reads cover, and when the first one in the period was.
    covered: float
    first: float | None

    @property
    def average_price(self) -> float | None:
        """Euros per kWh paid on average, over the energy that had a price."""
        return self.euros / self.priced_kwh if self.priced_kwh > 0 else None


def totals(stretches: Iterable[Interval], start: float, end: float) -> Totals:
    """The sum over the stretches that begin in [start, end)."""
    kwh = euros = priced = covered = 0.0
    first = None
    for stretch in stretches:
        if not start <= stretch.t0 < end:
            continue
        kwh += stretch.kwh
        covered += stretch.t1 - stretch.t0
        first = stretch.t0 if first is None else min(first, stretch.t0)
        if stretch.euros is not None:
            euros += stretch.euros
            priced += stretch.kwh
    return Totals(kwh, euros, priced, covered, first)


def _parse(line: str) -> Sample | None:
    try:
        data = json.loads(line)
        ts, wh = float(data[0]), float(data[1])
        raw = data[2] if len(data) > 2 else None
        price = float(raw) if isinstance(raw, int | float) and not isinstance(raw, bool) else None
    except (ValueError, TypeError, IndexError, KeyError):
        return None
    if ts != ts or wh != wh or wh < 0:  # NaN
        return None
    return Sample(ts, wh, price)


class PowerLog:
    """The record in memory and on disk. Safe to read from the web threads."""

    def __init__(self, path: str | Path, clock=time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.Lock()
        self._samples: deque[Sample] = deque()
        self._appended = 0
        # The last write problem, for the log; None while writing works.
        self.error: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        cutoff = self.clock() - KEEP_SECONDS
        clean = True
        last = None
        for line in lines:
            sample = _parse(line) if line.strip() else None
            # A damaged, old or out-of-order line is dropped, and the file rewritten.
            if sample is None or sample.ts < cutoff or (last is not None and sample.ts <= last):
                clean = False
                continue
            self._samples.append(sample)
            last = sample.ts
        if not clean:
            self._rewrite()

    @staticmethod
    def _line(sample: Sample) -> str:
        price = None if sample.price is None else round(sample.price, 5)
        return json.dumps([round(sample.ts, 1), round(sample.wh, 3), price]) + "\n"

    def _rewrite(self) -> None:
        data = "".join(self._line(s) for s in self._samples).encode()
        try:
            write_atomic(self.path, data)
            self.error = None
        except OSError as exc:
            self.error = str(exc)
        self._appended = 0

    def add(self, ts: float, wh: float | None, price: float | None = None) -> bool:
        """Record a read. False when it is not used: no counter, or too soon, or not later."""
        if wh is None or wh < 0 or wh != wh:
            return False
        with self._lock:
            last = self._samples[-1] if self._samples else None
            if last is not None and ts - last.ts < MIN_SPACING:
                return False
            sample = Sample(ts, wh, price)
            self._samples.append(sample)
            cutoff = ts - KEEP_SECONDS
            while self._samples and self._samples[0].ts < cutoff:
                self._samples.popleft()
            if self._appended >= COMPACT_AFTER:
                self._rewrite()
                return True
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(self._line(sample))
                self._appended += 1
                self.error = None
            except OSError as exc:
                self.error = str(exc)
        return True

    def samples(self, since: float | None = None) -> tuple[Sample, ...]:
        """The reads from `since` (Unix seconds) on, oldest first."""
        with self._lock:
            if since is None:
                return tuple(self._samples)
            return tuple(s for s in self._samples if s.ts >= since)

    def clear(self) -> None:
        """Forget everything (for tests and a deliberate reset)."""
        with self._lock:
            self._samples.clear()
            with contextlib.suppress(OSError):
                self.path.unlink()
