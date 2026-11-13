"""The rules, as pure functions: no I/O, no clock, no globals.

`decide(settings, prices, now, current, override)` returns what the relay
should do this quarter, which rule said so and why, in words. `plan_days`
runs the same rules over every known slot, for the chart.

The rules come from the cron script this service replaces, in its order:

1. a negative price switches on;
2. in the morning window, a price under the morning line switches on;
3. a price under the switch-on line switches on;
4. a price over the switch-off line switches off;
5. anything else keeps the relay as it is, and nothing is sent.

Each line is a factor times the day's average price, worked out in decimal
and rounded to 3 decimals (0.1 ct per kWh, the precision the page shows). The
comparisons are strict, as in the script: a price exactly on a line keeps the
state.

The defaults are those of the live script (boiler2.sh): on below 1.05 and off
above 1.25 times the average, and the morning rule only in the 05:00 quarter,
with the 1.25 line. A replay of the relay log matched all five logged
switches at the exact quarter with these factors. An older published copy of
the script said 1.20 and 1.50; the log rules those out.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from .prices import PriceBook, Slot, is_complete, local_day

MODES = ("shadow", "live")
AVERAGE_WINDOWS = ("day", "day_and_next")
OVERRIDE_ACTIONS = ("on", "off")
FACTOR_MIN = 0.5
FACTOR_MAX = 3.0
OVERRIDE_MAX = dt.timedelta(hours=24)
# A vacation switches the boiler off completely, for up to this long.
VACATION_MAX = dt.timedelta(days=60)
# When a vacation ends, the boiler heats this long whatever the price, so the
# water reaches a safe temperature again. Nothing shortens it. Sized for the
# 80 litre Itho Daalderop Duo (manual 01-06120-001) with its boost knob on,
# which adds the second element for 2750 W: the manual gives 2:30 hours to heat
# 75 degrees, and 80 kg x 4.186 kJ/(kg K) x 48 K = 4.5 kWh to 60 degrees takes
# 1.6 hours. Without the boost the boiler heats at 1000 W and needs about 7
# hours; the knob is on the boiler, which this service cannot turn, so the page
# tells the owner to. Once the water is hot the thermostat stops the heating.
HEAT_UP = dt.timedelta(hours=2.5)
# What an override is for: the owner's own, a vacation, or the heat-up after one.
OVERRIDE_KINDS = ("manual", "vacation", "heat_up")

# Every value the decision's `rule` can take; a bounded metric label.
RULES = (
    "override",
    "no_prices",
    "negative",
    "no_lines",
    "morning",
    "below_on",
    "above_off",
    "between",
)
ACTIONS = ("on", "off", "keep")
_ACTION = {"negative": "on", "morning": "on", "below_on": "on", "above_off": "off"}


@dataclass(frozen=True)
class Settings:
    mode: str = "shadow"
    on_factor: float = 1.05
    off_factor: float = 1.25
    morning_enabled: bool = True
    morning_start: dt.time = dt.time(5, 0)
    morning_end: dt.time = dt.time(5, 15)
    morning_factor: float = 1.25
    negative_enabled: bool = True
    average_window: str = "day"

    def to_json(self) -> dict:
        return {
            "mode": self.mode,
            "on_factor": self.on_factor,
            "off_factor": self.off_factor,
            "morning_enabled": self.morning_enabled,
            "morning_start": self.morning_start.strftime("%H:%M"),
            "morning_end": self.morning_end.strftime("%H:%M"),
            "morning_factor": self.morning_factor,
            "negative_enabled": self.negative_enabled,
            "average_window": self.average_window,
        }


SETTING_NAMES = tuple(Settings().to_json())


# -- validation: one validator for the form, the JSON API, env and the file --


def _factor(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, str):
        # A Dutch keyboard types 1,05.
        value = float(value.strip().replace(",", "."))
    if isinstance(value, int) and abs(value) > 10:
        # Refused before float(): a JSON integer of a few hundred digits overflows it.
        raise ValueError
    if not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError
    value = float(value)
    # Two decimals, as the form shows them: a third one would be dropped
    # without notice the next time someone saves the form.
    if abs(round(value, 2) - value) > 1e-9:
        raise ValueError
    value = round(value, 2)
    if not FACTOR_MIN <= value <= FACTOR_MAX:
        raise ValueError
    return value


def _flag(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError
    return value


def _quarter_time(value: object) -> dt.time:
    if not isinstance(value, str) or len(value.strip()) != 5:
        raise ValueError
    parsed = dt.time.fromisoformat(value.strip())
    if parsed.minute % 15:
        raise ValueError
    return parsed


def _choice(options: tuple[str, ...]):
    def parse(value: object) -> str:
        if value not in options:
            raise ValueError
        return str(value)

    return parse


_FACTOR_ERROR = (
    f"Enter a number from {FACTOR_MIN:.2f} to {FACTOR_MAX:.2f} with at most two decimals, "
    "such as 1.05."
)
_FIELDS = {
    "mode": (_choice(MODES), "Pick shadow or live."),
    "on_factor": (_factor, _FACTOR_ERROR),
    "off_factor": (_factor, _FACTOR_ERROR),
    "morning_enabled": (_flag, "Must be true or false."),
    "morning_start": (_quarter_time, "Enter a time on the quarter hour, such as 05:00."),
    "morning_end": (_quarter_time, "Enter a time on the quarter hour, such as 06:00."),
    "morning_factor": (_factor, _FACTOR_ERROR),
    "negative_enabled": (_flag, "Must be true or false."),
    "average_window": (_choice(AVERAGE_WINDOWS), "Pick one of the two averages."),
}


def parse_settings(raw: Mapping[str, object], base: Settings) -> tuple[Settings, dict[str, str]]:
    """Apply the fields in `raw` to `base`. Returns (settings, errors by field).

    With any error, the settings returned are `base`, unchanged.
    """
    errors: dict[str, str] = {}
    values: dict[str, object] = {}
    for name, value in raw.items():
        if name not in _FIELDS:
            errors[str(name)[:40]] = "Unknown setting."
            continue
        parse, message = _FIELDS[name]
        try:
            values[name] = parse(value)
        except (TypeError, ValueError, OverflowError):
            errors[name] = message
    if errors:
        return base, errors
    result = replace(base, **values)
    if result.off_factor < result.on_factor:
        errors["off_factor"] = "The switch-off line must be at or above the switch-on line."
    if result.morning_end <= result.morning_start:
        errors["morning_end"] = "The morning window must end after it starts."
    return (base, errors) if errors else (result, {})


# -- override -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Override:
    """The owner's "on (or off) until then", ahead of every rule."""

    action: str
    until: dt.datetime
    set_at: dt.datetime
    by: str | None = None
    kind: str = "manual"

    @property
    def target(self) -> bool:
        return self.action == "on"

    @property
    def locked(self) -> bool:
        """A vacation or its heat-up: only ending the vacation (or waiting) changes it."""
        return self.kind != "manual"

    def active(self, now: dt.datetime) -> bool:
        return now < self.until

    def to_json(self) -> dict:
        data = {
            "action": self.action,
            "until": self.until.isoformat(),
            "set_at": self.set_at.isoformat(),
            "by": self.by,
        }
        # Only when it is not the owner's plain override, so old state reads back as before.
        if self.kind != "manual":
            data["kind"] = self.kind
        return data

    @classmethod
    def from_json(cls, data: object) -> Override:
        if not isinstance(data, dict) or data.get("action") not in OVERRIDE_ACTIONS:
            raise ValueError("not an override")
        until = dt.datetime.fromisoformat(str(data.get("until")))
        set_at = dt.datetime.fromisoformat(str(data.get("set_at")))
        if until.tzinfo is None or set_at.tzinfo is None:
            raise ValueError("override times need a UTC offset")
        by = data.get("by")
        kind = data.get("kind", "manual")
        if kind not in OVERRIDE_KINDS:
            raise ValueError("not an override")
        return cls(
            str(data["action"]),
            until.astimezone(dt.UTC),
            set_at.astimezone(dt.UTC),
            by[:120] if isinstance(by, str) else None,
            kind,
        )


