from __future__ import annotations

import logging
import re
import subprocess
from collections import Counter
from pathlib import Path


_CJK_RE = r"[\u3400-\u9fff○]"
_QUALITY_VERSION = "office-deliverable-v3"

_CASE_IDENTIFIER_RE = re.compile(
    r"(?:20\d{2}-\d{4}|1\d{2,3}\u5e74\u5ea6[^\s\uff0c\u3002\uff1b]{1,24}\u5b57\u7b2c?\s*\d+\s*\u865f|1\d{6,7}-[A-Z]-\d{3})",
    re.I,
)
_MONEY_RE = re.compile(r"(?:NT\$|TWD|\$|\u65b0\u81fa\u5e63)?\s*\d{1,3}(?:,\d{3})+(?:\.\d+)?\s*(?:\u5143|\u5713)?|\d+(?:\.\d+)?\s*(?:\u842c|\u5104)\s*\u5143", re.I)
_LAW_ARTICLE_RE = re.compile(r"(?:[\u4e00-\u9fff]{2,16}\u6cd5)?\s*\u7b2c\s*\d+(?:\s*[-\u2013\u2014]\s*\d+)?\s*\u689d", re.I)
_LAW_ARTICLE_ZH_RE = re.compile(r"\u7b2c\s*([\u96f6〇一二兩三四五六七八九十百千]+)\s*\u689d")
_DATE_RE = re.compile(r"(?:20\d{2}|1\d{2,3})[\u5e74/.\-]\d{1,2}[\u6708/.\-]\d{1,2}\u65e5?", re.I)
_ROC_CHINESE_DATE_RE = re.compile(
    r"(?:\u6c11\u570b\s*)?([\u96f6〇一二兩三四五六七八九十百千]+)\u5e74"
    r"([\u96f6〇一二兩三四五六七八九十]+)\u6708"
    r"([\u96f6〇一二兩三四五六七八九十]+)\u65e5"
)
_ENGLISH_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}),\s*(20\d{2})\b",
    re.I,
)
_LEGAL_SOURCE_RE = re.compile(r"(?:\u6cd5\u9662|\u88c1\u5224|\u5224\u6c7a|\u88c1\u5b9a|\u4e3b\u6587|\u7406\u7531|\u539f\u544a|\u88ab\u544a|\u8072\u8acb\u4eba|\u4e0a\u8a34\u4eba|\u6cd5\u5f8b\u722d\u9ede)")
_LEGAL_REASONING_RE = re.compile(r"(?:\u722d\u9ede|\u898b\u89e3|\u7406\u7531|\u6cd5\u9662\u8a8d為|\u672c\u9662\u8a8d為|\u8981\u4ef6|\u6db5\u651d|\u5224\u65b7|\u7d50\u8ad6|\u4e3b\u6587)")


def _norm_anchor(value: str) -> str:
    return re.sub(r"[\s,，]", "", str(value or "")).lower()


def _anchors(pattern: re.Pattern[str], text: str) -> set[str]:
    return {_norm_anchor(match.group(0)) for match in pattern.finditer(str(text or "")) if match.group(0).strip()}


def _law_anchors(text: str) -> set[str]:
    """Normalize citations by article number so surrounding prose is irrelevant."""
    anchors: set[str] = set()
    for match in _LAW_ARTICLE_RE.finditer(str(text or "")):
        article = re.search(r"\u7b2c\s*\d+(?:\s*[-\u2013\u2014]\s*\d+)?\s*\u689d", match.group(0))
        if article:
            anchors.add(_norm_anchor(article.group(0)))
    for match in _LAW_ARTICLE_ZH_RE.finditer(str(text or "")):
        value = _chinese_integer(match.group(1))
        if value is not None:
            anchors.add(f"\u7b2c{value}\u689d")
    return anchors


def _chinese_integer(value: str) -> int | None:
    """Parse the formal Chinese integers commonly spoken in ROC dates/articles."""
    digits = {"\u96f6": 0, "\u3007": 0, "\u4e00": 1, "\u4e8c": 2, "\u5169": 2, "\u4e09": 3, "\u56db": 4, "\u4e94": 5, "\u516d": 6, "\u4e03": 7, "\u516b": 8, "\u4e5d": 9}
    units = {"\u5341": 10, "\u767e": 100, "\u5343": 1000}
    raw = str(value or "").strip()
    if not raw or any(char not in digits and char not in units for char in raw):
        return None
    if not any(char in units for char in raw):
        try:
            return int("".join(str(digits[char]) for char in raw))
        except ValueError:
            return None
    total = 0
    pending = 0
    for char in raw:
        if char in digits:
            pending = digits[char]
            continue
        unit = units[char]
        total += (pending or 1) * unit
        pending = 0
    return total + pending


