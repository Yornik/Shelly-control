"""The page and the JSON API: forms, validation, the CSRF token, Host and Origin checks."""

from __future__ import annotations

import datetime as dt
import re

import pytest
from conftest import NORMAL_DAY, TZ, ts

from shelly_control.metrics import make_registry
from shelly_control.rules import Settings
from shelly_control.web import create_app

HOST = "boiler.example.ts.net"
TOKEN = "test-token-0123456789"
HEADERS = {"Host": HOST}
FORM = {
    "csrf": TOKEN,
    "on_factor": "1.10",
    "off_factor": "1.30",
    "morning_enabled": "on",
    "morning_start": "05:00",
    "morning_end": "06:00",
    "morning_factor": "1.25",
    "negative_enabled": "on",
    "average_window": "day",
    "mode": "shadow",
}


@pytest.fixture
def setup(make_controller):
    def build(**kwargs):
        controller = make_controller(**kwargs)
        controller.tick()
        app = create_app(
            controller,
            tz=TZ,
            allowed_hosts=[HOST, "localhost:9123"],
            registry=make_registry(controller, TZ),
            token=TOKEN,
            # No controller thread runs here; the tests call tick() themselves.
            settle_seconds=0,
        )
        app.testing = True
        return controller, app.test_client()

    return build


@pytest.fixture
def client(setup):
    return setup()[1]


def api_headers(**extra):
    return {"Host": HOST, "X-CSRF-Token": TOKEN, **extra}


# -- the gate -----------------------------------------------------------------------------------


def test_the_page_answers_on_an_allowed_host(client):
    resp = client.get("/", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "<h1" in body and "Shadow mode" in body
    assert f'name="csrf" value="{TOKEN}"' in body


@pytest.mark.parametrize(
    "host", ["evil.example", "127.0.0.1:9123", "", "boiler.example.ts.net.evil"]
)
def test_any_other_host_is_refused(client, host):
    resp = client.get("/", headers={"Host": host})
    assert resp.status_code == 400
    assert client.get("/api/v1/status", headers={"Host": host}).status_code == 400


@pytest.mark.parametrize("path", ["/healthz", "/metrics", "/readyz"])
def test_probes_and_scrapes_need_no_known_host(client, path):
    # Kubernetes and Prometheus use the pod's IP address as the host.
    assert client.get(path, headers={"Host": "10.42.0.17:9123"}).status_code in (200, 503)


def test_security_headers(client):
    resp = client.get("/", headers=HEADERS)
    csp = resp.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "'unsafe-inline'" not in csp
    assert "frame-ancestors 'none'" in csp
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in resp.headers


def test_the_page_has_no_inline_style_or_script(client):
    body = client.get("/", headers=HEADERS).get_data(as_text=True)
    assert " style=" not in body
    assert not re.search(r"<script(?![^>]*src=)", body)
    assert "http://" not in body.replace('xmlns="http://www.w3.org/2000/svg"', "")


def test_static_files_are_served(client):
    for name in ("app.css", "app.js", "favicon.svg", "fonts/AtkinsonHyperlegibleNext-latin.woff2"):
        assert client.get(f"/static/{name}", headers=HEADERS).status_code == 200


# -- forms --------------------------------------------------------------------------------------


def test_saving_the_rules(setup):
    controller, client = setup()
    resp = client.post("/settings", data=FORM, headers=HEADERS)
    assert resp.status_code == 303
    assert resp.headers["Location"] == "/?notice=saved#rules"
    assert controller.snapshot().settings.on_factor == 1.1
    page = client.get("/?notice=saved", headers=HEADERS).get_data(as_text=True)
    assert "Rules saved." in page


def test_an_unticked_box_turns_a_rule_off(setup):
    controller, client = setup()
    form = {k: v for k, v in FORM.items() if k != "negative_enabled"}
    client.post("/settings", data=form, headers=HEADERS)
    assert controller.snapshot().settings.negative_enabled is False


def test_saving_the_same_rules_says_nothing_changed(setup):
    controller, client = setup()
    form = dict(FORM, on_factor="1.05", off_factor="1.25")
    resp = client.post("/settings", data=form, headers=HEADERS)
    assert resp.headers["Location"] == "/?notice=unchanged#rules"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"on_factor": "abc"}, "Enter a number from 0.50 to 3.00"),
        ({"off_factor": "1.00"}, "The switch-off line must be at or above the switch-on line."),
        ({"morning_end": "04:00"}, "The morning window must end after it starts."),
        ({"mode": "turbo"}, "Pick shadow or live."),
    ],
)
def test_a_bad_value_is_explained_and_nothing_is_saved(setup, changes, message):
    controller, client = setup()
    resp = client.post("/settings", data=dict(FORM, **changes), headers=HEADERS)
    assert resp.status_code == 422
    body = resp.get_data(as_text=True)
    assert message in body
    assert "The rules were not saved." in body
    assert 'aria-invalid="true"' in body
    assert controller.snapshot().settings == Settings()


