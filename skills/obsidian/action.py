#!/usr/bin/env python3
import logging
# -*- coding: utf-8 -*-
"""
MAGI Obsidian Integration Skill

Tasks:
  status         - Show vault config and index stats
  set_vault      - Configure vault path
  list_vaults    - Discover vaults from Obsidian config
  search         - Search note names and content
  read           - Read a specific note
  ingest         - Ingest notes into vector memory (dedup by hash)
  ingest_source  - Selective ingest from Synology source roots (Phase 2)
  ask            - Q&A over indexed notes with citations
"""

import argparse
from magi_v3 import fcntl_compat as fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
MAGI_ROOT = os.path.abspath(os.path.join(SKILL_DIR, "..", ".."))
if MAGI_ROOT not in sys.path:
    sys.path.insert(0, MAGI_ROOT)

from api.case_path_mapper import default_case_roots, preferred_case_roots
from skills.bridge.shared_utils.judgment_folder_names import judgment_folder_name

# ── Config ──────────────────────────────────────────────────────────

def _resolve_agent_dir() -> Path:
    raw = (
        os.environ.get("MAGI_OBSIDIAN_AGENT_DIR")
        or os.environ.get("MAGI_SHARED_AGENT_DIR")
        or os.environ.get("MAGI_AGENT_DIR")
        or ""
    ).strip()
    if raw:
        return Path(raw).expanduser()
    return Path(MAGI_ROOT) / ".agent"


AGENT_DIR = _resolve_agent_dir()
AGENT_DIR.mkdir(exist_ok=True)

INDEX_PATH = AGENT_DIR / "obsidian_index.json"
VAULT_CONFIG_PATH = AGENT_DIR / "obsidian_vault_config.json"
OBSIDIAN_APP_CONFIG = Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"
OBSIDIAN_CLI = os.environ.get("OBSIDIAN_CLI", "obsidian-cli")

CHUNK_CHARS = int(os.environ.get("MAGI_OBSIDIAN_CHUNK_CHARS", "1200"))
CHUNK_OVERLAP = int(os.environ.get("MAGI_OBSIDIAN_CHUNK_OVERLAP", "120"))
CHUNK_CAP = int(os.environ.get("MAGI_OBSIDIAN_CHUNK_CAP", "999999"))

IGNORE_FOLDERS = {".obsidian", ".trash", ".git", "node_modules", "__pycache__"}
IGNORE_PREFIXES = ("_template", "Template")
MAX_NOTE_BYTES = int(os.environ.get("MAGI_OBSIDIAN_MAX_NOTE_BYTES", str(1024 * 1024)))  # 1MB

# ── Source ingest folder filters ─────────────────────────────────
# Default exclude: low-value admin folders and bulk scanned files
DEFAULT_EXCLUDE_FOLDERS = {
    "00_委任狀",
    "01_法扶資料",
    "02_開辦資料",
    "03_結案資料",
    "06_閱卷資料",
    "11_回執",
}
# High-value folders (used when --include-folders is "high-value")
HIGH_VALUE_FOLDERS = {
    "04_我方歷次書狀",
    "05_對方歷次書狀",
    "07_證據資料",
    "08_筆錄",
    "09_法院通知或程序裁定",
    judgment_folder_name(10),
    "12_信件往返",
    "13_電子筆錄",
}
MIN_EXTRACTED_CHARS = int(os.environ.get("MAGI_INGEST_MIN_CHARS", "50"))
_KNOWN_MALFORMED_PDF_HINTS = (
    "may not be a pdf file",
    "malformed",
    "all pdf extractors failed",
    "no_extractable_text_after_pdftotext_fitz_pdfplumber_ocr",
    "[pdf 提取失敗",
    "pdf 提取失敗",
    "cannot find xref",
    "couldn't find trailer dictionary",
    "couldn't read xref table",
    "failed to open file",
    "cannot open broken document",
    "xref table",
)


def _is_known_malformed_pdf_skip(path: Path, error_text: str) -> bool:
    if path.suffix.lower() != ".pdf":
        return False
    msg = str(error_text or "").strip().lower()
    if not msg:
        return False
    return any(token in msg for token in _KNOWN_MALFORMED_PDF_HINTS)


# ── Vault Management ───────────────────────────────────────────────

def _load_vault_config() -> Dict:
    if VAULT_CONFIG_PATH.exists():
        try:
            return json.loads(VAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 84, exc_info=True)
    return {}


def _save_vault_config(cfg: Dict):
    VAULT_CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_index() -> Dict:
    if INDEX_PATH.exists():
        try:
            return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 97, exc_info=True)
    return {"notes": {}, "updated_at": ""}


def _save_index(idx: Dict):
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = INDEX_PATH.with_suffix(INDEX_PATH.suffix + ".lock")
    tmp_path = INDEX_PATH.with_suffix(f"{INDEX_PATH.suffix}.{os.getpid()}.{time.time_ns()}.tmp")
    with open(lock_path, "a") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            merged = {"notes": {}, "updated_at": ""}
            if INDEX_PATH.exists():
                try:
                    existing = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
                    if isinstance(existing, dict):
                        merged.update(existing)
                        merged["notes"] = dict(existing.get("notes") or {})
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 132, exc_info=True)
            merged["notes"].update(idx.get("notes") or {})
            for key, value in idx.items():
                if key != "notes":
                    merged[key] = value
            merged["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            tmp_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_path, INDEX_PATH)
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass


def _replace_index(idx: Dict):
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(idx or {})
    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    lock_path = INDEX_PATH.with_suffix(INDEX_PATH.suffix + ".lock")
    tmp_path = INDEX_PATH.with_suffix(f"{INDEX_PATH.suffix}.{os.getpid()}.{time.time_ns()}.tmp")
    with open(lock_path, "a") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_path, INDEX_PATH)
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass


def _prune_missing_index_entries(idx: Dict, vault: Path, *, dry_run: bool = False) -> List[str]:
    """Remove note index rows whose vault files no longer exist."""
    pruned: List[str] = []
    notes = idx.setdefault("notes", {})
    for rel in list(notes.keys()):
        if not isinstance(rel, str) or not rel.strip():
            continue
        path = Path(rel)
        full_path = path if path.is_absolute() else vault / rel
        if full_path.exists():
            continue
        pruned.append(rel)
        if not dry_run:
            notes.pop(rel, None)
    return sorted(pruned)


def _get_vault_path() -> Optional[Path]:
    cfg = _load_vault_config()
    vp = cfg.get("vault_path")
    if vp and Path(vp).is_dir():
        return Path(vp)
    return None


def _has_obsidian_cli() -> bool:
    try:
        subprocess.run([OBSIDIAN_CLI, "--version"], capture_output=True, timeout=5)
        return True
    except Exception:
        return False


def task_list_vaults() -> Dict:
    """Discover vaults from Obsidian desktop config."""
    vaults = []
    if OBSIDIAN_APP_CONFIG.exists():
        try:
            data = json.loads(OBSIDIAN_APP_CONFIG.read_text(encoding="utf-8"))
            raw = data.get("vaults", {})
            for vid, info in raw.items():
                vaults.append({
                    "id": vid,
                    "path": info.get("path", ""),
                    "open": info.get("open", False),
                })
        except Exception as e:
            return {"success": False, "error": f"Failed to read Obsidian config: {e}"}
    else:
        return {
            "success": True,
            "vaults": [],
            "message": "Obsidian desktop config not found. Use --task set_vault --vault-path <path> to set manually.",
        }
    return {"success": True, "vaults": vaults}