def _next_wall_time(clock: dt.time, now: dt.datetime, tz: ZoneInfo) -> dt.datetime | None:
    """The first moment after `now` that the local clock shows `clock`, in UTC.

    Compared as instants, not as wall times: in the hour that repeats when
    summer time ends, 02:15 can still be ahead (the second time round) after
    the clock has passed it once.
    """
    today = now.astimezone(tz).date()
    found = []
    for day in (today, today + dt.timedelta(days=1)):
        for fold in (0, 1):
            moment = dt.datetime.combine(day, clock.replace(fold=fold), tzinfo=tz)
            back = moment.astimezone(dt.UTC).astimezone(tz)
            # A time the clock skips when summer time starts reads back as another time.
            if (back.date(), back.hour, back.minute) == (day, clock.hour, clock.minute):
                found.append(moment.astimezone(dt.UTC))
    ahead = sorted(m for m in found if m > now)
    return ahead[0] if ahead else None


def parse_until(
    value: object,
    now: dt.datetime,
    tz: ZoneInfo,
    longest: dt.timedelta = OVERRIDE_MAX,
    too_long: str = "An override can last at most 24 hours.",
) -> dt.datetime:
    """An override end: an ISO time with its offset, or "HH:MM" (the next time the clock shows it).

    The page sends the ISO form, so a choice made on a page left open too long
    is refused as passed instead of becoming the same time tomorrow.
    Raises ValueError with a message for the page.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Enter the time the override ends, such as 18:00.")
    text = value.strip()
    now = now.astimezone(dt.UTC)
    if len(text) == 5:
        try:
            clock = dt.time.fromisoformat(text)
        except ValueError:
            raise ValueError("Enter the time as hours and minutes, such as 18:00.") from None
        until = _next_wall_time(clock, now, tz)
        if until is None:
            raise ValueError("The clock does not show that time in the next day.")
    else:
        try:
            until = dt.datetime.fromisoformat(text)
        except ValueError:
            raise ValueError("Enter a time such as 18:00, or an ISO 8601 date and time.") from None
        try:
            if until.tzinfo is None:
                until = until.replace(tzinfo=tz)
            until = until.astimezone(dt.UTC)
        except (OverflowError, ValueError):
            raise ValueError("Enter a time within the next 24 hours.") from None
    if until <= now:
        raise ValueError("That time has already passed. Pick a later one.")
    if until - now > longest:
        raise ValueError(too_long)
    return until


def parse_vacation_end(value: object, now: dt.datetime, tz: ZoneInfo) -> dt.datetime:
    """The moment a vacation ends: a date and time ("2026-11-20T12:00", the page's field).

    A bare clock time is refused: a vacation spans days, so the date matters.
    Raises ValueError with a message for the page.
    """
    if not isinstance(value, str) or len(value.strip()) <= 5:
        raise ValueError("Pick the date and time the boiler should start heating again.")
    days = VACATION_MAX.days
    return parse_until(value, now, tz, VACATION_MAX, f"A vacation can last at most {days} days.")


# -- lines and decisions ------------------------------------------------------------------------


@dataclass(frozen=True)
class Lines:
    """The day's average and the lines derived from it, in price units per kWh."""

    average: float
    on_line: float
    off_line: float
    morning_line: float


_PRECISION = Decimal("0.001")


def _decimal(value: float) -> Decimal:
    """A float as the decimal it was written as: repr is the shortest text that reads back."""
    return Decimal(repr(value))


def _rounded(value: Decimal) -> Decimal:
    """Three decimals, 0.1 ct per kWh: the precision the page shows."""
    return value.quantize(_PRECISION, rounding=ROUND_HALF_UP)


def lines_for(settings: Settings, book: PriceBook, day: dt.date, tz: ZoneInfo) -> Lines | None:
    """The lines of one local day, or None if that day's prices are incomplete.

    Worked out in decimal, so a price the exporter writes as 0.21 is exactly on
    a line of 0.210, and keeps the state, as it did in the script. In binary
    floating point 1.05 x 0.2 is 0.21000000000000002, and 0.21 would be "under" it.
    Each line comes from the unrounded average, so it is at most 0.05 ct from
    the script's.
    """
    slots = book.day(day, tz)
    if not is_complete(slots, day, tz):
        return None
    pool = list(slots)
    if settings.average_window == "day_and_next":
        following = book.day(day + dt.timedelta(days=1), tz)
        if is_complete(following, day + dt.timedelta(days=1), tz):
            pool += following
    # Weighted by length, so a day of hourly fallback prices averages the same
    # way; for a day of equal slots this is the plain mean the script used.
    seconds = sum(s.seconds for s in pool)
    average = sum(_decimal(s.total) * s.seconds for s in pool) / seconds
    return Lines(
        average=float(_rounded(average)),
        on_line=float(_rounded(_decimal(settings.on_factor) * average)),
        off_line=float(_rounded(_decimal(settings.off_factor) * average)),
        morning_line=float(_rounded(_decimal(settings.morning_factor) * average)),
    )


def in_morning(settings: Settings, slot: Slot, tz: ZoneInfo) -> bool:
    """Whether the slot overlaps the morning window of its day.

    For a quarter on the quarter-hour grid that is the same as "starts inside
    the window". An hour of the exporter's hourly fallback counts when any
    part of it is inside, so a window such as 05:30 to 06:00 still applies.
    """
    if not settings.morning_enabled:
        return False
    day = local_day(slot.start, tz)
    start = dt.datetime.combine(day, settings.morning_start, tzinfo=tz)
    end = dt.datetime.combine(day, settings.morning_end, tzinfo=tz)
    return slot.start < end and slot.end > start


def rule_for(settings: Settings, price: float, lines: Lines, morning: bool) -> str:
    """Which rule decides a slot. The order is the cron script's."""
    if settings.negative_enabled and price < 0:
        return "negative"
    if lines.average <= 0:
        # A factor times an average at or below zero turns the lines upside down.
        return "no_lines"
    if morning and price < lines.morning_line:
        return "morning"
    if price < lines.on_line:
        return "below_on"
    if price > lines.off_line:
        return "above_off"
    return "between"


def ct(value: float) -> str:
    """A price per kWh in cents, as the page shows it: 13.0 ct, −0.4 ct."""
    text = f"{value * 100:.1f}".replace("-", "\N{MINUS SIGN}")
    return f"{text} ct"


def explain(rule: str, price: float, lines: Lines) -> str:
    if rule == "negative":
        return f"The price is below zero ({ct(price)}), and a negative price always heats."
    if rule == "no_lines":
        return (
            "The day's average is zero or below, so the lines mean nothing today: "
            "the relay keeps its state."
        )
    if rule == "morning":
        return f"Morning top-up: {ct(price)} is under the {ct(lines.morning_line)} morning line."
    if rule == "below_on":
        return f"{ct(price)} is under the {ct(lines.on_line)} switch-on line."
    if rule == "above_off":
        return f"{ct(price)} is over the {ct(lines.off_line)} switch-off line."
    return (
        f"{ct(price)} is between the lines ({ct(lines.on_line)} and {ct(lines.off_line)}), "
        "so the relay keeps its state."
    )


def hhmm(moment: dt.datetime) -> str:
    """HH:MM of a local time; in the hour that repeats when summer time ends, says which one."""
    text = f"{moment:%H:%M}"
    if moment.replace(fold=1 - moment.fold).utcoffset() != moment.utcoffset():
        text += " summer time" if moment.dst() else " winter time"
    return text


def until_text(until: dt.datetime, now: dt.datetime, tz: ZoneInfo) -> str:
    """When something ends, as the page says it: 18:00 today, Mon 14:15 on another day."""
    local = until.astimezone(tz)
    if local.date() == now.astimezone(tz).date():
        return hhmm(local)
    return f"{local:%a} {hhmm(local)}"


def vacation_until_text(until: dt.datetime, now: dt.datetime, tz: ZoneInfo) -> str:
    """When a vacation ends, with the date: a trip can span weeks, and "Tue" would not say which."""
    local = until.astimezone(tz)
    if local.date() == now.astimezone(tz).date():
        return hhmm(local)
    return f"{local:%a} {local.day} {local:%b} {hhmm(local)}"


def explain_override(override: Override, tz: ZoneInfo, now: dt.datetime) -> str:
    until = until_text(override.until, now, tz)
    if override.kind == "vacation":
        until = vacation_until_text(override.until, now, tz)
        return f"Vacation: the boiler is switched off completely until {until}."
    if override.kind == "heat_up":
        return f"Heating after the vacation until {until}, whatever the price."
    if override.action == "on":
        return f"Override: heat until {until}."
    return f"Override: keep off until {until}."


@dataclass(frozen=True)
class Decision:
    at: dt.datetime
    rule: str
    action: str
    # The relay state the rules want. For "keep" that is the current state,
    # and None while the current state is unknown.
    target: bool | None
    reason: str
    slot: Slot | None = None
    lines: Lines | None = None
    morning: bool = False
    override: Override | None = None


def decide(
    settings: Settings,
    book: PriceBook,
    now: dt.datetime,
    current: bool | None,
    override: Override | None,
    tz: ZoneInfo,
) -> Decision:
    """What the relay should do now.

    `current` is the state a "keep" keeps: the relay as read in live mode,
    the state this service would have set in shadow mode; None if unknown.
    """
    now = now.astimezone(dt.UTC)
    slot = book.slot_at(now)
    lines = lines_for(settings, book, local_day(slot.start, tz), tz) if slot else None
    morning = slot is not None and in_morning(settings, slot, tz)
    if override is not None and override.active(now):
        return Decision(
            now,
            "override",
            override.action,
            override.target,
            explain_override(override, tz, now),
            slot,
            lines,
            morning,
            override,
        )
    if slot is None:
        return Decision(
            now,
            "no_prices",
            "keep",
            current,
            "There is no price for this quarter, so nothing is switched.",
        )
    if lines is None:
        day = local_day(slot.start, tz)
        known = len(book.day(day, tz))
        return Decision(
            now,
            "no_prices",
            "keep",
            current,
            f"Only {known} of today's prices are known, so the day's average is not; "
            "nothing is switched.",
            slot,
        )
    rule = rule_for(settings, slot.total, lines, morning)
    action = _ACTION.get(rule, "keep")
    target = current if action == "keep" else action == "on"
    return Decision(
        now, rule, action, target, explain(rule, slot.total, lines), slot, lines, morning
    )


# -- the plan, for the chart --------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedSlot:
    slot: Slot
    rule: str
    # The relay state the rules give this slot; None where it is unknown.
    target: bool | None
    reason: str
    past: bool
    current: bool


@dataclass(frozen=True)
class DayPlan:
    day: dt.date
    slots: tuple[PlannedSlot, ...]
    lines: Lines | None

    @property
    def complete(self) -> bool:
        return self.lines is not None


def plan_days(
    settings: Settings,
    book: PriceBook,
    now: dt.datetime,
    current: bool | None,
    override: Override | None,
    tz: ZoneInfo,
    days: list[dt.date],
) -> list[DayPlan]:
    """Run the rules over every known slot and return the plans of `days`.

    Past slots show what the rules said, carried forward from the first known
    slot. From the current slot on, the plan starts from `current` and
    includes an active override. A slot of a day whose prices are incomplete
    has no rule and an unknown state (None), and so has every "keep" after it
    until a rule decides again.
    """
    now = now.astimezone(dt.UTC)
    lines_cache: dict[dt.date, Lines | None] = {}
    planned: list[PlannedSlot] = []
    state: bool | None = None
    started = False
    for slot in book.slots:
        day = local_day(slot.start, tz)
        if day not in lines_cache:
            lines_cache[day] = lines_for(settings, book, day, tz)
        lines = lines_cache[day]
        past = slot.end <= now
        is_current = slot.start <= now < slot.end
        if not past and not started:
            # The first slot that is not over: from here on the plan starts
            # from the real state (or, in shadow mode, the virtual one).
            state, started = current, True
        if not past and override is not None and slot.start < override.until:
            rule, target, reason = (
                "override",
                override.target,
                explain_override(override, tz, now),
            )
        elif lines is None:
            rule, target = "no_prices", None
            reason = "Not all of this day's prices are known, so there is no plan."
        else:
            rule = rule_for(settings, slot.total, lines, in_morning(settings, slot, tz))
            action = _ACTION.get(rule, "keep")
            target = state if action == "keep" else action == "on"
            reason = explain(rule, slot.total, lines)
        state = target
        planned.append(PlannedSlot(slot, rule, target, reason, past, is_current))
    result = []
    for day in days:
        slots = tuple(p for p in planned if local_day(p.slot.start, tz) == day)
        result.append(DayPlan(day, slots, lines_cache.get(day)))
    return result


@dataclass(frozen=True)
class ClosestCall:
    """The slot ahead that came nearest to switching the relay, when nothing switches it."""

    slot: Slot
    # The line it stayed on the wrong side of, and its name as the page says it.
    line: float
    line_name: str
    # How far the price stayed from that line, in euros per kWh; 0 means on the line.
    margin: float


def closest_call(
    plans: list[DayPlan], settings: Settings, current: bool | None, tz: ZoneInfo
) -> ClosestCall | None:
    """Why no switch is planned: the known slot ahead that came nearest to one.

    With the relay on, that is the price nearest under the switch-off line; with
    it off, the price nearest over the line that would switch it on (the
    switch-on line, the morning line in the morning window, or zero). Only
    slots the lines decide count: an override, a day without all its prices or
    a day with an average at or below zero says nothing about the lines.
    """
    if current is None:
        return None
    best: ClosestCall | None = None
    for plan in plans:
        lines = plan.lines
        if lines is None:
            continue
        for planned in plan.slots:
            if planned.past or planned.rule in ("override", "no_prices", "no_lines"):
                continue
            price = planned.slot.total
            if current:
                candidates = [(lines.off_line, "switch-off", lines.off_line - price)]
            else:
                candidates = [(lines.on_line, "switch-on", price - lines.on_line)]
                if settings.morning_enabled and in_morning(settings, planned.slot, tz):
                    candidates.append((lines.morning_line, "morning", price - lines.morning_line))
                if settings.negative_enabled:
                    candidates.append((0.0, "zero", price))
            for line, name, margin in candidates:
                # A price over the line would switch: then a change is planned, not a call.
                if margin < 0:
                    continue
                if best is None or margin < best.margin:
                    best = ClosestCall(planned.slot, line, name, margin)
    return best


def next_changes(
    plans: list[DayPlan], now: dt.datetime, current: bool | None, limit: int = 2
) -> list[tuple[dt.datetime, bool]]:
    """The next times the planned relay state changes, after now."""
    changes: list[tuple[dt.datetime, bool]] = []
    state = current
    for plan in plans:
        for planned in plan.slots:
            if planned.past or planned.current:
                continue
            if planned.target is None:
                # Unknown: the next known state is no change from anything.
                state = None
                continue
            if state is not None and planned.target != state:
                changes.append((planned.slot.start, planned.target))
                if len(changes) >= limit:
                    return changes
            state = planned.target
    return changes
