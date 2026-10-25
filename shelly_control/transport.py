"""What the two HTTP clients share: base URL checks and a bounded, timed body read."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

# A base URL may name a host and a port, nothing else. Anything looser lets a
# path through: urlsplit reads http://host\relay\0 as one odd host name, and
# urllib3 then sends /%5Crelay%5C0/... to the device.
_NETLOC = re.compile(r"(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(?::[0-9]{1,5})?")

# Read replies in small pieces, so a server that sends one byte at a time runs
# into the deadline instead of holding the controller for minutes.
READ_CHUNK = 256


class BodyTooLarge(Exception):
    """The reply is bigger than any real reply."""


class BodyTimeout(Exception):
    """The reply did not arrive within the deadline."""


def normalize_base_url(url: str) -> str:
    """Return scheme://host[:port], or raise ValueError.

    The clients append their own paths, so the URL must not carry a path, a
    query or credentials.
    """
    # A path in SHELLY_URL would ride along on every request: a Gen1 control
    # path such as /relay/0?turn=on would switch the relay on each read.
    url = url.strip()
    if "://" not in url:
        url = f"http://{url}"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("must start with http:// or https://")
    if not parts.hostname:
        raise ValueError("has no host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("must not contain a user or password")
    if parts.path.rstrip("/") or parts.query or parts.fragment:
        raise ValueError("must be only scheme, host and port, without a path or query")
    if not _NETLOC.fullmatch(parts.netloc):
        raise ValueError("must be only scheme, host and port, without a path or query")
    parts.port  # noqa: B018 - raises ValueError for a port above 65535
    return f"{parts.scheme}://{parts.netloc}"


def origin_of(base_url: str) -> tuple[str, str]:
    parts = urlsplit(base_url)
    return parts.scheme, parts.netloc.lower()


def read_bounded(
    chunks: Iterable[bytes],
    limit: int,
    deadline: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> bytes:
    """Join `chunks` into one body; raise if it grows past `limit` or `deadline` passes."""
    body = bytearray()
    for chunk in chunks:
        body += chunk
        if len(body) > limit:
            raise BodyTooLarge(f"more than {limit} bytes")
        if monotonic() > deadline:
            raise BodyTimeout("the reply took too long")
    return bytes(body)
