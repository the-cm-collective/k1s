"""Simple HTTP server returning a custom message."""

from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.getenv("PORT", "8080"))
MESSAGE = os.getenv("MESSAGE", "hello from green")
APP_NAME = os.getenv("APP_NAME", "green")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: D401
        body = f"{MESSAGE} ({APP_NAME})\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
