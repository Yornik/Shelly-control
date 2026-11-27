"""The energy record: exact energy between reads, gaps left as gaps, a file that survives damage."""

from __future__ import annotations

import json

import pytest

from shelly_control.power import (
    MAX_GAP,
    MIN_SPACING,
    PowerLog,
    Sample,
    intervals,
    totals,
)


def test_the_energy_between_two_reads_is_the_average_power():
    (stretch,) = intervals([Sample(0, 100.0, 0.20), Sample(60, 117.5, 0.30)])
    assert stretch.wh == pytest.approx(17.5)
    assert stretch.watts == pytest.approx(1050)
    assert stretch.kwh == pytest.approx(0.0175)
    # The price in force when the stretch began.
    assert stretch.euros == pytest.approx(0.0175 * 0.20)


def test_a_gap_or_a_counter_that_went_back_is_not_averaged_over():
    found = intervals(
        [
            Sample(0, 100),
            Sample(60, 110),
            Sample(60 + MAX_GAP + 1, 500),  # the service was down: no average
            Sample(300, 520),
            Sample(360, 5),  # the device restarted and lost its counter
            Sample(420, 25),
        ]
    )
    assert [(s.t0, s.t1) for s in found] == [(0, 60), (241, 300), (360, 420)]


def test_a_stretch_without_a_price_has_no_euros():
    (stretch,) = intervals([Sample(0, 0, None), Sample(60, 10, None)])
    assert stretch.euros is None


def test_totals_count_the_stretches_that_begin_in_the_period():
    stretches = intervals(
        [Sample(0, 0, 0.2), Sample(60, 1000, 0.2), Sample(120, 2000, None), Sample(180, 3000, 0.4)]
    )
    t = totals(stretches, 0, 200)
    assert t.kwh == pytest.approx(3.0)
    assert t.priced_kwh == pytest.approx(2.0)
    assert t.euros == pytest.approx(0.4)
    assert t.average_price == pytest.approx(0.2)
    assert t.covered == 180 and t.first == 0
    only_later = totals(stretches, 100, 200)
    assert only_later.kwh == pytest.approx(1.0)
    assert only_later.priced_kwh == 0
    assert totals(stretches, 500, 600).average_price is None


def test_the_log_keeps_reads_on_disk_and_loads_them(tmp_path):
    log = PowerLog(tmp_path / "power.jsonl", clock=lambda: 1000.0)
    assert log.add(100, 5.0, 0.2)
    assert log.add(160, 6.5, None)
    again = PowerLog(tmp_path / "power.jsonl", clock=lambda: 1000.0)
    assert again.samples() == (Sample(100, 5.0, 0.2), Sample(160, 6.5, None))
    assert again.samples(since=150) == (Sample(160, 6.5, None),)


def test_reads_too_close_together_or_without_a_counter_are_not_kept(tmp_path):
    log = PowerLog(tmp_path / "power.jsonl", clock=lambda: 1000.0)
    assert log.add(100, 5.0)
    assert not log.add(100 + MIN_SPACING - 1, 5.1)
    assert not log.add(200, None)
    assert not log.add(200, -1.0)
    assert not log.add(200, float("nan"))
    assert len(log.samples()) == 1


def test_a_damaged_old_or_out_of_order_file_is_cleaned_not_fatal(tmp_path):
    path = tmp_path / "power.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps([900, 1.0, 0.2]),
                "not json",
                json.dumps([800, 2.0, 0.2]),  # out of order
                json.dumps(["x", 2.0]),
                json.dumps([1, 2.0, 0.2]),  # older than the record is kept
                json.dumps([1000, 3.0]),
                json.dumps([1100, 4.0, True]),  # a boolean is no price
            ]
        )
    )
    log = PowerLog(path, clock=lambda: 8 * 86400 + 1000.0)
    assert [s.ts for s in log.samples()] == [1000, 1100]
    assert log.samples()[1].price is None
    # Rewritten without the bad lines.
    assert len(path.read_text().splitlines()) == 2


def test_the_record_is_trimmed_to_a_week_and_a_day(tmp_path):
    clock = [0.0]
    log = PowerLog(tmp_path / "power.jsonl", clock=lambda: clock[0])
    log.add(0, 1.0)
    log.add(9 * 86400, 2.0)
    assert [s.ts for s in log.samples()] == [9 * 86400]


def test_a_file_that_cannot_be_written_never_raises(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("x")
    log = PowerLog(blocked / "power.jsonl", clock=lambda: 1000.0)
    assert log.add(100, 1.0)  # kept in memory
    assert log.error is not None
    assert len(log.samples()) == 1


def test_the_file_is_compacted_after_many_appends(tmp_path, monkeypatch):
    monkeypatch.setattr("shelly_control.power.COMPACT_AFTER", 3)
    log = PowerLog(tmp_path / "power.jsonl", clock=lambda: 1000.0)
    for i in range(10):
        log.add(100 + i * 60, float(i))
    assert len(log.samples()) == 10
    assert len((tmp_path / "power.jsonl").read_text().splitlines()) == 10
    log.clear()
    assert not (tmp_path / "power.jsonl").exists()
