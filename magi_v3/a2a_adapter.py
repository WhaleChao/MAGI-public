"""Disabled-by-default A2A 1.0 compatibility boundary.

This adapter can parse and record a proposal for future interoperability.  It
cannot dispatch jobs, write legal data, join WHALE, or become a production
owner.  Enabling execution requires a separate future release contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlsplit


A2A_SCHEMA = "magi.a2a-adapter/v1"
PROPOSAL_SCHEMA = "magi.a2a-proposal/v1"
WHALE_HOSTNAMES = frozenset({"whale"})
TAILNET_CGNAT = ipaddress.ip_network("100.64" + ".0.0/10")
SAFE_TASK_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class A2AAdapterError(ValueError):
    pass


def _federation_host_forbidden(host: str) -> bool:
    normalized = str(host or "").strip().lower().rstrip(".")
    if normalized in WHALE_HOSTNAMES or any(
        normalized.endswith("." + item) for item in WHALE_HOSTNAMES
    ):
        return True
    try:
        return ipaddress.ip_address(normalized) in TAILNET_CGNAT
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class A2AAdapterPolicy:
    enabled: bool = False
    mode: str = "proposal-only"
    writer_access: bool = False
    federation_enabled: bool = False
    allowed_remote_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode != "proposal-only":
            raise A2AAdapterError("A2A adapter is limited to proposal-only mode")
        if self.writer_access:
            raise A2AAdapterError("A2A adapter cannot hold writer access")
        if self.federation_enabled:
            raise A2AAdapterError("A2A federation is disabled")
        if any(_federation_host_forbidden(host) for host in self.allowed_remote_hosts):
            raise A2AAdapterError("WHALE federation cannot be re-enabled through A2A")

    @classmethod
    def load(cls, path: Path | str) -> "A2AAdapterPolicy":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping) or payload.get("schema") != A2A_SCHEMA:
            raise A2AAdapterError("A2A policy schema is invalid")
        return cls(
            enabled=bool(payload.get("enabled", False)),
            mode=str(payload.get("mode") or ""),
            writer_access=bool(payload.get("writer_access", False)),
            federation_enabled=bool(payload.get("federation_enabled", False)),
            allowed_remote_hosts=tuple(str(item) for item in payload.get("allowed_remote_hosts") or []),
        )


def create_proposal(
    policy: A2AAdapterPolicy,
    *,
    task_id: str,
    remote_url: str,
    capability: str,
    payload_digest: str,
) -> dict[str, Any]:
    if not policy.enabled:
        raise A2AAdapterError("A2A adapter is disabled")
    if not SAFE_TASK_RE.fullmatch(str(task_id or "")):
        raise A2AAdapterError("A2A task_id is invalid")
    parsed = urlsplit(str(remote_url or ""))
    host = str(parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise A2AAdapterError("A2A remote URL must be credential-free HTTPS")
    if _federation_host_forbidden(host):
        raise A2AAdapterError("WHALE federation remains disabled")
    if host not in set(policy.allowed_remote_hosts):
        raise A2AAdapterError("A2A remote host is not approved")
    if not re.fullmatch(r"[0-9a-f]{64}", str(payload_digest or "")):
        raise A2AAdapterError("A2A payload digest is invalid")
    proposal = {
        "schema": PROPOSAL_SCHEMA,
        "task_id": task_id,
        "remote_origin": f"https://{host}",
        "capability": str(capability or "")[:128],
        "payload_sha256": payload_digest,
        "mode": "proposal-only",
        "writer_access": False,
        "dispatch_performed": False,
        "federation_enabled": False,
    }
    proposal["receipt_sha256"] = hashlib.sha256(
        json.dumps(proposal, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return proposal


__all__ = ["A2AAdapterError", "A2AAdapterPolicy", "create_proposal"]
