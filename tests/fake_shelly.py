"""A fake Shelly Gen2 relay on 127.0.0.1, for testing the real client over real HTTP.

It answers GET /rpc/<Method> like firmware 1.7.x: Shelly.GetDeviceInfo without
authentication, everything else behind SHA-256 digest auth when a password is
set, with numeric nonces good for one request. Switch.Set really changes its
output. It records every request, including the ones the client must never
send.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

DEVICE_INFO = {
    "name": None,
    "id": "shellyplus1pm-0123456789ab",
    "mac": "0123456789AB",
    "slot": 0,
    "model": "SNSW-001P16EU",
    "gen": 2,
    "fw_id": "20250101-000000/1.7.5-gabcdef0",
    "ver": "1.7.5",
    "app": "Plus1PM",
    "auth_en": True,
    "auth_domain": "shellyplus1pm-0123456789ab",
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class FakeShelly:
    def __init__(self, password: str | None = None, output: bool = False):
        self.info = copy.deepcopy(DEVICE_INFO)
        self.password = password
        self.realm = self.info["id"]
        self.output = output
        self.apower = 1050.0 if output else 0.0
        self.source = "init"
        self.errors: list[str] | None = None
        # Knobs a test turns to break the device in one particular way.
        self.delay = 0.0
        self.http_status: int | None = None
        self.raw_body: bytes | None = None
        self.redirect_to: str | None = None
        # The relay ignores Switch.Set (a stuck relay).
        self.stuck = False
        # Switch.Set answers with an RPC error frame instead.
        self.set_error: tuple[int, int, str] | None = None
        # (HTTP method, path with query, whether auth was accepted)
        self.requests: list[tuple[str, str, bool]] = []
        self._nonces: set[str] = set()
        self._next_nonce = int(time.time())
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self.url = ""

    @property
    def sets(self) -> list[str]:
        """The query of every accepted Switch.Set, in order."""
        return [
            urlsplit(path).query
            for method, path, ok in self.requests
            if ok and urlsplit(path).path.casefold() == "/rpc/switch.set"
        ]

    # -- lifecycle -------------------------------------------------------------------------------

    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):
                pass

            def do_GET(self):
                fake._handle(self)

            def _refuse(self):
                fake.requests.append((self.command, self.path, False))
                fake._send(self, 405, b'{"code":405,"message":"only GET here"}')

            do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _refuse

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass

        self._server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # -- protocol --------------------------------------------------------------------------------

    def _send(self, handler, code: int, body: bytes, headers=()) -> None:
        handler.send_response(code)
        for name, value in headers:
            handler.send_header(name, value)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True
        handler.wfile.write(body)

    def _challenge(self, handler) -> None:
        with self._lock:
            self._next_nonce += 1
            nonce = str(self._next_nonce)
            self._nonces.add(nonce)
        header = f'Digest qop="auth", realm="{self.realm}", nonce="{nonce}", algorithm=SHA-256'
        self._send(handler, 401, b"", [("WWW-Authenticate", header)])

    def _authorized(self, handler) -> bool:
        value = handler.headers.get("Authorization", "")
        if not value.startswith("Digest "):
            return False
        fields = {k: a or b for k, a, b in re.findall(r'(\w+)=(?:"([^"]*)"|([^\s,]+))', value[7:])}
        nonce = fields.get("nonce", "")
        with self._lock:
            if nonce not in self._nonces:
                return False
            self._nonces.discard(nonce)
        ha1 = _sha256(f"admin:{self.realm}:{self.password}")
        ha2 = _sha256(f"{handler.command}:{fields.get('uri')}")
        expected = _sha256(f"{ha1}:{nonce}:{fields.get('nc')}:{fields.get('cnonce')}:auth:{ha2}")
        return (
            fields.get("username") == "admin"
            and fields.get("realm") == self.realm
            and fields.get("algorithm", "").upper() == "SHA-256"
            and fields.get("uri") == handler.path
            and fields.get("response") == expected
        )

    def _handle(self, handler) -> None:
        if self.delay:
            time.sleep(self.delay)
        url = urlsplit(handler.path)
        method = url.path.removeprefix("/rpc/") if url.path.startswith("/rpc/") else None
        public = method is not None and method.casefold() == "shelly.getdeviceinfo"
        if self.password and not public and not self._authorized(handler):
            self.requests.append((handler.command, handler.path, False))
            self._challenge(handler)
            return
        self.requests.append((handler.command, handler.path, True))
        if self.redirect_to is not None:
            self._send(handler, 302, b"", [("Location", self.redirect_to)])
            return
        if self.http_status is not None:
            self._send(handler, self.http_status, b'{"code":-114,"message":"unavailable"}')
            return
        if self.raw_body is not None:
            self._send(handler, 200, self.raw_body)
            return
        code, result = self._result(method, parse_qs(url.query))
        self._send(handler, code, json.dumps(result).encode())

    def status(self) -> dict:
        status = {
            "id": 0,
            "source": self.source,
            "output": self.output,
            "apower": self.apower,
            "voltage": 230.1,
            "current": round(self.apower / 230.1, 3),
            "aenergy": {"total": 1234.5, "by_minute": [0, 0, 0], "minute_ts": 1761400000},
            "temperature": {"tC": 41.2, "tF": 106.2},
        }
        if self.errors:
            status["errors"] = list(self.errors)
        return status

    def _result(self, method: str | None, query: dict) -> tuple[int, dict]:
        folded = (method or "").casefold()
        if folded == "shelly.getdeviceinfo":
            return 200, self.info
        if folded in ("switch.getstatus", "switch.set") and query.get("id") != ["0"]:
            return 500, {"code": -105, "message": "Argument 'id', value 7 not found!"}
        if folded == "switch.getstatus":
            return 200, self.status()
        if folded == "switch.set":
            if self.set_error is not None:
                http, code, message = self.set_error
                return http, {"code": code, "message": message}
            on = query.get("on")
            if on not in (["true"], ["false"]):
                return 400, {"code": -103, "message": "Missing required argument 'on'!"}
            was_on = self.output
            if not self.stuck:
                self.output = on == ["true"]
                self.apower = 1050.0 if self.output else 0.0
                self.source = "http"
            return 200, {"was_on": was_on}
        return 404, {"code": 404, "message": f"No handler for {method}"}
