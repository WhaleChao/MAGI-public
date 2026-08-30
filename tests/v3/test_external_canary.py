from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from magi_v3.external_canary import ExternalCanaryError, SCHEMA, sign_receipt, verify_receipt


def _keys() -> tuple[bytes, bytes]:
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def _receipt(now: datetime) -> dict:
    return {
        "schema": SCHEMA,
        "receipt_id": "receipt-1",
        "generated_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=10)).isoformat(),
        "target": {
            "host": "magi.example.test",
            "port": 443,
            "health_path": "/health",
            "protected_path": "/osc",
        },
        "vantage": {
            "provider": "ci-canary",
            "region": "outside-tailnet",
            "network_path": "public_internet_off_host",
            "tailscale_binary_present": False,
            "tailnet_connected": False,
        },
        "checks": {
            "dns_ipv4": {"ok": True, "answer_count": 1},
            "dns_ipv6": {"ok": True, "answer_count": 1},
            "tls": {"ok": True, "ipv4": True, "ipv6": True},
            "http_health": {"ok": True, "status": 200},
            "login_redirect": {"ok": True, "status": 302, "location_is_login": True},
        },
    }


def test_signed_fresh_offhost_receipt_verifies_all_required_layers() -> None:
    now = datetime.now(timezone.utc)
    private, public = _keys()
    signed = sign_receipt(_receipt(now), private_key_pem=private, key_id="canary-2026-01")
    result = verify_receipt(
        signed,
        public_key_pem=public,
        expected_host="magi.example.test",
        expected_key_id="canary-2026-01",
        now=now + timedelta(minutes=1),
    )
    assert result["ok"] is True
    assert result["off_host"] is True
    assert all(result["checks"].values())


def test_tamper_tailnet_or_missing_ipv6_fails_closed() -> None:
    now = datetime.now(timezone.utc)
    private, public = _keys()
    signed = sign_receipt(_receipt(now), private_key_pem=private, key_id="canary")
    signed["checks"]["dns_ipv6"]["ok"] = False
    with pytest.raises(ExternalCanaryError, match="signature mismatch"):
        verify_receipt(
            signed,
            public_key_pem=public,
            expected_host="magi.example.test",
            expected_key_id="canary",
            now=now,
        )

    tailnet = _receipt(now)
    tailnet["vantage"]["tailnet_connected"] = True
    signed_tailnet = sign_receipt(tailnet, private_key_pem=private, key_id="canary")
    with pytest.raises(ExternalCanaryError, match="not an attested off-host"):
        verify_receipt(
            signed_tailnet,
            public_key_pem=public,
            expected_host="magi.example.test",
            expected_key_id="canary",
            now=now,
        )
