#!/usr/bin/env python3
"""
cleanup_judgments_leaks.py
==========================
清理判決實務見解庫（judgments.json + court_judgments DB）的 AI preamble 漏風。

Problem
-------
部分 summary 欄位只是 LLM 對指令的回應（「好的，作為 MAGI 系統的 AI 助理...」），
而非實際從判決全文擷取的法院見解。這是 prompt 設計或 LLM refuse-to-answer 的殘留。

What this script does
---------------------
1. judgments.json：
   - 備份原檔 → .bak.<ts>
   - 為每筆 entry:
     * 偵測 preamble 漏風
     * 呼叫 _strip_preamble() 移除指令回應段落
     * 若淨化後無實質內容 → 從清單中刪除（純 preamble entry）
     * 以 URL 匹配 court_judgments DB，回填 full_text 欄位
     * 補 data_url（FJUD data.aspx 可瀏覽連結）
   - 原子寫回 .json

2. court_judgments DB：
   - 94 筆有 preamble 漏風的 summary
   - 淨化後若 < 30 字 → UPDATE summary=NULL（讓 resummary_all cron 下次重新摘要）
   - 否則 UPDATE summary=<cleaned>

Usage
-----
    python3 scripts/ops/cleanup_judgments_leaks.py --dry-run
    python3 scripts/ops/cleanup_judgments_leaks.py --apply
    python3 scripts/ops/cleanup_judgments_leaks.py --apply --enrich-json-fulltext
    python3 scripts/ops/cleanup_judgments_leaks.py --apply --scope json
    python3 scripts/ops/cleanup_judgments_leaks.py --apply --scope db
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_MAGI_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_MAGI_ROOT))

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_MAGI_ROOT / ".env")
except Exception:
    pass

from api.osc.insight_filters import (  # noqa: E402
    is_non_extractable_legal_insight,
    non_extractable_legal_insight_sql_where,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("cleanup-judgments")

JSON_PATH = _MAGI_ROOT / "skills" / "judgment-collector" / "judgments.json"

# ── 漏風 pattern（與 _sanitize_summary 對齊）──
_LEAK_SIGNATURES = [
    "作為 MAGI", "作為MAGI", "我將會", "我已理解", "AI 助理", "AI助理",
    "嚴格依照", "逐字擷取", "好的，作為", "請您提供", "請直接輸出校正",
    "請提供完整的判決書", "我將為您服務", "將為您服務",
    "您提供的文本片段", "我將嚴格依照", "我將依循以下步驟",
    "請提供您需要我摘要的判決書全文", "請您現在貼上判決書",
    "請將判決書貼於此", "判決書貼於下方", "原始資料未提供全文文字",
    "已存原始 JSON", "輸出內容：嚴格依照", "語言規範：全程使用",
    "而非創設新的法律見解", "而非闡述某個具有高度爭議性",
    "若需擷取量刑考量因素",
]

_PREAMBLE_LINE_PATTERNS = [
    r"^好的，.*",
    r"^.*作為\s*MAGI\s*系統的.*AI\s*助理.*$",
    r"^.*作為MAGI系統的AI助理.*$",
    r"^.*我已理解您的(?:指示|要求).*$",
    r"^.*我將(?:會|嚴格)?依照.*$",
    r"^.*以台灣律師事務所慣用.*$",
    r"^.*逐字擷取.*$",
    r"^.*嚴格依照.*原則.*$",
    r"^.*請您提供(?:需要分析的)?判決書(?:全文)?.*$",
    r"^.*請直接輸出校正.*$",
    r"^.*請提供完整的判決書.*$",
    r"^.*我將為您服務.*$",
    r"^\*\*請.*判決書.*\*\*$",
    r"^\*\*.*步驟執行.*\*\*$",
    r"^\s*\d+\.\s*\*\*(?:篩選|擷取|輸出)[：:]?\*\*.*$",
    r"^.*您提供的文本片段.*$",
    r"^.*鎖定.*本院按.*標記.*$",
    r"^.*若要嚴格執行.*擷取.*$",
]

_COMPILED_PREAMBLE = [re.compile(p, re.MULTILINE) for p in _PREAMBLE_LINE_PATTERNS]

# ── 有實質內容的 markers ──
_CONTENT_MARKERS = [
    r"##\s*實務見解",
    r"##\s*引用裁判",
    r"##\s*適用法條",
    r"本院[按認審查判]",
    r"惟查",
    r"經查",
    r"次按",
    r"按[，,]",
    r"\*\*【備註】\*\*",
]
_CONTENT_RE = re.compile("|".join(_CONTENT_MARKERS))


def has_leak(s: str) -> bool:
    return any(m in (s or "") for m in _LEAK_SIGNATURES) or is_non_extractable_legal_insight(s)


def has_real_content(s: str) -> bool:
    """Real content = content markers + substantive prose (not just empty templates)."""
    s = (s or "").strip()
    if not s:
        return False
    if not _CONTENT_RE.search(s):
        return False

    # Reject empty templates: "## 實務見解\n\n## 引用裁判\n（...）"
    # Check: does any non-marker line have substantial legal prose (>= 40 chars of CJK content)?
    lines = s.split("\n")
    for line in lines:
        stripped = line.strip().lstrip("#").lstrip("（(").rstrip("）)").strip()
        if not stripped:
            continue
        # Skip pure headers
        if stripped in ("實務見解", "引用裁判", "適用法條", "【備註】", "備註"):
            continue
        # Skip placeholder template text (common patterns)
        if any(t in stripped for t in (
            "從判決中逐字擷取",
            "列出判決內文中實際出現",
            "列出適用法條",
            "禁止自行編造",
            "請將判決書",
            "請您將判決書",
            "請您提供",
            "請提供",
            "我將立即為您",
            "為您服務",
            "若內文無引用",
            "輸出內容",
            "語言規範",
            "而非創設新的法律見解",
            "若需擷取量刑考量因素",
        )):
            continue
        # Count CJK chars
        cjk = sum(1 for ch in stripped if "\u4e00" <= ch <= "\u9fff")
        if cjk >= 40:
            return True
    return False


def strip_preamble(summary: str) -> str:
    """Strip AI preamble lines. Keep real content below."""
    s = str(summary or "").strip()
    if not s:
        return s

    # Phase 1: If 實務見解 / 本院 section exists, cut at first occurrence
    opinion_m = re.search(r"(?:##\s*實務見解|本院[按認審查判]|次按|經查|惟查)", s)
    if opinion_m and opinion_m.start() > 50:
        s = s[opinion_m.start():]

    # Phase 2: Line-by-line strip preamble patterns
    lines = s.split("\n")
    cleaned: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned.append(line)
            continue
        # Drop if matches any preamble pattern
        if any(pat.match(stripped) for pat in _COMPILED_PREAMBLE):
            continue
        cleaned.append(line)

    out = "\n".join(cleaned).strip()
    # Collapse triple-blank lines
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


# ── JSON URL ↔ DB jid 轉換 ──
def url_to_jid(url: str) -> str:
    m = re.search(r"/JDocFile/([^/]+)/([^/]+)/", url or "")
    if not m:
        return ""
    court = m.group(1)
    rest = urllib.parse.unquote(m.group(2))
    return f"{court},{rest}"


def url_to_jid_prefix(url: str) -> str:
    jid = url_to_jid(url)
    parts = jid.split(",")
    if len(parts) < 4:
        return ""
    return ",".join(parts[:4])  # court,year,type,num


def jid_to_data_url(jid: str) -> str:
    """Build a browsable FJUD data.aspx URL from jid.
    jid: 'TPSM,114,台上,1856,20250625,1'
    → https://judgment.judicial.gov.tw/FJUD/data.aspx?ty=JD&id=TPSM%2c114%2c%e5%8f%b0%e4%b8%8a%2c1856%2c20250625%2c1&ot=in
    """
    if not jid:
        return ""
    encoded = urllib.parse.quote(jid, safe="")
    return f"https://judgment.judicial.gov.tw/FJUD/data.aspx?ty=JD&id={encoded}&ot=in"


# ── DB access ──
def _get_db_conn():
    try:
        import mysql.connector
    except ImportError:
        logger.error("mysql-connector-python not installed")
        return None
    try:
        conn = mysql.connector.connect(
            host=os.environ.get("OSC_DB_HOST", "127.0.0.1"),
            port=int(os.environ.get("OSC_DB_PORT", "3306")),
            user=os.environ.get("OSC_DB_USER", "casper_service"),
            password=os.environ.get("OSC_DB_PASSWORD", ""),
            database=os.environ.get("OSC_DB_NAME", "law_firm_data"),
            use_pure=True,
            charset="utf8mb4",
        )
        return conn
    except Exception as e:
        logger.error("DB connect failed: %s", e)
        return None


# ── JSON cleanup ──
def cleanup_json(apply: bool, conn, *, enrich_fulltext: bool = False) -> Dict[str, Any]:
    if not JSON_PATH.exists():
        return {"error": "judgments.json not found"}

    with open(JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    stats = {
        "total": len(data),
        "clean_kept": 0,
        "cleaned_kept": 0,
        "pure_preamble_dropped": 0,
        "enriched_with_fulltext": 0,
        "enriched_with_data_url": 0,
    }

    out: List[Dict[str, Any]] = []
    db_cur = conn.cursor(dictionary=True) if conn else None

    for e in data:
        url = e.get("url", "") or ""
        summary = str(e.get("summary", "") or "")
        leaked = has_leak(summary)

        if is_non_extractable_legal_insight(summary):
            stats["pure_preamble_dropped"] += 1
            continue

        if not leaked:
            stats["clean_kept"] += 1
            new_entry = dict(e)
        else:
            cleaned = strip_preamble(summary)
            if len(cleaned) < 30 or not has_real_content(cleaned):
                # Pure preamble — drop
                stats["pure_preamble_dropped"] += 1
                continue
            stats["cleaned_kept"] += 1
            new_entry = dict(e)
            new_entry["summary"] = cleaned
            new_entry["_was_cleaned"] = True

        # Enriching full_text is useful for private local repair runs, but unsafe for
        # public repo artifacts.  Keep it opt-in so cleanup does not leak case text.
        if enrich_fulltext and db_cur and url:
            jid = url_to_jid(url)
            db_cur.execute(
                "SELECT jid, full_text FROM court_judgments WHERE jid=%s LIMIT 1",
                (jid,),
            )
            row = db_cur.fetchone()
            if not row:
                prefix = url_to_jid_prefix(url)
                if prefix:
                    db_cur.execute(
                        "SELECT jid, full_text FROM court_judgments WHERE jid LIKE %s ORDER BY jid DESC LIMIT 1",
                        (prefix + ",%",),
                    )
                    row = db_cur.fetchone()
            if row and row.get("full_text"):
                if "full_text" not in new_entry or not new_entry.get("full_text"):
                    new_entry["full_text"] = row["full_text"]
                    stats["enriched_with_fulltext"] += 1
                if "jid" not in new_entry:
                    new_entry["jid"] = row["jid"]
                if "data_url" not in new_entry:
                    new_entry["data_url"] = jid_to_data_url(row["jid"])
                    stats["enriched_with_data_url"] += 1

        out.append(new_entry)

    if db_cur:
        db_cur.close()

    stats["final_count"] = len(out)

    if apply:
        ts = time.strftime("%Y%m%d_%H%M%S")
        backup = JSON_PATH.with_suffix(f".json.bak.{ts}")
        backup.write_text(JSON_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        tmp = JSON_PATH.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(out, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(JSON_PATH)
        logger.info("JSON written (backup at %s)", backup)

    return stats


# ── DB cleanup ──
def cleanup_db(apply: bool, conn) -> Dict[str, Any]:
    stats = {
        "scanned": 0,
        "cleaned_updated": 0,
        "pure_preamble_nulled": 0,
        "archive_summary_nulled": 0,
        "legal_insights_deleted": 0,
        "unchanged": 0,
    }
    if not conn:
        return {"error": "no db connection"}

    cur = conn.cursor(dictionary=True)

    normalized_legal = (
        "REPLACE(REPLACE(REPLACE(REPLACE(CONCAT_WS('', "
        "`court_reference`, `insight_text`, `document_name`, `case_reason`, `raw_text`"
        "), ' ', ''), '\\n', ''), '\\r', ''), '\\t', '')"
    )
    li_where, li_params = non_extractable_legal_insight_sql_where(normalized_legal)
    cur.execute(f"SELECT COUNT(*) AS c FROM legal_insights WHERE {li_where}", li_params)
    stats["legal_insights_deleted"] = int((cur.fetchone() or {}).get("c") or 0)

    ors = " OR ".join(["summary LIKE %s"] * len(_LEAK_SIGNATURES))
    params = [f"%{m}%" for m in _LEAK_SIGNATURES]
    cur.execute(
        f"SELECT jid, summary FROM court_judgments WHERE {ors}",
        tuple(params),
    )
    rows = cur.fetchall()
    stats["scanned"] = len(rows)

    updates: List[Tuple[Optional[str], str]] = []

    for r in rows:
        jid = r["jid"]
        summary = r["summary"] or ""
        cleaned = strip_preamble(summary)
        if is_non_extractable_legal_insight(summary) or len(cleaned) < 30 or not has_real_content(cleaned):
            updates.append((None, jid))  # NULL out for resummary
            stats["pure_preamble_nulled"] += 1
        elif cleaned != summary.strip():
            updates.append((cleaned, jid))
            stats["cleaned_updated"] += 1
        else:
            stats["unchanged"] += 1

    cur.close()

    if apply and (updates or stats["legal_insights_deleted"]):
        up_cur = conn.cursor()
        if stats["legal_insights_deleted"]:
            up_cur.execute(f"DELETE FROM legal_insights WHERE {li_where}", li_params)
        for new_val, jid in updates:
            up_cur.execute(
                "UPDATE court_judgments SET summary=%s WHERE jid=%s",
                (new_val, jid),
            )
        conn.commit()
        up_cur.close()
        logger.info("DB updates applied: %d rows", len(updates))

    archive_updates: List[int] = []
    cur = conn.cursor(dictionary=True)
    archive_ors = " OR ".join(["summary_text LIKE %s"] * len(_LEAK_SIGNATURES))
    archive_params = [f"%{m}%" for m in _LEAK_SIGNATURES]
    cur.execute(
        f"SELECT id, summary_text FROM judgment_archive WHERE summary_text IS NOT NULL AND ({archive_ors})",
        tuple(archive_params),
    )
    for r in cur.fetchall() or []:
        summary = str(r.get("summary_text") or "")
        cleaned = strip_preamble(summary)
        if is_non_extractable_legal_insight(summary) or len(cleaned) < 30 or not has_real_content(cleaned):
            archive_updates.append(int(r.get("id") or 0))
    cur.close()
    archive_updates = [x for x in archive_updates if x > 0]
    stats["archive_summary_nulled"] = len(archive_updates)
    if apply and archive_updates:
        up_cur = conn.cursor()
        placeholders = ",".join(["%s"] * len(archive_updates))
        up_cur.execute(
            f"UPDATE judgment_archive SET summary_text=NULL, is_degraded=1 WHERE id IN ({placeholders})",
            tuple(archive_updates),
        )
        conn.commit()
        up_cur.close()
        logger.info("Archive summaries nulled: %d", len(archive_updates))

    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="實際寫入變更（否則 dry-run）")
    ap.add_argument("--dry-run", action="store_true", help="只列印統計，不寫入")
    ap.add_argument("--scope", choices=["json", "db", "both"], default="both")
    ap.add_argument(
        "--enrich-json-fulltext",
        action="store_true",
        help="將 DB 全文回填到 judgments.json（僅限私用本機修復；公開版不要使用）",
    )
    args = ap.parse_args()

    apply = args.apply and not args.dry_run
    mode = "APPLY" if apply else "DRY-RUN"
    logger.info("Mode: %s, scope=%s", mode, args.scope)

    conn = _get_db_conn()

    result: Dict[str, Any] = {"mode": mode, "scope": args.scope}

    if args.scope in ("json", "both"):
        result["json"] = cleanup_json(apply, conn, enrich_fulltext=bool(args.enrich_json_fulltext))
    if args.scope in ("db", "both"):
        result["db"] = cleanup_db(apply, conn)

    if conn:
        conn.close()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
