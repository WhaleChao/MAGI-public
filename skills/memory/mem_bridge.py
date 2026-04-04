import hashlib
import json
import logging
import os
import random
import re
import sys
import threading
import time
from datetime import datetime
from functools import lru_cache  # kept for any external consumers that may import it

from api.session.provenance import (
    build_source_signature,
    parse_source_provenance,
    render_provenance_badge,
)

try:
    from api.mysql_connector_guard import patch_mysql_connector_for_stability
except Exception:
    patch_mysql_connector_for_stability = None  # type: ignore[assignment]

import mysql.connector
import numpy as np
import requests

try:
    from skills.bridge.http_pool import get_session as _get_session
except ImportError:
    _get_session = requests.Session

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 29, exc_info=True)


logger = logging.getLogger("MemBridge")

if patch_mysql_connector_for_stability:
    os.environ.setdefault("MAGI_MYSQL_USE_PURE", "1")
    try:
        patch_mysql_connector_for_stability()
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 39, exc_info=True)

# Database config (prefer env, keep legacy fallbacks for compatibility)
DB_CONFIG = {
    "user": os.environ.get("DB_USER", "casper_service"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "host": os.environ.get("DB_HOST", "100.121.61.74"),
    "database": os.environ.get("DB_NAME", "magi_brain"),
}

_OMLX_EMBED_BASE = os.environ.get("MAGI_OMLX_EMBED_URL", "http://127.0.0.1:8081").rstrip("/")
# Legacy names (OLLAMA_*) kept for backward compat; MAGI_OMLX_EMBED_URL is canonical
OLLAMA_URL = os.environ.get("OLLAMA_EMBED_URL", f"{_OMLX_EMBED_BASE}/v1/embeddings")
OLLAMA_BATCH_URL = os.environ.get("OLLAMA_EMBED_BATCH_URL", f"{_OMLX_EMBED_BASE}/v1/embeddings")
MODEL = os.environ.get("MEM_EMBED_MODEL", "modernbert-embed-4bit")

OLLAMA_GENERATE_URL = os.environ.get("OLLAMA_GENERATE_URL", "http://127.0.0.1:8080/v1/chat/completions")
GENERATE_MODEL = os.environ.get("MEM_QUERY_EXPAND_MODEL", "TAIDE-12b-Chat-mlx-4bit")

MAX_VECTOR_SCAN = int(os.environ.get("MEMORY_MAX_VECTOR_SCAN", "5000"))
ENABLE_QUERY_EXPANSION = os.environ.get("MEMORY_ENABLE_QUERY_EXPANSION", "0") != "0"  # V3: disabled by default (saves ~5s)

_MEMORY_RECALL_CHATLOG_MARKERS = (
    "回顧",
    "回憶",
    "之前說",
    "你記得",
    "對話記錄",
    "聊天紀錄",
    "聊天記錄",
    "what did i say",
    "remember what i said",
    "conversation log",
)

_MEMORY_LOW_TRUST_MARKERS = (
    "chatlog|",
    "assistant_generated",
    "summary_derived",
    "generated_summary",
    "llm_summary",
)

_MEMORY_HIGH_TRUST_MARKERS = (
    "user_rule",
    "user_profile",
    "user_confirmed",
    "manual",
    "statute",
    "official",
    "verified",
    "judicial_api",
    "case_statutes",
    "legal_crawler_judgment",
    "legal_crawler_news",
)


def _normalize_source_text(source: str) -> str:
    return str(source or "").strip().lower()


def _query_prefers_chatlog(query: str) -> bool:
    q = _normalize_source_text(query)
    return any(marker in q for marker in _MEMORY_RECALL_CHATLOG_MARKERS)


def _query_terms(text: str) -> list[str]:
    raw = _normalize_source_text(text)
    if not raw:
        return []
    terms = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", raw)
    return [t for t in terms if t]


def _source_trust_weight(source: str, query: str = "") -> float:
    prov = parse_source_provenance(source)
    src = _normalize_source_text(prov.raw_source)
    if not src:
        return 0.60

    wants_chatlog = _query_prefers_chatlog(query)

    if prov.source_type in {"chatlog", "user_chat"}:
        if prov.role == "assistant":
            return 0.45 if wants_chatlog else 0.18
        return 0.85 if wants_chatlog else 0.28

    if prov.verified:
        return max(0.90, prov.confidence or 0.90)

    if prov.derived_from:
        return min(0.35, prov.confidence or 0.35)

    if any(marker in src for marker in _MEMORY_LOW_TRUST_MARKERS):
        if "chatlog|" in src:
            return 0.85 if wants_chatlog else 0.28
        return 0.45 if wants_chatlog else 0.18

    if any(marker in src for marker in _MEMORY_HIGH_TRUST_MARKERS):
        return 1.00

    if "user_chat_" in src:
        return 0.90 if wants_chatlog else 0.78

    if "crawler" in src or "research" in src or "web" in src or "news" in src or "briefing" in src:
        return 0.72

    if "codebase-ingest" in src:
        return 0.15

    if prov.confidence > 0.0:
        return prov.confidence

    return 0.65


def _rank_recall_results(query: str, results: list[dict]) -> list[dict]:
    ranked: list[dict] = []
    for item in results or []:
        if not isinstance(item, dict):
            continue
        src = str(item.get("source") or "")
        base_score = _safe_float(item.get("score"))
        trust_weight = _source_trust_weight(src, query=query)
        adjusted_score = base_score * trust_weight
        enriched = dict(item)
        enriched["base_score"] = base_score
        enriched["trust_weight"] = trust_weight
        enriched["score"] = adjusted_score
        enriched["provenance"] = parse_source_provenance(src).as_dict()
        ranked.append(enriched)

    ranked.sort(
        key=lambda x: (
            _safe_float(x.get("score")),
            _safe_float(x.get("trust_weight")),
            _safe_float(x.get("base_score")),
        ),
        reverse=True,
    )
    return ranked

# FAISS index (lazy init)
_FAISS_INDEX = None
_FAISS_INIT_LOCK = __import__("threading").Lock()
ENABLE_FAISS = os.environ.get("MEMORY_ENABLE_FAISS", "1") != "0"

# Circuit breaker: when Keeper (MariaDB) is offline, avoid reconnecting on every chunk.
# Note: float assignment is atomic under CPython GIL — no lock needed for this pattern.
KEEPER_CIRCUIT_SEC = int(os.environ.get("MEMORY_KEEPER_CIRCUIT_SEC", "300"))
_KEEPER_OFFLINE_UNTIL = 0.0


def _now_ts() -> float:
    try:
        return datetime.now().timestamp()
    except Exception:
        return 0.0


def _keeper_offline() -> bool:
    global _KEEPER_OFFLINE_UNTIL
    now = _now_ts()
    return bool(now and _KEEPER_OFFLINE_UNTIL > now)


def _mark_keeper_offline(reason: str = "") -> None:
    global _KEEPER_OFFLINE_UNTIL
    now = _now_ts()
    if not now:
        return
    _KEEPER_OFFLINE_UNTIL = now + max(30, int(KEEPER_CIRCUIT_SEC))
    if reason:
        logger.warning(f"Keeper offline (circuit {KEEPER_CIRCUIT_SEC}s): {reason}")


# Manual cache — lru_cache would permanently store zero vectors (Ollama busy fallback).
# We only cache genuinely non-zero embeddings so recall quality degrades gracefully.
_embed_mem: dict = {}
_EMBED_MEM_MAX = 4096


def _embedding_cache(text: str) -> tuple:
    cached = _embed_mem.get(text)
    if cached is not None:
        return cached
    emb = tuple(get_embedding(text))
    if any(v != 0.0 for v in emb):
        if len(_embed_mem) >= _EMBED_MEM_MAX:
            _embed_mem.pop(next(iter(_embed_mem)))
        _embed_mem[text] = emb
    return emb


def _zero_embedding(dim=768):
    return [0.0] * dim


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def _cosine_similarity(a, b):
    try:
        va = np.array(a, dtype=float)
        vb = np.array(b, dtype=float)
        denom = np.linalg.norm(va) * np.linalg.norm(vb)
        if denom <= 1e-12:
            return 0.0
        val = float(np.dot(va, vb) / denom)
        if np.isnan(val) or np.isinf(val):
            return 0.0
        return val
    except Exception:
        return 0.0


def _get_conn():
    # Avoid long hangs when Keeper is offline/unreachable.
    # mysql-connector uses `connection_timeout` (seconds).
    cfg = dict(DB_CONFIG)
    try:
        cfg["connection_timeout"] = int(os.environ.get("MEMORY_KEEPER_CONNECT_TIMEOUT_SEC", "3") or "3")
    except Exception:
        cfg["connection_timeout"] = 3
    cfg.setdefault("use_pure", True)
    return mysql.connector.connect(**cfg)


_EMBED_CIRCUIT: dict = {"failures": 0, "tripped_at": 0.0}
_EMBED_CB_THRESHOLD = 5
_EMBED_CB_COOLDOWN = 60  # seconds


def _embed_cb_open() -> bool:
    if _EMBED_CIRCUIT["failures"] < _EMBED_CB_THRESHOLD:
        return False
    elapsed = time.monotonic() - _EMBED_CIRCUIT["tripped_at"]
    return elapsed < _EMBED_CB_COOLDOWN


def _embed_cb_fail() -> None:
    _EMBED_CIRCUIT["failures"] += 1
    if _EMBED_CIRCUIT["failures"] >= _EMBED_CB_THRESHOLD:
        _EMBED_CIRCUIT["tripped_at"] = time.monotonic()


def _embed_cb_ok() -> None:
    _EMBED_CIRCUIT["failures"] = 0
    _EMBED_CIRCUIT["tripped_at"] = 0.0


_EMBED_MAX_RETRIES = int(os.environ.get("EMBED_MAX_RETRIES", "2"))
_EMBED_RETRY_BASE_SEC = float(os.environ.get("EMBED_RETRY_BASE_SEC", "0.4"))


def get_embedding(text):
    if _embed_cb_open():
        return _zero_embedding()

    # Try oMLX embeddings first (ModernBERT, same 768 dimensions as nomic-embed-text)
    try:
        from skills.bridge.melchior_client import embed_omlx, _omlx_embed_available
        if _omlx_embed_available():
            emb = embed_omlx(text)
            if isinstance(emb, list) and len(emb) > 0:
                _embed_cb_ok()
                return emb
    except Exception as e:
        logger.debug("oMLX embed fallthrough: %s", e)

    # Fallback to Ollama embeddings
    _timeout_sec = int(os.environ.get("MAGI_EMBED_TIMEOUT_SEC", "60"))
    for attempt in range(_EMBED_MAX_RETRIES + 1):
        try:
            response = _get_session().post(
                OLLAMA_URL,
                json={"model": MODEL, "input": text},
                timeout=_timeout_sec,
            )
            if response.status_code == 200:
                data = response.json()
                # Support both Ollama format {"embedding": [...]} and OpenAI format {"data": [{"embedding": [...]}]}
                emb = data.get("embedding")
                if not isinstance(emb, list) or not emb:
                    emb_data = data.get("data") or []
                    if emb_data and isinstance(emb_data, list):
                        emb = emb_data[0].get("embedding")
                if isinstance(emb, list) and emb:
                    _embed_cb_ok()
                    return emb
                logger.warning("Embedding API returned empty embedding")
                return _zero_embedding()
            if response.status_code == 503 and attempt < _EMBED_MAX_RETRIES:
                jitter = _EMBED_RETRY_BASE_SEC * (attempt + 1) + random.uniform(0.0, 0.3)
                logger.debug("Embedding 503 busy, retry %d in %.2fs", attempt + 1, jitter)
                time.sleep(jitter)
                continue
            logger.warning(f"Embedding API error: {response.status_code}")
            _embed_cb_fail()
            return _zero_embedding()
        except requests.exceptions.ReadTimeout:
            logger.warning("Embedding read timeout (attempt %d/%d, timeout=%ds)",
                           attempt + 1, _EMBED_MAX_RETRIES + 1, _timeout_sec)
            if attempt < _EMBED_MAX_RETRIES:
                wait = _EMBED_RETRY_BASE_SEC * (2 ** attempt) + random.uniform(0.5, 2.0)
                logger.info("Retrying embedding in %.1fs...", wait)
                time.sleep(wait)
                continue
            _embed_cb_fail()
            return _zero_embedding()
        except Exception as e:
            logger.warning(f"Embedding connection error: {e}")
            if attempt < _EMBED_MAX_RETRIES:
                time.sleep(_EMBED_RETRY_BASE_SEC * (attempt + 1))
                continue
            _embed_cb_fail()
            return _zero_embedding()
    _embed_cb_fail()
    return _zero_embedding()


_BATCH_503_COOLDOWN_SEC = float(os.environ.get("EMBED_BATCH_503_COOLDOWN_SEC", "2.0"))
_BATCH_INTER_CHUNK_SEC  = float(os.environ.get("EMBED_BATCH_INTER_CHUNK_SEC", "0.0"))


def get_embeddings_batch(texts, batch_size=32):
    """
    Generate embeddings for multiple texts using oMLX (primary) or Ollama (fallback).
    Returns: list of embedding vectors (same order as input texts).
    """
    if not texts:
        return []

    # Try oMLX batch embedding first
    try:
        from skills.bridge.melchior_client import embed_omlx_batch, _omlx_embed_available
        if _omlx_embed_available():
            results = embed_omlx_batch(texts)
            if results and all(isinstance(r, list) and len(r) > 0 for r in results):
                return results
    except Exception as e:
        logger.debug("oMLX batch embed fallthrough: %s", e)

    # Fallback to Ollama batch embedding
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        chunk = texts[i : i + batch_size]
        try:
            response = _get_session().post(
                OLLAMA_BATCH_URL,
                json={"model": MODEL, "input": chunk},
                timeout=max(30, len(chunk) * 2),
            )
            if response.status_code == 200:
                data = response.json()
                # Support both Ollama format ({"embeddings": [...]})
                # and OpenAI format ({"data": [{"embedding": [...]}]})
                embeddings = data.get("embeddings") or []
                if not embeddings and "data" in data:
                    embeddings = [item.get("embedding", []) for item in data["data"]]
                if len(embeddings) == len(chunk):
                    all_embeddings.extend(embeddings)
                    if _BATCH_INTER_CHUNK_SEC > 0 and (i + batch_size) < len(texts):
                        time.sleep(_BATCH_INTER_CHUNK_SEC)
                    continue
                logger.warning(
                    "Batch embed returned %d embeddings for %d inputs, falling back.",
                    len(embeddings), len(chunk),
                )
            elif response.status_code == 503:
                logger.warning(
                    "Batch embed 503 busy, cooling down %.1fs before sequential fallback.",
                    _BATCH_503_COOLDOWN_SEC,
                )
                time.sleep(_BATCH_503_COOLDOWN_SEC)
            else:
                logger.warning("Batch embed HTTP %d, falling back to sequential.", response.status_code)
        except Exception as e:
            logger.warning("Batch embed error: %s, falling back to sequential.", e)

        # Fallback: sequential
        for text in chunk:
            all_embeddings.append(get_embedding(text))

    return all_embeddings


def expand_query(query):
    """
    Query expansion for retrieval coverage.
    """
    if not ENABLE_QUERY_EXPANSION:
        return [query]
    if _query_prefers_chatlog(query):
        return [query]

    query_text = str(query or "").strip()
    if len(query_text) < 10:
        return [query_text or query]

    prompt = f"""<|begin_of_text|><|start_header_id|>system<|end_header_id|>
You are a search optimization AI.
Generate up to 3 concise variations of the user's search query.
Output ONLY one query per line.
<|eot_id|><|start_header_id|>user<|end_header_id|>
Query: {query}<|eot_id|><|start_header_id|>assistant<|end_header_id|>
"""
    try:
        response = _get_session().post(
            OLLAMA_GENERATE_URL,
            json={
                "model": GENERATE_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.4,
                "max_tokens": 128,
                "stream": False,
            },
            timeout=5,
        )
        if response.status_code == 200:
            data = response.json()
            choices = data.get("choices") or []
            raw = (choices[0].get("message") or {}).get("content", "").strip() if choices else data.get("response", "").strip()
            lines = raw.split("\n")
            variations = []
            base_tokens = set(_query_terms(query_text))
            for v in lines:
                cand = " ".join(v.strip().split())
                if not cand or cand == query_text:
                    continue
                if len(cand) > max(120, len(query_text) * 2):
                    continue
                cand_tokens = set(_query_terms(cand))
                if base_tokens:
                    overlap = len(base_tokens & cand_tokens) / max(1, len(base_tokens))
                    if overlap < 0.45:
                        continue
                variations.append(cand)
                if len(variations) >= 2:
                    break
            return [query_text] + variations
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 344, exc_info=True)

    return [query_text]