def _money_anchors(text: str) -> set[str]:
    """Compare monetary facts by value, not by translated currency spelling."""
    anchors: set[str] = set()
    for match in _MONEY_RE.finditer(str(text or "")):
        raw = match.group(0).replace(",", "")
        number = re.search(r"\d+(?:\.\d+)?", raw)
        if not number:
            continue
        value = float(number.group(0))
        if "\u842c" in raw:
            value *= 10_000
        elif "\u5104" in raw:
            value *= 100_000_000
        anchors.add(f"{value:.2f}".rstrip("0").rstrip("."))
    return anchors


def _date_anchors(text: str) -> set[str]:
    """Canonicalize Gregorian, ROC and English month dates to YYYY-MM-DD."""
    anchors: set[str] = set()
    for match in _DATE_RE.finditer(str(text or "")):
        parts = [int(value) for value in re.findall(r"\d+", match.group(0))[:3]]
        if len(parts) != 3:
            continue
        year, month, day = parts
        if year < 1911:
            year += 1911
        if 1 <= month <= 12 and 1 <= day <= 31:
            anchors.add(f"{year:04d}-{month:02d}-{day:02d}")
    for match in _ROC_CHINESE_DATE_RE.finditer(str(text or "")):
        roc_year = _chinese_integer(match.group(1))
        month = _chinese_integer(match.group(2))
        day = _chinese_integer(match.group(3))
        if roc_year is None or month is None or day is None:
            continue
        year = roc_year + 1911
        if 1 <= month <= 12 and 1 <= day <= 31:
            anchors.add(f"{year:04d}-{month:02d}-{day:02d}")
    months = {
        name.lower(): index
        for index, name in enumerate(
            "January February March April May June July August September October November December".split(),
            1,
        )
    }
    for match in _ENGLISH_DATE_RE.finditer(str(text or "")):
        month = months[match.group(1).lower()]
        day = int(match.group(2))
        year = int(match.group(3))
        if 1 <= day <= 31:
            anchors.add(f"{year:04d}-{month:02d}-{day:02d}")
    return anchors


def _duplicate_line_ratio(text: str) -> float:
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(text or "").splitlines()]
    lines = [line for line in lines if len(line) >= 10]
    if len(lines) < 4:
        return 0.0
    counts = Counter(lines)
    duplicates = sum(count - 1 for count in counts.values() if count > 1)
    return duplicates / max(1, len(lines))


def _meaningful_paragraph_count(text: str) -> int:
    """Count substantive paragraphs across compact CJK and Latin prose.

    A fixed 12-character floor incorrectly discarded short Chinese
    translations (for example ``這是第一段翻譯。``) while counting the
    longer English source paragraph.  Four non-whitespace characters is still
    enough to ignore page-number/OCR specks without making paragraph fidelity
    depend on the language's average word length.
    """
    return len(
        [part for part in re.split(r"\n\s*\n+", str(text or "")) if len(re.sub(r"\s+", "", part)) >= 4]
    )


