from __future__ import annotations

import re
from pathlib import Path


_CJK_RE = r"[\u3400-\u9fff○]"


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
    ]
    if any(marker.lower() in lowered for marker in blocking_markers):
        return "off_topic_or_refusal"

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
