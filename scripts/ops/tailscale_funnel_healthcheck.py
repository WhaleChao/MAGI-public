#!/usr/bin/env python3
"""Check and conservatively self-heal the approved Tailscale Funnel.

Local MagicDNS can resolve a Funnel host to the node's 100.x Tailnet address.
That proves tailnet access, but not public Funnel reachability.  This check
queries public DNS over UDP and TCP, probes each public ingress IP with
``curl --resolve``, verifies the public/authentication boundary, and may only
refresh Tailscale's NAT/socket/control bindings before reasserting the single
approved root proxy.  It never resets Funnel, takes Tailscale down, adds a
path, or publishes any port other than HTTPS 443 to MAGI web on
localhost:5002.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = Path(os.environ.get("MAGI_RUNTIME_DIR", "").strip() or ROOT / ".runtime").expanduser()
STATE_PATH = RUNTIME_DIR / "tailscale_funnel_health_latest.json"
MOBILE_ENTRY_PATH = "/mobile-app"
MOBILE_LOGIN_PATH = "/login?next=/mobile&mobile_app=1"
MOBILE_ENTRY_EXPECTED = "302 redirect to /login?next=/mobile&mobile_app=1"
TAILSCALE_APP_BIN = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
TAILSCALE_CLI_BIN = "/opt/homebrew/bin/tailscale"
MACOS_OPEN_BIN = "/usr/bin/open"
APPROVED_FUNNEL_PROXY = "http://127.0.0.1:5002"
APPROVED_FUNNEL_PATH = "/"
PUBLIC_DNS_RESOLVERS = ("1.1.1.1", "8.8.8.8")
DOH_ENDPOINTS = ("https://cloudflare-dns.com/dns-query", "https://dns.google/resolve")
PUBLIC_ENDPOINT_PATHS = {"/health", "/mobile-app", "/mobile", "/login"}
PROXY_ENV_KEYS = {
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
}


def _load_dotenv() -> None:
    if os.environ.get("MAGI_DISABLE_DOTENV", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _run(args: list[str], timeout: int = 20, *, clear_proxy_env: bool = False) -> dict[str, Any]:
    try:
        run_kwargs: dict[str, Any] = {"capture_output": True, "text": True, "timeout": timeout}
        # The macOS App Store/Standalone client exposes its CLI from the GUI
        # executable.  Non-interactive callers (cron, launchd, Python) can be
        # misclassified as GUI launches unless this documented switch is set.
        # Keep the override scoped to the audited app binary; Homebrew's
        # standalone client must retain its normal daemon/socket discovery.
        if clear_proxy_env or (args and args[0] == TAILSCALE_APP_BIN):
            child_env = os.environ.copy()
            if clear_proxy_env:
                for key in PROXY_ENV_KEYS:
                    child_env.pop(key, None)
            if args and args[0] == TAILSCALE_APP_BIN:
                child_env["TAILSCALE_BE_CLI"] = "1"
            run_kwargs["env"] = child_env
        proc = subprocess.run(args, **run_kwargs)
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
            "args": args,
        }
    except Exception as exc:
        return {"ok": False, "returncode": 124, "stdout": "", "stderr": str(exc), "args": args}


def _append_unique(items: list[str], item: str) -> None:
    if item and item not in items:
        items.append(item)


def _parse_curl_http_code(stdout: str) -> int:
    code_text = (stdout or "").strip()[-3:]
    try:
        return int(code_text)
    except Exception:
        return 0


def _is_public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def _extract_location_header(stdout: str) -> str:
    for line in (stdout or "").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == "location":
            return value.strip()
    return ""


def _mobile_redirect_ok(location: str) -> bool:
    if not location:
        return False
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    return parsed.path == "/login" and query.get("next", [None])[0] == "/mobile" and query.get("mobile_app", [None])[0] == "1"


def _tailscale_bin() -> str:
    configured = os.environ.get("MAGI_TAILSCALE_BIN", "")
    test_mode = os.environ.get("MAGI_TEST_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
    schedule_adapter = os.environ.get("MAGI_V3_SCHEDULE_ADAPTER", "").strip()
    if schedule_adapter == "real_entrypoint_fixture_v1":
        fixture_root_raw = os.environ.get("MAGI_V3_SCHEDULE_FIXTURE_ROOT", "").strip()
        if not configured or not fixture_root_raw:
            raise RuntimeError("V3 schedule fixture Tailscale binding is incomplete")
        try:
            fixture_root = Path(fixture_root_raw).expanduser().resolve(strict=True)
            fixture_cli = Path(configured).expanduser().resolve(strict=True)
            fixture_cli.relative_to(fixture_root)
        except (OSError, ValueError) as exc:
            raise RuntimeError("V3 schedule fixture Tailscale CLI escapes fixture root") from exc
        if not fixture_cli.is_file():
            raise RuntimeError("V3 schedule fixture Tailscale CLI is not a file")
        return str(fixture_cli)
    # An explicit production setting may select one of the audited installed
    # clients, but never an arbitrary executable.  Prefer the official App
    # client next: it shares the daemon release and avoids Homebrew/App version
    # skew.  Every absolute candidate is capability-probed before use.
    allowed = {TAILSCALE_APP_BIN, TAILSCALE_CLI_BIN}
    candidates = [configured] if configured and (test_mode or configured in allowed) else []
    candidates += [TAILSCALE_APP_BIN, TAILSCALE_CLI_BIN, "tailscale"]
    for candidate in candidates:
        if not candidate:
            continue
        if candidate == "tailscale":
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
            continue
        if Path(candidate).is_file() and os.access(candidate, os.X_OK) and _tailscale_cli_usable(candidate):
            return candidate
    return "tailscale"


def _tailscale_cli_usable(candidate: str) -> bool:
    """Read-only capability probe; reject app/daemon version disagreement."""
    version = _run([candidate, "version"], timeout=4)
    status = _run([candidate, "status", "--json"], timeout=6)
    funnel = _run([candidate, "funnel", "status", "--json"], timeout=6)
    if not (version["ok"] and status["ok"] and funnel["ok"]):
        return False
    try:
        data = json.loads(status["stdout"] or "{}")
    except Exception:
        return False
    daemon_version = str(data.get("Version") or "").strip()
    cli_version = re.search(r"\d+\.\d+(?:\.\d+)?", str(version["stdout"] or ""))
    return bool(daemon_version and cli_version and daemon_version.startswith(cli_version.group(0)))


def _load_funnel_status() -> dict[str, Any]:
    ts = _tailscale_bin()
    if ts == "tailscale" and shutil.which("tailscale") is None:
        return {"ok": True, "data": {}, "skipped_reason": "tailscale CLI unavailable"}
    res = _run([ts, "funnel", "status", "--json"], timeout=8)
    if not res["ok"]:
        return {"ok": False, "error": res["stderr"] or res["stdout"] or "tailscale funnel status failed"}
    try:
        return {"ok": True, "data": json.loads(res["stdout"] or "{}")}
    except Exception as exc:
        return {"ok": False, "error": f"invalid funnel status json: {exc}", "raw": res["stdout"]}


def _extract_targets(status: dict[str, Any]) -> list[dict[str, str]]:
    targets: list[dict[str, str]] = []
    web = status.get("Web") if isinstance(status, dict) else {}
    if not isinstance(web, dict):
        return targets
    for host_port, cfg in web.items():
        host = str(host_port).rsplit(":", 1)[0]
        handlers = cfg.get("Handlers") if isinstance(cfg, dict) else {}
        if not isinstance(handlers, dict):
            continue
        for path, handler in handlers.items():
            proxy = str((handler or {}).get("Proxy") or "")
            if not proxy:
                continue
            targets.append({"host": host, "path": str(path or "/"), "proxy": proxy})
    return targets


def _public_ips(host: str) -> list[str]:
    ips: list[str] = []
    if shutil.which("dig"):
        for resolver in PUBLIC_DNS_RESOLVERS:
            for record_type in ("A", "AAAA"):
                res = _run(["dig", f"@{resolver}", "+short", host, record_type], timeout=6)
                if not res["ok"]:
                    continue
                for line in res["stdout"].splitlines():
                    line = line.strip()
                    if _is_public_ip(line):
                        ips.append(line)
    elif shutil.which("nslookup"):
        for resolver in ("1.1.1.1", "8.8.8.8"):
            res = _run(["nslookup", host, resolver], timeout=6)
            if not res["ok"]:
                continue
            for line in res["stdout"].splitlines():
                match = re.search(r"Address:\s*([0-9a-fA-F:.]+)", line)
                if match and _is_public_ip(match.group(1)):
                    ips.append(match.group(1))
    return sorted(set(ips))


def _public_dns_matrix(host: str) -> dict[str, Any]:
    """Check the DNS paths commonly used by browsers without exposing IPs.

    Cloudflare keeps independent UDP/TCP caches.  A UDP answer alone can
    therefore coexist with a browser-visible NXDOMAIN over encrypted/TCP DNS.
    Treat partial agreement as convergence-in-progress rather than green.
    """

    hostname = str(host or "").strip().rstrip(".")
    if not hostname:
        return {"ok": False, "partial": False, "checks": [], "reason_code": "hostname_missing"}
    checks: list[dict[str, Any]] = []
    if shutil.which("dig"):
        for resolver in PUBLIC_DNS_RESOLVERS:
            for transport in ("udp", "tcp"):
                args = ["dig"]
                if transport == "tcp": args.append("+tcp")
                args.extend([f"@{resolver}", "+short", hostname, "A"])
                result = _run(args, timeout=5)
                addresses = {line.strip() for line in str(result.get("stdout") or "").splitlines() if _is_public_ip(line.strip())}
                checks.append({"resolver": resolver, "transport": transport, "ok": bool(result.get("ok") and addresses), "answer_count": len(addresses), "reason_code": "resolved" if addresses else "public_dns_unresolved"})
    for endpoint in DOH_ENDPOINTS:
        checks.append(_doh_check(endpoint, hostname))
    raw_by_resolver = {resolver: {c["transport"] for c in checks if c.get("resolver") == resolver and c.get("ok")} for resolver in PUBLIC_DNS_RESOLVERS}
    raw_passed = sum(1 for transports in raw_by_resolver.values() if {"udp", "tcp"}.issubset(transports))
    doh_passed = sum(1 for check in checks if check.get("transport") == "doh" and check["ok"])
    resolved = doh_passed >= 2 or raw_passed >= 2
    partial = not resolved and (raw_passed > 0 or doh_passed > 0)
    return {"ok": resolved, "partial": partial, "checks": checks, "reason_code": "resolved" if resolved else ("converging" if partial else "unresolved")}


def _doh_check(endpoint: str, hostname: str) -> dict[str, Any]:
    """Fixed public DoH probe; returns counts only, never response body/IPs."""
    try:
        url = f"{endpoint}?{urlencode({'name': hostname, 'type': 'A'})}"
        with urlopen(Request(url, headers={"Accept": "application/dns-json"}), timeout=5) as response:
            payload = json.loads(response.read(65536).decode("utf-8"))
        answers = payload.get("Answer") if isinstance(payload, dict) else []
        count = sum(1 for item in answers if isinstance(item, dict) and _is_public_ip(str(item.get("data") or "")))
        return {"resolver": urlparse(endpoint).hostname, "transport": "doh", "ok": bool(count), "answer_count": count, "reason_code": "resolved" if count else "public_dns_unresolved"}
    except Exception:
        return {"resolver": urlparse(endpoint).hostname, "transport": "doh", "ok": False, "answer_count": 0, "reason_code": "public_dns_unresolved"}


def _local_dns_resolution(host: str) -> dict[str, Any]:
    """Check the resolver used by applications on this host.

    Public Funnel probes intentionally bypass local DNS with ``curl --resolve``.
    That proves the service is reachable from the internet, but it does not prove
    Safari/Chrome on this Mac can resolve the public name.  Keep both signals:
    an external-green/local-DNS-broken state is operationally degraded, not a
    Funnel outage and not a successful end-user check.
    """

    hostname = str(host or "").strip().rstrip(".")
    if not hostname:
        return {"ok": None, "host": "", "reason_code": "hostname_missing"}

    if shutil.which("dscacheutil"):
        result = _run(["dscacheutil", "-q", "host", "-a", "name", hostname], timeout=5)
        output = str(result.get("stdout") or "")
        addresses = re.findall(r"(?im)^ip_address:\s*([^\s]+)\s*$", output)
        return {
            "ok": bool(result.get("ok") and addresses),
            "host": hostname,
            "address_count": len(set(addresses)),
            "reason_code": "resolved" if addresses else "local_dns_unresolved",
        }

    if shutil.which("getent"):
        result = _run(["getent", "ahosts", hostname], timeout=5)
        addresses = {
            line.split()[0]
            for line in str(result.get("stdout") or "").splitlines()
            if line.split()
        }
        return {
            "ok": bool(result.get("ok") and addresses),
            "host": hostname,
            "address_count": len(addresses),
            "reason_code": "resolved" if addresses else "local_dns_unresolved",
        }

    return {"ok": None, "host": hostname, "reason_code": "local_dns_probe_unavailable"}


def _observe_local_dns(payload: dict[str, Any], hosts: list[str], *, apply: bool = False) -> dict[str, Any]:
    unique_hosts = list(dict.fromkeys(str(host or "").strip().rstrip(".") for host in hosts if str(host or "").strip()))
    checks = [_local_dns_resolution(host) for host in unique_hosts]
    payload["local_dns"] = {
        "ok": None if not checks else all(check.get("ok") is True for check in checks),
        "checks": checks,
    }
    if checks and any(check.get("ok") is False for check in checks):
        if apply:
            action = _start_official_app()
            payload.setdefault("actions", []).append(action)
            if action.get("status") == "applied":
                time.sleep(2.0)
                rechecks = [_local_dns_resolution(host) for host in unique_hosts]
                payload["local_dns_after_repair"] = {
                    "ok": all(check.get("ok") is True for check in rechecks),
                    "checks": rechecks,
                }
                if rechecks and all(check.get("ok") is True for check in rechecks):
                    payload["status"] = "recovered"
                    payload["reason"] = "official Tailscale app restored local DNS resolution"
                    payload["local_access_degraded"] = False
                    return payload
        payload["status"] = "degraded"
        payload["reason"] = "public Funnel is reachable, but this Mac cannot resolve the public hostname"
        payload["local_access_degraded"] = True
        _append_unique(payload["next_actions"], "Start the official Tailscale app or repair this Mac's DNS, then rerun the health check.")
    return payload


def _probe(host: str, ip: str, path: str) -> dict[str, Any]:
    url_path = path if path.startswith("/") else f"/{path}"
    url = f"https://{host}{url_path if url_path != '/' else '/'}"
    resolve_ip = f"[{ip}]" if ":" in ip else ip
    res = _run(
        [
            "curl",
            "-q",
            "-sS",
            "-L",
            "--max-time",
            "20",
            "--noproxy",
            "*",
            "--resolve",
            f"{host}:443:{resolve_ip}",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            url,
        ],
        timeout=25,
        clear_proxy_env=True,
    )
    http_code = _parse_curl_http_code(res["stdout"])
    return {
        "host": host,
        "ip": ip,
        "path": path,
        "ok": bool(res["ok"] and 200 <= http_code < 500),
        "http_code": http_code,
        "stderr": res["stderr"][-240:],
    }


def _probe_dns_route(host: str, path: str) -> dict[str, Any]:
    """Probe the same route a browser uses, without pinning an ingress IP.

    Funnel ingress addresses are an anycast/edge implementation detail.  A
    direct ``--resolve`` probe remains useful diagnostic evidence, but some
    otherwise healthy edges reject pinned TLS connections.  The canonical
    DNS route is therefore the user-visible availability contract.
    """
    url_path = path if path.startswith("/") else f"/{path}"
    url = f"https://{host}{url_path if url_path != '/' else '/'}"
    res = _run(
        [
            "curl",
            "-q",
            "-sS",
            "-L",
            "--max-time",
            "20",
            "--noproxy",
            "*",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            url,
        ],
        timeout=25,
        clear_proxy_env=True,
    )
    http_code = _parse_curl_http_code(res["stdout"])
    return {
        "host": host,
        "path": path,
        "route": "public_dns",
        "ok": bool(res["ok"] and 200 <= http_code < 500),
        "http_code": http_code,
        "stderr": res["stderr"][-240:],
    }


def _configured_public_probes(url: str) -> list[dict[str, Any]]:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme.lower() != "https" or not host or parsed.port not in {None, 443}:
        return [{"url": url, "ok": False, "error": "public probe requires an HTTPS URL on port 443"}]

    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    ips = _public_ips(host)
    if not ips:
        return [{"url": url, "host": host, "path": path, "ok": False, "error": "no public DNS A/AAAA record"}]
    return [_probe(host, ip, path) for ip in ips]


def _curl_resolve_value(host: str, ip: str) -> str:
    resolve_ip = f"[{ip}]" if ":" in ip else ip
    return f"{host}:443:{resolve_ip}"


def _mobile_probe_args(url: str, *, host: str = "", ip: str = "") -> list[str]:
    args = [
        "curl",
        "-q",
        "-sS",
        "--max-time",
        "12",
        "--noproxy",
        "*",
        "-o",
        "/dev/null",
        "-D",
        "-",
        "-w",
        "\n%{http_code}",
    ]
    if bool(host) != bool(ip):
        raise ValueError("host and ip must be supplied together for an edge-pinned mobile probe")
    if host and ip:
        args.extend(["--resolve", _curl_resolve_value(host, ip)])
    args.append(url)
    return args


def _probe_mobile_login(url: str, *, host: str = "", ip: str = "") -> dict[str, Any]:
    res = _run(_mobile_probe_args(url, host=host, ip=ip), timeout=15, clear_proxy_env=True)
    http_code = _parse_curl_http_code(res["stdout"])
    return {
        "kind": "mobile_login",
        "url": url,
        "host": host,
        "ip": ip,
        "ok": bool(res["ok"] and 200 <= http_code < 400),
        "http_code": http_code,
        "stderr": res["stderr"][-240:],
        "expected": "HTTP 2xx/3xx login response for /login?next=/mobile&mobile_app=1",
    }


def _probe_mobile_entry_url(url: str, *, host: str = "", ip: str = "") -> dict[str, Any]:
    res = _run(_mobile_probe_args(url, host=host, ip=ip), timeout=15, clear_proxy_env=True)
    http_code = _parse_curl_http_code(res["stdout"])
    location = _extract_location_header(res["stdout"])
    ok = bool(res["ok"] and http_code in {301, 302, 303, 307, 308} and _mobile_redirect_ok(location))
    result: dict[str, Any] = {
        "kind": "mobile_entry",
        "url": url,
        "host": host,
        "ip": ip,
        "ok": ok,
        "http_code": http_code,
        "location": location,
        "expected": MOBILE_ENTRY_EXPECTED,
        "stderr": res["stderr"][-240:],
    }
    if not ok:
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            login_url = f"{parsed.scheme}://{parsed.netloc}{MOBILE_LOGIN_PATH}"
            result["login_probe"] = _probe_mobile_login(login_url, host=host, ip=ip)
    return result


def _public_base_url() -> str:
    explicit = (
        os.environ.get("MAGI_PUBLIC_BASE_URL")
        or os.environ.get("MAGI_MOBILE_BASE_URL")
        or os.environ.get("MAGI_TAILSCALE_URL")
        or os.environ.get("MAGI_TAILSCALE_FUNNEL_HEALTH_URL")
        or ""
    ).strip()
    if explicit:
        return explicit
    candidates = []
    configured_file = str(os.environ.get("MAGI_OSC_FILE_SHARE_PUBLIC_BASE_FILE") or "").strip()
    if configured_file:
        candidates.append(Path(configured_file).expanduser())
    candidates.append(RUNTIME_DIR / "osc_share_public_base_url.txt")
    for path in candidates:
        try:
            if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 2048:
                continue
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        except (OSError, UnicodeError):
            continue
    return ""


def _approved_funnel_target() -> dict[str, str] | None:
    """Return the single operator-approved public binding, or fail closed."""

    parsed = urlparse(_public_base_url())
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.port not in {None, 443}
        or parsed.username
        or parsed.password
    ):
        return None
    return {
        "host": parsed.hostname.rstrip("."),
        "path": APPROVED_FUNNEL_PATH,
        "proxy": APPROVED_FUNNEL_PROXY,
    }


def _funnel_scope(targets: list[dict[str, str]], status_data: dict[str, Any] | None = None) -> dict[str, Any]:
    approved = _approved_funnel_target()
    if approved is None:
        return {
            "ok": False,
            "repair_allowed": False,
            "reason_code": "approved_public_base_unconfigured",
        }
    if not targets:
        raw_scope_present = False
        if isinstance(status_data, dict):
            raw_scope_present = any(bool(status_data.get(key)) for key in ("TCP", "Web", "AllowFunnel"))
        return {
            "ok": False,
            "repair_allowed": not raw_scope_present,
            "reason_code": "funnel_scope_violation" if raw_scope_present else "approved_target_missing",
            "approved": approved,
        }
    normalized = [
        {
            "host": str(item.get("host") or "").rstrip("."),
            "path": str(item.get("path") or "/"),
            "proxy": str(item.get("proxy") or "").rstrip("/"),
        }
        for item in targets
    ]
    transport_ok = True
    transport_ports: list[str] = []
    allow_count = 0
    if isinstance(status_data, dict):
        tcp = status_data.get("TCP") if isinstance(status_data.get("TCP"), dict) else {}
        transport_ports = sorted(str(port) for port in tcp)
        tcp_443 = tcp.get("443") or tcp.get(443) or {}
        allow = status_data.get("AllowFunnel") if isinstance(status_data.get("AllowFunnel"), dict) else {}
        expected_allow = f"{approved['host']}:443"
        allow_count = len(allow)
        transport_ok = (
            transport_ports == ["443"]
            and isinstance(tcp_443, dict)
            and tcp_443.get("HTTPS") is True
            and allow == {expected_allow: True}
        )
    exact = len(normalized) == 1 and normalized[0] == approved and transport_ok
    return {
        "ok": exact,
        "repair_allowed": exact,
        "reason_code": "approved_scope" if exact else "funnel_scope_violation",
        "approved": approved,
        "target_count": len(normalized),
        "transport_ports": transport_ports,
        "allow_funnel_count": allow_count,
    }


def _public_endpoint_url(raw: str, endpoint_path: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return value

    path = parsed.path or ""
    normalized_path = path.rstrip("/") or "/"
    if normalized_path == endpoint_path:
        return value
    if normalized_path in PUBLIC_ENDPOINT_PATHS:
        next_path = endpoint_path
    else:
        prefix = path.rstrip("/")
        next_path = f"{prefix}{endpoint_path}" if prefix else endpoint_path
    return parsed._replace(path=next_path).geturl()


def _public_health_url() -> str:
    explicit = str(os.environ.get("MAGI_TAILSCALE_FUNNEL_HEALTH_URL") or "").strip()
    if explicit:
        return explicit
    return _public_endpoint_url(_public_base_url(), "/health")


def _public_mobile_entry_url() -> str:
    explicit = str(os.environ.get("MAGI_TAILSCALE_FUNNEL_MOBILE_URL") or "").strip()
    if explicit:
        return explicit
    return _public_endpoint_url(_public_base_url(), MOBILE_ENTRY_PATH)


def _probe_configured_mobile_entry() -> dict[str, Any]:
    url = _public_mobile_entry_url()
    if not url:
        return {"ok": None, "expected": MOBILE_ENTRY_EXPECTED, "probes": [], "skipped_reason": "no public mobile URL configured"}
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme.lower() != "https" or not host or parsed.port not in {None, 443}:
        probes = [{"kind": "mobile_entry", "url": url, "ok": False, "error": "public probe requires an HTTPS URL on port 443"}]
    else:
        ips = _public_ips(host)
        probes = (
            [_probe_mobile_entry_url(url, host=host, ip=ip) for ip in ips]
            if ips
            else [{"kind": "mobile_entry", "url": url, "host": host, "ok": False, "error": "no public DNS A/AAAA record"}]
        )
    return {"ok": any(probe.get("ok") for probe in probes), "expected": MOBILE_ENTRY_EXPECTED, "probes": probes}


def _probe_mobile_entry_targets(targets: list[dict[str, str]], ips_by_host: dict[str, list[str]]) -> dict[str, Any]:
    probes: list[dict[str, Any]] = []
    seen_hosts: set[str] = set()
    for target in targets:
        host = target["host"]
        if host in seen_hosts:
            continue
        seen_hosts.add(host)
        ips = ips_by_host.get(host) or _public_ips(host)
        ips_by_host[host] = ips
        if not ips:
            probes.append(
                {
                    "kind": "mobile_entry",
                    "host": host,
                    "path": MOBILE_ENTRY_PATH,
                    "ok": False,
                    "error": "no public DNS A/AAAA record",
                    "expected": MOBILE_ENTRY_EXPECTED,
                }
            )
            continue
        for ip in ips:
            probes.append(_probe_mobile_entry_url(f"https://{host}{MOBILE_ENTRY_PATH}", host=host, ip=ip))
    return {"ok": any(p.get("ok") for p in probes), "expected": MOBILE_ENTRY_EXPECTED, "probes": probes}


def _probe_mobile_entry_targets_dns(targets: list[dict[str, str]]) -> dict[str, Any]:
    probes: list[dict[str, Any]] = []
    seen_hosts: set[str] = set()
    for target in targets:
        host = target["host"]
        if host in seen_hosts:
            continue
        seen_hosts.add(host)
        probe = _probe_mobile_entry_url(f"https://{host}{MOBILE_ENTRY_PATH}")
        probe["route"] = "public_dns"
        probes.append(probe)
    return {"ok": any(p.get("ok") for p in probes), "expected": MOBILE_ENTRY_EXPECTED, "probes": probes}


def _boundary_location_ok(location: str, expected_next: str) -> bool:
    parsed = urlparse(str(location or ""))
    query = parse_qs(parsed.query)
    return parsed.path == "/login" and query.get("next", [None])[0] == expected_next


def _probe_boundary_url(host: str, ip: str, path: str, kind: str) -> dict[str, Any]:
    url = f"https://{host}{path}"
    args = [
        "curl",
        "-q",
        "-sS",
        "--max-time",
        "8",
        "--noproxy",
        "*",
    ]
    if ip:
        args.extend(["--resolve", _curl_resolve_value(host, ip)])
    args.extend(["-o", "/dev/null", "-D", "-", "-w", "\n%{http_code}", url])
    result = _run(args, timeout=10, clear_proxy_env=True)
    code = _parse_curl_http_code(result.get("stdout") or "")
    location = _extract_location_header(result.get("stdout") or "")
    if kind == "public":
        ok = code == 200
    elif kind == "invalid_share":
        ok = code == 404
    else:
        ok = code in {401, 403} or (
            code in {301, 302, 303, 307, 308} and _boundary_location_ok(location, path)
        )
    return {
        "path": path,
        "kind": kind,
        "ok": ok,
        "http_code": code,
        "login_redirect": bool(location and _boundary_location_ok(location, path)),
    }


def _probe_security_boundaries(
    targets: list[dict[str, str]],
    ips_by_host: dict[str, list[str]],
    *,
    use_dns_route: bool = False,
) -> dict[str, Any]:
    if not targets:
        return {"ok": False, "checks": [], "reason_code": "target_missing"}
    host = targets[0]["host"]
    ips = ips_by_host.get(host) or []
    if not ips and not use_dns_route:
        return {"ok": False, "checks": [], "reason_code": "public_dns_unresolved"}
    ip = "" if use_dns_route else ips[0]
    specs = (
        ("/login", "public"),
        ("/health", "public"),
        ("/readyz", "public"),
        ("/mobile/manifest.webmanifest", "public"),
        ("/lottery", "public"),
        ("/exam-tutor", "public"),
        ("/cookie-cutter", "public"),
        ("/dashboard", "protected"),
        ("/osc", "protected"),
        ("/mobile", "protected"),
        ("/sentencing-trends", "protected"),
        ("/api/osc/folders/roots", "protected"),
        ("/s/__magi_health_probe_invalid__", "invalid_share"),
    )
    checks = [_probe_boundary_url(host, ip, path, kind) for path, kind in specs]
    return {
        "ok": all(check.get("ok") is True for check in checks),
        "checks": checks,
        "route": "public_dns" if use_dns_route else "edge_pinned",
        "reason_code": "verified" if all(check.get("ok") is True for check in checks) else "boundary_failed",
    }


def _add_repair_guidance(payload: dict[str, Any], targets: list[dict[str, str]]) -> None:
    status = str(payload.get("status") or "")
    mobile_entry = payload.get("mobile_entry_after_repair") or payload.get("mobile_entry")
    mobile_entry = mobile_entry if isinstance(mobile_entry, dict) else {}
    needs_guidance = status in {"error", "failed", "failed_after_repair"} or mobile_entry.get("ok") is False
    if not needs_guidance:
        return

    actions: list[str] = list(payload.get("next_actions") or [])
    if targets:
        _append_unique(actions, "Run the bounded Funnel repair to reassert only HTTPS 443 to localhost:5002.")
    else:
        _append_unique(actions, "Configure Tailscale Funnel for the MAGI web port or set MAGI_PUBLIC_BASE_URL to the public Funnel base URL.")

    all_probe_sets = []
    for key in ("probes", "reprobes"):
        value = payload.get(key)
        if isinstance(value, list):
            all_probe_sets.extend(value)
    if isinstance(mobile_entry, dict):
        all_probe_sets.extend(mobile_entry.get("probes") or [])
    if any(isinstance(p, dict) and "no public DNS" in str(p.get("error", "")) for p in all_probe_sets):
        _append_unique(actions, "Confirm public Funnel DNS with `dig @1.1.1.1 <host> A` and re-enable Funnel if no public record exists.")

    if mobile_entry.get("ok") is False:
        _append_unique(
            actions,
            "Verify `/mobile-app` is forwarded to MAGI web and returns `302 /login?next=/mobile&mobile_app=1`.",
        )
        login_failed = False
        for probe in mobile_entry.get("probes") or []:
            login_probe = probe.get("login_probe") if isinstance(probe, dict) else None
            if isinstance(login_probe, dict) and not login_probe.get("ok"):
                login_failed = True
        if login_failed:
            _append_unique(actions, "Check the MAGI `/login` route locally before repairing public Funnel.")

    _append_unique(actions, "Rerun `scripts/ops/tailscale_funnel_healthcheck.py --print-json` after repair.")
    payload["next_actions"] = actions
    payload["restart_hint"] = (
        "If public probes return 000/5xx while local MAGI works, restart Tailscale/Funnel first; restart MAGI web only when "
        "`/login` or `/mobile-app` fails locally too."
    )


def _start_official_app() -> dict[str, Any]:
    if not Path(TAILSCALE_APP_BIN).is_file() or not Path(MACOS_OPEN_BIN).is_file():
        return {
            "action": "start_official_app",
            "status": "skipped",
            "reason_code": "official_app_unavailable",
        }
    result = _run([MACOS_OPEN_BIN, "-a", "Tailscale"], timeout=15)
    return {
        "action": "start_official_app",
        "status": "applied" if result.get("ok") else "failed",
        "result": result,
    }


def _reassert_approved_funnel(scope: dict[str, Any]) -> dict[str, Any]:
    approved = scope.get("approved") if isinstance(scope, dict) else None
    if not scope.get("repair_allowed") or not isinstance(approved, dict):
        return {
            "action": "reassert_approved_funnel",
            "status": "blocked",
            "reason_code": str(scope.get("reason_code") or "scope_not_approved"),
        }
    if approved != {
        "host": str(approved.get("host") or ""),
        "path": APPROVED_FUNNEL_PATH,
        "proxy": APPROVED_FUNNEL_PROXY,
    }:
        return {
            "action": "reassert_approved_funnel",
            "status": "blocked",
            "reason_code": "approved_target_contract_mismatch",
        }
    result = _run(
        [_tailscale_bin(), "funnel", "--bg", "--yes", APPROVED_FUNNEL_PROXY],
        timeout=20,
    )
    return {
        "action": "reassert_approved_funnel",
        "status": "applied" if result.get("ok") else "failed",
        "target": approved,
        "result": result,
    }


def _refresh_public_ingress(scope: dict[str, Any]) -> dict[str, Any]:
    """Refresh only the approved Funnel's live ingress bindings.

    A long-running macOS network extension can retain a stale NAT/socket or
    control-plane binding while ``funnel status`` still reports the expected
    rule.  These debug operations do not log out, take the tailnet down, reset
    Serve/Funnel configuration, or alter the approved proxy scope.
    """

    approved = scope.get("approved") if isinstance(scope, dict) else None
    if not scope.get("repair_allowed") or not isinstance(approved, dict):
        return {
            "action": "refresh_public_ingress",
            "status": "blocked",
            "reason_code": str(scope.get("reason_code") or "scope_not_approved"),
        }
    if approved != {
        "host": str(approved.get("host") or ""),
        "path": APPROVED_FUNNEL_PATH,
        "proxy": APPROVED_FUNNEL_PROXY,
    }:
        return {
            "action": "refresh_public_ingress",
            "status": "blocked",
            "reason_code": "approved_target_contract_mismatch",
        }

    ts = _tailscale_bin()
    steps = (
        ("restun", [ts, "debug", "restun"]),
        ("rebind", [ts, "debug", "rebind"]),
        ("netmap_refresh", [ts, "debug", "force-netmap-update"]),
    )
    results: list[dict[str, Any]] = []
    for name, command in steps:
        result = _run(command, timeout=15)
        results.append({"step": name, "result": result})
        if not result.get("ok"):
            return {
                "action": "refresh_public_ingress",
                "status": "failed",
                "reason_code": f"{name}_failed",
                "target": approved,
                "steps": results,
            }

    funnel = _reassert_approved_funnel(scope)
    results.append({"step": "funnel_reassert", "result": funnel})
    return {
        "action": "refresh_public_ingress",
        "status": "applied" if funnel.get("status") == "applied" else "failed",
        "reason_code": "bindings_refreshed" if funnel.get("status") == "applied" else "funnel_reassert_failed",
        "target": approved,
        "steps": results,
    }


def _schedule_fixture_enabled() -> bool:
    """Identify the offline adapter without weakening the production checker.

    The schedule campaign must exercise this real entrypoint, but its Seatbelt
    fixture cannot make a truthful claim about public DNS or an external edge.
    Keep that distinction explicit and local to the fixture branch below.
    """

    return (
        os.environ.get("MAGI_V3_REALISM_SANDBOX") == "1"
        and os.environ.get("MAGI_V3_SCHEDULE_ADAPTER") == "real_entrypoint_fixture_v1"
        and bool(str(os.environ.get("MAGI_TAILSCALE_FIXTURE_LOCAL_BACKEND") or "").strip())
    )


def _check_schedule_fixture(*, apply: bool) -> dict[str, Any]:
    """Run the bounded, host-independent Funnel contract fixture.

    This verifies the same production body can attest the exact 443 -> 5002
    scope, require a healthy local backend, and record one approved reassert.
    It deliberately omits public DNS/HTTP and labels those omissions so an
    offline campaign cannot manufacture an external-network result.
    """

    status = _load_funnel_status()
    payload: dict[str, Any] = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "status": "failed",
        "reason": "schedule fixture precondition failed",
        "targets": [],
        "scope": {},
        "public_dns": {
            "ok": None,
            "skipped": True,
            "reason_code": "offline_schedule_fixture",
        },
        "probes": [],
        "mobile_entry": {},
        "actions": [],
        "next_actions": [],
        "fixture": True,
        "external_network_probes": "omitted",
    }
    if not status.get("ok"):
        payload["reason"] = status.get("error") or "fixture Tailscale status unavailable"
        return payload

    targets = _extract_targets(status.get("data") or {})
    payload["targets"] = targets
    scope = _funnel_scope(targets, status.get("data") or {})
    payload["scope"] = scope
    backend = _local_funnel_backend_ready()
    payload["local_backend"] = backend
    if not scope.get("ok") or not scope.get("repair_allowed") or not backend.get("ok"):
        payload["reason"] = "fixture exact Funnel scope or local backend precondition failed"
        return payload

    target = targets[0]
    payload["probes"] = [{
        "host": target["host"],
        "path": target["path"],
        "route": "offline_fixture_initial",
        "ok": False,
        "http_code": 503,
    }]
    payload["mobile_entry"] = {
        "kind": "mobile_entry",
        "url": f"https://{target['host']}{MOBILE_ENTRY_PATH}",
        "ok": False,
        "http_code": 503,
        "fixture": True,
    }
    if not apply:
        payload["reason"] = "fixture requires the bounded apply step"
        return payload

    action = _reassert_approved_funnel(scope)
    payload["actions"].append(action)
    if action.get("status") != "applied":
        payload["reason"] = "fixture Funnel reassert failed"
        return payload

    payload["reprobes"] = [{
        "host": target["host"],
        "path": target["path"],
        "route": "offline_fixture_repaired",
        "ok": True,
        "http_code": 200,
    }]
    payload["mobile_entry_after_repair"] = {
        "kind": "mobile_entry",
        "url": f"https://{target['host']}{MOBILE_ENTRY_PATH}",
        "ok": True,
        "http_code": 302,
        "location": "/login?next=/mobile&mobile_app=1",
        "fixture": True,
    }
    payload.update({
        "status": "recovered",
        "reason": "offline fixture verified exact Funnel scope and bounded reassert",
        "ingress_mutation_suppressed": "offline_fixture_only",
    })
    return payload


def check(apply: bool = False) -> dict[str, Any]:
    _load_dotenv()
    if _schedule_fixture_enabled():
        return _check_schedule_fixture(apply=apply)
    status = _load_funnel_status()
    payload: dict[str, Any] = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "status": "skipped",
        "reason": "",
        "targets": [],
        "scope": {},
        "public_dns": {},
        "probes": [],
        "mobile_entry": {},
        "security_boundary": {},
        "actions": [],
        "next_actions": [],
        "restart_hint": "",
    }
    if not status["ok"] and apply:
        app_action = _start_official_app()
        payload["actions"].append(app_action)
        if app_action.get("status") == "applied":
            time.sleep(2.0)
            status = _load_funnel_status()
    if not status["ok"]:
        payload["mobile_entry"] = _probe_configured_mobile_entry()
        health_url = _public_health_url()
        if health_url:
            payload["probes"] = _configured_public_probes(health_url)
            if any(probe.get("ok") for probe in payload["probes"]):
                if payload["mobile_entry"].get("ok") is False:
                    payload.update({
                        "status": "failed",
                        "reason": "public health probe succeeded, but mobile entry/login probe failed",
                    })
                    _add_repair_guidance(payload, [])
                    return payload
                payload.update({
                    "status": "degraded",
                    "reason": "public health probe succeeded, but the Funnel scope could not be attested",
                    "scope_unattested": True,
                })
                parsed = urlparse(health_url)
                return _observe_local_dns(payload, [parsed.hostname or ""], apply=apply)
        if payload["mobile_entry"].get("ok") is True:
            payload.update(
                {
                    "status": "degraded",
                    "reason": "mobile entry probe succeeded, but the Funnel scope could not be attested",
                    "scope_unattested": True,
                }
            )
            parsed = urlparse(_public_mobile_entry_url())
            return _observe_local_dns(payload, [parsed.hostname or ""], apply=apply)
        payload.update({"status": "error", "reason": status.get("error", "status failed")})
        _add_repair_guidance(payload, [])
        return payload

    targets = _extract_targets(status["data"])
    payload["targets"] = targets
    scope = _funnel_scope(targets, status["data"])
    payload["scope"] = scope
    if not scope.get("ok") and not scope.get("repair_allowed"):
        payload.update(
            {
                "status": "failed",
                "reason": "Funnel scope differs from the single approved HTTPS-to-5002 binding",
                "action_required": True,
            }
        )
        _append_unique(payload["next_actions"], "Review the Funnel scope; automatic repair is blocked and no public rule was changed.")
        return payload
    if not targets:
        payload["mobile_entry"] = _probe_configured_mobile_entry()
        health_url = _public_health_url()
        if health_url:
            payload["probes"] = _configured_public_probes(health_url)
            if any(probe.get("ok") for probe in payload["probes"]):
                if payload["mobile_entry"].get("ok") is False:
                    payload.update({"status": "failed", "reason": "public health probe succeeded, but mobile entry/login probe failed"})
                    _add_repair_guidance(payload, [])
                    return payload
                payload.update(
                    {
                        "status": "degraded",
                        "reason": "public health probe succeeded, but the Funnel target was not attested by the CLI",
                        "scope_unattested": True,
                    }
                )
                parsed = urlparse(health_url)
                return _observe_local_dns(payload, [parsed.hostname or ""], apply=apply)
        if payload["mobile_entry"].get("ok") is True:
            payload.update(
                {
                    "status": "degraded",
                    "reason": "mobile entry probe succeeded, but the Funnel target was not attested by the CLI",
                    "scope_unattested": True,
                }
            )
            parsed = urlparse(_public_mobile_entry_url())
            return _observe_local_dns(payload, [parsed.hostname or ""], apply=apply)
        if payload["mobile_entry"].get("ok") is False:
            payload.update({"status": "failed", "reason": "no Funnel target in CLI output and mobile entry probe failed"})
            _add_repair_guidance(payload, [])
            return payload
        if apply and scope.get("repair_allowed"):
            action = _reassert_approved_funnel(scope)
            payload["actions"].append(action)
            if action.get("status") == "applied":
                payload.update(
                    {
                        "status": "degraded",
                        "reason": "approved Funnel target was restored; public DNS convergence is pending",
                        "dns_convergence_pending": True,
                    }
                )
                return payload
        payload.update({"status": "skipped", "reason": status.get("skipped_reason") or "no Funnel target configured"})
        return payload

    probes: list[dict[str, Any]] = []
    ips_by_host: dict[str, list[str]] = {}
    for target in targets:
        ips = _public_ips(target["host"])
        ips_by_host[target["host"]] = ips
        if not ips:
            probes.append({"host": target["host"], "path": target["path"], "ok": False, "error": "no public DNS A/AAAA record"})
            continue
        for ip in ips:
            probes.append(_probe(target["host"], ip, target["path"]))
    payload["probes"] = probes
    payload["mobile_entry"] = _probe_mobile_entry_targets(targets, ips_by_host)
    payload["public_dns"] = _public_dns_matrix(targets[0]["host"])

    public_ok = any(p.get("ok") for p in probes)
    mobile_ok = payload["mobile_entry"].get("ok")
    if not public_ok or mobile_ok is False:
        payload["canonical_dns_probes"] = [
            _probe_dns_route(target["host"], target["path"])
            for target in targets
        ]
        payload["canonical_mobile_entry"] = _probe_mobile_entry_targets_dns(targets)
        payload["canonical_dns_is_tailnet_only"] = True
    if public_ok and mobile_ok is not False:
        payload["security_boundary"] = _probe_security_boundaries(
            targets,
            ips_by_host,
            use_dns_route=False,
        )
        if payload["security_boundary"].get("ok") is not True:
            payload.update(
                {
                    "status": "failed",
                    "reason": "public authentication or endpoint boundary verification failed",
                    "action_required": True,
                }
            )
            _append_unique(payload["next_actions"], "Inspect MAGI authentication routes; Funnel self-repair is intentionally blocked.")
            return payload
        if payload["public_dns"].get("ok") is False:
            if apply:
                payload["actions"].append(_reassert_approved_funnel(scope))
            payload.update(
                {
                    "status": "degraded",
                    "reason": "public Funnel is reachable, but public DNS resolvers are still converging",
                    "dns_convergence_pending": True,
                }
            )
            _append_unique(payload["next_actions"], "Wait for the public DNS negative-cache TTL, then verify Cloudflare and Google DNS again.")
            return _observe_local_dns(payload, [target["host"] for target in targets], apply=apply)
        reason = "public Funnel and mobile entry probes succeeded" if mobile_ok is True else "public Funnel probe succeeded"
        payload.update({"status": "ok", "reason": reason})
        return _observe_local_dns(payload, [target["host"] for target in targets], apply=apply)
    if public_ok and mobile_ok is False:
        payload.update({"status": "failed", "reason": "public Funnel probe succeeded, but mobile entry/login probe failed"})
        _add_repair_guidance(payload, targets)
        return payload
    payload.update({"status": "failed", "reason": "all public Funnel probes failed"})
    if apply:
        payload["actions"].append(_refresh_public_ingress(scope))
        time.sleep(5.0)
        reprobes: list[dict[str, Any]] = []
        re_ips_by_host: dict[str, list[str]] = {}
        for target in targets:
            ips = _public_ips(target["host"])
            re_ips_by_host[target["host"]] = ips
            for ip in ips:
                reprobes.append(_probe(target["host"], ip, target["path"]))
        payload["reprobes"] = reprobes
        payload["mobile_entry_after_repair"] = _probe_mobile_entry_targets(targets, re_ips_by_host)
        repaired_public = any(p.get("ok") for p in reprobes)
        repaired_mobile = payload["mobile_entry_after_repair"].get("ok") is not False
        if not repaired_public or not repaired_mobile:
            payload["canonical_dns_reprobes"] = [
                _probe_dns_route(target["host"], target["path"])
                for target in targets
            ]
            payload["canonical_mobile_entry_after_repair"] = _probe_mobile_entry_targets_dns(targets)
            payload["canonical_dns_is_tailnet_only"] = True
        if repaired_public and repaired_mobile:
            payload["security_boundary_after_repair"] = _probe_security_boundaries(
                targets,
                re_ips_by_host,
                use_dns_route=False,
            )
        boundary_repaired = (payload.get("security_boundary_after_repair") or {}).get("ok") is True
        payload["status"] = "recovered" if repaired_public and repaired_mobile and boundary_repaired else "failed_after_repair"
        payload["reason"] = (
            "repaired and public/mobile probes succeeded"
            if payload["status"] == "recovered"
            else "repair did not restore public Funnel or mobile entry"
        )
    _add_repair_guidance(payload, targets)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="start the official app or reassert only the approved HTTPS-to-5002 Funnel")
    parser.add_argument("--print-json", action="store_true", help="print JSON only; useful for manual/live checks")
    parser.add_argument("--json-out", default=str(STATE_PATH))
    args = parser.parse_args(argv)

    payload = check(apply=args.apply)
    should_write = args.json_out != "-" and not (args.print_json and args.json_out == str(STATE_PATH))
    if should_write:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    # Local DNS degradation is actionable and visible, but the public service
    # remains healthy.  Do not turn it into a terminal cron failure/red light.
    return 0 if payload["status"] in {"ok", "skipped", "recovered", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
