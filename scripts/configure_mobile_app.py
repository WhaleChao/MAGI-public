#!/usr/bin/env python3
"""Write Capacitor mobile server config from deployment environment."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATHS = (
    ROOT / "mobile_app" / "capacitor.config.json",
    ROOT / "mobile_app" / "android" / "app" / "src" / "main" / "assets" / "capacitor.config.json",
)
DEFAULT_MOBILE_URL = "https://example.invalid/mobile-app"


def _configured_mobile_url() -> str:
    raw = (
        os.environ.get("MAGI_MOBILE_APP_URL")
        or os.environ.get("MAGI_CAPACITOR_SERVER_URL")
        or os.environ.get("MAGI_MOBILE_BASE_URL")
        or os.environ.get("MAGI_PUBLIC_BASE_URL")
        or DEFAULT_MOBILE_URL
    )
    url = str(raw or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit("MAGI mobile URL must be an absolute http(s) URL")
    if not parsed.path.rstrip("/").endswith("/mobile-app"):
        url = f"{url}/mobile-app"
    return url


def _write_config(path: Path, url: str) -> None:
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    server = dict(data.get("server") or {})
    server["url"] = url
    server["cleartext"] = url.startswith("http://")
    data["server"] = server
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    url = _configured_mobile_url()
    for path in CONFIG_PATHS:
        _write_config(path, url)
    print(f"Configured MAGI Mobile server URL: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