def task_set_vault(vault_path: str) -> Dict:
    """Set the active vault path."""
    p = Path(vault_path).expanduser().resolve()
    if not p.is_dir():
        return {"success": False, "error": f"Not a directory: {p}"}
    cfg = _load_vault_config()
    cfg["vault_path"] = str(p)
    cfg["vault_name"] = p.name
    cfg["set_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _save_vault_config(cfg)
    return {"success": True, "vault_path": str(p), "vault_name": p.name}


def task_status() -> Dict:
    """Show vault config and index stats."""
    cfg = _load_vault_config()
    idx = _load_index()
    vault_path = _get_vault_path()
    note_count = 0
    if vault_path:
        note_count = sum(1 for _ in vault_path.rglob("*.md")
                         if not any(part in IGNORE_FOLDERS for part in _.parts))
    return {
        "success": True,
        "vault_configured": vault_path is not None,
        "vault_path": str(vault_path) if vault_path else None,
        "vault_name": cfg.get("vault_name"),
        "notes_on_disk": note_count,
        "notes_indexed": len(idx.get("notes", {})),
        "last_index_update": idx.get("updated_at", "never"),
        "obsidian_cli_available": _has_obsidian_cli(),
    }


# ── Search ─────────────────────────────────────────────────────────

def _list_notes(vault: Path, folder: str = "") -> List[Path]:
    """List all .md files in vault, optionally scoped to a folder."""
    base = vault / folder if folder else vault
    if not base.is_dir():
        return []
    notes = []
    for f in base.rglob("*.md"):
        rel_parts = f.relative_to(vault).parts
        if any(part in IGNORE_FOLDERS for part in rel_parts):
            continue
        if any(f.name.startswith(p) for p in IGNORE_PREFIXES):
            continue
        if f.stat().st_size > MAX_NOTE_BYTES:
            continue
        notes.append(f)
    return sorted(notes)


def task_search(query: str, vault_path: Optional[Path] = None) -> Dict:
    """Search note names and content."""
    vault = vault_path or _get_vault_path()
    if not vault:
        return {"success": False, "error": "No vault configured. Use --task set_vault first."}

    q_lower = query.lower()
    results = []

    # Name search
    for note in _list_notes(vault):
        rel = str(note.relative_to(vault))
        if q_lower in rel.lower():
            results.append({"path": rel, "match": "name", "snippet": ""})

    # Content search (limit to avoid scanning huge vaults)
    content_limit = 200
    scanned = 0
    for note in _list_notes(vault):
        if scanned >= content_limit:
            break
        scanned += 1
        rel = str(note.relative_to(vault))
        # Skip if already matched by name
        if any(r["path"] == rel for r in results):
            continue
        try:
            text = note.read_text(encoding="utf-8", errors="replace")
            idx = text.lower().find(q_lower)
            if idx >= 0:
                start = max(0, idx - 40)
                end = min(len(text), idx + len(query) + 80)
                snippet = text[start:end].replace("\n", " ").strip()
                results.append({"path": rel, "match": "content", "snippet": snippet})
        except Exception:
            continue

    return {"success": True, "query": query, "results": results[:50]}


def task_read(note_path: str, vault_path: Optional[Path] = None) -> Dict:
    """Read a specific note."""
    vault = vault_path or _get_vault_path()
    if not vault:
        return {"success": False, "error": "No vault configured."}

    target = vault / note_path
    if not target.exists():
        # Try with .md extension
        target = vault / (note_path + ".md")
    if not target.exists():
        return {"success": False, "error": f"Note not found: {note_path}"}
    if not str(target.resolve()).startswith(str(vault.resolve())):
        return {"success": False, "error": "Path traversal denied."}

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        return {
            "success": True,
            "path": str(target.relative_to(vault)),
            "size": len(content),
            "content": content,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Ingest ─────────────────────────────────────────────────────────

def _note_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---", re.DOTALL)


def _parse_frontmatter_tags(content: str) -> List[str]:
    """Extract tags from YAML frontmatter.

    Supports two common formats:
      tags: [tag1, tag2]
      tags:
        - tag1
        - tag2
    Also handles 'tag:' (singular) as alias.
    """
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return []
    fm_text = m.group(1)
    tags: List[str] = []
    # Try to find a tags/tag line
    for line in fm_text.split("\n"):
        stripped = line.strip()
        if stripped.lower().startswith("tags:") or stripped.lower().startswith("tag:"):
            # Inline list: tags: [a, b, c] or tags: a, b, c
            value = stripped.split(":", 1)[1].strip()
            if value:
                # Remove brackets if present
                value = value.strip("[]")
                for t in value.split(","):
                    t = t.strip().strip("'\"")
                    if t:
                        tags.append(t)
        elif stripped.startswith("- ") and tags is not None:
            # YAML list continuation (only if we already found a tags key)
            # We check by looking if the previous non-list line was tags:
            t = stripped[2:].strip().strip("'\"")
            if t:
                tags.append(t)
    return tags


def _note_has_tags(content: str, required_tags: List[str]) -> bool:
    """Check if a note's frontmatter contains any of the required tags."""
    note_tags = [t.lower() for t in _parse_frontmatter_tags(content)]
    return any(rt.lower() in note_tags for rt in required_tags)


def task_ingest(
    folder: str = "",
    vault_path: Optional[Path] = None,
    force: bool = False,
    tags: Optional[List[str]] = None,
    since: Optional[str] = None,
) -> Dict:
    """Ingest notes into MAGI vector memory with dedup.

    Supports incremental sync modes:
      - folder: only ingest notes under a specific folder
      - tags:   only ingest notes whose frontmatter contains matching tags
      - since:  only ingest notes modified after this ISO date (e.g. '2026-03-01')
    """
    vault = vault_path or _get_vault_path()
    if not vault:
        return {"success": False, "error": "No vault configured."}

    # Parse --since date threshold
    since_ts: Optional[float] = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
            since_ts = since_dt.timestamp()
        except ValueError:
            return {"success": False, "error": f"Invalid --since date format: {since}. Use ISO format like 2026-03-01."}

    try:
        from skills.documents.vector_pipeline import ingest_text_to_vector_memory
    except ImportError:
        return {"success": False, "error": "vector_pipeline not available"}

    idx = _load_index()
    notes_map = idx.get("notes", {})
    vault_name = vault.name

    notes = _list_notes(vault, folder)
    try:
        note_limit = int(os.environ.get("MAGI_OBSIDIAN_INGEST_NOTE_LIMIT", "0") or 0)
    except ValueError:
        note_limit = 0
    try:
        checkpoint_every = int(os.environ.get("MAGI_OBSIDIAN_CHECKPOINT_EVERY", "10") or 10)
    except ValueError:
        checkpoint_every = 10
    checkpoint_every = max(1, checkpoint_every)

    def _indexed_chunks(rel: str) -> int:
        try:
            return int((notes_map.get(rel) or {}).get("chunks", 0) or 0)
        except (TypeError, ValueError):
            return 0

    if os.environ.get("MAGI_OBSIDIAN_INGEST_ZERO_CHUNKS_FIRST", "0").strip() == "1":
        notes = sorted(
            notes,
            key=lambda n: (
                0 if _indexed_chunks(str(n.relative_to(vault))) <= 0 else 1,
                str(n.relative_to(vault)),
            ),
        )

    # Apply --since filter (by file mtime)
    if since_ts is not None:
        notes = [n for n in notes if n.stat().st_mtime >= since_ts]
    if note_limit > 0:
        notes = notes[:note_limit]

    ingested = 0
    skipped = 0
    filtered_by_tag = 0
    errors = []
    total_chunks = 0
    checkpoint_writes = 0

    for note in notes:
        if total_chunks >= CHUNK_CAP:
            break

        rel = str(note.relative_to(vault))
        try:
            content = note.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            errors.append({"path": rel, "error": str(e)})
            continue

        # Apply --tags filter (by frontmatter)
        if tags and not _note_has_tags(content, tags):
            filtered_by_tag += 1
            continue

        h = _note_hash(content)
        mtime = int(note.stat().st_mtime)

        # Dedup check
        prev = notes_map.get(rel, {})
        prev_chunks = _indexed_chunks(rel)
        if not force and prev.get("hash") == h and prev.get("mtime") == mtime and prev_chunks > 0:
            skipped += 1
            continue

        # Build source metadata
        title = note.stem
        remaining_cap = CHUNK_CAP - total_chunks

        try:
            result = ingest_text_to_vector_memory(
                kind="obsidian",
                primary=f"obsidian|vault={vault_name}|path={rel}",
                title=title,
                text=content,
                chunk_chars=CHUNK_CHARS,
                overlap=CHUNK_OVERLAP,
                max_chunks_total=min(remaining_cap, 50),
            )
            if result.get("success"):
                chunks_written = result.get("chunks_written", 0)
                total_chunks += chunks_written
                notes_map[rel] = {
                    "hash": h,
                    "mtime": mtime,
                    "doc_key": result.get("doc_key", ""),
                    "chunks": chunks_written,
                    "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                ingested += 1
                if checkpoint_every and ingested % checkpoint_every == 0:
                    idx["notes"] = notes_map
                    _save_index(idx)
                    checkpoint_writes += 1
            else:
                errors.append({"path": rel, "error": result.get("error", "unknown")})
        except Exception as e:
            errors.append({"path": rel, "error": str(e)})

    idx["notes"] = notes_map
    _save_index(idx)

    result_dict: Dict = {
        "success": True,
        "vault": vault_name,
        "folder": folder or "(all)",
        "ingested": ingested,
        "skipped": skipped,
        "errors": len(errors),
        "total_chunks": total_chunks,
        "notes_considered": len(notes),
        "checkpoint_writes": checkpoint_writes,
        "error_details": errors[:10] if errors else [],
    }
    if note_limit > 0:
        result_dict["note_limit"] = note_limit
    if tags:
        result_dict["tags_filter"] = tags
        result_dict["filtered_by_tag"] = filtered_by_tag
    if since:
        result_dict["since"] = since

    return result_dict


# ── Phase 2: Selective Source Ingest ───────────────────────────────

# Source root mapping (from source_manifest.json).  Short-lived tasks such as
# ``sync_case_notes`` do not read case files, so an explicitly probe-free
# process must not touch SMB/File Provider mounts merely by importing this
# module.  File-ingest tasks still perform the normal discovery below.
_SKIP_IMPORT_PROBES = str(os.environ.get("MAGI_SKIP_IMPORT_PROBES") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_CASE_ROOTS = [] if _SKIP_IMPORT_PROBES else preferred_case_roots(include_closed=True)
_FALLBACK_CASE_ROOTS = [] if _SKIP_IMPORT_PROBES else default_case_roots(include_closed=True)
_EXPLICIT_ACTIVE_CASE_ROOT = os.environ.get("MAGI_OBSIDIAN_SOURCE_CASE_ROOT", "").strip()
_ACTIVE_CASE_ROOT = Path(_EXPLICIT_ACTIVE_CASE_ROOT) if _EXPLICIT_ACTIVE_CASE_ROOT else (
    _CASE_ROOTS[0]
    if _CASE_ROOTS
    else (
        _FALLBACK_CASE_ROOTS[0]
        if _FALLBACK_CASE_ROOTS
        else Path.home()
        / "Library"
        / "CloudStorage"
        / "SynologyDrive-homes"
        / "01_案件"
    )
)
_CLOSED_CASE_ROOT = _CASE_ROOTS[1] if len(_CASE_ROOTS) > 1 else (_FALLBACK_CASE_ROOTS[1] if len(_FALLBACK_CASE_ROOTS) > 1 else _ACTIVE_CASE_ROOT)
_FANG_SHARE = (os.environ.get("MAGI_OBSIDIAN_FANG_SHARE") or "lumi").strip().strip("/\\")
_FANG_FOLDER = (os.environ.get("MAGI_OBSIDIAN_FANG_FOLDER") or "fang").strip().strip("/\\")
SOURCE_ROOTS = {
    "案件": Path(_ACTIVE_CASE_ROOT),
    "結案": Path(_CLOSED_CASE_ROOT),
    "舊案": Path(_CLOSED_CASE_ROOT) / "舊案",
    "fang": Path("/Volumes") / _FANG_SHARE / _FANG_FOLDER,
}

# Ingest state file (tracks processed files for idempotency)
INGEST_STATE_PATH = AGENT_DIR / "obsidian_ingest_state.json"

_CASE_FOLDER_RE = re.compile(r"(\d{4}-\d{4})-(.+?)-(.*?)-(.*)")


def _load_ingest_state() -> Dict:
    if INGEST_STATE_PATH.exists():
        try:
            return json.loads(INGEST_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 469, exc_info=True)
    return {"files": {}, "updated_at": ""}


def _save_ingest_state(state: Dict):
    """Merge and atomically persist ingest progress.

    A scheduled run and an operator-triggered recovery may overlap.  The old
    direct write could both truncate the state on interruption and overwrite
    progress from the other run.  Merge under a file lock and replace only a
    fully flushed temporary file.
    """

    INGEST_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = INGEST_STATE_PATH.with_suffix(INGEST_STATE_PATH.suffix + ".lock")
    tmp_path = INGEST_STATE_PATH.with_suffix(
        f"{INGEST_STATE_PATH.suffix}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    with open(lock_path, "a") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            merged: Dict[str, Any] = {"files": {}, "updated_at": ""}
            if INGEST_STATE_PATH.exists():
                try:
                    existing = json.loads(
                        INGEST_STATE_PATH.read_text(encoding="utf-8")
                    )
                    if isinstance(existing, dict):
                        merged.update(existing)
                        merged["files"] = dict(existing.get("files") or {})
                except Exception:
                    logging.getLogger(__name__).warning(
                        "obsidian_ingest: existing state is unreadable; preserving new progress",
                        exc_info=True,
                    )
            merged["files"].update(dict(state.get("files") or {}))
            for key, value in state.items():
                if key != "files":
                    merged[key] = value
            merged["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            with open(tmp_path, "w", encoding="utf-8") as tmp_fh:
                json.dump(merged, tmp_fh, ensure_ascii=False, indent=2)
                tmp_fh.flush()
                os.fsync(tmp_fh.fileno())
            os.replace(tmp_path, INGEST_STATE_PATH)
            state.clear()
            state.update(merged)
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass


def _ingest_state_matches_stat(previous: Dict, stat_result: os.stat_result) -> bool:
    """Cheaply identify a previously hashed file before applying batch limit."""

    if not isinstance(previous, dict) or not previous.get("hash"):
        return False
    saved_ns = previous.get("mtime_ns")
    saved_size = previous.get("size")
    if saved_ns is not None and saved_size is not None:
        try:
            return (
                int(saved_ns) == int(stat_result.st_mtime_ns)
                and int(saved_size) == int(stat_result.st_size)
            )
        except (TypeError, ValueError):
            return False
    # Backward-compatible migration for the old second-resolution state.  A
    # hash is still required; the next changed mtime will upgrade this row to
    # nanosecond + size metadata.
    try:
        return int(previous.get("mtime")) == int(stat_result.st_mtime)
    except (TypeError, ValueError):
        return False


def _generate_frontmatter(
    source_root: str,
    source_path: str,
    source_relpath: str,
    file_type: str,
    mtime: int,
    case_info: Optional[Dict] = None,
    doc_key: str = "",
    file_hash_val: str = "",
    extraction_method: str = "",
    extraction_pages: Any = "",
    extraction_quality: str = "",
) -> str:
    """Generate YAML frontmatter for an extracted note."""
    lines = ["---"]
    lines.append("summary_schema: magi-obsidian-note-v2")
    lines.append(f"source_root: {source_root}")
    lines.append(f"source_path: {source_path}")
    lines.append(f"source_relpath: {source_relpath}")
    lines.append(f"file_type: {file_type}")
    if case_info:
        lines.append(f"case_number: {case_info.get('case_number', '')}")
        lines.append(f"client_name: {case_info.get('client_name', '')}")
    else:
        lines.append("case_number: ")
        lines.append("client_name: ")
    lines.append(f"doc_key: {doc_key}")
    lines.append(f"file_hash: {file_hash_val}")
    lines.append(f"mtime: {mtime}")
    if extraction_method:
        lines.append(f"extraction_method: {extraction_method}")
    if extraction_pages not in ("", None):
        lines.append(f"extraction_pages: {extraction_pages}")
    if extraction_quality:
        lines.append(f"extraction_quality: {extraction_quality}")
    lines.append(f"extracted_at: {time.strftime('%Y-%m-%dT%H:%M:%S')}")
    lines.append("---")
    return "\n".join(lines)


def _resolve_case_info(relpath: str) -> Optional[Dict]:
    """Try to parse case info from the relative path."""
    parts = Path(relpath).parts
    for part in parts:
        m = _CASE_FOLDER_RE.match(part)
        if m:
            return {
                "case_number": m.group(1),
                "client_name": m.group(2),
                "phase": m.group(3),
                "charge": m.group(4),
            }
    return None


def _sanitize_note_name(name: str) -> str:
    """Sanitize a filename for use as an Obsidian note name."""
    # Remove characters that are problematic in filenames
    bad = r'[<>:"/\\|?*\x00-\x1f]'
    name = re.sub(bad, "_", name)
    # Truncate
    if len(name) > 120:
        name = name[:120]
    return name.strip("_. ")


def _parse_frontmatter_dict(content: str) -> Dict[str, str]:
    m = _FRONTMATTER_RE.match(str(content or ""))
    if not m:
        return {}
    out: Dict[str, str] = {}
    for raw in m.group(1).splitlines():
        if ":" not in raw or raw.lstrip().startswith("-"):
            continue
        key, value = raw.split(":", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            out[key] = value
    return out


def _extract_section(content: str, heading: str) -> str:
    pattern = rf"##\s+{re.escape(heading)}\s*\n(.+?)(?=\n##\s+|\Z)"
    m = re.search(pattern, str(content or ""), flags=re.DOTALL)
    return m.group(1).strip() if m else ""


def _extract_existing_full_text(content: str) -> str:
    return _extract_section(content, "Full Text") or _extract_section(content, "全文") or ""


_GENERATED_IMAGE_ARTIFACT_RE = re.compile(
    r"!\[[^\]\n]*\]\(<[^>\n]*_images[/\\]imageFile\d+\.(?:png|jpe?g|webp)[^>\n]*>\)"
    r"|!\[[^\]\n]*\]\([^\n)]*_images[/\\]imageFile\d+\.(?:png|jpe?g|webp)[^\n)]*\)",
    re.IGNORECASE,
)
_GENERATED_IMAGE_ARTIFACT_LINE_RE = re.compile(
    r"(?im)^[^\n]*_images[/\\]imageFile\d+\.(?:png|jpe?g|webp)[^\n]*$"
)


def _generated_image_artifact_count(text: str) -> int:
    s = str(text or "")
    return len(_GENERATED_IMAGE_ARTIFACT_RE.findall(s)) + len(
        re.findall(r"_images[/\\]imageFile\d+\.(?:png|jpe?g|webp)", s, flags=re.IGNORECASE)
    )


def _remove_generated_image_artifacts(text: str) -> str:
    s = str(text or "")
    s = _GENERATED_IMAGE_ARTIFACT_RE.sub(" ", s)
    s = _GENERATED_IMAGE_ARTIFACT_LINE_RE.sub("", s)
    s = re.sub(r"(?im)^[>\-\s]*(?:LightPDF)?\s*$", "", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _strip_markdown_noise(text: str) -> str:
    s = _remove_generated_image_artifacts(text)
    s = re.sub(r"!\[[^\]\n]*\]\(<[^>\n]*>\)", " ", s)
    s = re.sub(r"!\[[^\]\n]*\]\([^)]+\)", " ", s)
    s = re.sub(r"^#+\s*", "", s, flags=re.MULTILINE)
    s = re.sub(r"^[\s>*-]*(?:\d{1,3}|[０-９]{1,3})\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def _text_signal(text: str) -> Dict[str, int]:
    s = _strip_markdown_noise(text)
    cjk = len(re.findall(r"[\u4e00-\u9fff]", s))
    alnum = len(re.findall(r"[A-Za-z0-9]", s))
    image_links = len(re.findall(r"!\[[^\]\n]*\]\(<[^>\n]*>\)|!\[[^\]\n]*\]\([^)]+\)", str(text or "")))
    image_artifacts = _generated_image_artifact_count(text)
    spaced_digit_noise = len(re.findall(r"(?:\d\s+){2,}\d", s))
    return {
        "chars": len(s),
        "cjk": cjk,
        "alnum": alnum,
        "image_links": image_links + image_artifacts,
        "spaced_digit_noise": spaced_digit_noise,
        "score": cjk * 2 + alnum - (image_links + image_artifacts) * 80 - spaced_digit_noise * 12,
    }


def _extraction_quality(text: str, method: str = "") -> str:
    signal = _text_signal(text)
    method_l = str(method or "").lower()
    if _generated_image_artifact_count(text) and signal["score"] < 180:
        return "image_only"
    if signal["chars"] < MIN_EXTRACTED_CHARS:
        return "too_short"
    if signal["image_links"] and signal["score"] < 180:
        return "image_only"
    if "markitdown" in method_l and signal["score"] < 240:
        return "weak_markitdown"
    if signal["score"] < 140:
        return "weak_text"
    return "ok"


def _sanitize_extracted_text_for_note(text: str) -> str:
    image_artifacts = _generated_image_artifact_count(text)
    cleaned = _remove_generated_image_artifacts(text)
    if image_artifacts and _text_signal(cleaned)["score"] < 120:
        return "（原始抽取結果僅包含圖片佔位符，MAGI 已移除這些無效連結；請以來源檔重新 OCR 或人工確認。）"
    return cleaned or str(text or "").strip()


def _split_candidate_lines(text: str, *, max_lines: int = 300) -> List[str]:
    cleaned = _strip_markdown_noise(text)
    raw_parts = re.split(r"[\n。；;]+", cleaned)
    out: List[str] = []
    seen = set()
    for part in raw_parts:
        line = re.sub(r"\s+", " ", part).strip(" -:：，,")
        if len(line) < 8:
            continue
        if len(re.sub(r"[\W_]+", "", line)) < 6:
            continue
        if line in seen:
            continue
        seen.add(line)
        out.append(line[:220])
        if len(out) >= max_lines:
            break
    return out


def _pick_lines(lines: List[str], keywords: Tuple[str, ...], *, limit: int = 5) -> List[str]:
    picked: List[str] = []
    for line in lines:
        if any(k in line for k in keywords):
            picked.append(line)
        if len(picked) >= limit:
            break
    return picked


def _extract_dates(text: str, *, limit: int = 8) -> List[str]:
    patterns = [
        r"\d{2,3}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日",
        r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日",
        r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}",
        r"\d{8}",
    ]
    seen = set()
    dates: List[str] = []
    for pat in patterns:
        for m in re.findall(pat, str(text or "")):
            d = re.sub(r"\s+", "", m)
            if d in seen:
                continue
            seen.add(d)
            dates.append(d)
            if len(dates) >= limit:
                return dates
    return dates


def _detect_document_type(title: str, relpath: str, text: str) -> str:
    hay = f"{title} {relpath} {text[:1200]}"
    rules = [
        ("判決/裁定/處分", ("判決", "裁定", "不起訴", "起訴書", "確定證明")),
        ("書狀", ("書狀", "答辯狀", "聲請狀", "陳報狀", "準備書")),
        ("筆錄", ("筆錄", "調查筆錄", "訊問筆錄", "準備程序")),
        ("法院通知", ("通知書", "開庭", "傳票", "期日", "庭期")),
        ("證據", ("證據", "存摺", "交易明細", "匯款", "照片", "截圖")),
        ("信件", ("函", "電子郵件", "email", "信件")),
    ]
    for label, keywords in rules:
        if any(k in hay for k in keywords):
            return label
    return "一般文件"


def _extract_case_markers(lines: List[str], text: str) -> List[str]:
    markers: List[str] = []
    for pat in (
        r"(臺灣[^，。\n]{0,18}法院|最高法院|最高行政法院|智慧財產及商業法院)",
        r"\d{2,3}\s*年度\s*[\u4e00-\u9fffA-Za-z]{1,8}\s*字\s*第\s*\d+\s*號",
        r"(原告|被告|聲請人|相對人|債權人|債務人|告訴人|代理人)\s*[^\n，。:：]{1,24}",
    ):
        for m in re.findall(pat, text[:3000]):
            value = m if isinstance(m, str) else "".join(m)
            value = re.sub(r"\s+", "", value)
            if value and value not in markers:
                markers.append(value)
            if len(markers) >= 8:
                return markers
    if not markers:
        markers.extend(lines[:3])
    return markers[:8]


def _bullet_lines(items: List[str], empty: str = "未見明確資料") -> str:
    if not items:
        return f"- {empty}"
    return "\n".join(f"- {item}" for item in items)


def _summarize_legal_meaning(doc_type: str, lines: List[str], dates: List[str]) -> List[str]:
    if doc_type == "法院通知":
        out = _pick_lines(lines, ("開庭", "庭期", "調解", "期日", "到庭", "續行"), limit=3)
        return out or ["此文件主要影響程序期日與到庭/補正安排，應納入行事曆與待辦追蹤。"]
    if doc_type == "筆錄":
        out = _pick_lines(lines, ("諭知", "宣示", "表示", "到庭", "程序", "調解", "不成立"), limit=4)
        return out or ["此文件記載程序進行與當事人陳述，應用於整理事實、爭點與後續期日。"]
    if doc_type == "書狀":
        out = _pick_lines(lines, ("聲明", "理由", "請求", "答辯", "爭執", "證據"), limit=4)
        return out or ["此文件屬攻防主張，應與對造書狀、證據及法院期日交叉檢查。"]
    if doc_type == "判決/裁定/處分":
        out = _pick_lines(lines, ("主文", "理由", "上訴", "抗告", "期間", "處分", "判決"), limit=5)
        return out or ["此文件可能決定案件階段或救濟期間，應檢查主文、理由及不變期間。"]
    if doc_type == "證據":
        return _pick_lines(lines, ("金額", "匯款", "交易", "帳戶", "證據", "照片", "紀錄"), limit=4) or [
            "此文件偏向證據材料，應標明待證事實、來源與可否提出。"
        ]
    return _pick_lines(lines, ("應", "不得", "期限", "法院", "程序", "證據"), limit=4) or [
        "此文件可供案件背景或後續檢索使用，尚未辨識出特定程序效果。"
    ]


def _build_structured_summary(
    title: str,
    relpath: str,
    text: str,
    *,
    method: str = "",
    pages: Any = "",
    case_info: Optional[Dict] = None,
) -> str:
    lines = _split_candidate_lines(text)
    dates = _extract_dates(text)
    doc_type = _detect_document_type(title, relpath, text)
    quality = _extraction_quality(text, method)
    markers = _extract_case_markers(lines, text)
    deadline_lines = _pick_lines(
        lines,
        ("期限", "應於", "前提出", "補正", "開庭", "庭期", "調解", "續行", "到庭", "上訴", "抗告", "不變期間"),
        limit=6,
    )
    issue_lines = _pick_lines(lines, ("爭點", "主張", "抗辯", "否認", "承認", "理由", "不成立", "犯罪事實"), limit=5)
    evidence_lines = _pick_lines(lines, ("證據", "存摺", "交易", "匯款", "明細", "照片", "截圖", "附件", "卷"), limit=5)
    key_points = _summarize_legal_meaning(doc_type, lines, dates)

    if quality != "ok":
        quality_note = (
            f"- 抽取品質：{quality}。此筆記仍保留來源與全文區，"
            "但應由下次 reextract/OCR 或人工確認後再作實質判斷。"
        )
    else:
        quality_note = ""

    case_label = ""
    if case_info:
        case_label = f"{case_info.get('case_number', '')} {case_info.get('client_name', '')}".strip()

    parts = [
        "## 摘要",
        "",
        f"- 文件類型：{doc_type}",
        f"- 案件：{case_label or '未從路徑辨識'}",
        f"- 來源檔：`{relpath}`",
        f"- 抽取：{method or '?'}；頁數：{pages or '?'}；品質：{quality}",
    ]
    if quality_note:
        parts.append(quality_note)
    parts += [
        "",
        "## 關鍵資訊",
        "",
        _bullet_lines(markers),
        "",
        "## 法律/程序意義",
        "",
        _bullet_lines(key_points),
        "",
        "## 期限與待辦",
        "",
        _bullet_lines(deadline_lines, "未在文字中辨識到明確期限或待辦"),
        "",
        "## 爭點與證據",
        "",
        "### 可能爭點",
        "",
        _bullet_lines(issue_lines, "未在文字中辨識到明確爭點"),
        "",
        "### 證據/資料",
        "",
        _bullet_lines(evidence_lines, "未在文字中辨識到明確證據材料"),
    ]
    return "\n".join(parts).strip()


def _build_note_content(
    *,
    frontmatter: str,
    title: str,
    relpath: str,
    suffix: str,
    result: Dict[str, Any],
    text: str,
    case_info: Optional[Dict] = None,
) -> str:
    method = str(result.get("method") or "")
    pages = result.get("pages", "?")
    clean_text = _sanitize_extracted_text_for_note(text)
    summary = _build_structured_summary(
        title,
        relpath,
        clean_text,
        method=method,
        pages=pages,
        case_info=case_info,
    )
    cleaned_noise = _strip_markdown_noise(clean_text)
    excerpt = cleaned_noise[:360].replace("\n", " ").strip()
    if len(cleaned_noise) > 360:
        excerpt += "..."

    return f"""{frontmatter}

# {title}

**Source:** `{relpath}`
**Type:** {suffix.lower()} | **Pages:** {pages} | **Method:** {method or '?'}

{summary}

## Extract

> {excerpt}

## Full Text

{clean_text}
"""


def _note_preference_key(path: Path, meta: Dict[str, str], content: str) -> Tuple[int, int, int, str]:
    stem = path.stem
    suffix_penalty = 1 if re.search(r"_\d+$", stem) else 0
    schema_bonus = 1 if meta.get("summary_schema") == "magi-obsidian-note-v2" else 0
    quality_bonus = 1 if meta.get("extraction_quality") == "ok" else 0
    method_penalty = 1 if "markitdown" in meta.get("extraction_method", "").lower() else 0
    return (
        schema_bonus + quality_bonus,
        -suffix_penalty - method_penalty,
        len(content),
        str(path),
    )


def _collect_existing_source_note_hashes(notes_dir: Path, vault: Path) -> Dict[str, Dict[str, str]]:
    existing: Dict[str, Dict[str, str]] = {}
    if not notes_dir.exists():
        return existing
    for note in notes_dir.rglob("summary__*.md"):
        try:
            content = note.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        meta = _parse_frontmatter_dict(content)
        fhash = str(meta.get("file_hash") or "").strip()
        if not fhash:
            continue
        rel = str(note.relative_to(vault))
        current = existing.get(fhash)
        candidate = {
            "note_path": rel,
            "source_relpath": meta.get("source_relpath", ""),
            "source_path": meta.get("source_path", ""),
            "mtime": meta.get("mtime", ""),
            "preference": repr(_note_preference_key(note, meta, content)),
        }
        if not current:
            existing[fhash] = candidate
            continue
        current_path = vault / current["note_path"]
        try:
            current_content = current_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            current_content = ""
        current_key = _note_preference_key(current_path, _parse_frontmatter_dict(current_content), current_content)
        candidate_key = _note_preference_key(note, meta, content)
        if candidate_key > current_key:
            existing[fhash] = candidate
    return existing


def _hydrate_ingest_state_from_existing_notes(
    source: str,
    files_state: Dict[str, Dict],
    existing_hash_notes: Dict[str, Dict[str, str]],
) -> int:
    """Recover lost ingest state from the durable note frontmatter.

    Obsidian notes are the durable product and already carry the source hash,
    relative path and mtime.  Rebuilding the lightweight cursor from those
    fields prevents an empty/corrupt cursor from re-extracting thousands of
    existing notes four files per day.
    """

    hydrated = 0
    for fhash, note in existing_hash_notes.items():
        relpath = str(note.get("source_relpath") or "").strip().lstrip("/\\")
        if not relpath or not fhash:
            continue
        state_key = f"{source}/{relpath}"
        if files_state.get(state_key, {}).get("hash"):
            continue
        try:
            mtime = int(float(str(note.get("mtime") or "0")))
        except (TypeError, ValueError):
            continue
        if mtime <= 0:
            continue
        files_state[state_key] = {
            "hash": str(fhash),
            "mtime": mtime,
            "note_path": str(note.get("note_path") or ""),
            "status": "indexed_state_recovered",
            "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        hydrated += 1
    return hydrated


def _update_index_entry(idx: Dict, vault: Path, rel: str, doc_key: str = "", chunks: int = 0) -> None:
    note_full = vault / rel
    if not note_full.exists():
        idx.setdefault("notes", {}).pop(rel, None)
        return
    content = note_full.read_text(encoding="utf-8", errors="replace")
    idx.setdefault("notes", {})[rel] = {
        "hash": _note_hash(content),
        "mtime": int(note_full.stat().st_mtime),
        "doc_key": doc_key,
        "chunks": chunks,
        "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def _full_file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _duplicate_identity(meta: Dict[str, str], content: str) -> str:
    full_text = _extract_existing_full_text(content) or content
    if _text_signal(full_text)["score"] < 120:
        return ""
    if os.environ.get("MAGI_OBSIDIAN_DUPLICATE_SOURCE_HASH", "0").strip() == "1":
        source_path = Path(meta.get("source_path", "")).expanduser()
        try:
            if source_path.exists() and source_path.is_file():
                return "file:" + _full_file_hash(source_path)
        except Exception:
            pass
    normalized = re.sub(r"\s+", "", _strip_markdown_noise(full_text))
    return "text:" + hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:16]


def _known_case_roots() -> List[Path]:
    roots: List[Path] = []
    for raw in list(preferred_case_roots(include_closed=True)) + list(default_case_roots(include_closed=True)):
        try:
            path = Path(raw).expanduser()
        except Exception:
            continue
        if path not in roots:
            roots.append(path)
    return roots


def _resolve_existing_source_path(meta: Dict[str, str]) -> Optional[Path]:
    raw_source = (meta.get("source_path") or "").strip()
    if raw_source:
        source_path = Path(raw_source).expanduser()
        if source_path.exists():
            return source_path

    rel = (meta.get("source_relpath") or "").strip().lstrip("/\\")
    filename = Path(raw_source or rel).name
    case_number = (meta.get("case_number") or "").strip()
    roots = _known_case_roots()

    if rel:
        rel_path = Path(rel)
        for root in roots:
            candidate = root / rel_path
            if candidate.exists():
                return candidate

    if case_number and filename:
        for root in roots:
            if not root.is_dir():
                continue
            try:
                case_dirs = list(root.glob(f"*/*/{case_number}-*"))
            except Exception:
                continue
            for case_dir in case_dirs[:8]:
                if not case_dir.is_dir():
                    continue
                try:
                    exact = list(case_dir.rglob(filename))
                except Exception:
                    continue
                if exact:
                    return exact[0]
    return None


def task_ingest_source(
    source: str,
    subpath: str = "",
    limit: int = 50,
    force: bool = False,
    include_folders: Optional[str] = None,
    exclude_folders: Optional[str] = None,
) -> Dict:
    """Selective ingest from a Synology source root into 20_Notes/.

    Extracts text from PDF/DOCX/TXT/MD files, generates Obsidian notes
    with metadata frontmatter, and ingests into vector memory.

    Folder filtering (applied to any ancestor folder in the file's path):
      --include-folders "high-value"   → only HIGH_VALUE_FOLDERS (書狀/筆錄/判決等)
      --include-folders "04_我方歷次書狀,08_筆錄"  → comma-separated whitelist
      --exclude-folders "default"      → skip DEFAULT_EXCLUDE_FOLDERS (閱卷/法扶/回執等)
      --exclude-folders "06_閱卷資料,01_法扶資料"   → comma-separated blacklist
      (default when neither specified: --exclude-folders default)
    """
    from skills.obsidian.extractors import extract_text, file_hash, SUPPORTED_EXTENSIONS

    # Resolve source root
    root = SOURCE_ROOTS.get(source)
    if not root:
        return {"success": False, "error": f"Unknown source: {source}. Valid: {', '.join(SOURCE_ROOTS.keys())}"}
    if not root.is_dir():
        return {"success": False, "error": f"Source root not accessible: {root}"}

    # Narrow to subpath
    scan_dir = root / subpath if subpath else root
    if not scan_dir.is_dir():
        return {"success": False, "error": f"Subpath not found: {scan_dir}"}

    # ── Build folder filter sets ──────────────────────────────────
    _include_set: Optional[set] = None
    _exclude_set: set = set()

    if include_folders:
        if include_folders.strip().lower() == "high-value":
            _include_set = HIGH_VALUE_FOLDERS.copy()
        else:
            _include_set = {f.strip() for f in include_folders.split(",") if f.strip()}

    if exclude_folders:
        if exclude_folders.strip().lower() == "default":
            _exclude_set = DEFAULT_EXCLUDE_FOLDERS.copy()
        else:
            _exclude_set = {f.strip() for f in exclude_folders.split(",") if f.strip()}
    elif not include_folders:
        # Neither specified → apply default exclusion
        _exclude_set = DEFAULT_EXCLUDE_FOLDERS.copy()

    def _folder_allowed(filepath: Path) -> bool:
        """Check if any ancestor folder name passes the include/exclude filter."""
        parts = set(filepath.relative_to(root).parts[:-1])  # folder parts only
        if _include_set:
            # At least one ancestor must be in the whitelist
            return bool(parts & _include_set)
        if _exclude_set:
            # No ancestor may be in the blacklist
            return not bool(parts & _exclude_set)
        return True

    # Get vault
    vault = _get_vault_path()
    if not vault:
        return {"success": False, "error": "No vault configured."}

    # Notes output dir
    notes_dir = vault / "20_Notes" / source
    notes_dir.mkdir(parents=True, exist_ok=True)

    # Load ingest state
    state = _load_ingest_state()
    files_state = state.get("files", {})
    existing_hash_notes = _collect_existing_source_note_hashes(notes_dir, vault)
    recovered_state_rows = _hydrate_ingest_state_from_existing_notes(
        source, files_state, existing_hash_notes
    )
    if recovered_state_rows:
        state["files"] = files_state
        _save_ingest_state(state)

    # Vector pipeline
    try:
        from skills.documents.vector_pipeline import ingest_text_to_vector_memory
    except ImportError:
        ingest_text_to_vector_memory = None

    # Collect files (with folder filter)
    all_files_with_mtime = []
    disappeared_during_scan = []
    filtered_by_folder = 0
    for f in scan_dir.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        if any(part.startswith(".") for part in f.relative_to(root).parts):
            continue
        if not _folder_allowed(f):
            filtered_by_folder += 1
            continue
        try:
            scan_stat = f.stat()
        except FileNotFoundError:
            # Synology Drive / NAS reconciliation may rename or remove a file
            # between rglob() and stat().  A transient source disappearance is
            # not an ingest failure and must never abort the entire batch.
            disappeared_during_scan.append(str(f.relative_to(root)))
            continue
        except OSError as e:
            disappeared_during_scan.append(
                f"{f.relative_to(root)} ({type(e).__name__}: {e})"
            )
            continue
        all_files_with_mtime.append((scan_stat.st_mtime, scan_stat, f))

    # Sort by mtime (newest first for relevance)
    all_files_with_mtime.sort(key=lambda item: item[0], reverse=True)

    # Apply the limit to actual work, not to the newest N paths.  The previous
    # order truncated first and then skipped unchanged files, so after one
    # successful run it kept rechecking the same newest paths forever and
    # never advanced into the backlog.
    all_files = []
    unchanged_prefiltered = 0
    for _mtime, scan_stat, f in all_files_with_mtime:
        relpath = str(f.relative_to(root))
        state_key = f"{source}/{relpath}"
        if not force and _ingest_state_matches_stat(
            files_state.get(state_key, {}), scan_stat
        ):
            unchanged_prefiltered += 1
            continue
        all_files.append(f)
        if limit > 0 and len(all_files) >= limit:
            break

    total_to_process = len(all_files)
    vault_name = vault.name
    processed = 0
    skipped = 0
    short_text = 0
    errors = []
    warnings = [
        {
            "path": relpath,
            "warning": "source disappeared or became unavailable during scan",
            "kind": "source_disappeared",
        }
        for relpath in disappeared_during_scan
    ]
    disappeared_skipped = len(disappeared_during_scan)
    malformed_skipped = 0
    duplicate_hash_skipped = 0
    notes_created = []
    t_start = time.time()
    state_mutations = 0
    last_checkpoint_at = t_start

    def _checkpoint(*, force_save: bool = False) -> None:
        nonlocal state_mutations, last_checkpoint_at
        if state_mutations <= 0 and not force_save:
            return
        now = time.time()
        if not force_save and state_mutations < 5 and now - last_checkpoint_at < 60:
            return
        state["files"] = files_state
        _save_ingest_state(state)
        state_mutations = 0
        last_checkpoint_at = now
        print("[ingest_source] 進度已安全存檔", flush=True)

    print(f"[ingest_source] 開始匯入: source={source}, subpath={subpath or '(root)'}, "
          f"候選檔案={total_to_process}, 排除={filtered_by_folder} (folder filter)", flush=True)
    if _include_set:
        print(f"  include: {sorted(_include_set)}", flush=True)
    if _exclude_set:
        print(f"  exclude: {sorted(_exclude_set)}", flush=True)

    for i, f in enumerate(all_files):
        relpath = str(f.relative_to(root))
        state_key = f"{source}/{relpath}"
        try:
            current_stat = f.stat()
            mtime = int(current_stat.st_mtime)
            mtime_ns = int(current_stat.st_mtime_ns)
            file_size = int(current_stat.st_size)
            fhash = file_hash(f)
        except FileNotFoundError as e:
            logging.getLogger(__name__).info(
                "obsidian_ingest: source disappeared before processing %s: %s",
                relpath,
                e,
            )
            warnings.append({
                "path": relpath,
                "warning": f"{type(e).__name__}: {e}",
                "kind": "source_disappeared",
            })
            disappeared_skipped += 1
            skipped += 1
            continue
        except OSError as e:
            logging.getLogger(__name__).warning(
                "obsidian_ingest: source unavailable before processing %s: %s",
                relpath,
                e,
            )
            errors.append({"path": relpath, "error": f"{type(e).__name__}: {e}"})
            continue

        # Progress log every 10 files
        if i > 0 and i % 10 == 0:
            elapsed = time.time() - t_start
            rate = processed / elapsed if elapsed > 0 and processed > 0 else 0
            print(f"[ingest_source] 進度 {i}/{total_to_process}  "
                  f"已匯入={processed} 跳過={skipped} 錯誤={len(errors)} "
                  f"({rate:.1f} files/sec)", flush=True)

        # Skip unchanged files
        prev = files_state.get(state_key, {})
        if not force and prev.get("hash") == fhash and prev.get("mtime") == mtime:
            prev.update({"mtime_ns": mtime_ns, "size": file_size})
            state_mutations += 1
            _checkpoint()
            skipped += 1
            continue

        # Extract text — per-file try/except 確保單檔失敗不炸整個 ingest job
        try:
            result = extract_text(f)
        except BaseException as e:
            if isinstance(e, FileNotFoundError):
                logging.getLogger(__name__).info(
                    "obsidian_ingest: source disappeared during extraction %s: %s",
                    relpath,
                    e,
                )
                warnings.append({
                    "path": relpath,
                    "warning": f"{type(e).__name__}: {e}",
                    "kind": "source_disappeared",
                })
                disappeared_skipped += 1
                skipped += 1
                continue
            if _is_known_malformed_pdf_skip(f, str(e)):
                logging.getLogger(__name__).warning(
                    "obsidian_ingest: skip malformed pdf %s: %s", relpath, e
                )
                warnings.append({
                    "path": relpath,
                    "warning": f"{type(e).__name__}: {e}",
                    "kind": "malformed_pdf",
                })
                malformed_skipped += 1
                skipped += 1
                files_state[state_key] = {
                    "hash": fhash,
                    "mtime": mtime,
                    "mtime_ns": mtime_ns,
                    "size": file_size,
                    "status": "malformed_pdf",
                    "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                state_mutations += 1
                _checkpoint()
                continue
            logging.getLogger(__name__).warning(
                "obsidian_ingest: skip unreadable file %s: %s", relpath, e
            )
            errors.append({"path": relpath, "error": f"{type(e).__name__}: {e}"})
            continue
        if not result.get("success"):
            err_msg = result.get("error", "extraction failed")
            if _is_known_malformed_pdf_skip(f, str(err_msg)):
                logging.getLogger(__name__).warning(
                    "obsidian_ingest: malformed pdf skipped %s: %s", relpath, err_msg
                )
                warnings.append({
                    "path": relpath,
                    "warning": str(err_msg),
                    "kind": "malformed_pdf",
                })
                malformed_skipped += 1
                skipped += 1
                files_state[state_key] = {
                    "hash": fhash,
                    "mtime": mtime,
                    "mtime_ns": mtime_ns,
                    "size": file_size,
                    "status": "malformed_pdf",
                    "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                state_mutations += 1
                _checkpoint()
                continue
            logging.getLogger(__name__).warning(
                "obsidian_ingest: extraction failed %s: %s", relpath, err_msg
            )
            errors.append({"path": relpath, "error": err_msg})
            continue

        text = result["text"]
        if not text or len(text.strip()) < MIN_EXTRACTED_CHARS:
            short_text += 1
            files_state[state_key] = {
                "hash": fhash,
                "mtime": mtime,
                "mtime_ns": mtime_ns,
                "size": file_size,
                "status": "short_text",
                "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            state_mutations += 1
            _checkpoint()
            continue

        # Resolve case info from path
        case_info = _resolve_case_info(relpath)

        # Build note path mirroring source structure
        rel_parent = Path(relpath).parent
        note_subdir = notes_dir / rel_parent
        note_subdir.mkdir(parents=True, exist_ok=True)

        note_name = _sanitize_note_name(f"summary__{f.stem}")
        note_path = note_subdir / f"{note_name}.md"
        planned_note_rel = str(note_path.relative_to(vault))

        canonical = existing_hash_notes.get(fhash)
        if canonical and canonical.get("note_path") != planned_note_rel:
            files_state[state_key] = {
                "hash": fhash,
                "mtime": mtime,
                "mtime_ns": mtime_ns,
                "size": file_size,
                "note_path": canonical.get("note_path", ""),
                "duplicate_of": canonical.get("note_path", ""),
                "duplicate_source_relpath": canonical.get("source_relpath", ""),
                "doc_key": "",
                "chunks": 0,
                "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            duplicate_hash_skipped += 1
            skipped += 1
            state_mutations += 1
            _checkpoint()
            continue

        # Generate frontmatter
        method = str(result.get("method") or "")
        quality = _extraction_quality(text, method)
        frontmatter = _generate_frontmatter(
            source_root=source,
            source_path=str(f),
            source_relpath=relpath,
            file_type=f.suffix.lstrip(".").lower(),
            mtime=mtime,
            case_info=case_info,
            file_hash_val=fhash,
            extraction_method=method,
            extraction_pages=result.get("pages", ""),
            extraction_quality=quality,
        )

        # Build note content
        title = f.stem
        note_content = _build_note_content(
            frontmatter=frontmatter,
            title=title,
            relpath=relpath,
            suffix=f.suffix,
            result=result,
            text=text,
            case_info=case_info,
        )
        # Write note
        note_path.write_text(note_content, encoding="utf-8")

        # Build enhanced source metadata for vector memory
        # Keep compact to stay within 250-char source limit in mem_bridge
        note_rel = str(note_path.relative_to(vault))
        case_num = case_info.get("case_number", "") if case_info else ""
        source_meta = (
            f"obsidian|vault={vault_name}"
            f"|source_root={source}"
            f"|case={case_num}"
            f"|note={note_rel}"
        )

        # Ingest into vector memory
        doc_key = ""
        chunks_written = 0
        if ingest_text_to_vector_memory:
            try:
                vr = ingest_text_to_vector_memory(
                    kind="obsidian",
                    primary=source_meta,
                    title=title,
                    text=note_content,
                    chunk_chars=CHUNK_CHARS,
                    overlap=CHUNK_OVERLAP,
                    max_chunks_total=50,
                )
                if vr.get("success"):
                    doc_key = vr.get("doc_key", "")
                    chunks_written = vr.get("chunks_written", 0)
            except Exception as e:
                errors.append({"path": relpath, "error": f"vector ingest: {e}"})

        # Update note frontmatter with doc_key
        if doc_key:
            note_content = note_content.replace("doc_key: ", f"doc_key: {doc_key}", 1)
            note_path.write_text(note_content, encoding="utf-8")

        # Track state
        files_state[state_key] = {
            "hash": fhash,
            "mtime": mtime,
            "mtime_ns": mtime_ns,
            "size": file_size,
            "note_path": note_rel,
            "doc_key": doc_key,
            "chunks": chunks_written,
            "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

        notes_created.append(note_rel)
        existing_hash_notes[fhash] = {
            "note_path": note_rel,
            "source_relpath": relpath,
            "source_path": str(f),
        }
        processed += 1
        state_mutations += 1
        _checkpoint()

    # Save state
    _checkpoint(force_save=True)

    # Also update the vault index
    # Build reverse mapping: note_path -> state entry (files_state is keyed by source path)
    note_path_to_state = {}
    for _sk, _sv in files_state.items():
        np = _sv.get("note_path", "")
        if np:
            note_path_to_state[np] = _sv

    idx = _load_index()
    for nc in notes_created:
        st = note_path_to_state.get(nc, {})
        _update_index_entry(idx, vault, nc, doc_key=st.get("doc_key", ""), chunks=st.get("chunks", 0))
    _save_index(idx)

    elapsed = time.time() - t_start
    print(f"[ingest_source] 完成！耗時 {elapsed:.1f}s  "
          f"匯入={processed} 跳過={skipped} 文字太短={short_text} 警告={len(warnings)} 錯誤={len(errors)} "
          f"重複={duplicate_hash_skipped} folder排除={filtered_by_folder}", flush=True)

    return {
        "success": True,
        "source": source,
        "subpath": subpath or "(root)",
        "scanned": total_to_process,
        "unchanged_prefiltered": unchanged_prefiltered,
        "recovered_state_rows": recovered_state_rows,
        "filtered_by_folder": filtered_by_folder,
        "processed": processed,
        "skipped": skipped,
        "source_disappeared_skipped": disappeared_skipped,
        "malformed_pdf_skipped": malformed_skipped,
        "duplicate_hash_skipped": duplicate_hash_skipped,
        "short_text_skipped": short_text,
        "warnings": len(warnings),
        "errors": len(errors),
        "elapsed_sec": round(elapsed, 1),
        "notes_created": notes_created[:20],
        "warning_details": warnings[:10] if warnings else [],
        "error_details": errors[:10] if errors else [],
        "include_folders": sorted(_include_set) if _include_set else None,
        "exclude_folders": sorted(_exclude_set) if _exclude_set else None,
    }


def task_repair_notes(
    vault_path: Optional[Path] = None,
    limit: int = 0,
    force: bool = False,
    reextract: bool = False,
    case_number: str = "",
) -> Dict:
    """Rewrite existing summary notes into the v2 structured summary format."""
    vault = vault_path or _get_vault_path()
    if not vault:
        return {"success": False, "error": "No vault configured."}
    notes_dir = vault / "20_Notes"
    if not notes_dir.is_dir():
        return {"success": False, "error": "20_Notes not found"}

    def _repair_priority(note_path: Path) -> Tuple[int, str]:
        try:
            content = note_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return (9, str(note_path))
        meta = _parse_frontmatter_dict(content)
        full_text = _extract_existing_full_text(content)
        method = meta.get("extraction_method") or ""
        missing_v2 = meta.get("summary_schema") != "magi-obsidian-note-v2"
        missing_sections = not (
            _extract_section(content, "摘要")
            and _extract_section(content, "期限與待辦")
            and _extract_section(content, "爭點與證據")
            and _extract_section(content, "法律/程序意義")
        )
        weak = (
            (meta.get("extraction_quality") or "") not in {"", "ok"}
            or _extraction_quality(full_text, method) != "ok"
            or (full_text and _text_signal(full_text)["score"] < 120)
            or "markitdown" in method.lower()
        )
        if reextract and weak:
            return (0, str(note_path))
        if missing_v2 or missing_sections:
            return (1, str(note_path))
        if weak:
            return (2, str(note_path))
        return (3, str(note_path))

    candidates = sorted(notes_dir.rglob("summary__*.md"), key=_repair_priority)
    if case_number:
        candidates = [p for p in candidates if case_number in str(p)]
    if limit > 0:
        candidates = candidates[:limit]

    try:
        from skills.obsidian.extractors import extract_text
    except Exception:
        extract_text = None

    idx = _load_index()
    repaired = 0
    skipped = 0
    reextracted = 0
    source_relocated = 0
    missing_sources = 0
    weak = 0
    errors: List[Dict[str, str]] = []

    for note in candidates:
        rel = str(note.relative_to(vault))
        try:
            content = note.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            errors.append({"path": rel, "error": str(e)})
            continue
        meta = _parse_frontmatter_dict(content)
        current_text = _extract_existing_full_text(content)
        current_method = meta.get("extraction_method") or ""
        current_needs_reextract = (
            reextract
            and current_text
            and (_extraction_quality(current_text, current_method) != "ok" or "markitdown" in current_method.lower())
        )
        if (
            not force
            and meta.get("summary_schema") == "magi-obsidian-note-v2"
            and _extract_section(content, "摘要")
            and _extract_section(content, "期限與待辦")
            and _extract_section(content, "爭點與證據")
            and _extract_section(content, "法律/程序意義")
            and not current_needs_reextract
        ):
            skipped += 1
            continue

        text = current_text
        method = current_method
        pages = meta.get("extraction_pages") or "?"
        original_source_raw = (meta.get("source_path") or "").strip()
        original_source_path = Path(original_source_raw).expanduser() if original_source_raw else None
        resolved_source_path = original_source_path if original_source_path and original_source_path.exists() else None
        needs_reextract_source = (
            reextract
            and extract_text is not None
            and (_extraction_quality(text, method) != "ok" or "markitdown" in method.lower())
        )
        if needs_reextract_source and resolved_source_path is None:
            resolved_source_path = _resolve_existing_source_path(meta)
        if resolved_source_path and resolved_source_path != original_source_path:
            source_relocated += 1
        source_path = resolved_source_path or original_source_path
        should_reextract = (
            needs_reextract_source
            and resolved_source_path is not None
        )
        if needs_reextract_source and resolved_source_path is None and (meta.get("source_path") or meta.get("source_relpath")):
            missing_sources += 1
        if should_reextract:
            try:
                result = extract_text(source_path)
                if result.get("success") and result.get("text"):
                    text = result["text"]
                    method = str(result.get("method") or method)
                    pages = result.get("pages", pages)
                    reextracted += 1
            except Exception as e:
                errors.append({"path": rel, "error": f"reextract: {e}"})

        if not text:
            weak += 1
            text = _strip_markdown_noise(content)
        text = _sanitize_extracted_text_for_note(text)

        case_info = {
            "case_number": meta.get("case_number", ""),
            "client_name": meta.get("client_name", ""),
        }
        title_match = re.search(r"^#\s+(.+)$", content, flags=re.MULTILINE)
        title = title_match.group(1).strip() if title_match else note.stem.replace("summary__", "")
        source_relpath = meta.get("source_relpath") or rel
        file_type = meta.get("file_type") or (source_path.suffix.lstrip(".") if source_path else "") or note.suffix.lstrip(".")
        mtime = int(float(meta.get("mtime") or note.stat().st_mtime))
        quality = _extraction_quality(text, method)
        if quality != "ok":
            weak += 1
        file_hash_val = meta.get("file_hash", "")
        if os.environ.get("MAGI_OBSIDIAN_REPAIR_REFRESH_FILE_HASH", "0").strip() == "1":
            try:
                if source_path and source_path.exists() and source_path.is_file():
                    file_hash_val = _full_file_hash(source_path)
            except Exception as exc:
                logging.getLogger(__name__).debug(
                    "cannot compare existing case card %s; rewriting safely: %s",
                    target_file,
                    type(exc).__name__,
                )
        frontmatter = _generate_frontmatter(
            source_root=meta.get("source_root", ""),
            source_path=str(source_path) if source_path else meta.get("source_path", ""),
            source_relpath=source_relpath,
            file_type=file_type,
            mtime=mtime,
            case_info=case_info,
            doc_key=meta.get("doc_key", ""),
            file_hash_val=file_hash_val,
            extraction_method=method,
            extraction_pages=pages,
            extraction_quality=quality,
        )
        new_content = _build_note_content(
            frontmatter=frontmatter,
            title=title,
            relpath=source_relpath,
            suffix=f".{file_type}" if file_type else note.suffix,
            result={"method": method, "pages": pages},
            text=text,
            case_info=case_info,
        )
        if new_content != content:
            note.write_text(new_content, encoding="utf-8")
            prev_idx = (idx.get("notes") or {}).get(rel, {})
            _update_index_entry(
                idx,
                vault,
                rel,
                doc_key=meta.get("doc_key", "") or prev_idx.get("doc_key", ""),
                chunks=int(prev_idx.get("chunks", 0) or 0),
            )
            repaired += 1
        else:
            skipped += 1

    _save_index(idx)
    return {
        "success": True,
        "scanned": len(candidates),
        "repaired": repaired,
        "skipped": skipped,
        "reextracted": reextracted,
        "source_relocated": source_relocated,
        "missing_sources": missing_sources,
        "weak_after_repair": weak,
        "errors": len(errors),
        "error_details": errors[:10],
    }


def task_cleanup_duplicate_notes(
    vault_path: Optional[Path] = None,
    dry_run: bool = False,
) -> Dict:
    """Move duplicate summary notes out of 20_Notes and prune stale index rows."""
    vault = vault_path or _get_vault_path()
    if not vault:
        return {"success": False, "error": "No vault configured."}
    notes_dir = vault / "20_Notes"
    if not notes_dir.is_dir():
        return {"success": False, "error": "20_Notes not found"}

    groups: Dict[str, List[Tuple[Path, Dict[str, str], str]]] = {}
    for note in notes_dir.rglob("summary__*.md"):
        try:
            content = note.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        meta = _parse_frontmatter_dict(content)
        identity = _duplicate_identity(meta, content)
        if not identity:
            continue
        case_no = meta.get("case_number") or ""
        key = f"{case_no}|{identity}"
        groups.setdefault(key, []).append((note, meta, content))

    archive_root = vault / "99_Archive" / "MAGI_duplicate_notes" / time.strftime("%Y%m%d")
    idx = _load_index()
    duplicate_groups = 0
    moved = 0
    planned: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []

    for key, items in groups.items():
        if len(items) < 2:
            continue
        duplicate_groups += 1
        canonical = max(items, key=lambda item: _note_preference_key(item[0], item[1], item[2]))
        canonical_rel = str(canonical[0].relative_to(vault))
        for note, meta, _content in items:
            if note == canonical[0]:
                continue
            rel = str(note.relative_to(vault))
            target = archive_root / rel
            planned.append({"from": rel, "to": str(target.relative_to(vault)), "canonical": canonical_rel})
            if dry_run:
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target = target.with_name(f"{target.stem}.{int(time.time())}{target.suffix}")
                os.replace(note, target)
                idx.setdefault("notes", {}).pop(rel, None)
                moved += 1
            except Exception as e:
                errors.append({"path": rel, "error": str(e)})

    orphaned_index = _prune_missing_index_entries(idx, vault, dry_run=dry_run)
    if not dry_run:
        _replace_index(idx)
    return {
        "success": True,
        "dry_run": dry_run,
        "duplicate_groups": duplicate_groups,
        "planned_moves": len(planned),
        "moved": moved,
        "orphaned_index_prunable": len(orphaned_index),
        "orphaned_index_pruned": 0 if dry_run else len(orphaned_index),
        "errors": len(errors),
        "moves": planned[:25],
        "sample_orphaned_index": orphaned_index[:10],
        "error_details": errors[:10],
    }


# ── Ask (Notebook Q&A) ────────────────────────────────────────────

def _note_path_to_wikilink(note_path: str) -> str:
    """Convert a relative note path like 'folder/My Note.md' to '[[My Note]]'."""
    name = Path(note_path).stem
    return f"[[{name}]]"


def format_citations_for_chat(citations: List[Dict], query: str = "", scope: str = "") -> str:
    """Format raw citations list into clean markdown for messaging platforms.

    Produces a readable response with:
      - A synthesized answer header
      - Numbered citations with note path, relevance score, and snippet
      - Wikilink references for Obsidian navigation
    """
    if not citations:
        return f"No relevant notes found for: {query}" if query else "No relevant notes found."

    lines: List[str] = []

    # Header
    scope_label = f" (scope: {scope})" if scope else ""
    lines.append(f"## Obsidian Q&A{scope_label}")
    lines.append(f"**Query:** {query}" if query else "")
    lines.append(f"**{len(citations)} relevant citation(s) found:**")
    lines.append("")

    for i, c in enumerate(citations, 1):
        note_path = c.get("note_path", "unknown")
        wikilink = _note_path_to_wikilink(note_path)
        score = c.get("score", 0)
        snippet = c.get("content", "").strip()
        # Truncate long snippets for chat readability
        if len(snippet) > 200:
            snippet = snippet[:200].rstrip() + "..."
        # Clean newlines for inline display
        snippet = snippet.replace("\n", " ")

        score_pct = f"{score * 100:.0f}%" if isinstance(score, float) and score <= 1 else str(score)
        original = c.get("original_path", "")
        source_root = c.get("source_root", "")

        lines.append(f"**{i}.** {wikilink}  ")
        lines.append(f"   Note: `{note_path}` | Relevance: {score_pct}")
        if source_root:
            lines.append(f"   Source: {source_root}")
        if original:
            lines.append(f"   Original: `{original}`")
        if snippet:
            lines.append(f"   > {snippet}")
        lines.append("")

    # Footer with wikilink summary
    wikilinks = [_note_path_to_wikilink(c.get("note_path", "")) for c in citations]
    unique_links = list(dict.fromkeys(wikilinks))  # dedupe preserving order
    lines.append("**Referenced notes:** " + ", ".join(unique_links))

    return "\n".join(lines)


def task_ask(query: str, scope: str = "", top_k: int = 5) -> Dict:
    """Q&A over indexed Obsidian notes with citations.

    Scope formats:
      source:案件        - filter to a specific source root
      source:fang        - filter to fang source
      folder:<subfolder> - filter by note folder prefix
      case:<number>      - filter by case number (e.g. 2025-0002)
      vault:<name>       - filter by vault name
      tag:<tag>          - filter by tag (basic)
    """
    try:
        from skills.memory.mem_bridge import recall
    except ImportError:
        return {"success": False, "error": "mem_bridge not available"}

    # Build source filter based on scope
    # recall() uses simple `source_contains in source_string` matching
    source_filter = "obsidian"
    if scope:
        if scope.startswith("source:"):
            source_name = scope[len("source:"):]
            source_filter = f"source_root={source_name}"
        elif scope.startswith("folder:"):
            folder_name = scope[len("folder:"):]
            source_filter = f"note=20_Notes/{folder_name}"
        elif scope.startswith("case:"):
            case_num = scope[len("case:"):]
            source_filter = f"case={case_num}"
        elif scope.startswith("vault:"):
            vault_name = scope[len("vault:"):]
            source_filter = f"vault={vault_name}"
        elif scope.startswith("tag:"):
            source_filter = "obsidian"

    try:
        results = recall(query, top_k=top_k, source_contains=source_filter)
    except Exception as e:
        return {"success": False, "error": str(e)}

    citations = []
    for r in results:
        source = r.get("source", "")
        # Parse source metadata from pipe-delimited format
        # Format: doc=X|kind=Y|primary=Z|... where primary itself may contain |key=val pairs
        parts = {}
        for segment in source.split("|"):
            if "=" in segment:
                k, v = segment.split("=", 1)
                parts[k] = v

        # Prefer 'note' (generated note path) over 'path' (original file)
        note_path = parts.get("note", parts.get("path", ""))
        # If note_path is a full absolute path, try to make it relative
        if note_path.startswith("/") and "20_Notes/" in note_path:
            note_path = note_path[note_path.index("20_Notes/"):]

        citations.append({
            "content": r.get("content", "")[:300],
            "score": r.get("score", 0),
            "note_path": note_path,
            "original_path": parts.get("path", ""),
            "source_root": parts.get("source_root", ""),
            "title": parts.get("title", ""),
            "chunk": parts.get("chunk", ""),
            "case": parts.get("case", ""),
        })

    # Format citations into readable markdown
    formatted = format_citations_for_chat(citations, query=query, scope=scope or "vault-wide")

    return {
        "success": True,
        "query": query,
        "scope": scope or "vault-wide",
        "citations": citations,
        "count": len(citations),
        "formatted": formatted,
    }


# ── Obsidian 雙向同步 — 寫回 vault ──────────────────────────────────

def task_writeback(note_name: str, content: str, folder: str = "MAGI",
                   vault_path: Optional[Path] = None) -> Dict:
    """
    將 MAGI 產出的內容寫回 Obsidian vault 為 .md 檔案。

    - note_name: 筆記名稱（不含 .md）
    - content: Markdown 內容
    - folder: vault 內的目標子資料夾（預設 MAGI）
    - 若同名檔案已存在且內容相同則跳過
    - 回傳 created/updated/skipped 狀態
    """
    vp = vault_path or _get_vault_path()
    if not vp:
        return {"success": False, "error": "vault 未設定，請先執行 set_vault"}

    safe_name = re.sub(r'[\\/*?:"<>|]', "_", note_name.strip())
    if not safe_name:
        return {"success": False, "error": "筆記名稱不能為空"}

    target_dir = vp / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / f"{safe_name}.md"

    # 加入 frontmatter
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    frontmatter = f"---\nsource: MAGI\ncreated: {now}\ntags: [magi-sync]\n---\n\n"
    full_content = frontmatter + content.strip() + "\n"

    # 檢查是否已存在且內容相同
    if target_file.exists():
        try:
            existing = target_file.read_text(encoding="utf-8")
            # 比較去掉 frontmatter 後的正文
            existing_body = re.sub(r"^---.*?---\s*", "", existing, flags=re.DOTALL).strip()
            new_body = content.strip()
            if existing_body == new_body:
                return {"success": True, "status": "skipped", "path": str(target_file),
                        "reason": "content_unchanged"}
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1023, exc_info=True)
        # 更新：保留原始 created，更新 modified
        full_content = re.sub(
            r"created: .+",
            f"created: {now}\nmodified: {now}",
            full_content,
            count=1,
        )
        target_file.write_text(full_content, encoding="utf-8")
        return {"success": True, "status": "updated", "path": str(target_file)}

    target_file.write_text(full_content, encoding="utf-8")
    return {"success": True, "status": "created", "path": str(target_file)}


def task_sync_case_notes(vault_path: Optional[Path] = None) -> Dict:
    """
    從 MAGI 資料庫取出案件資訊，在 30_Index/ 生成含 YAML frontmatter 的案件卡片。
    Dataview 可直接查詢這些卡片，無需 LLM。
    """
    vp = vault_path or _get_vault_path()
    if not vp:
        return {"success": False, "error": "vault 未設定"}

    try:
        _osc_dir = str(Path(MAGI_ROOT) / "skills" / "osc-orchestrator")
        if _osc_dir not in sys.path:
            sys.path.insert(0, _osc_dir)
        from osc_headless.db import DBConfig, connect_mysql
        conn = connect_mysql(DBConfig())
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT case_number, client_name, case_type, case_reason,
                   court_name, status, start_date, end_date,
                   COALESCE(NULLIF(court_case_no, ''), court_case_number, '') AS court_case_number
            FROM cases
            ORDER BY case_number DESC
            LIMIT 500
            """,
        )
        cases = cur.fetchall() or []
        cur.close()
        conn.close()
    except Exception as e:
        return {"success": False, "error": f"DB error: {e}"}

    created = 0
    updated = 0
    skipped = 0

    index_dir = vp / "30_Index"
    index_dir.mkdir(parents=True, exist_ok=True)
    now_str = time.strftime("%Y-%m-%d")

    for c in cases:
        case_no = c.get("case_number", "")
        client = c.get("client_name", "")
        if not case_no:
            continue

        # --- YAML frontmatter（供 Dataview 查詢）---
        def _esc(v):
            return str(v).replace('"', '\\"') if v else ""

        fm_lines = [
            "---",
            "type: case-card",
            f'case_number: "{_esc(case_no)}"',
            f'client_name: "{_esc(client)}"',
            f'case_type: "{_esc(c.get("case_type", ""))}"',
            f'case_reason: "{_esc(c.get("case_reason", ""))}"',
            f'court_name: "{_esc(c.get("court_name", ""))}"',
            f'court_case_number: "{_esc(c.get("court_case_number", ""))}"',
            f'status: "{_esc(c.get("status", ""))}"',
            f'start_date: "{_esc(c.get("start_date", ""))}"',
            f'updated: "{now_str}"',
            "tags: [case-card]",
            "---",
        ]
        frontmatter = "\n".join(fm_lines)

        # --- 正文（人類可讀）---
        title = f"{client} ({case_no})" if client else case_no
        body_lines = [f"# {title}", ""]
        if c.get("court_name"):
            body_lines.append(f"**法院**: {c['court_name']}")
        if c.get("court_case_number"):
            body_lines.append(f"**法院案號**: {c['court_case_number']}")
        if c.get("case_reason"):
            body_lines.append(f"**案由**: {c['case_reason']}")
        if c.get("case_type"):
            body_lines.append(f"**案件類型**: {c['case_type']}")
        if c.get("status"):
            body_lines.append(f"**狀態**: {c['status']}")
        if c.get("start_date"):
            body_lines.append(f"**開始日期**: {c['start_date']}")

        body_lines += [
            "",
            "## 相關文件",
            "",
            "```dataview",
            f'LIST file.name FROM "20_Notes" WHERE case_number = "{case_no}" SORT file.mtime DESC LIMIT 20',
            "```",
        ]
        body = "\n".join(body_lines)
        full_content = frontmatter + "\n\n" + body + "\n"

        # --- 寫檔（dedup：正文相同則跳過）---
        safe_no = re.sub(r'[\\/*?:"<>|]', "_", case_no)
        target_file = index_dir / f"{safe_no}.md"
        if target_file.exists():
            try:
                existing = target_file.read_text(encoding="utf-8")
                existing_body = re.sub(r"^---.*?---\s*", "", existing, flags=re.DOTALL).strip()
                if existing_body == body.strip():
                    skipped += 1
                    continue
            except Exception:
                pass
            target_file.write_text(full_content, encoding="utf-8")
            updated += 1
        else:
            target_file.write_text(full_content, encoding="utf-8")
            created += 1

    return {
        "success": True,
        "total": len(cases),
        "created": created,
        "updated": updated,
        "skipped": skipped,
    }


# ── CLI Entry Point ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MAGI Obsidian Integration")
    parser.add_argument("--task", type=str, default="status",
                        choices=["status", "list_vaults", "set_vault", "search",
                                 "read", "ingest", "ingest_source", "ask",
                                 "writeback", "repair_notes", "cleanup_duplicate_notes",
                                 "sync_case_notes", "help"])
    parser.add_argument("--vault-path", type=str, default="")
    parser.add_argument("--query", type=str, default="")
    parser.add_argument("--note", type=str, default="")
    parser.add_argument("--folder", type=str, default="")
    parser.add_argument("--source", type=str, default="",
                        help="Source root for ingest_source: 案件|fang|結案|舊案")
    parser.add_argument("--subpath", type=str, default="",
                        help="Relative subfolder within source root")
    parser.add_argument("--limit", type=int, default=50,
                        help="Max files to process in ingest_source")
    parser.add_argument("--scope", type=str, default="")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--tags", type=str, default="",
                        help="Comma-separated tags to filter notes by frontmatter (for ingest)")
    parser.add_argument("--since", type=str, default="",
                        help="ISO date string; only ingest notes modified after this date (e.g. 2026-03-01)")
    parser.add_argument("--include-folders", type=str, default="",
                        help="Folder whitelist: 'high-value' or comma-separated names (e.g. '04_我方歷次書狀,08_筆錄')")
    parser.add_argument("--exclude-folders", type=str, default="",
                        help="Folder blacklist: 'default' or comma-separated names (e.g. '06_閱卷資料')")
    parser.add_argument("--json-out", type=str, default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reextract", action="store_true",
                        help="For repair_notes, re-extract weak notes from source files when available")
    parser.add_argument("--case-number", type=str, default="",
                        help="For repair_notes, only repair notes whose path contains this case number")
    args = parser.parse_args()

    vault_override = Path(args.vault_path) if args.vault_path else None

    if args.task == "status":
        result = task_status()
    elif args.task == "list_vaults":
        result = task_list_vaults()
    elif args.task == "set_vault":
        if not args.vault_path:
            result = {"success": False, "error": "Provide --vault-path"}
        else:
            result = task_set_vault(args.vault_path)
    elif args.task == "search":
        if not args.query:
            result = {"success": False, "error": "Provide --query"}
        else:
            result = task_search(args.query, vault_override)
    elif args.task == "read":
        if not args.note:
            result = {"success": False, "error": "Provide --note"}
        else:
            result = task_read(args.note, vault_override)
    elif args.task == "ingest":
        tags_list = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None
        result = task_ingest(
            folder=args.folder,
            vault_path=vault_override,
            force=args.force,
            tags=tags_list,
            since=args.since or None,
        )
    elif args.task == "ingest_source":
        if not args.source:
            result = {"success": False, "error": "Provide --source (案件|fang|結案|舊案)"}
        else:
            result = task_ingest_source(
                source=args.source,
                subpath=args.subpath,
                limit=args.limit,
                force=args.force,
                include_folders=args.include_folders or None,
                exclude_folders=args.exclude_folders or None,
            )
    elif args.task == "ask":
        if not args.query:
            result = {"success": False, "error": "Provide --query"}
        else:
            result = task_ask(args.query, args.scope, args.top_k)
    elif args.task == "writeback":
        if not args.note or not args.query:
            result = {"success": False, "error": "Provide --note <name> --query <content>"}
        else:
            result = task_writeback(args.note, args.query, folder=args.folder or "MAGI",
                                    vault_path=vault_override)
    elif args.task == "repair_notes":
        result = task_repair_notes(
            vault_path=vault_override,
            limit=args.limit,
            force=args.force,
            reextract=args.reextract,
            case_number=args.case_number,
        )
    elif args.task == "cleanup_duplicate_notes":
        result = task_cleanup_duplicate_notes(vault_path=vault_override, dry_run=args.dry_run)
    elif args.task == "sync_case_notes":
        result = task_sync_case_notes(vault_path=vault_override)
    elif args.task == "help":
        result = {
            "commands": [
                "status - Show vault config and index stats",
                "list_vaults - Discover vaults from Obsidian config",
                "set_vault --vault-path <path> - Set active vault",
                "search --query <text> - Search notes",
                "read --note <path> - Read a note",
                "ingest [--folder <path>] [--tags <t1,t2>] [--since <ISO-date>] [--force] - Ingest vault notes into vector memory",
                "ingest_source --source 案件|fang|結案|舊案 [--subpath <rel>] [--limit N] [--force] [--include-folders high-value|folder1,folder2] [--exclude-folders default|folder1,folder2] - Extract & ingest from source",
                "ask --query <text> [--scope source:案件|folder:X|case:2025-0002] - Q&A with citations",
                "writeback --note <name> --query <content> [--folder <subfolder>] - Write note back to vault",
                "repair_notes [--force] [--reextract] [--limit N] [--case-number 2026-0001] - Rewrite summary notes into structured v2 format",
                "cleanup_duplicate_notes [--dry-run] - Move duplicate summary notes out of 20_Notes",
                "sync_case_notes - Sync active cases from DB to vault as .md notes",
            ]
        }
    else:
        result = {"success": False, "error": f"Unknown task: {args.task}"}

    output = json.dumps(result, ensure_ascii=False, indent=2)
    print(output)

    if args.json_out:
        Path(args.json_out).write_text(output, encoding="utf-8")


if __name__ == "__main__":
    main()
