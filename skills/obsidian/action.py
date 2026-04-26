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
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
MAGI_ROOT = os.path.abspath(os.path.join(SKILL_DIR, "..", ".."))
if MAGI_ROOT not in sys.path:
    sys.path.insert(0, MAGI_ROOT)

from api.case_path_mapper import default_case_roots, preferred_case_roots

# ── Config ──────────────────────────────────────────────────────────

AGENT_DIR = Path(MAGI_ROOT) / ".agent"
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
    "10_判決書",
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
    idx["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")


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

    # Apply --since filter (by file mtime)
    if since_ts is not None:
        notes = [n for n in notes if n.stat().st_mtime >= since_ts]

    ingested = 0
    skipped = 0
    filtered_by_tag = 0
    errors = []
    total_chunks = 0

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
        if not force and prev.get("hash") == h and prev.get("mtime") == mtime:
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
        "error_details": errors[:10] if errors else [],
    }
    if tags:
        result_dict["tags_filter"] = tags
        result_dict["filtered_by_tag"] = filtered_by_tag
    if since:
        result_dict["since"] = since

    return result_dict


# ── Phase 2: Selective Source Ingest ───────────────────────────────

# Source root mapping (from source_manifest.json)
_CASE_ROOTS = preferred_case_roots(include_closed=True)
_FALLBACK_CASE_ROOTS = default_case_roots(include_closed=True)
_ACTIVE_CASE_ROOT = _CASE_ROOTS[0] if _CASE_ROOTS else (_FALLBACK_CASE_ROOTS[0] if _FALLBACK_CASE_ROOTS else Path.home() / "Library" / "CloudStorage" / "SynologyDrive-homes" / "01_案件")
_CLOSED_CASE_ROOT = _CASE_ROOTS[1] if len(_CASE_ROOTS) > 1 else (_FALLBACK_CASE_ROOTS[1] if len(_FALLBACK_CASE_ROOTS) > 1 else _ACTIVE_CASE_ROOT)
SOURCE_ROOTS = {
    "案件": Path(_ACTIVE_CASE_ROOT),
    "結案": Path(_CLOSED_CASE_ROOT),
    "舊案": Path(_CLOSED_CASE_ROOT) / "舊案",
    "fang": Path("/Volumes/lumi/fang"),
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
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    INGEST_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _generate_frontmatter(
    source_root: str,
    source_path: str,
    source_relpath: str,
    file_type: str,
    mtime: int,
    case_info: Optional[Dict] = None,
    doc_key: str = "",
    file_hash_val: str = "",
) -> str:
    """Generate YAML frontmatter for an extracted note."""
    lines = ["---"]
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

    # Vector pipeline
    try:
        from skills.documents.vector_pipeline import ingest_text_to_vector_memory
    except ImportError:
        ingest_text_to_vector_memory = None

    # Collect files (with folder filter)
    all_files = []
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
        all_files.append(f)

    # Sort by mtime (newest first for relevance)
    all_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

    # Apply limit
    if limit > 0:
        all_files = all_files[:limit]

    total_to_process = len(all_files)
    vault_name = vault.name
    processed = 0
    skipped = 0
    short_text = 0
    errors = []
    warnings = []
    malformed_skipped = 0
    notes_created = []
    t_start = time.time()

    print(f"[ingest_source] 開始匯入: source={source}, subpath={subpath or '(root)'}, "
          f"候選檔案={total_to_process}, 排除={filtered_by_folder} (folder filter)", flush=True)
    if _include_set:
        print(f"  include: {sorted(_include_set)}", flush=True)
    if _exclude_set:
        print(f"  exclude: {sorted(_exclude_set)}", flush=True)

    for i, f in enumerate(all_files):
        relpath = str(f.relative_to(root))
        state_key = f"{source}/{relpath}"
        mtime = int(f.stat().st_mtime)
        fhash = file_hash(f)

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
            skipped += 1
            continue

        # Extract text — per-file try/except 確保單檔失敗不炸整個 ingest job
        try:
            result = extract_text(f)
        except BaseException as e:
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
                continue
            logging.getLogger(__name__).warning(
                "obsidian_ingest: extraction failed %s: %s", relpath, err_msg
            )
            errors.append({"path": relpath, "error": err_msg})
            continue

        text = result["text"]
        if not text or len(text.strip()) < MIN_EXTRACTED_CHARS:
            short_text += 1
            continue

        # Resolve case info from path
        case_info = _resolve_case_info(relpath)

        # Build note path mirroring source structure
        rel_parent = Path(relpath).parent
        note_subdir = notes_dir / rel_parent
        note_subdir.mkdir(parents=True, exist_ok=True)

        note_name = _sanitize_note_name(f"summary__{f.stem}")
        note_path = note_subdir / f"{note_name}.md"

        # Generate frontmatter
        frontmatter = _generate_frontmatter(
            source_root=source,
            source_path=str(f),
            source_relpath=relpath,
            file_type=f.suffix.lstrip(".").lower(),
            mtime=mtime,
            case_info=case_info,
            file_hash_val=fhash,
        )

        # Build note content
        title = f.stem
        # First ~200 chars as excerpt
        excerpt = text[:200].replace("\n", " ").strip()
        if len(text) > 200:
            excerpt += "..."

        note_content = f"""{frontmatter}

# {title}

**Source:** `{relpath}`
**Type:** {f.suffix.lower()} | **Pages:** {result.get('pages', '?')} | **Method:** {result.get('method', '?')}

## Excerpt

> {excerpt}

## Full Text

{text}
"""
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
                    text=text,
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
            "note_path": note_rel,
            "doc_key": doc_key,
            "chunks": chunks_written,
            "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

        notes_created.append(note_rel)
        processed += 1

        # Checkpoint every 50 files to survive kills
        if processed > 0 and processed % 50 == 0:
            state["files"] = files_state
            _save_ingest_state(state)
            print(f"[ingest_source] checkpoint saved ({processed} processed)", flush=True)

    # Save state
    state["files"] = files_state
    _save_ingest_state(state)

    # Also update the vault index
    # Build reverse mapping: note_path -> state entry (files_state is keyed by source path)
    note_path_to_state = {}
    for _sk, _sv in files_state.items():
        np = _sv.get("note_path", "")
        if np:
            note_path_to_state[np] = _sv

    idx = _load_index()
    for nc in notes_created:
        note_full = vault / nc
        if note_full.exists():
            content = note_full.read_text(encoding="utf-8", errors="replace")
            h = _note_hash(content)
            mt = int(note_full.stat().st_mtime)
            st = note_path_to_state.get(nc, {})
            idx.setdefault("notes", {})[nc] = {
                "hash": h,
                "mtime": mt,
                "doc_key": st.get("doc_key", ""),
                "chunks": st.get("chunks", 0),
                "ingested_at": st.get("ingested_at", ""),
            }
    _save_index(idx)

    elapsed = time.time() - t_start
    print(f"[ingest_source] 完成！耗時 {elapsed:.1f}s  "
          f"匯入={processed} 跳過={skipped} 文字太短={short_text} 警告={len(warnings)} 錯誤={len(errors)} "
          f"folder排除={filtered_by_folder}", flush=True)

    return {
        "success": True,
        "source": source,
        "subpath": subpath or "(root)",
        "scanned": total_to_process,
        "filtered_by_folder": filtered_by_folder,
        "processed": processed,
        "skipped": skipped,
        "malformed_pdf_skipped": malformed_skipped,
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
                                 "writeback", "sync_case_notes", "help"])
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
