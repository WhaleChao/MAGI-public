"""Whole-process macOS Seatbelt execution for mutable MAGI skills."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping, Sequence

from .skill_manifest import SkillManifestError, load_manifest, verify_catalog_approval


def _quoted(path: Path) -> str:
    return json.dumps(str(path.resolve(strict=False)), ensure_ascii=False)


def seatbelt_profile(manifest: Mapping[str, object], *, skill_dir: Path) -> str:
    permissions = manifest["permissions"]
    filesystem = permissions["filesystem"]
    network = permissions["network"]
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        '(allow file-write* (literal "/dev/null"))',
    ]
    for root in filesystem["write_roots"]:
        path = Path(root)
        rules.append(f"(allow file-write* (literal {_quoted(path)}))")
        rules.append(f"(allow file-write* (subpath {_quoted(path)}))")
    if network["mode"] == "none":
        rules.append("(deny network*)")
    else:
        # Hostname allowlists require the local egress mediator. Direct network
        # access remains fail-closed until that mediator is configured.
        proxy = (os.environ.get("MAGI_SKILL_EGRESS_PROXY") or "").strip()
        if not proxy:
            raise SkillManifestError("allowlisted skill network requires MAGI_SKILL_EGRESS_PROXY")
        rules.append("(deny network*)")
        rules.append("(allow network-outbound (remote ip \"localhost:*\"))")

    home = Path.home()
    protected = (
        home / ".ssh",
        home / "Library" / "Keychains",
        home / "Library" / "Mail",
        home / "Library" / "Messages",
        home / "Library" / "Safari",
        home / "Library" / "Application Support" / "MAGI" / "runtime",
        home / "Library" / "Application Support" / "MAGI" / "releases",
    )
    for path in protected:
        rules.append(f"(deny file-read* (literal {_quoted(path)}))")
        rules.append(f"(deny file-read* (subpath {_quoted(path)}))")
    return "".join(rules)


def _filtered_env(manifest: Mapping[str, object], env: Mapping[str, str]) -> dict[str, str]:
    permission_names = set(manifest["permissions"]["secrets"])
    safe_names = {
        "PATH",
        "LANG",
        "LC_ALL",
        "TZ",
        "PYTHONIOENCODING",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONPYCACHEPREFIX",
        "PYTHONPATH",
        "MAGI_SKILL_EGRESS_PROXY",
    }
    return {
        key: value
        for key, value in env.items()
        if key in safe_names or key in permission_names
    }


def run_manifested_skill(
    command: Sequence[str],
    *,
    skill_dir: Path,
    env: Mapping[str, str],
    timeout_seconds: int,
    require_approved: bool = False,
    catalog_path: Path | None = None,
) -> dict[str, object]:
    started = time.time()
    try:
        manifest = load_manifest(skill_dir)
        if require_approved:
            if catalog_path is None:
                raise SkillManifestError("approved catalog required for live skill")
            verify_catalog_approval(manifest, catalog_path=catalog_path)
        elif manifest["approval"]["status"] == "disabled":
            raise SkillManifestError("skill manifest is disabled")
        executable = Path("/usr/bin/sandbox-exec")
        if sys.platform != "darwin" or not executable.is_file():
            raise SkillManifestError("macOS Seatbelt unavailable")
        profile = seatbelt_profile(manifest, skill_dir=skill_dir)
    except SkillManifestError as exc:
        return {
            "rc": 126,
            "stdout": "",
            "stderr": str(exc),
            "duration_ms": int((time.time() - started) * 1000),
            "sandbox": {"ok": False, "kind": "macos_seatbelt", "fail_closed": True},
        }

    completed = subprocess.run(
        [str(executable), "-p", profile, "--", *command],
        cwd=str(skill_dir),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
        env=_filtered_env(manifest, env),
    )
    stderr = (completed.stderr or "").strip()
    sandbox_apply_denied = (
        completed.returncode == 71
        and "sandbox_apply: Operation not permitted" in stderr
    )
    return {
        "rc": completed.returncode,
        "stdout": (completed.stdout or "").strip(),
        "stderr": stderr,
        "duration_ms": int((time.time() - started) * 1000),
        "sandbox": (
            {
                "ok": False,
                "kind": "macos_seatbelt",
                "fail_closed": True,
                "reason": "sandbox_apply_denied_by_host",
                "manifest_skill_id": manifest["skill_id"],
            }
            if sandbox_apply_denied
            else {
                "ok": True,
                "kind": "macos_seatbelt",
                "network_mode": manifest["permissions"]["network"]["mode"],
                "manifest_skill_id": manifest["skill_id"],
            }
        ),
    }


__all__ = ["run_manifested_skill", "seatbelt_profile"]