def search_fulltext(cursor, query_variations):
    results = {}

    cleaned_queries = []
    for q in query_variations:
        clean = "".join(c for c in q if c.isalnum() or c.isspace())
        if clean:
            cleaned_queries.append(clean)

    if not cleaned_queries:
        return {}

    search_str = " ".join(cleaned_queries)

    try:
        sql = """
        SELECT id, MATCH(content) AGAINST (%s IN NATURAL LANGUAGE MODE) AS score
        FROM documents
        WHERE MATCH(content) AGAINST (%s IN NATURAL LANGUAGE MODE)
        LIMIT 30
        """
        cursor.execute(sql, (search_str, search_str))
        rows = cursor.fetchall()
        for doc_id, score in rows:
            results[doc_id] = _safe_float(score)
    except Exception as e:
        logger.warning(f"Fulltext search error: {e}")

    return results


def reciprocal_rank_fusion(vector_results, fulltext_results, k=60):
    fused_scores = {}

    for rank, (doc_id, _) in enumerate(vector_results):
        fused_scores[doc_id] = fused_scores.get(doc_id, 0.0) + 1 / (k + rank + 1)

    sorted_ft = sorted(fulltext_results.items(), key=lambda x: x[1], reverse=True)
    for rank, (doc_id, _) in enumerate(sorted_ft):
        fused_scores[doc_id] = fused_scores.get(doc_id, 0.0) + 1 / (k + rank + 1)

    return sorted(fused_scores.items(), key=lambda x: x[1], reverse=True)


