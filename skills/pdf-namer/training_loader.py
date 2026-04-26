# -*- coding: utf-8 -*-
"""
pdf-namer / training_loader.py
===============================
從 MariaDB `doc_rules` 載入已訓練的文件分類規則，
整合 Synology Drive 範本與 DB 資料，產生增強版 AI prompt。
"""

import json
import os
import sys
import logging
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("pdf-namer-loader")

_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _MAGI_ROOT not in sys.path:
    sys.path.insert(0, _MAGI_ROOT)

from api.runtime_paths import get_orch_dir

# ── DB Config ──
DB_HOST = os.environ.get("OSC_DB_HOST", os.environ.get("MAGI_REMOTE_DB_HOST", "127.0.0.1"))
DB_USER = os.environ.get("OSC_DB_USER", os.environ.get("DB_USER", "casper_service"))
DB_PASS = os.environ.get("OSC_DB_PASSWORD", os.environ.get("DB_PASSWORD", ""))
DB_NAME = "law_firm_data"
_DB_FAILURE_UNTIL = 0.0
_RULES_STATUS: Dict[str, object] = {
    "source": "unavailable",
    "degraded": True,
    "reason": "init",
    "rules_count": 0,
    "updated_at": None,
}

# Target archive types (matching user's 4 folder spec)
TARGET_ARCHIVE_TYPES = [
    "法院通知或程序裁定",
    "對方歷次書狀",
    "判決書",
    "證據資料",
    "我方歷次書狀",
    "閱卷資料",
    "回執",
    "信件往返",
]

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _update_rules_status(source: str, degraded: bool, reason: str = "", rules_count: int = 0):
    global _RULES_STATUS
    _RULES_STATUS = {
        "source": source,
        "degraded": bool(degraded),
        "reason": reason or "",
        "rules_count": int(max(0, rules_count)),
        "updated_at": __import__("datetime").datetime.now().isoformat(),
    }


def get_doc_rules_status() -> Dict[str, object]:
    """Expose latest doc_rules loading status for benchmark/health reporting."""
    return dict(_RULES_STATUS)


def _get_db_connection():
    """Get MariaDB connection with failover: remote → local socket → local TCP."""
    global _DB_FAILURE_UNTIL
    if os.environ.get("MAGI_PDF_NAMER_DISABLE_DB_RULES", "0").strip().lower() in {"1", "true", "yes", "on"}:
        _update_rules_status("cache", degraded=True, reason="db_rules_disabled")
        logger.warning("pdf-namer doc_rules DB access disabled; using cache fallback")
        return None
    allow_empty_password = _truthy(os.environ.get("MAGI_PDF_NAMER_ALLOW_EMPTY_DB_PASSWORD", "0"))
    if not str(DB_PASS or "").strip() and not allow_empty_password:
        _update_rules_status("cache", degraded=True, reason="missing_db_credentials")
        logger.warning("pdf-namer doc_rules DB credentials missing; using cache fallback")
        return None
    now = time.time()
    if _DB_FAILURE_UNTIL and now < _DB_FAILURE_UNTIL:
        _update_rules_status("cache", degraded=True, reason="db_circuit_open")
        return None
    import mysql.connector

    # Try TCP connection (remote or local via env)
    hosts = [DB_HOST]
    if DB_HOST != "127.0.0.1":
        hosts.append("127.0.0.1")
    for host in hosts:
        try:
            conn = mysql.connector.connect(
                host=host,
                user=DB_USER,
                password=DB_PASS,
                database=DB_NAME,
                connect_timeout=5,
            )
            if host != DB_HOST:
                logger.info("pdf-namer DB failover: using local DB (127.0.0.1)")
            _update_rules_status("db", degraded=False, reason="connected")
            return conn
        except Exception:
            continue

    # Final fallback: local unix socket (for dev/localhost)
    try:
        conn = mysql.connector.connect(
            user=DB_USER or "ai",
            password=DB_PASS,
            database=DB_NAME,
            unix_socket="/tmp/mysql.sock",
            connect_timeout=5,
        )
        _update_rules_status("db", degraded=False, reason="connected_via_socket")
        return conn
    except Exception as e:
        _DB_FAILURE_UNTIL = time.time() + 300
        _update_rules_status("cache", degraded=True, reason=f"db_connect_error:{type(e).__name__}")
        logger.warning("pdf-namer doc_rules DB unavailable; cache fallback (%s)", e)
        return None


