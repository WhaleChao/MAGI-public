#!/usr/bin/env python3
"""Resolve the sealed V2 backup source contract without checkout-local paths."""

from __future__ import annotations

import os
import pwd
from pathlib import Path
from typing import Any, Mapping, Sequence


LIVE_CONTRACT_ID = "magi.v3.formal-v2-live-backup-sources/v2"
LIVE_ROOT_KIND = "current_user_application_support_magi_v2"
LIVE_V2_HOME_RELATIVE = Path("Library/Application Support/MAGI/runtime/MAGI_v2")
LIVE_WEBSITE_RELATIVE = Path("whalechao.github.io")
LEGACY_CONTRACT_ID = "magi.v3.formal-v2-backup-sources/v1"


class SourceContractError(ValueError):
    """Raised when the backup contract can select a non-authoritative source."""


def account_home() -> Path:
    """Return the login account home, deliberately ignoring an isolated HOME."""

    raw = pwd.getpwuid(os.getuid()).pw_dir
    home = Path(raw)
    if not home.is_absolute() or str(home) != raw or home.is_symlink():
        raise SourceContractError("login account home is non-canonical or symlinked")
    return home.resolve(strict=False)


def resolve_source_contract(
    contract: Mapping[str, Any] | None,
    *,
    formal_databases: Sequence[str],
) -> dict[str, Any]:
    """Validate and resolve either the live v2 contract or legacy test fixtures.

    Schema v2 is the only portable production contract: it derives the running
    V2 location from the login account rather than embedding a checkout path.
    Schema v1 remains readable solely so synthetic evidence tests can bind
    explicit temporary roots; the sealed repository policy uses schema v2.
    """

    if not isinstance(contract, Mapping):
        raise SourceContractError("source_contract is missing")
    expected_databases = sorted(formal_databases)
    if contract.get("database_relatives") != expected_databases:
        raise SourceContractError("source_contract database inventory is invalid")

    if contract.get("schema_version") == 2:
        expected_keys = {
            "schema_version",
            "contract_id",
            "root_kind",
            "website_relative",
            "database_relatives",
        }
        if (
            set(contract) != expected_keys
            or contract.get("contract_id") != LIVE_CONTRACT_ID
            or contract.get("root_kind") != LIVE_ROOT_KIND
            or contract.get("website_relative") != LIVE_WEBSITE_RELATIVE.as_posix()
        ):
            raise SourceContractError("live source_contract is invalid")
        v2_root = account_home() / LIVE_V2_HOME_RELATIVE
        website_root = v2_root / LIVE_WEBSITE_RELATIVE
        return {
            **dict(contract),
            "v2_root": str(v2_root),
            "website_root": str(website_root),
        }

    if contract.get("schema_version") == 1:
        expected_keys = {
            "schema_version",
            "contract_id",
            "v2_root",
            "website_root",
            "database_relatives",
        }
        if set(contract) != expected_keys or contract.get("contract_id") != LEGACY_CONTRACT_ID:
            raise SourceContractError("legacy source_contract is invalid")
        for field in ("v2_root", "website_root"):
            value = contract.get(field)
            if not isinstance(value, str) or not Path(value).is_absolute() or str(Path(value)) != value:
                raise SourceContractError(f"legacy source_contract.{field} is not canonical absolute")
        return dict(contract)

    raise SourceContractError("source_contract schema_version is unsupported")