def _save_local_backup(content, source, embedding, is_synced):
    try:
        from skills.memory.local_db import save_local, save_vector_local

        lid = save_local(content, source, is_synced=is_synced)
        if lid > 0 and embedding is not None:
            save_vector_local(lid, embedding)
            return True
    except Exception as e:
        logger.warning(f"Local backup failed: {e}")
    return False


def _content_exists(cursor, content: str) -> bool:
    """Check if identical content already exists in documents table."""
    h = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()
    # Use MD5 prefix match + exact verify to avoid full-table scan
    cursor.execute(
        "SELECT 1 FROM documents WHERE MD5(content) = %s LIMIT 1", (h,)
    )
    return cursor.fetchone() is not None


def remember(content, source="manual", metadata: dict | None = None):
    """Store memory to Keeper; fallback to local backup if offline."""
    embedding = get_embedding(content)

    # Safe truncate for MySQL VARCHAR limits on 'source' column
    safe_source = build_source_signature(str(source or "manual"), metadata=metadata)[:250]

    if _keeper_offline():
        return _save_local_backup(content, safe_source, embedding, is_synced=False)

    try:
        conn = _get_conn()
        cursor = conn.cursor()

        # Dedup: skip if identical content already stored
        if _content_exists(cursor, content):
            logger.debug("Skipped duplicate content (source=%s)", safe_source[:60])
            cursor.close()
            conn.close()
            return True

        cursor.execute("INSERT INTO documents (content, source) VALUES (%s, %s)", (content, safe_source))
        doc_id = cursor.lastrowid

        cursor.execute(
            "INSERT INTO vectors (doc_id, embedding) VALUES (%s, %s)",
            (doc_id, json.dumps(embedding)),
        )

        conn.commit()
        _save_local_backup(content, safe_source, embedding, is_synced=True)

        # Sync to FAISS index
        if ENABLE_FAISS:
            try:
                fidx = _get_faiss_index()
                if fidx is not None:
                    fidx.add(doc_id, embedding)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 455, exc_info=True)  # non-fatal

        return True

    except mysql.connector.Error as e:
        _mark_keeper_offline(str(e)[:180])
        return _save_local_backup(content, safe_source, embedding, is_synced=False)

    except Exception as e:
        logger.error(f"Remember error: {e}")
        return _save_local_backup(content, safe_source, embedding, is_synced=False)

    finally:
        if "conn" in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def remember_batch(items):
    """
    Batch-insert multiple memories. Each item is a dict with 'content' and 'source'.
    Uses batch embedding to reduce Ollama round-trips.

    Args:
        items: list of {"content": str, "source": str}

    Returns:
        dict with 'ok', 'inserted', 'failed', 'total' counts.
    """
    if not items:
        return {"ok": True, "inserted": 0, "failed": 0, "total": 0}

    texts = [it.get("content", "") for it in items]
    sources = [
        build_source_signature(str(it.get("source", "batch")), metadata=it.get("metadata"))[:250]
        for it in items
    ]

    # Batch embed
    embeddings = get_embeddings_batch(texts)

    inserted = 0
    failed = 0

    if _keeper_offline():
        for i, (content, source) in enumerate(zip(texts, sources)):
            emb = embeddings[i] if i < len(embeddings) else _zero_embedding()
            if _save_local_backup(content, source, emb, is_synced=False):
                inserted += 1
            else:
                failed += 1
        return {"ok": failed == 0, "inserted": inserted, "failed": failed, "total": len(items)}

    conn = None
    try:
        conn = _get_conn()
        cursor = conn.cursor()

        # Dedup: compute hashes and check which already exist
        hashes = [hashlib.md5(t.encode("utf-8", errors="replace")).hexdigest() for t in texts]
        existing_hashes: set = set()
        # Check in batches of 200 to avoid query size limits
        for b_start in range(0, len(hashes), 200):
            batch_h = hashes[b_start : b_start + 200]
            ph = ",".join(["%s"] * len(batch_h))
            cursor.execute(f"SELECT DISTINCT MD5(content) FROM documents WHERE MD5(content) IN ({ph})", batch_h)
            existing_hashes.update(row[0] for row in cursor.fetchall())
        skipped = 0

        faiss_idx = _get_faiss_index() if ENABLE_FAISS else None

        for i, (content, source) in enumerate(zip(texts, sources)):
            # Skip duplicates
            if hashes[i] in existing_hashes:
                skipped += 1
                continue
            emb = embeddings[i] if i < len(embeddings) else _zero_embedding()
            try:
                cursor.execute(
                    "INSERT INTO documents (content, source) VALUES (%s, %s)",
                    (content, source),
                )
                doc_id = cursor.lastrowid
                cursor.execute(
                    "INSERT INTO vectors (doc_id, embedding) VALUES (%s, %s)",
                    (doc_id, json.dumps(emb)),
                )

                # FAISS incremental add
                if faiss_idx is not None:
                    try:
                        faiss_idx.add(doc_id, emb)
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 545, exc_info=True)

                inserted += 1
            except Exception as e:
                logger.warning("Batch insert item %d failed: %s", i, e)
                failed += 1

        conn.commit()

        # Save FAISS index if we added anything
        if inserted > 0 and faiss_idx is not None:
            try:
                faiss_idx.save_to_disk()
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 559, exc_info=True)

        if skipped:
            logger.info("remember_batch: skipped %d duplicates out of %d", skipped, len(items))
        return {"ok": failed == 0, "inserted": inserted, "failed": failed, "skipped": skipped, "total": len(items)}

    except mysql.connector.Error as e:
        _mark_keeper_offline(str(e)[:180])
        # Fallback: save everything locally
        for i, (content, source) in enumerate(zip(texts, sources)):
            emb = embeddings[i] if i < len(embeddings) else _zero_embedding()
            if _save_local_backup(content, source, emb, is_synced=False):
                inserted += 1
            else:
                failed += 1
        return {"ok": False, "inserted": inserted, "failed": failed, "total": len(items), "fallback": "local"}

    except Exception as e:
        logger.error(f"remember_batch error: {e}")
        return {"ok": False, "inserted": inserted, "failed": failed, "total": len(items), "error": str(e)}

    finally:
        if conn is not None and conn.is_connected():
            cursor.close()
            conn.close()


