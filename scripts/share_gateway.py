#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Paperclip public share gateway.

Only proxies `/s/<opaque-token>` to the local MAGI server. Everything else is
hidden so the public tunnel URL does not expose the Paperclip/MAGI console.
"""

from __future__ import annotations

import argparse
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


TOKEN_RE = re.compile(r"^/s/[A-Za-z0-9_-]{24,128}$")
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "date",
    "server",
    "set-cookie",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class ShareGatewayHandler(BaseHTTPRequestHandler):
    server_version = "PaperclipShareGateway/1.0"

    def do_GET(self) -> None:
        self._proxy_share()

    def do_HEAD(self) -> None:
        self._proxy_share()

    def do_POST(self) -> None:
        self._not_found()

    def do_PUT(self) -> None:
        self._not_found()

    def do_DELETE(self) -> None:
        self._not_found()

    def log_message(self, fmt: str, *args) -> None:
        print("%s - - [%s] %s" % (self.address_string(), self.log_date_time_string(), fmt % args), flush=True)

    def _not_found(self) -> None:
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write("not found\n".encode("utf-8"))

    def _proxy_share(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(b'{"ok":true,"service":"paperclip-share-gateway"}\n')
            return
        if not TOKEN_RE.fullmatch(parsed.path):
            self._not_found()
            return

        target_base = self.server.target_base.rstrip("/")
        target_url = target_base + parsed.path
        if parsed.query:
            target_url += "?" + parsed.query

        headers = {
            "User-Agent": "PaperclipShareGateway/1.0",
            "X-Forwarded-For": self.client_address[0],
            "X-Paperclip-Share-Gateway": "1",
        }
        req = urllib.request.Request(target_url, method=self.command, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.server.upstream_timeout) as resp:
                self.send_response(resp.status)
                for key, value in resp.headers.items():
                    if key.lower() in HOP_BY_HOP_HEADERS:
                        continue
                    self.send_header(key, value)
                self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
                self.end_headers()
                if self.command != "HEAD":
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
        except urllib.error.HTTPError as exc:
            self.send_response(exc.code)
            self.send_header("Content-Type", exc.headers.get("Content-Type", "text/plain; charset=utf-8"))
            self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(exc.read() or b"not found\n")
        except Exception as exc:
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(f"share gateway error: {exc}\n".encode("utf-8"))


class ShareGatewayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, *, target_base: str, upstream_timeout: int):
        super().__init__(addr, handler)
        self.target_base = target_base
        self.upstream_timeout = upstream_timeout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("PAPERCLIP_SHARE_GATEWAY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PAPERCLIP_SHARE_GATEWAY_PORT", "5014")))
    parser.add_argument("--target", default=os.environ.get("PAPERCLIP_SHARE_GATEWAY_TARGET", "http://127.0.0.1:5002"))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("PAPERCLIP_SHARE_GATEWAY_TIMEOUT", "120")))
    args = parser.parse_args()

    server = ShareGatewayServer(
        (args.host, args.port),
        ShareGatewayHandler,
        target_base=args.target,
        upstream_timeout=args.timeout,
    )
    print(f"Paperclip share gateway listening on http://{args.host}:{args.port} -> {args.target}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
