#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
statutes-vdb / action.py
========================
CASPER SKILL: 依案件推斷相關法規，抓條文入向量資料庫（MemBridge）供對話查詢。

Tasks:
  update_cases — 以案件清單更新法規向量庫（建議夜間排程使用）
  search       — 查詢向量庫（本機冒煙測試）
  help
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests


# Prefer project venv (keeps deps consistent; avoids system Python drift)
_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import ensure_orch_on_sys_path, get_magi_root_dir, get_orch_dir, get_skill_python

_CODE_DIR = str(get_orch_dir())
_VENV_PY = str(get_skill_python())
try:
    if os.path.exists(_VENV_PY) and os.path.realpath(sys.executable) != os.path.realpath(_VENV_PY):
        os.execv(_VENV_PY, [_VENV_PY, __file__, *sys.argv[1:]])
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 45, exc_info=True)

# Ensure MAGI root on sys.path so `import skills.*` works.
SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
_MAGI_ROOT = str(get_magi_root_dir())


LAW_ZIP_URL = os.environ.get("MAGI_MOJ_LAW_ZIP_URL", "https://law.moj.gov.tw/api/Ch/Law/json")
CACHE_DIR = os.environ.get("MAGI_LAW_CACHE_DIR", os.path.join(_MAGI_ROOT, "cache", "laws"))
STATE_PATH = os.environ.get("MAGI_LAW_VDB_STATE_PATH", os.path.join(_MAGI_ROOT, "_statutes_vdb_state.json"))

# Chunking: keep embeddings stable and avoid timeouts / overly-long prompts.
MAX_CHUNK_CHARS = int(os.environ.get("MAGI_STATUTE_CHUNK_MAX_CHARS", "1200"))
MIN_CHUNK_CHARS = int(os.environ.get("MAGI_STATUTE_CHUNK_MIN_CHARS", "200"))
logger = logging.getLogger("statutes-vdb")

def _eventlog(event: str, *, ok: Optional[bool] = None, payload: Optional[dict] = None, tags: Optional[dict] = None) -> None:
    """
    Best-effort：將法規入庫結果寫入向量記憶（事件型），便於日後追溯。
    """
    try:
        ensure_orch_on_sys_path()
        import magi_eventlog  # type: ignore
        magi_eventlog.remember_event(event, ok=ok, payload=payload or {}, tags=tags or {}, source="statutes_vdb")
    except Exception:
        return


def _internet_enabled() -> bool:
    return os.environ.get("MAGI_ALLOW_INTERNET", "0").strip().lower() in {"1", "true", "yes", "on"}


def _now_iso() -> str:
    return datetime.now().isoformat()


def _json_load_maybe(s: str) -> Any:
    s = (s or "").strip()
    if not s:
        return {}
    if s.startswith("{") or s.startswith("["):
        return json.loads(s)
    return {"text": s}


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json(path: str, data: dict) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(p) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, str(p))
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 107, exc_info=True)


