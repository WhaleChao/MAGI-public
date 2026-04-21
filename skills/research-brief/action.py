#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
research-brief / action.py

Extensible multi-language research literature crawler with user-managed namespaces.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_THIS = Path(__file__).resolve()
_MAGI_ROOT = _THIS.parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))
# Allow sibling module imports (fetchers, translator_bridge, digest)
if str(_THIS.parent) not in sys.path:
    sys.path.insert(0, str(_THIS.parent))

logger = logging.getLogger("research-brief")
logging.basicConfig(
    level=os.environ.get("RESEARCH_BRIEF_LOG", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# ───────── paths ─────────

_RUNTIME_DIR = _MAGI_ROOT / ".runtime" / "research_brief"
_NS_DIR = _RUNTIME_DIR / "namespaces"
_SEEN_PATH = _RUNTIME_DIR / "seen.json"
_LAST_DIGEST_PATH = _RUNTIME_DIR / "last_digest.jsonl"
_SEEDS_DIR = _THIS.parent / "seeds"

_SEEN_TTL_DAYS = int(os.environ.get("RESEARCH_BRIEF_DEDUPE_TTL_DAYS", "60"))
_MAX_ENTRIES_PER_NS = int(os.environ.get("RESEARCH_BRIEF_MAX_ENTRIES_PER_NS", "12"))


# ───────── IO helpers ─────────

def _ok(payload: Dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("success") else 1


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("load %s failed: %s", path, e)
        return default


def _ensure_seeds_bootstrapped() -> None:
    """Copy seed JSONs into .runtime on first use (only if namespace doesn't exist)."""
    _NS_DIR.mkdir(parents=True, exist_ok=True)
    if not _SEEDS_DIR.exists():
        return
    for seed in _SEEDS_DIR.glob("*.json"):
        try:
            with open(seed, "r", encoding="utf-8") as f:
                data = json.load(f)
            ns_name = data.get("namespace") or seed.stem
            target = _NS_DIR / f"{_safe_filename(ns_name)}.json"
            if not target.exists():
                _atomic_write_json(target, data)
                logger.info("Seeded namespace: %s", ns_name)
        except Exception as e:
            logger.warning("seed bootstrap failed for %s: %s", seed, e)


def _safe_filename(name: str) -> str:
    # Preserve CJK; replace filesystem-unsafe chars
    bad = '<>:"/\\|?*\0'
    out = "".join("_" if c in bad else c for c in name).strip()
    return out or "unnamed"


# ───────── namespace ops ─────────

def _list_namespaces() -> List[str]:
    _ensure_seeds_bootstrapped()
    if not _NS_DIR.exists():
        return []
    return sorted(p.stem for p in _NS_DIR.glob("*.json"))


def _load_namespace(name: str) -> Optional[Dict[str, Any]]:
    _ensure_seeds_bootstrapped()
    path = _NS_DIR / f"{_safe_filename(name)}.json"
    if not path.exists():
        return None
    return _load_json(path, None)


def _save_namespace(name: str, data: Dict[str, Any]) -> None:
    path = _NS_DIR / f"{_safe_filename(name)}.json"
    _atomic_write_json(path, data)


def _delete_namespace(name: str) -> bool:
    path = _NS_DIR / f"{_safe_filename(name)}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def _new_empty_namespace(name: str) -> Dict[str, Any]:
    return {
        "namespace": name,
        "topic_key": "research_daily",
        "keywords": [],
        "sources": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ───────── dedupe ─────────
#
# Two-layer dedupe:
#   url_hash   — primary key; dedupes by URL so we don't hit the source again.
#   content_hash — fingerprint of title+snippet; if a URL is already seen but the
#                  content has meaningfully changed (e.g. an evolving advisory
#                  or an updated academic abstract), re-notify.
#
# Seen structure (new format):
#   { "<url_hash>": {"ts": <unix>, "content": "<content_hash>", "url": "<url>"} }
# Backward compatible: legacy format {"<url_hash>": <unix>} still honored as
# "seen but no content fingerprint" → content change triggers re-notify.

def _hash_url(u: str) -> str:
    return hashlib.sha256(u.strip().lower().encode("utf-8")).hexdigest()[:16]


def _hash_content(title: str, snippet: str) -> str:
    blob = (title or "").strip() + "|" + (snippet or "").strip()[:1500]
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _load_seen() -> Dict[str, Any]:
    data = _load_json(_SEEN_PATH, {})
    if not isinstance(data, dict):
        return {}
    return data


def _save_seen(seen: Dict[str, Any]) -> None:
    _atomic_write_json(_SEEN_PATH, seen)


def _seen_timestamp(entry: Any) -> float:
    """Normalize entry (legacy float or new dict) → timestamp."""
    if isinstance(entry, (int, float)):
        return float(entry)
    if isinstance(entry, dict):
        return float(entry.get("ts", 0) or 0)
    return 0.0


def _seen_content_hash(entry: Any) -> str:
    """Extract content hash from seen record (empty for legacy records)."""
    if isinstance(entry, dict):
        return str(entry.get("content") or "")
    return ""


def _prune_seen(seen: Dict[str, Any]) -> Dict[str, Any]:
    cutoff = time.time() - (_SEEN_TTL_DAYS * 86400)
    return {k: v for k, v in seen.items() if _seen_timestamp(v) >= cutoff}


# ───────── keyword filter ─────────

def _match_keywords(text: str, keywords: List[str]) -> bool:
    if not keywords:
        return True  # No filter → pass all
    if not text:
        return False
    low = text.lower()
    for kw in keywords:
        k = (kw or "").strip()
        if not k:
            continue
        if k.lower() in low or k in text:
            return True
    return False


# ───────── task: CRUD ─────────

def task_list() -> int:
    _ensure_seeds_bootstrapped()
    names = _list_namespaces()
    data: Dict[str, Any] = {"success": True, "namespaces": []}
    for n in names:
        ns = _load_namespace(n)
        if ns:
            data["namespaces"].append({
                "name": ns.get("namespace", n),
                "topic_key": ns.get("topic_key", ""),
                "source_count": len(ns.get("sources", [])),
                "keyword_count": len(ns.get("keywords", [])),
            })
    return _ok(data)


def task_list_namespace(namespace: str) -> int:
    ns = _load_namespace(namespace)
    if not ns:
        return _ok({"success": False, "error": "namespace_not_found", "namespace": namespace})
    return _ok({
        "success": True,
        "namespace": ns.get("namespace"),
        "topic_key": ns.get("topic_key"),
        "keywords": ns.get("keywords", []),
        "sources": ns.get("sources", []),
    })


def task_add_namespace(namespace: str) -> int:
    if not namespace.strip():
        return _ok({"success": False, "error": "empty_name"})
    if _load_namespace(namespace):
        return _ok({"success": False, "error": "namespace_already_exists", "namespace": namespace})
    ns = _new_empty_namespace(namespace)
    _save_namespace(namespace, ns)
    return _ok({"success": True, "namespace": namespace, "message": f"命名空間 '{namespace}' 已建立"})


def task_remove_namespace(namespace: str) -> int:
    ns = _load_namespace(namespace)
    if not ns:
        return _ok({"success": False, "error": "namespace_not_found"})
    if ns.get("sources"):
        return _ok({"success": False, "error": "namespace_not_empty",
                    "source_count": len(ns["sources"]),
                    "hint": "請先移除所有來源，或用 --force"})
    _delete_namespace(namespace)
    return _ok({"success": True, "message": f"命名空間 '{namespace}' 已刪除"})


def task_add_source(namespace: str, url: str, *, stype: str = "rss",
                    lang: Optional[str] = None, note: Optional[str] = None) -> int:
    ns = _load_namespace(namespace)
    if not ns:
        return _ok({"success": False, "error": "namespace_not_found"})
    url = url.strip()
    if not url:
        return _ok({"success": False, "error": "empty_url"})
    for s in ns.get("sources", []):
        if s.get("url") == url:
            return _ok({"success": False, "error": "source_already_exists"})
    ns.setdefault("sources", []).append({
        "url": url,
        "type": stype,
        "lang": lang,
        "note": note or "",
        "added_at": datetime.now(timezone.utc).isoformat(),
    })
    _save_namespace(namespace, ns)
    return _ok({"success": True, "message": f"已加入 {url} 至 {namespace}"})


def task_remove_source(namespace: str, url: str) -> int:
    ns = _load_namespace(namespace)
    if not ns:
        return _ok({"success": False, "error": "namespace_not_found"})
    before = len(ns.get("sources", []))
    ns["sources"] = [s for s in ns.get("sources", []) if s.get("url") != url.strip()]
    if len(ns["sources"]) == before:
        return _ok({"success": False, "error": "source_not_found"})
    _save_namespace(namespace, ns)
    return _ok({"success": True, "message": f"已從 {namespace} 移除 {url}"})


def task_add_keyword(namespace: str, keyword: str) -> int:
    ns = _load_namespace(namespace)
    if not ns:
        return _ok({"success": False, "error": "namespace_not_found"})
    kws = ns.setdefault("keywords", [])
    if keyword.strip() in kws:
        return _ok({"success": False, "error": "keyword_already_exists"})
    kws.append(keyword.strip())
    _save_namespace(namespace, ns)
    return _ok({"success": True, "message": f"已加入關鍵字 '{keyword}' 至 {namespace}"})


def task_remove_keyword(namespace: str, keyword: str) -> int:
    ns = _load_namespace(namespace)
    if not ns:
        return _ok({"success": False, "error": "namespace_not_found"})
    kws = ns.get("keywords", [])
    if keyword.strip() not in kws:
        return _ok({"success": False, "error": "keyword_not_found"})
    kws.remove(keyword.strip())
    _save_namespace(namespace, ns)
    return _ok({"success": True, "message": f"已從 {namespace} 移除 '{keyword}'"})


# ───────── task: fetch + digest ─────────

def _fetch_namespace(ns: Dict[str, Any], seen: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Fetch all sources for a namespace, filter by keywords, dedupe via seen.

    Two-layer dedupe:
      - URL unseen → collect
      - URL seen, content_hash differs → collect (content updated)
      - URL seen, content_hash same → skip (silent)
    """
    from fetchers import fetch_source  # local sibling
    keywords = ns.get("keywords", [])
    collected: List[Dict[str, Any]] = []
    for src in ns.get("sources", []):
        try:
            raw_entries = fetch_source(src)
        except Exception as e:
            logger.warning("fetch_source failed %s: %s", src.get("url"), e)
            continue
        src_name = src.get("note") or src.get("url", "")
        for ent in raw_entries:
            u = ent.get("url", "")
            if not u:
                continue
            h = _hash_url(u)
            content_hash = _hash_content(ent.get("title", ""), ent.get("snippet", ""))
            prior = seen.get(h)
            if prior is not None:
                prior_content = _seen_content_hash(prior)
                # Known URL + same content → skip
                if prior_content and prior_content == content_hash:
                    continue
                # Known URL + content changed → mark as update and include
                if prior_content:
                    ent["_content_changed"] = True
            body_for_filter = (ent.get("title", "") + " " + ent.get("snippet", ""))
            if not _match_keywords(body_for_filter, keywords):
                continue
            ent["source_name"] = src_name
            ent["_hash"] = h
            ent["_content_hash"] = content_hash
            collected.append(ent)
            if len(collected) >= _MAX_ENTRIES_PER_NS:
                break
        if len(collected) >= _MAX_ENTRIES_PER_NS:
            break
    return collected


# ───────── memory ingestion ─────────

def _ingest_entries_to_memory(namespace: str, entries: List[Dict[str, Any]]) -> int:
    """
    Write each entry into MAGI vector memory so recall can find it later.
    Returns number of successfully ingested entries. Silent-catches failures.
    """
    if not entries:
        return 0
    try:
        from skills.memory.mem_bridge import remember_batch  # type: ignore
    except Exception as e:
        logger.debug("mem_bridge unavailable: %s", e)
        return 0

    payloads: List[Dict[str, Any]] = []
    for e in entries:
        title = str(e.get("title") or "").strip()
        snippet = str(e.get("raw") or e.get("snippet") or "").strip()
        if not title and not snippet:
            continue
        text = f"{title}\n\n{snippet}"[:4000]
        meta = {
            "namespace": namespace,
            "url": e.get("url"),
            "source_name": e.get("source_name"),
            "lang_hint": e.get("lang_hint"),
            "published": e.get("published"),
            "trust": "external_source",
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }
        # mem_bridge.remember_batch expects {"content", "source", "metadata"}
        payloads.append({
            "content": text,
            "source": f"research-brief:{namespace}",
            "metadata": meta,
        })

    if not payloads:
        return 0
    try:
        result = remember_batch(payloads)
        if isinstance(result, dict):
            return int(result.get("inserted", len(payloads)))
        if isinstance(result, int):
            return result
        return len(payloads)
    except Exception as e:
        logger.warning("remember_batch failed for ns=%s: %s", namespace, e)
        return 0


def task_fetch(namespace: str) -> int:
    ns = _load_namespace(namespace)
    if not ns:
        return _ok({"success": False, "error": "namespace_not_found"})
    seen = _prune_seen(_load_seen())
    entries = _fetch_namespace(ns, seen)
    return _ok({
        "success": True,
        "namespace": namespace,
        "new_entries": len(entries),
        "titles": [e.get("title", "")[:80] for e in entries],
    })


def task_digest(namespace: Optional[str] = None, *, notify: bool = True) -> int:
    """Fetch + summarize + notify. namespace=None → all namespaces."""
    _ensure_seeds_bootstrapped()
    names = [namespace] if namespace else _list_namespaces()
    seen = _prune_seen(_load_seen())
    from digest import format_digest

    results: List[Dict[str, Any]] = []
    for n in names:
        ns = _load_namespace(n)
        if not ns:
            results.append({"namespace": n, "success": False, "error": "not_found"})
            continue
        entries = _fetch_namespace(ns, seen)
        topic_key = ns.get("topic_key") or "research_daily"
        if not entries:
            results.append({"namespace": n, "success": True, "new_entries": 0})
            continue
        try:
            digest_text = format_digest(n, entries, keyword_pool=ns.get("keywords", []))
        except Exception as e:
            logger.error("format_digest failed for %s: %s", n, e)
            results.append({"namespace": n, "success": False, "error": f"digest_error: {e}"})
            continue
        # Mark seen (new format: {ts, content, url})
        now = time.time()
        for e in entries:
            seen[e["_hash"]] = {
                "ts": now,
                "content": e.get("_content_hash", ""),
                "url": e.get("url", ""),
            }
        # Ingest into vector memory so MAGI recall can find these later
        try:
            ingested = _ingest_entries_to_memory(n, entries)
        except Exception as exc:
            logger.warning("memory ingestion failed for %s: %s", n, exc)
            ingested = 0
        # Count how many were "content updates" vs brand-new
        updates = sum(1 for e in entries if e.get("_content_changed"))
        brand_new = len(entries) - updates
        # Log digest
        try:
            _LAST_DIGEST_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_LAST_DIGEST_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "namespace": n,
                    "count": len(entries),
                    "new": brand_new,
                    "updated": updates,
                    "ingested": ingested,
                    "titles": [e.get("title", "")[:80] for e in entries],
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug("digest log append failed: %s", e)
        # Notify
        delivered = False
        if notify:
            try:
                from skills.ops.red_phone import alert_admin
                alert_admin(digest_text, severity="info",
                            source=f"research_brief.{n}",
                            topic_key=topic_key)
                delivered = True
            except Exception as e:
                logger.warning("notify failed for %s: %s", n, e)
        results.append({
            "namespace": n,
            "success": True,
            "new_entries": len(entries),
            "brand_new": brand_new,
            "content_updates": updates,
            "ingested_to_memory": ingested,
            "topic_key": topic_key,
            "delivered": delivered,
        })
    _save_seen(seen)
    return _ok({"success": True, "results": results, "total_namespaces": len(names)})


def task_query(namespace: str, keyword: str) -> int:
    """Ad-hoc search within a namespace's sources (fetch + filter)."""
    ns = _load_namespace(namespace)
    if not ns:
        return _ok({"success": False, "error": "namespace_not_found"})
    from fetchers import fetch_source  # local sibling
    hits: List[Dict[str, Any]] = []
    low = keyword.lower()
    for src in ns.get("sources", []):
        try:
            entries = fetch_source(src)
        except Exception:
            continue
        for e in entries:
            blob = (e.get("title", "") + " " + e.get("snippet", "")).lower()
            if low in blob or keyword in (e.get("title", "") + e.get("snippet", "")):
                e["source_name"] = src.get("note") or src.get("url", "")
                hits.append(e)
            if len(hits) >= 10:
                break
        if len(hits) >= 10:
            break
    return _ok({
        "success": True,
        "namespace": namespace,
        "keyword": keyword,
        "hits": len(hits),
        "results": [{"title": h.get("title"), "url": h.get("url"),
                     "source": h.get("source_name")} for h in hits],
    })


def task_self_test() -> int:
    errs: List[str] = []
    # Check seeds
    if not _SEEDS_DIR.exists():
        errs.append("seeds_dir_missing")
    # Check bootstrap
    try:
        _ensure_seeds_bootstrapped()
    except Exception as e:
        errs.append(f"bootstrap_failed: {e}")
    # List namespaces
    names = _list_namespaces()
    if len(names) < 1:
        errs.append("no_namespaces_after_bootstrap")
    # Try importing modules
    try:
        from fetchers import fetch_rss  # noqa
        from translator_bridge import translate_to_zh_hant  # noqa
        from digest import format_digest  # noqa
    except Exception as e:
        errs.append(f"module_import_failed: {e}")

    return _ok({
        "success": not errs,
        "errors": errs or None,
        "namespaces": names,
        "runtime_dir": str(_RUNTIME_DIR),
    })


# ───────── CLI ─────────

def main() -> int:
    parser = argparse.ArgumentParser(description="MAGI research-brief")
    parser.add_argument("--task", required=True,
                        choices=["list", "list_namespace", "add_namespace", "remove_namespace",
                                 "add_source", "remove_source", "add_keyword", "remove_keyword",
                                 "fetch", "digest", "digest_all", "query", "self_test"])
    parser.add_argument("--namespace", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--type", dest="stype", default="rss")
    parser.add_argument("--lang", default=None)
    parser.add_argument("--note", default=None)
    parser.add_argument("--keyword", default="")
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args()

    t = args.task

    if t == "list":
        return task_list()
    if t == "list_namespace":
        return task_list_namespace(args.namespace)
    if t == "add_namespace":
        return task_add_namespace(args.namespace)
    if t == "remove_namespace":
        return task_remove_namespace(args.namespace)
    if t == "add_source":
        return task_add_source(args.namespace, args.url, stype=args.stype,
                               lang=args.lang, note=args.note)
    if t == "remove_source":
        return task_remove_source(args.namespace, args.url)
    if t == "add_keyword":
        return task_add_keyword(args.namespace, args.keyword)
    if t == "remove_keyword":
        return task_remove_keyword(args.namespace, args.keyword)
    if t == "fetch":
        return task_fetch(args.namespace)
    if t == "digest":
        return task_digest(args.namespace or None, notify=not args.no_notify)
    if t == "digest_all":
        return task_digest(None, notify=not args.no_notify)
    if t == "query":
        return task_query(args.namespace, args.keyword)
    if t == "self_test":
        return task_self_test()

    return _ok({"success": False, "error": f"unknown_task: {t}"})


if __name__ == "__main__":
    sys.exit(main())