def _fallback_local_search(query, top_k, source_contains: str = ""):
    try:
        from skills.memory.local_db import search_local

        results = search_local(query, limit=top_k, source_contains=(source_contains or ""))
        return [
            {
                "id": r.get("id"),
                "content": r.get("content", ""),
                "source": f"{r.get('source', 'local')} [Local]",
                "score": 0.5,
            }
            for r in results
        ]
    except Exception:
        return []


_FAISS_REBUILD_LAUNCHED = False
_FAISS_REBUILD_PID: int | None = None

_FAISS_REBUILD_SCRIPT_MARKER = "MEMORY_ENABLE_FAISS"


def _kill_stale_faiss_rebuilds():
    """Kill any orphaned FAISS rebuild subprocesses from previous server runs.
    Delegates to daemon unified reaper when available."""
    try:
        from daemon import request_kill
        killed = request_kill(_FAISS_REBUILD_SCRIPT_MARKER, "FAISS rebuild cleanup")
        if killed:
            logger.info("🧹 daemon reaper cleaned FAISS rebuilds: PIDs %s", killed)
        return
    except ImportError:
        pass
    # Legacy fallback — daemon not importable
    import signal
    try:
        import psutil
        current = os.getpid()
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                if proc.pid == current:
                    continue
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if _FAISS_REBUILD_SCRIPT_MARKER in cmdline and "build_from_db" in cmdline:
                    logger.info("🧹 Killing orphaned FAISS rebuild PID %s", proc.pid)
                    os.kill(proc.pid, signal.SIGTERM)
            except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
                pass
    except ImportError:
        import subprocess as _sp
        try:
            _sp.run(
                ["pkill", "-f", f"{_FAISS_REBUILD_SCRIPT_MARKER}.*build_from_db"],
                capture_output=True, timeout=5,
            )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 644, exc_info=True)


