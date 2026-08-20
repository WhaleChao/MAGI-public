#!/usr/bin/env python3
"""Apply MAGI tenant schema to the live auth and OSC databases."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR") or Path(__file__).resolve().parents[2]).resolve()
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(MAGI_ROOT / ".env", override=True)
except Exception:
    pass

from api.saas_schema import apply_tenant_schema, inspect_tenant_schema, tenant_id_from_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply MAGI formal SaaS tenant schema")
    parser.add_argument("--tenant-id", default=os.environ.get("MAGI_TENANT_ID") or tenant_id_from_env("magi-primary"))
    parser.add_argument("--tenant-name", default=os.environ.get("MAGI_TENANT_NAME") or "MAGI Primary Tenant")
    parser.add_argument("--public-base-url", default=os.environ.get("MAGI_PUBLIC_BASE_URL") or "")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)

    if args.check_only:
        payload = inspect_tenant_schema(tenant_id=args.tenant_id)
    else:
        payload = apply_tenant_schema(
            tenant_id=args.tenant_id,
            tenant_name=args.tenant_name,
            public_base_url=args.public_base_url,
        )
        payload["inspection"] = inspect_tenant_schema(tenant_id=args.tenant_id)
        payload["ok"] = bool(payload.get("ok")) and bool(payload["inspection"].get("ok"))

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