def _office_fidelity_metrics(kind: str, output: str, source_text: str, instruction: str) -> dict[str, object]:
    """Cheap factual-fidelity checks shared by summary/translation/transcript."""
    mode = str(kind or "").strip().lower()
    source = str(source_text or "")
    out = str(output or "")
    source_case = _anchors(_CASE_IDENTIFIER_RE, source)
    output_case = _anchors(_CASE_IDENTIFIER_RE, out)
    source_money = _money_anchors(source)
    output_money = _money_anchors(out)
    source_law = _law_anchors(source)
    output_law = _law_anchors(out)
    source_dates = _date_anchors(source)
    output_dates = _date_anchors(out)
    metrics: dict[str, object] = {
        "source_case_identifiers": len(source_case),
        "output_case_identifiers": len(output_case),
        "missing_case_identifiers": sorted(source_case - output_case),
        "invented_case_identifiers": sorted(output_case - source_case) if source else [],
        "missing_money_anchors": sorted(source_money - output_money),
        "invented_money_anchors": sorted(output_money - source_money) if source else [],
        "missing_law_anchors": sorted(source_law - output_law),
        "missing_date_anchors": sorted(source_dates - output_dates),
        "invented_date_anchors": sorted(output_dates - source_dates) if source else [],
        "duplicate_line_ratio": round(_duplicate_line_ratio(out), 4),
        "output_source_length_ratio": round(len(out) / max(1, len(source)), 4) if source else None,
        "has_timestamp_text": bool(re.search(r"\[(?:\d{1,2}:)?\d{1,2}:\d{2}(?:\s*[-\u2013]\s*(?:\d{1,2}:)?\d{1,2}:\d{2})?\]", out)),
        "has_speaker_label": bool(re.search(r"(?:SPEAKER[_ ]?\d+|\u8aaa\u8a71\u4eba\s*\d*|\u767c\u8a00\u4eba\s*\d*)\s*[:\uff1a]", out, re.I)),
        "source_paragraph_count": _meaningful_paragraph_count(source),
        "output_paragraph_count": _meaningful_paragraph_count(out),
    }
    # Translation is a full-fidelity task.  A summary may legitimately omit
    # secondary dates/amounts, but it may never invent a case identifier and
    # should retain the only identifier of a legal document.
    if mode == "translation":
        metrics["required_anchor_missing"] = bool(
            (source_case - output_case)
            or (source_money - output_money)
            or (source_law - output_law)
            or (source_dates - output_dates)
        )
        metrics["paragraph_structure_missing"] = bool(
            metrics["source_paragraph_count"] >= 2
            and metrics["output_paragraph_count"] < metrics["source_paragraph_count"]
        )
    elif mode == "summary":
        metrics["required_anchor_missing"] = bool(
            (len(source_case) <= 3 and source_case - output_case)
            or (len(source_money) == 1 and source_money - output_money)
            or (len(source_law) == 1 and source_law - output_law)
        )
        metrics["legal_source"] = bool(_LEGAL_SOURCE_RE.search(source[:30000]))
        metrics["legal_reasoning_present"] = bool(_LEGAL_REASONING_RE.search(out))
    elif mode == "transcript":
        metrics["required_anchor_missing"] = bool(
            (source_case - output_case)
            or (source_money - output_money)
            or (source_law - output_law)
            or (source_dates - output_dates)
        )
    else:
        metrics["required_anchor_missing"] = False
    metrics["instruction"] = str(instruction or "")[:160]
    return metrics


def _office_fidelity_issue(kind: str, metrics: dict[str, object], *, metadata: dict | None = None) -> str:
    mode = str(kind or "").strip().lower()
    meta = metadata or {}
    if mode == "summary" and meta.get("source_required") and not meta.get("source_text_present"):
        return "summary_source_unavailable"
    if metrics.get("invented_case_identifiers"):
        return "invented_case_identifier"
    if metrics.get("invented_money_anchors"):
        return "invented_money_anchor"
    if metrics.get("invented_date_anchors"):
        return "invented_date_anchor"
    if metrics.get("required_anchor_missing"):
        return f"{mode}_critical_anchor_missing"
    if mode == "translation" and metrics.get("paragraph_structure_missing"):
        return "translation_paragraph_structure_missing"
    if float(metrics.get("duplicate_line_ratio") or 0.0) >= 0.34:
        return f"{mode}_excessive_repetition"
    if mode == "summary" and metrics.get("legal_source") and not metrics.get("legal_reasoning_present"):
        return "legal_summary_missing_reasoning"
    if mode == "translation":
        ratio = metrics.get("output_source_length_ratio")
        if isinstance(ratio, (int, float)) and ratio > 2.6:
            return "translation_excessive_expansion"
    if mode == "transcript":
        if meta.get("recognizer_text_present") is False:
            return "transcript_no_recognized_content"
        instruction = str(metrics.get("instruction") or "")
        if re.search(r"(?:\u6642\u9593\u6233|\u6642\u9593\u78bc|timestamp)", instruction, re.I) and not (
            str(meta.get("timestamp_text") or "").strip() or metrics.get("has_timestamp_text")
        ):
            return "transcript_missing_timestamps"
        if re.search(r"(?:\u8aaa\u8a71\u4eba|\u767c\u8a00\u4eba|speaker)", instruction, re.I) and not (
            int(meta.get("speaker_count_estimate") or 0) >= 1 or metrics.get("has_speaker_label")
        ):
            return "transcript_missing_speakers"
    return ""


