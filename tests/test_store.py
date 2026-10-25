"""State on disk: atomic writes, broken and old files, and write failures."""

from __future__ import annotations

import datetime as dt
import errno
import json
import os

import pytest
from conftest import NORMAL_DAY, Clock, at, book_of, make_day, ts

from shelly_control import store as store_module
from shelly_control.rules import Override, Settings
from shelly_control.store import (
    DECISIONS_FILE,
    PRICES_FILE,
    SCHEMA_VERSION,
    STATE_FILE,
    Store,
    StoreError,
    write_atomic,
)

LIVE_DEFAULTS = Settings(mode="live", on_factor=1.1)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path, Clock(ts(NORMAL_DAY, 12)))


def test_no_file_means_the_defaults(store):
    loaded = store.load_state(LIVE_DEFAULTS)
    assert loaded.settings == LIVE_DEFAULTS
    assert loaded.override is None
    assert loaded.problem is None
    # Nothing is written until someone saves.
    assert not (store.dir / STATE_FILE).exists()


def test_settings_and_override_round_trip(store):
    settings = Settings(mode="live", on_factor=1.12, morning_start=dt.time(4, 45))
    held = Override("off", at(NORMAL_DAY, 18).astimezone(dt.UTC), at(NORMAL_DAY, 12), "me")
    store.save_state(settings, held, "me")
    loaded = store.load_state(Settings())
    assert loaded.settings == settings
    assert loaded.override == held
    assert loaded.updated_by == "me"
    data = json.loads((store.dir / STATE_FILE).read_text())
    assert data["version"] == SCHEMA_VERSION


def test_temporary_files_a_crash_left_behind_are_removed_at_start(tmp_path):
    old = tmp_path / ".state.json.abc123.tmp"
    fresh = tmp_path / ".prices.json.def456.tmp"
    other = tmp_path / ".notes.txt.tmp"
    for path in (old, fresh, other):
        path.write_text("{")
    os.utime(old, (1, 1))
    os.utime(other, (1, 1))
    Store(tmp_path, Clock(ts(NORMAL_DAY, 12)))
    # Only this store's own leftovers, and only old ones: a write in progress is kept.
    assert not old.exists()
    assert fresh.exists() and other.exists()


def test_a_write_leaves_no_temporary_file(store):
    store.save_state(Settings(), None, None)
    store.save_state(Settings(on_factor=1.3, off_factor=1.4), None, None)
    assert sorted(p.name for p in store.dir.iterdir()) == [STATE_FILE]


def test_a_failed_rename_keeps_the_old_file_whole(store, monkeypatch):
    store.save_state(Settings(on_factor=1.11), None, None)

    def broken(src, dst):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(store_module.os, "replace", broken)
    with pytest.raises(StoreError, match="Input/output error"):
        store.save_state(Settings(on_factor=1.22), None, None)
    monkeypatch.undo()
    assert store.load_state(Settings()).settings.on_factor == 1.11
    assert sorted(p.name for p in store.dir.iterdir()) == [STATE_FILE]


def test_a_read_only_volume_raises_a_store_error(store, monkeypatch):
    def read_only(path, data):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(store_module, "write_atomic", read_only)
    with pytest.raises(
        StoreError, match=r"state.json could not be written \(Read-only file system\)"
    ):
        store.save_state(Settings(), None, None)


def test_write_atomic_creates_the_directory(tmp_path):
    target = tmp_path / "new" / "file.json"
    write_atomic(target, b"{}")
    assert target.read_bytes() == b"{}"


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b"[]",
        b'{"version": 1, "settings": {"on_factor": 9}}',
        b'{"version": 1, "settings": {"on_factor": 1.1, "toggle": 1}}',
        b'{"version": 1, "settings": "x"}',
        b'{"version": "1", "settings": {}}',
        b'{"version": 1, "settings": {}, "override": {"action": "boil"}}',
        b"[" * 20000 + b"]" * 20000,
    ],
)
def test_an_unreadable_file_is_moved_aside_and_shadow_mode_is_forced(store, content):
    (store.dir / STATE_FILE).write_bytes(content)
    loaded = store.load_state(LIVE_DEFAULTS)
    assert loaded.settings.mode == "shadow"
    assert loaded.settings.on_factor == LIVE_DEFAULTS.on_factor
    assert "moved to state.json.unreadable-20261027T110000Z" in loaded.problem
    assert not (store.dir / STATE_FILE).exists()
    assert (store.dir / "state.json.unreadable-20261027T110000Z").read_bytes() == content


def test_a_file_from_a_newer_release_is_kept_aside(store):
    (store.dir / STATE_FILE).write_text(json.dumps({"version": 99, "settings": {}}))
    loaded = store.load_state(LIVE_DEFAULTS)
    assert loaded.settings.mode == "shadow"
    assert "newer release" in loaded.problem
    assert (store.dir / "state.json.newer-20261027T110000Z").exists()


def test_a_hand_written_flat_file_is_upgraded(store):
    # Version 0: no version field, just settings, as someone might write it on the NAS.
    (store.dir / STATE_FILE).write_text(json.dumps({"on_factor": 1.15, "mode": "live"}))
    loaded = store.load_state(Settings())
    assert loaded.problem is None
    assert loaded.settings.on_factor == 1.15
    assert loaded.settings.mode == "live"
    assert loaded.settings.off_factor == Settings().off_factor


def test_settings_an_older_file_lacks_take_their_defaults(store):
    data = {"version": 1, "settings": {"on_factor": 1.1, "off_factor": 1.3}, "override": None}
    (store.dir / STATE_FILE).write_text(json.dumps(data))
    loaded = store.load_state(Settings(average_window="day_and_next"))
    assert loaded.settings.on_factor == 1.1
    assert loaded.settings.average_window == "day_and_next"


def test_an_unreadable_file_that_cannot_be_moved_is_named(store, monkeypatch):
    (store.dir / STATE_FILE).write_text("{")

    def no_rename(src, dst):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(store_module.os, "replace", no_rename)
    loaded = store.load_state(Settings())
    assert "could not be moved" in loaded.problem


def test_decisions_round_trip_and_a_broken_log_is_empty(store):
    entries = [{"kind": "evaluation", "ts": "2026-10-27T11:00:05+00:00", "rule": "between"}]
    store.save_decisions(entries)
    assert store.load_decisions() == entries
    (store.dir / DECISIONS_FILE).write_text("{")
    assert store.load_decisions() == []
    (store.dir / DECISIONS_FILE).write_text('{"entries": [1, {"a": 1}]}')
    assert store.load_decisions() == [{"a": 1}]


def test_price_cache_round_trip_and_a_broken_cache_is_ignored(store):
    book = book_of(make_day(NORMAL_DAY), fetched_at=123.0)
    store.save_prices(book)
    assert store.load_prices() == book
    (store.dir / PRICES_FILE).write_text('{"slots": [{"start": "x"}]}')
    assert store.load_prices() is None


def test_files_are_plain_json_readable_on_the_nas(store):
    store.save_state(Settings(), None, "me")
    text = (store.dir / STATE_FILE).read_text()
    assert text.endswith("\n")
    assert '"on_factor": 1.05' in text
    assert oct(os.stat(store.dir / STATE_FILE).st_mode & 0o777) == "0o600"
