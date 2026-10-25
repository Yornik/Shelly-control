"""The service may switch the relay and read it, nothing else; these keep that true."""

from __future__ import annotations

import shutil

import pytest
import requests
from conftest import PASSWORD

from scripts.check_allowlist import (
    EXPECTED_METHODS,
    PACKAGE,
    check_package,
    check_source,
    check_templates,
)
from shelly_control.shelly import (
    ALLOWED_METHODS,
    WRITE_METHODS,
    AllowlistViolation,
    ShellyClient,
    ShellyError,
)

# Every request path the client can produce. Pinned here as well as in the guard.
EXPECTED_PATHS = {
    "/rpc/Shelly.GetDeviceInfo",
    "/rpc/Switch.GetStatus?id=0",
    "/rpc/Switch.Set?id=0&on=true",
    "/rpc/Switch.Set?id=0&on=false",
}


def test_package_passes_the_guard():
    assert check_package() == []


def test_allowlist_is_pinned():
    assert dict(ALLOWED_METHODS) == EXPECTED_METHODS
    assert WRITE_METHODS == {"Switch.Set"}


def test_allowlist_cannot_be_changed_at_runtime():
    with pytest.raises(TypeError):
        ALLOWED_METHODS["Switch.Toggle"] = frozenset()  # type: ignore[index]


def test_every_request_is_a_get_to_a_pinned_path(fake_shelly):
    client = ShellyClient(fake_shelly.url)
    client.get_device_info()
    client.get_switch_status(0)
    client.set_output(0, True)
    client.set_output(0, False)
    assert {method for method, _, _ in fake_shelly.requests} == {"GET"}
    assert {path for _, path, _ in fake_shelly.requests} == EXPECTED_PATHS


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("Switch.Toggle", {"id": 0}),
        ("Switch.SetConfig", {"id": 0}),
        ("Shelly.Reboot", {}),
        ("Shelly.GetStatus", {}),
        ("Shelly.GetConfig", {}),
        ("Sys.SetConfig", {}),
        ("Script.Start", {"id": 1}),
        ("HTTP.GET", {}),
        ("Switch.ResetCounters", {"id": 0}),
        # Allowlisted methods with a parameter that is not.
        ("Switch.Set", {"id": 0, "on": True, "toggle_after": 60}),
        ("Switch.Set", {"id": 0}),
        ("Switch.Set", {"id": 0, "on": 1}),
        ("Switch.Set", {"id": True, "on": True}),
        ("Switch.Set", {"id": 99, "on": True}),
        ("Switch.GetStatus", {"id": "0&on=true"}),
        ("Shelly.GetDeviceInfo", {"ident": 1}),
    ],
)
def test_client_refuses_before_sending(fake_shelly, method, params):
    client = ShellyClient(fake_shelly.url)
    with pytest.raises(AllowlistViolation):
        client._call(method, **params)
    assert fake_shelly.requests == []


@pytest.mark.parametrize(
    ("http_method", "path"),
    [
        ("POST", "/rpc/Switch.Set"),
        ("POST", "/rpc"),
        ("GET", "/rpc/Switch.Toggle?id=0"),
        ("GET", "/rpc/Shelly.GetStatus"),
        ("GET", "/relay/0?turn=on"),
        ("GET", "/relay/0?turn=on&timer=60"),
        ("GET", "/%5Crelay%5C0/rpc/Switch.GetStatus?id=0"),
        ("GET", "/rpc/Switch.Set?id=0&on=true&toggle_after=60"),
        ("GET", "/rpc/Switch.Set?id=0&on=1"),
        ("GET", "/rpc/Switch.Set?id=true&on=true"),
        ("GET", "/rpc/Switch.Set?id=0&id=1&on=true"),
        ("GET", "/rpc/Switch.Set?on=true"),
        ("GET", "/rpc/Switch.GetStatus?id=0#x"),
        ("GET", "/rpc/Shelly.GetDeviceInfo?ident=true"),
        ("HEAD", "/rpc/Switch.GetStatus?id=0"),
    ],
)
def test_the_session_itself_refuses_anything_but_an_allowlisted_get(fake_shelly, http_method, path):
    # Code that reaches the session without _call still meets the check that
    # runs as each request is sent.
    client = ShellyClient(fake_shelly.url)
    with pytest.raises(AllowlistViolation):
        client.session.request(http_method, fake_shelly.url + path, timeout=2)
    prepared = client.session.prepare_request(requests.Request(http_method, fake_shelly.url + path))
    with pytest.raises(AllowlistViolation):
        client.session.send(prepared, timeout=2)
    assert fake_shelly.requests == []


def test_the_session_refuses_another_host(fake_shelly):
    client = ShellyClient(fake_shelly.url)
    other = fake_shelly.url.replace("127.0.0.1", "localhost")
    with pytest.raises(AllowlistViolation):
        client.session.get(f"{other}/rpc/Switch.GetStatus?id=0", timeout=2)
    assert fake_shelly.requests == []


def test_the_digest_retry_passes_the_send_time_check(fake_shelly):
    fake_shelly.password = PASSWORD
    ShellyClient(fake_shelly.url, PASSWORD).set_output(0, True)
    assert [ok for _, _, ok in fake_shelly.requests] == [False, True]
    assert fake_shelly.output is True


def test_method_names_are_matched_like_the_device_does(fake_shelly):
    client = ShellyClient(fake_shelly.url)
    assert client._call("switch.getstatus", id=0)
    assert fake_shelly.requests[0][1] == "/rpc/Switch.GetStatus?id=0"


def test_violation_is_not_swallowed_as_a_shelly_error():
    assert not issubclass(AllowlistViolation, ShellyError)