def test_going_live_needs_a_confirmation(setup):
    controller, client = setup()
    resp = client.post("/settings", data=dict(FORM, mode="live"), headers=HEADERS)
    assert resp.status_code == 422
    assert "Tick the box" in resp.get_data(as_text=True)
    assert controller.snapshot().settings.mode == "shadow"
    resp = client.post(
        "/settings", data=dict(FORM, mode="live", confirm_live="yes"), headers=HEADERS
    )
    assert resp.status_code == 303
    assert controller.snapshot().settings.mode == "live"


def test_going_live_is_refused_when_switching_is_off(setup):
    controller, client = setup(allow_switching=False)
    form = dict(FORM, mode="live", confirm_live="yes")
    resp = client.post("/settings", data=form, headers=HEADERS)
    assert resp.status_code == 422
    assert "ALLOW_SWITCHING=true" in resp.get_data(as_text=True)
    assert controller.snapshot().settings.mode == "shadow"


def test_setting_and_ending_an_override(setup, fake_shelly):
    controller, client = setup()
    resp = client.post(
        "/override", data={"csrf": TOKEN, "action": "on", "until": "18:00"}, headers=HEADERS
    )
    assert resp.status_code == 303
    override = controller.snapshot().override
    assert override.action == "on"
    assert override.until == dt.datetime.fromtimestamp(ts(NORMAL_DAY, 18), dt.UTC)
    page = client.get("/", headers=HEADERS).get_data(as_text=True)
    assert "Set to heat until 18:00." in page
    resp = client.post("/override/clear", data={"csrf": TOKEN}, headers=HEADERS)
    assert resp.headers["Location"] == "/?notice=cleared#override"
    assert controller.snapshot().override is None


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"action": "boil", "until": "18:00"}, "Pick heat or keep off."),
        ({"action": "on", "until": "later"}, "Enter the time as hours and minutes"),
        ({"action": "on"}, "Enter the time the override ends"),
    ],
)
def test_a_bad_override_is_explained(client, data, message):
    resp = client.post("/override", data={"csrf": TOKEN, **data}, headers=HEADERS)
    assert resp.status_code == 422
    body = resp.get_data(as_text=True)
    assert message in body
    assert 'class="field-error error-summary" id="err-override" role="alert" tabindex="-1"' in body


def test_the_page_offers_full_times_and_refuses_one_that_passed(setup, clock):
    controller, client = setup()
    body = client.get("/", headers=HEADERS).get_data(as_text=True)
    assert '<option value="2026-10-27T12:15:00+01:00">12:15</option>' in body
    assert 'action="/override#err-override"' in body
    # The page stood open: 12:15 has passed when the button is pressed.
    clock.set(ts(NORMAL_DAY, 12, 20))
    data = {"csrf": TOKEN, "action": "on", "until": "2026-10-27T12:15:00+01:00"}
    resp = client.post("/override", data=data, headers=HEADERS)
    assert resp.status_code == 422
    assert "That time has already passed." in resp.get_data(as_text=True)
    assert controller.snapshot().override is None
    data["until"] = "2026-10-27T14:00:00+01:00"
    assert client.post("/override", data=data, headers=HEADERS).status_code == 303
    assert controller.snapshot().override.until == dt.datetime.fromtimestamp(
        ts(NORMAL_DAY, 14), dt.UTC
    )


