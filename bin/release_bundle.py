from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tomllib
import zipfile
from typing import Any


SAFE_ROOT_FILES = (
    ".env.example",
    ".gitignore",
    "LICENSE",
    "README.md",
    "README.zh-TW.md",
    "MAGI_功能介紹手冊.docx",
    "MAGI_對外版使用說明.docx",
    "daemon.py",
    "setup_wizard.py",
    "sitecustomize.py",
    "pyproject.toml",
    "requirements.txt",
    "requirements-optional.txt",
    "requirements-windows.txt",
    "start_magi.sh",
    "start_magi.bat",
    "init_auth.sql",
    "setup_magi_brain.sql",
    "uv.lock",
)

SAFE_GENERIC_DIRS = (
    "api",
    "bin",
    "docs",
    "migrations",
    "scripts",
    "skills",
    "templates",
)

SAFE_SPECIAL_DIRS = (
    "casper_ecosystem",
    "json",
    "static",
)

SANITIZED_JSON_EXAMPLES = {
    "config.json": "config.example.json",
    "legalbridge_config.json": "legalbridge_config.example.json",
}

SAFE_JSON_FILES = (
    "holidays_config.json",
)

SAFE_STATIC_FILES = (
    "iron_dome_patterns.json",
)

SAFE_STATIC_DIRS = ()

SEED_DIRECTORIES = (
    "static/exports",
    "static/images",
    "static/audio",
)

GLOBAL_EXCLUDED_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    ".agent",
    ".magi_doc_runs",
    ".runtime_site_packages",
    ".smoke_outputs",
    ".tmp_file_review_email_parse",
    ".venv",
    ".venv_judicial",
    "venv",
    "build",
    "dist",
    "archive",
    "backups",
    "cache",
    "downloads",
    "laf_downloads",
    "logs",
    "musicgen_venv",
    "node_modules",
    ".next",
    ".git",
    "_autopilot_runs",
    "_bg_jobs",
    "_db_backups",
    "_debug_reports",
    "_logs",
    "exports",
    "reports",
}

GLOBAL_EXCLUDED_DIR_PREFIXES = (
    ".laf_chrome_profile",
    "_laf_formal_capture",
    "laf_guided_capture",
    "laf_portal_snapshot",
    "laf_official_visual_smoke",
    "eefile_snapshot_",
    "_pending_gmail_drafts",
)

GLOBAL_EXCLUDED_PATH_PREFIXES = (
    "docs/deploy/",
)

GLOBAL_EXCLUDED_PATH_GLOBS = (
    "docs/architecture/*_architecture_graph.json",
    "docs/CLAUDE_FIX_LOG_ARCHIVE.md",
    "docs/guides/MAGI_操作手冊.md",
    "skills/judgment-collector/judgments.json",
    "skills/pdf-namer/db_rules_cache.json",
)

GLOBAL_EXCLUDED_FILE_NAMES = {
    ".DS_Store",
    ".brain_ngl_hint.json",
    ".brain_state.json",
    ".draft_processed_emails.json",
    "_autopilot.lock",
    "_autopilot_nightly.err",
    "_autopilot_nightly.log",
    "_autopilot_state.json",
    "_autopilot_tick.err",
    "_autopilot_tick.log",
    "_crawl_targets.json",
    "_eventlog.jsonl",
    "_filing_log.json",
    "_laf_condition_manual_done.json",
    "_laf_folder_audit_20260221.json",
    "_statutes_vdb_state.json",
    "active_tasks.json",
    "app_log.txt",
    "cortex_sync_state.json",
    "daemon.lock",
    "downloaded_registry.json",
    "file_review_auto_state.json",
    "guardian_control.json",
    "laf_notifications.log",
    "magi_status.json",
    "nohup.out",
    "openclaw_cron_runner.lock",
    "openclaw_cron_runner.log",
    "openclaw_cron_runner_state.json",
    "osc_database.db",
    "process_guardian_state.json",
    "processed_laf_emails.json",
    "processed_laf_emails_general.json",
    "repair_report.json",
    "report.json",
    "review_cache.json",
    "server.log",
    "system_test_report.json",
    "transcript_manual_queue.jsonl",
}

