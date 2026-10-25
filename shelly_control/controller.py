"""The controller: one thread that reads prices, decides and switches.

Every call to the device and to the exporter happens on this one thread, one
evaluation at a time, so two decisions can never race each other to the
relay. The web interface only changes the settings or the override and asks
for an evaluation; it never talks to the device itself.

When it evaluates:

- at every quarter hour, a few seconds after the slot starts;
- at once after a change to the settings or the override;
- when an override ends;
- for a retry, when the device could not be read or switched, and for a
  re-check in shadow mode when the relay disagrees with the rules (the old
  controller may simply not have switched yet).

The loop never sleeps longer than 30 s, so a clock step or a long pause (a
suspended node) leads to one evaluation of the quarter it wakes up in, not to
a burst of evaluations for every quarter it missed. A quarter is evaluated
once: a clock that steps back a few seconds across a quarter boundary does
not run the previous quarter again. The spacing between switches and the
wait after a refused password are measured on the monotonic clock, which no
clock step moves.

When it switches: only in live mode or for an override, only when
ALLOW_SWITCHING is on, only when the relay's state differs from the decision,
never on a rule's decision without a usable price (an override needs none),
never while the device reports a protection error, and never twice within
MIN_SWITCH_INTERVAL_SECONDS (60 s by default).
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from zoneinfo import ZoneInfo

from .logs import event
from .prices import (
    PRICE_ERROR_REASONS,
    ExporterViolation,
    PriceBook,
    PriceClient,
    PriceError,
    local_day,
    merge,
)
from .rules import (
    Decision,
    Override,
    Settings,
    decide,
    lines_for,
    parse_settings,
    until_text,
)
from .shelly import (
    SHELLY_ERROR_REASONS,
    AllowlistViolation,
    DeviceInfo,
    ShellyAuthError,
    ShellyClient,
    ShellyError,
    SwitchStatus,
)
from .store import Store, StoreError

LOG = logging.getLogger("shelly-control")

QUARTER = 900
# The loop wakes at least this often, so a clock step is noticed in time.
MAX_SLEEP = 30.0
# A quarter's evaluation reads the prices again if the last try is older.
PRICE_MIN_GAP = 60.0
# After a 401, wait this long before sending the password again.
AUTH_BACKOFF = 300.0
# Retries and re-checks per quarter, after the quarter's own evaluation.
MAX_FOLLOWUPS = 3
# While the exporter stays away, repeat the error line this often.
REMINDER_SECONDS = 600.0

SWITCH_FAILURE_REASONS = (*SHELLY_ERROR_REASONS, "not_confirmed")
TRIGGERS = ("start", "quarter", "change", "override_end", "retry", "recheck")

# What happened to a decision; a short word for the log and the page.
OUTCOMES = (
    "switched",  # Switch.Set sent and the relay confirmed it
    "failed",  # Switch.Set failed, or the relay did not follow
    "unchanged",  # the relay already is where the decision wants it
    "would_switch",  # shadow mode: the relay differs, nothing is sent
    "held_back",  # the relay differs, but switching is not allowed now
    "kept",  # the rules keep the state: nothing to send
    "no_action",  # no usable price: nothing is decided
)

_SETTING_LABELS = {
    "mode": "Mode",
    "on_factor": "Switch-on line",
    "off_factor": "Switch-off line",
    "morning_enabled": "Morning top-up",
    "morning_start": "Morning top-up start",
    "morning_end": "Morning top-up end",
    "morning_factor": "Morning line",
    "negative_enabled": "Negative prices rule",
    "average_window": "Average",
}


def _due(last: float | None, now: float, interval: float) -> bool:
    """Whether `interval` has passed since `last`. A clock that stepped back counts as passed."""
    return last is None or not 0 <= now - last < interval


def _iso(moment: float | None) -> str | None:
    if moment is None:
        return None
    return dt.datetime.fromtimestamp(moment, dt.UTC).isoformat(timespec="seconds")


def _show(value: object) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


@dataclass(frozen=True)
class Outcome:
    kind: str
    note: str = ""
    sent: bool = False
    relay_after: bool | None = None
    error: str | None = None
    # When to evaluate again because of this outcome, if at all.
    retry_at: float | None = None


@dataclass(frozen=True)
class Counters:
    evaluations: int = 0
    switch_commands: Mapping[str, int] = field(default_factory=dict)
    switch_failures: Mapping[str, int] = field(default_factory=dict)
    device_read_errors: Mapping[str, int] = field(default_factory=dict)
    price_read_errors: Mapping[str, int] = field(default_factory=dict)
    shadow_disagreements: int = 0
    state_write_errors: int = 0


@dataclass(frozen=True)
class Snapshot:
    """A consistent, read-only view for the page, the API and /metrics."""

    now: float
    settings: Settings
    override: Override | None
    updated_at: str | None
    updated_by: str | None
    decision: Decision | None
    last_entry: dict | None
    current: bool | None
    relay: SwitchStatus | None
    relay_read_at: float | None
    relay_error: str | None
    device: DeviceInfo | None
    book: PriceBook
    price_attempt_at: float | None
    price_success_at: float | None
    price_error: str | None
    entries: tuple[dict, ...]
    store_problem: str | None
    store_error: str | None
    switching_allowed: bool
    last_evaluation_at: float | None
    counters: Counters


class Controller:
    def __init__(
        self,
        *,
        shelly: ShellyClient,
        prices: PriceClient,
        store: Store,
        tz: ZoneInfo,
        defaults: Settings,
        switch_id: int = 0,
        allow_switching: bool = False,
        clock=time.time,
        monotonic=time.monotonic,
        evaluation_delay: float = 5.0,
        price_refresh: float = 300.0,
        min_switch_interval: float = 60.0,
        retry_delay: float = 60.0,
        log_size: int = 700,
    ) -> None:
        self.shelly = shelly
        self.prices = prices
        self.store = store
        self.tz = tz
        self.switch_id = switch_id
        self.allow_switching = allow_switching
        self.clock = clock
        # For intervals that must not move with the wall clock.
        self.monotonic = monotonic
        self.evaluation_delay = evaluation_delay
        self.price_refresh = price_refresh
        self.min_switch_interval = min_switch_interval
        self.retry_delay = retry_delay

        self._lock = threading.RLock()
        # Held for a whole evaluation: one at a time, also if called directly.
        self._evaluating = threading.Lock()
        self._evaluated = threading.Condition(self._lock)
        self._wake = threading.Event()
        self._change_requested = False

        loaded = store.load_state(defaults)
        self._settings = loaded.settings
        self._override = loaded.override
        self._updated_at = loaded.updated_at
        self._updated_by = loaded.updated_by
        self._store_problem = loaded.problem
        self._store_error: str | None = None

        self._entries: deque[dict] = deque(store.load_decisions(), maxlen=log_size)
        self._book = store.load_prices() or PriceBook()
        self._price_attempt_at: float | None = None
        self._price_success_at: float | None = self._book.fetched_at
        self._price_error: str | None = None
        self._price_failing_since: float | None = None
        self._price_reminder_at = 0.0

        self._decision: Decision | None = None
        self._last_entry: dict | None = None
        # The state a "keep" keeps: the relay as read in live mode, the state
        # this service would have set in shadow mode.
        self._current: bool | None = None
        self._relay: SwitchStatus | None = None
        self._relay_read_at: float | None = None
        self._relay_error: str | None = None
        self._device: DeviceInfo | None = None
        # Both on the monotonic clock.
        self._auth_blocked_until: float | None = None
        self._last_switch_at: float | None = None
        self._last_evaluation_at: float | None = None

        self._last_quarter: int | None = None
        self._followup_at: float | None = None
        self._followup_kind = "retry"
        self._followups = 0

        self._evaluations = 0
        self._switch_commands = {"on": 0, "off": 0}
        self._switch_failures = dict.fromkeys(SWITCH_FAILURE_REASONS, 0)
        self._device_read_errors = dict.fromkeys(SHELLY_ERROR_REASONS, 0)
        self._price_read_errors = dict.fromkeys(PRICE_ERROR_REASONS, 0)
        self._shadow_disagreements = 0
        self._state_write_errors = 0

    # -- what the web layer may call -------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(
                now=self.clock(),
                settings=self._settings,
                override=self._override,
                updated_at=self._updated_at,
                updated_by=self._updated_by,
                decision=self._decision,
                last_entry=self._last_entry,
                current=self._current,
                relay=self._relay,
                relay_read_at=self._relay_read_at,
                relay_error=self._relay_error,
                device=self._device,
                book=self._book,
                price_attempt_at=self._price_attempt_at,
                price_success_at=self._price_success_at,
                price_error=self._price_error,
                entries=tuple(self._entries),
                store_problem=self._store_problem,
                store_error=self._store_error,
                switching_allowed=self.allow_switching,
                last_evaluation_at=self._last_evaluation_at,
                counters=Counters(
                    evaluations=self._evaluations,
                    switch_commands=dict(self._switch_commands),
                    switch_failures=dict(self._switch_failures),
                    device_read_errors=dict(self._device_read_errors),
                    price_read_errors=dict(self._price_read_errors),
                    shadow_disagreements=self._shadow_disagreements,
                    state_write_errors=self._state_write_errors,
                ),
            )

    def update_settings(
        self, changes: Mapping[str, object], by: str | None
    ) -> tuple[Settings, dict[str, str]]:
        """Validate and apply `changes`. Returns (settings now in force, errors by field)."""
        with self._lock:
            old = self._settings
            new, errors = parse_settings(changes, old)
            if errors:
                return old, errors
            changed = [
                f"{_SETTING_LABELS[k]}: {_show(old.to_json()[k])} to {_show(v)}"
                for k, v in new.to_json().items()
                if old.to_json()[k] != v
            ]
            if not changed:
                return old, {}
            self._settings = new
            if new.mode != old.mode:
                # Shadow mode's state is a guess; live mode starts from the relay.
                self._current = self._relay.output if self._relay else None
            self._change("settings", "; ".join(changed), by, mode=new.mode, settings=new.to_json())
        self.request_evaluation()
        return new, {}

    def set_override(self, action: str, until: dt.datetime, by: str | None) -> Override:
        now = dt.datetime.fromtimestamp(self.clock(), dt.UTC)
        override = Override(action, until.astimezone(dt.UTC), now, by)
        with self._lock:
            self._override = override
            when = until_text(override.until, now, self.tz)
            summary = f"Override: {'heat' if action == 'on' else 'keep off'} until {when}"
            self._change("override", summary, by, override=override.to_json())
        self.request_evaluation()
        return override

    def clear_override(self, by: str | None) -> bool:
        with self._lock:
            if self._override is None:
                return False
            self._override = None
            self._change("override", f"Override ended early; {self._after_override()}", by)
        self.request_evaluation()
        return True

    def _after_override(self) -> str:
        """What happens to the relay when an override ends, for the log."""
        if self._settings.mode == "live":
            return "the rules decide again"
        # Shadow mode never switches on a rule's decision, so nothing puts the
        # relay back: it stays where the override left it.
        return "in shadow mode the relay stays as the override left it"

    def request_evaluation(self) -> None:
        with self._lock:
            self._change_requested = True
        self._wake.set()

    def wait_for_evaluation(self, after: int, timeout: float) -> bool:
        """Wait until more than `after` evaluations have run, or `timeout` passes."""
        deadline = time.monotonic() + timeout
        with self._evaluated:
            while self._evaluations <= after:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._evaluated.wait(remaining)
            return True

    # -- the loop --------------------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        event(LOG, "controller_start", "controller running")
        while not stop.is_set():
            try:
                self.tick()
            except (AllowlistViolation, ExporterViolation):
                raise
            except Exception:  # noqa: BLE001 - one bad evaluation must not stop the controller
                LOG.exception("evaluation failed", extra={"fields": {"event": "bug"}})
            self._wake.wait(self.seconds_until_due())
            self._wake.clear()

    def stop(self) -> None:
        self._wake.set()

    def tick(self, now: float | None = None) -> str | None:
        """Do whatever is due now. Returns the evaluation's trigger, if one ran."""
        now = self.clock() if now is None else now
        quarter = int(now // QUARTER) * QUARTER
        with self._lock:
            trigger = None
            if self._change_requested:
                self._change_requested = False
                trigger = "change"
            elif self._new_quarter(quarter) and now >= quarter + self.evaluation_delay:
                trigger = "start" if self._last_quarter is None else "quarter"
            elif self._override is not None and now >= self._override.until.timestamp():
                trigger = "override_end"
            elif self._followup_at is not None and now >= self._followup_at:
                trigger = self._followup_kind
            if trigger in ("start", "quarter"):
                self._last_quarter = quarter
                self._followups = 0
                self._followup_at = None
            elif trigger in ("retry", "recheck"):
                self._followup_at = None
            price_due = _due(self._price_attempt_at, now, self.price_refresh)
        if trigger is not None:
            self.evaluate(trigger, now)
        elif price_due:
            self.refresh_prices(now)
        return trigger

    def _new_quarter(self, quarter: int) -> bool:
        """Whether `quarter` has not been evaluated yet.

        Only a later quarter is new. A clock that stepped back a little (an NTP
        step across a quarter boundary) would otherwise run the previous
        quarter again at once and switch back what was just switched. A clock
        that stepped back by more than a whole quarter was wrong before, and
        the quarter it now shows is evaluated.
        """
        last = self._last_quarter
        return last is None or quarter > last or quarter < last - QUARTER

    def seconds_until_due(self, now: float | None = None) -> float:
        now = self.clock() if now is None else now
        with self._lock:
            quarter = int(now // QUARTER) * QUARTER
            if self._new_quarter(quarter) and now < quarter + self.evaluation_delay:
                due = quarter + self.evaluation_delay
            else:
                due = quarter + QUARTER + self.evaluation_delay
            candidates = [due]
            if self._price_attempt_at is not None:
                candidates.append(self._price_attempt_at + self.price_refresh)
            if self._override is not None:
                candidates.append(self._override.until.timestamp())
            if self._followup_at is not None:
                candidates.append(self._followup_at)
        return min(MAX_SLEEP, max(0.2, min(candidates) - now))

    # -- prices ----------------------------------------------------------------------------------

    def refresh_prices(self, now: float | None = None) -> bool:
        now = self.clock() if now is None else now
        with self._lock:
            self._price_attempt_at = now
        try:
            slots = self.prices.fetch()
        except ExporterViolation:
            raise
        except PriceError as exc:
            self._price_failed(now, exc)
            return False
        today = dt.datetime.fromtimestamp(now, self.tz).date()
        with self._lock:
            book = merge(self._book, slots, now, self.tz, today - dt.timedelta(days=1))
            changed = book.slots != self._book.slots
            self._book = book
            self._price_success_at = now
            self._price_error = None
            recovered = self._price_failing_since
            self._price_failing_since = None
        if recovered is not None:
            event(
                LOG,
                "prices_back",
                f"the exporter answers again after {int(now - recovered)} s",
            )
        if changed:
            last = book.slots[-1].end.astimezone(self.tz).isoformat() if book.slots else None
            event(
                LOG,
                "prices_changed",
                "new prices",
                slots=len(book.slots),
                known_until=last,
            )
            self._persist(lambda: self.store.save_prices(book))
        return True

    def _price_failed(self, now: float, exc: PriceError) -> None:
        with self._lock:
            self._price_read_errors[exc.reason] += 1
            self._price_error = str(exc)
            first = self._price_failing_since is None
            if first:
                self._price_failing_since = now
                self._price_reminder_at = now
            remind = not first and now - self._price_reminder_at >= REMINDER_SECONDS
            if remind:
                self._price_reminder_at = now
            since = self._price_failing_since
        if first or remind:
            event(
                LOG,
                "prices_failing",
                f"cannot read prices: {exc}",
                logging.ERROR,
                reason=exc.reason,
                failing_for_seconds=int(now - since),
            )

    # -- one evaluation --------------------------------------------------------------------------

    def evaluate(self, trigger: str, now: float | None = None) -> dict:
        with self._evaluating:
            return self._evaluate(trigger, self.clock() if now is None else now)

    def _evaluate(self, trigger: str, now: float) -> dict:
        if _due(self._price_attempt_at, now, PRICE_MIN_GAP):
            self.refresh_prices(now)
        moment = dt.datetime.fromtimestamp(now, dt.UTC)
        with self._lock:
            if self._override is not None and not self._override.active(moment):
                local = until_text(self._override.until, moment, self.tz)
                self._override = None
                self._change(
                    "override", f"Override ended at {local}; {self._after_override()}", None
                )
            settings, override = self._settings, self._override

        relay, read_error = self._read_relay(now)

        with self._lock:
            if relay is not None and (settings.mode == "live" or self._current is None):
                self._current = relay.output
            elif relay is None and settings.mode == "live":
                self._current = None
            current = self._current
            decision = decide(settings, self._book, moment, current, override, self.tz)

        outcome = self._apply(decision, relay, settings, now)

        with self._lock:
            if relay is not None and outcome.relay_after is not None:
                self._relay = replace(relay, output=outcome.relay_after)
            if decision.target is not None and settings.mode == "shadow":
                self._current = decision.target
            elif outcome.relay_after is not None:
                self._current = outcome.relay_after
            if (
                trigger == "recheck"
                and outcome.kind == "would_switch"
                and decision.rule != "override"
            ):
                self._shadow_disagreements += 1
            self._schedule_followup(trigger, outcome, now)
            entry = self._entry(trigger, now, decision, settings, relay, outcome, read_error)
            self._decision = decision
            self._last_entry = entry
            self._entries.append(entry)
            self._last_evaluation_at = now
            self._evaluations += 1
            entries = list(self._entries)
            self._evaluated.notify_all()
        event(LOG, "evaluation", "", **entry)
        self._persist(lambda: self.store.save_decisions(entries))
        return entry

    def _schedule_followup(self, trigger: str, outcome: Outcome, now: float) -> None:
        if self._followups >= MAX_FOLLOWUPS:
            return
        if outcome.retry_at is not None:
            self._followup_at, self._followup_kind = outcome.retry_at, "retry"
        elif outcome.kind == "would_switch" and trigger != "recheck":
            # The old controller may switch a few seconds after the quarter
            # starts. Look again before counting a disagreement.
            self._followup_at, self._followup_kind = now + self.retry_delay, "recheck"
        else:
            return
        self._followups += 1

    def _read_relay(self, now: float) -> tuple[SwitchStatus | None, str | None]:
        if self._auth_blocked_until is not None and self.monotonic() < self._auth_blocked_until:
            return None, "waiting after the device refused the password"
        error: ShellyError | None = None
        for _ in range(2):
            try:
                status = self.shelly.get_switch_status(self.switch_id)
            except AllowlistViolation:
                raise
            except ShellyError as exc:
                error = exc
                with self._lock:
                    self._device_read_errors[exc.reason] += 1
                if isinstance(exc, ShellyAuthError):
                    self._auth_blocked_until = self.monotonic() + AUTH_BACKOFF
                    break
                continue
            with self._lock:
                recovered = self._relay_error is not None
                self._relay, self._relay_read_at, self._relay_error = status, now, None
            if recovered:
                event(LOG, "device_back", "the device answers again")
            if self._device is None:
                self._read_device_info()
            return status, None
        message = str(error)
        with self._lock:
            first = self._relay_error is None
            self._relay_error = message
        if first:
            event(LOG, "device_failing", f"cannot read the relay: {message}", logging.ERROR)
        return None, message

    def _read_device_info(self) -> None:
        try:
            info = self.shelly.get_device_info()
        except ShellyError:
            return
        with self._lock:
            self._device = info
        event(
            LOG,
            "device_found",
            f"found {info.model or 'a Shelly'} (gen {info.gen}), firmware {info.version}, "
            f"authentication {'on' if info.auth_enabled else 'off'}",
        )
        if not info.auth_enabled:
            event(
                LOG,
                "device_auth_off",
                "authentication is off on the device: anyone on its network can switch the "
                "relay. Turn it on in the Shelly app or web interface.",
                logging.WARNING,
            )

    def _apply(
        self, decision: Decision, relay: SwitchStatus | None, settings: Settings, now: float
    ) -> Outcome:
        """Send Switch.Set if, and only if, this decision may and must change the relay."""
        if decision.rule == "no_prices":
            return Outcome("no_action", "Nothing is decided without a price.")
        if decision.action == "keep" or decision.target is None:
            return Outcome("kept")
        target = decision.target
        word = "on" if target else "off"
        if relay is None:
            return Outcome(
                "held_back",
                "The relay's state is unknown, so nothing is sent.",
                retry_at=now + self.retry_delay,
            )
        if relay.output == target:
            return Outcome("unchanged", f"The relay is already {word}.")
        if decision.rule != "override" and settings.mode != "live":
            return Outcome("would_switch", f"Shadow mode: would switch {word}.")
        if not self.allow_switching:
            return Outcome(
                "held_back",
                f"Would switch {word}, but switching is turned off for this installation "
                "(ALLOW_SWITCHING).",
            )
        if relay.errors:
            return Outcome(
                "held_back",
                f"Would switch {word}, but the device reports {', '.join(relay.errors)}.",
            )
        mono = self.monotonic()
        if (
            self._last_switch_at is not None
            and mono - self._last_switch_at < self.min_switch_interval
        ):
            wait = self._last_switch_at + self.min_switch_interval - mono
            return Outcome(
                "held_back",
                f"Waiting {math.ceil(wait)} s before switching again, to spare the relay.",
                retry_at=now + wait,
            )
        self._last_switch_at = mono
        try:
            self.shelly.set_output(self.switch_id, target)
        except AllowlistViolation:
            raise
        except ShellyError as exc:
            with self._lock:
                self._switch_failures[exc.reason] += 1
            if isinstance(exc, ShellyAuthError):
                self._auth_blocked_until = self.monotonic() + AUTH_BACKOFF
            return Outcome("failed", error=str(exc), retry_at=now + self.retry_delay)
        with self._lock:
            self._switch_commands[word] += 1
        try:
            after = self.shelly.get_switch_status(self.switch_id).output
        except ShellyError as exc:
            return Outcome(
                "switched",
                f"Switched {word}; the device did not confirm.",
                sent=True,
                error=str(exc),
            )
        if after != target:
            with self._lock:
                self._switch_failures["not_confirmed"] += 1
            return Outcome(
                "failed",
                sent=True,
                relay_after=after,
                error=f"Sent {word}, but the relay is still {'on' if after else 'off'}.",
                retry_at=now + self.retry_delay,
            )
        return Outcome("switched", f"Switched {word}.", sent=True, relay_after=after)

    # -- records ---------------------------------------------------------------------------------

    def _entry(
        self,
        trigger: str,
        now: float,
        decision: Decision,
        settings: Settings,
        relay: SwitchStatus | None,
        outcome: Outcome,
        read_error: str | None,
    ) -> dict:
        slot, lines = decision.slot, decision.lines
        before = relay.output if relay else None
        after = outcome.relay_after if outcome.relay_after is not None else before
        reason = decision.reason
        if decision.rule == "no_prices" and self._price_error:
            reason = f"{reason.removesuffix('.')}: {self._price_error}."
        return {
            "kind": "evaluation",
            "ts": _iso(now),
            "trigger": trigger,
            "slot_start": slot.start.astimezone(self.tz).isoformat() if slot else None,
            "price": slot.total if slot else None,
            "average": round(lines.average, 6) if lines else None,
            "on_line": round(lines.on_line, 6) if lines else None,
            "off_line": round(lines.off_line, 6) if lines else None,
            "morning_line": round(lines.morning_line, 6) if lines else None,
            "morning": decision.morning,
            "rule": decision.rule,
            "action": decision.action,
            "target": decision.target,
            "mode": settings.mode,
            "override": decision.override.to_json() if decision.override else None,
            "outcome": outcome.kind,
            "sent": outcome.sent,
            "relay_before": before,
            "relay_after": after,
            "reason": reason,
            "note": outcome.note,
            "error": outcome.error or read_error,
        }

    def _change(self, what: str, summary: str, by: str | None, **fields) -> None:
        """Record a change by the owner (or an override running out), and save the state."""
        now = self.clock()
        entry = {"kind": "change", "ts": _iso(now), "what": what, "summary": summary, "by": by}
        self._entries.append(entry)
        self._updated_at, self._updated_by = _iso(now), by
        event(LOG, "change", summary, what=what, by=by, **fields)
        settings, override = self._settings, self._override
        self._persist(lambda: self.store.save_state(settings, override, by), state=True)

    def _persist(self, write, *, state: bool = False) -> None:
        try:
            write()
        except StoreError as exc:
            with self._lock:
                self._state_write_errors += 1
                first = self._store_error is None
                self._store_error = str(exc)
            if first:
                event(LOG, "store_failing", str(exc), logging.ERROR)
            return
        with self._lock:
            if self._store_error is not None:
                event(LOG, "store_back", "the state directory takes writes again")
            self._store_error = None
            if state:
                # The settings on disk are the ones in force again.
                self._store_problem = None


def prices_usable(snapshot: Snapshot, tz: ZoneInfo) -> bool:
    """Whether the prices cover this quarter and the whole of its day."""
    moment = dt.datetime.fromtimestamp(snapshot.now, dt.UTC)
    slot = snapshot.book.slot_at(moment)
    if slot is None:
        return False
    return lines_for(snapshot.settings, snapshot.book, local_day(slot.start, tz), tz) is not None