def test_notices_show_in_the_section_they_are_about(client):
    body = client.get("/?notice=override", headers=HEADERS).get_data(as_text=True)
    override = body[body.index('id="override"') : body.index('id="rules"')]
    assert "Override set." in override
    body = client.get("/?notice=saved", headers=HEADERS).get_data(as_text=True)
    rules = body[body.index('id="rules"') : body.index('id="log"')]
    assert "Rules saved." in rules
    resp = client.post("/override/clear", data={"csrf": TOKEN}, headers=HEADERS)
    assert resp.headers["Location"] == "/?notice=no_override#override"


def test_no_override_when_switching_is_off(setup):
    controller, client = setup(allow_switching=False)
    data = {"csrf": TOKEN, "action": "on", "until": "18:00"}
    resp = client.post("/override", data=data, headers=HEADERS)
    assert resp.status_code == 422
    assert controller.snapshot().override is None


# -- CSRF and cross-site requests ---------------------------------------------------------------


@pytest.mark.parametrize("token", [None, "", "wrong", TOKEN + "x"])
def test_a_form_without_the_token_is_refused(setup, token):
    controller, client = setup()
    form = {k: v for k, v in FORM.items() if k != "csrf"}
    if token is not None:
        form["csrf"] = token
    resp = client.post("/settings", data=form, headers=HEADERS)
    assert resp.status_code == 403
    assert "This form expired" in resp.get_data(as_text=True)
    assert controller.snapshot().settings == Settings()


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": f"https://{HOST}.evil"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_cross_site_requests_are_refused_even_with_the_token(setup, headers):
    controller, client = setup()
    resp = client.post("/settings", data=FORM, headers={**HEADERS, **headers})
    assert resp.status_code == 403
    assert controller.snapshot().settings == Settings()


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": f"https://{HOST}", "Sec-Fetch-Site": "same-origin"},
        # Chrome sends Origin: null on a form post under a strict referrer
        # policy; Sec-Fetch-Site still says where it came from.
        {"Origin": "null", "Sec-Fetch-Site": "same-origin"},
        # A form typed into the address bar or a bookmark.
        {"Sec-Fetch-Site": "none"},
        # An older browser that sends only Origin.
        {"Origin": f"https://{HOST}"},
        # curl and scripts send neither.
        {},
    ],
)
def test_a_same_origin_post_is_accepted(client, headers):
    assert client.post("/settings", data=FORM, headers={**HEADERS, **headers}).status_code == 303


def test_the_referrer_policy_keeps_the_origin_header_for_our_own_forms(client):
    resp = client.get("/", headers=HEADERS)
    assert resp.headers["Referrer-Policy"] == "same-origin"


def test_changes_need_post(client):
    assert client.get("/settings", headers=HEADERS).status_code == 405
    assert client.get("/override/clear", headers=HEADERS).status_code == 405


def test_a_large_body_is_refused(client):
    resp = client.post("/settings", data={"csrf": TOKEN, "x": "a" * 20000}, headers=HEADERS)
    assert resp.status_code == 413


def test_the_notice_text_never_comes_from_the_url(client):
    body = client.get("/?notice=<script>alert(1)</script>", headers=HEADERS).get_data(as_text=True)
    assert "alert(1)" not in body


def test_who_changed_something_is_escaped(setup):
    controller, client = setup()
    headers = {**HEADERS, "Tailscale-User-Login": "<img src=x onerror=alert(1)>"}
    client.post("/settings", data=FORM, headers=headers)
    body = client.get("/", headers=HEADERS).get_data(as_text=True)
    assert "<img src=x" not in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body


