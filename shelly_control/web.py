"""The web interface and its JSON API, plus /metrics, /healthz and /readyz.

There is no login: whoever reaches the page can change the rules and switch
the relay. The tailnet is the gate. What this module adds, because a browser
on the tailnet also visits other sites:

- Host allowlist (ALLOWED_HOSTS) on every page and API call, so a DNS
  rebinding page cannot talk to the service under its own name. /metrics,
  /healthz and /readyz are exempt: probes and scrapes arrive with the pod's IP.
- State changes only by POST, PUT or DELETE. Each is refused if the browser
  says it comes from another site (Sec-Fetch-Site), or, from a browser that
  does not say, if its Origin is not this host.
- A synchronizer token on every form and API change. It is made once per
  process and never stored, so there is no cookie, session or secret key.
- JSON API changes also need Content-Type: application/json. No CORS headers
  are ever sent.
- A Content-Security-Policy without inline script or style; templates escape
  everything.
"""

from __future__ import annotations

import datetime as dt
import hmac
import json
import logging
import secrets
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, jsonify, redirect, render_template, request
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from prometheus_client.registry import CollectorRegistry
from werkzeug.exceptions import HTTPException

from .controller import Controller, OverrideLocked, Snapshot, prices_usable
from .logs import event
from .rules import (
    OVERRIDE_ACTIONS,
    Settings,
    ct,
    parse_until,
    parse_vacation_end,
    plan_days,
)
from .view import HEAT_UP_TEXT, build_view

LOG = logging.getLogger("shelly-control.web")

