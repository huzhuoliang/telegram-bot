"""Local HTTP notification server.

Endpoints:
  POST /send        {"text": "message text"}
  POST /send_photo  {"photo": "/path/to/file.png", "caption": "optional"}
                    {"photo": "https://...",        "caption": "optional"}
"""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

logger = logging.getLogger(__name__)


class _NotifyHandler(BaseHTTPRequestHandler):
    # Set by run_notify_server before the server starts
    telegram_client = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._respond(400, b"Empty body")
            return
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except Exception:
            self._respond(400, b"Invalid JSON")
            return

        if self.path == "/send":
            text = data.get("text", "")
            if not text:
                self._respond(400, b"Missing 'text'")
                return
            ok = self.telegram_client.send_message(str(text))
            self._respond(200 if ok else 500, b"ok" if ok else b"send failed")

        elif self.path == "/send_photo":
            photo = data.get("photo", "")
            if not photo:
                self._respond(400, b"Missing 'photo'")
                return
            caption = data.get("caption", "")
            ok = self.telegram_client.send_photo(photo, caption)
            self._respond(200 if ok else 500, b"ok" if ok else b"send failed")

        else:
            self._respond(404, b"Not found")

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, b"ok")
        else:
            self._respond(404, b"Not found")

    def _respond(self, code: int, body: bytes):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        logger.debug("notify_server: " + fmt, *args)


def run_notify_server(
    telegram_client,
    port: int,
    shutdown_event: threading.Event,
):
    _NotifyHandler.telegram_client = telegram_client
    server = HTTPServer(("127.0.0.1", port), _NotifyHandler)
    server.timeout = 1  # unblock every second to check shutdown_event
    logger.info("Notify server listening on 127.0.0.1:%d", port)
    while not shutdown_event.is_set():
        server.handle_request()
    server.server_close()
    logger.info("Notify server stopped")
