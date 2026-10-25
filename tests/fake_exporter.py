"""A fake tibber-exporter on 127.0.0.1 that serves /homes/<id>/prices like the real one.

Like the real exporter, every other path gets HTTP 200 with the text
"Tibber exporter", a wrong home id included. It records every request.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HOME_ID = "00000000-0000-4000-8000-000000000000"


class FakeExporter:
    def __init__(self, prices: list | None = None, home_id: str = HOME_ID):
        self.prices = prices
        self.home_id = home_id
        self.delay = 0.0
        self.http_status: int | None = None
        self.raw_body: bytes | None = None
        self.content_type = "application/json; charset=utf-8"
        self.redirect_to: str | None = None
        self.requests: list[tuple[str, str]] = []
        self._server: ThreadingHTTPServer | None = None
        self.url = ""

    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):
                pass

            def do_GET(self):
                fake._handle(self)

            do_POST = do_PUT = do_DELETE = do_HEAD = do_GET

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

    def _send(self, handler, code: int, body: bytes, content_type: str, headers=()) -> None:
        handler.send_response(code)
        for name, value in headers:
            handler.send_header(name, value)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _handle(self, handler) -> None:
        self.requests.append((handler.command, handler.path))
        if self.delay:
            time.sleep(self.delay)
        path = urlsplit(handler.path).path
        if path != f"/homes/{self.home_id}/prices":
            # The exporter's catch-all route.
            self._send(handler, 200, b"Tibber exporter\n", "text/plain; charset=utf-8")
            return
        if self.redirect_to is not None:
            self._send(handler, 302, b"", "text/plain", [("Location", self.redirect_to)])
            return
        if self.http_status is not None:
            self._send(handler, self.http_status, b"error\n", "text/plain")
            return
        body = self.raw_body
        if body is None:
            body = (json.dumps(self.prices) + "\n").encode()
        self._send(handler, 200, body, self.content_type)