GLOBAL_EXCLUDED_FILE_PREFIXES = (
    "apply_form_",
    "apply_form_inside_",
    "debug_",
    "music_fallback_",
    "~$",
)

GLOBAL_EXCLUDED_FILE_SUFFIXES = (
    ".db",
    ".db-shm",
    ".db-wal",
    ".invalid_20260216_170511",
    ".lock",
    ".log",
    ".pickle",
    ".pid",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
)

GLOBAL_EXCLUDED_FILE_CONTAINS = (
    "credentials",
    "cookie",
    "token",
)

GLOBAL_EXCLUDED_FILE_REGEXES = (
    re.compile(r"\.bak(?:[._].*)?$", re.IGNORECASE),
    re.compile(r"\.backup(?:[._].*)?$", re.IGNORECASE),
    re.compile(r"_nightly_report\.json$", re.IGNORECASE),
)

SECRET_KEYWORDS = (
    "access_token",
    "api_key",
    "bot_token",
    "channel_secret",
    "credential",
    "key",
    "password",
    "secret",
    "token",
    "webhook",
)

PATH_KEYWORDS = (
    "base_path",
    "browser_profile_dir",
    "business_card_path",
    "download_folder",
    "folder",
    "path",
    "target_folder",
)

HOST_KEYWORDS = (
    "host",
    "hostname",
)

USER_KEYWORDS = (
    "user",
    "username",
)

ABSOLUTE_PATH_RE = re.compile(r"^(?:~[/\\]|/|[A-Za-z]:[/\\])")


@dataclass
class BundleResult:
    bundle_dir: Path
    archive_path: Path
    version: str
    files_copied: int
    generated_files: list[str]


def _repo_version(source_root: Path) -> str:
    pyproject = source_root / "pyproject.toml"
    if not pyproject.exists():
        return "0.0.0"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    version = str(project.get("version") or "").strip()
    return version or "0.0.0"


