# ruff: noqa
"""Simple HTTP server returning a custom message."""

from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, HTTPServer
import socket
import sys
import logging

PORT = int(os.getenv("PORT", "8080"))
MESSAGE = os.getenv("MESSAGE", "hello from green")
APP_NAME = os.getenv("APP_NAME", "green")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: D401
        logging.info("GET %s from %s", self.path, self.client_address[0])
        if self.path == "/healthz":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        host = socket.gethostname()
        body = f"{MESSAGE} ({APP_NAME}@{host})\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        try:
            logging.info("%s - - " + format, self.client_address[0], *args)
        except Exception:
            pass


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(message)s")
    logging.info("starting %s server on :%d", APP_NAME, PORT)
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
