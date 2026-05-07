import logging
import hashlib
import json
import os
from datetime import datetime
from typing import Dict, List, Tuple

from skills.memory.mem_bridge import remember, remember_batch
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

INDEX_PATH = os.environ.get(
    "MAGI_DOC_VECTOR_INDEX_PATH",
    f"{_MAGI_ROOT}/.agent/doc_vector_index.json",
)


def _now_iso() -> str:
    return datetime.now().isoformat()


def _sha1(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def _doc_key(kind: str, primary: str) -> str:
    return f"{(kind or 'doc').strip().lower()}-{_sha1(primary)[:12]}"


def _load_index() -> dict:
    try:
        if os.path.exists(INDEX_PATH):
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def _save_index(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
        with open(INDEX_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 44, exc_info=True)


def _chunk_text(text: str, chunk_chars: int = 1200, overlap: int = 120, max_chunks: int = 120) -> List[str]:
    s = (text or "").strip()
    if not s:
        return []
    chunk_chars = int(max(300, chunk_chars))
    overlap = int(max(0, min(overlap, chunk_chars // 2)))
    max_chunks = int(max(1, max_chunks))
    out = []
    i = 0
    while i < len(s) and len(out) < max_chunks:
        out.append(s[i : i + chunk_chars])
        i += max(1, chunk_chars - overlap)
    return out


def _prepare_embedding_inputs(parts: List[str]) -> List[str]:
    if not parts:
        return []
    try:
        from skills.engine.chinese_nlp import segment_for_indexing_many

        prepared = segment_for_indexing_many(parts)
        if isinstance(prepared, list) and len(prepared) == len(parts):
            return [str(item or "").strip() or parts[idx] for idx, item in enumerate(prepared)]
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 55, exc_info=True)
    return list(parts)


def _dedupe_batch_items(items: List[Dict]) -> Tuple[List[Dict], int]:
    """Remove exact-duplicate chunk contents within a single ingest batch."""
    deduped = []
    seen = set()
    skipped = 0
    for item in items or []:
        content = str(item.get("content") or "")
        if not content:
            continue
        key = _sha1(content)
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        deduped.append(item)
    return deduped, skipped


def ingest_sections_to_vector_memory(
    *,
    url: str,
    title: str,
    sections: List[Dict],
    chunk_chars: int = 1200,
    overlap: int = 120,
    max_chunks_total: int = 120,
) -> Dict:
    """
    Ingest a tabbed webpage/document into the vector memory (Keeper) as chunked entries.

    Each chunk is stored via `mem_bridge.remember()` with a structured source string so we can
    filter later using `recall(..., source_contains=doc_key)`.
    """
    url = (url or "").strip()
    if not url:
        return {"success": False, "error": "missing url"}
    doc_key = _doc_key("url", url)
    title = (title or "").strip()

    sections = [s for s in (sections or []) if isinstance(s, dict)]
    chunks_written = 0
    items = []
    budget = int(max(1, max_chunks_total))

    batch_items = []
    for sec in sections:
        sec_id = (sec.get("id") or "").strip() or "section"
        sec_title = (sec.get("title") or sec_id).strip()
        body = (sec.get("content") or "").strip()
        if not body:
            continue
        parts = _chunk_text(body, chunk_chars=chunk_chars, overlap=overlap, max_chunks=budget)
        if not parts:
            continue
        embedding_parts = _prepare_embedding_inputs(parts)
        for idx, part in enumerate(parts, start=1):
            if chunks_written >= budget:
                break
            src = (
                f"doc={doc_key}|kind=url|url={url}|title={title}|"
                f"section={sec_id}|section_title={sec_title}|chunk={idx}/{len(parts)}"
            )
            batch_items.append(
                {
                    "content": part,
                    "source": src,
                    "embedding_input": embedding_parts[idx - 1],
                }
            )
            chunks_written += 1
        budget = max(0, budget - len(parts))
        if chunks_written >= int(max_chunks_total):
            break
            
    batch_items, deduped_in_batch = _dedupe_batch_items(batch_items)
    res = remember_batch(batch_items)
    for b in batch_items:
        items.append({"source": b["source"], "ok": True, "chars": len(b["content"])})

    index = _load_index()
    entry = {
        "doc_key": doc_key,
        "kind": "url",
        "url": url,
        "title": title,
        "sections": [{"id": (s.get("id") or ""), "title": (s.get("title") or "")} for s in sections][:20],
        "chunks_written": chunks_written,
        "updated_at": _now_iso(),
    }
    index[doc_key] = entry
    _save_index(index)

    return {
        "success": True,
        "doc_key": doc_key,
        "chunks_written": chunks_written,
        "deduped_in_batch": deduped_in_batch,
        "index_path": INDEX_PATH,
        "items": items[:10],
    }


def ingest_text_to_vector_memory(
    *,
    kind: str,
    primary: str,
    title: str,
    text: str,
    chunk_chars: int = 1200,
    overlap: int = 120,
    max_chunks_total: int = 240,
) -> Dict:
    """
    Ingest arbitrary text into vector memory with chunking.

    - kind: "file" | "text" | "pdf" | etc.
    - primary: stable identifier for doc_key derivation (e.g., file path, URL, or full text hash seed)
    """
    kind = (kind or "doc").strip().lower()
    primary = (primary or "").strip()
    title = (title or "").strip()
    body = (text or "").strip()
    if not primary:
        return {"success": False, "error": "missing primary"}
    if not body:
        return {"success": False, "error": "missing text"}

    doc_key = _doc_key(kind, primary)
    parts = _chunk_text(body, chunk_chars=chunk_chars, overlap=overlap, max_chunks=int(max(1, max_chunks_total)))
    if not parts:
        return {"success": False, "error": "empty after chunking"}

    chunks_written = 0
    items = []

    batch_items = []
    embedding_parts = _prepare_embedding_inputs(parts)
    for idx, part in enumerate(parts, start=1):
        src = f"doc={doc_key}|kind={kind}|primary={primary}|title={title}|chunk={idx}/{len(parts)}"
        batch_items.append(
            {
                "content": part,
                "source": src,
                "embedding_input": embedding_parts[idx - 1],
            }
        )

    batch_items, deduped_in_batch = _dedupe_batch_items(batch_items)

    # Use batch insertion to avoid long hangs and sequential timeouts
    res = remember_batch(batch_items)
    chunks_written = int(res.get("inserted", 0) or 0)
    chunks_covered = chunks_written + int(res.get("skipped", 0) or 0)

    # Reconstruct items for the return dictionary
    for b in batch_items:
        items.append({"source": b["source"], "ok": True, "chars": len(b["content"])})

    index = _load_index()
    entry = {
        "doc_key": doc_key,
        "kind": kind,
        "primary": primary,
        "title": title,
        "chunks_written": chunks_covered,
        "chunks_inserted": chunks_written,
        "updated_at": _now_iso(),
    }
    index[doc_key] = entry
    _save_index(index)

    return {
        "success": True,
        "doc_key": doc_key,
        "chunks_written": chunks_covered,
        "chunks_inserted": chunks_written,
        "deduped_in_batch": deduped_in_batch,
        "index_path": INDEX_PATH,
        "items": items[:10],
    }