def _norm_name(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", "", s)
    s = s.replace("　", "")
    return s


def _case_domain(case_path: str) -> str:
    p = (case_path or "")
    # Keep it simple and robust: infer from folder names.
    if "/刑事/" in p or "刑事" in p:
        return "刑事"
    if "/民事/" in p or "民事" in p:
        return "民事"
    if "/行政/" in p or "行政" in p:
        return "行政"
    if "家事" in p:
        return "家事"
    if "消費者債務清理" in p:
        return "消債"
    return ""


DEFAULT_LAWS_BY_DOMAIN: Dict[str, List[str]] = {
    "刑事": ["刑法", "刑事訴訟法"],
    "民事": ["民法", "民事訴訟法"],
    "行政": ["行政程序法", "行政訴訟法"],
    "家事": ["民法", "家事事件法"],
    "消債": ["消費者債務清理條例", "民事訴訟法"],
}


LAW_NAME_HINT_RE = re.compile(r"([\u4e00-\u9fff]+(?:法|條例|通則|細則|規則|施行細則|施行法))")


def _extract_law_hints_from_case_path(case_path: str) -> List[str]:
    """
    從案件資料夾名稱拆出「看起來像法規名稱」的片段。
    例：2025-0088-余秋菊-二審-毒品危害防制條例
    """
    if not case_path:
        return []
    base = os.path.basename(case_path.rstrip(os.sep))
    parts = re.split(r"[-_－—–]+", base)
    hints: List[str] = []
    for part in parts:
        part = (part or "").strip()
        if not part:
            continue
        # Direct match if the part itself looks like a law name.
        m = LAW_NAME_HINT_RE.fullmatch(part.replace(" ", ""))
        if m:
            hints.append(m.group(1))
            continue
        # Otherwise, find inside.
        for mm in LAW_NAME_HINT_RE.finditer(part):
            hints.append(mm.group(1))
    # de-dup while preserving order
    out: List[str] = []
    seen: Set[str] = set()
    for h in hints:
        n = _norm_name(h)
        if n and n not in seen:
            seen.add(n)
            out.append(h)
    return out


def _download_zip_bytes(url: str, *, timeout_sec: int = 60) -> bytes:
    r = requests.get(url, timeout=timeout_sec)
    if r.status_code != 200:
        raise RuntimeError(f"http_{r.status_code}")
    return r.content


@dataclass
class LawDataset:
    update_date: str
    laws: List[dict]
    by_name: Dict[str, dict]


def _load_dataset_cached() -> LawDataset:
    """
    Cache the MOJ zip on disk and parse ChLaw.json.
    """
    Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
    zip_path = os.path.join(CACHE_DIR, "ChLaw.json.zip")
    meta_path = os.path.join(CACHE_DIR, "meta.json")

    meta = _load_json(meta_path) or {}
    max_age_sec = int(os.environ.get("MAGI_LAW_CACHE_MAX_AGE_SEC", str(24 * 3600)))
    now = time.time()

    need_fetch = True
    try:
        st = os.stat(zip_path)
        if st.st_size > 1024 and (now - st.st_mtime) < max_age_sec:
            need_fetch = False
    except Exception:
        need_fetch = True

    if need_fetch:
        try:
            if not _internet_enabled():
                raise RuntimeError("internet_disabled(MAGI_ALLOW_INTERNET=0)")
            b = _download_zip_bytes(LAW_ZIP_URL, timeout_sec=90)
            tmp = zip_path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(b)
            os.replace(tmp, zip_path)
            meta = {"fetched_at": _now_iso(), "url": LAW_ZIP_URL, "size": len(b)}
            _save_json(meta_path, meta)
        except Exception as e:
            # If network is restricted/offline, fall back to existing cache if present.
            if not os.path.exists(zip_path):
                _eventlog("statutes_vdb:dataset:fetch", ok=False, payload={"error": str(e)[:220], "has_cache": False})
                raise RuntimeError(f"download_failed_and_no_cache: {e}")
            _eventlog("statutes_vdb:dataset:fetch", ok=False, payload={"error": str(e)[:220], "has_cache": True})
            meta = {"fetched_at": meta.get("fetched_at"), "url": LAW_ZIP_URL, "error": str(e)[:200]}
            _save_json(meta_path, meta)

    with zipfile.ZipFile(zip_path, "r") as z:
        if "ChLaw.json" not in z.namelist():
            raise RuntimeError("zip_missing_ChLaw.json")
        with z.open("ChLaw.json", "r") as f:
            # ChLaw.json has UTF-8 BOM; json.load handles it for text mode, but here is bytes.
            raw = f.read()
    # Strip BOM if present.
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    data = json.loads(raw.decode("utf-8", errors="replace"))
    update_date = str(data.get("UpdateDate") or "").strip()
    laws = data.get("Laws") or []
    by_name: Dict[str, dict] = {}
    for law in laws:
        nm = _norm_name(law.get("LawName") or "")
        if nm:
            by_name[nm] = law
    return LawDataset(update_date=update_date, laws=laws, by_name=by_name)


def _split_chunks(text: str) -> List[str]:
    """
    Split statute content into chunks for stable embeddings.
    """
    t = (text or "").strip()
    if not t:
        return []
    if len(t) <= MAX_CHUNK_CHARS:
        return [t]

    # Prefer splitting by full-width punctuation / line breaks.
    parts = re.split(r"(?:\n{2,}|\r\n{2,}|。|；|;)", t)
    chunks: List[str] = []
    buf = ""
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        if not buf:
            buf = p
            continue
        if len(buf) + 1 + len(p) <= MAX_CHUNK_CHARS:
            buf = buf + "。 " + p
        else:
            if len(buf) >= MIN_CHUNK_CHARS:
                chunks.append(buf.strip())
                buf = p
            else:
                # If too short, force append anyway to reduce tiny chunks.
                chunks.append((buf + "。 " + p).strip())
                buf = ""
    if buf.strip():
        chunks.append(buf.strip())

    # Final safety: hard cut any oversized chunk
    out: List[str] = []
    for c in chunks:
        c = c.strip()
        if not c:
            continue
        if len(c) <= MAX_CHUNK_CHARS:
            out.append(c)
        else:
            for i in range(0, len(c), MAX_CHUNK_CHARS):
                out.append(c[i : i + MAX_CHUNK_CHARS].strip())
    return [x for x in out if x]


def _remember(text: str, source: str) -> bool:
    from skills.memory.mem_bridge import remember
    return bool(remember(text, source=source))


def _remember_batch(items):
    """Batch-insert items via mem_bridge.remember_batch(). Returns dict with counts."""
    from skills.memory.mem_bridge import remember_batch
    return remember_batch(items)


def _recall(query: str, top_k: int = 5) -> list:
    from skills.memory.mem_bridge import recall
    r = recall(query, top_k=top_k)
    if isinstance(r, dict):
        return r.get("results") or r.get("items") or []
    return r or []


def _compute_case_laws(case_number: str, case_path: str, dataset: LawDataset) -> Tuple[List[str], List[str]]:
    hints = _extract_law_hints_from_case_path(case_path)
    domain = _case_domain(case_path)
    defaults = DEFAULT_LAWS_BY_DOMAIN.get(domain, [])

    candidate = []
    candidate.extend(hints)
    candidate.extend(defaults)

    # Normalize + keep only existing law names.
    found: List[str] = []
    missing: List[str] = []
    seen: Set[str] = set()
    def _resolve(n: str) -> Optional[str]:
        if n in dataset.by_name:
            return dataset.by_name[n]["LawName"]
        # Fuzzy fallback for common abbreviations, e.g. 刑法 -> 中華民國刑法
        for key, law in dataset.by_name.items():
            if n and (n in key or key.endswith(n)):
                return law.get("LawName") or ""
        return None

    for nm in candidate:
        n = _norm_name(nm)
        if not n or n in seen:
            continue
        seen.add(n)
        resolved = _resolve(n)
        if resolved:
            found.append(resolved)
        else:
            missing.append(nm)

    return found, missing


def task_update_cases(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    payload:
      {
        "cases": [{"case_number":"2025-0088","case_path":"/..."}],
        "force": false
      }
    """
    t0 = time.time()
    force = bool(payload.get("force", False))
    cases = payload.get("cases") or []
    if not isinstance(cases, list):
        return {"ok": False, "error": "cases must be a list"}

    dataset = _load_dataset_cached()
    state = _load_json(STATE_PATH) or {}
    ingested = state.get("ingested") or {}  # law_norm -> {"update_date": "...", "ts": "...", "article_chunks": int}

    cases_processed = 0
    laws_linked: Dict[str, List[str]] = {}
    laws_missing: Dict[str, List[str]] = {}

    laws_ingested: List[str] = []
    laws_skipped: List[str] = []
    law_errors: List[str] = []

    # Pre-collect all laws needed for this batch.
    needed_laws: List[str] = []
    for c in cases:
        cn = str((c or {}).get("case_number") or "").strip()
        cp = str((c or {}).get("case_path") or "").strip()
        if not cp:
            continue
        laws, missing = _compute_case_laws(cn, cp, dataset)
        if laws:
            laws_linked[cn or cp] = laws
        if missing:
            laws_missing[cn or cp] = missing
        needed_laws.extend(laws)
        cases_processed += 1

        # Store "case ↔ laws" mapping (always; cheap)
        try:
            if cn:
                doc = {
                    "case_number": cn,
                    "case_path": cp,
                    "domain": _case_domain(cp),
                    "laws": laws,
                    "dataset_update": dataset.update_date,
                    "ts": _now_iso(),
                }
                _remember(json.dumps(doc, ensure_ascii=False), source=f"case_statutes|case_number={cn}|update={dataset.update_date}")
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 409, exc_info=True)

    # De-dup needed_laws while preserving order.
    uniq: List[str] = []
    seen: Set[str] = set()
    for nm in needed_laws:
        n = _norm_name(nm)
        if n and n not in seen:
            seen.add(n)
            uniq.append(nm)

    # Ingest each law if not already ingested for this dataset update.
    for law_name in uniq:
        ln = _norm_name(law_name)
        if not ln:
            continue
        law = dataset.by_name.get(ln)
        if not law:
            continue

        prev = (ingested.get(ln) or {})
        if (not force) and prev.get("update_date") == dataset.update_date:
            laws_skipped.append(law.get("LawName") or law_name)
            continue

        try:
            arts = law.get("LawArticles") or []
            batch_items = []
            for a in arts:
                art_no = str((a or {}).get("ArticleNo") or "").strip()
                art_ct = str((a or {}).get("ArticleContent") or "").strip()
                if not art_no or not art_ct:
                    continue
                chunks = _split_chunks(art_ct)
                for idx, ch in enumerate(chunks, start=1):
                    suffix = f"#{idx}" if len(chunks) > 1 else ""
                    source = f"statute|law={law.get('LawName')}|article={art_no}{suffix}|update={dataset.update_date}"
                    text = f"{law.get('LawName')}\n{art_no}{suffix}\n{ch}"
                    batch_items.append({"content": text, "source": source})

            if batch_items:
                result = _remember_batch(batch_items)
                chunk_count = result.get("inserted", 0)
            else:
                chunk_count = 0

            ingested[ln] = {"update_date": dataset.update_date, "ts": _now_iso(), "article_chunks": int(chunk_count)}
            laws_ingested.append(law.get("LawName") or law_name)
        except Exception as e:
            law_errors.append(f"{law.get('LawName') or law_name}: {type(e).__name__}: {e}")

    state["last_run"] = {"ts": _now_iso(), "dataset_update": dataset.update_date}
    state["ingested"] = ingested
    _save_json(STATE_PATH, state)

    ok = (len(law_errors) == 0)
    out = {
        "ok": ok,
        "dataset_update": dataset.update_date,
        "cases_processed": cases_processed,
        "laws_linked": laws_linked,
        "laws_missing": laws_missing,
        "laws_ingested": laws_ingested,
        "laws_skipped": laws_skipped,
        "errors": law_errors[:20],
        "elapsed_sec": round(time.time() - t0, 3),
        "state_path": STATE_PATH,
        "cache_dir": CACHE_DIR,
    }
    _eventlog(
        "statutes_vdb:update_cases",
        ok=bool(ok),
        payload={
            "dataset_update": dataset.update_date,
            "cases_processed": cases_processed,
            "laws_ingested": len(laws_ingested),
            "laws_skipped": len(laws_skipped),
            "missing_cases": len(laws_missing or {}),
            "errors": (law_errors or [])[:5],
            "elapsed_sec": out.get("elapsed_sec"),
        },
    )

    if bool(payload.get("background_fill", True)):
        import subprocess
        import threading as _threading
        cmd = [sys.executable, os.path.abspath(__file__), "--task", "background_fill {}"]
        try:
            _p = subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _threading.Thread(target=_p.wait, daemon=True).start()
        except Exception as e:
            out["background_fill_error"] = str(e)

    return out

def task_background_fill(payload: Dict[str, Any]) -> Dict[str, Any]:
    dataset = _load_dataset_cached()
    state = _load_json(STATE_PATH) or {}
    ingested = state.get("ingested") or {}
    force = bool(payload.get("force", False))
    laws_ingested = []
    law_errors = []

    for law_name, law in dataset.by_name.items():
        now = datetime.now()
        # 依據要求：將背景法規向量化集中在「半夜 01:00 到早上 08:30」之間執行
        is_allowed_time = (1 <= now.hour < 8) or (now.hour == 8 and now.minute < 30)
        if not is_allowed_time:
            break

        ln = _norm_name(law_name)
        prev = ingested.get(ln) or {}
        if (not force) and prev.get("update_date") == dataset.update_date:
            continue
        
        try:
            arts = law.get("LawArticles") or []
            batch_items = []
            for a in arts:
                art_no = str((a or {}).get("ArticleNo") or "").strip()
                art_ct = str((a or {}).get("ArticleContent") or "").strip()
                if not art_no or not art_ct:
                    continue
                chunks = _split_chunks(art_ct)
                for idx, ch in enumerate(chunks, start=1):
                    suffix = f"#{idx}" if len(chunks) > 1 else ""
                    source = f"statute|law={law.get('LawName')}|article={art_no}{suffix}|update={dataset.update_date}"
                    text = f"{law.get('LawName')}\n{art_no}{suffix}\n{ch}"
                    batch_items.append({"content": text, "source": source})

            if batch_items:
                result = _remember_batch(batch_items)
                chunk_count = result.get("inserted", 0)
            else:
                chunk_count = 0

            ingested[ln] = {"update_date": dataset.update_date, "ts": _now_iso(), "article_chunks": int(chunk_count)}
            laws_ingested.append(law.get("LawName") or law_name)
            
            # 隨時儲存狀態，以便中斷後可接續
            state["last_run"] = {"ts": _now_iso(), "dataset_update": dataset.update_date}
            state["ingested"] = ingested
            _save_json(STATE_PATH, state)

            time.sleep(0.5)  # 批次化後可降低 sleep (從 1.2s → 0.5s)
        except Exception as e:
            law_errors.append(f"{law_name}: {e}")

    return {
        "ok": True,
        "laws_ingested": len(laws_ingested),
        "errors": len(law_errors),
        "message": "Background fill finished or paused due to time limit."
    }


_CRIME_ARTICLE_MAP = {
    "詐欺罪": ["中華民國刑法|第 339 條"],
    "詐欺": ["中華民國刑法|第 339 條"],
    "加重詐欺罪": ["中華民國刑法|第 339-4 條"],
    "強盜罪": ["中華民國刑法|第 328 條"],
    "搶奪罪": ["中華民國刑法|第 325 條"],
    "恐嚇取財罪": ["中華民國刑法|第 346 條"],
    "恐嚇罪": ["中華民國刑法|第 305 條", "中華民國刑法|第 346 條"],
    "侵占罪": ["中華民國刑法|第 335 條"],
    "背信罪": ["中華民國刑法|第 342 條"],
    "重利罪": ["中華民國刑法|第 344 條"],
    "傷害罪": ["中華民國刑法|第 277 條"],
    "重傷罪": ["中華民國刑法|第 278 條"],
    "強制性交罪": ["中華民國刑法|第 221 條"],
    "妨害自由罪": ["中華民國刑法|第 302 條"],
    "公然侮辱罪": ["中華民國刑法|第 309 條"],
    "誹謗罪": ["中華民國刑法|第 310 條"],
    "毀損罪": ["中華民國刑法|第 354 條"],
    "偽造文書罪": ["中華民國刑法|第 210 條", "中華民國刑法|第 211 條"],
    "贓物罪": ["中華民國刑法|第 349 條"],
    "遺棄罪": ["中華民國刑法|第 293 條", "中華民國刑法|第 294 條"],
    "擄人勒贖罪": ["中華民國刑法|第 347 條"],
    "放火罪": ["中華民國刑法|第 173 條", "中華民國刑法|第 174 條"],
    "酒駕": ["中華民國刑法|第 185-3 條"],
    "不能安全駕駛罪": ["中華民國刑法|第 185-3 條"],
    "肇事逃逸罪": ["中華民國刑法|第 185-4 條"],
}


def _statute_keyword_search(query: str, top_k: int = 10) -> list:
    """Fast keyword-based statute search using SQL LIKE on content.

    Chinese text cannot be effectively searched via MySQL fulltext (needs ngram),
    and nomic-embed-text produces poor embeddings for Chinese legal text.
    Use direct keyword matching as the primary retrieval path.
    """
    try:
        from skills.memory.mem_bridge import _get_conn
        conn = _get_conn()
        cur = conn.cursor()

        # Direct article lookup for known crime names (handles cases where
        # the article text doesn't contain the crime name, e.g. §339 "詐欺罪")
        _mapped_articles = []
        _compact_q = re.sub(r"\s+", "", query)
        _map_keys = [_compact_q]
        for sfx in ("罪",):
            if _compact_q.endswith(sfx) and len(_compact_q) > len(sfx) + 1:
                _map_keys.append(_compact_q[:-len(sfx)])
        for mk in _map_keys:
            if mk in _CRIME_ARTICLE_MAP:
                for art_key in _CRIME_ARTICLE_MAP[mk]:
                    law_name, art_num = art_key.split("|")
                    cur.execute(
                        "SELECT id, content, source FROM documents "
                        "WHERE source LIKE %s AND source LIKE %s "
                        "ORDER BY id DESC LIMIT 1",
                        (f"%law={law_name}|%", f"%article={art_num}|%"),
                    )
                    row = cur.fetchone()
                    if row:
                        _mapped_articles.append({
                            "id": row[0], "content": row[1], "source": row[2],
                            "score": 1.0, "_rel": 10,
                        })

        # Build search variants: original, compact, individual tokens, stem
        variants = [query]
        compact = re.sub(r"\s+", "", query)
        if compact and compact != query:
            variants.append(compact)
        for tok in re.split(r"[\s,，。;；:：\-_()（）]+", query):
            tok = (tok or "").strip()
            if len(tok) >= 2:
                variants.append(tok)
        # Strip common legal suffixes to find definition articles
        # e.g. "殺人罪" → "殺人", "詐欺罪" → "詐欺"
        for suffix in ("罪", "條", "章"):
            if compact.endswith(suffix) and len(compact) > len(suffix) + 1:
                stem = compact[:-len(suffix)]
                if stem not in variants:
                    variants.append(stem)

        seen_ids: set = set()
        # Pre-populate with mapped articles
        for ma in _mapped_articles:
            seen_ids.add(ma["id"])
        items = []
        for v in variants:
            if not v:
                continue
            # Relevance tiers:
            #   5 = substantive law (刑法) + definition pattern (為X罪)
            #   4 = substantive law (刑法/民法) + term in first 200 chars
            #   3 = procedural law (刑訴/民訴) + term in first 200 chars
            #   2 = source contains query term
            #   1 = term in first 200 chars of content
            #   0 = term elsewhere in content
            cur.execute(
                "SELECT id, content, source, "
                "  CASE WHEN source LIKE '%%law=中華民國刑法|%%' "
                "            AND content LIKE %s THEN 5 "
                "       WHEN (source LIKE '%%law=中華民國刑法|%%' OR source LIKE '%%law=民法|%%') "
                "            AND LEFT(content, 200) LIKE %s THEN 4 "
                "       WHEN (source LIKE '%%law=刑事訴訟法|%%' OR source LIKE '%%law=民事訴訟法|%%') "
                "            AND LEFT(content, 200) LIKE %s THEN 3 "
                "       WHEN source LIKE %s THEN 2 "
                "       WHEN LEFT(content, 200) LIKE %s THEN 1 "
                "       ELSE 0 END AS relevance "
                "FROM documents "
                "WHERE source LIKE 'statute|%%' AND content LIKE %s "
                "AND content NOT LIKE '%%（刪除）%%' "
                "ORDER BY relevance DESC, id DESC LIMIT %s",
                (f"%為{v}%", f"%{v}%", f"%{v}%", f"%{v}%", f"%{v}%", f"%{v}%", max(top_k * 3, 30)),
            )
            for row in cur.fetchall():
                doc_id, content, source, relevance = row[0], row[1], row[2], row[3]
                if doc_id not in seen_ids:
                    seen_ids.add(doc_id)
                    items.append({"id": doc_id, "content": content, "source": source, "score": 1.0, "_rel": relevance})
                elif relevance > 0:
                    # Update relevance if this variant scores higher
                    for it in items:
                        if it["id"] == doc_id and it.get("_rel", 0) < relevance:
                            it["_rel"] = relevance
                            break

        # Merge mapped articles
        items = _mapped_articles + items

        # Boost definition articles in 中華民國刑法 only:
        # Article body starts directly with query stem → likely the definition article
        # e.g. §271 "殺人者，處..." (boost) vs §332 "犯強盜罪而故意殺人者" (no boost)
        _query_compact = re.sub(r"\s+", "", query)
        _stems = [_query_compact]
        for suffix in ("罪", "條", "章"):
            if _query_compact.endswith(suffix) and len(_query_compact) > len(suffix) + 1:
                _stems.append(_query_compact[:-len(suffix)])
        for item in items:
            if "law=中華民國刑法|" not in item.get("source", ""):
                continue
            content = item.get("content", "")
            _body_start = content.find("條\n")
            if _body_start < 0:
                continue
            _body = content[_body_start + 2:_body_start + 30]
            for s in _stems:
                # Definition pattern: body starts with stem + 者 within next 3 chars
                # e.g. "殺人者，處..." (✓) vs "竊盜或搶奪" (✗)
                if _body.startswith(s) and "者" in _body[len(s):len(s) + 3]:
                    item["_rel"] = max(item.get("_rel", 0), 7)
                    break

        # Sort by relevance (highest first), then by id DESC
        items.sort(key=lambda x: (x.get("_rel", 0), x["id"]), reverse=True)

        # Deduplicate by law+article (keep highest-relevance version)
        deduped = {}
        for item in items:
            src = item["source"]
            key_match = re.search(r"law=([^|]+)\|article=([^|]+)", src)
            if key_match:
                key = f"{key_match.group(1)}|{key_match.group(2)}"
                if key not in deduped:
                    deduped[key] = item
            else:
                deduped[src] = item

        result = list(deduped.values())[:top_k]
        for r in result:
            r.pop("_rel", None)
        return result
    except Exception as e:
        logger.warning("statute keyword search failed: %s", e)
        return []


def task_search(payload: Dict[str, Any]) -> Dict[str, Any]:
    q = str(payload.get("query") or "").strip()
    if not q:
        return {"ok": False, "error": "missing query"}
    top_k = int(payload.get("top_k") or 5)

    # Primary: fast keyword search on statute content (SQL LIKE).
    # This is more reliable than vector search for Chinese legal text
    # because nomic-embed-text produces poor Chinese embeddings.
    items = _statute_keyword_search(q, top_k=top_k)

    # Fallback: vector recall (slower, less accurate for Chinese statutes)
    if not items:
        try:
            from skills.memory.mem_bridge import recall
            items = recall(q, top_k=top_k, source_contains="statute|") or []
        except Exception:
            items = _recall(q, top_k=top_k)

    return {"ok": True, "query": q, "top_k": top_k, "items": items}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    args = parser.parse_args()

    task = (args.task or "").strip()
    if task in ("help", "--help", "-h"):
        print(json.dumps({"ok": True, "tasks": ["help", "update_cases", "search"]}, ensure_ascii=False, indent=2))
        return

    parts = task.split(" ", 1)
    cmd = parts[0]
    payload = _json_load_maybe(parts[1] if len(parts) > 1 else "")

    if cmd in ("update_cases", "update", "nightly"):
        out = task_update_cases(payload if isinstance(payload, dict) else {})
    elif cmd in ("background_fill",):
        out = task_background_fill(payload if isinstance(payload, dict) else {})
    elif cmd in ("search", "查法規", "法規查詢"):
        out = task_search(payload if isinstance(payload, dict) else {})
    else:
        out = {"ok": False, "error": f"未知 task: {task}"}

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