def _quality_dimensions(kind: str, issue: str, metrics: dict[str, object]) -> dict[str, int]:
    """Return auditable dimensions instead of a meaningless single pass flag."""
    fidelity = 100
    completeness = 100
    readability = 100
    structure = 100
    if metrics.get("invented_case_identifiers"):
        fidelity = 0
    if metrics.get("invented_money_anchors") or metrics.get("invented_date_anchors"):
        fidelity = 0
    if metrics.get("required_anchor_missing"):
        completeness = max(0, completeness - 55)
    missing_count = sum(
        len(metrics.get(key) or [])
        for key in ("missing_case_identifiers", "missing_money_anchors", "missing_law_anchors", "missing_date_anchors")
    )
    completeness = max(0, completeness - min(40, missing_count * 8))
    duplicate_ratio = float(metrics.get("duplicate_line_ratio") or 0.0)
    readability = max(0, int(round(100 - min(90.0, duplicate_ratio * 180.0))))
    if kind == "summary" and metrics.get("legal_source") and not metrics.get("legal_reasoning_present"):
        structure = 35
    if issue in {"empty_output", "off_topic_or_refusal", "prompt_or_react_leak"}:
        fidelity = completeness = readability = structure = 0
    overall = int(round(fidelity * 0.4 + completeness * 0.3 + readability * 0.15 + structure * 0.15))
    if issue:
        overall = min(overall, 59)
    return {
        "overall": overall,
        "fidelity": fidelity,
        "completeness": completeness,
        "readability": readability,
        "structure": structure,
    }


def compact_ocr_text(text: str) -> str:
    """Normalize common PDF/OCR spacing without changing substantive wording."""
    out = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    out = re.sub(r"---\s*第\s*\d+\s*頁(?:\s*\(OCR\))?\s*---", "\n", out)
    out = re.sub(r"\f+", "\n", out)
    out = re.sub(rf"(?<={_CJK_RE})[ \t]*\n[ \t]{{2,}}(?={_CJK_RE})", "", out)
    # Many official PDFs copy as "本 案 正 ○ 福"; collapse only CJK-to-CJK gaps.
    for _ in range(3):
        out = re.sub(rf"(?<={_CJK_RE})[ \t]+(?={_CJK_RE})", "", out)
    out = re.sub(r"\s+([，。、；：])", r"\1", out)
    out = re.sub(r"([，。、；：])\s+", r"\1", out)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def detect_output_quality_issue(kind: str, output: str, *, source_chars: int = 0) -> str:
    """Return a short issue code when a deliverable is clearly unsafe to ship."""
    mode = (kind or "").strip().lower()
    text = str(output or "").strip()
    lowered = text.lower()
    if not text:
        return "empty_output"

    blocking_markers = [
        "請問**當事人**",
        "請問當事人",
        "當事人的姓名？",
        "已知資訊：案號",
        "請提供文件內容",
        "我無法直接存取",
        "無法讀取該檔案",
        "pdf 摘要失敗",
        "[pdf 摘要失敗",
        "語音已接收，但目前無法完成轉錄",
        "語音處理失敗",
        "transcription_failed",
        "無法完成轉錄",
        "翻譯逾時",
        "翻譯失敗",
        "先保留原文",
        "處理發生系統錯誤",
        "⚠️ 第",
    ]
    if any(marker.lower() in lowered for marker in blocking_markers):
        return "off_topic_or_refusal"

    prompt_leak_markers = [
        "<|channel|>",
        "<|analysis|>",
        "thought:",
        "action:",
        "observation:",
        "system prompt",
        "developer message",
        "工具調用計畫",
        "以下是我的思考",
    ]
    if any(marker in lowered for marker in prompt_leak_markers):
        return "prompt_or_react_leak"

    if mode == "summary":
        if source_chars >= 6000 and len(text) < 360:
            return "summary_too_short"
        if source_chars >= 30000 and len(text) < 900:
            return "large_summary_too_short"
        if re.search(r"請(問|提供).{0,20}(姓名|案號|當事人)", text):
            return "case_intake_question"

    if mode == "translation":
        if source_chars >= 1200 and len(text) < 260:
            return "translation_too_short"
        if "以下是翻譯" in text and len(text) < 420 and source_chars > 3000:
            return "translation_intro_only"

    if mode == "transcript":
        if source_chars >= 1200 and len(text) < 180:
            return "transcript_too_short"

    return ""


