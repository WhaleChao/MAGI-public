#!/usr/bin/env python3
import logging
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

ARCHIVE_ROOT = os.environ.get("JUDICIAL_ARCHIVE_ROOT", f"{_MAGI_ROOT}/archive/judicial_search").strip()
DEFAULT_MAX_RESULTS = int(os.environ.get("JUDICIAL_ARCHIVE_MAX_RESULTS", "120"))
DEFAULT_MAX_CHARS = int(os.environ.get("JUDICIAL_ARCHIVE_MAX_CHARS", "300000"))
DEFAULT_SEARCH_POOL_CAP = int(os.environ.get("JUDICIAL_ARCHIVE_SEARCH_POOL_CAP", "3000"))
DEFAULT_RELAXED_POOL_CAP = int(os.environ.get("JUDICIAL_ARCHIVE_RELAXED_POOL_CAP", "6000"))
DEFAULT_FIRST_PASS_MULT = int(os.environ.get("JUDICIAL_ARCHIVE_FIRST_PASS_MULT", "16"))
DEFAULT_RELAXED_MULT = int(os.environ.get("JUDICIAL_ARCHIVE_RELAXED_MULT", "40"))
from api.runtime_paths import get_orch_dir

CODE_DIR = os.environ.get("CASPER_CODE_DIR", str(get_orch_dir())).strip()

# Make local code (casper_tools_client.py) importable even when CWD is the skill folder.
if CODE_DIR and os.path.isdir(CODE_DIR) and CODE_DIR not in sys.path:
    sys.path.insert(0, CODE_DIR)