# -- the JSON API -------------------------------------------------------------------------------


def test_api_status(client):
    data = client.get("/api/v1/status", headers=HEADERS).get_json()
    assert data["mode"] == "shadow"
    assert data["decision"]["rule"] == "below_on"
    assert data["prices"]["usable"] is True
    assert data["relay"]["on"] is False
    assert data["csrf_token"] == TOKEN


def test_api_settings_round_trip(setup):
    controller, client = setup()
    resp = client.put("/api/v1/settings", json={"on_factor": 1.15}, headers=api_headers())
    assert resp.status_code == 200
    assert resp.get_json()["on_factor"] == 1.15
    assert client.get("/api/v1/settings", headers=HEADERS).get_json()["on_factor"] == 1.15


def test_api_changes_need_json_and_the_token(setup):
    controller, client = setup()
    resp = client.put(
        "/api/v1/settings",
        data="on_factor=1.2",
        headers=api_headers(**{"Content-Type": "application/x-www-form-urlencoded"}),
    )
    assert resp.status_code == 415
    resp = client.put("/api/v1/settings", json={"on_factor": 1.2}, headers=HEADERS)
    assert resp.status_code == 403
    assert resp.get_json()["error"].startswith("This form expired")
    assert controller.snapshot().settings == Settings()


def test_a_refused_save_lands_on_its_errors(client):
    resp = client.post("/settings", data=dict(FORM, on_factor="2.5"), headers=HEADERS)
    assert resp.status_code == 422
    body = resp.get_data(as_text=True)
    assert 'action="/settings#rules-errors"' in body
    assert 'id="rules-errors" role="alert" tabindex="-1"' in body
    summary = body[body.index('class="error-summary"') :]
    assert 'tabindex="-1"' in summary[:120]
    # Each problem links to its field.
    assert '<a href="#off_factor">Switch off above: The switch-off line must be' in summary


@pytest.mark.parametrize(
    "body",
    [
        '{"on_factor": ' + "9" * 400 + "}",
        "[" * 15000,
        '{"on_factor": 1e999}',
    ],
)
def test_malformed_api_input_is_a_client_error_not_a_crash(client, body):
    resp = client.put(
        "/api/v1/settings",
        data=body,
        headers=api_headers(**{"Content-Type": "application/json"}),
    )
    assert resp.status_code in (400, 422)


