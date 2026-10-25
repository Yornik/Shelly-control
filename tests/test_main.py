"""Configuration from the environment, and the wiring of the service."""

from __future__ import annotations

import datetime as dt

import pytest
from fake_exporter import HOME_ID

from shelly_control.__main__ import build
from shelly_control.config import DEFAULT_EXPORTER_URL, Config
from shelly_control.rules import Settings

BASE = {"SHELLY_URL": "http://192.168.1.25", "TIBBER_HOME_ID": HOME_ID}


def config(**env) -> Config:
    c = Config({**BASE, **env})
    c.validate()
    return c


def test_defaults():
    c = config()
    assert c.shelly_url == "http://192.168.1.25"
    assert c.shelly_password is None
    assert c.exporter_url == DEFAULT_EXPORTER_URL
    assert c.listen_port == 9123
    assert c.allowed_hosts == ["localhost:9123", "127.0.0.1:9123"]
    assert c.allow_switching is False
    assert c.state_dir == "/data"
    assert str(c.tz) == "Europe/Amsterdam"
    assert c.defaults == Settings()


def test_defaults_for_the_settings_come_from_the_environment():
    c = config(
        DEFAULT_MODE="live",
        DEFAULT_ON_FACTOR="1.2",
        DEFAULT_OFF_FACTOR="1.5",
        DEFAULT_MORNING_FACTOR="1.5",
        DEFAULT_MORNING_ENABLED="false",
        DEFAULT_MORNING_START="04:00",
    )
    assert c.defaults == Settings(
        mode="live",
        on_factor=1.2,
        off_factor=1.5,
        morning_factor=1.5,
        morning_enabled=False,
        morning_start=dt.time(4),
    )


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"SHELLY_URL": ""}, "SHELLY_URL"),
        ({"SHELLY_URL": "http://192.168.1.25/relay/0?turn=on"}, "SHELLY_URL"),
        ({"TIBBER_HOME_ID": ""}, "TIBBER_HOME_ID"),
        ({"TIBBER_HOME_ID": "my-home"}, "TIBBER_HOME_ID must be a UUID"),
        ({"TIBBER_EXPORTER_URL": "http://x/homes"}, "TIBBER_EXPORTER_URL"),
        ({"TIMEZONE": "Mars/Olympus"}, "TIMEZONE"),
        ({"ALLOWED_HOSTS": "https://boiler.example.ts.net"}, "ALLOWED_HOSTS"),
        ({"ALLOWED_HOSTS": " , "}, "ALLOWED_HOSTS"),
        ({"ALLOW_SWITCHING": "maybe"}, "ALLOW_SWITCHING"),
        ({"SHELLY_TIMEOUT_SECONDS": "0"}, "SHELLY_TIMEOUT_SECONDS"),
        ({"SHELLY_TIMEOUT_SECONDS": "x"}, "SHELLY_TIMEOUT_SECONDS"),
        ({"LISTEN_PORT": "70000"}, "LISTEN_PORT"),
        ({"MIN_SWITCH_INTERVAL_SECONDS": "1"}, "MIN_SWITCH_INTERVAL_SECONDS"),
        ({"DEFAULT_ON_FACTOR": "9"}, "DEFAULT_ON_FACTOR"),
        ({"DEFAULT_MODE": "on"}, "DEFAULT_MODE"),
    ],
)
def test_bad_configuration_stops_with_a_message(env, message):
    with pytest.raises(SystemExit, match=message):
        config(**env)


def test_a_url_with_a_password_is_not_echoed():
    with pytest.raises(SystemExit) as exc:
        config(SHELLY_URL="http://admin:secret@192.168.1.25")
    assert "secret" not in str(exc.value)


def test_allowed_hosts_are_lower_case():
    c = config(ALLOWED_HOSTS="Boiler.Example.ts.net, localhost:9123")
    assert c.allowed_hosts == ["boiler.example.ts.net", "localhost:9123"]


def test_build_wires_the_controller_and_the_app(tmp_path):
    c = config(STATE_DIR=str(tmp_path), ALLOW_SWITCHING="true")
    controller, app = build(c)
    assert controller.allow_switching is True
    assert controller.snapshot().settings == Settings()
    client = app.test_client()
    assert client.get("/healthz").status_code == 200
    assert client.get("/", headers={"Host": "localhost:9123"}).status_code == 200
