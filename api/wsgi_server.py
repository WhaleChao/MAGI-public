"""Production WSGI serving helpers for MAGI HTTP applications."""

from __future__ import annotations

import os


def serve(app, *, host: str, port: int, service_name: str) -> None:
    """Serve a Flask app with bounded production worker settings."""
    from waitress import serve as waitress_serve

    threads = max(2, min(32, int(os.environ.get("MAGI_WSGI_THREADS", "8") or "8")))
    channel_timeout = max(30, int(os.environ.get("MAGI_WSGI_CHANNEL_TIMEOUT", "120") or "120"))
    waitress_serve(
        app,
        host=host,
        port=int(port),
        threads=threads,
        channel_timeout=channel_timeout,
        cleanup_interval=15,
        clear_untrusted_proxy_headers=True,
        ident=f"MAGI-{service_name}",
    )