def _ok(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("success") else 1


def _load_jsonish(text: str) -> dict:
    t = (text or "").strip()
    if not t:
        return {}
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else {"value": v}
    except Exception:
        return {"value": t}


def _safe_filename(s: str, max_len: int = 80) -> str:
    s = (s or "").strip()
    s = re.sub(r"[\\/:*?\"<>|\\n\\r\\t]+", "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return "untitled"
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s


def _short(s: str, width: int = 220) -> str:
    return textwrap.shorten((s or "").replace("\n", " "), width=width, placeholder="...")


def _heuristic_boolify(query: str) -> str:
    """
    Minimal, deterministic fallback boolifier for 司法院裁判書全文內容。
    Output uses AND/OR/NOT and parentheses only.
    """
    q = (query or "").strip()
    if not q:
        return ""

    # Normalize common connectors (Taiwan usage).
    rep = {
        "並且": " AND ",
        "而且": " AND ",
        "以及": " AND ",
        "還有": " AND ",
        "跟": " AND ",
        "與": " AND ",
        "和": " AND ",
        "&": " AND ",
        "或是": " OR ",
        "或者": " OR ",
        "或": " OR ",
        "|": " OR ",
    }
    for k, v in rep.items():
        q = q.replace(k, v)

    # NOT patterns: "排除 X", "不要 X", "不含 X"
    q = re.sub(r"(排除|不要|不含|去掉|剔除)\s+", " NOT ", q)

    # Basic cleanup + tokenize
    q = q.replace("（", "(").replace("）", ")")
    q = re.sub(r"\s+([()])\s*", r" \1 ", q)
    q = re.sub(r"\s+", " ", q).strip()

    tokens = [t for t in q.split(" ") if t]
    if not tokens:
        return ""

    ops = {"AND", "OR", "NOT"}
    neg_words = {"排除", "不要", "不含", "去掉", "剔除"}

    # If the user gave no explicit operator, default to AND between terms.
    has_op = any((t.upper() in ops) for t in tokens)
    if not has_op and len(tokens) > 1:
        tokens = [t for pair in zip(tokens, ["AND"] * (len(tokens) - 1) + [""]) for t in pair if t]

    # Normalize operators and NOT words
    norm = []
    for t in tokens:
        u = t.upper()
        if t in neg_words:
            norm.append("NOT")
        elif u in ops:
            norm.append(u)
        else:
            norm.append(t)

    # Remove repeated/invalid operator sequences
    cleaned = []
    for t in norm:
        if t in ops:
            if not cleaned:
                continue
            if cleaned[-1] in ops:
                # Prefer NOT if present, otherwise keep the latter operator.
                if cleaned[-1] == "NOT" and t != "NOT":
                    continue
                if t == cleaned[-1]:
                    continue
                cleaned[-1] = t
                continue
        cleaned.append(t)

    # Trim trailing operators
    while cleaned and cleaned[-1] in ops:
        cleaned.pop()

    # Drop dangling NOT (e.g., "... NOT" or "... NOT AND ...")
    out = []
    for i, t in enumerate(cleaned):
        if t == "NOT":
            nxt = cleaned[i + 1] if i + 1 < len(cleaned) else ""
            if not nxt or nxt in ops or nxt == ")":
                continue
        out.append(t)

    q = " ".join(out).strip()
    q = re.sub(r"\s+", " ", q).strip()
    return q


def _casper_boolify(query: str, timeout_sec: int = 60) -> dict:
    """
    Ask CASPER to convert natural language into a boolean query string.
    Returns: {success, boolean_query, source, error?}
    """
    q = (query or "").strip()
    if not q:
        return {"success": False, "error": "missing query"}

    try:
        from casper_tools_client import casper_chat
    except Exception as e:
        return {"success": False, "error": f"import casper_tools_client failed: {type(e).__name__}: {str(e)[:120]}"}

    prompt = "\n".join(
        [
            "你是「司法院裁判書查詢（全文內容）」的布林查詢轉換器。",
            "請把使用者的自然語句需求，轉成可以直接貼到司法院「全文內容」欄位的布林查詢字串。",
            "也請支援使用者用「Google 搜尋風格」輸入（空白=AND、+、-、OR、引號片語）。",
            "",
            "輸出規則：",
            "- 只輸出一行查詢字串本身，不要任何解釋、不要 JSON。",
            "- 目標語法請用：",
            "  - AND 用 '+' 連接（例如：詐欺+洗錢）",
            "  - NOT 用 '-' 前綴（例如：-未遂）",
            "  - OR 用 ' OR '（前後要有空白）",
            "- 不要輸出括號，不要輸出引號（有引號片語就把空白改成 '+'）。",
            "- 保留法院/案由/時間等詞當成一般關鍵詞即可（例如：臺灣高等法院、損害賠償）。",
            "",
            "使用者輸入：",
            q,
            "",
            "查詢字串：",
        ]
    )
    try:
        r = casper_chat(prompt, timeout_sec=int(timeout_sec))
    except Exception as e:
        # CASPER tools API 可能暫時不可用；此時直接回退 heuristic，不要讓整個技能炸掉。
        return {"success": False, "error": f"casper_chat exception: {type(e).__name__}: {str(e)[:160]}"}
    if not isinstance(r, dict) or not r.get("success"):
        return {"success": False, "error": (r.get("error") if isinstance(r, dict) else "casper_chat failed")}

    raw = (r.get("response") or r.get("output") or "").strip()
    if not raw:
        return {"success": False, "error": "empty casper output"}

    line = raw.splitlines()[0].strip()
    line = line.strip("\"'` ")
    # Sanitize: keep only allowed chars
    line = line.replace("（", " ").replace("）", " ").replace("(", " ").replace(")", " ")
    line = re.sub(r"\b(and|or|not)\b", lambda m: m.group(1).upper(), line, flags=re.I)
    # Allow only: Chinese/alnum/underscore/space/+/- and 'OR'
    line = re.sub(r"[^\w\u4e00-\u9fff\s+\-]", " ", line)
    line = re.sub(r"\s+", " ", line).strip()
    line = re.sub(r"\s*OR\s*", " OR ", line, flags=re.I).strip()
    if not line:
        return {"success": False, "error": "casper output sanitized to empty"}
    return {"success": True, "boolean_query": line, "source": "casper", "route": r.get("route", ""), "model": r.get("model", "")}


def _looks_like_google_query(q: str) -> bool:
    s = (q or "").strip()
    if not s:
        return False
    # Explicit operators or google-ish tokens.
    if "+" in s:
        return True
    if re.search(r"(^|\s)-\S+", s):
        return True
    if re.search(r"\bOR\b|\|", s, flags=re.I):
        return True
    if "\"" in s or "“" in s or "”" in s:
        return True
    return False


def _is_keyword_query(q: str) -> bool:
    """
    Decide whether the user's input is closer to "keywords" than a natural-language request.
    If so, treat spaces as AND deterministically (Google-like), instead of asking CASPER to paraphrase.
    """
    s = (q or "").strip()
    if not s:
        return False
    # Too long is usually a sentence.
    if len(s) > 60:
        return False
    # Contains common request verbs/particles -> sentence.
    if re.search(r"(請|幫我|麻煩|想要|希望|我要|目標|最後|幫忙)", s):
        return False
    # If it's mostly CJK/alnum/space and has multiple terms, treat as keywords.
    if re.fullmatch(r"[()\w\u4e00-\u9fff\s+\-\"“”]+", s) and len([t for t in re.split(r"\s+", s) if t]) >= 2:
        return True
    return False


def _google_to_fjud(q: str) -> str:
    """
    Convert a google-like query string into FJUD-friendly form.
    Rules:
    - spaces => AND => '+'
    - '+' stays '+'
    - '-term' stays as NOT
    - 'OR' stays as ' OR '
    - quoted phrases: inner spaces become '+'
    """
    s = (q or "").strip()
    if not s:
        return ""

    # Normalize quotes
    s = s.replace("“", "\"").replace("”", "\"").replace("’", "'").replace("‘", "'")

    # Extract quoted phrases, replace with placeholders
    phrases: list[str] = []

    def _sub(m):
        phrases.append(m.group(1))
        return f"__PHRASE_{len(phrases)-1}__"

    s = re.sub(r"\"([^\"]+)\"", _sub, s)
    s = re.sub(r"\s+", " ", s).strip()

    # Normalize OR and separators
    s = s.replace("&", " ")
    s = s.replace("|", " OR ")
    s = re.sub(r"\b(or)\b", "OR", s, flags=re.I)
    s = re.sub(r"\s*OR\s*", " OR ", s).strip()

    # Tokenize by space
    toks = [t for t in s.split(" ") if t]
    out = []
    for t in toks:
        if t == "OR":
            out.append("OR")
            continue
        # Restore phrase placeholder
        pm = re.match(r"__PHRASE_(\d+)__", t)
        if pm:
            idx = int(pm.group(1))
            phrase = (phrases[idx] if 0 <= idx < len(phrases) else "").strip()
            # phrase inner spaces => AND
            phrase = re.sub(r"\s+", "+", phrase)
            out.append(phrase)
            continue
        out.append(t)

    # Join: default AND between adjacent terms => '+'
    parts: list[str] = []
    for t in out:
        if t == "OR":
            # Trim trailing '+'
            while parts and parts[-1] == "+":
                parts.pop()
            if parts and parts[-1] != " OR ":
                parts.append(" OR ")
            continue
        if parts and parts[-1] not in {" OR "}:
            parts.append("+")
        parts.append(t)

    q2 = "".join(parts)
    q2 = q2.replace("+-", "-")
    q2 = re.sub(r"\+{2,}", "+", q2)
    q2 = re.sub(r"\s+", " ", q2).strip()
    q2 = re.sub(r"\+\s*OR\s*\+", " OR ", q2)
    q2 = re.sub(r"\s*OR\s*", " OR ", q2).strip()
    return q2


def _extract_must_terms(query: str) -> list[str]:
    """
    Extract a small set of "must keep" terms from user input, so CASPER boolify
    cannot accidentally drop them.
    """
    q_full = (query or "").strip()
    if not q_full:
        return []

    # Remove parenthetical explanations to avoid pulling in filler words.
    # Keep case markers separately (we extract them from the full text below).
    q = re.sub(r"（[^）]{0,80}）", " ", q_full)
    q = re.sub(r"\([^)]{0,80}\)", " ", q)
    if not q:
        return []

    # Prefer explicit quoted phrases as must-keep (without quotes).
    q_norm = q.replace("“", "\"").replace("”", "\"")
    must: list[str] = []
    for m in re.findall(r"\"([^\"]+)\"", q_norm):
        p = re.sub(r"\s+", " ", (m or "").strip())
        if len(p) >= 2:
            must.append(p)

    # Key legal/court tokens.
    for k in ["最高法院", "刑事", "民事", "不正訊問", "未具結", "相當因果關係"]:
        if k in q and k not in must:
            must.append(k)

    # Extract continuous CJK terms (>=2 chars) with stopword filtering.
    stop = {
        "並且",
        "而且",
        "以及",
        "或者",
        "排除",
        "不要",
        "不含",
        "去掉",
        "剔除",
        "我",
        "我想",
        "我要",
        "希望",
        "目標",
        "最後",
        "請",
        "找",
        "查詢",
        "結果",
        "判例",
        "號判例",
        "我最後的目標是",
    }
    for m in re.findall(r"[\u4e00-\u9fff]{2,}", q):
        if m in stop:
            continue
        if any(x in m for x in ["目標", "希望", "最後", "我要", "我想"]):
            continue
        if m not in must:
            must.append(m)

    # Extract "台上/台抗/台簡" style case markers.
    for m in re.findall(r"\d{1,3}\s*台\s*上\s*\d{1,6}", q_full):
        s = re.sub(r"\s+", "", m)
        if s and s not in must:
            must.append(s)
    for m in re.findall(r"\d{1,3}台上字第?\d{1,6}號?", q_full):
        s = re.sub(r"\s+", "", m)
        if s and s not in must:
            must.append(s)

    # Keep only a small set to avoid over-constraining.
    out: list[str] = []
    seen = set()
    for t in must:
        t2 = (t or "").strip()
        if not t2:
            continue
        if t2 in seen:
            continue
        seen.add(t2)
        out.append(t2)
        if len(out) >= 8:
            break
    return out


def _ensure_terms_in_fjud_query(fjud_query: str, must_terms: list[str]) -> str:
    s = (fjud_query or "").strip()
    if not s:
        return ""
    must_terms = must_terms or []

    # Normalize for contains check
    s_chk = s.replace(" ", "")
    add = []
    for t in must_terms:
        t_chk = (t or "").strip().replace(" ", "")
        if not t_chk:
            continue
        if t_chk in s_chk:
            continue
        # Phrase: spaces -> '+'
        add.append(re.sub(r"\s+", "+", t.strip()))

    if not add:
        return s

    # Append as AND constraints.
    if s.endswith("+"):
        s2 = s + "+".join(add)
    else:
        s2 = s + "+" + "+".join(add)
    s2 = s2.replace("+-", "-")
    s2 = re.sub(r"\+{2,}", "+", s2)
    s2 = re.sub(r"\s+", " ", s2).strip()
    return s2


def _user_wants_exclude(original_query: str, term: str) -> bool:
    q = (original_query or "").strip()
    t = (term or "").strip()
    if not q or not t:
        return False
    # Simple patterns indicating exclusion intent.
    return bool(re.search(rf"(排除|不要|不含|去掉|剔除)\s*{re.escape(t)}", q))


def _fix_unwanted_negatives(fjud_query: str, original_query: str, must_terms: list[str]) -> str:
    """
    If CASPER accidentally negates a term the user clearly asked for, flip it back.
    Example: '-最高法院' when the input includes '最高法院' without exclusion intent.
    """
    s = (fjud_query or "").strip()
    if not s:
        return ""
    for t in (must_terms or []):
        t2 = (t or "").strip()
        if not t2:
            continue
        if t2 not in (original_query or ""):
            continue
        if _user_wants_exclude(original_query, t2):
            continue
        # Replace occurrences like:
        # - start: -TERM -> TERM
        s = re.sub(rf"^-{re.escape(t2)}(?=(\+|$| OR ))", t2, s)
        # - mid: X-TERM -> X+TERM (the '-' would otherwise glue terms)
        s = re.sub(rf"(?<=[\w\u4e00-\u9fff])-{re.escape(t2)}", "+" + t2, s)
        # - common: +-TERM -> +TERM
        s = s.replace("+-" + t2, "+" + t2)
    s = s.replace("+-", "-")
    s = re.sub(r"\+{2,}", "+", s)
    return s.strip("+ ").strip()


def _normalize_or_usage(fjud_query: str, original_query: str) -> str:
    """
    If the user's input has no OR intent, but the generated query uses OR,
    collapse OR to AND (i.e., '+').
    """
    s = (fjud_query or "").strip()
    if not s:
        return ""
    q = (original_query or "")
    or_intent = (" 或 " in q) or ("或者" in q) or bool(re.search(r"\bOR\b|\|", q, flags=re.I))
    if not or_intent and " OR " in s:
        s = s.replace(" OR ", "+")
    s = re.sub(r"\+{2,}", "+", s)
    s = s.replace("+-", "-")
    return s.strip("+ ").strip()


def _collapse_case_markers(fjud_query: str) -> str:
    """
    Collapse token sequences like '76+台上+192' into '76台上192', and remove duplicates.
    """
    s = (fjud_query or "").strip()
    if not s:
        return ""
    s = re.sub(r"\s*OR\s*", " OR ", s, flags=re.I).strip()
    or_parts = [p.strip() for p in s.split(" OR ") if p.strip()]
    out_or = []
    for p in or_parts:
        toks = [t for t in p.split("+") if t]
        collapsed = []
        i = 0
        while i < len(toks):
            a = toks[i].strip()
            b = toks[i + 1].strip() if i + 1 < len(toks) else ""
            c = toks[i + 2].strip() if i + 2 < len(toks) else ""
            if re.fullmatch(r"\d{1,3}", a) and b == "台上" and re.fullmatch(r"\d{1,6}", c):
                collapsed.append(f"{a}台上{c}")
                i += 3
                continue
            collapsed.append(a)
            i += 1
        # De-dup while preserving order
        seen = set()
        final = []
        for t in collapsed:
            if not t:
                continue
            if t in seen:
                continue
            seen.add(t)
            final.append(t)
        out_or.append("+".join(final))
    return " OR ".join(out_or).strip()


def _to_fjud_query(boolean_query: str) -> str:
    """
    Convert a normalized boolean expression with AND/OR/NOT tokens into
    a FJUD-friendly query string:
    - AND => '+'
    - NOT => prefix '-' on the next term when possible
    - OR stays as ' OR ' (FJUD accepts OR keyword)
    """
    s = (boolean_query or "").strip()
    if not s:
        return ""
    s = re.sub(r"\b(and|or|not)\b", lambda m: m.group(1).upper(), s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    toks = s.split(" ")
    out = []
    pending_not = False
    for t in toks:
        u = t.upper()
        if u == "AND":
            out.append("+")
            continue
        if u == "OR":
            out.append("OR")
            continue
        if u == "NOT":
            pending_not = True
            continue

        term = t
        if pending_not:
            # If term already has '-' prefix, keep it.
            term = term if term.startswith("-") else ("-" + term)
            pending_not = False
        out.append(term)

    # Remove leading/trailing '+' and collapse duplicates.
    cleaned = []
    for t in out:
        if t == "+":
            if not cleaned or cleaned[-1] in {"+", "OR"}:
                continue
            cleaned.append(t)
            continue
        if t == "OR":
            if not cleaned or cleaned[-1] in {"+", "OR"}:
                continue
            cleaned.append(t)
            continue
        cleaned.append(t)
    while cleaned and cleaned[-1] in {"+", "OR"}:
        cleaned.pop()

    # Join with compact '+' and spaced OR
    parts = []
    for t in cleaned:
        if t == "+":
            continue
        if t == "OR":
            parts.append(" OR ")
            continue
        if parts and parts[-1] not in {" OR "}:
            # default AND between adjacent terms -> '+'
            parts.append("+")
        parts.append(t)

    # Compact: remove '+' around OR blocks
    q = "".join(parts)
    q = re.sub(r"\+\s*OR\s*\+", " OR ", q)
    q = q.replace("+-", "-")
    q = re.sub(r"\+{2,}", "+", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


def _to_search_input(fjud_query: str) -> str:
    """
    Convert our compact FJUD query string (with + / - / OR) into a query string
    that works well in the site's '全文內容' input.
    Example:
      詐欺+洗錢-未遂 -> 詐欺 AND 洗錢 NOT 未遂
      不正訊問+刑事+最高法院+未具結 -> 不正訊問 AND 刑事 AND 最高法院 AND 未具結
    """
    s = (fjud_query or "").strip()
    if not s:
        return ""
    s = re.sub(r"\s*OR\s*", " OR ", s, flags=re.I).strip()
    parts = [p.strip() for p in s.split(" OR ") if p.strip()]
    out_parts = []
    for p in parts:
        tokens = [t for t in p.split("+") if t]
        if not tokens:
            continue
        seg = []
        for t in tokens:
            tt = t.strip()
            if not tt:
                continue
            # Handle inline negatives like "洗錢-未遂-既遂"
            if tt.startswith("-") and len(tt) > 1:
                seg.append("NOT " + tt[1:])
                continue
            if "-" in tt:
                parts2 = [x for x in tt.split("-") if x != ""]
                if parts2:
                    seg.append(parts2[0])
                    for neg in parts2[1:]:
                        if neg:
                            seg.append("NOT " + neg)
                continue
            seg.append(tt)
        if not seg:
            continue
        # Insert AND between positive terms while keeping NOT prefix inline.
        built = []
        for item in seg:
            if item.startswith("NOT "):
                built.append(item)
                continue
            if built and not built[-1].endswith("AND") and not built[-1].startswith("NOT "):
                built.append("AND")
            built.append(item)
        # Cleanup accidental "X AND NOT Y" (leave as is)
        out_parts.append(" ".join(built).replace("  ", " ").strip())
    return " OR ".join(out_parts).strip()

def boolify(query: str, timeout_sec: int = 60) -> dict:
    q = (query or "").strip()
    if not q:
        return {"success": False, "error": "missing query"}

    must_terms = _extract_must_terms(q)

    # Treat simple keyword inputs as Google-like even without explicit operators.
    if _is_keyword_query(q):
        fjud = _google_to_fjud(q)
        fjud = _ensure_terms_in_fjud_query(fjud, must_terms)
        fjud = _normalize_or_usage(fjud, q)
        fjud = _collapse_case_markers(fjud)
        return {"success": True, "query": q, "boolean_query": fjud, "boolean_query_raw": q, "source": "keyword_like"}

    # If user already uses a google-like query, convert deterministically (do not ask CASPER).
    if _looks_like_google_query(q):
        fjud = _google_to_fjud(q)
        fjud = _ensure_terms_in_fjud_query(fjud, must_terms)
        fjud = _normalize_or_usage(fjud, q)
        fjud = _collapse_case_markers(fjud)
        return {"success": True, "query": q, "boolean_query": fjud, "boolean_query_raw": q, "source": "google_like"}

    # Prefer CASPER conversion, fallback to heuristic.
    cb = _casper_boolify(q, timeout_sec=int(timeout_sec))
    if cb.get("success") and (cb.get("boolean_query") or "").strip():
        # If CASPER already produced google-like query, normalize it; otherwise convert AND/NOT to FJUD style.
        if _looks_like_google_query(cb["boolean_query"]):
            fjud = _google_to_fjud(cb["boolean_query"])
        else:
            fjud = _to_fjud_query(cb["boolean_query"])
        fjud = _ensure_terms_in_fjud_query(fjud or cb["boolean_query"], must_terms)
        fjud = _fix_unwanted_negatives(fjud, q, must_terms)
        fjud = _normalize_or_usage(fjud, q)
        fjud = _collapse_case_markers(fjud)
        return {
            "success": True,
            "query": q,
            "boolean_query": fjud or cb["boolean_query"],
            "boolean_query_raw": cb["boolean_query"],
            "source": cb.get("source", "casper"),
            "route": cb.get("route", ""),
            "model": cb.get("model", ""),
        }

    hb = _heuristic_boolify(q)
    fjud = _ensure_terms_in_fjud_query(_to_fjud_query(hb) or hb, must_terms)
    fjud = _fix_unwanted_negatives(fjud, q, must_terms)
    fjud = _normalize_or_usage(fjud, q)
    fjud = _collapse_case_markers(fjud)
    return {
        "success": True,
        "query": q,
        "boolean_query": fjud,
        "boolean_query_raw": hb,
        "source": "heuristic",
        "warning": cb.get("error", ""),
    }


def _run_skill(skill: str, task: str, timeout_sec: int = 120, route_key: str = "") -> dict:
    import urllib.request
    import urllib.error

    try:
        from api.routing.service_registry import get_service_url as _gsurl
        _tools_def = _gsurl("tools_api")
    except Exception:
        _tools_def = "http://127.0.0.1:5003"
    tools_api = os.environ.get("MAGI_TOOLS_API", _tools_def).rstrip("/")
    payload = {
        "skill": skill,
        "task": task,
        "timeout_sec": int(timeout_sec),
        # Keep sub-skill execution deterministic; auto_repair can mutate args and return non-JSON help output.
        "auto_repair": False,
        "rollback_on_fail": True,
        "auto_install_deps": False,
        "route_key": route_key,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(tools_api + "/skills/run", data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=max(5, int(timeout_sec) + 15)) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw or "{}")
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        return {"success": False, "error": f"http {e.code}", "detail": raw[:800]}
    except Exception as e:
        return {"success": False, "error": str(e)[:240]}


def _skill_json_task(command: str, payload: dict) -> str:
    return f"{str(command or '').strip()}{json.dumps(payload or {}, ensure_ascii=False, separators=(',', ':'))}"


def _parse_skill_output(run_result: dict) -> dict:
    if not isinstance(run_result, dict) or not run_result.get("success"):
        return {"success": False, "error": (run_result.get("error") if isinstance(run_result, dict) else "run failed")}
    raw = (run_result.get("output") or "").strip()
    if not raw:
        return {"success": False, "error": "empty skill output"}
    try:
        obj = json.loads(raw)
    except Exception as e:
        return {"success": False, "error": f"skill output json parse failed: {e}", "raw": raw[:800]}
    return obj if isinstance(obj, dict) else {"success": False, "error": "skill output is not a json object"}


def search_archive(query: str, max_results: int = DEFAULT_MAX_RESULTS, max_chars: int = DEFAULT_MAX_CHARS, headless: bool = True, timeout_sec: int = 120) -> dict:
    q = (query or "").strip()
    if not q:
        return {"success": False, "error": "missing query"}

    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    key = hashlib.sha256((q + "|" + now).encode("utf-8")).hexdigest()[:12]
    run_id = f"{now}_{key}"
    archive_dir = os.path.join(ARCHIVE_ROOT, run_id)
    os.makedirs(archive_dir, exist_ok=True)

    b = boolify(q, timeout_sec=min(60, int(timeout_sec)))
    boolean_query = (b.get("boolean_query") or "").strip()
    if not boolean_query:
        return {"success": False, "error": "boolify produced empty query"}
    # Prefer the compact query format (+ / - / OR). It works well with the site and avoids odd AND parsing.
    search_input = boolean_query

    rk = "judicial-flow-search-archive:" + key

    def _is_admin_query(s: str) -> bool:
        s = (s or "").strip()
        if not s:
            return False
        # Heuristic: treat as admin law query if user mentions admin courts or "行政".
        if "行政" in s:
            return True
        if "高等行政法院" in s or "最高行政法院" in s:
            return True
        return False

    def _court_policy(s: str) -> dict:
        """
        Decide which courts to keep. Default: Supreme Court only.
        For admin cases: admin courts only.
        """
        if _is_admin_query(s):
            return {
                "mode": "admin_only",
                "allowed_prefixes": [
                    "最高行政法院",
                    "臺北高等行政法院",
                    "臺中高等行政法院",
                    "高雄高等行政法院",
                    "臺南高等行政法院",
                ],
                # Use UI court filter labels (must match the select option text).
                "courts_select_labels": [
                    "最高行政法院(含改制前行政法院)",
                    "臺北高等行政法院",
                    "臺中高等行政法院",
                    "高雄高等行政法院",
                    "臺南高等行政法院",
                ],
                "allow_fallback": False,
            }
        # For non-admin queries: Supreme Court only (do not downshift).
        return {"mode": "supreme_only", "allowed_prefixes": ["最高法院"], "courts_select_labels": ["最高法院"], "allow_fallback": False}

    def _filter_by_court(items: list[dict], policy: dict) -> list[dict]:
        allowed = policy.get("allowed_prefixes") or []
        if not allowed:
            return items
        out = []
        for it in items:
            title = (it.get("title") or "").strip()
            if not title:
                continue
            if any(title.startswith(pfx) for pfx in allowed):
                out.append(it)
        return out

    policy = _court_policy(q)

    # If we're already selecting a specific court, do not require the court name to appear in full-text keywords.
    for pfx in (policy.get("allowed_prefixes") or []):
        pfx = (pfx or "").strip()
        if not pfx:
            continue
        search_input = search_input.replace("+" + pfx, "").replace(pfx + "+", "").replace(pfx, "")
    search_input = re.sub(r"\+{2,}", "+", search_input).strip("+ ").strip()
    if not search_input:
        # If we removed everything (e.g., user only wrote "最高法院"), keep the original boolean query.
        search_input = boolean_query

    # 1) Search
    # Fetch a larger pool first, then filter by court before fetching full texts.
    first_pass_mult = max(8, int(DEFAULT_FIRST_PASS_MULT))
    pool_cap = max(200, int(DEFAULT_SEARCH_POOL_CAP))
    pool = max(30, int(max_results) * first_pass_mult)
    pool = min(pool, pool_cap)
    search_payload = {
        "keywords": search_input,
        "max_results": int(pool),
        "headless": bool(headless),
        "timeout_sec": min(90, int(timeout_sec)),
        "courts": policy.get("courts_select_labels") or [],
    }
    sr = _run_skill("judicial-web-search", _skill_json_task("search", search_payload), timeout_sec=int(timeout_sec) + 60, route_key=rk)
    sp = _parse_skill_output(sr)
    if not sp.get("success"):
        return {
            "success": False,
            "error": "judicial search failed",
            "detail": sp.get("error") or sp.get("raw") or "",
            "boolean_query": boolean_query,
            "search_input": search_input,
        }

    results_path = (sp.get("results_path") or "").strip()
    results = sp.get("results") or []
    if results_path and os.path.exists(results_path):
        try:
            with open(results_path, "r", encoding="utf-8") as f:
                obj = json.load(f) or {}
            if isinstance(obj, dict) and isinstance(obj.get("results"), list):
                results = obj.get("results") or results
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 890, exc_info=True)

    def _extract_content_terms(original_query: str, policy: dict) -> list[str]:
        terms = _extract_must_terms(original_query)
        drop = set(policy.get("allowed_prefixes") or [])
        drop |= {"刑事", "民事", "裁定", "判決", "號", "字"}
        out = []
        for t in terms:
            if t in drop:
                continue
            if any(t.startswith(x) for x in drop if x):
                continue
            if t not in out:
                out.append(t)
        return out

    def _pick_anchor(terms: list[str], original_query: str) -> str:
        # Prefer case marker like 76台上192
        m = re.search(r"\d{1,3}\s*台\s*上\s*\d{1,6}", original_query)
        if m:
            return re.sub(r"\s+", "", m.group(0))
        m2 = re.search(r"\d{1,3}台上字第?\d{1,6}號?", original_query)
        if m2:
            return re.sub(r"\s+", "", m2.group(0)).replace("號", "")
        # Prefer "未具結" if present (common evidence term)
        if "未具結" in terms:
            return "未具結"
        return terms[0] if terms else ""

    # 1.5) Court filtering (defense in depth; should already be handled by UI filter)
    # Apply policy on the full pool first, then take the desired page size.
    filtered_all = _filter_by_court(results, policy)
    used_fallback = False
    allow_fallback = bool(policy.get("allow_fallback", False))
    if not filtered_all and allow_fallback:
        filtered_all = results
        used_fallback = True
    filtered = filtered_all[: max(0, int(max_results))]

    # 1.6) Relaxed mode for strict policies:
    # If nothing matched (common when the full-text doesn't contain the court name or extra terms),
    # run a second pass using an anchor term, then filter by other terms in the fetched full text.
    relaxed = {"enabled": False}
    content_terms = _extract_content_terms(q, policy)
    if not filtered and content_terms and not allow_fallback:
        anchor = _pick_anchor(content_terms, q)
        rest = [t for t in content_terms if t != anchor]
        if anchor:
            relaxed["enabled"] = True
            relaxed["anchor"] = anchor
            relaxed["required_terms"] = rest

            # Second-pass search: anchor only, still within court selection.
            search_payload2 = dict(search_payload)
            search_payload2["keywords"] = anchor
            relaxed_mult = max(20, int(DEFAULT_RELAXED_MULT))
            relaxed_cap = max(400, int(DEFAULT_RELAXED_POOL_CAP))
            search_payload2["max_results"] = min(relaxed_cap, max(100, int(max_results) * relaxed_mult))
            sr2 = _run_skill("judicial-web-search", _skill_json_task("search", search_payload2), timeout_sec=int(timeout_sec) + 60, route_key=rk + ":relaxed")
            sp2 = _parse_skill_output(sr2)
            if sp2.get("success"):
                rp2 = (sp2.get("results_path") or "").strip()
                cand = sp2.get("results") or []
                if rp2 and os.path.exists(rp2):
                    try:
                        with open(rp2, "r", encoding="utf-8") as f:
                            obj2 = json.load(f) or {}
                        if isinstance(obj2, dict) and isinstance(obj2.get("results"), list):
                            cand = obj2.get("results") or cand
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 960, exc_info=True)
                # Filter candidates by court title prefix as an extra guard.
                cand = _filter_by_court(cand, policy)
                # We'll fetch text and keep those containing all required terms.
                filtered = []
                filtered_all = []
                for it in cand:
                    if len(filtered) >= int(max_results):
                        break
                    title = (it.get("title") or "").strip()
                    url = (it.get("url") or "").strip()
                    if not title or not url:
                        continue
                    fetch_payload = {"url": url, "headless": bool(headless), "timeout_sec": 60, "max_chars": int(max_chars)}
                    fr = _run_skill("judicial-web-search", _skill_json_task("fetch_text", fetch_payload), timeout_sec=int(timeout_sec) + 60, route_key=rk + ":relaxed")
                    fp = _parse_skill_output(fr)
                    if not fp.get("success"):
                        continue
                    text_path = (fp.get("text_path") or "").strip()
                    raw_text = ""
                    if text_path and os.path.exists(text_path):
                        try:
                            with open(text_path, "r", encoding="utf-8", errors="replace") as f:
                                raw_text = f.read()
                        except Exception:
                            raw_text = ""
                    if not raw_text:
                        continue
                    if any((t not in raw_text) for t in rest):
                        continue
                    filtered_all.append(it)
                    filtered.append(it)

    # Copy results json into archive
    archived_results_path = ""
    if results_path and os.path.exists(results_path):
        try:
            archived_results_path = os.path.join(archive_dir, "results.json")
            shutil.copy2(results_path, archived_results_path)
        except Exception:
            archived_results_path = ""

    # 2) Fetch text for each result
    fetched = []
    for idx, it in enumerate(filtered, start=1):
        title = (it.get("title") or "").strip()
        url = (it.get("url") or "").strip()
        if not title or not url:
            continue

        fetch_payload = {"url": url, "headless": bool(headless), "timeout_sec": 60, "max_chars": int(max_chars)}
        fr = _run_skill("judicial-web-search", _skill_json_task("fetch_text", fetch_payload), timeout_sec=int(timeout_sec) + 60, route_key=rk)
        fp = _parse_skill_output(fr)
        row: dict[str, Any] = {"idx": idx, "title": title, "url": url, "success": bool(fp.get("success"))}
        if not fp.get("success"):
            row["error"] = fp.get("error") or fp.get("raw") or "fetch_text failed"
            fetched.append(row)
            continue

        src_path = (fp.get("text_path") or "").strip()
        archive_txt = ""
        if src_path and os.path.exists(src_path):
            try:
                archive_txt = os.path.join(archive_dir, f"{idx:03d}_{_safe_filename(title, 64)}.txt")
                shutil.copy2(src_path, archive_txt)
            except Exception:
                archive_txt = ""

        row.update(
            {
                "text_path": src_path,
                "archived_text_path": archive_txt,
                "text_chars": fp.get("text_chars", 0),
                "text_preview": fp.get("text_preview", ""),
            }
        )
        fetched.append(row)

    # 3) Write manifest + report
    manifest = {
        "success": True,
        "run_id": run_id,
        "timestamp": datetime.now().isoformat(),
        "query": q,
        "boolean_query": boolean_query,
        "search_input": search_input,
        "court_policy": policy,
        "court_filter_fallback": used_fallback,
        "relaxed": relaxed,
        "boolify_source": b.get("source", ""),
        "boolify_warning": b.get("warning", ""),
        "search": {
            "count_preview": sp.get("count", 0),
            "results_path": results_path,
            "archived_results_path": archived_results_path,
            "results_len": len(results),
            "filtered_len": len(filtered),
            "filtered_total_len": len(filtered_all),
        },
        "items": fetched,
    }

    manifest_path = os.path.join(archive_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    lines = []
    lines.append(f"查詢時間：{manifest['timestamp']}")
    lines.append(f"原始語句：{q}")
    lines.append(f"布林查詢：{boolean_query}")
    if manifest.get("boolify_source"):
        lines.append(f"布林來源：{manifest['boolify_source']}")
    if manifest.get("boolify_warning"):
        lines.append(f"布林警告：{manifest['boolify_warning']}")
    if manifest.get("court_policy"):
        lines.append(f"法院偏好：{manifest['court_policy'].get('mode','')}")
        lines.append(f"允許法院：{', '.join(manifest['court_policy'].get('allowed_prefixes') or [])}")
        if used_fallback:
            lines.append("法院過濾：本次過濾後為 0 筆，已改用未過濾清單（fallback）")
    lines.append("")
    lines.append(f"結果筆數（本次抓取）：{len([x for x in fetched if x.get('success')])}/{len(fetched)}")
    lines.append(f"歸檔資料夾：{archive_dir}")
    lines.append("")
    for row in fetched:
        title = row.get("title", "")
        url = row.get("url", "")
        ok = row.get("success", False)
        ap = row.get("archived_text_path") or ""
        if ok:
            lines.append(f"- [OK] {title}")
            lines.append(f"  URL: {url}")
            if ap:
                lines.append(f"  檔案: {ap}")
        else:
            lines.append(f"- [FAIL] {title}")
            lines.append(f"  URL: {url}")
            lines.append(f"  錯誤: {row.get('error','')}")
    report_path = os.path.join(archive_dir, "report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).strip() + "\n")

    try:
        preview_limit = int(os.environ.get("JUDICIAL_ARCHIVE_PREVIEW_LIMIT", "10") or "10")
    except Exception:
        preview_limit = 10
    preview_limit = max(1, min(preview_limit, 50))

    preview_items = []
    for row in fetched[:preview_limit]:
        preview_items.append(
            {
                "title": row.get("title", ""),
                "url": row.get("url", ""),
                "archived_text_path": row.get("archived_text_path", ""),
                "success": bool(row.get("success")),
            }
        )

    return {
        "success": True,
        "query": q,
        "boolean_query": boolean_query,
        "archive_dir": archive_dir,
        "manifest_path": manifest_path,
        "report_path": report_path,
        "count": len(fetched),
        "ok": len([x for x in fetched if x.get("success")]),
        "preview_limit": preview_limit,
        "items_preview": preview_items,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="judicial-flow-search-archive skill")
    ap.add_argument("--task", default="help", help="task text")
    args = ap.parse_args()
    task = (args.task or "").strip()

    if task in {"help", "summary", "list"}:
        return _ok({"success": True, "commands": ["help", "self_test", "boolify {..json..}", "search_archive {..json..}", "判決搜尋 ..."]})

    if task == "self_test":
        smoke_max = int(os.environ.get("JUDICIAL_ARCHIVE_SMOKE_MAX_RESULTS", "8") or "8")
        r = search_archive("詐欺 並且 洗錢 排除 未遂", max_results=smoke_max, max_chars=8000, headless=True, timeout_sec=140)
        ok = bool(r.get("success") and r.get("ok", 0) >= 1 and (r.get("report_path") or ""))
        return _ok({"success": bool(ok), "details": r})

    if task.startswith("boolify") or task.startswith("布林化"):
        key = "boolify" if task.startswith("boolify") else "布林化"
        payload = _load_jsonish(task[len(key) :].strip())
        q = (payload.get("query") or payload.get("value") or "").strip()
        t = int(payload.get("timeout_sec", 60) or 60)
        return _ok(boolify(q, timeout_sec=t))

    if task.startswith("search_archive") or task.startswith("判決搜尋"):
        key = "search_archive" if task.startswith("search_archive") else "判決搜尋"
        payload = _load_jsonish(task[len(key) :].strip())
        q = (payload.get("query") or payload.get("keywords") or payload.get("value") or "").strip()
        max_results = int(payload.get("max_results", DEFAULT_MAX_RESULTS) or DEFAULT_MAX_RESULTS)
        max_chars = int(payload.get("max_chars", DEFAULT_MAX_CHARS) or DEFAULT_MAX_CHARS)
        headless = bool(payload.get("headless", True))
        t = int(payload.get("timeout_sec", 120) or 120)
        return _ok(search_archive(q, max_results=max_results, max_chars=max_chars, headless=headless, timeout_sec=t))

    return _ok({"success": False, "error": f"unknown task: {task}"})


if __name__ == "__main__":
    raise SystemExit(main())