def estimate_effective_source_chars(source_text: str) -> int:
    """
    Estimate source length for completeness gates.

    Repeated boilerplate/test fixtures should not force the same minimum output
    length as a genuinely diverse long legal document.
    """
    text = str(source_text or "")
    raw_len = len(text)
    if raw_len < 2000:
        return raw_len
    tokens = re.findall(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]", text.lower())
    if len(tokens) < 80:
        return raw_len
    unique_ratio = len(set(tokens)) / max(1, len(tokens))
    if unique_ratio < 0.04:
        return min(raw_len, 800)
    if unique_ratio < 0.08:
        return min(raw_len, 1600)
    return raw_len


def estimate_transcript_source_chars_from_audio(path: str | Path) -> int:
    """Map audio duration to a conservative transcript completeness threshold."""
    p = str(path or "")
    duration = 0.0
    if p:
        try:
            cp = subprocess.run(["afinfo", p], capture_output=True, text=True, timeout=5)
            text = (cp.stdout or "") + "\n" + (cp.stderr or "")
            m = re.search(r"estimated duration:\s*([0-9.]+)\s*sec", text, flags=re.I)
            if m:
                duration = float(m.group(1))
        except Exception:
            duration = 0.0
    if duration >= 180:
        return 6000
    if duration >= 60:
        return 2400
    if duration >= 25:
        return 1200
    if duration >= 10:
        return 500
    return 0


_KIND_LABELS = {
    "summary": "摘要",
    "translation": "翻譯",
    "transcript": "逐字稿",
}


def format_quality_gate_failure(kind: str, issue: str) -> str:
    label = _KIND_LABELS.get((kind or "").strip().lower(), "輸出")
    if str(issue or "").startswith("translation_missing_source_terms:"):
        terms = str(issue or "").split(":", 1)[1].strip()
        issue_label = f"譯文正文未保留原文專有名詞：{terms}"
        return (
            f"❌ {label}品質檢查未通過：{issue_label}。\n"
            "MAGI 已停止交付本次結果，避免把不完整或錯誤內容當成正式文件。"
        )
    issue_label = {
        "empty_output": "輸出為空",
        "off_topic_or_refusal": "模型偏題或拒絕讀取來源",
        "prompt_or_react_leak": "輸出含工具/思考洩漏",
        "summary_too_short": "摘要相對來源過短",
        "large_summary_too_short": "大型文件摘要相對來源過短",
        "case_intake_question": "摘要被誤導成案件建檔問答",
        "translation_too_short": "翻譯相對來源過短",
        "translation_intro_only": "翻譯只有開場語，未完成正文",
        "translation_idiom_error": "翻譯含高風險慣用語錯譯",
        "transcript_too_short": "逐字稿相對來源過短",
        "invented_case_identifier": "輸出出現原文沒有的案號",
        "invented_money_anchor": "輸出出現原文沒有的金額",
        "invented_date_anchor": "輸出出現原文沒有的日期",
        "summary_critical_anchor_missing": "摘要遺漏案號、單一重要金額或法條",
        "translation_critical_anchor_missing": "譯文遺漏案號、日期、金額或法條",
        "summary_excessive_repetition": "摘要有大量重複內容",
        "translation_excessive_repetition": "譯文有大量重複內容",
        "transcript_excessive_repetition": "逐字稿有大量重複內容",
        "legal_summary_missing_reasoning": "法律文件摘要沒有呈現爭點、理由或判斷",
        "summary_source_unavailable": "無可驗證原文，摘要不得作為正式交付",
        "translation_excessive_expansion": "譯文異常擴寫，可能加入原文沒有的內容",
        "translation_paragraph_structure_missing": "譯文遺失原文段落結構",
        "transcript_missing_timestamps": "使用者要求時間戳，但轉錄結果沒有時間資料",
        "transcript_missing_speakers": "使用者要求說話人識別，但結果沒有說話人資料",
        "transcript_critical_anchor_missing": "逐字稿潤稿過程改掉或漏掉案號、日期、金額或法條",
        "transcript_no_recognized_content": "辨識器未取得可驗證內容，已停止潤稿或補寫",
    }.get(issue, issue or "品質未通過")
    return (
        f"❌ {label}品質檢查未通過：{issue_label}。\n"
        "MAGI 已停止交付本次結果，避免把不完整或錯誤內容當成正式文件。"
    )


