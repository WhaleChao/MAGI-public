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
import sys
import time
from urllib.error import URLError
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from magi_v3.external_canary import load_from_environment as _load_offhost_canary


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


def _canonical_tailnet_ip(value: Any) -> str:
    raw = str(value or "").strip().split("/", 1)[0]
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return ""


def _evaluate_tailnet_member_access(netmap: dict[str, Any]) -> dict[str, Any]:
    """Attest the compiled member-to-node HTTPS grant without exposing topology."""

    self_node = netmap.get("SelfNode") if isinstance(netmap, dict) else {}
    self_node = self_node if isinstance(self_node, dict) else {}
    self_user = str(self_node.get("User") or "").strip()
    self_ips = {
        ip
        for value in self_node.get("Addresses") or []
        if (ip := _canonical_tailnet_ip(value))
    }
    families = {ipaddress.ip_address(value).version for value in self_ips}
    if not self_user or not self_ips:
        return {"ok": False, "attested": True, "reason_code": "tailnet_self_identity_missing"}
    if families != {4, 6}:
        return {
            "ok": False,
            "attested": True,
            "reason_code": "tailnet_dual_stack_destination_missing",
            "destination_family_count": len(families),
        }

    member_sources = set(self_ips)
    peers = netmap.get("Peers") if isinstance(netmap.get("Peers"), list) else []
    member_peer_count = 0
    for peer in peers:
        if not isinstance(peer, dict) or str(peer.get("User") or "").strip() != self_user:
            continue
        member_peer_count += 1
        member_sources.update(
            ip
            for value in peer.get("Addresses") or []
            if (ip := _canonical_tailnet_ip(value))
        )

    rules = netmap.get("PacketFilterRules")
    rules = rules if isinstance(rules, list) else []
    relevant: list[tuple[set[str], set[tuple[str, int, int]], set[int]]] = []
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("CapGrant"):
            continue
        destinations: set[tuple[str, int, int]] = set()
        for item in rule.get("DstPorts") or []:
            if not isinstance(item, dict):
                continue
            ip = _canonical_tailnet_ip(item.get("IP"))
            ports = item.get("Ports") if isinstance(item.get("Ports"), dict) else {}
            try:
                first = int(ports.get("First"))
                last = int(ports.get("Last"))
            except (TypeError, ValueError):
                continue
            if ip:
                destinations.add((ip, first, last))
        if any(ip in self_ips for ip, _first, _last in destinations):
            sources = {
                ip
                for value in rule.get("SrcIPs") or []
                if (ip := _canonical_tailnet_ip(value))
            }
            protocols = {
                int(value)
                for value in rule.get("IPProto") or []
                if isinstance(value, int) or str(value).isdigit()
            }
            relevant.append((sources, destinations, protocols))

    expected_destinations = {(value, 443, 443) for value in self_ips}
    exact = [
        row
        for row in relevant
        if row[0] == member_sources and row[1] == expected_destinations and row[2] == {6}
    ]
    ok = len(relevant) == 1 and len(exact) == 1 and member_peer_count > 0
    return {
        "ok": ok,
        "attested": True,
        "reason_code": "member_https_dual_stack_verified" if ok else "member_https_grant_missing_or_drifted",
        "destination_family_count": len(families),
        "member_peer_count": member_peer_count,
        "member_source_count": len(member_sources),
        "relevant_rule_count": len(relevant),
    }


