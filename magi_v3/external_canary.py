"""Signed, privacy-minimized off-host availability evidence."""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "magi.off-host-canary/v1"
REQUIRED_CHECKS = ("dns_ipv4", "dns_ipv6", "tls", "http_health", "login_redirect")


class ExternalCanaryError(ValueError):
    pass


def canonical_bytes(receipt: Mapping[str, Any]) -> bytes:
    unsigned = json.loads(json.dumps(receipt))
    unsigned.pop("signature", None)
    return json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_receipt(receipt: Mapping[str, Any], *, private_key_pem: bytes, key_id: str) -> dict[str, Any]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ExternalCanaryError("off-host canary key must be Ed25519")
    value = json.loads(json.dumps(receipt))
    value.pop("signature", None)
    signature = key.sign(canonical_bytes(value))
    value["signature"] = {
        "algorithm": "ed25519",
        "key_id": key_id,
        "value": base64.b64encode(signature).decode("ascii"),
    }
    return value


def verify_receipt(
    receipt: Mapping[str, Any],
    *,
    public_key_pem: bytes,
    expected_host: str,
    expected_key_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not isinstance(receipt, Mapping) or receipt.get("schema") != SCHEMA:
        raise ExternalCanaryError("invalid off-host canary schema")
    signature = receipt.get("signature")
    if not isinstance(signature, Mapping) or signature.get("algorithm") != "ed25519":
        raise ExternalCanaryError("off-host canary signature required")
    if signature.get("key_id") != expected_key_id:
        raise ExternalCanaryError("off-host canary key ID mismatch")
    try:
        signature_bytes = base64.b64decode(str(signature.get("value") or ""), validate=True)
        key = serialization.load_pem_public_key(public_key_pem)
    except Exception as exc:
        raise ExternalCanaryError("invalid off-host canary key material") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise ExternalCanaryError("off-host canary public key must be Ed25519")
    try:
        key.verify(signature_bytes, canonical_bytes(receipt))
    except InvalidSignature as exc:
        raise ExternalCanaryError("off-host canary signature mismatch") from exc

    target = receipt.get("target")
    if not isinstance(target, Mapping):
        raise ExternalCanaryError("off-host canary target required")
    if str(target.get("host") or "").lower().rstrip(".") != expected_host.lower().rstrip("."):
        raise ExternalCanaryError("off-host canary target mismatch")
    if target.get("port") != 443 or target.get("health_path") != "/health" or target.get("protected_path") != "/osc":
        raise ExternalCanaryError("off-host canary route contract mismatch")

    vantage = receipt.get("vantage")
    if not isinstance(vantage, Mapping):
        raise ExternalCanaryError("off-host vantage required")
    if (
        vantage.get("network_path") != "public_internet_off_host"
        or vantage.get("tailnet_connected") is not False
        or not str(vantage.get("provider") or "").strip()
        or not str(vantage.get("region") or "").strip()
    ):
        raise ExternalCanaryError("canary is not an attested off-host vantage")

    checks = receipt.get("checks")
    if not isinstance(checks, Mapping):
        raise ExternalCanaryError("off-host checks required")
    for name in REQUIRED_CHECKS:
        check = checks.get(name)
        if not isinstance(check, Mapping) or check.get("ok") is not True:
            raise ExternalCanaryError(f"off-host check failed: {name}")

    def _time(name: str) -> datetime:
        try:
            value = datetime.fromisoformat(str(receipt.get(name) or "").replace("Z", "+00:00"))
        except ValueError as exc:
            raise ExternalCanaryError(f"invalid {name}") from exc
        if value.tzinfo is None:
            raise ExternalCanaryError(f"{name} must be timezone-aware")
        return value.astimezone(timezone.utc)

    generated = _time("generated_at")
    expires = _time("expires_at")
    observed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if generated > observed_now or expires <= observed_now or expires <= generated:
        raise ExternalCanaryError("off-host canary receipt is expired or future-dated")
    if (expires - generated).total_seconds() > 900:
        raise ExternalCanaryError("off-host canary validity exceeds 15 minutes")
    return {
        "ok": True,
        "off_host": True,
        "host": expected_host.lower().rstrip("."),
        "receipt_id": str(receipt.get("receipt_id") or ""),
        "generated_at": generated.isoformat(),
        "expires_at": expires.isoformat(),
        "provider": str(vantage["provider"]),
        "region": str(vantage["region"]),
        "checks": {name: True for name in REQUIRED_CHECKS},
    }


def load_from_environment(*, expected_host: str) -> dict[str, Any]:
    receipt_raw = (os.environ.get("MAGI_OFFHOST_CANARY_RECEIPT") or "").strip()
    key_raw = (os.environ.get("MAGI_OFFHOST_CANARY_PUBLIC_KEY") or "").strip()
    key_id = (os.environ.get("MAGI_OFFHOST_CANARY_KEY_ID") or "").strip()
    if not receipt_raw or not key_raw or not key_id:
        return {"ok": False, "off_host": False, "reason_code": "off_host_receipt_unconfigured"}
    try:
        receipt_path = Path(receipt_raw).expanduser()
        key_path = Path(key_raw).expanduser()
        if receipt_path.is_symlink() or key_path.is_symlink():
            raise ExternalCanaryError("off-host evidence paths must not be symlinks")
        if receipt_path.stat().st_size > 64 * 1024 or key_path.stat().st_size > 16 * 1024:
            raise ExternalCanaryError("off-host evidence file too large")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        return verify_receipt(
            receipt,
            public_key_pem=key_path.read_bytes(),
            expected_host=expected_host,
            expected_key_id=key_id,
        )
    except (OSError, json.JSONDecodeError, ExternalCanaryError) as exc:
        return {
            "ok": False,
            "off_host": False,
            "reason_code": "off_host_receipt_invalid",
            "error": str(exc)[:240],
        }


__all__ = [
    "ExternalCanaryError",
    "REQUIRED_CHECKS",
    "SCHEMA",
    "canonical_bytes",
    "load_from_environment",
    "sign_receipt",
    "verify_receipt",
]
