"""State on disk: the settings and override, the decision log, the last good prices.

Three small JSON files in STATE_DIR (a 1 GiB volume in the cluster):

- state.json: the settings and an active override. Written only when someone
  changes them, so it is also the audit trail of the last change.
- decisions.json: the last few hundred decisions, for the page after a restart.
- prices.json: the last good price list, so the controller can still decide
  while the exporter is down.

Every write goes to a temporary file in the same directory, is flushed to disk,
then renamed over the old file, so a crash or a full disk leaves either the
old file or the new one, never half of one. The volume is NFS, which is why
this is plain JSON and not SQLite.

A failed write never stops a decision: the controller keeps the state in
memory, counts the failure and shows a banner.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .prices import PriceBook
from .rules import SETTING_NAMES, Override, Settings, parse_settings

SCHEMA_VERSION = 1
STATE_FILE = "state.json"
DECISIONS_FILE = "decisions.json"
PRICES_FILE = "prices.json"
# A temporary file older than this is left over from a crash mid-write.
STALE_TEMP_SECONDS = 60


class StoreError(Exception):
    """A file could not be written. The message is safe to show on the page."""


@dataclass(frozen=True)
class LoadedState:
    settings: Settings
    override: Override | None
    # Text for a banner when the file could not be used as it was.
    problem: str | None = None
    updated_at: str | None = None
    updated_by: str | None = None


def _describe(exc: OSError) -> str:
    return exc.strerror or exc.__class__.__name__


def write_atomic(path: Path, data: bytes) -> None:
    """Replace `path` with `data` in one step. Raises OSError."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    # Make the rename itself durable. Some file systems refuse to sync a
    # directory; the rename has happened either way.
    with contextlib.suppress(OSError):
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _dump(data: dict) -> bytes:
    return (json.dumps(data, indent=1, ensure_ascii=False) + "\n").encode()


class Store:
    def __init__(self, directory: str | Path, clock=time.time) -> None:
        self.dir = Path(directory)
        self.clock = clock
        self.remove_stale_temp_files()

    def remove_stale_temp_files(self) -> list[str]:
        """Delete temporary files a crash between writing and renaming left behind.

        Only this store's own pattern (.state.json.<random>.tmp and the like),
        and only files older than a minute, so a write in progress is never
        touched. Returns the names removed.
        """
        removed = []
        # File times are real time, whatever clock the store was given.
        cutoff = time.time() - STALE_TEMP_SECONDS
        for name in (STATE_FILE, DECISIONS_FILE, PRICES_FILE):
            try:
                found = list(self.dir.glob(f".{name}.*.tmp"))
            except OSError:
                return removed
            for path in found:
                with contextlib.suppress(OSError):
                    if path.is_file() and path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed.append(path.name)
        return removed

    # -- settings and override -------------------------------------------------------------------

    def load_state(self, defaults: Settings) -> LoadedState:
        """The saved settings, or `defaults` when there are none.

        A file that cannot be used is moved aside, and the defaults are used in
        shadow mode, whatever the defaults say: an unknown state never switches.
        """
        path = self.dir / STATE_FILE
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return LoadedState(defaults, None)
        except OSError as exc:
            return LoadedState(
                replace(defaults, mode="shadow"),
                None,
                f"The saved settings could not be read ({_describe(exc)}). The defaults are "
                "in use, in shadow mode.",
            )
        try:
            return self._decode(json.loads(raw), defaults)
        except _NewerFile:
            aside = self._set_aside(path, "newer")
            return LoadedState(
                replace(defaults, mode="shadow"),
                None,
                f"The saved settings were written by a newer release and were moved to "
                f"{aside}. The defaults are in use, in shadow mode.",
            )
        except (ValueError, TypeError, KeyError, RecursionError):
            aside = self._set_aside(path, "unreadable")
            return LoadedState(
                replace(defaults, mode="shadow"),
                None,
                f"The saved settings could not be used and were moved to {aside}. The "
                "defaults are in use, in shadow mode.",
            )

    def _decode(self, data: object, defaults: Settings) -> LoadedState:
        if not isinstance(data, dict):
            raise ValueError("not an object")
        version = data.get("version")
        if version is None:
            # Version 0: a hand-written file with just the settings, flat,
            # such as {"on_factor": 1.1}. Read it as the settings.
            data = {
                "version": SCHEMA_VERSION,
                "settings": {k: v for k, v in data.items() if k in SETTING_NAMES},
                "override": None,
            }
        elif isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("bad version")
        elif version > SCHEMA_VERSION:
            raise _NewerFile
        raw_settings = data.get("settings")
        if not isinstance(raw_settings, dict):
            raise ValueError("no settings")
        # Settings a file does not have yet (it was written by an older
        # release) keep their defaults.
        settings, errors = parse_settings(raw_settings, defaults)
        if errors:
            raise ValueError(f"invalid settings: {sorted(errors)}")
        override = Override.from_json(data["override"]) if data.get("override") else None
        updated_by = data.get("updated_by")
        updated_at = data.get("updated_at")
        return LoadedState(
            settings,
            override,
            None,
            updated_at if isinstance(updated_at, str) else None,
            updated_by if isinstance(updated_by, str) else None,
        )

    def _set_aside(self, path: Path, why: str) -> str:
        stamp = dt.datetime.fromtimestamp(self.clock(), dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        target = path.with_name(f"{path.name}.{why}-{stamp}")
        try:
            os.replace(path, target)
        except OSError:
            return f"nowhere (it could not be moved: the file is still {path.name})"
        return target.name

    def save_state(
        self, settings: Settings, override: Override | None, updated_by: str | None
    ) -> None:
        data = {
            "version": SCHEMA_VERSION,
            "settings": settings.to_json(),
            "override": override.to_json() if override else None,
            "updated_at": dt.datetime.fromtimestamp(self.clock(), dt.UTC).isoformat(),
            "updated_by": updated_by,
        }
        self._write(STATE_FILE, data)

    # -- decision log ----------------------------------------------------------------------------

    def load_decisions(self) -> list[dict]:
        """The saved log, or an empty one. A broken log is not worth a banner."""
        try:
            data = json.loads((self.dir / DECISIONS_FILE).read_bytes())
        except (OSError, ValueError, RecursionError):
            return []
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, dict)]

    def save_decisions(self, entries: list[dict]) -> None:
        self._write(DECISIONS_FILE, {"version": SCHEMA_VERSION, "entries": entries})

    # -- price cache -----------------------------------------------------------------------------

    def load_prices(self) -> PriceBook | None:
        try:
            data = json.loads((self.dir / PRICES_FILE).read_bytes())
            return PriceBook.from_json(data)
        except (OSError, ValueError, TypeError, RecursionError):
            return None

    def save_prices(self, book: PriceBook) -> None:
        self._write(PRICES_FILE, {"version": SCHEMA_VERSION, **book.to_json()})

    # -- writing ---------------------------------------------------------------------------------

    def _write(self, name: str, data: dict) -> None:
        try:
            write_atomic(self.dir / name, _dump(data))
        except OSError as exc:
            raise StoreError(f"{name} could not be written ({_describe(exc)})") from None


class _NewerFile(Exception):
    pass