def load_doc_rules_from_db(
    target_types: List[str] = None,
    include_all: bool = False,
) -> List[Dict]:
    """
    Load doc_rules from MariaDB.
    
    Args:
        target_types: List of archive_destination_type to filter.
                      Default: TARGET_ARCHIVE_TYPES
        include_all: If True, load all rules regardless of type.
    
    Returns:
        List of dicts with: doc_type, filename_template, archive_destination_type, description
    """
    conn = _get_db_connection()
    if not conn:
        cached = _load_cached_rules()
        if cached:
            _update_rules_status("cache", degraded=True, reason=_RULES_STATUS.get("reason", "db_unavailable"), rules_count=len(cached))
        else:
            _update_rules_status("unavailable", degraded=True, reason=_RULES_STATUS.get("reason", "no_cache"), rules_count=0)
        return cached
    
    try:
        cursor = conn.cursor(dictionary=True)
        
        if include_all:
            cursor.execute("""
                SELECT doc_type, filename_template, archive_destination_type, description
                FROM doc_rules
                WHERE is_enabled = 1
                ORDER BY archive_destination_type, doc_type
            """)
        else:
            types = target_types or TARGET_ARCHIVE_TYPES
            placeholders = ", ".join(["%s"] * len(types))
            cursor.execute(f"""
                SELECT doc_type, filename_template, archive_destination_type, description
                FROM doc_rules
                WHERE is_enabled = 1
                AND archive_destination_type IN ({placeholders})
                ORDER BY archive_destination_type, doc_type
            """, types)
        
        rules = cursor.fetchall()
        cursor.close()
        conn.close()
        
        # Cache locally
        _cache_rules(rules)
        
        _update_rules_status("db", degraded=False, reason="ok", rules_count=len(rules))
        logger.info(f"✅ 從 MariaDB 載入 {len(rules)} 筆 doc_rules")
        return rules
        
    except Exception as e:
        logger.error(f"❌ 查詢失敗: {e}")
        if conn:
            conn.close()
        cached = _load_cached_rules()
        if cached:
            _update_rules_status("cache", degraded=True, reason=f"db_query_error:{type(e).__name__}", rules_count=len(cached))
        else:
            _update_rules_status("unavailable", degraded=True, reason=f"db_query_error:{type(e).__name__}", rules_count=0)
        return cached


def _cache_rules(rules: List[Dict]):
    """Cache rules locally for offline use."""
    cache_path = os.path.join(SKILL_DIR, "db_rules_cache.json")
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(rules, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"⚠️ 快取寫入失敗: {e}")


def _load_cached_rules() -> List[Dict]:
    """Load rules from local cache when DB is offline."""
    cache_path = os.path.join(SKILL_DIR, "db_rules_cache.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                rules = json.load(f)
            logger.info(f"📋 從本地快取載入 {len(rules)} 筆 doc_rules")
            return rules
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 138, exc_info=True)
    return []


def build_db_enhanced_prompt(rules: List[Dict] = None) -> str:
    """
    Build an enhanced AI prompt section from DB doc_rules.
    Groups rules by archive_destination_type, showing filename templates.
    """
    if rules is None:
        rules = load_doc_rules_from_db()
    
    if not rules:
        return ""
    
    # Group by archive_destination_type
    from collections import defaultdict
    groups = defaultdict(list)
    for r in rules:
        dest = r.get("archive_destination_type", "其他")
        groups[dest].append(r)
    
    lines = ["\n## MariaDB 已訓練規則（doc_rules）\n"]
    lines.append("以下規則來自過去 Pigeonhole 系統學習的 1,290 筆文件分類記錄：\n")
    
    for dest, dest_rules in sorted(groups.items()):
        lines.append(f"\n### 歸檔至：{dest}（{len(dest_rules)} 筆）")
        # Show up to 5 examples per type
        for r in dest_rules[:5]:
            template = r.get("filename_template", "")
            doc_type = r.get("doc_type", "")
            lines.append(f"- 文件類型: `{doc_type}`")
            lines.append(f"  命名模板: `{template}`")
        if len(dest_rules) > 5:
            lines.append(f"  ... 另有 {len(dest_rules) - 5} 筆")
    
    return "\n".join(lines)


