"""The price client against a fake tibber-exporter, and the price book."""

from __future__ import annotations

import datetime as dt
import json

import pytest
import requests
from conftest import DST_END, NORMAL_DAY, TZ, at, book_of, make_day
from fake_exporter import HOME_ID

from shelly_control.prices import (
    ExporterViolation,
    PriceBook,
    PriceClient,
    PriceError,
    PriceHTTPError,
    PriceNotServed,
    PriceTimeout,
    PriceUnreachable,
    is_complete,
    merge,
    parse_prices,
)

TOMORROW = NORMAL_DAY + dt.timedelta(days=1)


# -- parsing ------------------------------------------------------------------------------------


def test_null_is_an_empty_list():
    # The exporter encodes its empty list as null before its first price query.
    assert parse_prices(None) == ()


def test_slots_are_sorted_deduplicated_and_in_utc():
    day = make_day(NORMAL_DAY)
    shuffled = day[50:] + day[:50] + day[10:12]
    slots = parse_prices(shuffled)
    assert len(slots) == 96
    assert [s.start for s in slots] == sorted(s.start for s in slots)
    assert slots[0].start == at(NORMAL_DAY, 0)
    assert slots[0].start.tzinfo is dt.UTC
    assert slots[0].end - slots[0].start == dt.timedelta(minutes=15)


def test_a_slot_without_a_total_is_skipped():
    day = make_day(NORMAL_DAY)
    day[5]["total"] = None
    assert len(parse_prices(day)) == 95


def test_hourly_prices_make_hour_long_slots():
    slots = parse_prices(make_day(NORMAL_DAY, step=3600))
    assert len(slots) == 24
    assert all(s.seconds == 3600 for s in slots)


@pytest.mark.parametrize(
    "data",
    [
        {"startsAt": "2026-10-27T00:00:00+01:00", "total": 0.2},
        [["2026-10-27T00:00:00+01:00", 0.2]],
        [{"total": 0.2}],
        [{"startsAt": "yesterday", "total": 0.2}],
        [{"startsAt": "2026-10-27T00:00:00", "total": 0.2}],
        [{"startsAt": "2026-10-27T00:00:00+01:00", "total": "0.2"}],
        [{"startsAt": "2026-10-27T00:00:00+01:00", "total": True}],
        [
            {"startsAt": "2026-10-27T00:00:00+01:00", "total": 0.2},
            {"startsAt": "2026-10-27T00:05:00+01:00", "total": 0.2},
        ],
    ],
)
def test_malformed_replies_are_refused(data):
    with pytest.raises(PriceError):
        parse_prices(data)


def test_completeness_needs_midnight_to_midnight_without_gaps():
    slots = parse_prices(make_day(NORMAL_DAY))
    assert is_complete(slots, NORMAL_DAY, TZ)
    assert not is_complete(slots[1:], NORMAL_DAY, TZ)
    assert not is_complete(slots[:-1], NORMAL_DAY, TZ)
    assert not is_complete(slots[:40] + slots[41:], NORMAL_DAY, TZ)
    assert not is_complete((), NORMAL_DAY, TZ)


def test_slot_lookup_uses_start_and_end():
    book = book_of(make_day(NORMAL_DAY))
    assert book.slot_at(at(NORMAL_DAY, 0)).start == at(NORMAL_DAY, 0)
    assert book.slot_at(at(NORMAL_DAY, 0, 14, 59)).start == at(NORMAL_DAY, 0)
    assert book.slot_at(at(NORMAL_DAY, 0, 15)).start == at(NORMAL_DAY, 0, 15)
    assert book.slot_at(at(NORMAL_DAY, 0) - dt.timedelta(seconds=1)) is None
    assert book.slot_at(at(TOMORROW, 0)) is None


def test_just_after_midnight_the_new_day_is_found_in_a_list_that_still_holds_yesterday():
    # For up to a minute after midnight the exporter's lists are a day behind:
    # yesterday and today. Picking by time still finds today's first quarter.
    book = book_of(make_day(NORMAL_DAY), make_day(TOMORROW, lambda i, local: 0.11))
    slot = book.slot_at(at(TOMORROW, 0, 0, 30))
    assert slot.total == 0.11
    assert book.complete(TOMORROW, TZ)


# -- merging a fresh read with what is known ----------------------------------------------------


def test_merge_keeps_cached_quarters_when_the_exporter_falls_back_to_hours():
    cached = book_of(make_day(NORMAL_DAY), make_day(TOMORROW))
    hourly = parse_prices(make_day(NORMAL_DAY, step=3600) + make_day(TOMORROW, step=3600))
    merged = merge(cached, hourly, 100.0, TZ, NORMAL_DAY)
    assert len(merged.day(NORMAL_DAY, TZ)) == 96
    assert len(merged.day(TOMORROW, TZ)) == 96
    assert merged.fetched_at == 100.0


def test_merge_takes_a_fresh_complete_day_and_drops_old_days():
    yesterday = NORMAL_DAY - dt.timedelta(days=2)
    cached = book_of(make_day(yesterday), make_day(NORMAL_DAY, lambda i, local: 0.5))
    fresh = parse_prices(make_day(NORMAL_DAY) + make_day(TOMORROW))
    merged = merge(cached, fresh, 1.0, TZ, NORMAL_DAY - dt.timedelta(days=1))
    assert merged.days(TZ) == [NORMAL_DAY, TOMORROW]
    assert merged.slot_at(at(NORMAL_DAY, 3)).total == 0.18


