"""The Shelly client against a fake Gen2 relay, with and without digest auth."""

from __future__ import annotations

import pytest
from conftest import PASSWORD

from shelly_control.shelly import (
    ShellyAuthError,
    ShellyClient,
    ShellyError,
    ShellyHTTPError,
    ShellyTimeout,
    ShellyUnreachable,
)
from shelly_control.transport import normalize_base_url


def test_device_info(fake_shelly):
    info = ShellyClient(fake_shelly.url).get_device_info()
    assert info.id == "shellyplus1pm-0123456789ab"
    assert info.version == "1.7.5"
    assert info.auth_enabled is True


def test_switch_status(fake_shelly):
    fake_shelly.output = True
    fake_shelly.apower = 1052.5
    status = ShellyClient(fake_shelly.url).get_switch_status(0)
    assert status.output is True
    assert status.apower == 1052.5
    assert status.source == "init"
    assert status.errors == ()
    assert status.timer_running is False


def test_protection_errors_are_read_and_bounded(fake_shelly):
    fake_shelly.errors = ["overtemp", "something_new"]
    status = ShellyClient(fake_shelly.url).get_switch_status(0)
    assert status.errors == ("overtemp", "other")


@pytest.mark.parametrize("on", [True, False])
def test_set_output_switches_and_reports_the_previous_state(fake_shelly, on):
    fake_shelly.output = not on
    client = ShellyClient(fake_shelly.url)
    assert client.set_output(0, on) is (not on)
    assert fake_shelly.output is on
    assert fake_shelly.sets == [f"id=0&on={'true' if on else 'false'}"]


def test_digest_auth_answers_the_challenge(fake_shelly):
    fake_shelly.password = PASSWORD
    client = ShellyClient(fake_shelly.url, PASSWORD)
    client.get_switch_status(0)
    client.set_output(0, True)
    assert fake_shelly.output is True
    # Firmware 1.7.x challenges every request: 401 first, then the answer.
    assert [(path, ok) for _, path, ok in fake_shelly.requests] == [
        ("/rpc/Switch.GetStatus?id=0", False),
        ("/rpc/Switch.GetStatus?id=0", True),
        ("/rpc/Switch.Set?id=0&on=true", False),
        ("/rpc/Switch.Set?id=0&on=true", True),
    ]


def test_device_info_needs_no_password(fake_shelly):
    fake_shelly.password = PASSWORD
    assert ShellyClient(fake_shelly.url).get_device_info().id


def test_a_wrong_password(fake_shelly):
    fake_shelly.password = PASSWORD
    with pytest.raises(ShellyAuthError, match="refused the password"):
        ShellyClient(fake_shelly.url, "wrong").get_switch_status(0)
    assert fake_shelly.output is False


def test_a_missing_password(fake_shelly):
    fake_shelly.password = PASSWORD
    with pytest.raises(ShellyAuthError, match="requires a password"):
        ShellyClient(fake_shelly.url).set_output(0, True)
    assert fake_shelly.output is False


def test_an_rpc_error_frame(fake_shelly):
    fake_shelly.set_error = (500, -114, "Device busy")
    with pytest.raises(ShellyHTTPError, match=r"RPC error -114: Device busy"):
        ShellyClient(fake_shelly.url).set_output(0, True)


def test_an_unknown_switch_id(fake_shelly):
    with pytest.raises(ShellyHTTPError, match="-105"):
        ShellyClient(fake_shelly.url).get_switch_status(7)


@pytest.mark.parametrize(
    "body", [b"not json", b"[1, 2]", b'{"id": 0}', b"", b"[" * 20000 + b"]" * 20000]
)
def test_a_reply_that_is_not_a_switch_status(fake_shelly, body):
    fake_shelly.raw_body = body
    with pytest.raises(ShellyError):
        ShellyClient(fake_shelly.url).get_switch_status(0)


def test_a_redirect_is_not_followed(fake_shelly):
    fake_shelly.redirect_to = "/rpc/Switch.Set?id=0&on=true"
    with pytest.raises(ShellyHTTPError, match="302"):
        ShellyClient(fake_shelly.url).get_switch_status(0)
    assert len(fake_shelly.requests) == 1


def test_timeout(fake_shelly):
    fake_shelly.delay = 1.5
    with pytest.raises(ShellyTimeout):
        ShellyClient(fake_shelly.url, timeout=0.3).get_switch_status(0)


def test_connection_refused(closed_port_url):
    with pytest.raises(ShellyUnreachable):
        ShellyClient(closed_port_url, timeout=1).get_switch_status(0)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("192.168.1.25", "http://192.168.1.25"),
        ("http://192.168.1.25/", "http://192.168.1.25"),
        ("https://boiler.lan:8443", "https://boiler.lan:8443"),
        ("http://[fe80::1]:80", "http://[fe80::1]:80"),
    ],
)
def test_base_urls_that_are_accepted(raw, expected):
    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://192.168.1.25/relay/0?turn=on",
        "http://192.168.1.25?x=1",
        "http://admin:pw@192.168.1.25",
        "ftp://192.168.1.25",
        "http://",
        "http://host\\relay\\0",
        "http://host:99999",
    ],
)
def test_base_urls_that_are_refused(raw):
    with pytest.raises(ValueError):
        normalize_base_url(raw)
