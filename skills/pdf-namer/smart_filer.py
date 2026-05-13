# -*- coding: utf-8 -*-
"""
pdf-namer / smart_filer.py
===========================
Smart Filing Engine — 掃描檔自動歸檔

Flow:
  01_掃描檔放置區/*.pdf
    → analyze (AI 命名)
      → match_to_case (比對案件)
        → 高信心 → 移入案件子資料夾
        → 低信心 → 移入 03_程式歸檔失敗區
      → 命名失敗 → 移入 04_程式無法命名區

After filing, generates a report for LINE/DC notification.
Supports human correction: user tells CASPER to re-file → learns from mistake.
"""

import json
import os
import re
import shutil
import logging
import time
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import ensure_orch_on_sys_path, get_orch_dir
from api.case_path_mapper import (
    default_scan_roots,
    default_synology_share_roots,
    preferred_case_roots,
    preferred_synology_share_roots,
)

logger = logging.getLogger("pdf-namer-filer")

# ── Paths ──
_SYNOLOGY_ROOTS = preferred_synology_share_roots(include_closed=False)
_FALLBACK_SYNOLOGY_ROOTS = default_synology_share_roots(include_closed=False)
SYNOLOGY_ROOT = _SYNOLOGY_ROOTS[0] if _SYNOLOGY_ROOTS else (_FALLBACK_SYNOLOGY_ROOTS[0] if _FALLBACK_SYNOLOGY_ROOTS else str(Path.home() / "Library" / "CloudStorage" / "SynologyDrive-homes"))
_CASE_ROOTS = preferred_case_roots(include_closed=False)
CASE_ROOT = _CASE_ROOTS[0] if _CASE_ROOTS else os.path.join(SYNOLOGY_ROOT, "01_案件")
_SCAN_ROOTS = default_scan_roots()
SCAN_ROOT = os.path.dirname(_SCAN_ROOTS[0]) if _SCAN_ROOTS else os.path.join(SYNOLOGY_ROOT, "02_掃描檔案")

SCAN_INBOX   = os.path.join(SCAN_ROOT, "01_掃描檔放置區")
SCAN_STAGED  = os.path.join(SCAN_ROOT, "02_自動歸檔區")
SCAN_FAIL    = os.path.join(SCAN_ROOT, "03_程式歸檔失敗區")
SCAN_NONAME  = os.path.join(SCAN_ROOT, "04_程式無法命名區")

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(SKILL_DIR, "_case_index.json")
FILING_LOG_PATH = os.path.join(SKILL_DIR, "_filing_log.json")

# Filing confidence threshold — anything below goes to failure zone
# Load threshold from nightly training auto-adjustment (closed feedback loop)
_THRESHOLD_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_threshold_state.json")
FILING_CONFIDENCE_THRESHOLD = 0.82  # Default; overridden by nightly training state
try:
    import json as _json_t
    _ts = _json_t.loads(open(_THRESHOLD_STATE_PATH, encoding="utf-8").read() or "{}")
    if _ts.get("threshold"):
        FILING_CONFIDENCE_THRESHOLD = float(_ts["threshold"])
except Exception:
    pass

OSC_ORCH_PATH = f"{_MAGI_ROOT}/skills/osc-orchestrator/action.py"
OSC_ORCH_PY = os.environ.get("MAGI_SKILL_PYTHON", f"{_MAGI_ROOT}/venv/bin/python3")

CODE_DIR = str(get_orch_dir())

def _eventlog(event: str, *, ok: Optional[bool] = None, payload: Optional[dict] = None, tags: Optional[dict] = None) -> None:
    """
    Best-effort：將歸檔/命名/比對結果寫入向量記憶，便於日後對話查詢/追溯。
    """
    try:
        ensure_orch_on_sys_path()
        import magi_eventlog  # type: ignore
        magi_eventlog.remember_event(event, ok=ok, payload=payload or {}, tags=tags or {}, source="pdf_namer")
    except Exception:
        return

# ── Doc Type → Subfolder Mapping ──
# Uses the NAME part (not the number prefix) since numbering varies
DOC_TYPE_TO_SUBFOLDER = {
    # pdf-namer doc_type → subfolder name keyword (matches XX_ prefix stripped)
    # ── 法院裁判 ──
    "判決":     "判決書",
    "支付命令": "法院通知或程序裁定",
    "裁定":     "法院通知或程序裁定",
    # ── 法院通知 ──
    "庭通知書": "法院通知或程序裁定",
    "法院通知": "法院通知或程序裁定",
    "法院_通知": "法院通知或程序裁定",
    "法院_通知(支付命令)": "法院通知或程序裁定",
    "法院_傳票": "法院通知或程序裁定",
    "函文":     "法院通知或程序裁定",
    "通知書":   "法院通知或程序裁定",
    "開庭通知": "法院通知或程序裁定",
    "期日通知": "法院通知或程序裁定",
    "傳票":     "法院通知或程序裁定",
    # ── 檢察機關 ──
    "起訴書":   "法院通知或程序裁定",
    "不起訴處分書": "法院通知或程序裁定",
    "聲請簡易判決處刑書": "法院通知或程序裁定",
    # ── 書狀 ──
    "書狀_我方": "我方歷次書狀",
    "書狀_對造": "對方歷次書狀",
    "對造_書狀": "對方歷次書狀",
    "對造書狀": "對方歷次書狀",
    "答辯狀":   "對方歷次書狀",
    "陳報狀":   "對方歷次書狀",
    "聲請書":   "我方歷次書狀",
    "抗告狀":   "我方歷次書狀",
    "上訴狀":   "我方歷次書狀",
    "債清_書狀": "我方歷次書狀",
    # ── 筆錄 ──
    "筆錄":       "筆錄",
    "訊問筆錄":   "筆錄",
    "調查筆錄":   "筆錄",
    "準備程序筆錄": "筆錄",
    "審判筆錄":   "筆錄",
    "勘驗筆錄":   "筆錄",
    # ── 證據資料 ──
    "證據":       "證據資料",
    "扣押物品目錄表": "證據資料",
    "扣押物品收據":   "證據資料",
    "贓證物品清單":   "證據資料",
    "驗傷診斷書":     "證據資料",
    "相驗屍體證明書": "證據資料",
    # ── 令狀 ──
    "搜索票":   "法院通知或程序裁定",
    "拘票":     "法院通知或程序裁定",
    "押票":     "法院通知或程序裁定",
    "提票":     "法院通知或程序裁定",
    "通緝書":   "法院通知或程序裁定",
    # ── 委任 / 閱卷 / 法扶 ──
    "委任狀":   "委任",
    "委任相關": "回執",
    "閱卷":     "閱卷資料",
    "無償委任資料": "無償委任資料",
    "法扶表單": "法扶資料",
    "法扶回報": "結案資料",
    # ── 其他 ──
    "收據":     "回執",
    "信件":     "回執",
    "契約":     "回執",
}