def get_template_for_doc_type(doc_type_text: str, rules: List[Dict] = None) -> Optional[Dict]:
    """
    Find the best matching doc_rule for a given document type text.
    Uses multi-tier matching: exact → substring containment → token overlap.
    """
    if rules is None:
        rules = load_doc_rules_from_db()

    if not rules:
        return None

    query = str(doc_type_text or "").strip()
    if not query:
        return None

    # Tier 1: Exact match
    for r in rules:
        if r["doc_type"] == query:
            return r

    # Tier 2: Substring containment (prefer shorter rule_type = more specific)
    # e.g. query="刑事判決" matches rule "刑事判決", "判決" matches rule "判決"
    contains_matches = []
    for r in rules:
        rt = r["doc_type"]
        if query in rt or rt in query:
            contains_matches.append(r)
    if contains_matches:
        # Prefer the rule whose doc_type is closest in length to query
        contains_matches.sort(key=lambda r: abs(len(r["doc_type"]) - len(query)))
        return contains_matches[0]

    # Tier 3: Token overlap with minimum threshold
    # For Chinese text, use character bigrams for better matching
    def _bigrams(s):
        return set(s[i:i+2] for i in range(len(s) - 1)) if len(s) >= 2 else {s}

    q_bigrams = _bigrams(query)
    best_match = None
    best_score = 0.0

    for r in rules:
        rt = r["doc_type"]
        r_bigrams = _bigrams(rt)
        if not q_bigrams or not r_bigrams:
            continue
        overlap = len(q_bigrams & r_bigrams)
        score = overlap / max(len(q_bigrams), len(r_bigrams), 1)
        if score > best_score and score > 0.4:
            best_score = score
            best_match = r

    return best_match


