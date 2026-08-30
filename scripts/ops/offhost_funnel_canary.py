#!/usr/bin/env python3
"""Run and sign the public-only MAGI canary from a non-Tailnet host."""

from __future__ import annotations

import argparse
import http.client
import json
import shutil
import socket
import ssl
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from magi_v3.external_canary import SCHEMA, sign_receipt


def _tailnet_connected() -> tuple[bool, bool]:
    executable = shutil.which("tailscale")
    if not executable:
        return False, False
    try:
        result = subprocess.run(
            [executable, "status", "--json"], capture_output=True, text=True, timeout=5, check=False
        )
        value = json.loads(result.stdout or "{}") if result.returncode == 0 else {}
        connected = value.get("BackendState") == "Running" and bool((value.get("Self") or {}).get("Online"))
        return True, bool(connected)
    except Exception:
        return True, True


def _addresses(host: str) -> tuple[list[str], list[str]]:
    values = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    ipv4 = sorted({item[4][0] for item in values if item[0] == socket.AF_INET})
    ipv6 = sorted({item[4][0] for item in values if item[0] == socket.AF_INET6})
    return ipv4, ipv6


def _tls_family(host: str, addresses: list[str], family: int) -> bool:
    context = ssl.create_default_context()
    for address in addresses:
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(10)
        try:
            sock.connect((address, 443))
            with context.wrap_socket(sock, server_hostname=host) as wrapped:
                if wrapped.version() and wrapped.getpeercert():
                    return True
        except OSError:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass
    return False


def _request(host: str, path: str) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPSConnection(host, 443, timeout=15, context=ssl.create_default_context())
    try:
        connection.request("GET", path, headers={"Accept": "application/json", "User-Agent": "MAGI-OffHost-Canary/1"})
        response = connection.getresponse()
        body = response.read(65536)
        return response.status, {key.lower(): value for key, value in response.getheaders()}, body
    finally:
        connection.close()


def run(host: str, *, provider: str, region: str) -> dict:
    binary_present, connected = _tailnet_connected()
    if connected:
        raise RuntimeError("off-host canary refuses to run while Tailnet is connected")
    ipv4, ipv6 = _addresses(host)
    tls4 = _tls_family(host, ipv4, socket.AF_INET) if ipv4 else False
    tls6 = _tls_family(host, ipv6, socket.AF_INET6) if ipv6 else False
    health_status, _, health_body = _request(host, "/health")
    protected_status, protected_headers, _ = _request(host, "/osc")
    location = protected_headers.get("location", "")
    health_json = False
    try:
        health_json = isinstance(json.loads(health_body.decode("utf-8")), dict)
    except Exception:
        pass
    now = datetime.now(timezone.utc)
    return {
        "schema": SCHEMA,
        "receipt_id": uuid.uuid4().hex,
        "generated_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=10)).isoformat(),
        "target": {"host": host, "port": 443, "health_path": "/health", "protected_path": "/osc"},
        "vantage": {
            "provider": provider,
            "region": region,
            "network_path": "public_internet_off_host",
            "tailscale_binary_present": binary_present,
            "tailnet_connected": False,
        },
        "checks": {
            "dns_ipv4": {"ok": bool(ipv4), "answer_count": len(ipv4)},
            "dns_ipv6": {"ok": bool(ipv6), "answer_count": len(ipv6)},
            "tls": {"ok": bool(tls4 and tls6), "ipv4": tls4, "ipv6": tls6},
            "http_health": {"ok": health_status == 200 and health_json, "status": health_status},
            "login_redirect": {
                "ok": protected_status in {301, 302, 303, 307, 308} and location.startswith("/login"),
                "status": protected_status,
                "location_is_login": location.startswith("/login"),
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.private_key.is_symlink() or args.private_key.stat().st_mode & 0o077:
        raise SystemExit("private key must be a non-symlink mode-0600 file")
    receipt = sign_receipt(
        run(args.host.lower().rstrip("."), provider=args.provider, region=args.region),
        private_key_pem=args.private_key.read_bytes(),
        key_id=args.key_id,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({"ok": all(item["ok"] for item in receipt["checks"].values()), "receipt": str(args.output)}))
    return 0 if all(item["ok"] for item in receipt["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
