#!/usr/bin/env python3
"""
reprocess_insights.py — 重新處理 legal_insights 全部條目
==========================================
用新的「逐字擷取」prompt 重新處理所有 legal_insights 條目。

策略（按優先順序取得原文）：
1. 已有 raw_text → 直接用
2. court_reference 是 JID 格式 → 從司法院 API 抓全文
3. court_reference 是自然語言 → 解析成 JID 後從 API 抓
4. 嘗試從 cached .txt 檔搜尋
5. 都找不到 → 跳過，列入報告

Usage:
    python reprocess_insights.py                    # 全部重處理
    python reprocess_insights.py --dry-run          # 模擬模式
    python reprocess_insights.py --limit 10         # 限制筆數
    python reprocess_insights.py --only-with-raw    # 只處理有 raw_text 的
    python reprocess_insights.py --start-id 300     # 從指定 ID 開始
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import ssl
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib import request as _urlrequest, error as _urlerror

MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", str(Path.home() / "Desktop/MAGI")))
sys.path.insert(0, str(MAGI_ROOT))

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(MAGI_ROOT / ".env")
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("reprocess-insights")

# ── 路徑 ──────────────────────────────────────────────────────────────
NORM_ROOT = Path.home() / ".cache/judgment_collector/judicial_api/normalized"
CODEX_TIMEOUT = int(os.environ.get("OSC_INSIGHT_SUMMARY_TIMEOUT_SEC", "120"))

# ── 司法院 API ────────────────────────────────────────────────────────
JDG_API_BASE = os.environ.get("JUDICIAL_API_BASE", "https://data.judicial.gov.tw/jdg/api").rstrip("/")
JDG_USER = os.environ.get("MAGI_JUDICIAL_API_USER") or os.environ.get("JDG_API_USER") or ""
JDG_PASS = os.environ.get("MAGI_JUDICIAL_API_PASS") or os.environ.get("JDG_API_PASSWORD") or ""

# 法院名稱 → JID code 反向映射
_COURT_NAME_TO_CODE = {
    "最高法院": "TPSM",
    "最高行政法院": "TPHA",
    "懲戒法院": "TPHP",
    "臺灣高等法院": "TPHM",
    "臺北高等行政法院": "TPBA",
    "臺中高等行政法院": "TCDA",
    "高雄高等行政法院": "KSHA",
    # 地方法院
    "臺灣臺北地方法院": "TPDM",
    "臺灣新北地方法院": "PCDM",
    "臺灣士林地方法院": "SLDM",
    "臺灣桃園地方法院": "TYDM",
    "臺灣新竹地方法院": "SCDM",
    "臺灣苗栗地方法院": "MLDM",
    "臺灣臺中地方法院": "TCDM",
    "臺灣南投地方法院": "NTDM",
    "臺灣彰化地方法院": "CHDM",
    "臺灣雲林地方法院": "ULDM",
    "臺灣嘉義地方法院": "CYDM",
    "臺灣臺南地方法院": "TNDM",
    "臺灣高雄地方法院": "KSDM",
    "臺灣屏東地方法院": "PTDM",
    "臺灣花蓮地方法院": "HLDM",
    "臺灣臺東地方法院": "TTDM",
    "臺灣宜蘭地方法院": "ILDM",
    "臺灣基隆地方法院": "KLDM",
    "臺灣澎湖地方法院": "PHDM",
    "臺灣橋頭地方法院": "CTDM",
    # 高等法院分院
    "臺灣高等法院臺中分院": "TCHM",
    "臺灣高等法院臺南分院": "TNHM",
    "臺灣高等法院高雄分院": "KSHM",
    "臺灣高等法院花蓮分院": "HLHM",
    # 少家法院
    "臺灣高雄少年及家事法院": "KSJD",
}

# ── Prompt（與 weekend_resummary.py 一致）──────────────────────────────
PROMPT_TEMPLATE = (
    "你是一位精確的法律助理。你的唯一任務是從一份判決書全文中，"
    "「逐字擷取」可供其他案件參考的「實務見解」或「法律原則」。\n\n"
    "案由：{case_reason}\n\n"
    "【嚴格規則】\n"
    "1. 【只要擷取】：找到判決書中「法院認為...」、「法院審酌...」、「按...」、"
    "「...應解為...」、「查...」等段落，找出最具法律原則價值的一到三個段落。\n"
    "2. 【逐字複製】：你「必須」逐字(verbatim)複製找到的段落。\n"
    "3. 【禁止】：嚴禁「摘要」、「改寫」、「精煉」或「加入你自己的文字」。"
    "你不是在寫摘要，你是在「複製」關鍵原文。\n"
    "4. 【禁止】：禁止使用「頁 1」、「頁 2」或「一、二、三」的編號格式。\n"
    "5. 【禁止】：禁止輸出案件概要、事實摘要、判決結果等敘述。只要法律見解原文。\n\n"
    "【高品質範例】\n"
    "根據最高法院109年度台上大字第3826號刑事大法庭裁定，毒品危害防制條例第20條第3項"
    "關於「3年後再犯」的定義，並不因施用毒品者於3年內是否有其他犯罪紀錄而受到影響。"
    "此裁定的立法真諦在於，鑑於施用毒品者具有「病患性犯人」的特質，應優先提供治療"
    "與戒癮協助。\n\n"
    "【格式化輸出】\n"
    "## 實務見解\n（從判決中逐字擷取的法院見解原文，一到三個關鍵段落）\n\n"
    "## 適用法條\n（列出本判決適用的法條）\n\n"
    "【注意事項】\n"
    "- 若判決中找不到有法律原則價值的見解（如純事實認定），回覆「本判決無可擷取之實務見解」\n"
    "- 若判決內文與案由明顯不符，回覆「案由不符，無法擷取」\n\n"
    "判決全文：\n{full_text}"
)

STRUCTURE_HEADERS = ["實務見解", "法院見解", "適用法條", "法院認為", "應解為"]
REJECT_KEYWORDS = ["無法擷取", "無可擷取", "案由不符"]


# ── DB ──────────────────────────────────────────────────────────────
def _get_db():
    import pymysql
    host = os.environ.get("OSC_DB_HOST") or os.environ.get("MAGI_DB_HOST") or "localhost"
    user = os.environ.get("OSC_DB_USER") or os.environ.get("MAGI_DB_USER") or ""
    pwd = os.environ.get("OSC_DB_PASSWORD") or os.environ.get("MAGI_DB_PASSWORD") or ""
    dbname = os.environ.get("OSC_DB_NAME") or os.environ.get("MAGI_DB_NAME") or ""
    port = int(os.environ.get("OSC_DB_PORT") or os.environ.get("MAGI_DB_PORT") or "3306")
    return pymysql.connect(host=host, user=user, password=pwd, database=dbname,
                           port=port, charset="utf8mb4", autocommit=True)


# ── 司法院 API 取全文 ────────────────────────────────────────────────
_ssl_ctx_cache: dict[str, Any] = {}


def _build_ssl_context() -> ssl.SSLContext:
    cached = _ssl_ctx_cache.get("ctx")
    if cached:
        return cached
    import certifi
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    _ssl_ctx_cache["ctx"] = ctx
    return ctx


def _jdg_post(path: str, payload: dict, timeout_sec: int = 25) -> Any:
    url = JDG_API_BASE + "/" + path.lstrip("/")
    data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    req = _urlrequest.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    ctx = _build_ssl_context()
    try:
        with _urlrequest.urlopen(req, timeout=max(5, timeout_sec), context=ctx) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw or "{}")
    except Exception as e:
        return {"error": str(e)[:240]}


_api_token_cache: dict[str, str] = {}


def _get_api_token() -> str:
    cached = _api_token_cache.get("token")
    if cached:
        return cached
    if not JDG_USER or not JDG_PASS:
        return ""
    auth = _jdg_post("Auth", {"user": JDG_USER, "password": JDG_PASS}, timeout_sec=20)
    token = auth.get("Token") or ""
    if token:
        _api_token_cache["token"] = token
    return token


def _fetch_fulltext_by_jid(jid: str) -> Optional[str]:
    """Fetch judgment full text from judicial API by JID."""
    token = _get_api_token()
    if not token:
        return None
    resp = _jdg_post("JDoc", {"token": token, "j": jid}, timeout_sec=30)
    if isinstance(resp, dict) and "error" in resp:
        # Token might have expired, retry once
        _api_token_cache.clear()
        token = _get_api_token()
        if not token:
            return None
        resp = _jdg_post("JDoc", {"token": token, "j": jid}, timeout_sec=30)
    if not isinstance(resp, dict):
        return None
    jfullx = resp.get("JFULLX") if isinstance(resp.get("JFULLX"), dict) else {}
    full_text = str(jfullx.get("JFULLCONTENT") or "").strip()
    if len(full_text) > 200:
        return full_text
    return None


def _is_jid_format(ref: str) -> bool:
    """Check if court_reference is in JID format like 'TPSM,92,台上,4003,20030101,1'."""
    return bool(re.match(r'^[A-Z]{3,5},', ref or ""))


def _parse_court_reference(ref: str) -> dict:
    """Parse '最高法院 92 年度 台上 字第 4003 號刑事判決' into components."""
    ref = (ref or "").strip()
    m = re.match(
        r'(.+?)\s+(\d+)\s*年度?\s*(.+?)\s*字第?\s*(\d+)\s*號(.*)$',
        ref
    )
    if m:
        return {
            "court": m.group(1).strip(),
            "year": m.group(2).strip(),
            "case_code": m.group(3).strip(),
            "number": m.group(4).strip(),
            "suffix": m.group(5).strip(),
        }
    return {}


def _court_ref_to_jid(ref: str) -> Optional[str]:
    """Convert natural language court reference to a partial JID for API lookup."""
    parsed = _parse_court_reference(ref)
    if not parsed:
        return None
    court_code = _COURT_NAME_TO_CODE.get(parsed["court"])
    if not court_code:
        # Try fuzzy matching
        for name, code in _COURT_NAME_TO_CODE.items():
            if name in parsed["court"] or parsed["court"] in name:
                court_code = code
                break
    if not court_code:
        return None
    # JID format: CODE,YEAR,CASE_CODE,NUMBER
    # We don't have date, but JDoc might accept partial JID
    return f"{court_code},{parsed['year']},{parsed['case_code']},{parsed['number']}"


def _fetch_fulltext_for_ref(court_ref: str) -> Optional[str]:
    """Try to fetch full text from judicial API for a court_reference."""
    if _is_jid_format(court_ref):
        return _fetch_fulltext_by_jid(court_ref)
    # Convert to JID and try
    jid = _court_ref_to_jid(court_ref)
    if jid:
        text = _fetch_fulltext_by_jid(jid)
        if text:
            return text
    return None


# ── 從 cached .txt 檔案搜尋原文 ──────────────────────────────────────
def _build_txt_index() -> dict[str, Path]:
    index = {}
    if not NORM_ROOT.exists():
        return index
    for txt_path in NORM_ROOT.glob("*/*.txt"):
        index[txt_path.stem] = txt_path
    return index


def _find_text_in_cache(court_ref: str, txt_index: dict[str, Path]) -> Optional[str]:
    """Try to find judgment text in cached .txt files by matching JID in slug."""
    if _is_jid_format(court_ref):
        # JID like "ILDM,113,訴,762,20251030,1" → slug contains "ILDM_113_訴_762"
        parts = court_ref.split(",")
        if len(parts) >= 4:
            # Match court + year + code + number in slug
            slug_key = f"{parts[0]}_{parts[1]}_{parts[2]}_{parts[3]}"
            for slug, path in txt_index.items():
                if slug_key in slug:
                    try:
                        full = path.read_text("utf-8", errors="replace")
                        if len(full) > 500:
                            return full
                    except Exception:
                        continue
    # Natural language refs are too unreliable for cache matching — skip
    return None


# ── Codex 摘要（走 openclaw_codex_bridge，跟 weekend_resummary 同路徑）──
RESUMMARY_SESSION_ID = "reprocess-insights-batch"
SUMMARY_TIMEOUT_SEC = 300

_shutdown_requested = False


def _clear_codex_cooldown() -> None:
    try:
        from skills.bridge.llm_direct import load_runtime_state, save_runtime_state
        state = load_runtime_state()
        if int(state.get("cooldown_until_ts", 0)) > 0:
            state["cooldown_until_ts"] = 0
            state["cooldown_reason"] = ""
            state["consecutive_failures"] = 0
            save_runtime_state(state)
            logger.info("Cleared Codex cooldown state")
    except Exception:
        pass


def _summarize_with_codex(raw_text: str, case_reason: str) -> Optional[str]:
    """Use Codex OAuth (openclaw_codex_bridge) to generate verbatim extraction."""
    try:
        from skills.bridge.llm_direct import feature_enabled, run_prompt

        if not feature_enabled("summary"):
            logger.warning("    Codex summary feature disabled")
            return None

        text = raw_text[:150000] if len(raw_text) > 150000 else raw_text
        prompt = PROMPT_TEMPLATE.format(
            case_reason=case_reason or "未知",
            full_text=text,
        )

        result = run_prompt(
            feature="summary",
            prompt=prompt,
            timeout_sec=SUMMARY_TIMEOUT_SEC,
            session_id=RESUMMARY_SESSION_ID,
        )

        # Handle cooldown
        error = str(result.get("error", ""))
        if "cooldown_active" in error:
            wait_sec = int(result.get("cooldown_remaining_sec", 0)) or 60
            wait_sec = min(wait_sec + 10, 900)
            logger.info("    Codex cooldown, waiting %ds...", wait_sec)
            deadline = time.time() + wait_sec
            while time.time() < deadline and not _shutdown_requested:
                time.sleep(min(10, deadline - time.time()))
            if _shutdown_requested:
                return None
            _clear_codex_cooldown()
            result = run_prompt(
                feature="summary",
                prompt=prompt,
                timeout_sec=SUMMARY_TIMEOUT_SEC,
                session_id=RESUMMARY_SESSION_ID,
            )

        if not result.get("success"):
            logger.warning("    Codex failed: error=%s duration=%sms model=%s rc=%s",
                           str(result.get("error", "unknown"))[:200],
                           result.get("duration_ms", "?"),
                           result.get("model", "?"),
                           result.get("returncode", "?"))
            # Debug: dump stdout tail to see response structure
            stdout_tail = result.get("stdout_tail", "")
            if stdout_tail:
                logger.info("    stdout_tail: %s", stdout_tail[:500])
            return None

        summary = str(result.get("text") or "").strip()
        if not summary or len(summary) < 20:
            logger.warning("    Codex returned empty/short text (%d chars): [%s], rc=%s",
                           len(summary), summary[:100], result.get("returncode"))
            return None

        if any(kw in summary for kw in REJECT_KEYWORDS):
            logger.info("    Codex rejected (no extractable insight)")
            return summary

        if not any(h in summary for h in STRUCTURE_HEADERS):
            logger.warning("    Summary missing structure headers, accepting anyway")

        return summary

    except Exception as e:
        logger.error("    Codex error: %s", e)
        return None


def main():
    parser = argparse.ArgumentParser(description="Reprocess legal_insights with new verbatim extraction prompt")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, don't update DB")
    parser.add_argument("--limit", type=int, default=0, help="Max entries to process (0=all)")
    parser.add_argument("--only-with-raw", action="store_true", help="Only process rows that have raw_text")
    parser.add_argument("--start-id", type=int, default=0, help="Start from this ID")
    parser.add_argument("--delay", type=float, default=5.0, help="Delay between Codex calls (sec)")
    parser.add_argument("--skip-api", action="store_true", help="Skip judicial API fetch (cache + raw_text only)")
    args = parser.parse_args()

    conn = _get_db()
    import pymysql
    cur = conn.cursor(pymysql.cursors.DictCursor)

    # Fetch all rows
    where_clause = "WHERE id >= %s" if args.start_id else "WHERE 1=1"
    params = (args.start_id,) if args.start_id else ()
    cur.execute(f"""
        SELECT id, case_number, document_name, court_reference, court_type,
               insight_type, insight_text, case_reason, source_file, raw_text,
               is_degraded
        FROM legal_insights
        {where_clause}
        ORDER BY id ASC
    """, params)
    all_rows = cur.fetchall()
    logger.info("Total rows: %d", len(all_rows))

    # Build cache index
    logger.info("Building text file index from %s ...", NORM_ROOT)
    txt_index = _build_txt_index()
    logger.info("Indexed %d cached text files", len(txt_index))

    # Stats
    processed = 0
    updated = 0
    skipped_no_text = 0
    failed = 0
    source_counts = {"raw_text": 0, "api": 0, "cache": 0}

    rows = all_rows
    if args.only_with_raw:
        rows = [r for r in rows if r.get("raw_text") and len(r["raw_text"]) > 100]
        logger.info("Filtered to %d rows with raw_text", len(rows))

    if args.limit > 0:
        rows = rows[:args.limit]

    total = len(rows)
    logger.info("Processing %d rows (dry_run=%s, skip_api=%s)", total, args.dry_run, args.skip_api)

    for i, row in enumerate(rows):
        rid = row["id"]
        court_ref = row.get("court_reference") or ""
        case_reason = row.get("case_reason") or ""
        raw_text = (row.get("raw_text") or "").strip()

        logger.info("[%d/%d] id=%d ref=%s", i + 1, total, rid, court_ref[:60])

        # Step 1: Find raw text (priority: raw_text > API > cache)
        source = "raw_text"
        if not raw_text or len(raw_text) < 100:
            # Try judicial API
            if not args.skip_api and court_ref:
                api_text = _fetch_fulltext_for_ref(court_ref)
                if api_text:
                    raw_text = api_text
                    source = "api"
                    logger.info("    Fetched from API (%d chars)", len(raw_text))

            # Try cache
            if not raw_text or len(raw_text) < 100:
                cached = _find_text_in_cache(court_ref, txt_index)
                if cached:
                    raw_text = cached
                    source = "cache"
                    logger.info("    Found in cache (%d chars)", len(raw_text))

            # No text found
            if not raw_text or len(raw_text) < 100:
                logger.warning("    No raw text available, skipping")
                skipped_no_text += 1
                continue

        source_counts[source] = source_counts.get(source, 0) + 1

        # Step 2: Call Codex
        if args.dry_run:
            logger.info("    [DRY RUN] Would re-process (%s, %d chars)", source, len(raw_text))
            processed += 1
            continue

        summary = _summarize_with_codex(raw_text, case_reason)
        if not summary:
            logger.warning("    Failed to get summary")
            failed += 1
            time.sleep(args.delay)
            continue

        # Step 3: Update DB
        try:
            if source in ("api", "cache") and (not row.get("raw_text") or len(row["raw_text"]) < 100):
                cur.execute(
                    "UPDATE legal_insights SET insight_text = %s, raw_text = %s, is_degraded = 0 WHERE id = %s",
                    (summary, raw_text, rid),
                )
            else:
                cur.execute(
                    "UPDATE legal_insights SET insight_text = %s, is_degraded = 0 WHERE id = %s",
                    (summary, rid),
                )
            updated += 1
            logger.info("    Updated (%d chars, source=%s)", len(summary), source)
        except Exception as e:
            logger.error("    DB update failed: %s", e)
            failed += 1

        processed += 1
        time.sleep(args.delay)

        # Progress report every 20
        if processed % 20 == 0:
            logger.info(
                "=== Progress: %d/%d processed, %d updated, %d skipped, %d failed | sources: %s ===",
                processed, total, updated, skipped_no_text, failed, source_counts,
            )

    # Final report
    report = {
        "total_rows": len(all_rows),
        "processed": processed,
        "updated": updated,
        "skipped_no_text": skipped_no_text,
        "failed": failed,
        "sources": source_counts,
        "dry_run": args.dry_run,
    }
    logger.info("=== Final Report ===")
    logger.info(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
