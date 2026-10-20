"""Tiny HTTP server: serves the single-page UI and the JSON API (standard library only)."""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import api, db

STATIC = Path(__file__).parent / "static"
MAX_BODY = 12 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "ZapuzaQC/1.0"

    def _send(self, status, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        url = urlparse(self.path)
        if method == "GET" and url.path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        body = None
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, {"detail": "Request too large"})
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                return self._send(400, {"detail": "Invalid JSON"})
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        headers = {k.lower(): v for k, v in self.headers.items()}
        try:
            status, payload = api.dispatch(method, url.path, query, body, headers, self.client_address[0])
        except Exception as exc:  # never leak a traceback to the tablet
            sys.stderr.write(f"Unhandled error: {exc!r}\n")
            status, payload = 500, {"detail": "Internal server error"}
        self._send(status, payload)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def log_message(self, fmt, *args):
        if os.environ.get("QC_QUIET") != "1":
            super().log_message(fmt, *args)


def make_server(host="127.0.0.1", port=8000):
    return ThreadingHTTPServer((host, port), Handler)


def main():
    db.init_db()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"Zapuza QC running on http://{host}:{port}  (database: {db.db_path()})")
    make_server(host, port).serve_forever()


if __name__ == "__main__":
    main()
