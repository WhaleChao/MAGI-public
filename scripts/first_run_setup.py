#!/usr/bin/env python3
"""First-run setup guide for MAGI.

This script is intentionally conservative: it reports what a new operator must
configure without printing secret values. With --write-env it creates a local
.env from .env.example and fills only generated local secrets.
"""

from __future__ import annotations

import argparse
import json
import platform
import secrets
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
ENV_FILE = REPO_ROOT / ".env"

REQUIRED_ENV_KEYS = ("FLASK_SECRET_KEY", "MAGI_API_KEY", "DB_HOST", "DB_USER", "DB_PASSWORD")
OPTIONAL_CHANNEL_KEYS = (
    "DISCORD_BOT_TOKEN",
    "OPENCLAW_TELEGRAM_BOT_TOKEN",
    "MAGI_LINE_CHANNEL_ACCESS_TOKEN",
)
PUBLIC_BLOCKED_MARKERS = ("law" + "snote", "WHALE" + "LAWYER", "whale" + "lawyer", "lumi" + "63181107")


@dataclass(frozen=True)
class SetupItem:
    key: str
    title: str
    status: str
    detail: str
    action: str = ""


def _parse_env(path: Path = ENV_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _is_placeholder(value: str) -> bool:
    v = str(value or "").strip()
    if not v:
        return True
    lowered = v.lower()
    return (
        v.startswith("<")
        or "<<replace_with" in lowered
        or "your-" in lowered
        or v in {"changeme", "replace-me", "random-hex-string"}
    )


def _write_env_from_example(env_path: Path = ENV_FILE, *, overwrite: bool = False) -> dict[str, Any]:
    if env_path.exists() and not overwrite:
        return {"ok": True, "created": False, "path": str(env_path)}
    if not ENV_EXAMPLE.exists():
        return {"ok": False, "created": False, "error": ".env.example not found"}
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    replacements = {
        "FLASK_SECRET_KEY=<random-hex-string>": f"FLASK_SECRET_KEY={secrets.token_hex(32)}",
        "MAGI_API_KEY=<random-hex-string>": f"MAGI_API_KEY={secrets.token_hex(32)}",
        "MAGI_ROOT_DIR=/path/to/MAGI_v2": f"MAGI_ROOT_DIR={REPO_ROOT}",
        "MAGI_SKILL_PYTHON=/path/to/MAGI_v2/venv/bin/python3": f"MAGI_SKILL_PYTHON={REPO_ROOT / 'venv' / 'bin' / 'python3'}",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    env_path.write_text(text, encoding="utf-8")
    return {"ok": True, "created": True, "path": str(env_path)}


def _tracked_files() -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO_ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return [line for line in proc.stdout.splitlines() if line]
    except Exception:
        return []


def _public_isolation_findings() -> list[str]:
    findings: list[str] = []
    for rel in _tracked_files():
        if rel == ".gitignore":
            continue
        lower = rel.lower()
        if any(marker.lower() in lower for marker in PUBLIC_BLOCKED_MARKERS):
            findings.append(rel)
    return findings


def build_first_run_checklist(*, public_mode: bool = False, env_path: Path = ENV_FILE) -> dict[str, Any]:
    env = _parse_env(env_path)
    missing_required = [key for key in REQUIRED_ENV_KEYS if _is_placeholder(env.get(key, ""))]
    configured_channels = [key for key in OPTIONAL_CHANNEL_KEYS if not _is_placeholder(env.get(key, ""))]
    public_findings = _public_isolation_findings() if public_mode else []
    venv_paths = [REPO_ROOT / "venv", REPO_ROOT / ".venv"]
    items = [
        SetupItem(
            "python",
            "Python 版本",
            "pass" if sys.version_info >= (3, 10) else "fail",
            platform.python_version(),
            "安裝 Python 3.10 以上版本。",
        ),
        SetupItem(
            "virtualenv",
            "虛擬環境",
            "pass" if any(path.exists() for path in venv_paths) else "warn",
            " / ".join(str(path) for path in venv_paths),
            "執行 python3 scripts/install_magi.py --yes。",
        ),
        SetupItem(
            "env_file",
            "本機 .env",
            "pass" if env_path.exists() else "warn",
            str(env_path),
            "執行 python3 scripts/first_run_setup.py --write-env。",
        ),
        SetupItem(
            "required_env",
            "必要設定",
            "pass" if not missing_required else "warn",
            "ok" if not missing_required else "缺少：" + ", ".join(missing_required),
            "編輯 .env；不要把 .env 加進 git。",
        ),
        SetupItem(
            "channels",
            "通訊軟體",
            "pass" if configured_channels else "warn",
            "已設定：" + ", ".join(configured_channels) if configured_channels else "尚未設定，可先略過。",
            "需要 Discord/Telegram/LINE 時再填 token。",
        ),
        SetupItem(
            "doctor",
            "本機偵測",
            "pending",
            "python3 scripts/magi_doctor.py --json",
            "完成 .env 後執行。",
        ),
        SetupItem(
            "public_isolation",
            "公開版隔離",
            "pass" if not public_findings else "fail",
            "ok" if not public_findings else "疑似私有項目：" + ", ".join(public_findings[:8]),
            "公開推送前執行 scripts/public_release_audit.py --public-isolation --strict。",
        ),
    ]
    if not public_mode:
        items.append(
            SetupItem(
                "private_modules",
                "私用模組",
                "guarded",
                "OSC/法扶/內部資料路徑只在私用版依 .env 啟用。",
                "公開版不要填入私人 NAS、portal 或 DB 憑證。",
            )
        )
    summary = {
        "pass": sum(1 for item in items if item.status == "pass"),
        "warn": sum(1 for item in items if item.status == "warn"),
        "fail": sum(1 for item in items if item.status == "fail"),
        "pending": sum(1 for item in items if item.status == "pending"),
        "guarded": sum(1 for item in items if item.status == "guarded"),
    }
    return {
        "ok": summary["fail"] == 0,
        "public_mode": public_mode,
        "env_path": str(env_path),
        "summary": summary,
        "items": [asdict(item) for item in items],
        "next_commands": [
            "python3 scripts/install_magi.py --dry-run",
            "python3 scripts/first_run_setup.py --write-env",
            "python3 scripts/magi_doctor.py --json",
            "python3 scripts/public_release_audit.py --public-isolation --strict",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guide first-time MAGI setup without exposing secrets.")
    parser.add_argument("--write-env", action="store_true", help="create .env from .env.example if missing")
    parser.add_argument("--overwrite-env", action="store_true", help="overwrite .env when used with --write-env")
    parser.add_argument("--public", action="store_true", help="run public-release isolation checks")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--output", type=Path, help="write checklist JSON to this path")
    args = parser.parse_args(argv)

    write_result = None
    if args.write_env:
        write_result = _write_env_from_example(overwrite=args.overwrite_env)
    payload = build_first_run_checklist(public_mode=args.public)
    if write_result is not None:
        payload["write_env"] = write_result
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"MAGI first-run setup: {'OK' if payload['ok'] else 'CHECK NEEDED'} {payload['summary']}")
        for item in payload["items"]:
            print(f"- {item['status'].upper():7} {item['title']}: {item['detail']}")
        print("Next:")
        for command in payload["next_commands"]:
            print(f"  {command}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