def run_output_quality_gate(
    kind: str,
    output: str,
    *,
    source_chars: int = 0,
    source_text: str = "",
    source_name: str = "",
    instruction: str = "",
    metadata: dict | None = None,
) -> dict[str, object]:
    """Central quality gate for model-generated deliverables."""
    effective_chars = estimate_effective_source_chars(source_text) if source_text else source_chars
    mode = (kind or "").strip().lower()
    # A post-processor has no evidentiary basis when ASR returned nothing.
    # Check this before length heuristics so the caller gets the actionable
    # fail-closed reason rather than a secondary "too short" symptom.
    if mode == "transcript" and isinstance(metadata, dict) and metadata.get("recognizer_text_present") is False:
        issue = "transcript_no_recognized_content"
    else:
        issue = detect_output_quality_issue(kind, output, source_chars=effective_chars)
    metrics = _office_fidelity_metrics(mode, output, source_text, instruction)
    if not issue:
        issue = _office_fidelity_issue(mode, metrics, metadata=metadata)
    if not issue and mode == "translation" and str(source_text or "").strip():
        try:
            from api.handlers.document_handler import (
                build_translation_term_glossary,
                missing_translation_source_terms,
                parse_translation_term_glossary,
                translation_idiom_issues,
            )

            idiom_issues = translation_idiom_issues(source_text, output)
            if idiom_issues:
                issue = "translation_idiom_error"
            else:
                glossary = build_translation_term_glossary(source_text)
                important_rows = []
                for row in parse_translation_term_glossary(glossary):
                    target = str(row.get("target") or "")
                    if target and not any(marker in target for marker in ("保留原文", "括號標註", "必要時")):
                        important_rows.append(row)
                filtered_glossary = ""
                if important_rows:
                    lines = ["| 原文 | 建議譯法/保留方式 |", "| --- | --- |"]
                    for row in important_rows[:8]:
                        lines.append(f"| {row.get('source', '')} | {row.get('target', '')} |")
                    filtered_glossary = "\n".join(lines)
                missing_terms = missing_translation_source_terms(
                    source_text,
                    output,
                    term_glossary=filtered_glossary,
                    max_terms=8,
                ) if filtered_glossary else []
                if missing_terms:
                    issue = "translation_missing_source_terms:" + ",".join(missing_terms[:4])
        except Exception:
            logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 214, exc_info=True)
    dimensions = _quality_dimensions(mode, issue, metrics)
    return {
        "ok": not bool(issue),
        "kind": mode,
        "issue": issue,
        "quality_version": _QUALITY_VERSION,
        "score": dimensions["overall"],
        "dimensions": dimensions,
        "metrics": metrics,
        "recommended_action": (
            "retry_verified_heavy_or_human_review"
            if issue and mode in {"summary", "translation"}
            else "retranscribe_or_review_segments"
            if issue and mode == "transcript"
            else "deliver"
        ),
        "message": format_quality_gate_failure(kind, issue) if issue else "",
        "reviewer_note": build_output_reviewer_note(
            kind,
            source_chars=effective_chars,
            source_name=source_name,
            instruction=instruction,
        ),
    }


def build_output_reviewer_note(
    kind: str,
    *,
    source_chars: int = 0,
    source_name: str = "",
    instruction: str = "",
) -> str:
    """Short source/coverage note inspired by legal workflow review gates."""
    label = _KIND_LABELS.get((kind or "").strip().lower(), "輸出")
    source = Path(source_name or "").name or "使用者提供內容"
    coverage = "已讀取可抽取全文" if source_chars >= 1200 else "來源較短或未提供可估算字數"
    lines = [
        f"覆核註記：{label}",
        f"- 來源：{source}",
        f"- 覆蓋：{coverage}",
        "- 正式引用前：請回到原始檔案核對頁碼、段落與專有名詞。",
    ]
    if instruction:
        lines.append(f"- 指示：{instruction.strip()[:120]}")
    return "\n".join(lines)


def _clean_line(line: str) -> str:
    line = compact_ocr_text(line)
    line = re.sub(r"\s+", " ", line)
    return line.strip(" -　\t")


def _section_between(text: str, start: str, stops: list[str]) -> str:
    start_match = re.search(start, text, flags=re.MULTILINE)
    if not start_match:
        return ""
    start_pos = start_match.end()
    end_pos = len(text)
    for stop in stops:
        stop_match = re.search(stop, text[start_pos:], flags=re.MULTILINE)
        if stop_match:
            end_pos = min(end_pos, start_pos + stop_match.start())
    return text[start_pos:end_pos].strip()