def _load_tailnet_member_access() -> dict[str, Any]:
    ts = _tailscale_bin()
    if ts == "tailscale" and shutil.which("tailscale") is None:
        return {"ok": None, "attested": False, "reason_code": "tailscale_cli_unavailable"}
    result = _run([ts, "debug", "netmap"], timeout=10)
    if not result.get("ok"):
        return {"ok": None, "attested": False, "reason_code": "tailnet_netmap_unavailable"}
    try:
        payload = json.loads(str(result.get("stdout") or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {"ok": None, "attested": False, "reason_code": "tailnet_netmap_invalid"}
    return _evaluate_tailnet_member_access(payload)


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


def _edge_probe_coverage(probes: list[dict[str, Any]]) -> dict[str, Any]:
    """Require every DNS-advertised edge that this host could probe.

    A browser can be sent to any advertised Funnel address.  Treating one
    successful address as globally healthy hides a partial relay outage and
    can disagree with the network an operator is actually using.
    """

    advertised = [probe for probe in probes if str(probe.get("ip") or "").strip()]
    by_family: dict[str, dict[str, int]] = {
        "ipv4": {"advertised": 0, "passed": 0, "failed": 0},
        "ipv6": {"advertised": 0, "passed": 0, "failed": 0},
    }
    for probe in advertised:
        try:
            family = "ipv6" if ipaddress.ip_address(str(probe["ip"])).version == 6 else "ipv4"
        except ValueError:
            continue
        by_family[family]["advertised"] += 1
        outcome = "passed" if probe.get("ok") is True else "failed"
        by_family[family][outcome] += 1
    passed = sum(1 for probe in advertised if probe.get("ok") is True)
    failed = len(advertised) - passed
    return {
        "ok": bool(advertised) and failed == 0,
        "partial": passed > 0 and failed > 0,
        "advertised": len(advertised),
        "passed": passed,
        "failed": failed,
        "by_family": by_family,
        "vantage": "host_to_public_edge_pinned",
        "off_host": False,
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
    return {
        "ok": bool(probes) and all(probe.get("ok") is True for probe in probes),
        "expected": MOBILE_ENTRY_EXPECTED,
        "probes": probes,
    }


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
    return {
        "ok": bool(probes) and all(p.get("ok") is True for p in probes),
        "expected": MOBILE_ENTRY_EXPECTED,
        "probes": probes,
    }


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
    return {
        "ok": bool(probes) and all(p.get("ok") is True for p in probes),
        "expected": MOBILE_ENTRY_EXPECTED,
        "probes": probes,
    }


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


def _local_funnel_backend_ready() -> dict[str, Any]:
    """Require the existing local web service before changing Funnel state.

    Funnel repair must never turn an already-working public route into a route
    that points at a dead local process.  Keep this preflight local-only and
    bounded; it intentionally does not restart MAGI or Tailscale.
    """

    url = f"{APPROVED_FUNNEL_PROXY.rstrip('/')}/health"
    try:
        with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=5) as response:
            status = int(getattr(response, "status", 0) or response.getcode() or 0)
        return {
            "ok": 200 <= status < 300,
            "url": url,
            "http_code": status,
            "reason_code": "local_backend_ready" if 200 <= status < 300 else "local_backend_unhealthy",
        }
    except (OSError, URLError, ValueError) as exc:
        return {
            "ok": False,
            "url": url,
            "http_code": 0,
            "reason_code": "local_backend_unreachable",
            "error": type(exc).__name__,
        }


def _refresh_public_ingress(scope: dict[str, Any]) -> dict[str, Any]:
    """Refresh only the approved Funnel's live ingress bindings.

    This is deliberately non-disruptive.  A previous implementation used
    debug rebind operations here; even though they did not issue ``off``, they
    could still create a browser-visible gap.  The only permitted mutation is
    the idempotent root reassert, and only after the existing local backend is
    healthy.  Never reset, remove, take down, or restart the current Funnel.
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

    backend = _local_funnel_backend_ready()
    if not backend.get("ok"):
        return {
            "action": "refresh_public_ingress",
            "status": "blocked",
            "reason_code": str(backend.get("reason_code") or "local_backend_unhealthy"),
            "target": approved,
            "local_backend": backend,
            "disruption_policy": "no_disruptive_funnel_mutation",
        }

    funnel = _reassert_approved_funnel(scope)
    steps = [{"step": "local_backend_preflight", "result": backend}, {"step": "funnel_reassert", "result": funnel}]
    return {
        "action": "refresh_public_ingress",
        "status": "applied" if funnel.get("status") == "applied" else "failed",
        "reason_code": "non_disruptive_root_reassert" if funnel.get("status") == "applied" else "funnel_reassert_failed",
        "target": approved,
        "steps": steps,
        "disruption_policy": "no_disruptive_funnel_mutation",
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


def _check_host_vantage(apply: bool = False) -> dict[str, Any]:
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

    payload["edge_coverage"] = _edge_probe_coverage(probes)
    public_ok = payload["edge_coverage"]["ok"]
    mobile_ok = payload["mobile_entry"].get("ok")
    if not public_ok or mobile_ok is False:
        payload["canonical_dns_probes"] = [
            _probe_dns_route(target["host"], target["path"])
            for target in targets
        ]
        payload["canonical_mobile_entry"] = _probe_mobile_entry_targets_dns(targets)
        payload["canonical_dns_is_tailnet_only"] = True
        # A Tailscale-managed hostname can legitimately resolve through the
        # local MagicDNS/Tailnet path while public recursive resolvers return
        # no A record.  That is not evidence that the live ingress is down.
        # Treat the canonical route as an observable degraded state and do
        # not run any Funnel mutation; the old path repeatedly performed
        # restun/rebind/netmap refreshes here and created browser-visible gaps.
        canonical_public_ok = all(
            probe.get("ok") is True
            for probe in payload["canonical_dns_probes"]
        ) and bool(payload["canonical_dns_probes"])
        canonical_mobile_ok = payload["canonical_mobile_entry"].get("ok") is True
        no_public_edge_ips = not any(ips_by_host.values())
        if no_public_edge_ips and not public_ok and canonical_public_ok and canonical_mobile_ok:
            payload["security_boundary"] = _probe_security_boundaries(
                targets,
                ips_by_host,
                use_dns_route=True,
            )
            if payload["security_boundary"].get("ok") is True:
                payload.update(
                    {
                        "status": "degraded",
                        "reason": "canonical Tailnet/DNS route is reachable, but public edge DNS is unavailable",
                        "public_edge_unattested": True,
                    }
                )
                _append_unique(
                    payload["next_actions"],
                    "Verify public Funnel DNS from an external resolver; no Funnel refresh was performed because the canonical route is healthy.",
                )
                return _observe_local_dns(
                    payload,
                    [target["host"] for target in targets],
                    apply=False,
                )
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
            # The routed edge and mobile entry have already succeeded, and
            # the exact authentication boundary was verified above.  A
            # partial resolver matrix is therefore an observation about DNS
            # cache convergence, not evidence that the Funnel binding is
            # missing.  Reasserting a healthy Funnel can force a short
            # restun/rebind window and surface as ERR_CONNECTION_CLOSED to a
            # browser, so never mutate ingress on this branch.
            payload.update(
                {
                    "status": "degraded",
                    "reason": "public Funnel is reachable, but public DNS resolvers are still converging",
                    "dns_convergence_pending": True,
                    "ingress_mutation_suppressed": "public_route_verified",
                }
            )
            _append_unique(
                payload["next_actions"],
                "Wait for the public DNS negative-cache TTL, then verify Cloudflare and Google DNS again; the healthy Funnel was left unchanged.",
            )
            return _observe_local_dns(payload, [target["host"] for target in targets], apply=apply)
        reason = "public Funnel and mobile entry probes succeeded" if mobile_ok is True else "public Funnel probe succeeded"
        payload.update({"status": "ok", "reason": reason})
        return _observe_local_dns(payload, [target["host"] for target in targets], apply=apply)
    if public_ok and mobile_ok is False:
        payload.update({"status": "failed", "reason": "public Funnel probe succeeded, but mobile entry/login probe failed"})
        _add_repair_guidance(payload, targets)
        return payload
    payload.update({"status": "failed", "reason": "one or more advertised public Funnel edges failed"})
    if apply:
        # A single pinned-edge failure is not sufficient authority to mutate
        # a CLI-attested Funnel.  Transient edge/TLS churn has repeatedly
        # recovered within seconds while an immediate reassert introduced a
        # browser-visible connection gap.  Require a second independent edge
        # and mobile probe first; when it recovers, verify the exact security
        # boundary and leave ingress untouched.
        time.sleep(2.0)
        confirm_probes: list[dict[str, Any]] = []
        confirm_ips_by_host: dict[str, list[str]] = {}
        for target in targets:
            ips = _public_ips(target["host"])
            confirm_ips_by_host[target["host"]] = ips
            for ip in ips:
                confirm_probes.append(_probe(target["host"], ip, target["path"]))
        payload["confirmation_probes"] = confirm_probes
        payload["mobile_entry_confirmation"] = _probe_mobile_entry_targets(targets, confirm_ips_by_host)
        payload["edge_coverage_confirmation"] = _edge_probe_coverage(confirm_probes)
        confirmed_public = payload["edge_coverage_confirmation"]["ok"]
        confirmed_mobile = payload["mobile_entry_confirmation"].get("ok") is not False
        if confirmed_public and confirmed_mobile:
            payload["security_boundary_confirmation"] = _probe_security_boundaries(
                targets,
                confirm_ips_by_host,
                use_dns_route=False,
            )
            if payload["security_boundary_confirmation"].get("ok") is not True:
                payload.update(
                    {
                        "status": "failed",
                        "reason": "transient public route recovered, but authentication or endpoint boundary verification failed",
                        "action_required": True,
                    }
                )
                _append_unique(payload["next_actions"], "Inspect MAGI authentication routes; Funnel self-repair is intentionally blocked.")
                return payload
            payload["ingress_mutation_suppressed"] = "transient_public_probe_recovered"
            if payload["public_dns"].get("ok") is False:
                payload.update(
                    {
                        "status": "degraded",
                        "reason": "transient public probe recovered; public DNS resolvers are still converging",
                        "dns_convergence_pending": True,
                    }
                )
                _append_unique(
                    payload["next_actions"],
                    "Wait for the public DNS negative-cache TTL; the recovered Funnel was left unchanged.",
                )
            else:
                payload.update(
                    {
                        "status": "recovered",
                        "reason": "transient public probe recovered without changing Funnel",
                    }
                )
            return _observe_local_dns(
                payload,
                [target["host"] for target in targets],
                apply=False,
            )
        if scope.get("ok") is True:
            # The CLI still attests the one exact, code-owned Funnel binding.
            # Replaying the same command cannot repair an independently
            # failing public edge with evidence, but it can interrupt the
            # healthy Tailnet/canonical path.  Keep the failure visible and
            # defer mutation to a reviewed repair instead of making the
            # health checker the source of an outage.
            payload.update(
                {
                    "status": "failed",
                    "reason": "public Funnel probes failed twice, but the exact configured ingress was left unchanged",
                    "action_required": True,
                    "ingress_mutation_suppressed": "verified_scope_public_failure",
                }
            )
            _append_unique(
                payload["next_actions"],
                "Inspect Tailscale edge/DNS health; the exact configured Funnel was not replayed automatically.",
            )
            return payload
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
        payload["edge_coverage_after_repair"] = _edge_probe_coverage(reprobes)
        repaired_public = payload["edge_coverage_after_repair"]["ok"]
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


def _offhost_canary_required() -> bool:
    configured = str(os.environ.get("MAGI_REQUIRE_OFFHOST_CANARY") or "").strip().lower()
    if configured:
        return configured in {"1", "true", "yes", "on"}
    return bool(str(os.environ.get("MAGI_V3_RELEASE_MANIFEST") or "").strip())


def check(apply: bool = False) -> dict[str, Any]:
    """Combine host diagnostics with an independent signed external receipt.

    Host DNS, pinned-edge and canonical-route checks remain valuable repair
    evidence, but a sealed production release cannot turn them into an
    off-host availability claim by itself.
    """
    payload = _check_host_vantage(apply=apply)
    if payload.get("fixture") is True:
        payload["tailnet_member_access"] = {
            "ok": None,
            "attested": False,
            "skipped": True,
            "reason_code": "offline_schedule_fixture",
        }
        payload["external_canary"] = {
            "ok": None,
            "off_host": False,
            "skipped": True,
            "reason_code": "offline_schedule_fixture",
        }
        payload["availability_claim"] = "offline_contract_fixture_only"
        return payload

    member_access = _load_tailnet_member_access()
    payload["tailnet_member_access"] = member_access
    if member_access.get("ok") is False and member_access.get("attested") is True:
        payload["status"] = "failed"
        payload["reason"] = "Tailnet members are not granted the exact dual-stack HTTPS path to MAGI"
        payload["action_required"] = True
        _append_unique(
            payload.setdefault("next_actions", []),
            "Restore the reviewed member grant to this node's IPv4 and IPv6 TCP 443 only; keep Funnel unchanged.",
        )
    elif member_access.get("ok") is None and payload.get("status") in {"ok", "recovered"}:
        payload["status"] = "degraded"
        payload["reason"] = "public ingress passed, but the Tailnet member HTTPS policy could not be attested"
        _append_unique(
            payload.setdefault("next_actions", []),
            "Verify the local Tailscale netmap and rerun this check; public Funnel evidence does not prove peer access.",
        )

    targets = payload.get("targets") if isinstance(payload.get("targets"), list) else []
    host = ""
    if targets and isinstance(targets[0], dict):
        host = str(targets[0].get("host") or "").strip().rstrip(".")
    if not host:
        configured = _public_health_url()
        host = str(urlparse(configured).hostname or "") if configured else ""
    external = (
        _load_offhost_canary(expected_host=host)
        if host
        else {"ok": False, "off_host": False, "reason_code": "off_host_target_missing"}
    )
    payload["external_canary"] = external
    payload["host_vantage"] = {
        "off_host": False,
        "claim": "host_to_public_edge_or_tailnet_diagnostic",
    }

    if external.get("ok") is True:
        payload["availability_claim"] = "externally_verified_public_availability"
        return payload

    payload["availability_claim"] = "host_to_edge_only"
    if _offhost_canary_required() and payload.get("status") in {"ok", "recovered", "degraded"}:
        previous_status = str(payload.get("status") or "")
        previous_reason = str(payload.get("reason") or "")
        payload["host_vantage_status"] = previous_status
        payload["host_vantage_reason"] = previous_reason
        payload["status"] = "degraded"
        payload["reason"] = "host-to-edge diagnostics passed, but fresh signed off-host DNS/TLS/HTTP evidence is unavailable"
        _append_unique(
            payload["next_actions"],
            "Run the independent non-Tailnet canary and publish its signed receipt; local checks cannot declare external availability.",
        )
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