def _launch_faiss_rebuild_bg():
    """Launch FAISS rebuild in a background subprocess (non-blocking).
    Kills any orphaned rebuild processes before spawning a new one.
    """
    global _FAISS_REBUILD_LAUNCHED, _FAISS_REBUILD_PID
    if _FAISS_REBUILD_LAUNCHED:
        # Check if previous rebuild is still running
        if _FAISS_REBUILD_PID:
            try:
                os.kill(_FAISS_REBUILD_PID, 0)  # probe — still alive
                return
            except OSError:
                pass  # dead, allow re-launch
        else:
            return
    _FAISS_REBUILD_LAUNCHED = True

    # Kill any orphaned rebuilds from previous server runs
    _kill_stale_faiss_rebuilds()

    import subprocess
    _magi_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _venv_py = os.path.join(_magi_root, "venv", "bin", "python3")
    if not os.path.exists(_venv_py):
        _venv_py = sys.executable
    _script = (
        "import sys, os; sys.path.insert(0, os.environ.get('MAGI_ROOT', '.')); "
        "os.environ['MEMORY_ENABLE_FAISS']='1'; "
        "from skills.memory.faiss_index import FAISSMemoryIndex; "
        "from skills.memory.mem_bridge import DB_CONFIG; "
        "idx = FAISSMemoryIndex(dim=768); "
        "n = idx.build_from_db(DB_CONFIG, batch_size=5000); "
        "print(f'FAISS rebuild done: {n} vectors')"
    )
    try:
        _log_path = os.path.join(_magi_root, ".agent", "faiss_rebuild.log")
        _log_f = open(_log_path, "w")
        proc = subprocess.Popen(
            [_venv_py, "-c", _script],
            cwd=_magi_root,
            stdout=_log_f,
            stderr=_log_f,
            env={**os.environ, "MAGI_ROOT": _magi_root},
        )
        _FAISS_REBUILD_PID = proc.pid
        threading.Thread(target=proc.wait, daemon=True).start()
        logger.info("FAISS background rebuild subprocess launched (PID %s, log: %s)", proc.pid, _log_path)
    except Exception as e:
        logger.warning("Failed to launch FAISS rebuild subprocess: %s", e)
        _FAISS_REBUILD_LAUNCHED = False


