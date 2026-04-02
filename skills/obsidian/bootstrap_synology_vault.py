#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bootstrap a local Obsidian vault that exposes Synology/MariaDB-backed sources
as stable source links for MAGI.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import sys

MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from api.case_path_mapper import default_case_roots, preferred_case_roots


HOME = Path.home()
DEFAULT_VAULT = HOME / "Documents" / "MAGI_Obsidian_Vault"
AGENT_DIR = MAGI_ROOT / ".agent"
VAULT_CONFIG_PATH = AGENT_DIR / "obsidian_vault_config.json"

_CASE_ROOTS = preferred_case_roots(include_closed=True)
_FALLBACK_CASE_ROOTS = default_case_roots(include_closed=True)
_ACTIVE_CASE_ROOT = Path(_CASE_ROOTS[0] if _CASE_ROOTS else (_FALLBACK_CASE_ROOTS[0] if _FALLBACK_CASE_ROOTS else HOME / "Library" / "CloudStorage" / "SynologyDrive-homes" / "01_案件"))
_CLOSED_CASE_ROOT = Path(_CASE_ROOTS[1] if len(_CASE_ROOTS) > 1 else (_FALLBACK_CASE_ROOTS[1] if len(_FALLBACK_CASE_ROOTS) > 1 else _ACTIVE_CASE_ROOT))


SOURCE_SPECS = [
    {
        "name": "案件",
        "description": "MariaDB folder_path 對應的現行案件來源。",
        "candidates": [
            _ACTIVE_CASE_ROOT,
        ],
    },
    {
        "name": "結案",
        "description": "結案歸檔來源。",
        "candidates": [
            _CLOSED_CASE_ROOT,
        ],
    },
    {
        "name": "舊案",
        "description": "lumi 結案區中的舊案來源。",
        "candidates": [
            _CLOSED_CASE_ROOT / "舊案",
        ],
    },
    {
        "name": "fang",
        "description": "fang 資料夾全文獻來源。",
        "candidates": [
            Path("/Volumes/lumi/fang"),
        ],
    },
]


def pick_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def ensure_symlink(link_path: Path, target_path: Path) -> None:
    if link_path.is_symlink():
        if os.path.realpath(link_path) == str(target_path.resolve()):
            return
        link_path.unlink()
    elif link_path.exists():
        raise RuntimeError(f"{link_path} already exists and is not a symlink")
    link_path.symlink_to(target_path)


def directory_snapshot(path: Path, limit: int = 12) -> tuple[int, list[str]]:
    try:
        entries = sorted(
            (
                p
                for p in path.iterdir()
                if p.name and not p.name.startswith(".") and not p.name.startswith("._")
            ),
            key=lambda p: p.name.lower(),
        )
    except Exception:
        return 0, []
    preview = []
    for entry in entries[:limit]:
        suffix = "/" if entry.is_dir() else ""
        preview.append(f"{entry.name}{suffix}")
    return len(entries), preview


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def set_magi_vault(vault_path: Path) -> None:
    AGENT_DIR.mkdir(exist_ok=True)
    payload = {
        "vault_path": str(vault_path),
        "vault_name": vault_path.name,
        "set_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    VAULT_CONFIG_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_home_note(vault_path: Path, sources: list[dict]) -> str:
    lines = [
        "# MAGI Obsidian Vault",
        "",
        f"- Vault path: `{vault_path}`",
        "- Purpose: 把 Synology / MariaDB 路徑當成穩定來源層，向量資料庫只做可重建索引。",
        "- Import mode: 使用來源連結，不直接複製大量原始檔進 vault。",
        "",
        "## Source Roots",
        "",
    ]
    for src in sources:
        state = "ready" if src["resolved_path"] else "missing"
        lines.extend(
            [
                f"### {src['name']}",
                f"- State: `{state}`",
                f"- Description: {src['description']}",
                f"- Source path: `{src['resolved_path'] or 'NOT_FOUND'}`",
                f"- Vault link: `10_Sources/{src['name']}`",
                "",
            ]
        )
    lines.extend(
        [
            "## How To Use",
            "",
            "- 在 Obsidian 裡直接開這個 vault，即可瀏覽 `10_Sources/` 底下的來源連結。",
            "- 在 MAGI 內使用 `obsidian search`、`obsidian read`、`obsidian ingest`、`obsidian ask`。",
            "- 若要重新整理來源索引，重跑 `bootstrap_synology_vault.py`。",
            "",
            "## Stability",
            "",
            "- Vault / 原始檔 是 source of truth。",
            "- 向量資料庫只應保存 embeddings 與 chunk 索引，壞掉可重建。",
            "",
        ]
    )
    return "\n".join(lines)


def build_source_index(source: dict) -> str:
    lines = [
        f"# {source['name']} Source Index",
        "",
        f"- Description: {source['description']}",
        f"- Source path: `{source['resolved_path'] or 'NOT_FOUND'}`",
        f"- Generated at: `{time.strftime('%Y-%m-%d %H:%M:%S')}`",
        "",
    ]
    if not source["resolved_path"]:
        lines.append("- Status: source path not found on this machine.")
        return "\n".join(lines)

    lines.extend(
        [
            f"- Direct child count: `{source['child_count']}`",
            "",
            "## Preview",
            "",
        ]
    )
    for item in source["preview"]:
        lines.append(f"- `{item}`")
    if not source["preview"]:
        lines.append("- `(empty)`")
    lines.extend(
        [
            "",
            "## Note",
            "",
            "- 這裡先建立來源索引與檔案連結，不做大規模內容轉檔。",
            "- 若要做 NotebookLM-style 問答，下一步應對選定目錄做抽取與 selective ingest。",
            "",
        ]
    )
    return "\n".join(lines)


def bootstrap(vault_path: Path) -> dict:
    vault_path.mkdir(parents=True, exist_ok=True)
    for rel in ("00_Admin", "10_Sources", "20_Notes", "30_Index"):
        (vault_path / rel).mkdir(parents=True, exist_ok=True)

    resolved_sources: list[dict] = []
    for spec in SOURCE_SPECS:
        resolved = pick_existing(spec["candidates"])
        info = {
            "name": spec["name"],
            "description": spec["description"],
            "resolved_path": str(resolved) if resolved else "",
            "child_count": 0,
            "preview": [],
        }
        if resolved:
            ensure_symlink(vault_path / "10_Sources" / spec["name"], resolved)
            count, preview = directory_snapshot(resolved)
            info["child_count"] = count
            info["preview"] = preview
        resolved_sources.append(info)

    write_text(vault_path / "00_Home.md", build_home_note(vault_path, resolved_sources))
    for src in resolved_sources:
        write_text(vault_path / "30_Index" / f"{src['name']}.md", build_source_index(src))

    manifest = {
        "vault_path": str(vault_path),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sources": resolved_sources,
    }
    write_text(
        vault_path / "00_Admin" / "source_manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )
    set_magi_vault(vault_path)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap MAGI Obsidian vault from Synology sources")
    parser.add_argument("--vault-path", default=str(DEFAULT_VAULT))
    args = parser.parse_args()

    manifest = bootstrap(Path(args.vault_path).expanduser().resolve())
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
