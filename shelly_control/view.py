"""What the page says, worked out from one controller snapshot.

Plain strings and numbers only: the templates escape everything, and nothing
here produces markup.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .chart import ChartModel, build_chart, y_scale
from .controller import Snapshot, prices_usable
from .rules import DayPlan, Lines, ct, hhmm, lines_for, next_changes, plan_days, until_text

# Below this the relay is on but the boiler draws nothing: its thermostat is satisfied.
IDLE_WATTS = 20.0
LOG_ROWS = 48

AVERAGE_LABELS = {
    "day": "Today's quarters, midnight to midnight",
    "day_and_next": "Today's and tomorrow's quarters, once tomorrow's prices are known",
}


@dataclass(frozen=True)
class Banner:
    level: str  # "error", "warning" or "info"
    text: str


@dataclass(frozen=True)
class Hero:
    word: str
    cls: str
    why: str
    detail: str | None
    plan: str | None
    # Shadow mode, said on the hero itself, not only in the small header pill.
    watching: str | None = None


@dataclass(frozen=True)
class LogRow:
    time: str
    label: str
    cls: str
    text: str
    who: str | None


@dataclass(frozen=True)
class StatusRow:
    name: str
    value: str


@dataclass(frozen=True)
class DayView:
    chart: ChartModel | None
    title: str
    empty: str | None


@dataclass(frozen=True)
class Key:
    """Which entries the chart key needs: only what the charts draw."""

    zero: bool
    morning: bool
    unknown: bool
    nodata: bool
    lines: bool = True
    planned: bool = True


@dataclass(frozen=True)
class PageView:
    name: str
    mode: str
    live: bool
    switching_allowed: bool
    # The header pill: "Live", "Live, switching off" or "Shadow mode".
    mode_label: str
    mode_cls: str
    hero: Hero
    banners: tuple[Banner, ...]
    days: tuple[DayView, ...]
    key: Key | None
    lines_today: Lines | None
    override_text: str | None
    override_after: str
    override_options: tuple[tuple[str, str], ...]
    override_default: str
    quarter_times: tuple[str, ...]
    log: tuple[LogRow, ...]
    status: tuple[StatusRow, ...]
    average_labels: dict[str, str]


def _local(moment: float | dt.datetime, tz: ZoneInfo) -> dt.datetime:
    if isinstance(moment, dt.datetime):
        return moment.astimezone(tz)
    return dt.datetime.fromtimestamp(moment, tz)


def _when(moment: dt.datetime, today: dt.date) -> str:
    """12:45 today, Sat 21:30 for any other day."""
    if moment.date() == today:
        return f"{moment:%H:%M}"
    return f"{moment:%a %H:%M}"


def _title(day: dt.date, today: dt.date) -> str:
    name = "Today" if day == today else "Tomorrow" if day > today else "Yesterday"
    return f"{name}, {day:%A} {day.day} {day:%B}"


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s ago"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min ago"


AFTER_OVERRIDE = {
    "live": "Then the rules decide again.",
    "shadow": (
        "Then the relay stays as the override left it: shadow mode does not switch it back."
    ),
}


def _shadow_sentence(action: str, relay_on: bool | None, reason: str) -> str:
    """What the rules want, against what the relay is, without claiming anything is sent."""
    if action == "keep":
        return f"The rules would leave the relay as it is: {reason}"
    want = action == "on"
    word = "on" if want else "off"
    if relay_on is None:
        return f"The rules would switch it {word}: {reason}"
    if relay_on == want:
        return f"The rules want it {word}, and it already is: {reason}"
    return f"The rules would switch it {word}: {reason}"


def _hero(snap: Snapshot, tz: ZoneInfo, plans: list[DayPlan], now: dt.datetime) -> Hero:
    # A state read before the device stopped answering is not shown as the state now.
    relay = None if snap.relay_error else snap.relay
    if relay is None:
        word, cls = "Unknown", "is-unknown"
    elif relay.output and (relay.apower is None or relay.apower >= IDLE_WATTS):
        word, cls = "Heating", "is-on"
    elif relay.output:
        word, cls = "On", "is-on"
    else:
        word, cls = "Off", "is-off"

    decision = snap.decision
    shadow = snap.settings.mode != "live"
    detail = None
    if relay is not None and relay.output and relay.apower is not None:
        if relay.apower < IDLE_WATTS:
            detail = "The relay is on, but the boiler draws no power: it is up to temperature."
    if snap.relay_error:
        # The banner gives the technical reason, once.
        detail = "The Shelly does not answer, so the relay's state is unknown and nothing is sent."
        if snap.relay is not None and snap.relay_read_at is not None:
            read = _when(_local(snap.relay_read_at, tz), now.astimezone(tz).date())
            detail += f" At the last read, {read}, it was {'on' if snap.relay.output else 'off'}."
    elif relay is None:
        detail = "The relay has not been read yet."

    if decision is None:
        why = "Waiting for the first decision."
    elif decision.rule == "override":
        until = until_text(decision.override.until, now, tz)
        what = "heat" if decision.override.action == "on" else "stay off"
        why = (
            f"Set by hand to {what} until {until}. {AFTER_OVERRIDE['shadow' if shadow else 'live']}"
        )
    elif decision.rule == "no_prices":
        # The banner above says why there is no price.
        why = decision.reason
    elif not shadow:
        why = _live_sentence(decision.rule, decision.slot.total, decision.lines, decision.reason)
    else:
        why = _shadow_sentence(decision.action, relay.output if relay else None, decision.reason)
    entry = snap.last_entry
    held = entry and entry.get("outcome") in ("held_back", "failed") and decision is not None
    # A silent device is said above already; its entry would only repeat it.
    if held and not snap.relay_error:
        problem = entry.get("error") or entry.get("note")
        if problem:
            detail = f"{detail} {problem}" if detail else problem

    plan_line = None
    if decision is not None and decision.rule != "no_prices":
        changes = next_changes(plans, now, snap.current)
        if changes:
            parts = []
            for i, (moment, target) in enumerate(changes):
                local = moment.astimezone(tz)
                day = "" if local.date() == now.astimezone(tz).date() else "tomorrow "
                state = "on" if target else "off"
                if shadow:
                    parts.append(f"{state} {day}at {hhmm(local)}")
                else:
                    parts.append(f"{state}{' again' if i else ''} {day}at {hhmm(local)}")
            if shadow:
                plan_line = "The rules would switch " + ", then ".join(parts) + "."
            else:
                plan_line = "Plan: " + ", ".join(parts) + "."
        elif plans and any(p.slots for p in plans):
            plan_line = (
                "The rules would change nothing while prices are known."
                if shadow
                else "Plan: no change while prices are known."
            )
    watching = (
        "Shadow mode: this service decides and logs, but does not switch the relay."
        if shadow
        else None
    )
    return Hero(word, cls, why, detail, plan_line, watching)


def _live_sentence(rule: str, price: float, lines: Lines | None, reason: str) -> str:
    if lines is None:
        return reason
    if rule == "below_on":
        return f"Power costs {ct(price)} per kWh now, under the {ct(lines.on_line)} switch-on line."
    if rule == "above_off":
        return (
            f"Power costs {ct(price)} per kWh now, over the {ct(lines.off_line)} switch-off line."
        )
    if rule == "between":
        return (
            f"Power costs {ct(price)} per kWh now, between the {ct(lines.on_line)} and "
            f"{ct(lines.off_line)} lines, so the relay stays as it is."
        )
    if rule == "morning":
        return (
            f"Morning top-up: power costs {ct(price)} per kWh, under the "
            f"{ct(lines.morning_line)} morning line."
        )
    if rule == "negative":
        return f"Power costs {ct(price)} per kWh now. Below zero, the boiler always heats."
    return reason


def _banners(snap: Snapshot, tz: ZoneInfo, usable: bool) -> list[Banner]:
    banners = []
    if snap.store_error:
        banners.append(
            Banner(
                "error",
                f"Changes are kept in memory only: {snap.store_error}. They are lost when the "
                "service restarts.",
            )
        )
    if snap.store_problem:
        banners.append(Banner("warning", snap.store_problem))
    if not usable:
        why = snap.price_error or "the exporter has not served today's prices yet"
        banners.append(
            Banner("error", f"No usable prices: {why}. Nothing is switched until there are.")
        )
    elif snap.price_error and snap.price_success_at is not None:
        read = _local(snap.price_success_at, tz).strftime("%H:%M")
        banners.append(
            Banner(
                "warning",
                f"The exporter cannot be read ({snap.price_error}). Deciding with the prices "
                f"read at {read}, which still cover today.",
            )
        )
    if snap.relay_error:
        banners.append(
            Banner(
                "error",
                f"The Shelly does not answer: {snap.relay_error}. Nothing is switched until "
                "it does.",
            )
        )
    if snap.relay is not None and snap.relay.errors:
        banners.append(
            Banner(
                "error",
                f"The device reports {', '.join(snap.relay.errors)}. It is not switched until "
                "that clears.",
            )
        )
    if snap.settings.mode == "live" and not snap.switching_allowed:
        banners.append(
            Banner(
                "warning",
                "Live mode, but switching is turned off for this installation "
                "(ALLOW_SWITCHING), so nothing is switched.",
            )
        )
    if snap.device is not None and not snap.device.auth_enabled:
        banners.append(
            Banner(
                "warning",
                "Authentication is off on the device: anyone on its network can switch it. "
                "Turn it on in the Shelly app.",
            )
        )
    return banners


def _log(snap: Snapshot, tz: ZoneInfo, today: dt.date) -> list[LogRow]:
    rows = []
    for entry in reversed(snap.entries[-LOG_ROWS:]):
        try:
            moment = _local(dt.datetime.fromisoformat(str(entry.get("ts"))), tz)
        except ValueError:
            continue
        when = _when(moment, today)
        if entry.get("kind") == "change":
            label = "Override" if entry.get("what") == "override" else "Rules"
            by = entry.get("by")
            rows.append(
                LogRow(when, label, "change", str(entry.get("summary", "")), by and str(by))
            )
            continue
        target = entry.get("target")
        word = "on" if target else "off"
        outcome = entry.get("outcome")
        label, cls = {
            "switched": (f"Switched {word}", f"act-{word}"),
            "failed": ("Failed", "act-error"),
            "unchanged": (word.capitalize(), f"act-{word}"),
            "would_switch": (f"Would switch {word}", "act-would"),
            "held_back": ("Held back", "act-error"),
            "kept": ("Keep", "act-keep"),
            "no_action": ("No price", "act-error"),
        }.get(str(outcome), ("?", "act-keep"))
        text = str(entry.get("reason") or "")
        if outcome in ("held_back", "failed"):
            extra = entry.get("error") or entry.get("note")
            if extra:
                text = f"{text} {extra}"
        rows.append(LogRow(when, label, cls, text, None))
    return rows


def _status(snap: Snapshot, tz: ZoneInfo, today: dt.date) -> list[StatusRow]:
    rows = []
    book = snap.book
    if book.slots:
        until = _local(book.slots[-1].end, tz)
        ends = {
            today + dt.timedelta(days=1): "the end of today",
            today + dt.timedelta(days=2): "the end of tomorrow",
        }
        known = _when(until, today)
        if until.time() == dt.time(0) and until.date() in ends:
            known = ends[until.date()]
        read = f", read {_ago(snap.now - snap.price_success_at)}" if snap.price_success_at else ""
        rows.append(StatusRow("Prices", f"known until {known}{read}"))
    else:
        rows.append(StatusRow("Prices", "none yet"))
    if snap.relay_error:
        last = (
            f", last read {_ago(snap.now - snap.relay_read_at)}"
            if snap.relay_read_at is not None
            else ", never read since the start"
        )
        rows.append(StatusRow("Relay", f"not answering{last}"))
    elif snap.relay is not None and snap.relay_read_at is not None:
        relay = snap.relay
        state = "on" if relay.output else "off"
        power = f", {relay.apower:.0f} W" if relay.apower is not None else ""
        source = f", last changed by {relay.source}" if relay.source else ""
        rows.append(
            StatusRow(
                "Relay", f"{state}{power}{source}, read {_ago(snap.now - snap.relay_read_at)}"
            )
        )
    if snap.device is not None:
        device = snap.device
        rows.append(StatusRow("Device", f"{device.model or 'Shelly'}, firmware {device.version}"))
    rows.append(
        StatusRow(
            "Switching",
            "allowed for this installation"
            if snap.switching_allowed
            else "turned off for this installation (ALLOW_SWITCHING)",
        )
    )
    if snap.updated_at:
        try:
            changed = _when(_local(dt.datetime.fromisoformat(snap.updated_at), tz), today)
        except ValueError:
            changed = snap.updated_at
        # The name comes from a header any client could send: shown, not vouched for.
        who = f", by {snap.updated_by} (as named by the Tailscale proxy)" if snap.updated_by else ""
        rows.append(StatusRow("Last change", f"{changed}{who}"))
    return rows


def _until_options(now: dt.datetime, tz: ZoneInfo) -> tuple[tuple[tuple[str, str], ...], str]:
    """Every quarter hour of the next 24 hours, as (ISO time, label), and the default.

    A list of clock times reads the same on every phone, unlike a time input,
    which some browsers show with AM and PM. The value is the full time with
    its offset, so an option that has passed while the page stood open is
    refused, not read as the same time tomorrow.
    """
    utc = now.astimezone(dt.UTC)
    first = utc.replace(minute=utc.minute // 15 * 15, second=0, microsecond=0)
    today = now.astimezone(tz).date()
    options = []
    for i in range(1, 97):
        # Steps of real time: in the hour that repeats when summer time ends
        # both 02:15s are offered, and a skipped hour never is.
        moment = (first + dt.timedelta(minutes=15 * i)).astimezone(tz)
        label = hhmm(moment) if moment.date() == today else f"{hhmm(moment)} tomorrow"
        options.append((moment.isoformat(), label))
    default = (first + dt.timedelta(hours=2)).astimezone(tz)
    return tuple(options), default.isoformat()


def _empty_text(day: dt.date, today: dt.date, local_now: dt.datetime, any_prices: bool) -> str:
    if day == today:
        return "There are no prices for today yet. Nothing is switched until there are."
    if not any_prices:
        return "No prices for tomorrow either. Tibber publishes them around 13:00."
    if local_now.hour < 13:
        return "Tomorrow's prices usually arrive around 13:00. The plan fills in when they do."
    return (
        "Tomorrow's prices are not in yet. Tibber usually publishes them around 13:00; "
        "the plan fills in when they arrive."
    )


def build_view(snap: Snapshot, tz: ZoneInfo, name: str) -> PageView:
    now = dt.datetime.fromtimestamp(snap.now, dt.UTC)
    local_now = now.astimezone(tz)
    today = local_now.date()
    tomorrow = today + dt.timedelta(days=1)
    plans = plan_days(
        snap.settings, snap.book, now, snap.current, snap.override, tz, [today, tomorrow]
    )
    shown = [p for p in plans if p.slots]
    morning = snap.settings.morning_enabled
    scale = y_scale(shown, morning=morning)
    any_prices = bool(shown)
    days = []
    charts = []
    for plan in plans:
        title = _title(plan.day, today)
        if plan.slots and scale is not None:
            chart_id = "today" if plan.day == today else "tomorrow"
            chart = build_chart(plan, scale, now, tz, chart_id, title, snap.settings)
            charts.append(chart)
            days.append(DayView(chart, title, None))
        else:
            days.append(DayView(None, title, _empty_text(plan.day, today, local_now, any_prices)))
    key = None
    if charts:
        key = Key(
            zero=scale is not None and scale.low < 0,
            morning=any(c.morning is not None for c in charts),
            unknown=any(c.unknown for c in charts),
            nodata=any(c.nodata for c in charts),
            lines=any(c.band is not None for c in charts),
            planned=any(p.target is not None for plan in plans for p in plan.slots),
        )

    usable = prices_usable(snap, tz)
    override = snap.override
    override_text = None
    if override is not None and override.until > now:
        what = "heat" if override.action == "on" else "keep off"
        override_text = f"Set to {what} until {until_text(override.until, now, tz)}."
    options, default_until = _until_options(now, tz)
    live = snap.settings.mode == "live"
    if live and snap.switching_allowed:
        mode_label, mode_cls = "Live", "live"
    elif live:
        mode_label, mode_cls = "Live, switching off", "live-blocked"
    else:
        mode_label, mode_cls = "Shadow mode", "shadow"
    return PageView(
        name=name,
        mode=snap.settings.mode,
        live=live,
        switching_allowed=snap.switching_allowed,
        mode_label=mode_label,
        mode_cls=mode_cls,
        hero=_hero(snap, tz, plans, now),
        banners=tuple(_banners(snap, tz, usable)),
        days=tuple(days),
        key=key,
        lines_today=lines_for(snap.settings, snap.book, today, tz),
        override_text=override_text,
        override_after=AFTER_OVERRIDE["live" if live else "shadow"],
        override_options=options,
        override_default=default_until,
        quarter_times=tuple(f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 15, 30, 45)),
        log=tuple(_log(snap, tz, today)),
        status=tuple(_status(snap, tz, today)),
        average_labels=AVERAGE_LABELS,
    )
