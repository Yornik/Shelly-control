"""Client for the local RPC API of a Shelly Gen2 relay: two reads and Switch.Set.

Gen2 devices (Plus, Pro, Mini) answer `GET /rpc/<Method>?<params>` with the
bare result as JSON. With authentication on, every call except
Shelly.GetDeviceInfo needs SHA-256 digest auth as user `admin`, with the
device id as realm (shelly-api-docs.shelly.cloud, General/Authentication).
requests' HTTPDigestAuth does that; Python's urllib cannot (MD5 and SHA-1 only).

Three methods exist here: the device's identity, one switch's status, and
Switch.Set to turn that switch on or off. Nothing in this module can change a
setting, start a timer, run a script, update firmware or restart the device.
The Plus 1PM is frozen at firmware 1.7.x, so no 2.0 parameter (such as `tag`)
is ever sent.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import parse_qsl, urlsplit

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPDigestAuth

from .transport import (
    READ_CHUNK,
    BodyTimeout,
    BodyTooLarge,
    normalize_base_url,
    origin_of,
    read_bounded,
)

# Gen2 has one user, and it is always called admin.
DIGEST_USER = "admin"

# Every RPC method this client may call, with the parameters each may carry.
# _call refuses anything else before a byte leaves the process, the session
# checks every request again as it is sent, and CI
# (scripts/check_allowlist.py) fails if this changes.
ALLOWED_METHODS = MappingProxyType(
    {
        "Shelly.GetDeviceInfo": frozenset(),
        "Switch.GetStatus": frozenset({"id"}),
        "Switch.Set": frozenset({"id", "on"}),
    }
)

# The one method that changes the device. Only Controller._apply reaches it.
WRITE_METHODS = frozenset({"Switch.Set"})

# The device treats method names case-insensitively, so the allowlist is
# looked up the same way and the canonical spelling is what goes on the wire.
_CANONICAL = {method.casefold(): method for method in ALLOWED_METHODS}
_WIRE_PATHS = {f"/rpc/{method}": method for method in ALLOWED_METHODS}

# A switch status is under 1 KiB. A bigger reply is not from a Shelly.
MAX_REPLY_BYTES = 64 * 1024

# Switch ids are small: 0 on a Plus 1PM, up to 3 on a Pro 4PM.
MAX_SWITCH_ID = 15


class ShellyError(Exception):
    """A call failed or its reply made no sense. The controller retries later."""

    # Bounded label value for the error counters.
    reason = "invalid_response"


class ShellyTimeout(ShellyError):
    reason = "timeout"


class ShellyUnreachable(ShellyError):
    reason = "connection"


class ShellyHTTPError(ShellyError):
    reason = "http"


class ShellyAuthError(ShellyError):
    """HTTP 401: no password, or the wrong one. Retrying at once will not help."""

    reason = "auth"


SHELLY_ERROR_REASONS = ("timeout", "connection", "http", "auth", "invalid_response")


class AllowlistViolation(RuntimeError):
    """Code tried to call a method or send a request outside ALLOWED_METHODS.

    Deliberately not a ShellyError: the controller retries those, while this
    one is a bug that has to stop the process.
    """


@dataclass(frozen=True)
class DeviceInfo:
    id: str
    model: str
    gen: str
    version: str
    auth_enabled: bool

    @classmethod
    def from_reply(cls, data: dict) -> DeviceInfo:
        device_id = data.get("id")
        if not isinstance(device_id, str) or not device_id:
            raise ShellyError("the device info carried no device id")

        def text(key: str) -> str:
            value = data.get(key)
            return "" if value is None else str(value)[:64]

        return cls(
            id=device_id[:64],
            model=text("model"),
            gen=text("gen"),
            version=text("ver"),
            auth_enabled=data.get("auth_en") is True,
        )


# Protection errors the device documents for a switch. Anything else is "other".
SWITCH_ERRORS = ("overtemp", "overpower", "overvoltage", "undervoltage")


@dataclass(frozen=True)
class SwitchStatus:
    """What a decision needs from Switch.GetStatus."""

    output: bool
    # What last changed the output, as the device names it (init, http, timer, ...).
    source: str | None
    # Active power in W, for the page: "on but about 0 W" means the boiler's
    # own thermostat is satisfied.
    apower: float | None
    errors: tuple[str, ...]
    timer_running: bool

    @classmethod
    def from_reply(cls, data: dict) -> SwitchStatus:
        output = data.get("output")
        if not isinstance(output, bool):
            raise ShellyError("the switch status has no output state")
        source = data.get("source")
        apower = data.get("apower")
        raw_errors = data.get("errors")
        errors = (
            tuple(
                str(e) if str(e) in SWITCH_ERRORS else "other"
                for e in raw_errors
                if isinstance(e, str)
            )
            if isinstance(raw_errors, list)
            else ()
        )
        return cls(
            output=output,
            source=source[:32] if isinstance(source, str) else None,
            apower=float(apower)
            if isinstance(apower, int | float) and not isinstance(apower, bool)
            else None,
            errors=errors,
            timer_running="timer_started_at" in data,
        )


def _wire_value(value: object) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _check_wire(method: str | None, url: str | None, origin: tuple[str, str]) -> None:
    """Raise AllowlistViolation unless this is a GET of an allowlisted call on the device."""
    parts = urlsplit(url or "")
    canonical = _WIRE_PATHS.get(parts.path)
    query = parse_qsl(parts.query, keep_blank_values=True)
    allowed = ALLOWED_METHODS[canonical] if canonical else frozenset()
    keys = [key for key, _ in query]
    if (
        method != "GET"
        or canonical is None
        or (parts.scheme, parts.netloc.lower()) != origin
        or parts.fragment
        or len(set(keys)) != len(keys)
        or set(keys) != allowed
        or any(not _valid_wire_param(key, value) for key, value in query)
    ):
        raise AllowlistViolation(f"refusing to send {method} {parts.path[:60]}: not allowlisted")


def _valid_wire_param(key: str, value: str) -> bool:
    if key == "id":
        return value.isascii() and value.isdigit() and int(value) <= MAX_SWITCH_ID
    if key == "on":
        return value in ("true", "false")
    return False


class _AllowlistAdapter(HTTPAdapter):
    """Checks each request as it is sent, after requests has built the final URL.

    The second line of the runtime check, after the one in _call. It also
    covers the request that answers a digest challenge, and anything that
    reaches the session without going through _call.
    """

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self._origin = origin_of(base_url)

    def send(self, request, *args, **kwargs):
        _check_wire(request.method, request.url, self._origin)
        return super().send(request, *args, **kwargs)


class ShellyClient:
    def __init__(self, base_url: str, password: str | None = None, *, timeout: float = 5.0) -> None:
        self.base_url = normalize_base_url(base_url)
        self.timeout = timeout
        self.has_password = bool(password)
        self.session = requests.Session()
        # Replaces requests' default adapters, so every request this session
        # sends, whichever code sends it, is checked against the allowlist.
        adapter = _AllowlistAdapter(self.base_url)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        # The device is on the LAN: no proxy from the environment, and no
        # credentials from ~/.netrc.
        self.session.trust_env = False
        if password:
            # requests answers the device's 401 challenge itself, with the
            # algorithm the challenge names (SHA-256 on Gen2).
            self.session.auth = HTTPDigestAuth(DIGEST_USER, password)

    def _call(self, method: str, **params: int | bool) -> dict:
        canonical = _CANONICAL.get(method.casefold())
        if canonical is None:
            raise AllowlistViolation(f"refusing to call {method}: not an allowlisted method")
        if set(params) != ALLOWED_METHODS[canonical]:
            raise AllowlistViolation(
                f"refusing to call {canonical} with parameters {sorted(params)}"
            )
        for key, value in params.items():
            valid = (
                type(value) is int and 0 <= value <= MAX_SWITCH_ID
                if key == "id"
                else type(value) is bool
            )
            if not valid:
                raise AllowlistViolation(f"refusing to call {canonical}: bad value for {key}")
        # Each try of a digest request is two round trips (challenge, answer),
        # each allowed the timeout; the body read shares that budget.
        deadline = time.monotonic() + 2 * self.timeout
        try:
            resp = self.session.get(
                f"{self.base_url}/rpc/{canonical}",
                params={key: _wire_value(value) for key, value in params.items()} or None,
                timeout=self.timeout,
                # A redirect could point anywhere on the device, so none is followed.
                allow_redirects=False,
                stream=True,
            )
        except requests.Timeout:
            raise ShellyTimeout(f"{canonical} timed out after {self.timeout:g} s") from None
        except requests.RequestException as exc:
            raise ShellyUnreachable(f"{canonical} failed ({exc.__class__.__name__})") from None
        with resp:
            if resp.status_code == 401:
                if self.has_password:
                    raise ShellyAuthError("the device refused the password (HTTP 401)")
                raise ShellyAuthError("the device requires a password (HTTP 401)")
            try:
                body = read_bounded(resp.iter_content(READ_CHUNK), MAX_REPLY_BYTES, deadline)
            except BodyTooLarge:
                raise ShellyError(f"{canonical} sent more than {MAX_REPLY_BYTES} bytes") from None
            except BodyTimeout:
                raise ShellyTimeout(f"{canonical}'s reply took too long") from None
            except requests.RequestException as exc:
                raise ShellyUnreachable(
                    f"{canonical}'s reply broke off ({exc.__class__.__name__})"
                ) from None
            status = resp.status_code
        try:
            data = json.loads(body) if body else None
        except (ValueError, RecursionError):
            # RecursionError: JSON nested deeper than the parser goes. Not a reply either.
            data = None
        if status != 200:
            # Gen2 answers a failed call with {"code": -103, "message": "..."}.
            detail = ""
            if isinstance(data, dict) and isinstance(data.get("code"), int):
                detail = f" (RPC error {data['code']}: {str(data.get('message', ''))[:80]})"
            raise ShellyHTTPError(f"{canonical} answered HTTP {status}{detail}")
        if not isinstance(data, dict):
            raise ShellyError(f"{canonical} returned a reply that is not a JSON object")
        return data

    # -- the three calls -------------------------------------------------------------------------

    def get_device_info(self) -> DeviceInfo:
        """Identity and firmware. Never needs a password, even with auth on."""
        return DeviceInfo.from_reply(self._call("Shelly.GetDeviceInfo"))

    def get_switch_status(self, switch_id: int) -> SwitchStatus:
        return SwitchStatus.from_reply(self._call("Switch.GetStatus", id=switch_id))

    def set_output(self, switch_id: int, on: bool) -> bool | None:
        """Turn one switch on or off. Returns whether it was on before, if the device says."""
        reply = self._call("Switch.Set", id=switch_id, on=on)
        was_on = reply.get("was_on")
        return was_on if isinstance(was_on, bool) else None
