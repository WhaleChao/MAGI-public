#!/usr/bin/env python3
"""
Knowledge Lint — MAGI 知識品質掃描
===================================

Inspired by Karpathy's LLM Wiki "lint" operation. Periodically scans
the knowledge base for quality issues:

1. **Duplicate detection**: Find near-duplicate entries in magi_brain
2. **Contradiction scan**: Use LLM to detect conflicting facts across
   documents for the same case
3. **Staleness check**: Flag wiki pages whose source notes have changed
4. **Orphan detection**: Find vector entries with no corresponding
   Obsidian note, or notes with no vector embedding
5. **Insight quality**: Flag insights that are too short, degraded,
   or contain boilerplate

Output: JSON report + optional Obsidian note with findings.

Usage:
    # Full lint scan
    python scripts/knowledge_lint.py

    # Quick scan (no LLM, just structural checks)
    python scripts/knowledge_lint.py --quick

    # Write results to Obsidian vault
    python scripts/knowledge_lint.py --write-to-vault

    # Dry-run (scan but don't fix)
    python scripts/knowledge_lint.py --dry-run

Cron: Runs as part of nightly cycle (夜議 agenda item)
"""

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if MAGI_ROOT not in sys.path:
    sys.path.insert(0, MAGI_ROOT)

logger = logging.getLogger("knowledge_lint")

# ── Config ──────────────────────────────────────────────────────────
AGENT_DIR = Path(MAGI_ROOT) / ".agent"
VAULT_CONFIG_PATH = AGENT_DIR / "obsidian_vault_config.json"
INDEX_PATH = AGENT_DIR / "obsidian_index.json"
WIKI_STATE_PATH = AGENT_DIR / "wiki_synthesizer_state.json"
INGEST_STATE_PATH = AGENT_DIR / "obsidian_ingest_state.json"

REPORT_DIR = Path(MAGI_ROOT) / "static"
REPORT_PATH = REPORT_DIR / "knowledge_lint_latest.json"
CLEANUP_REPORT_PATH = REPORT_DIR / "knowledge_duplicate_cleanup_latest.json"
DEFAULT_DUPLICATE_BACKUP_DIR = Path(MAGI_ROOT) / "archive" / "knowledge_duplicate_cleanup"

# Thresholds
MIN_INSIGHT_LEN = 100  # chars — insights shorter than this are flagged
DUPLICATE_SIM_THRESHOLD = 0.95  # cosine similarity for near-duplicate
MAX_LLM_CHECKS = 10  # max contradiction checks per run
DUPLICATE_PLAN_GROUP_LIMIT = 50
DELETE_BATCH_SIZE = 500

INSIGHT_DEGRADED_MARKERS = (
    "摘要失敗",
    "timeout",
    "逾時",
    "系統降級回覆",
    "無法擷取",
    "請稍後再試",
    "模型忙碌",
)


# ── Helpers ─────────────────────────────────────────────────────────

def _get_vault_path() -> Optional[Path]:
    if VAULT_CONFIG_PATH.exists():
        try:
            cfg = json.loads(VAULT_CONFIG_PATH.read_text("utf-8"))
            vp = Path(cfg.get("vault_path", ""))
            if vp.is_dir():
                return vp
        except Exception:
            pass
    return None


def _db_connect(db_name: str = "magi_brain"):
    """Connect to MariaDB."""
    import mysql.connector
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    if db_name == "magi_brain":
        return mysql.connector.connect(
            host=os.environ.get("DB_HOST", "127.0.0.1"),
            port=int(os.environ.get("DB_PORT", "3306")),
            user=os.environ.get("DB_USER", "casper_service"),
            password=os.environ.get("DB_PASSWORD", ""),
            database="magi_brain",
        )
    else:
        return mysql.connector.connect(
            host=os.environ.get("OSC_DB_HOST", "127.0.0.1"),
            port=int(os.environ.get("OSC_DB_PORT", "3306")),
            user=os.environ.get("OSC_DB_USER", "python_user"),
            password=os.environ.get("OSC_DB_PASSWORD", ""),
            database="law_firm_data",
        )


def _extract_insight_body(text: str) -> str:
    s = str(text or "").strip()
    for marker in ("實務見解：", "## 實務見解"):
        if marker in s:
            return s.split(marker, 1)[1].strip()
    return s


def _is_low_quality_insight(text: str, is_degraded: bool) -> Tuple[bool, str]:
    s = str(text or "").strip()
    if not s:
        return True, "empty"
    if any(marker.lower() in s.lower() for marker in INSIGHT_DEGRADED_MARKERS):
        return True, "degraded_marker"

    body = _extract_insight_body(s)
    placeholder_markers = (
        "未發現符合擷取規則",
        "從判決中逐字擷取",
        "列出本判決適用的法條",
    )
    body_without_placeholders = body
    for marker in placeholder_markers:
        body_without_placeholders = body_without_placeholders.replace(marker, "")
    body_without_placeholders = re.sub(r"[#_（）()：:\s\n\r\t-]+", "", body_without_placeholders)

    if len(body_without_placeholders) < MIN_INSIGHT_LEN and bool(is_degraded):
        return True, "degraded_short"
    if len(body_without_placeholders) < 25:
        return True, "substantive_body_too_short"
    return False, ""


def _vector_rows_for_doc_key(conn, doc_key: str) -> int:
    if not doc_key:
        return 0
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM documents WHERE source LIKE %s",
            (f"%doc={doc_key}%",),
        )
        row = cur.fetchone() or {}
        return int(row.get("cnt") or 0)
    finally:
        cur.close()


