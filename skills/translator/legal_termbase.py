"""
法學翻譯術語庫 (T4).

三層 termbase:
  Tier 1: Taiwan MOJ 官方雙語法規 (SQLite articles + terms)
  Tier 2: 學理/判例術語 (legal_academic_terms.json seed)
  Tier 3: 新穎詞 — 保留原文 + 提示暫譯（prompt 規則，不入庫）

Public API:
  build_tier1_from_moj(json_root)  → sqlite path
  lookup_tier2(term, language)     → dict | None
  build_glossary_for_chunk(text, target_lang) → (tier1_block, tier2_block)
  build_vector_index(db_path)      → faiss path | None
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).resolve().parent
_MAGI_ROOT = _THIS_DIR.parents[1]
_DATA_DIR = _MAGI_ROOT / "data" / "moj_bilingual"
_DB_PATH = _DATA_DIR / "termbase.sqlite"
_FAISS_PATH = _DATA_DIR / "termbase.faiss"
_TIER2_PATH = _DATA_DIR / "legal_academic_terms.json"

# ---------------------------------------------------------------------------
# Tier 1 — MOJ bilingual database
# ---------------------------------------------------------------------------

def _create_db_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        law_zh TEXT,
        law_en TEXT,
        article_no TEXT,
        text_zh TEXT
    );
    CREATE TABLE IF NOT EXISTS terms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        term_zh TEXT,
        term_en TEXT,
        law_zh TEXT,
        article_no TEXT,
        confidence TEXT DEFAULT 'medium'
    );
    CREATE INDEX IF NOT EXISTS idx_terms_zh ON terms(term_zh);
    CREATE INDEX IF NOT EXISTS idx_terms_en ON terms(term_en);
    CREATE INDEX IF NOT EXISTS idx_articles_law ON articles(law_zh);
    """)
    conn.commit()