def sync_db_to_training() -> Dict:
    """
    Sync doc_rules from DB to local training data.
    Merges DB rules with existing Synology Drive training data.
    """
    # Load DB rules (filtered to target types)
    db_rules = load_doc_rules_from_db()
    
    # Load existing training data
    training_path = os.path.join(SKILL_DIR, "training_data.json")
    existing = []
    if os.path.exists(training_path):
        with open(training_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
    
    # Add DB rules as training entries
    db_entries = []
    for r in db_rules:
        db_entries.append({
            "filename": r["doc_type"],
            "category": _map_archive_to_category(r.get("archive_destination_type", "")),
            "confidence": 0.9,  # DB rules are high confidence 
            "folder_type": r.get("archive_destination_type", "其他"),
            "date": None,
            "party": None,
            "case_number": None,
            "court": None,
            "filename_template": r.get("filename_template", ""),
            "text_method": "db_rule",
            "source": "mariadb_doc_rules",
        })
    
    merged = existing + db_entries
    
    # Save merged
    with open(training_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    
    return {
        "existing_entries": len(existing),
        "db_entries": len(db_entries),
        "merged_total": len(merged),
        "db_rules_loaded": len(db_rules),
    }


def _map_archive_to_category(archive_type: str) -> str:
    """Map archive_destination_type to pdf-namer category."""
    mapping = {
        "法院通知或程序裁定": "法院通知",
        "對方歷次書狀": "書狀_對造",
        "判決書": "判決",
        "證據資料": "證據",
        "我方歷次書狀": "書狀_我方",
        "閱卷資料": "閱卷",
        "回執": "收據",
        "信件往返": "信件",
        "委任契約書": "契約",
        "法扶資料": "法扶表單",
        "開辦資料": "法扶表單",
        "結案資料": "法扶回報",
        "筆錄": "筆錄",
    }
    # Handle numbered prefixes like "01_法扶資料"
    clean = archive_type
    import re
    m = re.match(r'^\d+_(.+)$', archive_type)
    if m:
        clean = m.group(1)
    
    return mapping.get(clean, mapping.get(archive_type, "其他"))


def save_learning_to_db(
    original_filename: str,
    correct_doc_type: str,
    correct_filename: str,
    confidence: float = 1.0,
    sample_text: str = "",
) -> bool:
    """
    Save a human-corrected filename to MariaDB learning_history.
    This is the core of the continuous learning loop.
    
    Returns:
        True if saved successfully.
    """
    conn = _get_db_connection()
    if not conn:
        # Save locally for later sync
        _save_learning_local(original_filename, correct_doc_type, correct_filename, confidence, sample_text)
        return False
    
    try:
        cursor = conn.cursor()
        
        # Find matching rule_id if exists
        cursor.execute(
            "SELECT rule_id FROM doc_rules WHERE doc_type = %s LIMIT 1",
            (correct_doc_type,)
        )
        row = cursor.fetchone()
        rule_id = row[0] if row else None
        
        # Insert into learning_history
        cursor.execute("""
            INSERT INTO learning_history
            (original_filename, learned_doc_type, confidence_score, sample_text, rule_id)
            VALUES (%s, %s, %s, %s, %s)
        """, (original_filename, correct_doc_type, confidence, sample_text[:500], rule_id))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        logger.info(f"✅ 學習紀錄已存入 DB: {original_filename} → {correct_doc_type}")
        
        # Also save to local training data
        _append_to_training_data({
            "filename": correct_filename,
            "category": correct_doc_type,
            "confidence": confidence,
            "folder_type": "human_correction",
            "date": None,
            "party": None,
            "case_number": None,
            "court": None,
            "original_filename": original_filename,
            "text_method": "human_learn",
            "source": "learn_command",
        })
        
        return True
        
    except Exception as e:
        logger.error(f"❌ 儲存學習紀錄失敗: {e}")
        if conn:
            conn.close()
        _save_learning_local(original_filename, correct_doc_type, correct_filename, confidence, sample_text)
        return False


def _save_learning_local(
    original_filename: str, doc_type: str, correct_filename: str,
    confidence: float, sample_text: str
):
    """Save learning locally when DB is offline."""
    local_path = os.path.join(SKILL_DIR, "_pending_learns.json")
    pending = []
    if os.path.exists(local_path):
        try:
            with open(local_path, "r", encoding="utf-8") as f:
                pending = json.load(f)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 386, exc_info=True)
    
    pending.append({
        "original_filename": original_filename,
        "doc_type": doc_type,
        "correct_filename": correct_filename,
        "confidence": confidence,
        "sample_text": sample_text[:500],
        "timestamp": __import__("datetime").datetime.now().isoformat(),
    })
    
    with open(local_path, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=2)
    logger.info(f"📋 學習紀錄暫存本地: {local_path} ({len(pending)} 筆)")


def _append_to_training_data(entry: Dict):
    """Append a single entry to training data."""
    training_path = os.path.join(SKILL_DIR, "training_data.json")
    data = []
    if os.path.exists(training_path):
        try:
            with open(training_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 411, exc_info=True)
    data.append(entry)
    with open(training_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_learning_history(limit: int = 50) -> List[Dict]:
    """Load recent entries from learning_history table."""
    conn = _get_db_connection()
    if not conn:
        return []
    
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT original_filename, learned_doc_type, confidence_score, learned_at
            FROM learning_history
            ORDER BY learned_at DESC
            LIMIT %s
        """, (limit,))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        
        # Convert datetime to string
        for r in rows:
            if r.get("learned_at"):
                r["learned_at"] = str(r["learned_at"])
        
        return rows
    except Exception as e:
        logger.error(f"❌ 讀取學習紀錄失敗: {e}")
        if conn:
            conn.close()
        return []


def sync_pending_learns() -> int:
    """Sync locally-saved learning records to DB when it comes back online."""
    local_path = os.path.join(SKILL_DIR, "_pending_learns.json")
    if not os.path.exists(local_path):
        return 0
    
    try:
        with open(local_path, "r", encoding="utf-8") as f:
            pending = json.load(f)
    except Exception:
        return 0
    
    if not pending:
        return 0
    
    synced = 0
    conn = _get_db_connection()
    if not conn:
        return 0
    
    try:
        cursor = conn.cursor()
        for p in pending:
            try:
                cursor.execute("""
                    INSERT INTO learning_history
                    (original_filename, learned_doc_type, confidence_score, sample_text)
                    VALUES (%s, %s, %s, %s)
                """, (p["original_filename"], p["doc_type"], p.get("confidence", 1.0), p.get("sample_text", "")))
                synced += 1
            except Exception as e:
                logger.warning(f"⚠️ 同步失敗: {e}")
        
        conn.commit()
        cursor.close()
        conn.close()
        
        if synced == len(pending):
            try:
                # Respect global no-delete policy (quarantine/keep when enabled)
                orch_dir = str(get_orch_dir())
                if orch_dir not in sys.path:
                    sys.path.insert(0, orch_dir)
                import safe_fs
                no_delete = os.environ.get("MAGI_NO_DELETE", "1").strip().lower() in {"1", "true", "yes", "on"}
                safe_fs.safe_remove(local_path, reason="pdf_namer_pending_learns", allow_delete=(not no_delete))
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 495, exc_info=True)
        
        logger.info(f"✅ 已同步 {synced}/{len(pending)} 筆待處理學習紀錄")
        return synced
    except Exception:
        if conn:
            conn.close()
        return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "sync":
            result = sync_db_to_training()
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif cmd == "prompt":
            print(build_db_enhanced_prompt())
        elif cmd == "rules":
            rules = load_doc_rules_from_db(include_all=True)
            print(f"Loaded {len(rules)} rules")
            for r in rules[:10]:
                print(f"  {r['doc_type'][:40]} → {r.get('archive_destination_type', '?')}")
        elif cmd == "history":
            history = load_learning_history()
            print(f"Recent {len(history)} learning entries:")
            for h in history:
                print(f"  {h['learned_at']} | {h['original_filename'][:40]} → {h['learned_doc_type']}")
        elif cmd == "sync-pending":
            count = sync_pending_learns()
            print(f"Synced {count} pending records")
        else:
            print(f"Unknown command: {cmd}")
    else:
        print("Usage: python training_loader.py [sync|prompt|rules|history|sync-pending]")