# ════════════════════════════════════════════════════════════════════════════
#  CASE INDEX
# ════════════════════════════════════════════════════════════════════════════

def build_case_index(force_rebuild: bool = False) -> List[Dict]:
    """
    Scan 01_案件/ and build a searchable index of all cases.
    Each entry: {case_type, domain, folder_name, parties, case_id, reason, path, subfolders}
    """
    if not force_rebuild and os.path.exists(INDEX_PATH):
        age = time.time() - os.path.getmtime(INDEX_PATH)
        if age < 3600:  # Cache valid for 1 hour
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                return json.load(f)

    index = []
    if not os.path.isdir(CASE_ROOT):
        logger.warning(f"案件根目錄不存在: {CASE_ROOT}")
        return index

    for case_type in os.listdir(CASE_ROOT):
        type_path = os.path.join(CASE_ROOT, case_type)
        if not os.path.isdir(type_path) or case_type.startswith("."):
            continue

        for domain in os.listdir(type_path):
            domain_path = os.path.join(type_path, domain)
            if not os.path.isdir(domain_path) or domain.startswith("."):
                continue

            for case_folder in os.listdir(domain_path):
                case_path = os.path.join(domain_path, case_folder)
                if not os.path.isdir(case_path) or case_folder.startswith("."):
                    continue

                parsed = _parse_case_folder(case_folder)

                # List subfolders
                subfolders = []
                for sf in os.listdir(case_path):
                    sf_path = os.path.join(case_path, sf)
                    if os.path.isdir(sf_path) and not sf.startswith("."):
                        subfolders.append(sf)

                entry = {
                    "case_type": case_type,
                    "domain": domain,
                    "folder_name": case_folder,
                    "path": case_path,
                    "parties": parsed["parties"],
                    "case_id": parsed["case_id"],
                    "year": parsed["year"],
                    "seq": parsed["seq"],
                    "stage": parsed["stage"],
                    "reason": parsed["reason"],
                    "subfolders": subfolders,
                }
                index.append(entry)

    # Save cache
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    logger.info(f"✅ 案件索引建立完成: {len(index)} 筆")
    return index


def _parse_case_folder(name: str) -> Dict:
    """
    Parse case folder name pattern: YYYY-NNNN-當事人-審級-案由
    Examples:
      2025-0047-[當事人D]-消費者債務清理-更生
      2025-0015-林正雄-一審-毒品危害防制條例
      2026-0006-劉沅璋-一審-搶奪
    """
    result = {"parties": [], "case_id": "", "year": "", "seq": "", "stage": "", "reason": ""}

    m = re.match(r'^(\d{4})-(\d{4})-(.+)$', name)
    if not m:
        result["parties"] = [name]
        return result

    result["year"] = m.group(1)
    result["seq"] = m.group(2)
    result["case_id"] = f"{m.group(1)}-{m.group(2)}"
    rest = m.group(3)

    # Split remaining parts: 當事人-審級-案由
    parts = rest.split("-")
    if len(parts) >= 1:
        # First part is always the party name (may contain non-Chinese chars)
        result["parties"] = [parts[0].strip()]
    if len(parts) >= 2:
        result["stage"] = parts[1].strip()
    if len(parts) >= 3:
        result["reason"] = "-".join(parts[2:]).strip()

    return result


# ════════════════════════════════════════════════════════════════════════════
#  MATCHING ENGINE
# ════════════════════════════════════════════════════════════════════════════