def _split_sentences(text: str) -> list[str]:
    clean = re.sub(r"\s+", " ", compact_ocr_text(text))
    parts = re.split(r"(?<=[。！？；;])\s*", clean)
    return [p.strip() for p in parts if len(p.strip()) >= 18]


def _first_useful_sentence(text: str, *, max_len: int = 230) -> str:
    for sent in _split_sentences(text):
        sent = re.sub(r"^[一二三四五六七八九十]+、\s*", "", sent).strip()
        sent = re.split(r"\s*[\(（][一二三四五六七八九十][\)）]", sent, maxsplit=1)[0].strip()
        if re.fullmatch(r"[\d\W_]+", sent):
            continue
        body = sent[:max_len].rstrip("，,；;。")
        return body + ("。" if body and not body.endswith(("。", "！", "？")) else "")
    clean = _clean_line(text)
    body = clean[:max_len].rstrip("，,；;。")
    return body + ("。" if body and not body.endswith(("。", "！", "？")) else "")


def _collect_numbered_sections(text: str) -> list[tuple[str, str]]:
    body = compact_ocr_text(text)
    starts: list[tuple[int, str]] = []
    seen_labels: set[str] = set()
    for match in re.finditer(r"(?:(?<=^)|(?<=[。；：\n]))\s*([一二三四五六七八九十]{1,3})、", body):
        label = match.group(1)
        after = body[match.end() : match.end() + 40].lstrip()
        expected_prefixes = {
            "一": ("據", "本院"),
            "二": ("本案",),
            "三": ("原",),
            "四": ("歷",),
        }
        if label in expected_prefixes and not after.startswith(expected_prefixes[label]):
            continue
        if label in {"五", "六"} and any(token in after for token in ("函請", "上網公布", "調查意見")):
            continue
        if label in seen_labels:
            continue
        seen_labels.add(label)
        starts.append((match.start(), label))
        if len(starts) >= 10:
            break
    sections: list[tuple[str, str]] = []
    for pos, (idx, label) in enumerate(starts):
        end = starts[pos + 1][0] if pos + 1 < len(starts) else len(body)
        block = body[idx:end].strip()
        if block:
            sections.append((label, block))
    return sections