def build_tier1_from_moj(json_root: Path, db_path: Optional[Path] = None) -> Path:
    """
    掃 json_root 下的 *.json 法規檔，建 SQLite termbase.
    json_root 可以是 FalVMingLing/ 子目錄或包含 *.json 的目錄。
    db_path: optional override (useful for tests); defaults to _DB_PATH.
    回傳 SQLite 路徑。
    """
    json_root = Path(json_root)
    if db_path is None:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        db_path = _DB_PATH
    else:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    _create_db_schema(conn)
    conn.execute("DELETE FROM articles")
    conn.execute("DELETE FROM terms")
    conn.commit()

    inserted_articles = 0
    inserted_terms = 0
    max_files = int(os.environ.get("MAGI_TERMBASE_MAX_FILES", "2000") or "2000")
    files_processed = 0

    # Walk up to 3 levels deep
    json_files: List[Path] = []
    for root, _dirs, files in os.walk(str(json_root)):
        depth = len(Path(root).relative_to(json_root).parts)
        if depth > 3:
            break
        for fname in files:
            if fname.lower().endswith(".json"):
                json_files.append(Path(root) / fname)
        if len(json_files) >= max_files:
            break

    for jf in json_files[:max_files]:
        if files_processed % 100 == 0 and files_processed > 0:
            time.sleep(0.02)
        try:
            with open(str(jf), "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue

        if not isinstance(data, dict):
            continue

        law_zh = str(data.get("法規名稱") or data.get("name") or "").strip()
        law_en = str(data.get("英文法規名稱") or data.get("eng") or "").strip()
        if not law_zh:
            continue

        articles = data.get("法條") or data.get("articles") or []
        if not isinstance(articles, list):
            articles = []

        for art in articles:
            if not isinstance(art, dict):
                continue
            article_no = str(art.get("條號") or art.get("no") or "").strip()
            text_zh = str(art.get("條文內容") or art.get("content") or "").strip()
            if not text_zh:
                continue
            conn.execute(
                "INSERT INTO articles (law_zh, law_en, article_no, text_zh) VALUES (?, ?, ?, ?)",
                (law_zh, law_en, article_no, text_zh[:4000]),
            )
            inserted_articles += 1
            # Extract key legal nouns as terms (simple heuristic for 4字以上詞)
            for kw in _extract_legal_terms_heuristic(text_zh):
                if kw:
                    conn.execute(
                        "INSERT OR IGNORE INTO terms (term_zh, term_en, law_zh, article_no, confidence) VALUES (?, ?, ?, ?, ?)",
                        (kw, "", law_zh, article_no, "medium"),
                    )
                    inserted_terms += 1

        files_processed += 1

    conn.commit()
    conn.close()
    logger.info(
        "build_tier1_from_moj: files=%d articles=%d terms=%d → %s",
        files_processed, inserted_articles, inserted_terms, db_path,
    )
    return db_path


def _extract_legal_terms_heuristic(text: str) -> List[str]:
    """Heuristic: extract 2-8 char Chinese noun phrases likely to be legal terms."""
    # Match noun-like patterns: 2~8 Chinese chars, not pure particles
    terms = re.findall(r'[\u4e00-\u9fff]{2,8}', text)
    # Filter common particles/function words
    _STOP = frozenset(["之間", "因此", "不得", "依照", "以下", "但書", "以上", "規定", "第一", "第二"])
    return [t for t in terms if t not in _STOP]


def lookup_tier1(term_zh: str, db_path: Optional[Path] = None) -> List[Dict]:
    """Exact lookup in terms table by Chinese term."""
    path = str(db_path or _DB_PATH)
    if not os.path.exists(path):
        return []
    try:
        conn = sqlite3.connect(path)
        rows = conn.execute(
            "SELECT term_zh, term_en, law_zh, article_no FROM terms WHERE term_zh = ? LIMIT 5",
            (term_zh,),
        ).fetchall()
        conn.close()
        return [{"term_zh": r[0], "term_en": r[1], "law_zh": r[2], "article_no": r[3]} for r in rows]
    except Exception:
        return []


def lookup_article(law_zh: str, article_no: str, db_path: Optional[Path] = None) -> Optional[Dict]:
    """Look up a specific article by law name and article number."""
    path = str(db_path or _DB_PATH)
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(path)
        row = conn.execute(
            "SELECT law_zh, law_en, article_no, text_zh FROM articles "
            "WHERE law_zh = ? AND article_no LIKE ? LIMIT 1",
            (law_zh, f"%{article_no}%"),
        ).fetchone()
        conn.close()
        if row:
            return {"law_zh": row[0], "law_en": row[1], "article_no": row[2], "text_zh": row[3]}
        return None
    except Exception:
        return None


def build_vector_index(db_path: Optional[Path] = None) -> Optional[Path]:
    """
    Build a FAISS vector index from article text_zh.
    Requires ModernBERT at port 8081. Returns FAISS path or None if unavailable.
    """
    path = db_path or _DB_PATH
    if not os.path.exists(str(path)):
        logger.warning("termbase.sqlite not found; skipping vector index")
        return None
    try:
        import faiss
        import numpy as np
        import requests as _req

        embed_url = os.environ.get("MAGI_EMBED_URL", "http://127.0.0.1:8081/v1/embeddings")
        conn = sqlite3.connect(str(path))
        rows = conn.execute("SELECT id, text_zh FROM articles WHERE text_zh != '' LIMIT 5000").fetchall()
        conn.close()
        if not rows:
            return None

        ids, texts = zip(*rows)
        embeddings = []
        batch_size = 32
        for i in range(0, len(texts), batch_size):
            batch = list(texts[i:i + batch_size])
            resp = _req.post(embed_url, json={"input": batch, "model": "text-embedding"}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("data", []):
                embeddings.append(item["embedding"])

        mat = np.array(embeddings, dtype="float32")
        dim = mat.shape[1]
        index = faiss.IndexFlatIP(dim)
        faiss.normalize_L2(mat)
        index.add(mat)
        faiss.write_index(index, str(_FAISS_PATH))
        logger.info("Vector index built: %d articles → %s", len(ids), _FAISS_PATH)
        return _FAISS_PATH
    except Exception as e:
        logger.info("Vector index build skipped: %s", e)
        return None


# ---------------------------------------------------------------------------
# Tier 2 — Academic legal terms
# ---------------------------------------------------------------------------

_TIER2_CACHE: Optional[Dict] = None
_TIER2_MTIME: float = 0.0


def _load_tier2() -> Dict:
    global _TIER2_CACHE, _TIER2_MTIME
    path = str(_TIER2_PATH)
    if not os.path.exists(path):
        return {}
    try:
        mtime = os.path.getmtime(path)
        if _TIER2_CACHE is not None and mtime <= _TIER2_MTIME:
            return _TIER2_CACHE
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Remove metadata key
        data.pop("__meta", None)
        _TIER2_CACHE = data
        _TIER2_MTIME = mtime
        return data
    except Exception:
        return {}


def lookup_tier2(term: str, language: str = "en") -> Optional[Dict]:
    """
    Exact lookup of a term in the Tier 2 academic terms JSON.
    language: 'en' to look up English term, 'zh' to look up Chinese term.
    Returns dict with 'zh', 'source', 'confidence' or None.
    """
    data = _load_tier2()
    if not data:
        return None
    term_norm = (term or "").strip().lower()
    if language == "en":
        # Direct lookup (case-insensitive)
        for key, val in data.items():
            if key.lower() == term_norm and isinstance(val, dict):
                return val
        return None
    else:
        # Reverse lookup: find entries where any zh value matches
        for key, val in data.items():
            if not isinstance(val, dict):
                continue
            zh_list = val.get("zh") or []
            if isinstance(zh_list, str):
                zh_list = [zh_list]
            if term in zh_list or term_norm in [z.lower() for z in zh_list]:
                return {"en": key, **val}
        return None


def _detect_tier2_in_text(text: str, direction: str = "en2zh") -> List[Tuple[str, Dict]]:
    """
    Scan text for Tier 2 terms. Returns list of (term, entry) matches.
    direction: 'en2zh' (find English terms in text) or 'zh2en' (find Chinese terms).
    """
    data = _load_tier2()
    if not data:
        return []
    results = []
    if direction == "en2zh":
        for key, val in data.items():
            if not isinstance(val, dict):
                continue
            # Case-insensitive whole-word search
            pattern = r'\b' + re.escape(key) + r'\b'
            if re.search(pattern, text, re.IGNORECASE):
                results.append((key, val))
    else:
        for key, val in data.items():
            if not isinstance(val, dict):
                continue
            zh_list = val.get("zh") or []
            if isinstance(zh_list, str):
                zh_list = [zh_list]
            for zh in zh_list:
                if zh and zh in text:
                    results.append((zh, {"en": key, **val}))
                    break
    return results


# ---------------------------------------------------------------------------
# Glossary builder
# ---------------------------------------------------------------------------

_LEGAL_SIGNALS_ZH = frozenset([
    "民法", "刑法", "行政法", "訴訟", "侵權", "契約", "賠償", "判決",
    "裁定", "起訴", "撤銷", "上訴", "損害", "被告", "原告", "法院",
    "第", "條", "款", "項", "法規", "法條", "違約", "義務", "權利",
])
_LEGAL_SIGNALS_EN = frozenset([
    "article", "civil code", "criminal code", "plaintiff", "defendant",
    "court", "tort", "damages", "contract", "statute", "liability",
    "pursuant", "provision", "section", "clause",
])


def _is_legal_text(text: str) -> bool:
    """Detect if text is likely legal content requiring termbase enrichment."""
    t_lower = (text or "").lower()
    t_orig = text or ""
    zh_hits = sum(1 for s in _LEGAL_SIGNALS_ZH if s in t_orig)
    en_hits = sum(1 for s in _LEGAL_SIGNALS_EN if s in t_lower)
    return zh_hits >= 2 or en_hits >= 2 or (zh_hits >= 1 and en_hits >= 1)


def build_glossary_for_chunk(text: str, target_lang: str = "繁體中文") -> Tuple[str, str]:
    """
    Build Tier 1 and Tier 2 glossary blocks for injection into translation prompt.
    Returns (tier1_block, tier2_block) — empty strings if termbase unavailable or no hits.
    """
    if not text or not text.strip():
        return "", ""
    if not _is_legal_text(text):
        return "", ""

    # Determine direction
    has_cjk = bool(re.search(r'[\u4e00-\u9fff]', text))
    direction = "zh2en" if has_cjk else "en2zh"

    tier1_lines: List[str] = []
    tier2_lines: List[str] = []

    # Tier 1: look for article references
    if os.path.exists(str(_DB_PATH)):
        try:
            # Detect "民法第 184 條" type patterns
            art_refs = re.findall(r'([\u4e00-\u9fff]{2,8})\s*第\s*(\d+)\s*條', text)
            for law_name, art_num in art_refs[:5]:
                row = lookup_article(law_name, art_num)
                if row:
                    en_name = row.get("law_en") or ""
                    tier1_lines.append(
                        f"{law_name}第{art_num}條 = {en_name} Article {art_num}: {row['text_zh'][:150]}"
                        if direction == "zh2en"
                        else f"Article {art_num} of {en_name or law_name}: {row['text_zh'][:150]}"
                    )
        except Exception as e:
            logger.debug("tier1 lookup failed: %s", e)

    # Tier 2: scan text for academic terms
    try:
        tier2_hits = _detect_tier2_in_text(text, direction=direction)
        for term, entry in tier2_hits[:10]:
            if direction == "en2zh":
                zh_vals = entry.get("zh") or []
                if isinstance(zh_vals, str):
                    zh_vals = [zh_vals]
                pref = "，".join(zh_vals[:2])
                src = entry.get("source", "")
                conf = entry.get("confidence", "high")
                tier2_lines.append(f"{term} → {pref}（{src}，{conf}）")
            else:
                en_key = entry.get("en") or term
                pref = entry.get("zh") or []
                if isinstance(pref, list):
                    pref = "，".join(pref[:2])
                tier2_lines.append(f"{term} → {en_key}（{entry.get('source', '')}）")
    except Exception as e:
        logger.debug("tier2 scan failed: %s", e)

    tier1_block = "\n".join(tier1_lines) if tier1_lines else ""
    tier2_block = "\n".join(tier2_lines) if tier2_lines else ""
    return tier1_block, tier2_block


def build_legal_prompt(
    text: str,
    target_lang: str,
    tier1_block: str = "",
    tier2_block: str = "",
) -> str:
    """
    Build the full legal translation prompt with three-tier rules injected.
    """
    t1 = tier1_block.strip() or "（未建置 MOJ 雙語法規庫，略）"
    t2 = tier2_block.strip() or "（無匹配學理術語）"
    return (
        f"[系統指令 — 法學翻譯模式]\n"
        f"你是法學翻譯專家。請依下列三層權威規則翻譯：\n\n"
        f"【Tier 1 - MOJ 官方雙語法規】（強制採用，不得改寫）\n{t1}\n\n"
        f"【Tier 2 - 學理/判例術語】（優先採用，除非上下文明顯衝突）\n{t2}\n\n"
        f"【Tier 3 - 新穎/不確定詞彙處理規則】\n"
        f"1. 若詞彙不在 Tier 1/2 且你無 100% 把握其標準譯法，必須：\n"
        f"   - 中→英：保留中文原詞 + 提出暫譯。如：「規範空間 (tentative: Nomosphere)」\n"
        f"   - 英→中：保留英文原詞 + 提出暫譯。如：「Nomosphere（暫譯：規範空間）」\n"
        f"2. 不可自信地輸出單一翻譯而隱藏不確定性。\n"
        f"3. 專有名詞（人名、地名、機構名）一律保留原文，必要時加括號譯名。\n\n"
        f"任務：翻譯下列文本為 {target_lang}，保留法律精確性與不確定性標示。\n\n"
        f"{text}"
    )


# ---------------------------------------------------------------------------
# Legal mode detection
# ---------------------------------------------------------------------------

def should_use_legal_mode(text: str) -> bool:
    """
    Return True if MAGI_TRANSLATOR_LEGAL_MODE env is '1' or ('auto' and text is legal).
    """
    mode = os.environ.get("MAGI_TRANSLATOR_LEGAL_MODE", "auto").strip().lower()
    if mode == "0":
        return False
    if mode == "1":
        return True
    # auto
    return _is_legal_text(text)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="法學翻譯術語庫管理工具")
    parser.add_argument("command", choices=["build", "lookup", "glossary"],
                        help="build: 建立 termbase | lookup: 查詢術語 | glossary: 示範輸出")
    parser.add_argument("--moj-dir", default="", help="MOJ JSON 根目錄（build 用）")
    parser.add_argument("--term", default="", help="查詢詞（lookup 用）")
    parser.add_argument("--lang", default="en", help="查詢語言 en/zh（lookup 用）")
    parser.add_argument("--text", default="", help="翻譯文本（glossary 用）")
    parser.add_argument("--target", default="繁體中文", help="目標語言（glossary 用）")
    args = parser.parse_args()

    if args.command == "build":
        moj_dir = args.moj_dir or str(_DATA_DIR / "repo")
        db = build_tier1_from_moj(Path(moj_dir))
        try:
            conn = sqlite3.connect(str(db))
            n_a = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            n_t = conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
            conn.close()
        except Exception:
            n_a, n_t = 0, 0
        print(json.dumps({"success": True, "db": str(db), "articles": n_a, "terms": n_t},
                         ensure_ascii=False, indent=2))

    elif args.command == "lookup":
        entry = lookup_tier2(args.term, language=args.lang)
        print(json.dumps({"success": bool(entry), "term": args.term, "result": entry},
                         ensure_ascii=False, indent=2))

    elif args.command == "glossary":
        t1, t2 = build_glossary_for_chunk(args.text, target_lang=args.target)
        print(json.dumps({"tier1": t1, "tier2": t2}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