def _looks_like_absolute_path(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return bool(ABSOLUTE_PATH_RE.match(text))


def _sanitize_json_payload(payload: Any, *, key: str = "") -> Any:
    key_lower = str(key or "").strip().lower()

    if isinstance(payload, dict):
        return {k: _sanitize_json_payload(v, key=str(k)) for k, v in payload.items()}

    if isinstance(payload, list):
        return [_sanitize_json_payload(item, key=key) for item in payload]

    if isinstance(payload, str):
        if any(keyword in key_lower for keyword in SECRET_KEYWORDS):
            return "<REDACTED>"
        if key_lower in HOST_KEYWORDS or any(key_lower.endswith(f"_{item}") for item in HOST_KEYWORDS):
            return "<HOST>"
        if key_lower in USER_KEYWORDS or any(key_lower.endswith(f"_{item}") for item in USER_KEYWORDS):
            return "<USER>"
        if any(keyword in key_lower for keyword in PATH_KEYWORDS) or _looks_like_absolute_path(payload):
            return "<PATH>"
        return payload

    return payload


def sanitize_json_payload(payload: Any) -> Any:
    return _sanitize_json_payload(payload)


def _skip_hidden_part(part: str) -> bool:
    return part.startswith(".") and part not in {".env.example"}


def _should_skip_dir(rel_path: PurePosixPath) -> bool:
    rel_text = rel_path.as_posix().rstrip("/") + "/"
    if any(rel_text.startswith(prefix) for prefix in GLOBAL_EXCLUDED_PATH_PREFIXES):
        return True
    parts = rel_path.parts
    for part in parts:
        if part in GLOBAL_EXCLUDED_DIR_NAMES:
            return True
        if _skip_hidden_part(part):
            return True
        if any(part.startswith(prefix) for prefix in GLOBAL_EXCLUDED_DIR_PREFIXES):
            return True
    return False


def _should_skip_file(rel_path: PurePosixPath) -> bool:
    rel_text = rel_path.as_posix()
    if any(rel_text.startswith(prefix) for prefix in GLOBAL_EXCLUDED_PATH_PREFIXES):
        return True
    if any(fnmatch.fnmatch(rel_text, pattern) for pattern in GLOBAL_EXCLUDED_PATH_GLOBS):
        return True
    if _should_skip_dir(rel_path.parent):
        return True

    name = rel_path.name
    lower_name = name.lower()

    if _skip_hidden_part(name):
        return True
    if name in GLOBAL_EXCLUDED_FILE_NAMES:
        return True
    if any(lower_name.startswith(prefix.lower()) for prefix in GLOBAL_EXCLUDED_FILE_PREFIXES):
        return True
    if any(lower_name.endswith(suffix.lower()) for suffix in GLOBAL_EXCLUDED_FILE_SUFFIXES):
        return True
    if any(keyword in lower_name for keyword in GLOBAL_EXCLUDED_FILE_CONTAINS):
        return True
    if any(rx.search(name) for rx in GLOBAL_EXCLUDED_FILE_REGEXES):
        return True

    if lower_name in {
        "_case_index.json",
        "_learned_filename_rules.json",
        "training_data.json",
        "law" + "snote_cookies.json",
    }:
        return True

    return False


def _copy_file(src: Path, dst: Path, *, copied: list[int]) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    copied[0] += 1


def _copy_generic_tree(source_root: Path, bundle_root: Path, top_level_name: str, *, copied: list[int]) -> None:
    src_root = source_root / top_level_name
    if not src_root.is_dir():
        return

    for current, dirnames, filenames in os.walk(src_root):
        current_path = Path(current)
        rel_current = PurePosixPath(top_level_name) / PurePosixPath(current_path.relative_to(src_root).as_posix())

        kept_dirs: list[str] = []
        for dirname in sorted(dirnames):
            rel_dir = rel_current / dirname
            if not _should_skip_dir(rel_dir):
                kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in sorted(filenames):
            rel_file = rel_current / filename
            if _should_skip_file(rel_file):
                continue
            src_file = current_path / filename
            dst_file = bundle_root / rel_file.as_posix()
            _copy_file(src_file, dst_file, copied=copied)


def _copy_static_tree(source_root: Path, bundle_root: Path, *, copied: list[int]) -> None:
    src_root = source_root / "static"
    if not src_root.is_dir():
        return

    dst_root = bundle_root / "static"
    dst_root.mkdir(parents=True, exist_ok=True)

    for filename in SAFE_STATIC_FILES:
        src_file = src_root / filename
        if src_file.is_file() and not _should_skip_file(PurePosixPath("static") / filename):
            _copy_file(src_file, dst_root / filename, copied=copied)

    for dirname in SAFE_STATIC_DIRS:
        src_dir = src_root / dirname
        if src_dir.is_dir():
            _copy_generic_tree(source_root, bundle_root, f"static/{dirname}", copied=copied)

    for rel_dir in SEED_DIRECTORIES:
        (bundle_root / rel_dir).mkdir(parents=True, exist_ok=True)


def _copy_json_tree(source_root: Path, bundle_root: Path, *, copied: list[int], generated_files: list[str]) -> None:
    src_root = source_root / "json"
    if not src_root.exists():
        return

    dst_root = bundle_root / "json"
    dst_root.mkdir(parents=True, exist_ok=True)

    for filename in SAFE_JSON_FILES:
        src_file = src_root / filename
        if src_file.is_file():
            _copy_file(src_file, dst_root / filename, copied=copied)

    for source_name, target_name in SANITIZED_JSON_EXAMPLES.items():
        src_file = src_root / source_name
        if not src_file.is_file():
            continue
        try:
            payload = json.loads(src_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        sanitized = sanitize_json_payload(payload)
        target_path = dst_root / target_name
        target_path.write_text(
            json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        generated_files.append(str(target_path.relative_to(bundle_root)))


def _copy_casper_ecosystem(source_root: Path, bundle_root: Path, *, copied: list[int]) -> None:
    src_root = source_root / "casper_ecosystem" / "law_firm_orchestrators"
    if not src_root.is_dir():
        return

    dst_root = bundle_root / "casper_ecosystem" / "law_firm_orchestrators"
    dst_root.mkdir(parents=True, exist_ok=True)

    for src_file in sorted(src_root.iterdir()):
        if not src_file.is_file():
            continue
        if src_file.name == "legalbridge_core.py":
            continue
        if src_file.suffix not in {".py", ".md"}:
            continue
        if src_file.name.startswith(("test_", "mock_")):
            continue
        rel = PurePosixPath("casper_ecosystem") / "law_firm_orchestrators" / src_file.name
        if _should_skip_file(rel):
            continue
        _copy_file(src_file, dst_root / src_file.name, copied=copied)


def _write_manifest(bundle_root: Path, *, version: str, files_copied: int, generated_files: list[str]) -> Path:
    manifest_path = bundle_root / "RELEASE_MANIFEST.json"
    payload = {
        "bundle_name": bundle_root.name,
        "version": version,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "files_copied": files_copied,
        "generated_files": generated_files,
        "seed_directories": list(SEED_DIRECTORIES),
        "policy": {
            "safe_root_files": list(SAFE_ROOT_FILES),
            "safe_generic_dirs": list(SAFE_GENERIC_DIRS),
            "safe_special_dirs": list(SAFE_SPECIAL_DIRS),
        },
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def _archive_bundle(bundle_root: Path, archive_path: Path) -> Path:
    if archive_path.exists():
        archive_path.unlink()
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(bundle_root.rglob("*")):
            arcname = PurePosixPath(bundle_root.name) / PurePosixPath(path.relative_to(bundle_root).as_posix())
            zf.write(path, arcname.as_posix())
    return archive_path


def build_release_bundle(
    source_root: Path,
    output_root: Path,
    *,
    bundle_name: str | None = None,
    force: bool = False,
) -> BundleResult:
    source_root = source_root.resolve()
    output_root = output_root.resolve()
    version = _repo_version(source_root)

    if not bundle_name:
        stamp = datetime.now().strftime("%Y%m%d")
        bundle_name = f"MAGI-external-{version}-{stamp}"

    bundle_root = output_root / bundle_name
    archive_path = output_root / f"{bundle_name}.zip"

    if bundle_root.exists():
        if not force:
            raise FileExistsError(f"bundle already exists: {bundle_root}")
        shutil.rmtree(bundle_root)

    output_root.mkdir(parents=True, exist_ok=True)
    bundle_root.mkdir(parents=True, exist_ok=True)

    copied = [0]
    generated_files: list[str] = []

    for filename in SAFE_ROOT_FILES:
        src_file = source_root / filename
        if src_file.is_file():
            _copy_file(src_file, bundle_root / filename, copied=copied)

    for dirname in SAFE_GENERIC_DIRS:
        _copy_generic_tree(source_root, bundle_root, dirname, copied=copied)

    _copy_casper_ecosystem(source_root, bundle_root, copied=copied)
    _copy_json_tree(source_root, bundle_root, copied=copied, generated_files=generated_files)
    _copy_static_tree(source_root, bundle_root, copied=copied)

    manifest_path = _write_manifest(
        bundle_root,
        version=version,
        files_copied=copied[0],
        generated_files=generated_files,
    )
    generated_files.append(str(manifest_path.relative_to(bundle_root)))

    _archive_bundle(bundle_root, archive_path)

    return BundleResult(
        bundle_dir=bundle_root,
        archive_path=archive_path,
        version=version,
        files_copied=copied[0],
        generated_files=generated_files,
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a customer-safe MAGI release bundle.")
    parser.add_argument(
        "--source-root",
        default=str(Path(__file__).resolve().parent.parent),
        help="MAGI source tree to package",
    )
    parser.add_argument(
        "--output-root",
        default=str(Path(__file__).resolve().parent.parent / "dist" / "release"),
        help="Directory to place the cleaned bundle and archive",
    )
    parser.add_argument(
        "--bundle-name",
        default="",
        help="Override the output bundle directory/archive name",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing bundle with the same name",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    result = build_release_bundle(
        Path(args.source_root),
        Path(args.output_root),
        bundle_name=(args.bundle_name or None),
        force=bool(args.force),
    )

    print(f"[MAGI] Release bundle ready: {result.bundle_dir}")
    print(f"[MAGI] Archive ready: {result.archive_path}")
    print(f"[MAGI] Version: {result.version}")
    print(f"[MAGI] Files copied: {result.files_copied}")
    if result.generated_files:
        print("[MAGI] Generated:")
        for item in result.generated_files:
            print(f"  - {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