@pytest.mark.parametrize("until", ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:00-05:00"])
def test_an_override_time_at_the_edge_of_the_calendar_is_refused(client, until):
    resp = client.post(
        "/api/v1/override", json={"action": "on", "until": until}, headers=api_headers()
    )
    assert resp.status_code == 422
    data = {"csrf": TOKEN, "action": "on", "until": until}
    assert client.post("/override", data=data, headers=HEADERS).status_code == 422


def test_a_factor_with_three_decimals_is_refused_not_rounded(client):
    resp = client.put("/api/v1/settings", json={"on_factor": 1.055}, headers=api_headers())
    assert resp.status_code == 422
    assert "two decimals" in resp.get_json()["errors"]["on_factor"]


def test_api_validation_errors(client):
    resp = client.put("/api/v1/settings", json={"on_factor": "x", "nope": 1}, headers=api_headers())
    assert resp.status_code == 422
    assert set(resp.get_json()["errors"]) == {"on_factor", "nope"}
    resp = client.put("/api/v1/settings", json={"mode": "live"}, headers=api_headers())
    assert resp.status_code == 422
    assert "confirm_live" in resp.get_json()["errors"]
    resp = client.put("/api/v1/settings", json=[1], headers=api_headers())
    assert resp.status_code == 400


def test_api_going_live(setup):
    controller, client = setup()
    resp = client.put(
        "/api/v1/settings", json={"mode": "live", "confirm_live": True}, headers=api_headers()
    )
    assert resp.status_code == 200
    assert controller.snapshot().settings.mode == "live"


def test_api_override(setup, fake_shelly):
    controller, client = setup()
    resp = client.post(
        "/api/v1/override", json={"action": "on", "until": "13:30"}, headers=api_headers()
    )
    assert resp.status_code == 200
    assert resp.get_json()["action"] == "on"
    assert controller.tick() == "change"
    assert fake_shelly.output is True
    resp = client.delete(
        "/api/v1/override", headers=api_headers(**{"Content-Type": "application/json"})
    )
    assert resp.get_json() == {"cleared": True}
    resp = client.post(
        "/api/v1/override", json={"action": "on", "until": "yesterday"}, headers=api_headers()
    )
    assert resp.status_code == 422
    assert "until" in resp.get_json()["errors"]


def test_api_plan(client):
    today = client.get("/api/v1/plan?day=today", headers=HEADERS).get_json()
    assert today["day"] == "2026-10-27"
    assert today["complete"] is True
    assert len(today["slots"]) == 96
    assert today["average"] == pytest.approx(0.20)
    tomorrow = client.get("/api/v1/plan?day=tomorrow", headers=HEADERS).get_json()
    assert tomorrow["day"] == "2026-10-28"
    resp = client.get("/api/v1/plan?day=monday", headers=HEADERS)
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_api_decisions(client):
    data = client.get("/api/v1/decisions?limit=1", headers=HEADERS).get_json()
    assert len(data["entries"]) == 1
    assert data["entries"][0]["kind"] == "evaluation"
    assert client.get("/api/v1/decisions?limit=x", headers=HEADERS).status_code == 400


# -- operations ---------------------------------------------------------------------------------


def test_healthz_and_readyz(setup, fake_exporter, make_controller):
    controller, client = setup()
    assert client.get("/healthz").get_data(as_text=True) == "ok\n"
    assert client.get("/readyz").status_code == 200


def test_readyz_without_prices(setup, fake_exporter):
    fake_exporter.http_status = 500
    controller, client = setup()
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert "no usable price" in resp.get_data(as_text=True)


def test_metrics(client):
    body = client.get("/metrics").get_data(as_text=True)
    assert 'shelly_control_decision_rule{rule="below_on"} 1.0' in body
    assert "shelly_control_live 0.0" in body
    assert "process_" in body or "python_" in body


# -- what the page says -------------------------------------------------------------------------


def test_the_page_says_why_in_shadow_mode(client):
    body = client.get("/", headers=HEADERS).get_data(as_text=True)
    # The fake relay starts off.
    assert "The rules would switch it on: 18.0 ct is under the 21.0 ct switch-on line." in body
    assert "Shadow mode: this service decides and logs, but does not switch the relay." in body
    assert "The rules would switch off at 17:00, then on at 20:00." in body
    assert '<span class="mode shadow">Shadow mode</span>' in body


def test_the_page_shows_missing_prices_as_a_problem(setup, fake_exporter):
    fake_exporter.http_status = 503
    controller, client = setup()
    body = client.get("/", headers=HEADERS).get_data(as_text=True)
    assert "No usable prices: the exporter answered HTTP 503." in body
    assert "There are no prices for today yet." in body


def test_the_page_shows_a_failed_save(setup):
    controller, client = setup()

    def broken(*args):
        from shelly_control.store import StoreError

        raise StoreError("state.json could not be written (Read-only file system)")

    controller.store.save_state = broken
    client.post("/settings", data=FORM, headers=HEADERS)
    body = client.get("/", headers=HEADERS).get_data(as_text=True)
    assert "Changes are kept in memory only" in body


def test_error_pages_are_html_for_the_page_and_json_for_the_api(client):
    assert "text/html" in client.get("/nope", headers=HEADERS).content_type
    assert client.get("/api/v1/nope", headers=HEADERS).get_json()["error"]
