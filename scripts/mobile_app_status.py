#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess


def _run(args: list[str]) -> dict:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=4)
        return {"ok": result.returncode == 0, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
    except Exception as exc:
        return {"ok": False, "stdout": "", "stderr": str(exc)}


def main() -> int:
    serve = _run(["tailscale", "serve", "status", "--json"])
    status = _run(["tailscale", "status", "--json"])
    payload = {"tailscale_status": status["ok"], "tailscale_serve": serve["ok"]}
    if serve["stdout"]:
        try:
            payload["serve"] = json.loads(serve["stdout"])
        except Exception:
            payload["serve_raw"] = serve["stdout"]
    if status["stdout"]:
        try:
            data = json.loads(status["stdout"])
            self_node = data.get("Self") or {}
            payload["dns_name"] = str(self_node.get("DNSName") or "").rstrip(".")
            ips = self_node.get("TailscaleIPs") or []
            payload["ip"] = ips[0] if ips else ""
            payload["online"] = bool(self_node.get("Online"))
        except Exception:
            payload["status_raw"] = status["stdout"]
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