def _get_faiss_index():
    """Lazy-init the FAISS index singleton."""
    global _FAISS_INDEX
    if _FAISS_INDEX is not None:
        return _FAISS_INDEX

    with _FAISS_INIT_LOCK:
        if _FAISS_INDEX is not None:
            return _FAISS_INDEX
        try:
            from skills.memory.faiss_index import FAISSMemoryIndex

            idx = FAISSMemoryIndex.get_instance(dim=768)

            # If empty, launch background subprocess to rebuild (non-blocking).
            if idx.total == 0 and not _keeper_offline():
                logger.warning(
                    "FAISS index empty — launching background rebuild subprocess."
                )
                _launch_faiss_rebuild_bg()
            elif not _keeper_offline():
                # Try syncing new records
                try:
                    added = idx.sync_new_from_db(DB_CONFIG)
                    if added > 0:
                        logger.info("FAISS synced %d new vectors", added)
                except Exception as e:
                    logger.warning("FAISS sync failed (non-fatal): %s", e)

            _FAISS_INDEX = idx
            return idx
        except Exception as e:
            logger.warning("Failed to initialize FAISS: %s", e)
            return None


def _batch_fetch_docs(cursor, doc_ids):
    """Fetch multiple documents in one query instead of N queries."""
    if not doc_ids:
        return {}
    placeholders = ",".join(["%s"] * len(doc_ids))
    sql = f"SELECT id, content, source, created_at FROM documents WHERE id IN ({placeholders})"
    cursor.execute(sql, tuple(doc_ids))
    rows = cursor.fetchall()
    return {row[0]: row for row in rows}