OPS_PATHS = frozenset({"/metrics", "/healthz", "/readyz"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SAME_SITE = frozenset({"same-origin", "none"})
CSRF_HEADER = "X-CSRF-Token"

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; object-src 'none'"
)

# ?notice= takes one of these keys; the text never comes from the URL. Each
# notice shows in the section it is about.
NOTICES = {
    "saved": ("rules", "Rules saved."),
    "unchanged": ("rules", "Nothing changed."),
    "override": ("override", "Override set."),
    "cleared": ("override", "Override ended."),
    "no_override": ("override", "There was no override to end."),
    "vacation": ("vacation", "Vacation started. The boiler is switched off completely."),
    "vacation_ended": (
        "vacation",
        f"Vacation ended. The boiler heats for {HEAT_UP_TEXT} whatever the price, "
        "so the water can reach a safe temperature.",
    ),
    "heat_locked": (
        "vacation",
        "The boiler is heating after the vacation and stays on until that time is over.",
    ),
}

# Field names as the page labels them, and the control each error links to.
FIELDS = {
    "on_factor": ("Switch on below", "on_factor"),
    "off_factor": ("Switch off above", "off_factor"),
    "morning_enabled": ("Morning top-up", "morning_enabled"),
    "morning_start": ("Morning top-up from", "morning_start"),
    "morning_end": ("Morning top-up until", "morning_end"),
    "morning_factor": ("Morning line", "morning_factor"),
    "negative_enabled": ("Negative prices", "negative_enabled"),
    "average_window": ("Average", "average_window-day"),
    "hot_every_hours": ("Legionella guard", "hot_every_hours"),
    "mode": ("Mode", "mode-shadow"),
    "confirm_live": ("Going live", "confirm_live"),
}

# A page view or API change waits this long for the evaluation it caused, so
# the answer already shows the new decision.
SETTLE_SECONDS = 3.0
MAX_DECISIONS = 5000
# Distinct rejected Host values logged, so a scanner cannot fill the log.
MAX_LOGGED_HOSTS = 20

CONFIRM_VACATION = (
    "Tick the box to confirm that you understand the water may be unsafe until the boiler "
    "has fully heated up again."
)
VACATION_RUNNING = (
    "A vacation is running. End it first; the boiler then heats before anything else."
)
HEAT_UP_RUNNING = (
    f"The boiler is heating after a vacation, for at least {HEAT_UP_TEXT}, so the water "
    "can reach a safe temperature. It cannot be changed until that time is over."
)
SWITCHING_OFF = (
    "Switching is turned off for this installation. Set ALLOW_SWITCHING=true in its "
    "configuration first."
)
CONFIRM_LIVE = "Tick the box to confirm that this service may switch the relay from now on."


def _by() -> str | None:
    """Who made a change, for the log only: the Tailscale proxy's header, if any.

    Anyone who reaches the service can send this header, so it is never used
    to decide anything.
    """
    login = request.headers.get("Tailscale-User-Login", "").strip()[:120]
    return login if login and login.isprintable() else None


def _form_settings(form) -> dict[str, object]:
    """Form fields as parse_settings takes them; an unticked box is simply absent."""
    raw: dict[str, object] = {
        name: form.get(name, "")
        for name in (
            "on_factor",
            "off_factor",
            "morning_start",
            "morning_end",
            "morning_factor",
            "average_window",
            "hot_every_hours",
            "mode",
        )
    }
    raw["morning_enabled"] = "morning_enabled" in form
    raw["negative_enabled"] = "negative_enabled" in form
    return raw


def _form_values(settings: Settings) -> dict[str, object]:
    values = settings.to_json()
    for name in ("on_factor", "off_factor", "morning_factor"):
        values[name] = f"{settings.to_json()[name]:.2f}"
    return values


def _mode_errors(raw_mode: object, current: str, switching: bool, confirmed: bool) -> dict:
    if raw_mode != "live" or current == "live":
        return {}
    if not switching:
        return {"mode": SWITCHING_OFF}
    if not confirmed:
        return {"confirm_live": CONFIRM_LIVE}
    return {}


def _json_body() -> object:
    """The request's JSON, or None if it is not JSON this service can read.

    Parsed here rather than with get_json(silent=True), which lets a
    RecursionError (JSON nested a thousand levels deep) through as a 500.
    """
    try:
        return json.loads(request.get_data(cache=False))
    except (ValueError, RecursionError):
        return None


def _decision_json(snap: Snapshot) -> dict | None:
    entry = snap.last_entry
    return dict(entry) if entry else None


def create_app(
    controller: Controller,
    *,
    tz: ZoneInfo,
    allowed_hosts: list[str],
    display_name: str = "Boiler",
    registry: CollectorRegistry | None = None,
    token: str | None = None,
    settle_seconds: float = SETTLE_SECONDS,
) -> Flask:
    app = Flask(__name__)
    app.config.update(
        MAX_CONTENT_LENGTH=16 * 1024,
        MAX_FORM_MEMORY_SIZE=8 * 1024,
        MAX_FORM_PARTS=50,
    )
    app.json.sort_keys = False
    app.jinja_env.filters["ct"] = ct
    hosts = frozenset(h.lower() for h in allowed_hosts)
    csrf = token or secrets.token_urlsafe(32)
    logged_hosts: set[str] = set()

    def settle(after: int) -> None:
        if settle_seconds > 0:
            controller.wait_for_evaluation(after, settle_seconds)

    # -- the gate --------------------------------------------------------------------------------

    @app.before_request
    def gate():
        if request.path in OPS_PATHS:
            return None
        host = (request.environ.get("HTTP_HOST") or "").lower()
        if host not in hosts:
            if len(logged_hosts) < MAX_LOGGED_HOSTS and host not in logged_hosts:
                logged_hosts.add(host)
                event(
                    LOG,
                    "host_refused",
                    "refused a request for a host that is not in ALLOWED_HOSTS",
                    logging.WARNING,
                    host=host[:80],
                )
            abort(400, "This address is not one this service answers to.")
        if request.method in UNSAFE_METHODS:
            # The order of Go's CrossOriginProtection: a browser that sends
            # Sec-Fetch-Site has said where the request comes from, and its
            # Origin may then be "null" (Chrome sends that for a form under a
            # strict referrer policy). Without it, Origin must match the Host.
            site = request.headers.get("Sec-Fetch-Site")
            origin = request.headers.get("Origin")
            if site is not None:
                if site not in SAME_SITE:
                    abort(403, "Refused: the request came from another site.")
            elif origin is not None and urlsplit(origin).netloc.lower() != host:
                abort(403, "Refused: the request came from another origin.")
            if request.path.startswith("/api/"):
                if not request.is_json:
                    abort(415, "Send JSON, with Content-Type: application/json.")
                sent = request.headers.get(CSRF_HEADER, "")
            else:
                sent = request.form.get("csrf", "")
            if not hmac.compare_digest(sent.encode(), csrf.encode()):
                abort(403, "This form expired. Reload the page and try again.")
        return None

    @app.after_request
    def headers(resp: Response) -> Response:
        resp.headers["X-Content-Type-Options"] = "nosniff"
        # Not no-referrer: under that, browsers without Sec-Fetch-Site send
        # "Origin: null" on the page's own forms. There are no outside links.
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if not request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException):
        message = exc.description or exc.name
        if exc.code == 413:
            message = "The request is too large."
        if request.path.startswith("/api/"):
            return jsonify(error=message), exc.code
        return (
            render_template("error.html", code=exc.code, title=exc.name, message=message),
            exc.code,
        )

    # -- the page --------------------------------------------------------------------------------

    def page(
        status: int = 200,
        errors: dict[str, str] | None = None,
        form: dict[str, object] | None = None,
        override_error: str | None = None,
        notice: str | None = None,
        until: str | None = None,
        vacation: dict[str, object] | None = None,
    ):
        snap = controller.snapshot()
        view = build_view(snap, tz, display_name)
        section, text = NOTICES.get(notice or "", (None, None))
        errors = errors or {}
        error_links = [
            (FIELDS[name][1], FIELDS[name][0], message)
            for name, message in errors.items()
            if name in FIELDS
        ]
        options = [value for value, _ in view.override_options]
        return (
            render_template(
                "index.html",
                v=view,
                csrf=csrf,
                errors=errors,
                error_links=error_links,
                form=form or _form_values(snap.settings),
                override_error=override_error,
                # Keep a refused choice selected when it is still on offer.
                until_selected=until if until in options else view.override_default,
                # A vacation that was asked for but not confirmed: the page asks again, with
                # the day and time kept and the confirmation open.
                vacation=vacation or {},
                heat_up_text=HEAT_UP_TEXT,
                notice=text,
                notice_section=section,
            ),
            status,
        )

    @app.get("/")
    def index():
        return page(notice=request.args.get("notice"))

    @app.post("/settings")
    def settings_form():
        snap = controller.snapshot()
        raw = _form_settings(request.form)
        errors = _mode_errors(
            raw["mode"],
            snap.settings.mode,
            snap.switching_allowed,
            request.form.get("confirm_live") == "yes",
        )
        if not errors:
            before = snap.counters.evaluations
            new, errors = controller.update_settings(raw, _by())
            if not errors:
                changed = new != snap.settings
                if changed:
                    settle(before)
                return redirect(f"/?notice={'saved' if changed else 'unchanged'}#rules", 303)
        shown = {**_form_values(snap.settings), **raw}
        return page(422, errors=errors, form=shown)

    @app.post("/override")
    def override_form():
        snap = controller.snapshot()
        action = request.form.get("action")
        chosen = request.form.get("until")
        message = None
        if action not in OVERRIDE_ACTIONS:
            message = "Pick heat or keep off."
        elif not snap.switching_allowed:
            message = SWITCHING_OFF
        elif (held := controller.override_lock()) is not None:
            message = VACATION_RUNNING if held.kind == "vacation" else HEAT_UP_RUNNING
        else:
            now = dt.datetime.fromtimestamp(controller.clock(), dt.UTC)
            try:
                until = parse_until(chosen, now, tz)
            except ValueError as exc:
                message = str(exc)
        if message:
            return page(422, override_error=message, until=chosen)
        try:
            controller.set_override(action, until, _by())
        except OverrideLocked as exc:
            message = VACATION_RUNNING if exc.held.kind == "vacation" else HEAT_UP_RUNNING
            return page(422, override_error=message, until=chosen)
        settle(snap.counters.evaluations)
        return redirect("/?notice=override#override", 303)

    def _vacation_end(day: object, clock: object) -> str | None:
        """The page's day and time as one ISO time, or None when either is missing."""
        if isinstance(day, str) and isinstance(clock, str) and day.strip() and clock.strip():
            return f"{day.strip()}T{clock.strip()}"
        return None

    @app.post("/vacation")
    def vacation_form():
        """Switch the boiler off completely, and only with the confirmation ticked."""
        snap = controller.snapshot()
        day, clock = request.form.get("vacation_day"), request.form.get("vacation_time")
        kept = {"day": day, "time": clock}
        message = None
        if not snap.switching_allowed:
            message = SWITCHING_OFF
        elif (held := controller.override_lock()) is not None:
            message = VACATION_RUNNING if held.kind == "vacation" else HEAT_UP_RUNNING
        else:
            now = dt.datetime.fromtimestamp(controller.clock(), dt.UTC)
            try:
                until = parse_vacation_end(_vacation_end(day, clock), now, tz)
            except ValueError as exc:
                message = str(exc)
        if message:
            return page(422, vacation={**kept, "error": message})
        if request.form.get("confirm_vacation") != "yes":
            # Without the script this is the second step: the same page, the confirmation open.
            # With it, a missing tick is a form that was sent some other way.
            asked = request.form.get("asked") == "yes"
            return page(
                422,
                vacation={**kept, "confirm": True, "error": CONFIRM_VACATION if asked else None},
            )
        try:
            controller.set_override("off", until, _by(), kind="vacation")
        except OverrideLocked as exc:
            message = VACATION_RUNNING if exc.held.kind == "vacation" else HEAT_UP_RUNNING
            return page(422, vacation={**kept, "error": message})
        settle(snap.counters.evaluations)
        return redirect("/?notice=vacation#vacation", 303)

    @app.post("/override/clear")
    def override_clear_form():
        snap = controller.snapshot()
        ending = snap.override is not None and snap.override.kind == "vacation"
        try:
            cleared = controller.clear_override(_by())
        except OverrideLocked:
            return redirect("/?notice=heat_locked#vacation", 303)
        if cleared:
            settle(snap.counters.evaluations)
            if ending:
                return redirect("/?notice=vacation_ended#vacation", 303)
            return redirect("/?notice=cleared#override", 303)
        return redirect("/?notice=no_override#override", 303)

    # -- the JSON API ----------------------------------------------------------------------------

    def status_json(snap: Snapshot) -> dict:
        override = snap.override
        active = override is not None and override.until.timestamp() > snap.now
        return {
            "now": dt.datetime.fromtimestamp(snap.now, dt.UTC).isoformat(timespec="seconds"),
            "mode": snap.settings.mode,
            "switching_allowed": snap.switching_allowed,
            "decision": _decision_json(snap),
            "relay": None
            if snap.relay is None
            else {
                "on": snap.relay.output,
                "power_watts": snap.relay.apower,
                "source": snap.relay.source,
                "errors": list(snap.relay.errors),
                "read_at": _iso(snap.relay_read_at),
            },
            "relay_error": snap.relay_error,
            "override": override.to_json() if active else None,
            "prices": {
                "usable": prices_usable(snap, tz),
                "read_at": _iso(snap.price_success_at),
                "error": snap.price_error,
                "known_until": snap.book.slots[-1].end.isoformat() if snap.book.slots else None,
            },
            "settings": snap.settings.to_json(),
            "state_saved": snap.store_error is None,
            "csrf_token": csrf,
        }

    @app.get("/api/v1/status")
    def api_status():
        return jsonify(status_json(controller.snapshot()))

    @app.get("/api/v1/plan")
    def api_plan():
        which = request.args.get("day", "today")
        if which not in ("today", "tomorrow"):
            abort(400, "day must be today or tomorrow.")
        snap = controller.snapshot()
        now = dt.datetime.fromtimestamp(snap.now, dt.UTC)
        day = now.astimezone(tz).date() + dt.timedelta(days=which == "tomorrow")
        (plan,) = plan_days(
            snap.settings, snap.book, now, snap.current, snap.override, tz, [day], snap.last_hot
        )
        lines = plan.lines
        return jsonify(
            day=day.isoformat(),
            complete=plan.complete,
            average=lines.average if lines else None,
            on_line=lines.on_line if lines else None,
            off_line=lines.off_line if lines else None,
            morning_line=lines.morning_line if lines else None,
            slots=[
                {
                    "start": p.slot.start.astimezone(tz).isoformat(),
                    "end": p.slot.end.astimezone(tz).isoformat(),
                    "price": p.slot.total,
                    "target": p.target,
                    "rule": p.rule,
                    "reason": p.reason,
                    "past": p.past,
                }
                for p in plan.slots
            ],
        )

    @app.get("/api/v1/settings")
    def api_settings():
        return jsonify(controller.snapshot().settings.to_json())

    @app.put("/api/v1/settings")
    def api_settings_put():
        data = _json_body()
        if not isinstance(data, dict):
            abort(400, "Send a JSON object with the settings to change.")
        snap = controller.snapshot()
        confirmed = data.pop("confirm_live", False) is True
        errors = _mode_errors(
            data.get("mode"), snap.settings.mode, snap.switching_allowed, confirmed
        )
        if not errors:
            before = snap.counters.evaluations
            new, errors = controller.update_settings(data, _by())
            if not errors:
                if new != snap.settings:
                    settle(before)
                return jsonify(new.to_json())
        return jsonify(errors=errors), 422

    @app.post("/api/v1/override")
    def api_override():
        data = _json_body()
        if not isinstance(data, dict):
            abort(400, 'Send {"action": "on" or "off", "until": "18:00" or an ISO time}.')
        snap = controller.snapshot()
        if data.get("action") not in OVERRIDE_ACTIONS:
            return jsonify(errors={"action": "Must be on or off."}), 422
        if not snap.switching_allowed:
            return jsonify(errors={"action": SWITCHING_OFF}), 422
        now = dt.datetime.fromtimestamp(controller.clock(), dt.UTC)
        try:
            until = parse_until(data.get("until"), now, tz)
        except ValueError as exc:
            return jsonify(errors={"until": str(exc)}), 422
        try:
            override = controller.set_override(data["action"], until, _by())
        except OverrideLocked as exc:
            held = VACATION_RUNNING if exc.held.kind == "vacation" else HEAT_UP_RUNNING
            return jsonify(errors={"action": held}), 409
        settle(snap.counters.evaluations)
        return jsonify(override.to_json())

    @app.post("/api/v1/vacation")
    def api_vacation():
        data = _json_body()
        if not isinstance(data, dict):
            abort(400, 'Send {"until": an ISO time, "confirm": true}.')
        snap = controller.snapshot()
        if not snap.switching_allowed:
            return jsonify(errors={"until": SWITCHING_OFF}), 422
        # The same confirmation as the page asks for, here as an explicit true.
        if data.get("confirm") is not True:
            return jsonify(errors={"confirm": CONFIRM_VACATION}), 422
        now = dt.datetime.fromtimestamp(controller.clock(), dt.UTC)
        try:
            until = parse_vacation_end(data.get("until"), now, tz)
        except ValueError as exc:
            return jsonify(errors={"until": str(exc)}), 422
        try:
            override = controller.set_override("off", until, _by(), kind="vacation")
        except OverrideLocked as exc:
            held = VACATION_RUNNING if exc.held.kind == "vacation" else HEAT_UP_RUNNING
            return jsonify(errors={"until": held}), 409
        settle(snap.counters.evaluations)
        return jsonify(override.to_json())

    @app.delete("/api/v1/override")
    def api_override_delete():
        snap = controller.snapshot()
        try:
            cleared = controller.clear_override(_by())
        except OverrideLocked:
            return jsonify(cleared=False, error=HEAT_UP_RUNNING), 409
        if cleared:
            settle(snap.counters.evaluations)
        return jsonify(cleared=cleared)

    @app.get("/api/v1/decisions")
    def api_decisions():
        try:
            limit = int(request.args.get("limit", "100"))
        except ValueError:
            abort(400, "limit must be a whole number.")
        limit = max(1, min(limit, MAX_DECISIONS))
        entries = controller.snapshot().entries[-limit:]
        return jsonify(entries=list(reversed(entries)))

    # -- operations ------------------------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return Response("ok\n", mimetype="text/plain")

    @app.get("/readyz")
    def readyz():
        snap = controller.snapshot()
        problems = []
        if not prices_usable(snap, tz):
            problems.append("no usable price for this quarter")
        if snap.last_evaluation_at is None or snap.now - snap.last_evaluation_at > 1200:
            problems.append("no evaluation in the last 20 minutes")
        if problems:
            return Response(f"not ready: {'; '.join(problems)}\n", 503, mimetype="text/plain")
        return Response("ready\n", mimetype="text/plain")

    @app.get("/metrics")
    def metrics():
        if registry is None:
            abort(404)
        return Response(generate_latest(registry), mimetype=None, content_type=CONTENT_TYPE_LATEST)

    return app


def _iso(moment: float | None) -> str | None:
    if moment is None:
        return None
    return dt.datetime.fromtimestamp(moment, dt.UTC).isoformat(timespec="seconds")
