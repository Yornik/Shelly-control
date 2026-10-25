"""Runtime configuration, from environment variables only."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import fields
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .prices import UUID
from .rules import Settings, parse_settings
from .transport import normalize_base_url

DEFAULT_EXPORTER_URL = "http://tibber-exporter.monitoring.svc:8080"
DEFAULT_PORT = 9123

# DEFAULT_<NAME> sets the default of a setting until someone saves the
# settings on the page; then the saved value wins.
DEFAULT_PREFIX = "DEFAULT_"

_HOST = re.compile(r"(?:\[[0-9a-f:.]+\]|[a-z0-9.-]+)(?::[0-9]{1,5})?")
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off", "")


def _number(env: Mapping[str, str], name: str, default: str, kind=float):
    raw = env.get(name, default).strip() or default
    try:
        value = kind(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {raw!r}") from None
    if not math.isfinite(value):
        raise SystemExit(f"{name} must be a finite number")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise SystemExit(f"{name} must be true or false, got {raw!r}")


def defaults_from_env(env: Mapping[str, str]) -> Settings:
    """Settings() with every DEFAULT_<NAME> variable applied, validated like the page."""
    raw: dict[str, object] = {}
    for f in fields(Settings):
        name = DEFAULT_PREFIX + f.name.upper()
        if name not in env:
            continue
        value: object = env[name].strip()
        if f.name.endswith("_enabled"):
            value = _bool(env, name, False)
        raw[f.name] = value
    settings, errors = parse_settings(raw, Settings())
    if errors:
        problems = "; ".join(f"{DEFAULT_PREFIX}{k.upper()}: {v}" for k, v in errors.items())
        raise SystemExit(f"invalid default setting: {problems}")
    return settings


class Config:
    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        self.shelly_url = env.get("SHELLY_URL", "").strip()
        self.shelly_password = env.get("SHELLY_PASSWORD", "") or None
        self.switch_id = _number(env, "SHELLY_SWITCH_ID", "0", int)
        self.shelly_timeout = _number(env, "SHELLY_TIMEOUT_SECONDS", "5")
        self.exporter_url = env.get("TIBBER_EXPORTER_URL", "").strip() or DEFAULT_EXPORTER_URL
        self.home_id = env.get("TIBBER_HOME_ID", "").strip()
        self.exporter_timeout = _number(env, "TIBBER_EXPORTER_TIMEOUT_SECONDS", "5")
        self.timezone = env.get("TIMEZONE", "").strip() or "Europe/Amsterdam"
        self.state_dir = env.get("STATE_DIR", "").strip() or "/data"
        self.listen_port = _number(env, "LISTEN_PORT", str(DEFAULT_PORT), int)
        self.allowed_hosts = [
            h.strip().lower()
            for h in env.get(
                "ALLOWED_HOSTS", f"localhost:{self.listen_port},127.0.0.1:{self.listen_port}"
            ).split(",")
            if h.strip()
        ]
        self.allow_switching = _bool(env, "ALLOW_SWITCHING", False)
        self.display_name = env.get("DISPLAY_NAME", "").strip() or "Boiler"
        self.price_refresh = _number(env, "PRICE_REFRESH_SECONDS", "300")
        self.evaluation_delay = _number(env, "EVALUATION_DELAY_SECONDS", "5")
        self.min_switch_interval = _number(env, "MIN_SWITCH_INTERVAL_SECONDS", "60")
        self.decision_log_size = _number(env, "DECISION_LOG_SIZE", "700", int)
        self.defaults = defaults_from_env(env)
        self.tz: ZoneInfo | None = None

    def validate(self) -> None:
        if not self.shelly_url:
            raise SystemExit(
                "missing required environment variable: SHELLY_URL "
                "(for example http://<device-address>)"
            )
        try:
            self.shelly_url = normalize_base_url(self.shelly_url)
        except ValueError as exc:
            # Not echoed: a URL with a password in it would end up in the log.
            raise SystemExit(f"SHELLY_URL {exc}") from None
        try:
            self.exporter_url = normalize_base_url(self.exporter_url)
        except ValueError as exc:
            raise SystemExit(f"TIBBER_EXPORTER_URL {exc}") from None
        if not self.home_id:
            raise SystemExit(
                "missing required environment variable: TIBBER_HOME_ID (the home's id as "
                "the tibber-exporter serves it, a UUID)"
            )
        if not UUID.fullmatch(self.home_id):
            raise SystemExit("TIBBER_HOME_ID must be a UUID (8-4-4-4-12 hexadecimal digits)")
        try:
            self.tz = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise SystemExit(f"TIMEZONE {self.timezone!r} is not a known time zone") from None
        if not 0 <= self.switch_id <= 15:
            raise SystemExit("SHELLY_SWITCH_ID must be from 0 to 15")
        for name, value, low, high in (
            ("SHELLY_TIMEOUT_SECONDS", self.shelly_timeout, 0.5, 30),
            ("TIBBER_EXPORTER_TIMEOUT_SECONDS", self.exporter_timeout, 0.5, 30),
            ("PRICE_REFRESH_SECONDS", self.price_refresh, 60, 3600),
            ("EVALUATION_DELAY_SECONDS", self.evaluation_delay, 0, 120),
            ("MIN_SWITCH_INTERVAL_SECONDS", self.min_switch_interval, 10, 900),
            ("DECISION_LOG_SIZE", self.decision_log_size, 10, 5000),
        ):
            if not low <= value <= high:
                raise SystemExit(f"{name} must be from {low:g} to {high:g}")
        if not 0 < self.listen_port < 65536:
            raise SystemExit("LISTEN_PORT must be between 1 and 65535")
        if not self.allowed_hosts:
            raise SystemExit("ALLOWED_HOSTS must name at least one host")
        for host in self.allowed_hosts:
            if not _HOST.fullmatch(host):
                raise SystemExit(
                    f"ALLOWED_HOSTS entry {host[:60]!r} must be a host name with an optional "
                    "port, such as boiler.example.ts.net, without http://"
                )
        if len(self.display_name) > 40:
            raise SystemExit("DISPLAY_NAME must be at most 40 characters")
