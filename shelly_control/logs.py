"""One JSON object per line on stdout, so Loki (or jq) can read every field.

An evaluation is one line with every input and the outcome. Lifecycle lines
(start, stop, device found, exporter lost and back) carry an `event` and a
`msg`. No line ever holds the device password or the Tibber home id.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, object] = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname.lower(),
            "logger": record.name,
        }
        fields = record.__dict__.get("fields")
        if isinstance(fields, dict):
            line.update(fields)
        line.setdefault("event", "message")
        message = record.getMessage()
        if message:
            line["msg"] = message
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line, ensure_ascii=False, default=str)


def configure(level: int = logging.INFO) -> None:  # pragma: no cover - process wiring
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # waitress logs every queue warning at WARNING; keep those, drop its INFO chatter.
    logging.getLogger("waitress").setLevel(logging.WARNING)


def event(logger: logging.Logger, name: str, msg: str = "", level: int = logging.INFO, **fields):
    """Log one structured line: {"event": name, ...fields, "msg": msg}."""
    logger.log(level, msg, extra={"fields": {"event": name, **fields}})