def match_to_case(
    text: str,
    filename: str,
    doc_type: str = "",
    analysis_result: Dict = None,
    case_index: List[Dict] = None,
) -> Dict:
    """
    Match a document to a case folder + subfolder.
    
    Returns:
        {
            "matched": bool,
            "case_path": str,
            "subfolder": str,
            "full_dest": str,
            "confidence": float,
            "match_method": str,
            "case_info": dict,
        }
    """
    if case_index is None:
        case_index = build_case_index()

    if not case_index:
        return {"matched": False, "confidence": 0, "reason": "案件索引為空"}

    result = analysis_result or {}

    # Extract searchable fields from analysis
    parties_found = _extract_parties_from_text(text, case_index)
    case_numbers = _extract_case_numbers(text)

    candidates = []

    # ── Strategy 1: Case number exact match (highest confidence) ──
    for case_num in case_numbers:
        for c in case_index:
            # Match against folder seq number
            if case_num in text and any(p in text for p in c["parties"]):
                candidates.append((c, 0.95, "案號+當事人"))

    # ── Strategy 2: Party name match ──
    for party, matched_cases in parties_found.items():
        if len(matched_cases) == 1:
            # Unique party → single case → high confidence
            candidates.append((matched_cases[0], 0.92, f"當事人唯一匹配({party})"))
        elif len(matched_cases) > 1:
            # Multiple cases for same party → try to disambiguate
            best = _disambiguate(matched_cases, text, doc_type, result)
            if best:
                candidates.append((best, 0.85, f"當事人+篩選({party})"))

    # ── Strategy 3: Filename hints ──
    for c in case_index:
        for party in c["parties"]:
            if party in filename and len(party) >= 2:
                candidates.append((c, 0.88, f"檔名含當事人({party})"))

    # ── Strategy 3b: Analysis result parties (from Vision/OCR naming) ──
    analysis_parties = result.get("parties") or []
    if isinstance(analysis_parties, list):
        for ap in analysis_parties:
            if not ap or len(ap) < 2:
                continue
            for c in case_index:
                for cp in c["parties"]:
                    if ap == cp or cp in ap or ap in cp:
                        candidates.append((c, 0.90, f"命名分析當事人({ap})"))

    # ── Strategy 4: RAG History match ──
    try:
        from rag_feedback import rag_engine
        if text and len(text) > 20:
            rag_results = rag_engine.query(text[:1000], n_results=1)
            if rag_results:
                score, meta = rag_results[0]
                rel_path = meta.get("relative_path", "")
                for c in case_index:
                    if c["folder_name"] in rel_path:
                        candidates.append((c, min(0.92, score + 0.3), f"RAG歷史學習(相似度{score:.2f})"))
                        break
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 337, exc_info=True)

    if not candidates:
        return {"matched": False, "confidence": 0, "reason": "無法比對到任何案件"}

    # Pick best candidate
    candidates.sort(key=lambda x: x[1], reverse=True)
    best_case, confidence, method = candidates[0]

    # Determine subfolder
    subfolder = _find_subfolder(best_case, doc_type)

    if not subfolder:
        return {
            "matched": True,
            "confidence": confidence * 0.7,  # Lower confidence without subfolder
            "case_path": best_case["path"],
            "subfolder": "",
            "full_dest": best_case["path"],
            "match_method": method,
            "case_info": best_case,
            "reason": "找到案件但無法確定子資料夾",
        }

    full_dest = os.path.join(best_case["path"], subfolder)

    return {
        "matched": True,
        "confidence": confidence,
        "case_path": best_case["path"],
        "subfolder": subfolder,
        "full_dest": full_dest,
        "match_method": method,
        "case_info": {
            "folder_name": best_case["folder_name"],
            "case_type": best_case["case_type"],
            "domain": best_case["domain"],
            "parties": best_case["parties"],
        },
    }


def _extract_parties_from_text(text: str, case_index: List[Dict]) -> Dict[str, List[Dict]]:
    """Find which party names from our case index appear in the document text.

    Handles simplified/traditional Chinese mismatch from OCR (e.g. 陳晓菁 vs 陳曉菁).
    """
    # Prepare traditional-converted text for fallback matching
    text_tc = ""
    try:
        import opencc
        _s2t = opencc.OpenCC("s2t")
        text_tc = _s2t.convert(text)
    except Exception:
        text_tc = text

    found = {}
    for c in case_index:
        for party in c["parties"]:
            if len(party) < 2:
                continue
            if party in text or party in text_tc:
                if party not in found:
                    found[party] = []
                found[party].append(c)
    return found


def _extract_case_numbers(text: str) -> List[str]:
    """Extract court case numbers from text (e.g., 113年度訴字第123號)."""
    patterns = [
        r'\d{2,3}\s*年度?\s*\S{1,6}字\s*第?\s*\d+\s*號',
        r'\d{2,3}\s*年\s*\S+\s*字第\s*\d+\s*號',
    ]
    numbers = []
    for p in patterns:
        for m in re.finditer(p, text):
            numbers.append(m.group())
    return numbers


def _disambiguate(cases: List[Dict], text: str, doc_type: str, analysis: Dict) -> Optional[Dict]:
    """When multiple cases match the same party, try to pick the best one."""
    scores = []
    for c in cases:
        score = 0
        # Prefer cases whose reason appears in text
        if c["reason"] and c["reason"] in text:
            score += 3
        # Prefer more recent cases
        try:
            year = int(c["year"])
            score += (year - 2024) * 0.5
        except (ValueError, TypeError):
            pass
        # Prefer matching domain keywords in text
        domain_keywords = {
            "刑事": ["被告", "公訴", "檢察", "刑事"],
            "民事": ["原告", "被告", "民事"],
            "消費者債務清理": ["更生", "清算", "債務", "消債"],
            "行政": ["行政", "訴願"],
        }
        for domain, kws in domain_keywords.items():
            if c["domain"] == domain and any(kw in text for kw in kws):
                score += 2
        scores.append((c, score))

    scores.sort(key=lambda x: x[1], reverse=True)

    if scores and scores[0][1] > 0:
        # Only return if clear winner
        if len(scores) < 2 or scores[0][1] > scores[1][1]:
            return scores[0][0]
    return None