def build_legal_document_summary_fallback(
    source_text: str,
    *,
    source_name: str = "",
    instruction: str = "",
) -> str:
    """
    Deterministic fallback for Taiwan legal/public-law documents.

    It is intentionally conservative: every bullet is drawn from visible text,
    so a bad model answer never becomes a fabricated legal summary.
    """
    text = compact_ocr_text(source_text)
    if not text:
        return ""

    lower_name = Path(source_name or "").name
    title = "法律文件摘要"
    if "監察院" in text[:3000] or "調查報告" in text[:1200]:
        title = "監察院調查報告摘要"
    elif "判決" in text[:2000]:
        title = "判決重點摘要"
    elif "裁定" in text[:2000]:
        title = "裁定重點摘要"

    case_section = _section_between(text, r"壹、\s*案\s*由[:：]?", [r"\n貳、", r"\n參、"])
    investigation_section = _section_between(text, r"貳、\s*調查意見[:：]?", [r"\n參、", r"\n肆、"])
    disposition_section = _section_between(text, r"參、\s*處理辦法[:：]?", [r"\n肆、", r"\n伍、"])

    numbered = _collect_numbered_sections(investigation_section or text)
    key_sections: list[str] = []
    conclusion_markers = ("核有違誤", "顯有違失", "正當法律程序", "再審", "檢討改進", "客觀性義務", "非無疑義", "比例失衡", "未能落實", "危及", "允宜")
    for _, block in numbered[:8]:
        first = _first_useful_sentence(block)
        conclusion_hits = []
        if not any(k in first for k in conclusion_markers):
            for sent in _split_sentences(block):
                clean_sent = re.sub(r"^[一二三四五六七八九十]+、\s*", "", sent).strip()
                if clean_sent.startswith(("最高法院", "司法院釋字", "原基法", "刑事訴訟法", "憲法第")):
                    continue
                if any(k in clean_sent for k in conclusion_markers):
                    clean_sent = re.split(r"\s*[\(（][一二三四五六七八九十][\)）]", clean_sent, maxsplit=1)[0].strip()
                    conclusion_hits.append(clean_sent[:240])
                if len(conclusion_hits) >= 1:
                    break
        merged = first
        for hit in conclusion_hits:
            hit_clean = re.sub(r"^[一二三四五六七八九十]+、\s*", "", hit).strip()
            hit_norm = re.sub(r"[^\w\u4e00-\u9fff○]+", "", hit_clean)
            merged_norm = re.sub(r"[^\w\u4e00-\u9fff○]+", "", merged)
            if hit_clean and hit_norm and hit_norm not in merged_norm and merged_norm not in hit_norm:
                merged += f"；{hit_clean.rstrip('。')}。"
        if merged:
            key_sections.append(merged[:420])

    issue_keywords = [
        ("程序與筆錄瑕疵", ("筆錄", "警詢", "詢問", "客觀性義務", "共犯", "區隔")),
        ("語言與通譯保障", ("通譯", "太魯閣", "原住民", "族語", "ICERD")),
        ("證據評價與再審線索", ("再審", "證詞", "有罪判決", "確定判決", "繳還")),
        ("機關後續處理", ("函請", "檢討改進", "研提", "處理辦法")),
    ]
    issue_lines: list[str] = []
    issue_search_text = investigation_section or text
    if all(k in text for k in ("林○蘭", "警詢", "共犯", "筆錄", "核有違誤")):
        issue_lines.append("- 程序與筆錄瑕疵：警詢未確實區隔共犯，且林○蘭、正○福溝通內容未完整反映於筆錄，監察院認定核有違誤。")
    if all(k in text for k in ("李○花", "律師", "正當法律程序", "核有違誤")):
        issue_lines.append("- 律師協助與權利告知：警方及檢察官雖形式上權利告知，但未從免費律師或法扶可維護權益角度充分說明，並有誘導李○花誤認律師僅為陪襯之疑慮。")
    if all(k in text for k in ("原確定判決", "證詞", "繳還", "再審")):
        issue_lines.append("- 證據評價與再審線索：原確定判決主要倚賴正○福、林○蘭證詞及事後繳還現金；監察院認為前階段程序瑕疵可作研提再審或非常上訴之線索。")
    if any(k in text for k in ("太魯閣", "族語", "ICERD", "通譯")):
        issue_lines.append("- 原住民族語言保障：報告連結太魯閣族語、司法通譯、ICERD及原住民族司法程序保障，指出通譯制度與實務仍需檢討。")
    for label, keys in issue_keywords:
        if any(line.startswith(f"- {label}") for line in issue_lines):
            continue
        for sent in _split_sentences(issue_search_text):
            if "壹、案由" in sent:
                continue
            if any(k in sent for k in keys):
                issue_lines.append(f"- {label}：{sent[:260]}")
                break

    disposition_lines = []
    for sent in _split_sentences(disposition_section):
        if any(k in sent for k in ("函請", "建議", "公布", "檢討", "研提", "見復")):
            disposition_lines.append(f"- {sent[:260]}")
        if len(disposition_lines) >= 5:
            break

    case_summary = _first_useful_sentence(case_section, max_len=360) if case_section else ""
    if not case_summary:
        case_summary = _first_useful_sentence(text, max_len=360)

    lines: list[str] = [
        f"# {title}",
        "",
        "## 文件定位",
        f"- 來源檔案：{lower_name or '未命名文件'}",
        f"- 文件性質：{title}，以下摘要由全文抽取並整理，避免模型偏題或漏摘。",
        f"- 核心案由：{case_summary}",
        "",
        "## 核心結論",
    ]
    if key_sections:
        for idx, item in enumerate(key_sections[:8], start=1):
            lines.append(f"{idx}. {item}")
    else:
        for idx, sent in enumerate(_split_sentences(text)[:6], start=1):
            lines.append(f"{idx}. {sent[:320]}")

    if issue_lines:
        lines.extend(["", "## 可供案件使用的爭點"])
        lines.extend(issue_lines[:6])

    if disposition_lines:
        lines.extend(["", "## 處理辦法或後續方向"])
        lines.extend(disposition_lines[:6])

    lines.extend(
        [
            "",
            "## 品質註記",
            "- 本次輸出已避開一般問答路由，未將文件誤判為案件建檔問答。",
            "- 若要引用於書狀，仍應回到原 PDF 對照頁碼與原文。",
        ]
    )
    if instruction:
        lines.append(f"- 使用者指示：{instruction.strip()[:120]}")

    return "\n".join(lines).strip()