def recall(query, top_k=3, source_contains: str = "",
           exclude_sources: tuple = ("codebase-ingest",)):
    want = max(1, int(top_k))
    if _keeper_offline():
        data = _fallback_local_search(query, want * 30, source_contains=source_contains)
        if source_contains:
            data = [x for x in data if source_contains in (x.get("source") or "")]
        data = _rank_recall_results(query, data)
        if not _query_prefers_chatlog(query) and not source_contains:
            trusted = [x for x in data if _safe_float(x.get("trust_weight")) >= 0.5]
            untrusted = [x for x in data if _safe_float(x.get("trust_weight")) < 0.5]
            data = trusted + untrusted
        return data[:want]

    try:
        conn = _get_conn()
        cursor = conn.cursor()

        query_embedding = list(_embedding_cache(query))
        _embedding_ok = any(abs(v) > 1e-12 for v in query_embedding[:10])
        if not _embedding_ok:
            logger.warning("Query embedding is zero (Ollama timeout?); falling back to fulltext-only recall")

        # ---------- FAISS fast path ----------
        faiss_idx = _get_faiss_index() if ENABLE_FAISS else None

        if _embedding_ok and faiss_idx is not None and faiss_idx.total > 0 and not source_contains:
            # FAISS global KNN — only when no source filter.
            _faiss_k = max(want * 6, 40)
            faiss_results = faiss_idx.search(query_embedding, top_k=_faiss_k)
            _MIN_SIM = 0.50  # discard low-relevance cosine hits (anti memory pollution)
            top_vectors = [(doc_id, score) for doc_id, score in faiss_results if score >= _MIN_SIM]
        elif _embedding_ok and faiss_idx is not None and faiss_idx.total > 0 and source_contains:
            # Source-filtered: use FAISS to pre-filter candidates, then
            # intersect with source filter in DB.  Much faster than brute-force
            # scanning all matching vectors from DB.
            _faiss_k = max(want * 20, 200)
            faiss_results = faiss_idx.search(query_embedding, top_k=_faiss_k)
            _candidate_ids = [doc_id for doc_id, score in faiss_results if score >= 0.40]
            if _candidate_ids:
                _ph = ",".join(["%s"] * len(_candidate_ids))
                cursor.execute(
                    f"SELECT v.doc_id, v.embedding FROM vectors v "
                    f"JOIN documents d ON v.doc_id = d.id "
                    f"WHERE v.doc_id IN ({_ph}) AND d.source LIKE %s",
                    (*_candidate_ids, f"%{source_contains}%"),
                )
                _filtered_rows = cursor.fetchall()
                import numpy as _np
                _MIN_SIM = 0.50
                top_vectors = []
                _q = _np.array(query_embedding, dtype=_np.float32)
                _q_norm = _np.linalg.norm(_q)
                for doc_id, vec_json in _filtered_rows:
                    try:
                        _v = _np.array(json.loads(vec_json), dtype=_np.float32)
                        _v_norm = _np.linalg.norm(_v)
                        if _q_norm > 1e-12 and _v_norm > 1e-12:
                            _sim = float(_np.dot(_v, _q) / (_v_norm * _q_norm))
                            if _sim >= _MIN_SIM:
                                top_vectors.append((doc_id, _sim))
                    except Exception:
                        continue
                top_vectors.sort(key=lambda x: x[1], reverse=True)
            else:
                top_vectors = []
        elif _embedding_ok:
            # FAISS unavailable: brute-force scan with LIMIT protection.
            if source_contains:
                cursor.execute(
                    "SELECT v.doc_id, v.embedding FROM vectors v "
                    "JOIN documents d ON v.doc_id = d.id "
                    "WHERE d.source LIKE %s LIMIT %s",
                    (f"%{source_contains}%", MAX_VECTOR_SCAN),
                )
            else:
                cursor.execute(
                    "SELECT doc_id, embedding FROM vectors ORDER BY doc_id DESC LIMIT %s",
                    (MAX_VECTOR_SCAN,),
                )
            vec_rows = cursor.fetchall()

            # Batch cosine similarity with numpy for speed
            try:
                import numpy as _np
                _doc_ids = []
                _vecs = []
                for doc_id, vec_json in vec_rows:
                    try:
                        _vecs.append(json.loads(vec_json))
                        _doc_ids.append(doc_id)
                    except Exception:
                        continue
                if _vecs:
                    _q = _np.array(query_embedding, dtype=_np.float32)
                    _m = _np.array(_vecs, dtype=_np.float32)
                    _q_norm = _np.linalg.norm(_q)
                    _m_norms = _np.linalg.norm(_m, axis=1)
                    _valid = (_q_norm > 1e-12) & (_m_norms > 1e-12)
                    _scores = _np.zeros(len(_vecs), dtype=_np.float32)
                    if _q_norm > 1e-12:
                        _scores[_valid] = (_m[_valid] @ _q) / (_m_norms[_valid] * _q_norm)
                    _top_idx = _np.argsort(-_scores)[:max(want * 20, 200)]
                    _MIN_SIM = 0.50
                    top_vectors = [(_doc_ids[i], float(_scores[i])) for i in _top_idx if _scores[i] >= _MIN_SIM]
                else:
                    top_vectors = []
                logger.info("Brute-force numpy scan: %d vecs → %d candidates (source=%s)",
                            len(vec_rows), len(top_vectors), source_contains or "all")
            except ImportError:
                # numpy unavailable — fall back to per-row computation
                vector_candidates = []
                for doc_id, vec_json in vec_rows:
                    try:
                        vec = json.loads(vec_json)
                    except Exception:
                        continue
                    score = _cosine_similarity(query_embedding, vec)
                    if score >= 0.50:
                        vector_candidates.append((doc_id, score))
                vector_candidates.sort(key=lambda x: x[1], reverse=True)
                _bf_limit = 40 if not source_contains else max(want * 20, 200)
                top_vectors = vector_candidates[:_bf_limit]
        else:
            # Embedding failed — skip vector search, rely on fulltext only
            top_vectors = []

        # ---------- Fulltext search ----------
        variations = expand_query(query)
        fulltext_scores = search_fulltext(cursor, variations)

        # ---------- RRF Fusion ----------
        fused_results = reciprocal_rank_fusion(top_vectors, fulltext_scores)
        source_contains = (source_contains or "").strip()
        # When source filter is active, keep more candidates before filtering
        _fused_limit = max(want * 12, 60) if not source_contains else max(want * 30, 300)
        top_fused = fused_results[:_fused_limit]

        # ---------- Batch fetch results ----------
        candidate_ids = [doc_id for doc_id, _ in top_fused]
        docs_map = _batch_fetch_docs(cursor, candidate_ids)

        results_data = []
        for doc_id, rrf_score in top_fused:
            doc = docs_map.get(doc_id)
            if not doc:
                continue
            src = doc[2] or ""
            if source_contains and (source_contains not in src):
                continue
            # 排除指定 source namespace（預設排除 codebase-ingest）
            if exclude_sources and any(ex in src for ex in exclude_sources):
                continue
            results_data.append(
                {
                    "id": doc[0],
                    "content": doc[1],
                    "source": doc[2],
                    "score": float(rrf_score),
                }
            )
            if len(results_data) >= want:
                break

        results_data = _rank_recall_results(query, results_data)
        if not _query_prefers_chatlog(query) and not source_contains:
            trusted = [x for x in results_data if _safe_float(x.get("trust_weight")) >= 0.5]
            untrusted = [x for x in results_data if _safe_float(x.get("trust_weight")) < 0.5]
            results_data = trusted + untrusted

        return results_data

    except mysql.connector.Error as e:
        _mark_keeper_offline(str(e)[:180])
        logger.warning(f"Keeper unavailable, fallback to local search: {e}")
        data = _fallback_local_search(query, want * 30, source_contains=source_contains)
        if source_contains:
            data = [x for x in data if source_contains in (x.get("source") or "")]
        data = _rank_recall_results(query, data)
        if not _query_prefers_chatlog(query) and not source_contains:
            trusted = [x for x in data if _safe_float(x.get("trust_weight")) >= 0.5]
            untrusted = [x for x in data if _safe_float(x.get("trust_weight")) < 0.5]
            data = trusted + untrusted
        return data[:want]

    except Exception as e:
        logger.error(f"Recall error: {e}")
        data = _fallback_local_search(query, want * 30, source_contains=source_contains)
        if source_contains:
            data = [x for x in data if source_contains in (x.get("source") or "")]
        data = _rank_recall_results(query, data)
        if not _query_prefers_chatlog(query) and not source_contains:
            trusted = [x for x in data if _safe_float(x.get("trust_weight")) >= 0.5]
            untrusted = [x for x in data if _safe_float(x.get("trust_weight")) < 0.5]
            data = trusted + untrusted
        return data[:want]

    finally:
        if "conn" in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def forget(query):
    """
    Deletes the strongest-matching memory safely.
    """
    return False, "Policy: forget/delete is disabled (no-delete requirement)."


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python mem_bridge.py [remember|recall|forget] 'text'")
        sys.exit(1)

    action = sys.argv[1]
    text = sys.argv[2]

    if action == "remember":
        ok = remember(text)
        print("OK" if ok else "FAILED")
    elif action == "recall":
        print(json.dumps(recall(text), ensure_ascii=False, indent=2))
    elif action == "forget":
        print(forget(text))
    else:
        print("Unknown action")