def _parse_id_list(ids_text: str) -> List[int]:
    ids = []
    for part in str(ids_text or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids


def _chunked_ids(ids: List[int], size: int = DELETE_BATCH_SIZE) -> List[List[int]]:
    if not ids:
        return []
    size = max(1, int(size))
    return [ids[i : i + size] for i in range(0, len(ids), size)]


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return value


def _fetch_duplicate_rows(conn, max_groups: int = DUPLICATE_PLAN_GROUP_LIMIT) -> List[Dict]:
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT MD5(content) AS h, COUNT(*) AS cnt, "
        "GROUP_CONCAT(id ORDER BY id SEPARATOR ',') AS ids, "
        "MIN(CHAR_LENGTH(content)) AS min_len "
        "FROM documents "
        "GROUP BY MD5(content) "
        "HAVING COUNT(*) > 1 "
        "ORDER BY cnt DESC "
        "LIMIT %s",
        (max(1, int(max_groups)),),
    )
    rows = cur.fetchall()
    cur.close()
    return rows


def _fetch_documents_for_ids(conn, ids: List[int]) -> List[Dict]:
    if not ids:
        return []
    cur = conn.cursor(dictionary=True)
    ph = ",".join(["%s"] * len(ids))
    cur.execute(
        f"SELECT id, content, source, created_at, synced FROM documents WHERE id IN ({ph}) ORDER BY id",
        tuple(ids),
    )
    rows = cur.fetchall()
    cur.close()
    return rows


def _fetch_vectors_for_ids(conn, ids: List[int]) -> List[Dict]:
    if not ids:
        return []
    cur = conn.cursor(dictionary=True)
    ph = ",".join(["%s"] * len(ids))
    cur.execute(
        f"SELECT doc_id, embedding FROM vectors WHERE doc_id IN ({ph}) ORDER BY doc_id",
        tuple(ids),
    )
    rows = cur.fetchall()
    cur.close()
    return rows


def _delete_vectors_for_ids(conn, ids: List[int]) -> int:
    if not ids:
        return 0
    deleted = 0
    cur = conn.cursor()
    for batch in _chunked_ids(ids):
        ph = ",".join(["%s"] * len(batch))
        cur.execute(f"DELETE FROM vectors WHERE doc_id IN ({ph})", tuple(batch))
        deleted += int(cur.rowcount or 0)
    cur.close()
    return deleted


def _delete_documents_for_ids(conn, ids: List[int]) -> int:
    if not ids:
        return 0
    deleted = 0
    cur = conn.cursor()
    for batch in _chunked_ids(ids):
        ph = ",".join(["%s"] * len(batch))
        cur.execute(f"DELETE FROM documents WHERE id IN ({ph})", tuple(batch))
        deleted += int(cur.rowcount or 0)
    cur.close()
    return deleted


def _count_documents_for_ids(conn, ids: List[int]) -> int:
    if not ids:
        return 0
    total = 0
    cur = conn.cursor()
    for batch in _chunked_ids(ids):
        ph = ",".join(["%s"] * len(batch))
        cur.execute(f"SELECT COUNT(*) FROM documents WHERE id IN ({ph})", tuple(batch))
        total += int(cur.fetchone()[0] or 0)
    cur.close()
    return total


def _count_vectors_for_ids(conn, ids: List[int]) -> int:
    if not ids:
        return 0
    total = 0
    cur = conn.cursor()
    for batch in _chunked_ids(ids):
        ph = ",".join(["%s"] * len(batch))
        cur.execute(f"SELECT COUNT(*) FROM vectors WHERE doc_id IN ({ph})", tuple(batch))
        total += int(cur.fetchone()[0] or 0)
    cur.close()
    return total


def _fetch_ids_for_hash(conn, hash_value: str) -> List[int]:
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM documents WHERE MD5(content) = %s ORDER BY id",
        (hash_value,),
    )
    ids = [int(row[0]) for row in cur.fetchall()]
    cur.close()
    return ids


def _fetch_existing_document_ids(conn, ids: List[int]) -> set:
    if not ids:
        return set()
    existing = set()
    cur = conn.cursor()
    for batch in _chunked_ids(ids):
        ph = ",".join(["%s"] * len(batch))
        cur.execute(f"SELECT id FROM documents WHERE id IN ({ph})", tuple(batch))
        existing.update(int(row[0]) for row in cur.fetchall())
    cur.close()
    return existing


def _fetch_duplicate_counts_for_hashes(conn, hashes: List[str]) -> Dict[str, int]:
    if not hashes:
        return {}
    counts: Dict[str, int] = {}
    cur = conn.cursor()
    for batch in _chunked_ids([h for h in hashes if h], size=200):
        if not batch:
            continue
        ph = ",".join(["%s"] * len(batch))
        cur.execute(
            f"SELECT MD5(content) AS h, COUNT(*) AS cnt "
            f"FROM documents "
            f"WHERE MD5(content) IN ({ph}) "
            f"GROUP BY MD5(content)",
            tuple(batch),
        )
        for row in cur.fetchall():
            h = str(row[0] or "")
            cnt = int(row[1] or 0)
            if h:
                counts[h] = counts.get(h, 0) + cnt
    cur.close()
    return counts


def _insert_document_rows(conn, rows: List[Dict]) -> int:
    if not rows:
        return 0
    cur = conn.cursor()
    sql = (
        "INSERT INTO documents (id, content, source, created_at, synced) "
        "VALUES (%s, %s, %s, %s, %s)"
    )
    payload = [
        (
            int(r.get("id")),
            r.get("content"),
            r.get("source"),
            r.get("created_at"),
            int(r.get("synced", 0) or 0),
        )
        for r in rows
    ]
    cur.executemany(sql, payload)
    inserted = int(cur.rowcount or 0)
    cur.close()
    return inserted


def _insert_vector_rows(conn, rows: List[Dict]) -> int:
    if not rows:
        return 0
    cur = conn.cursor()
    sql = "INSERT INTO vectors (doc_id, embedding) VALUES (%s, %s)"
    payload = [
        (int(r.get("doc_id")), r.get("embedding"))
        for r in rows
    ]
    cur.executemany(sql, payload)
    inserted = int(cur.rowcount or 0)
    cur.close()
    return inserted