def _find_subfolder(case: Dict, doc_type: str) -> str:
    """
    Find the actual subfolder name in the case using doc_type mapping.
    Priority: DB archive_destination_type → hardcoded DOC_TYPE_TO_SUBFOLDER.
    """
    target_keyword = ""

    # Tier 1: Look up archive_destination_type from MariaDB doc_rules
    if doc_type:
        try:
            from training_loader import get_template_for_doc_type
            rule = get_template_for_doc_type(doc_type)
            if rule and rule.get("archive_destination_type"):
                target_keyword = rule["archive_destination_type"]
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 468, exc_info=True)

    # Tier 2: Fallback to hardcoded mapping
    if not target_keyword:
        target_keyword = DOC_TYPE_TO_SUBFOLDER.get(doc_type, "")

    if not target_keyword:
        return ""

    for sf in case.get("subfolders", []):
        # Strip number prefix (e.g., "09_法院通知或程序裁定" → "法院通知或程序裁定")
        clean = re.sub(r'^\d+_', '', sf)
        if target_keyword in clean or clean in target_keyword:
            return sf
    return ""


# ════════════════════════════════════════════════════════════════════════════
#  FILING ENGINE
# ════════════════════════════════════════════════════════════════════════════

def _unique_target_path(path: str) -> str:
    """Return a non-conflicting file path by appending timestamp suffix when needed."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    for _ in range(50):
        suffix = datetime.now().strftime("%H%M%S_%f")
        candidate = f"{base}_{suffix}{ext}"
        if not os.path.exists(candidate):
            return candidate
        time.sleep(0.001)
    # Last fallback
    return f"{base}_{datetime.now().strftime('%H%M%S_%f')}{ext}"


def _process_single_pdf(
    fname: str,
    *,
    dry_run: bool,
    case_index: List[Dict],
    task_analyze_fn,
    extract_text_fn,
) -> Tuple[str, Dict]:
    src_path = os.path.join(SCAN_INBOX, fname)
    logger.info(f"  Processing: {fname}")
    _eventlog("pdf_filing:analyze:start", ok=None, payload={"file": fname, "src_path": src_path, "dry_run": bool(dry_run)}, tags={"file": fname})

    try:
        # Step 1: Analyze and name
        analysis_raw = task_analyze_fn(src_path)
        analysis = json.loads(analysis_raw)

        if "error" in analysis or not analysis.get("suggested_filename"):
            record = {
                "original": fname,
                "error": analysis.get("error", "無法產生建議檔名"),
            }
            _eventlog("pdf_filing:analyze:done", ok=False, payload={"file": fname, "error": record["error"]}, tags={"file": fname})
            if not dry_run:
                _safe_move(src_path, os.path.join(SCAN_NONAME, fname))
            return "unnamed", record

        suggested = analysis["suggested_filename"]
        doc_type = analysis.get("doc_type", "")
        confidence_name = analysis.get("confidence", 0.5)
        if analysis.get("requires_stamp_verification") and (not analysis.get("stamp_verified")):
            record = {
                "original": fname,
                "new_name": suggested,
                "doc_type": doc_type,
                "confidence": round(float(confidence_name or 0), 3),
                "reason": "收章戳日期未驗證，已暫停自動歸檔",
            }
            _eventlog(
                "pdf_filing:failed",
                ok=False,
                payload={
                    "original": fname,
                    "new_name": suggested,
                    "reason": record["reason"],
                    "confidence": record["confidence"],
                    "doc_type": doc_type,
                    "date_method": analysis.get("date_method"),
                },
                tags={"file": fname, "doc_type": doc_type},
            )
            if not dry_run:
                dest_fail = _unique_target_path(os.path.join(SCAN_FAIL, suggested or fname))
                shutil.move(src_path, dest_fail)
                record["status"] = "blocked_no_stamp_verification"
                record["new_name"] = os.path.basename(dest_fail)
            else:
                record["status"] = "preview_blocked_no_stamp_verification"
            return "failed", record
        _eventlog(
            "pdf_filing:analyze:done",
            ok=True,
            payload={
                "file": fname,
                "suggested_filename": suggested,
                "doc_type": doc_type,
                "date": analysis.get("date"),
                "date_method": analysis.get("date_method"),
                "confidence": confidence_name,
            },
            tags={"file": fname, "doc_type": doc_type},
        )

        # Step 2: Extract text for matching
        text, _ = extract_text_fn(src_path)

        # Step 3: Match to case (use suggested name for better party matching)
        match = match_to_case(
            text=text,
            filename=suggested or fname,
            doc_type=doc_type,
            analysis_result=analysis,
            case_index=case_index,
        )

        if match.get("matched") and match.get("confidence", 0) >= FILING_CONFIDENCE_THRESHOLD:
            # ✅ High confidence → file it
            dest_dir = match["full_dest"]
            dest_path = os.path.join(dest_dir, suggested)

            record = {
                "original": fname,
                "new_name": suggested,
                "doc_type": doc_type,
                "destination": dest_dir,
                "case": match.get("case_info", {}).get("folder_name", ""),
                "subfolder": match.get("subfolder", ""),
                "confidence": round(match["confidence"], 3),
                "method": match.get("match_method", ""),
            }

            if not dry_run:
                if os.path.isdir(dest_dir):
                    final_dest = _unique_target_path(dest_path)
                    shutil.move(src_path, final_dest)
                    final_name = os.path.basename(final_dest)
                    if final_name == suggested:
                        record["status"] = "filed"
                    else:
                        record["status"] = "filed_alt"
                        record["new_name"] = final_name
                    _run_bookmarker(final_dest)
                    _best_effort_sync_osc_todos(final_dest, match, analysis)
                else:
                    record["status"] = "dest_missing"
                    _safe_move(src_path, os.path.join(SCAN_FAIL, fname))
                    _eventlog(
                        "pdf_filing:failed",
                        ok=False,
                        payload={
                            "original": fname,
                            "new_name": record.get("new_name"),
                            "reason": "目標資料夾不存在",
                            "confidence": record.get("confidence"),
                            "status": record.get("status"),
                            "doc_type": doc_type,
                        },
                        tags={"file": fname, "doc_type": doc_type},
                    )
                    return "failed", record
            else:
                record["status"] = "preview"

            _eventlog(
                "pdf_filing:filed",
                ok=True,
                payload={
                    "original": fname,
                    "new_name": record.get("new_name"),
                    "destination": record.get("destination"),
                    "case": record.get("case"),
                    "subfolder": record.get("subfolder"),
                    "confidence": record.get("confidence"),
                    "method": record.get("method"),
                    "status": record.get("status"),
                },
                tags={"file": fname, "case": record.get("case", ""), "doc_type": doc_type},
            )
            return "filed", record

        # ⚠️ Low confidence or no match → failure zone
        reason = match.get("reason", "信心度不足")
        record = {
            "original": fname,
            "new_name": suggested,
            "doc_type": doc_type,
            "confidence": round(match.get("confidence", 0), 3),
            "reason": reason,
        }

        if not dry_run:
            dest_fail = _unique_target_path(os.path.join(SCAN_FAIL, suggested))
            shutil.move(src_path, dest_fail)
            record["status"] = "moved_to_fail"
            record["new_name"] = os.path.basename(dest_fail)
        else:
            record["status"] = "preview_fail"

        _eventlog(
            "pdf_filing:failed",
            ok=False,
            payload={
                "original": fname,
                "new_name": record.get("new_name"),
                "reason": record.get("reason"),
                "confidence": record.get("confidence"),
                "status": record.get("status"),
                "doc_type": doc_type,
            },
            tags={"file": fname, "doc_type": doc_type},
        )
        return "failed", record

    except Exception as e:
        logger.error(f"  ❌ Exception processing {fname}: {e}")
        _eventlog("pdf_filing:error", ok=False, payload={"file": fname, "error": str(e)[:220]}, tags={"file": fname})
        return "skipped", {"original": fname, "error": str(e)}


def process_scan_folder(dry_run: bool = True, notify: bool = True, max_workers: Optional[int] = None) -> Dict:
    """
    Main entry: process all PDFs in 01_掃描檔放置區.

    Uses batch architecture to minimize model switching:
      Phase 1: GLM-OCR batch — OCR all pages (model loaded once)
      Phase 2: Gemma 4 batch — analyze all OCR texts (model loaded once)
      Phase 3: Filing — match to cases and file

    Returns a filing report with results for each file.
    """
    from action import task_analyze, extract_text

    report = {
        "timestamp": datetime.now().isoformat(),
        "dry_run": dry_run,
        "filed": [],       # Successfully filed
        "failed": [],      # Named but can't determine destination
        "unnamed": [],     # Can't even name
        "skipped": [],     # Not a PDF / other issues
    }

    if not os.path.isdir(SCAN_INBOX):
        report["error"] = f"掃描檔放置區不存在: {SCAN_INBOX}"
        return report

    # Get files (Synology/CloudStorage 有時 listdir 會卡住；改用 ls + timeout，避免整個流程掛死)
    pdfs: List[str] = []
    try:
        p = subprocess.run(
            ["/bin/ls", "-1", SCAN_INBOX],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        if p.returncode != 0:
            report["error"] = f"掃描檔放置區讀取失敗: {p.stderr.strip()[:200]}"
            return report
        for line in (p.stdout or "").splitlines():
            f = (line or "").strip()
            if not f or f.startswith("."):
                continue
            if f.lower().endswith(".pdf"):
                pdfs.append(f)
    except subprocess.TimeoutExpired:
        report["error"] = "掃描檔放置區讀取逾時（Synology Drive 可能忙碌/離線）。"
        return report
    except Exception:
        # Fallback to Python listdir (best-effort)
        try:
            pdfs = [f for f in os.listdir(SCAN_INBOX) if f.lower().endswith(".pdf") and not f.startswith(".")]
        except Exception as e:
            report["error"] = f"掃描檔放置區讀取失敗: {e}"
            return report

    if not pdfs:
        report["message"] = "掃描檔放置區無 PDF"
        return report

    logger.info(f"📂 掃描檔放置區發現 {len(pdfs)} 份 PDF")

    # Build case index
    case_index = build_case_index()

    logger.info(f"🧵 pdf-namer batch mode (GLM-OCR → Gemma 4)")

    # ── Batch Phase 1: OCR all pages with GLM-OCR (model loaded once) ──
    from action import batch_ocr_pages, batch_analyze_texts
    pdf_paths = [os.path.join(SCAN_INBOX, f) for f in pdfs]
    ocr_results = batch_ocr_pages(pdf_paths)

    # ── Batch Phase 2: Analyze all OCR texts with Gemma 4 (model loaded once) ──
    analysis_results = batch_analyze_texts(ocr_results)

    # ── Phase 3: Filing — use pre-computed analysis for each PDF ──
    ordered_results: Dict[int, Tuple[str, Dict]] = {}
    for idx, fname in enumerate(pdfs):
        ordered_results[idx] = _process_single_pdf(
            fname,
            dry_run=dry_run,
            case_index=case_index,
            task_analyze_fn=task_analyze,
            extract_text_fn=extract_text,
        )

    for idx in sorted(ordered_results):
        bucket, record = ordered_results[idx]
        if bucket not in report:
            bucket = "skipped"
        report[bucket].append(record)

    # Save filing log
    _save_filing_log(report)

    # Send LINE notification if not dry run
    if not dry_run and notify:
        _send_filing_report(report)

    _eventlog(
        "pdf_filing:summary",
        ok=True,
        payload={
            "dry_run": bool(dry_run),
            "filed": len(report.get("filed") or []),
            "failed": len(report.get("failed") or []),
            "unnamed": len(report.get("unnamed") or []),
            "skipped": len(report.get("skipped") or []),
        },
    )
    return report


def _safe_move(src: str, dest: str):
    """Move file, handling duplicates by adding timestamp."""
    dest = _unique_target_path(dest)
    try:
        shutil.move(src, dest)
    except Exception as e:
        logger.error(f"移動失敗: {src} → {dest}: {e}")

def _run_bookmarker(pdf_path: str):
    """Run pdf-bookmarker skill on the filed PDF."""
    try:
        bm_action = os.path.join(os.path.dirname(SKILL_DIR), "pdf-bookmarker", "action.py")
        if not os.path.exists(bm_action):
            logger.warning("pdf-bookmarker skill not found")
            return
        
        logger.info(f"Running bookmarker on {os.path.basename(pdf_path)}...")
        # Use same python as current process
        py = sys.executable
        subprocess.run(
            [py, bm_action, "--task", "scan_file", "--path", pdf_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120
        )
    except Exception as e:
        logger.warning(f"Bookmarker failed: {e}")

def _best_effort_sync_osc_todos(filed_path: str, match: Dict, analysis: Dict) -> None:
    """
    After a successful filing, best-effort sync OSC todos into DB.
    This must be safe: never blocks filing; never deletes files.
    """
    if os.environ.get("PDF_NAMER_OSC_TODO_SYNC", "1").strip() != "1":
        return
    if not filed_path or (not os.path.exists(filed_path)):
        return
    if not os.path.exists(OSC_ORCH_PATH):
        return

    case_folder_name = ((match or {}).get("case_info") or {}).get("folder_name") or ""
    m = re.search(r"(\d{4}-\d{4})", case_folder_name)
    case_number = m.group(1) if m else ""
    parties = (analysis or {}).get("parties") or []
    if isinstance(parties, str):
        client_name = parties.strip()
    else:
        client_name = "、".join([p for p in parties if isinstance(p, str) and p.strip()])[:80]

    payload = {
        "path": filed_path,
        "case_number": case_number,
        "case_folder_name": case_folder_name,
        "client_name": client_name,
        "doc_type": (analysis or {}).get("doc_type", ""),
        "suggested_filename": (analysis or {}).get("suggested_filename", ""),
        "analysis": analysis or {},
    }
    
    py = OSC_ORCH_PY if os.path.exists(OSC_ORCH_PY) else sys.executable
    try:
        # Step 1: Parse and sync to local DB
        task_sync = "todo_sync " + json.dumps(payload, ensure_ascii=False)
        r1 = subprocess.run(
            [py, OSC_ORCH_PATH, "--task", task_sync],
            capture_output=True,
            text=True,
            timeout=45,
        )
        if r1.returncode != 0:
            logger.warning(f"OSC 待辦同步失敗(rc={r1.returncode}): {(r1.stderr or r1.stdout or '').strip()[:300]}")
            return
            
        # Step 2: Push unsynced DB items to Google Calendar
        task_gcal = "gcal_sync " + json.dumps({"limit": 50}, ensure_ascii=False)
        r2 = subprocess.run(
            [py, OSC_ORCH_PATH, "--task", task_gcal],
            capture_output=True,
            text=True,
            timeout=45,
        )
        if r2.returncode != 0:
            logger.warning(f"Google Calendar 同步失敗(rc={r2.returncode}): {(r2.stderr or r2.stdout or '').strip()[:300]}")
            
        # Step 3: 推 Discord 通知（best-effort）
        # 根因: OSC task_todo_sync 回傳的 JSON 是 {"ok": true, ...}
        # 不是 {"success": true}，原先 substring 判斷永遠不會命中。
        if r1.returncode == 0:
            stdout1 = (r1.stdout or "").strip()
            sync_ok = False
            try:
                _parsed = json.loads(stdout1) if stdout1 else {}
                if isinstance(_parsed, dict):
                    sync_ok = bool(_parsed.get("ok") or _parsed.get("success"))
            except Exception:
                # Fallback: substring check across both key variants
                _lower = stdout1.lower()
                sync_ok = (('"ok": true' in _lower) or ('"success": true' in _lower))
            if sync_ok:
                _push_discord_pdf_filing(
                    case_folder_name or analysis.get("case_folder_name", ""),
                    os.path.basename(filed_path),
                )

    except Exception as e:
        logger.warning(f"OSC 待辦/日曆同步呼叫失敗: {e}")


def _push_discord_pdf_filing(case_folder_name: str, file_name: str) -> None:
    """Best-effort Discord push after OSC todo sync（使用既有 red_phone 基礎設施）。"""
    try:
        message = (
            f"📄 PDF 命名完成\n"
            f"案件：{case_folder_name}\n"
            f"檔案：{file_name}\n"
            f"待辦已同步至 OSC"
        )
        try:
            from skills.ops.red_phone import send_telegram_push_with_status  # type: ignore
            send_telegram_push_with_status(
                message, severity="info", source="pdf_namer",
                topic_key="filing", queue_on_fail=False
            )
        except ImportError:
            import sys as _sys
            _sys.path.insert(0, os.path.join(os.path.dirname(SKILL_DIR), "ops"))
            from red_phone import send_telegram_push_with_status  # type: ignore
            send_telegram_push_with_status(
                message, severity="info", source="pdf_namer",
                topic_key="filing", queue_on_fail=False
            )
    except Exception:
        logging.getLogger(__name__).debug("Discord push failed", exc_info=True)


def sync_osc_todos_for_path(final_path: str) -> Dict:
    """供 action.py 單檔 rename 後呼叫的公開入口。

    從 final_path 推 case_folder_name（往上找到 NAS 案件根目錄），
    然後呼叫既有的 _best_effort_sync_osc_todos()。
    Best-effort：失敗只 log，不丟例外。
    """
    if os.environ.get("PDF_NAMER_OSC_TODO_SYNC", "1") != "1":
        return {"success": False, "skipped": "feature_flag_off"}

    try:
        parts = os.path.normpath(final_path).split(os.sep)
        try:
            idx = parts.index("01_案件")
            if idx + 2 < len(parts):
                case_folder_name = parts[idx + 2]
            else:
                return {"success": False, "skipped": "case_folder_not_resolved"}
        except ValueError:
            return {"success": False, "skipped": "not_in_case_tree"}

        match = {"case_info": {"folder_name": case_folder_name}, "case_folder": os.sep.join(parts[:idx + 3])}
        analysis = {"case_folder_name": case_folder_name}
        _best_effort_sync_osc_todos(final_path, match, analysis)
        return {"success": True, "case_folder_name": case_folder_name}
    except Exception as e:
        logging.getLogger(__name__).warning(
            "sync_osc_todos_for_path failed: %s", e, exc_info=True
        )
        return {"success": False, "error": str(e)}


def _save_filing_log(report: Dict):
    """Append to filing log for history tracking."""
    history = []
    if os.path.exists(FILING_LOG_PATH):
        try:
            with open(FILING_LOG_PATH, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 913, exc_info=True)

    history.append(report)

    # Keep last 100 reports
    if len(history) > 100:
        history = history[-100:]

    with open(FILING_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def _send_filing_report(report: Dict):
    """Send filing summary via TG + Discord (歸檔通知 channel)."""
    _push = None
    try:
        from skills.ops.red_phone import send_telegram_push_with_status  # type: ignore
        _push = lambda msg: send_telegram_push_with_status(
            msg, severity="info", source="pdf_namer", topic_key="filing", queue_on_fail=True)
    except ImportError:
        try:
            import sys
            sys.path.insert(0, os.path.join(os.path.dirname(SKILL_DIR), "ops"))
            from red_phone import send_line_push
            _push = send_line_push
        except ImportError:
            logger.warning("TG push 不可用")
            return

    filed = report.get("filed", [])
    failed = report.get("failed", [])
    unnamed = report.get("unnamed", [])

    lines = ["📁 CASPER 歸檔報告", f"時間: {report.get('timestamp', '')[:16]}", ""]

    if filed:
        lines.append(f"✅ 成功歸檔: {len(filed)} 份")
        for f_item in filed[:8]:
            lines.append(f"  {f_item['new_name'][:60]}")
            case_name = f_item.get('case', '')
            if case_name:
                lines.append(f"  → {case_name[:30]}/{f_item.get('subfolder', '')}")
        if len(filed) > 8:
            lines.append(f"  ... 另有 {len(filed) - 8} 份")

    if failed:
        lines.append(f"\n⚠️ 需人工確認: {len(failed)} 份")
        for f_item in failed[:5]:
            lines.append(f"  {f_item.get('new_name', f_item.get('original', '?'))[:60]}")
            lines.append(f"  原因: {f_item.get('reason', '信心度不足')[:20]}")
        lines.append("\n💡 回覆「歸檔 [檔名] [案件名]」可讓我重新歸")

    if unnamed:
        lines.append(f"\n❌ 無法命名: {len(unnamed)} 份")

    if not filed and not failed and not unnamed:
        lines.append("📭 無新掃描檔")

    try:
        _push("\n".join(lines))
    except Exception as e:
        logger.error(f"TG 發送失敗: {e}")


# ════════════════════════════════════════════════════════════════════════════
#  CORRECTION HANDLER — User corrections via LINE/DC
# ════════════════════════════════════════════════════════════════════════════

def correct_filing(
    filename: str,
    target_case: str,
    target_subfolder: str = "",
) -> Dict:
    """
    Handle a human correction: move a file from failure zone to the correct
    case folder, and learn from the correction.
    
    Args:
        filename: The PDF filename (with or without path)
        target_case: The case folder name or partial match (e.g., "[當事人D]" or "2025-0047")
        target_subfolder: Optional subfolder name keyword (e.g., "法院通知")
    
    Returns:
        Result dict with action taken
    """
    # Step 1: Find the file
    src_path = _find_file_in_zones(filename)
    if not src_path:
        return {"error": f"找不到檔案: {filename}", "searched": [SCAN_FAIL, SCAN_NONAME, SCAN_STAGED]}

    # Step 2: Find the target case
    case_index = build_case_index()
    matched_case = None

    for c in case_index:
        # Match by case ID, party name, or folder name
        if (target_case in c["folder_name"] or
            target_case in c.get("case_id", "") or
            any(target_case in p for p in c["parties"])):
            matched_case = c
            break

    if not matched_case:
        return {"error": f"找不到案件: {target_case}", "available_count": len(case_index)}

    # Step 3: Determine subfolder
    dest_subfolder = ""
    if target_subfolder:
        for sf in matched_case.get("subfolders", []):
            clean = re.sub(r'^\d+_', '', sf)
            if target_subfolder in clean or clean in target_subfolder:
                dest_subfolder = sf
                break

    if not dest_subfolder and target_subfolder:
        return {
            "error": f"在案件 {matched_case['folder_name']} 中找不到子資料夾: {target_subfolder}",
            "available": matched_case.get("subfolders", []),
        }

    dest_dir = os.path.join(matched_case["path"], dest_subfolder) if dest_subfolder else matched_case["path"]
    dest_path = os.path.join(dest_dir, os.path.basename(src_path))

    if not os.path.isdir(dest_dir):
        return {"error": f"目標資料夾不存在: {dest_dir}"}

    # Step 4: Move file
    if os.path.exists(dest_path):
        base, ext = os.path.splitext(os.path.basename(src_path))
        ts = datetime.now().strftime("%H%M%S")
        dest_path = os.path.join(dest_dir, f"{base}_{ts}{ext}")

    try:
        shutil.move(src_path, dest_path)
    except Exception as e:
        return {"error": f"移動失敗: {e}"}

    # Step 5: Learn from correction
    _learn_from_correction(
        filename=os.path.basename(src_path),
        case_folder=matched_case["folder_name"],
        subfolder=dest_subfolder,
        parties=matched_case["parties"],
    )

    return {
        "action": "corrected",
        "file": os.path.basename(src_path),
        "from": os.path.dirname(src_path),
        "to": dest_dir,
        "case": matched_case["folder_name"],
        "subfolder": dest_subfolder,
    }


def _find_file_in_zones(filename: str) -> Optional[str]:
    """Search for a file across all scan zones."""
    basename = os.path.basename(filename)

    for zone in [SCAN_FAIL, SCAN_NONAME, SCAN_STAGED, SCAN_INBOX]:
        # Exact match
        candidate = os.path.join(zone, basename)
        if os.path.exists(candidate):
            return candidate

        # Partial match (fuzzy)
        if os.path.isdir(zone):
            for f in os.listdir(zone):
                if basename in f or f in basename:
                    return os.path.join(zone, f)

    return None


def _learn_from_correction(
    filename: str,
    case_folder: str,
    subfolder: str,
    parties: List[str],
):
    """
    Save the correction to learning_history so CASPER improves next time.
    Records: what filename → which case → which subfolder.
    """
    try:
        # Determine doc_type from subfolder
        doc_type = ""
        clean_sf = re.sub(r'^\d+_', '', subfolder) if subfolder else ""
        from action import DOC_TYPES
        for dt in DOC_TYPES:
            if dt in clean_sf:
                doc_type = dt
                break
        if not doc_type: doc_type = clean_sf

        text_preview = f"[校正歸檔] 案件:{case_folder} 子資料夾:{subfolder} 當事人:{','.join(parties)}"
        from rag_feedback import rag_engine
        from action import extract_text
        src_path = _find_file_in_zones(filename) or filename
        p_text, _ = extract_text(src_path)
        if p_text:
            text_preview = p_text[:1000]

        rag_engine.log_feedback(text_preview, case_folder, doc_type, filename)
        logger.info(f"📚 RAG 已學習: {filename} → {case_folder}/{subfolder}")
    except Exception as e:
        logger.warning(f"RAG 學習儲存失敗: {e}")

    # Also save to a local correction log for pattern analysis
    correction_log_path = os.path.join(SKILL_DIR, "_corrections.json")
    corrections = []
    if os.path.exists(correction_log_path):
        try:
            with open(correction_log_path, "r", encoding="utf-8") as f:
                corrections = json.load(f)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1129, exc_info=True)

    corrections.append({
        "timestamp": datetime.now().isoformat(),
        "filename": filename,
        "case": case_folder,
        "subfolder": subfolder,
        "parties": parties,
    })

    with open(correction_log_path, "w", encoding="utf-8") as f:
        json.dump(corrections, f, ensure_ascii=False, indent=2)


# ════════════════════════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python smart_filer.py index          # Build case index")
        print("  python smart_filer.py scan            # Dry-run scan")
        print("  python smart_filer.py scan --execute  # Execute filing")
        print("  python smart_filer.py scan --execute --workers=4")
        print("  python smart_filer.py correct FILE CASE [SUBFOLDER]")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "index":
        idx = build_case_index(force_rebuild=True)
        print(f"Built index: {len(idx)} cases")
        for c in idx[:10]:
            print(f"  {c['case_type']}/{c['domain']}/{c['folder_name']} ({len(c['subfolders'])} subfolders)")

    elif cmd == "scan":
        execute = "--execute" in sys.argv
        workers = None
        for arg in sys.argv[2:]:
            if arg.startswith("--workers="):
                try:
                    workers = int(arg.split("=", 1)[1].strip())
                except Exception:
                    workers = None
                break
        result = process_scan_folder(dry_run=not execute, notify=execute, max_workers=workers)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "correct":
        if len(sys.argv) < 4:
            print("Usage: python smart_filer.py correct <filename> <case_name> [subfolder]")
            sys.exit(1)
        sf = sys.argv[4] if len(sys.argv) > 4 else ""
        result = correct_filing(sys.argv[2], sys.argv[3], sf)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    else:
        print(f"Unknown command: {cmd}")