def test_merge_keeps_a_cached_day_the_fresh_read_lacks():
    cached = book_of(make_day(NORMAL_DAY), make_day(TOMORROW))
    fresh = parse_prices(make_day(NORMAL_DAY))
    merged = merge(cached, fresh, 1.0, TZ, NORMAL_DAY)
    assert merged.complete(TOMORROW, TZ)


def test_cache_file_round_trip_keeps_mixed_resolutions():
    book = PriceBook(
        parse_prices(make_day(NORMAL_DAY)) + parse_prices(make_day(TOMORROW, step=3600)), 42.0
    )
    again = PriceBook.from_json(json.loads(json.dumps(book.to_json())))
    assert again == book


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"slots": "x"},
        {
            "slots": [
                {
                    "start": "2026-10-27T00:00:00+00:00",
                    "end": "2026-10-27T00:10:00+00:00",
                    "total": 1,
                }
            ]
        },
        {
            "slots": [
                {
                    "start": "2026-10-27T00:00:00+00:00",
                    "end": "2026-10-27T00:15:00+00:00",
                    "total": True,
                }
            ]
        },
        {"slots": [], "fetched_at": "now"},
    ],
)
def test_a_broken_cache_file_is_refused(data):
    with pytest.raises(ValueError):
        PriceBook.from_json(data)


def test_the_dst_day_round_trips_through_the_client_shape():
    entries = make_day(DST_END)
    assert len(entries) == 100
    assert entries[8]["startsAt"] == "2026-10-25T02:00:00+02:00"
    assert entries[12]["startsAt"] == "2026-10-25T02:00:00+01:00"
    assert len(parse_prices(entries)) == 100


# -- the client against the fake exporter -------------------------------------------------------


def test_fetch_reads_today_and_tomorrow(fake_exporter):
    slots = PriceClient(fake_exporter.url, HOME_ID).fetch()
    assert len(slots) == 192
    # GET of the plain list (today and tomorrow), never period=today or period=now.
    assert fake_exporter.requests == [("GET", f"/homes/{HOME_ID}/prices")]


def test_fetch_of_null_is_empty(fake_exporter):
    fake_exporter.prices = None
    assert PriceClient(fake_exporter.url, HOME_ID).fetch() == ()


def test_a_wrong_home_id_is_reported_as_not_served(fake_exporter):
    other = "11111111-1111-4111-8111-111111111111"
    with pytest.raises(PriceNotServed, match="TIBBER_HOME_ID"):
        PriceClient(fake_exporter.url, other).fetch()


def test_http_errors(fake_exporter):
    fake_exporter.http_status = 503
    with pytest.raises(PriceHTTPError, match="503"):
        PriceClient(fake_exporter.url, HOME_ID).fetch()


def test_a_redirect_is_not_followed(fake_exporter):
    fake_exporter.redirect_to = "http://example.com/"
    with pytest.raises(PriceHTTPError, match="302"):
        PriceClient(fake_exporter.url, HOME_ID).fetch()
    assert len(fake_exporter.requests) == 1


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b'[{"startsAt": 1}]', "application/json"),
        (b"not json", "application/json"),
        (b"[]", "text/html"),
        (b"[" + b" " * (1024 * 1024 + 10) + b"]", "application/json"),
        # Nested deeper than the parser goes: a PriceError, not a RecursionError.
        (b"[" * 20000 + b"]" * 20000, "application/json"),
    ],
)
def test_bad_bodies(fake_exporter, body, content_type):
    fake_exporter.raw_body = body
    fake_exporter.content_type = content_type
    with pytest.raises(PriceError):
        PriceClient(fake_exporter.url, HOME_ID).fetch()


def test_connection_refused(closed_port_url):
    with pytest.raises(PriceUnreachable):
        PriceClient(closed_port_url, HOME_ID, timeout=1).fetch()


def test_timeout(fake_exporter):
    fake_exporter.delay = 1.5
    with pytest.raises(PriceTimeout):
        PriceClient(fake_exporter.url, HOME_ID, timeout=0.3).fetch()


@pytest.mark.parametrize("home_id", ["", "abc", HOME_ID + "/../x", "../" + HOME_ID[3:]])
def test_the_home_id_must_be_a_uuid(fake_exporter, home_id):
    with pytest.raises(ValueError):
        PriceClient(fake_exporter.url, home_id)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", f"/homes/{HOME_ID}/prices"),
        ("GET", f"/homes/{HOME_ID}/prices?period=now"),
        ("GET", f"/homes/{HOME_ID}/prices#x"),
        ("GET", "/homes"),
        ("GET", "/metrics"),
        ("DELETE", f"/homes/{HOME_ID}/prices"),
    ],
)
def test_the_session_refuses_anything_but_the_price_read(fake_exporter, method, path):
    client = PriceClient(fake_exporter.url, HOME_ID)
    with pytest.raises(ExporterViolation):
        client.session.request(method, fake_exporter.url + path, timeout=2)
    prepared = client.session.prepare_request(requests.Request(method, fake_exporter.url + path))
    with pytest.raises(ExporterViolation):
        client.session.send(prepared, timeout=2)
    assert fake_exporter.requests == []


def test_the_session_refuses_another_host(fake_exporter):
    client = PriceClient(fake_exporter.url, HOME_ID)
    other = fake_exporter.url.replace("127.0.0.1", "localhost")
    with pytest.raises(ExporterViolation):
        client.session.get(f"{other}/homes/{HOME_ID}/prices", timeout=2)
    assert fake_exporter.requests == []


def test_the_violation_is_not_a_price_error():
    assert not issubclass(ExporterViolation, PriceError)