def _load_cleanup_summary() -> Dict:
    if not CLEANUP_REPORT_PATH.exists():
        return {}
    try:
        data = json.loads(CLEANUP_REPORT_PATH.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_cleanup_summary(summary: Dict) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    CLEANUP_REPORT_PATH.write_text(
        json.dumps(_json_safe(summary), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _duplicate_cleanup_gate(dupe_rows: List[Dict]) -> Dict:
    latest = _load_cleanup_summary()
    if not dupe_rows:
        if latest.get("mode") == "apply" and latest.get("verified") is True:
            return {
                "status": "applied_verified",
                "reason": "",
                "report_path": str(CLEANUP_REPORT_PATH),
            }
        return {"status": "not_required", "reason": ""}

    if not latest:
        return {
            "status": "pending_apply",
            "reason": "duplicate cleanup has not been applied yet",
            "report_path": str(CLEANUP_REPORT_PATH),
        }

    latest_status = str(latest.get("status", "") or "").strip().lower()
    if latest_status in {"blocked_by_manual_backup", "unsupported_format"}:
        return {
            "status": latest_status,
            "reason": latest.get("reason", ""),
            "report_path": str(CLEANUP_REPORT_PATH),
        }

    if latest.get("mode") == "apply" and latest.get("verified") is True:
        after = latest.get("after", {}) if isinstance(latest.get("after"), dict) else {}
        if int(after.get("duplicate_groups", 0) or 0) == 0:
            return {
                "status": "blocked_by_regeneration",
                "reason": (
                    "duplicate cleanup was verified to zero, but duplicates reappeared; "
                    "a background source is re-ingesting duplicate contents"
                ),
                "report_path": str(CLEANUP_REPORT_PATH),
            }
        return {
            "status": "partially_applied",
            "reason": "duplicates still remain after last apply",
            "report_path": str(CLEANUP_REPORT_PATH),
        }

    return {
        "status": "pending_apply",
        "reason": "latest duplicate cleanup run is not verified",
        "report_path": str(CLEANUP_REPORT_PATH),
    }


def build_duplicate_cleanup_plan(dupes: List[Dict], max_groups: int = DUPLICATE_PLAN_GROUP_LIMIT) -> Dict:
    """Build dry-run cleanup plan (keep min id, remove the rest) for duplicate vectors."""
    groups = []
    total_delete = 0

    for row in (dupes or [])[: max(1, int(max_groups))]:
        ids = _parse_id_list(row.get("ids", ""))
        if len(ids) < 2:
            continue
        ids = sorted(ids)
        keep_id = ids[0]
        remove_ids = ids[1:]
        total_delete += len(remove_ids)
        groups.append(
            {
                "hash": row.get("h"),
                "count": int(row.get("cnt", len(ids))),
                "keep_id": keep_id,
                "remove_ids": remove_ids,
            }
        )

    sql_preview = []
    for g in groups[:10]:
        if not g["remove_ids"]:
            continue
        ids_csv = ",".join(str(i) for i in g["remove_ids"])
        sql_preview.append(f"DELETE FROM vectors WHERE doc_id IN ({ids_csv});")
        sql_preview.append(f"DELETE FROM documents WHERE id IN ({ids_csv});")

    return {
        "mode": "dry_run",
        "groups": groups,
        "groups_planned": len(groups),
        "estimated_delete_documents": total_delete,
        "estimated_delete_vectors": total_delete,
        "sql_preview": sql_preview,
        "notes": [
            "先備份再刪除；本計畫預設只輸出，不自動執行。",
            "每組保留最小 id 作為 canonical entry。",
        ],
    }


def _backup_faiss_files(backup_dir: Path) -> Dict:
    try:
        from skills.memory import faiss_index as fi

        index_dir = Path(getattr(fi, "INDEX_DIR", Path(MAGI_ROOT) / "skills" / "memory" / "index_cache"))
        target_files = [
            index_dir / getattr(fi, "INDEX_FILE", "mem_index.faiss"),
            index_dir / getattr(fi, "IDMAP_FILE", "mem_idmap.npy"),
            index_dir / "meta.json",
        ]
    except Exception:
        return {
            "ok": False,
            "status": "unsupported_format",
            "reason": "unable to resolve FAISS index paths",
            "path": "",
            "files": [],
        }

    backup_target = backup_dir / "faiss"
    backup_target.mkdir(parents=True, exist_ok=True)
    copied = []
    for src in target_files:
        if not src.exists():
            continue
        dst = backup_target / src.name
        shutil.copy2(src, dst)
        copied.append(src.name)

    return {
        "ok": True,
        "status": "ok",
        "path": str(backup_target),
        "files": copied,
        "source_dir": str(index_dir),
    }


def _restore_faiss_files(faiss_backup: Dict) -> Dict:
    if not isinstance(faiss_backup, dict) or not faiss_backup.get("ok"):
        return {"restored": False, "reason": "no faiss backup"}

    source_path = Path(str(faiss_backup.get("path", "")))
    source_dir = source_path
    live_dir_str = str(faiss_backup.get("source_dir", "") or "").strip()
    live_dir = Path(live_dir_str) if live_dir_str else None
    if not source_dir.exists() or live_dir is None:
        return {"restored": False, "reason": "invalid faiss backup path"}
    live_dir.mkdir(parents=True, exist_ok=True)

    restored = 0
    for file_name in faiss_backup.get("files", []):
        src = source_dir / str(file_name)
        if not src.exists():
            continue
        dst = live_dir / str(file_name)
        shutil.copy2(src, dst)
        restored += 1
    return {"restored": restored > 0, "files_restored": restored}


def _rebuild_faiss_index() -> Dict:
    try:
        from skills.memory.faiss_index import FAISSMemoryIndex

        idx = FAISSMemoryIndex.get_instance()
        n = idx.build_from_db(
            {
                "host": os.environ.get("DB_HOST", "127.0.0.1"),
                "port": int(os.environ.get("DB_PORT", "3306")),
                "user": os.environ.get("DB_USER", "casper_service"),
                "password": os.environ.get("DB_PASSWORD", ""),
                "database": "magi_brain",
            }
        )
        return {"ok": True, "vectors_indexed": int(n), "index_type": getattr(idx, "index_type", "unknown")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _prepare_backup_payload(
    conn,
    remove_ids: List[int],
    keep_ids: List[int],
    groups: List[Dict],
    backup_dir: Path,
) -> Tuple[Path, Dict]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    docs_rows = _fetch_documents_for_ids(conn, remove_ids)
    vector_rows = _fetch_vectors_for_ids(conn, remove_ids)
    payload = {
        "created_at": datetime.now().isoformat(),
        "remove_ids": [int(i) for i in remove_ids],
        "keep_ids": [int(i) for i in keep_ids],
        "groups": groups,
        "documents": [_json_safe(r) for r in docs_rows],
        "vectors": [_json_safe(r) for r in vector_rows],
    }
    backup_path = backup_dir / "duplicate_cleanup_backup.json"
    backup_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return backup_path, payload


def _restore_from_backup_payload(conn, payload: Dict) -> Dict:
    docs = payload.get("documents", []) if isinstance(payload, dict) else []
    vectors = payload.get("vectors", []) if isinstance(payload, dict) else []
    inserted_docs = _insert_document_rows(conn, docs)
    inserted_vectors = _insert_vector_rows(conn, vectors)
    return {
        "inserted_documents": inserted_docs,
        "inserted_vectors": inserted_vectors,
    }


def _verify_duplicate_cleanup(conn, groups: List[Dict], removed_ids: List[int], faiss_ok: bool = True) -> Dict:
    keep_ok = True
    remove_ok = True
    hash_ok = True
    targeted_duplicate_groups = 0
    targeted_extra_entries = 0

    if _count_documents_for_ids(conn, removed_ids) != 0:
        remove_ok = False
    if _count_vectors_for_ids(conn, removed_ids) != 0:
        remove_ok = False

    keep_ids = [int(g.get("keep_id", 0) or 0) for g in groups if int(g.get("keep_id", 0) or 0) > 0]
    existing_keep_ids = _fetch_existing_document_ids(conn, keep_ids)
    if any(keep_id not in existing_keep_ids for keep_id in keep_ids):
        keep_ok = False

    hashes = sorted({str(g.get("hash", "") or "") for g in groups if g.get("hash")})
    duplicate_counts = _fetch_duplicate_counts_for_hashes(conn, hashes)
    for h in hashes:
        cnt = int(duplicate_counts.get(h, 0) or 0)
        extra = max(0, cnt - 1)
        targeted_extra_entries += extra
        if extra > 0:
            targeted_duplicate_groups += 1

    if targeted_duplicate_groups > 0:
        hash_ok = False

    ok = keep_ok and remove_ok and hash_ok and bool(faiss_ok)
    return {
        "ok": ok,
        "keep_ok": keep_ok,
        "remove_ok": remove_ok,
        "hash_ok": hash_ok,
        "faiss_ok": bool(faiss_ok),
        "targeted_duplicate_groups": targeted_duplicate_groups,
        "targeted_extra_entries": targeted_extra_entries,
    }


def cleanup_duplicate_vectors(
    *,
    apply: bool = False,
    max_groups: int = DUPLICATE_PLAN_GROUP_LIMIT,
    backup_dir: Optional[Path] = None,
    rebuild_faiss: bool = True,
    auto_rollback: bool = True,
    quiet: bool = False,
) -> Dict:
    backup_root = Path(backup_dir or DEFAULT_DUPLICATE_BACKUP_DIR)
    started = time.time()

    conn = _db_connect("magi_brain")
    try:
        dupes_before = _fetch_duplicate_rows(conn, max_groups=max_groups)
        total_extra_before = sum(int(d.get("cnt", 0) or 0) - 1 for d in dupes_before)
        plan = build_duplicate_cleanup_plan(dupes_before, max_groups=max_groups)
        groups = plan.get("groups", [])
        remove_ids = [int(i) for g in groups for i in g.get("remove_ids", [])]
        keep_ids = [int(g.get("keep_id")) for g in groups if g.get("keep_id")]

        summary = {
            "ts": datetime.now().isoformat(),
            "mode": "apply" if apply else "dry_run",
            "status": "ok" if not apply else "pending",
            "before": {
                "duplicate_groups": len(dupes_before),
                "total_extra_entries": total_extra_before,
            },
            "after": {
                "duplicate_groups": len(dupes_before),
                "total_extra_entries": total_extra_before,
            },
            "planned_groups": len(groups),
            "removed_count": 0,
            "backup_path": "",
            "verified": False,
            "cleanup_plan": plan,
            "verification": {},
            "faiss": {},
            "rollback": {"attempted": False, "restored": False},
            "rollback_hint": "",
            "elapsed_sec": 0.0,
        }

        if not groups:
            summary["status"] = "ok"
            summary["verified"] = True
            summary["verification"] = {"ok": True, "reason": "no duplicates"}
            summary["elapsed_sec"] = round(time.time() - started, 2)
            _write_cleanup_summary(summary)
            if not quiet:
                print("✅ duplicate cleanup: no duplicate groups found")
            return summary

        if not apply:
            summary["status"] = "dry_run"
            summary["verification"] = {"ok": False, "reason": "dry_run_no_apply"}
            summary["elapsed_sec"] = round(time.time() - started, 2)
            _write_cleanup_summary(summary)
            if not quiet:
                print(
                    f"🔍 duplicate cleanup dry-run: {len(groups)} groups, "
                    f"{len(remove_ids)} removable entries"
                )
            return summary

        run_dir = backup_root / datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        backup_path, payload = _prepare_backup_payload(
            conn,
            remove_ids=remove_ids,
            keep_ids=keep_ids,
            groups=groups,
            backup_dir=run_dir,
        )
        summary["backup_path"] = str(backup_path)
        summary["rollback_hint"] = (
            f"使用備份檔 {backup_path} 重新插回 documents/vectors，"
            "並將 backup/faiss 目錄覆蓋回 live FAISS index。"
        )

        faiss_backup = _backup_faiss_files(run_dir)
        summary["faiss"]["backup"] = faiss_backup
        if not faiss_backup.get("ok"):
            summary["status"] = str(faiss_backup.get("status") or "unsupported_format")
            summary["reason"] = str(faiss_backup.get("reason") or "FAISS backup failed")
            summary["elapsed_sec"] = round(time.time() - started, 2)
            _write_cleanup_summary(summary)
            return summary

        try:
            _delete_vectors_for_ids(conn, remove_ids)
            removed_docs = _delete_documents_for_ids(conn, remove_ids)
            conn.commit()
            summary["removed_count"] = int(removed_docs)
        except Exception as e:
            conn.rollback()
            summary["status"] = "blocked_by_manual_backup"
            summary["reason"] = f"delete_failed:{e}"
            summary["elapsed_sec"] = round(time.time() - started, 2)
            _write_cleanup_summary(summary)
            return summary

        if rebuild_faiss:
            summary["faiss"]["rebuild"] = _rebuild_faiss_index()
        else:
            summary["faiss"]["rebuild"] = {"ok": True, "skipped": True}
        faiss_rebuild_ok = bool((summary["faiss"].get("rebuild") or {}).get("ok"))

        dupes_after = _fetch_duplicate_rows(conn, max_groups=max_groups)
        total_extra_after = sum(int(d.get("cnt", 0) or 0) - 1 for d in dupes_after)
        summary["after"] = {
            "duplicate_groups": len(dupes_after),
            "total_extra_entries": total_extra_after,
        }

        try:
            verification = _verify_duplicate_cleanup(conn, groups, remove_ids, faiss_ok=faiss_rebuild_ok)
        except Exception as e:
            verification = {
                "ok": False,
                "keep_ok": False,
                "remove_ok": False,
                "hash_ok": False,
                "faiss_ok": bool(faiss_rebuild_ok),
                "targeted_duplicate_groups": -1,
                "targeted_extra_entries": -1,
                "error": f"verify_exception:{e}",
            }
        summary["verification"] = verification
        summary["verified"] = bool(verification.get("ok"))
        summary["status"] = "ok" if summary["verified"] else "error"

        if not summary["verified"] and auto_rollback:
            summary["rollback"]["attempted"] = True
            try:
                rollback_result = _restore_from_backup_payload(conn, payload)
                faiss_restore = _restore_faiss_files(faiss_backup)
                conn.commit()
                summary["rollback"]["restored"] = True
                summary["rollback"]["result"] = rollback_result
                summary["rollback"]["faiss"] = faiss_restore
            except Exception as e:
                conn.rollback()
                summary["rollback"]["restored"] = False
                summary["rollback"]["error"] = str(e)

        summary["elapsed_sec"] = round(time.time() - started, 2)
        _write_cleanup_summary(summary)
        return summary
    finally:
        conn.close()


# ── Lint Checks ─────────────────────────────────────────────────────

def check_duplicate_vectors() -> Dict:
    """Find near-duplicate content in magi_brain.documents (by MD5)."""
    try:
        conn = _db_connect("magi_brain")
        dupes = _fetch_duplicate_rows(conn, max_groups=DUPLICATE_PLAN_GROUP_LIMIT)
        conn.close()

        total_extra = sum(d["cnt"] - 1 for d in dupes)
        plan = build_duplicate_cleanup_plan(dupes, max_groups=DUPLICATE_PLAN_GROUP_LIMIT)
        cleanup_gate = _duplicate_cleanup_gate(dupes)

        return {
            "check": "duplicate_vectors",
            "status": "warn" if dupes else "ok",
            "duplicate_groups": len(dupes),
            "total_extra_entries": total_extra,
            "top_dupes": [
                {
                    "hash": d["h"],
                    "count": d["cnt"],
                    "ids": d["ids"],
                    "min_content_len": d["min_len"],
                }
                for d in dupes[:10]
            ],
            "cleanup_plan": plan,
            "cleanup_gate": cleanup_gate,
        }
    except Exception as e:
        return {"check": "duplicate_vectors", "status": "error", "error": str(e)}


def check_insight_quality() -> Dict:
    """Flag degraded or low-quality insights."""
    try:
        conn = _db_connect("law_firm_data")
        cur = conn.cursor(dictionary=True)

        cur.execute(
            "SELECT id, insight_text, COALESCE(is_degraded, 0) AS is_degraded "
            "FROM legal_insights"
        )
        rows = cur.fetchall()

        cur.close()
        conn.close()

        total = len(rows)
        reasons = Counter()
        issue_ids = []
        for row in rows:
            bad, reason = _is_low_quality_insight(
                row.get("insight_text") or "",
                bool(row.get("is_degraded")),
            )
            if bad:
                reasons[reason] += 1
                issue_ids.append(row.get("id"))

        issues = sum(reasons.values())
        return {
            "check": "insight_quality",
            "status": "warn" if issues > 0 else "ok",
            "total_insights": total,
            "degraded": reasons.get("degraded_marker", 0) + reasons.get("degraded_short", 0),
            "too_short": reasons.get("substantive_body_too_short", 0),
            "empty": reasons.get("empty", 0),
            "boilerplate": 0,
            "issue_reasons": dict(reasons),
            "sample_issue_ids": issue_ids[:10],
            "healthy": total - issues,
            "health_pct": round((total - issues) / max(total, 1) * 100, 1),
        }
    except Exception as e:
        return {"check": "insight_quality", "status": "error", "error": str(e)}


def check_wiki_staleness() -> Dict:
    """Check if any wiki pages are outdated (source notes changed)."""
    vault = _get_vault_path()
    if not vault:
        return {"check": "wiki_staleness", "status": "skip", "reason": "no vault"}

    if not WIKI_STATE_PATH.exists():
        return {"check": "wiki_staleness", "status": "skip", "reason": "no wiki state"}

    try:
        wiki_state = json.loads(WIKI_STATE_PATH.read_text("utf-8"))
    except Exception:
        return {"check": "wiki_staleness", "status": "error", "error": "cannot read wiki state"}

    import re
    CASE_FOLDER_RE = re.compile(r"(\d{4}-\d{4})-(.+?)-(.*?)-(.*)")

    # Check each synthesized case
    stale_cases = []
    up_to_date = 0
    notes_dir = vault / "20_Notes"

    for case_number, case_state in wiki_state.get("cases", {}).items():
        prev_hashes = case_state.get("source_hashes", {})

        # Check current note hashes
        changed = False
        current_paths = set()
        for md_file in notes_dir.rglob("*.md"):
            # Check if this note belongs to this case
            for part in md_file.parts:
                m = CASE_FOLDER_RE.match(part)
                if m and m.group(1) == case_number:
                    rel = str(md_file.relative_to(vault))
                    current_paths.add(rel)
                    try:
                        content = md_file.read_text("utf-8", errors="replace")
                        h = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()[:12]
                        if prev_hashes.get(rel) != h:
                            changed = True
                    except Exception:
                        changed = True
                    break

        # Check for removed notes
        for prev_path in prev_hashes:
            if prev_path not in current_paths:
                changed = True

        if changed:
            stale_cases.append({
                "case": case_number,
                "client": case_state.get("client_name", ""),
                "synthesized_at": case_state.get("synthesized_at", ""),
            })
        else:
            up_to_date += 1

    return {
        "check": "wiki_staleness",
        "status": "warn" if stale_cases else "ok",
        "stale_cases": len(stale_cases),
        "up_to_date": up_to_date,
        "details": stale_cases[:10],
    }


def check_orphan_notes() -> Dict:
    """Find Obsidian notes without vector embeddings, and vice versa."""
    vault = _get_vault_path()
    if not vault:
        return {"check": "orphan_notes", "status": "skip", "reason": "no vault"}

    if not INDEX_PATH.exists():
        return {"check": "orphan_notes", "status": "skip", "reason": "no index"}

    try:
        idx = json.loads(INDEX_PATH.read_text("utf-8"))
    except Exception:
        return {"check": "orphan_notes", "status": "error", "error": "cannot read index"}

    notes_in_index = set(idx.get("notes", {}).keys())

    # Find actual .md files in the vault.  The index legitimately contains
    # dashboard/wiki/index notes too, so comparing it only against 20_Notes
    # creates false orphan alarms.
    actual_notes = set()
    for md in vault.rglob("*.md"):
        rel_parts = md.relative_to(vault).parts
        if any(part in {".obsidian", ".trash", ".git", "node_modules", "__pycache__"} for part in rel_parts):
            continue
        actual_notes.add(str(md.relative_to(vault)))

    # Notes on disk but not indexed
    unindexed = actual_notes - notes_in_index
    # Notes in index but file missing
    orphaned_index = notes_in_index - actual_notes

    # Notes indexed but with 0 chunks
    zero_chunks = []
    zero_chunks_verified = 0
    try:
        conn = _db_connect("magi_brain")
    except Exception:
        conn = None
    try:
        for path, info in idx.get("notes", {}).items():
            if info.get("chunks", 0) != 0 or path not in actual_notes:
                continue
            doc_key = str(info.get("doc_key") or "")
            if doc_key and conn is not None and _vector_rows_for_doc_key(conn, doc_key) > 0:
                zero_chunks_verified += 1
                continue
            zero_chunks.append(path)
    finally:
        if conn is not None:
            conn.close()

    return {
        "check": "orphan_notes",
        "status": "warn" if (unindexed or orphaned_index or zero_chunks) else "ok",
        "total_on_disk": len(actual_notes),
        "total_in_index": len(notes_in_index),
        "unindexed": len(unindexed),
        "orphaned_index_entries": len(orphaned_index),
        "zero_chunk_notes": len(zero_chunks),
        "zero_chunk_verified_in_db": zero_chunks_verified,
        "sample_unindexed": sorted(unindexed)[:5],
        "sample_orphaned": sorted(orphaned_index)[:5],
        "sample_zero_chunk": sorted(zero_chunks)[:5],
    }


def check_contradiction_scan(use_llm: bool = True) -> Dict:
    """
    Use LLM to detect contradictions within cases.

    Lightweight version: compare wiki overview ⚠️ sections.
    Full version: sample pairs of documents from same case, ask LLM.
    """
    if not use_llm:
        return {"check": "contradiction_scan", "status": "skip", "reason": "llm disabled"}

    vault = _get_vault_path()
    if not vault:
        return {"check": "contradiction_scan", "status": "skip", "reason": "no vault"}

    wiki_dir = vault / "30_Wiki"
    if not wiki_dir.is_dir():
        return {"check": "contradiction_scan", "status": "skip", "reason": "no wiki pages yet"}

    # Read existing wiki overviews and check for ⚠️ markers
    contradictions = []
    clean_cases = 0

    for case_dir in sorted(wiki_dir.iterdir()):
        if not case_dir.is_dir():
            continue
        overview = case_dir / "overview.md"
        if not overview.exists():
            continue

        try:
            content = overview.read_text("utf-8", errors="replace")
        except Exception:
            continue

        # Count ⚠️ markers
        warning_count = content.count("⚠️")
        # Check for contradiction section
        has_contradiction_section = "矛盾" in content or "待確認" in content

        if warning_count > 0 or has_contradiction_section:
            # Extract the contradiction section
            match = re.search(r"##\s*⚠️.*?\n(.+?)(?=\n##|\Z)", content, re.DOTALL)
            excerpt = match.group(1).strip()[:300] if match else ""

            contradictions.append({
                "case": case_dir.name,
                "warning_count": warning_count,
                "excerpt": excerpt,
            })
        else:
            clean_cases += 1

    return {
        "check": "contradiction_scan",
        "status": "warn" if contradictions else "ok",
        "cases_with_contradictions": len(contradictions),
        "clean_cases": clean_cases,
        "details": contradictions[:10],
    }


# ── Report Generation ───────────────────────────────────────────────

def _format_report_md(results: List[Dict]) -> str:
    """Format lint results as Obsidian-friendly markdown."""
    lines = [
        f"# 🔍 MAGI 知識品質報告",
        f"",
        f"**掃描時間**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"",
    ]

    status_icons = {"ok": "✅", "warn": "⚠️", "error": "❌", "skip": "⏭️"}

    # Summary table
    lines.append("## 摘要")
    lines.append("")
    lines.append("| 檢查項目 | 狀態 | 說明 |")
    lines.append("|---------|------|------|")

    check_labels = {
        "duplicate_vectors": "向量重複",
        "insight_quality": "見解品質",
        "wiki_staleness": "Wiki 時效",
        "orphan_notes": "孤立筆記",
        "contradiction_scan": "矛盾偵測",
    }

    for r in results:
        icon = status_icons.get(r.get("status", ""), "❓")
        label = check_labels.get(r.get("check", ""), r.get("check", ""))
        summary = _summarize_check(r)
        lines.append(f"| {label} | {icon} | {summary} |")

    lines.append("")

    # Details
    for r in results:
        check = r.get("check", "")
        label = check_labels.get(check, check)
        lines.append(f"## {label}")
        lines.append("")

        if check == "duplicate_vectors":
            lines.append(f"- 重複群組: {r.get('duplicate_groups', 0)}")
            lines.append(f"- 多餘條目: {r.get('total_extra_entries', 0)}")
            plan = r.get("cleanup_plan") or {}
            if plan:
                lines.append(f"- 清理計畫（dry-run）: {plan.get('groups_planned', 0)} 組，預估刪除 {plan.get('estimated_delete_documents', 0)} 筆")
            gate = r.get("cleanup_gate") or {}
            if gate:
                lines.append(f"- 清理閘門: {gate.get('status', 'unknown')}")
                if gate.get("reason"):
                    lines.append(f"  - 原因: {gate.get('reason')}")
        elif check == "insight_quality":
            lines.append(f"- 總見解數: {r.get('total_insights', 0)}")
            lines.append(f"- 健康比例: {r.get('health_pct', 0)}%")
            lines.append(f"- 降級: {r.get('degraded', 0)} | 過短: {r.get('too_short', 0)} | 空白: {r.get('empty', 0)} | 樣板: {r.get('boilerplate', 0)}")
        elif check == "wiki_staleness":
            lines.append(f"- 過時 wiki: {r.get('stale_cases', 0)}")
            lines.append(f"- 最新: {r.get('up_to_date', 0)}")
            for d in r.get("details", []):
                lines.append(f"  - {d['case']} ({d.get('client', '')}) — 合成於 {d.get('synthesized_at', '?')}")
        elif check == "orphan_notes":
            lines.append(f"- 磁碟筆記: {r.get('total_on_disk', 0)} | 索引筆記: {r.get('total_in_index', 0)}")
            lines.append(f"- 未索引: {r.get('unindexed', 0)} | 孤立索引: {r.get('orphaned_index_entries', 0)} | 零向量: {r.get('zero_chunk_notes', 0)}")
        elif check == "contradiction_scan":
            lines.append(f"- 有矛盾案件: {r.get('cases_with_contradictions', 0)}")
            lines.append(f"- 無矛盾案件: {r.get('clean_cases', 0)}")
            for d in r.get("details", []):
                lines.append(f"  - **{d['case']}** ({d.get('warning_count', 0)} ⚠️)")
                if d.get("excerpt"):
                    lines.append(f"    > {d['excerpt'][:150]}")

        if r.get("error"):
            lines.append(f"- ❌ 錯誤: {r['error']}")

        lines.append("")

    return "\n".join(lines)


def _summarize_check(r: Dict) -> str:
    check = r.get("check", "")
    status = r.get("status", "")
    if status == "skip":
        return r.get("reason", "跳過")
    if status == "error":
        return f"錯誤: {r.get('error', '')[:50]}"

    if check == "duplicate_vectors":
        n = r.get("duplicate_groups", 0)
        if not n:
            return "無重複"
        gate = (r.get("cleanup_gate") or {}).get("status", "")
        if gate:
            return f"{n} 組重複（gate: {gate}）"
        return f"{n} 組重複"
    elif check == "insight_quality":
        return f"{r.get('health_pct', 0)}% 健康 ({r.get('total_insights', 0)} 筆)"
    elif check == "wiki_staleness":
        n = r.get("stale_cases", 0)
        return f"{n} 個 wiki 需更新" if n else "全部最新"
    elif check == "orphan_notes":
        u = r.get("unindexed", 0)
        o = r.get("orphaned_index_entries", 0)
        return f"未索引 {u} / 孤立 {o}" if (u or o) else "同步正常"
    elif check == "contradiction_scan":
        n = r.get("cases_with_contradictions", 0)
        return f"{n} 個案件有矛盾標記" if n else "無矛盾"
    return str(status)


# ── Main ────────────────────────────────────────────────────────────

def lint(
    quick: bool = False,
    write_to_vault: bool = False,
    quiet: bool = False,
):
    """Run all lint checks and produce a report."""
    t0 = time.time()

    if not quiet:
        print("🔍 MAGI 知識品質掃描開始...\n")

    results = []

    # 1. Duplicate vectors (always fast, no LLM)
    if not quiet:
        print("  [1/5] 向量重複檢查...", end=" ", flush=True)
    r = check_duplicate_vectors()
    results.append(r)
    if not quiet:
        print(f"{'✅' if r['status'] == 'ok' else '⚠️'}")

    # 2. Insight quality (fast, no LLM)
    if not quiet:
        print("  [2/5] 見解品質檢查...", end=" ", flush=True)
    r = check_insight_quality()
    results.append(r)
    if not quiet:
        print(f"{'✅' if r['status'] == 'ok' else '⚠️'}")

    # 3. Wiki staleness (fast, no LLM)
    if not quiet:
        print("  [3/5] Wiki 時效檢查...", end=" ", flush=True)
    r = check_wiki_staleness()
    results.append(r)
    if not quiet:
        print(f"{'✅' if r['status'] == 'ok' else '⚠️'}")

    # 4. Orphan notes (fast, no LLM)
    if not quiet:
        print("  [4/5] 孤立筆記檢查...", end=" ", flush=True)
    r = check_orphan_notes()
    results.append(r)
    if not quiet:
        print(f"{'✅' if r['status'] == 'ok' else '⚠️'}")

    # 5. Contradiction scan (uses LLM in full mode)
    if not quiet:
        print("  [5/5] 矛盾偵測...", end=" ", flush=True)
    r = check_contradiction_scan(use_llm=not quick)
    results.append(r)
    if not quiet:
        print(f"{'✅' if r['status'] == 'ok' else '⚠️'}")

    elapsed = time.time() - t0

    # Build report
    report = {
        "scan_time": datetime.now().isoformat(),
        "elapsed_sec": round(elapsed, 1),
        "mode": "quick" if quick else "full",
        "checks": results,
        "summary": {
            "total_checks": len(results),
            "ok": sum(1 for r in results if r.get("status") == "ok"),
            "warn": sum(1 for r in results if r.get("status") == "warn"),
            "error": sum(1 for r in results if r.get("status") == "error"),
            "skip": sum(1 for r in results if r.get("status") == "skip"),
        },
    }

    # Save JSON report
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # Write to Obsidian vault
    if write_to_vault:
        vault = _get_vault_path()
        if vault:
            md_content = _format_report_md(results)
            try:
                sys.path.insert(0, os.path.join(MAGI_ROOT, "skills", "obsidian"))
                from action import task_writeback
                wr = task_writeback(
                    f"知識品質報告_{datetime.now().strftime('%Y%m%d')}",
                    md_content,
                    folder="MAGI/品質報告",
                    vault_path=vault,
                )
                if not quiet:
                    print(f"\n📝 報告已寫入 Obsidian: {wr.get('path', '')}")
            except Exception as e:
                logger.warning("Failed to write to vault: %s", e)

    if not quiet:
        print(f"\n{'='*50}")
        s = report["summary"]
        print(f"掃描完成！耗時 {elapsed:.1f}s")
        print(f"  ✅ {s['ok']}  ⚠️ {s['warn']}  ❌ {s['error']}  ⏭️ {s['skip']}")
        print(f"  報告: {REPORT_PATH}")

    return report


def main():
    parser = argparse.ArgumentParser(description="MAGI 知識品質掃描 (Knowledge Lint)")
    parser.add_argument("--quick", action="store_true", help="快速模式（不使用 LLM）")
    parser.add_argument("--write-to-vault", action="store_true", help="將報告寫入 Obsidian vault")
    parser.add_argument("--quiet", action="store_true", help="安靜模式")
    parser.add_argument("--dry-run", action="store_true", help="同 --quick")
    parser.add_argument("--cleanup-duplicates", action="store_true", help="執行 duplicate cleanup（預設 dry-run）")
    parser.add_argument("--apply", action="store_true", help="搭配 --cleanup-duplicates 實際刪除 duplicate extra entries")
    parser.add_argument("--backup-dir", default=str(DEFAULT_DUPLICATE_BACKUP_DIR), help="duplicate cleanup 備份路徑")
    parser.add_argument("--max-groups", type=int, default=DUPLICATE_PLAN_GROUP_LIMIT, help="duplicate cleanup 最大處理群組數")
    parser.add_argument("--no-faiss-rebuild", action="store_true", help="duplicate cleanup 後跳過 FAISS rebuild")
    parser.add_argument("--no-auto-rollback", action="store_true", help="verify 失敗時不要自動 rollback")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.cleanup_duplicates:
        if args.apply and args.dry_run:
            raise SystemExit("--apply 與 --dry-run 不能同時使用")
        summary = cleanup_duplicate_vectors(
            apply=bool(args.apply),
            max_groups=int(max(1, args.max_groups)),
            backup_dir=Path(args.backup_dir),
            rebuild_faiss=not args.no_faiss_rebuild,
            auto_rollback=not args.no_auto_rollback,
            quiet=args.quiet,
        )
        if not args.quiet:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            print(f"cleanup report: {CLEANUP_REPORT_PATH}")
        if args.apply and not summary.get("verified"):
            raise SystemExit(2)
        return

    lint(
        quick=args.quick or args.dry_run,
        write_to_vault=args.write_to_vault,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    main()