# -- the CI guard catches each of these ---------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        'M = "Switch.Toggle"',
        'M = "switch.toggle"',
        'M = "Switch.SetConfig"',
        'M = "Sys.SetConfig"',
        'M = "Shelly.Reboot"',
        'M = "Shelly.FactoryReset"',
        'M = "Switch.ResetCounters"',
        'M = "Script.Start"',
        'M = "Webhook.Create"',
        'M = "HTTP.GET"',
        'M = "Shelly.GetConfig"',
        'M = "Shelly.GetStatus"',
        'M = "KVS.Get"',
        'M = "Shelly.SomethingNew"',
        'P = {"toggle_after": 60}',
        'URL = "/relay/0?turn=on"',
        'URL = "/ota?url=x"',
        'URL = "/some/new/path"',
        'U = "ws://device/rpc"',
        "def toggle_relay(): ...",
        # The Tibber API itself, and the exporter's other periods.
        'U = "https://api.tibber.com/v1-beta/gql"',
        'Q = "{viewer {homes}}"; K = "graphql"',
        'H = {"Authorization": "Bearer x"}',
        'U = f"{base}/prices?period=now"',
        "tibber_token = 1",
        # HTTP calls around the two clients.
        "self.session.post(u, allow_redirects=False)",
        "self.session.get(u)",
        "requests.get(u)",
        "requests.post(u)",
        "requests.Session().get(u, allow_redirects=False)",
        "s = client.session\ns.get(u, allow_redirects=False)",
        "class ShellyClient:\n    def _call(self):\n        return self.session.get(u)",
        "class ShellyClient:\n    def other(self):\n"
        "        return self.session.get(u, allow_redirects=False)",
        "self.session.send(prepared)",
        "self.session.prepare_request(r)",
        "client.session.mount('http://', HTTPAdapter())",
        "from requests import Session",
        "from requests.api import get",
        "import importlib",
        'eval("1")',
        'getattr(self.session, "post")',
        "f = client.__doc__",
        "import socket",
        "import subprocess",
        "os.system('curl x')",
        "from urllib.request import urlopen",
    ],
)
def test_guard_catches_write_side_code(source):
    assert check_source(source, "probe.py"), source


@pytest.mark.parametrize(
    "source",
    [
        # Only Controller._apply may switch.
        "def handler(controller):\n    controller.shelly.set_output(0, True)",
        "class Controller:\n    def evaluate(self):\n        self.shelly.set_output(0, True)",
        "f = set_output",
        # Switch.Set is spelled out in shelly.py only.
        'M = "Switch.Set"',
    ],
)
def test_guard_keeps_switching_in_one_place(source):
    assert check_source(source, "web.py"), source


def test_guard_allows_the_one_switch_call_site():
    source = "class Controller:\n    def _apply(self):\n        self.shelly.set_output(0, True)\n"
    assert check_source(source, "controller.py") == []


def test_only_the_controller_imports_the_device_client():
    assert check_source("from .shelly import ShellyClient", "web.py")
    assert check_source("from .shelly import ShellyClient", "controller.py") == []


def test_requests_is_only_imported_by_the_two_clients():
    assert check_source("import requests", "web.py")
    assert check_source("import requests", "prices.py") == []


def test_routes_are_allowed_in_web_py_only():
    routes = '@app.post("/settings")\ndef f(): ...\n@app.put("/api/v1/settings")\ndef g(): ...\n'
    assert check_source(routes, "web.py") == []
    assert check_source(routes, "controller.py")


def test_guard_ignores_comments_and_docstrings():
    source = (
        '"""Never call Switch.Toggle or /relay/0?turn=on."""\n'
        "# never call Switch.Set, Shelly.Reboot or /relay/0?turn=on\nx = 1\n"
    )
    assert check_source(source, "probe.py") == []


@pytest.mark.parametrize(
    "snippet",
    [
        "{{ reason|safe }}",
        "{{ reason | safe }}",
        "{% autoescape false %}x{% endautoescape %}",
        '<p style="color:red">x</p>',
        "<style>p{}</style>",
        '<button onclick="go()">x</button>',
        "<script>alert(1)</script>",
        '<link rel="stylesheet" href="https://fonts.example/x.css">',
        '<form method="post" action="/relay/0">',
        '<a href="javascript:void(0)">x</a>',
    ],
)
def test_guard_checks_the_templates(tmp_path, snippet):
    package = tmp_path / "shelly_control"
    shutil.copytree(PACKAGE / "templates", package / "templates")
    shutil.copytree(PACKAGE / "static", package / "static")
    assert check_templates(package) == []
    (package / "templates" / "probe.html").write_text(snippet)
    assert check_templates(package), snippet


@pytest.mark.parametrize("snippet", ["el.innerHTML = x;", "fetch('/api/v1/override')", "eval(x)"])
def test_guard_checks_the_page_script(tmp_path, snippet):
    package = tmp_path / "shelly_control"
    shutil.copytree(PACKAGE / "static", package / "static")
    (package / "templates").mkdir()
    (package / "static" / "probe.js").write_text(snippet)
    assert check_templates(package)


def test_guard_counts_http_call_sites(tmp_path):
    package = tmp_path / "shelly_control"
    shutil.copytree(PACKAGE, package, ignore=shutil.ignore_patterns("__pycache__"))
    prices = package / "prices.py"
    line = "deadline = time.monotonic() + 2 * self.timeout\n"
    second = line + "        self.session.get(self._url, allow_redirects=False)\n"
    assert prices.read_text().count(line) == 1
    prices.write_text(prices.read_text().replace(line, second))
    problems = check_package(package)
    assert any("exactly one HTTP call site" in p for p in problems)
