#!/usr/bin/env python3
"""Build the ROC 100+ judicial/bar essay bank and its offline archive.

The build is intentionally offline-curated.  MAGI never invents a rubric at
review time: official MOEX scoring-point documents are converted to a locked
qualitative rubric; where MOEX did not publish one, public model answers are
archived and converted by a fixed offline compiler to a clearly-labelled
non-official practice rubric.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


MOEX_INDEX = "https://wwwq.moex.gov.tw/exam/wFrmExamQandASearch.aspx"
HIGHPOINT_LIST = "https://lawyer.get.com.tw/exam/List.aspx"
HEADERS = {"User-Agent": "MAGI-private-exam-tutor/2.0 (offline archival; private use)"}
CURRENT_ROC_YEAR = datetime.now(ZoneInfo("Asia/Taipei")).year - 1911
YEARS = tuple(range(100, CURRENT_ROC_YEAR + 1))
DISCOVERED_RUBRICS: dict[int, dict] = {}
MOEX_NEWS_RSS = "https://wwwc.moex.gov.tw/main/news/wfrmNewsRSSdetail.aspx?Kind=3"

# A small number of public Highpoint PDFs are indexed only under their stable
# file URL, not the newer Download.ashx catalog.  Keep the exact public PDF as
# provenance instead of treating the search-result text as an answer.
SUPPLEMENTAL_REFERENCE_ANSWERS = (
    {
        "year": 100,
        "exam_name": "特考三等-司法官第二試",
        "exam_kind": "judicial",
        "subject": "憲法與行政法",
        "subject_key": "constitutional",
        "url": "https://lawyer.get.com.tw/file/Paper/Lawyer/L3-42.pdf",
        "source": "高點法律網公開高分詳解",
    },
)

NEWS_IDS = {106: 3209, 107: 3589, 108: 3903, 109: 4174, 110: 4614, 111: 4841, 112: 6190, 113: 7454, 114: 7714}
RUBRIC_FILE_IDS = {
    108: {"constitutional": 9486, "civil_1": 9487, "civil_2": 9488, "criminal": 9489, "business": 9490, "labor": 9491, "tax": 9492, "maritime": 9493, "ip": 9494},
    109: {"constitutional": 10872, "civil_1": 10873, "civil_2": 10874, "criminal": 10875, "business": 10876, "labor": 10877, "tax": 10878, "maritime": 10879, "ip": 10880},
    110: {"constitutional": 12728, "civil_1": 12729, "civil_2": 12730, "criminal": 12731, "business": 12732, "ip": 12733, "labor": 12734, "tax": 12735, "maritime": 12736},
    111: {"constitutional": 13886, "civil_1": 13887, "civil_2": 13888, "criminal": 13889, "business": 13890, "ip": 13891, "labor": 13892, "tax": 13893, "maritime": 13894},
    112: {"constitutional": 15339, "civil_1": 15340, "civil_2": 15341, "criminal": 15342, "business": 15343, "ip": 15344, "labor": 15345, "tax": 15346, "maritime": 15347},
    113: {"constitutional": 18747, "civil_1": 18748, "civil_2": 18749, "criminal": 18750, "business": 18751, "ip": 18752, "labor": 18753, "tax": 18754, "maritime": 18755},
    114: {"constitutional": 20076, "civil_1": 20077, "civil_2": 20078, "criminal": 20079, "business": 20080, "ip": 20081, "labor": 20082, "tax": 20083, "maritime": 20084},
}

# The 106/107 announcements used the former sequential-attachment endpoint and
# kept distinct judicial-officer and lawyer documents. The original URLs are
# retained as provenance even when the current MOEX site no longer serves them.
OLD_RUBRIC_SERIALS = {
    106: {
        "judicial": {"constitutional": 1, "civil_1": 2, "civil_2": 3, "criminal": 4, "business_old": 5},
        "lawyer": {"constitutional": 6, "civil_1": 7, "civil_2": 8, "criminal": 9, "business": 10, "ip": 11, "labor": 12, "tax": 13, "maritime": 14},
    },
    107: {
        "judicial": {"constitutional": 1, "civil_1": 2, "civil_2": 3, "criminal": 4, "business_old": 5},
        "lawyer": {"constitutional": 6, "criminal": 7, "business": 8, "civil_1": 9, "civil_2": 10, "ip": 11, "labor": 12, "tax": 13, "maritime": 14},
    },
}

# These timestamps identify exact MOEX PDF responses preserved by the Internet
# Archive. They are retrieval routes only; the original MOEX URL remains the
# canonical provenance key stored in the archive manifest.
ARCHIVED_RUBRIC_REPLAYS = {
    (106, kind, key): "20180216234742"
    for kind, rows in OLD_RUBRIC_SERIALS[106].items()
    for key in rows
}

SUBJECT_LABELS = {
    "constitutional": "憲法與行政法",
    "civil": "民法與民事訴訟法",
    "civil_1": "民法與民事訴訟法(一)",
    "civil_2": "民法與民事訴訟法(二)",
    "criminal": "刑法與刑事訴訟法",
    "business": "公司法、保險法與證券交易法",
    "business_old": "商事法（公司法、保險法、票據法、證券交易法）",
    "ip": "智慧財產法",
    "labor": "勞動社會法",
    "tax": "財稅法",
    "maritime": "海商法與海洋法",
}
NUMERALS = "一二三四五六七八九十"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalized(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")))


def subject_key(label: str) -> str:
    value = normalized(label).replace("（", "(").replace("）", ")")
    if "國文" in value:
        return ""
    if "憲法" in value and "行政法" in value:
        return "constitutional"
    if "民法" in value and "民事訴訟" in value:
        if "(一)" in value or "（一）" in label or "民法部分" in value or "民法部份" in value:
            return "civil_1"
        if "(二)" in value or "（二）" in label or "民事訴訟法及民法" in value:
            return "civil_2"
        return "civil"
    if "刑法" in value and "刑事訴訟" in value:
        return "criminal"
    if "智慧財產" in value:
        return "ip"
    if "勞動社會" in value:
        return "labor"
    if "財稅" in value:
        return "tax"
    if "海商" in value and "海洋" in value:
        return "maritime"
    if "商事法" in value:
        return "business_old"
    if "公司法" in value and ("保險法" in value or "證券交易" in value):
        return "business"
    return ""


def exam_kind(title: str) -> str:
    has_judicial = "司法官" in title
    has_lawyer = "律師" in title
    if has_judicial and has_lawyer:
        return "combined"
    if has_judicial:
        return "judicial"
    return "lawyer"


class Builder:
    def __init__(self, output_root: Path, *, highpoint_delay: float = 4.0):
        self.output_root = output_root.resolve()
        self.archive_root = self.output_root / "archive"
        self.cache_root = self.output_root / "cache"
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.highpoint_rate_limited = False
        self.highpoint_delay = max(1.0, float(highpoint_delay))
        manifest_path = self.archive_root / "archive_manifest.json"
        if manifest_path.is_file():
            self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        else:
            self.manifest = {"schema_version": 1, "generated_at": now_iso(), "files": {}, "summary": {}}

    def fetch(self, url: str, *, params: dict | None = None) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                response = self.session.get(url, params=params, timeout=60, allow_redirects=True)
                if response.status_code == 429:
                    if "get.com.tw" in response.url and attempt >= 1:
                        self.highpoint_rate_limited = True
                        raise RuntimeError(f"public model-answer source rate limited: {response.url}")
                    time.sleep(min(25, 10 + attempt * 5))
                    continue
                response.raise_for_status()
                return response
            except Exception as exc:
                if "public model-answer source rate limited" in str(exc):
                    raise
                last_error = exc
                time.sleep(1.5 + attempt * 2)
        raise RuntimeError(f"download failed: {url}: {last_error}")

    def archive_pdf(
        self,
        url: str,
        *,
        category: str,
        stem: str,
        metadata: dict,
        retrieval_url: str = "",
    ) -> Path:
        existing = self.manifest["files"].get(url)
        if isinstance(existing, dict):
            path = self.archive_root / str(existing.get("relative_path") or "")
            if path.is_file() and path.stat().st_size > 1_000:
                return path
        if "get.com.tw" in url and self.highpoint_rate_limited:
            raise RuntimeError("public model-answer source rate limited for this build; deferred")
        response = self.fetch(retrieval_url or url)
        content = response.content
        if not content.startswith(b"%PDF"):
            raise RuntimeError(
                f"not a PDF: {retrieval_url or url} "
                f"({response.headers.get('content-type')}, {len(content)} bytes)"
            )
        digest = hashlib.sha256(content).hexdigest()
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-")[:120]
        relative = f"{category}/{safe_stem}-{digest[:12]}.pdf"
        path = self.archive_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".pdf.partial")
        temporary.write_bytes(content)
        temporary.replace(path)
        self.manifest["files"][url] = {
            "status": "saved", "relative_path": relative, "sha256": digest,
            "bytes": len(content), "content_type": "application/pdf",
            "saved_at": now_iso(), "category": category, **metadata,
        }
        if retrieval_url:
            self.manifest["files"][url].update({
                "retrieval_url": retrieval_url,
                "archived_source": True,
                "source_status": "official_attachment_removed_from_current_site",
            })
        self.write_manifest()
        time.sleep(self.highpoint_delay if "get.com.tw" in url else 0.25)
        return path

    def write_manifest(self) -> None:
        saved = [item for item in self.manifest["files"].values() if item.get("status") == "saved"]
        self.manifest["summary"] = {
            "saved": len(saved),
            "pending": sum(1 for item in self.manifest["files"].values() if item.get("status") != "saved"),
            "bytes": sum(int(item.get("bytes") or 0) for item in saved),
        }
        path = self.archive_root / "archive_manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.partial")
        temporary.write_text(json.dumps(self.manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)


def pdf_text(path: Path) -> str:
    archive_root = next((parent for parent in path.parents if parent.name == "archive"), None)
    cache_path = (
        archive_root.parent / "cache" / "pdf-text" / f"{path.stem}.txt"
        if archive_root is not None else None
    )
    if cache_path is not None and cache_path.is_file():
        return cache_path.read_text(encoding="utf-8")
    result = subprocess.run(["pdftotext", "-layout", str(path), "-"], check=True, capture_output=True, text=True)
    text = unicodedata.normalize("NFKC", result.stdout).replace("\x0c", "\n<<<PAGE_BREAK>>>\n")
    if len(normalized(text.replace("<<<PAGE_BREAK>>>", ""))) < 80:
        # The 106 MOEX scoring documents are image-only PDFs.  OCR is part of
        # the offline curation step, never part of a live MAGI grading request.
        with tempfile.TemporaryDirectory(prefix="magi-exam-ocr-") as temporary:
            directory = Path(temporary)
            subprocess.run(
                ["pdftoppm", "-r", "180", "-png", str(path), "page"],
                cwd=directory,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            pages: list[str] = []
            for image_path in sorted(directory.glob("page-*.png")):
                ocr = subprocess.run(
                    ["tesseract", image_path.name, "stdout", "-l", "chi_tra+eng", "--psm", "6"],
                    cwd=directory,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                pages.append(ocr.stdout.strip())
            text = "\n<<<PAGE_BREAK>>>\n".join(pages)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(".txt.partial")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(cache_path)
    return text


def split_top_level_questions(text: str) -> list[str]:
    matches = list(re.finditer(rf"(?m)^\s*([{NUMERALS}]+)、", text))
    starts: list[int] = []
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else min(len(text), match.start() + 3500)
        probe = text[match.start():next_start]
        if (
            re.search(r"[（(]\s*\d{1,3}(?:\.\d+)?\s*分\s*[）)]", probe)
            or re.search(r"命題意旨|答題關鍵|【\s*擬\s*答\s*】", probe)
        ):
            starts.append(match.start())
    starts = sorted(set(starts))
    if not starts:
        return []
    return [text[start:(starts[index + 1] if index + 1 < len(starts) else len(text))].replace("<<<PAGE_BREAK>>>", "").strip() for index, start in enumerate(starts)]


def split_rubric_questions(text: str) -> list[str]:
    matches = list(re.finditer(
        rf"(?m)^\s*第\s*([{NUMERALS}]+)\s*題(?:\s*(?:評分要點|試題解析與評分重點說明))?\s*$",
        text,
    ))
    starts: list[int] = []
    seen: set[str] = set()
    for match in matches:
        if match.group(1) not in seen:
            seen.add(match.group(1)); starts.append(match.start())
    return [text[start:(starts[index + 1] if index + 1 < len(starts) else len(text))].replace("<<<PAGE_BREAK>>>", "").strip() for index, start in enumerate(starts)]


def align_reference_questions(text: str, questions: list[str]) -> list[str]:
    """Align each official question with the matching problem statement in a model answer.

    Model-answer PDFs repeat Chinese numeral headings inside their answers, so
    simply taking the nth heading silently shifts later questions.  Alignment
    instead compares the official question text with every same-number heading,
    then locks the section boundaries only after all best matches are known.
    """
    headings = list(re.finditer(rf"(?m)^\s*([{NUMERALS}]+)、", text))
    selected: list[int | None] = []
    cursor = 0
    for question in questions:
        number_match = re.match(rf"\s*([{NUMERALS}]+)、", question)
        number = number_match.group(1) if number_match else ""
        question_key = normalized(question)[:600]
        best_position: int | None = None
        best_score = 0.0
        for heading in headings:
            if heading.start() < cursor or (number and heading.group(1) != number):
                continue
            probe = normalized(text[heading.start():heading.start() + max(2400, len(question) * 2)])[:1800]
            exact = next((size for size in (160, 120, 80, 50, 35) if question_key[:size] in probe), 0)
            if exact:
                exact_position = probe.find(question_key[:exact])
                score = 3.0 + exact / 200.0 - min(1.0, max(0, exact_position) / 1000.0)
            else:
                match = SequenceMatcher(None, question_key[:450], probe[:900], autojunk=False).find_longest_match()
                score = match.size / max(1, min(450, len(question_key)))
            if score > best_score:
                best_score = score
                best_position = heading.start()
        if best_position is None or best_score < 0.10:
            selected.append(None)
            continue
        selected.append(best_position)
        cursor = best_position + 1

    sections: list[str] = []
    for index, start in enumerate(selected):
        if start is None:
            sections.append("")
            continue
        end = next((position for position in selected[index + 1:] if position is not None), len(text))
        sections.append(text[start:end].replace("<<<PAGE_BREAK>>>", "").strip())
    return sections


def question_score_parts(section: str) -> list[dict]:
    values = []
    for index, match in enumerate(re.finditer(r"[（(]\s*(\d{1,3}(?:\.\d+)?)\s*分\s*[）)]", section), start=1):
        points = float(match.group(1))
        if 0 < points <= 300:
            values.append({"id": f"P{index}", "label": f"第 {index} 小題", "max_score": int(points) if points.is_integer() else points, "official_excerpt": match.group(0), "source": "official_question"})
    return values


def question_score(section: str, fallback: int = 50) -> tuple[int | float, list[dict]]:
    parts = question_score_parts(section)
    total = sum(float(item["max_score"]) for item in parts)
    if total <= 0:
        total = fallback
    return (int(total) if float(total).is_integer() else total), parts


def clean_issue_label(text: str) -> str:
    value = re.sub(rf"^\s*(?:第?[（(]?[{NUMERALS}0-9]+[)）、.．]?\s*(?:子題)?)", "", text).strip()
    value = re.sub(r"\s+", " ", value)
    value = re.split(r"[。；;]", value, maxsplit=1)[0]
    return value[:80] or "依來源列示之爭點"


def rubric_candidates(text: str, *, official: bool) -> list[tuple[str, str]]:
    if official:
        marker = re.search(r"【\s*評分要點\s*】", text)
        body = text[marker.end():] if marker else text
        body = re.split(r"【\s*閱卷委員的話\s*】", body, maxsplit=1)[0]
    else:
        marker = re.search(r"擬\s*答", text)
        body = text[marker.end():] if marker else text
    line_matches = list(re.finditer(rf"(?m)^\s*((?:第\s*)?[（(]?[{NUMERALS}0-9]+[)）、.．](?:\s*子題)?\s*)([^\n]{{4,}})", body))
    candidates: list[tuple[str, str]] = []
    for index, match in enumerate(line_matches):
        start = match.start(2)
        end = line_matches[index + 1].start() if index + 1 < len(line_matches) else min(len(body), start + 900)
        excerpt = body[start:end].strip()
        compact = re.sub(r"\s+", " ", excerpt)
        if official and re.match(r"^(若|如僅|概略|論述.*不完全|未有完全|仍將|則給予)", compact):
            continue
        label = clean_issue_label(match.group(2))
        if len(label) < 3 or any(label == old for old, _ in candidates):
            continue
        candidates.append((label, excerpt[:520].strip()))
        if len(candidates) >= (60 if official else 16):
            break
    if not candidates:
        paragraphs = [item.strip() for item in re.split(r"\n\s*\n", body) if len(normalized(item)) >= 30]
        for paragraph in paragraphs[:6]:
            candidates.append((clean_issue_label(paragraph), paragraph[:520]))
    return candidates


def allocated_points(total: float, count: int) -> list[float]:
    if count <= 0:
        return []
    unit = math.floor(total / count * 2) / 2
    values = [unit] * count
    values[-1] = round(total - sum(values[:-1]), 1)
    return values


def stored_rubric(section: str, *, basis: str, max_score: float, source_url: str, source_sha256: str) -> dict | None:
    official = basis == "official"
    candidates = rubric_candidates(section, official=official)
    if not candidates:
        return None
    points = [None] * len(candidates) if official else allocated_points(max_score, len(candidates))
    issues = []
    scoring = []
    structures = []
    for index, ((label, excerpt), point) in enumerate(zip(candidates, points), start=1):
        issue_id = f"I{index}"
        issue = {
            "id": issue_id, "issue": label,
            "rubric_source": "official" if official else "reference_derived",
            "rule": excerpt, "trigger_facts": [], "application_targets": [], "common_traps": [],
        }
        if official:
            issue.update({"official_excerpt": excerpt, "official_points": None})
            scoring.append({"item": label, "source": "official", "official_excerpt": excerpt, "points": None})
        else:
            issue.update({"source_excerpt": excerpt, "points": point, "official_points": point, "allocation_source": "offline_reference_derived"})
            scoring.append({"item": label, "source": "reference_derived", "source_excerpt": excerpt, "points": point})
        issues.append(issue)
        structures.append({"heading": label, "purpose": "先提出規範，再連結題目事實並作小結。", "issue_ids": [issue_id], "points": ["規範", "涵攝", "結論"]})
    return {
        "schema_version": 2,
        "curator": "離線來源校訂",
        "curated_at": now_iso(),
        "curation_method": "verbatim_official_scoring_points" if official else "reference_answer_outline_with_fixed_allocation",
        "source_sha256": source_sha256,
        "problem_overview": candidates[0][0],
        "official_rubric": {
            "provided": official, "source_url": source_url,
            "summary": "考選部官方評分要點逐字建檔" if official else "依封存公開擬答預先整理之練習評分尺（非官方）",
            "numeric_scoring_available": not official,
            "total_points_found": max_score if not official else None,
            "alignment_note": "官方未明列逐爭點數字，不另行配分。" if official else "配分由離線編輯器在官方題卷本題總分內預先配置，非考選部配分。",
        },
        "issues": issues, "ideal_structure": structures, "scoring_rubric": scoring,
        "alternative_views": [],
        "cautions": [
            "只使用考選部官方評分要點原文；未明列逐爭點數字時不虛構配分。"
            if official else
            "本評分尺依封存公開擬答預先整理，非考選部或補習班公布之正式配分；MAGI 不得修改。"
        ],
        "confidence": "高" if official else "中",
    }


def discover_exams(builder: Builder, year: int) -> list[dict]:
    response = builder.fetch(MOEX_INDEX, params={"y": year + 1911})
    soup = BeautifulSoup(response.text, "html.parser")
    found = []
    for option in soup.select("option[value]"):
        title = option.get_text(" ", strip=True)
        code = str(option.get("value") or "").strip()
        if code and "第二試" in title and ("司法官" in title or "律師" in title) and "第一試" not in title:
            found.append({"code": code, "title": title, "exam_kind": exam_kind(title)})
    unique = {item["code"]: item for item in found}
    if not unique:
        raise RuntimeError(f"{year}: MOEX second-stage exam code not found")
    return list(unique.values())


def official_papers(builder: Builder, year: int, exam: dict) -> list[dict]:
    page_url = f"{MOEX_INDEX}?e={exam['code']}&y={year + 1911}"
    soup = BeautifulSoup(builder.fetch(page_url).text, "html.parser")
    papers = []
    seen: set[str] = set()
    for label in soup.select("label.exam-title"):
        subject = label.get_text(" ", strip=True)
        key = subject_key(subject)
        if not key:
            continue
        row = label.find_parent("tr")
        anchor = next((a for a in (row.select("a[href]") if row else []) if "wHandExamQandA_File" in str(a.get("href")) and "t=Q" in str(a.get("href"))), None)
        if not anchor:
            continue
        url = urljoin(page_url, str(anchor.get("href")))
        if url in seen:
            continue
        seen.add(url)
        papers.append({"subject": subject, "subject_key": key, "question_url": url, "official_page_url": page_url, **exam})
    return papers


def highpoint_answers(builder: Builder) -> list[dict]:
    all_rows: dict[str, dict] = {}
    for term in ("律師、司法官第二試", "司法官第二試", "律師第二試"):
        print(f"[answers] indexing {term}", flush=True)
        empty_pages = 0
        term_seen: set[str] = set()
        for page in range(1, 81):
            response = builder.fetch(HIGHPOINT_LIST, params={"iPageNo": page, "sFilter": term, "sFilterType": 0})
            soup = BeautifulSoup(response.text, "html.parser")
            rows = []
            for tr in soup.select("tr"):
                cells = tr.find_all("td", recursive=False)
                if len(cells) < 5:
                    continue
                anchor = cells[4].find("a", href=True)
                if not anchor or "Download.ashx" not in str(anchor.get("href")):
                    continue
                year_text = cells[3].get_text(" ", strip=True)
                if not year_text.isdigit() or int(year_text) not in YEARS:
                    continue
                group = cells[1].get_text(" ", strip=True)
                subject = cells[2].get_text(" ", strip=True)
                key = subject_key(subject)
                if not key:
                    continue
                url = urljoin(response.url, str(anchor.get("href")))
                rows.append({"year": int(year_text), "exam_name": group, "exam_kind": exam_kind(group), "subject": subject, "subject_key": key, "url": url, "source": "高點法律網公開高分詳解"})
            page_urls = {item["url"] for item in rows}
            if rows and page_urls.issubset(term_seen):
                print(f"[answers] {term} page {page}: repeated final page; stop", flush=True)
                break
            term_seen.update(page_urls)
            for item in rows:
                all_rows[item["url"]] = item
            if rows:
                print(f"[answers] {term} page {page}: {len(rows)} rows", flush=True)
            empty_pages = empty_pages + 1 if not rows else 0
            if empty_pages >= 2:
                break
    return list(all_rows.values())


def compatible_answer(paper: dict, answer: dict) -> bool:
    if paper["year"] != answer["year"] or paper["subject_key"] != answer["subject_key"]:
        return False
    if paper["exam_kind"] == "combined" or answer["exam_kind"] == "combined":
        return True
    return paper["exam_kind"] == answer["exam_kind"]


def discover_official_rubric_sources(builder: Builder) -> dict[int, dict]:
    """Capture future scoring-point announcements from the official RSS feed.

    The feed is short-lived, so every scheduled run merges discoveries into a
    persistent cache.  Once a news page is seen, its exact attachment URLs are
    retained even after the item falls out of the feed.
    """
    cache_path = builder.output_root / "rubric_sources.json"
    if cache_path.is_file():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            payload = {"schema_version": 1, "years": {}}
    else:
        payload = {"schema_version": 1, "years": {}}
    rows = payload.get("years") if isinstance(payload.get("years"), dict) else {}
    try:
        response = builder.fetch(MOEX_NEWS_RSS)
        soup = BeautifulSoup(response.content, "xml")
        for item in soup.find_all("item"):
            title = item.title.get_text(" ", strip=True) if item.title else ""
            link = item.link.get_text(" ", strip=True) if item.link else ""
            match = re.search(r"(\d{3})年.*司法官.*律師.*第二試.*評分要點", title)
            if not match or not link:
                continue
            year = int(match.group(1))
            if year not in YEARS:
                continue
            page = BeautifulSoup(builder.fetch(link).text, "html.parser")
            files: dict[str, str] = {}
            for anchor in page.select("a[href*='wHandEditorExtend_File.ashx']"):
                descriptor = " ".join((str(anchor.get("title") or ""), anchor.get_text(" ", strip=True)))
                key = subject_key(descriptor)
                if key:
                    files[key] = urljoin(link, str(anchor.get("href") or ""))
            if files:
                news_id_match = re.search(r"news_id=(\d+)", link)
                rows[str(year)] = {
                    "title": title,
                    "news_url": link,
                    "news_id": int(news_id_match.group(1)) if news_id_match else None,
                    "files": files,
                    "discovered_at": now_iso(),
                }
    except Exception as exc:
        # Known mappings and already-captured feed items remain usable during a
        # transient RSS outage; question discovery still proceeds independently.
        print(f"WARN MOEX rubric RSS discovery failed: {exc}", file=sys.stderr)
    payload.update({"schema_version": 1, "updated_at": now_iso(), "years": rows})
    temporary = cache_path.with_suffix(".json.partial")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(cache_path)
    discovered = {int(year): dict(value) for year, value in rows.items() if str(year).isdigit() and isinstance(value, dict)}
    DISCOVERED_RUBRICS.clear()
    DISCOVERED_RUBRICS.update(discovered)
    return discovered


def official_news_url(year: int) -> str:
    dynamic = DISCOVERED_RUBRICS.get(year) or {}
    if dynamic.get("news_url"):
        return str(dynamic["news_url"])
    news_id = NEWS_IDS.get(year)
    return (
        f"https://wwwc.moex.gov.tw/main/news/wfrmNews.aspx?kind=3&menu_id=42&news_id={news_id}"
        if news_id else ""
    )


def official_rubric_descriptor(year: int, kind: str, key: str) -> dict | None:
    dynamic = DISCOVERED_RUBRICS.get(year) or {}
    dynamic_files = dynamic.get("files") if isinstance(dynamic.get("files"), dict) else {}
    if dynamic_files.get(key):
        return {"url": str(dynamic_files[key]), "retrieval_url": ""}
    old_serial = OLD_RUBRIC_SERIALS.get(year, {}).get(kind, {}).get(key)
    if old_serial:
        url = (
            "https://wwwc.moex.gov.tw/main/news/wHandNews_File.ashx"
            f"?news_id={NEWS_IDS[year]}&serial_no={old_serial}"
        )
        timestamp = ARCHIVED_RUBRIC_REPLAYS.get((year, kind, key), "")
        retrieval_url = ""
        if timestamp:
            archived_original = url.replace("https://", "http://", 1)
            retrieval_url = f"https://web.archive.org/web/{timestamp}id_/{archived_original}"
        return {"url": url, "retrieval_url": retrieval_url}

    file_id = RUBRIC_FILE_IDS.get(year, {}).get(key)
    if file_id:
        return {
            "url": (
                "https://wwwc.moex.gov.tw/main/controls/wHandEditorExtend_File.ashx"
                f"?Fun=News&menu_id=42&item_id={NEWS_IDS[year]}&file_id={file_id}"
            ),
            "retrieval_url": "",
        }
    return None


def build(output_root: Path, *, highpoint_delay: float = 4.0) -> dict:
    builder = Builder(output_root, highpoint_delay=highpoint_delay)
    output_root.mkdir(parents=True, exist_ok=True)
    discover_official_rubric_sources(builder)
    entries: list[dict] = []
    documents: list[dict] = []
    paper_rows: list[dict] = []
    papers_cache = output_root / "moex_papers.json"
    if papers_cache.is_file():
        paper_rows = json.loads(papers_cache.read_text(encoding="utf-8"))
        for item in paper_rows:
            item["subject_key"] = subject_key(item.get("subject") or "")
        print(f"[questions] loaded cached MOEX index: {len(paper_rows)} subject PDFs", flush=True)
    cached_paper_years = {int(item.get("year") or 0) for item in paper_rows}
    for year in YEARS:
        if year in cached_paper_years:
            continue
        print(f"[{year}] discovering MOEX second-stage exams", flush=True)
        try:
            for exam in discover_exams(builder, year):
                for paper in official_papers(builder, year, exam):
                    paper["year"] = year
                    paper_rows.append(paper)
        except RuntimeError as exc:
            # Before the annual second stage, absence is an expected pending
            # state rather than a reason to destroy the prior runtime overlay.
            print(f"[{year}] pending: {exc}", file=sys.stderr)
    papers_cache.write_text(json.dumps(paper_rows, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    paper_rows = [item for item in paper_rows if int(item.get("year") or 0) in YEARS]

    answers_cache = output_root / "highpoint_answers.json"
    if answers_cache.is_file():
        answers = json.loads(answers_cache.read_text(encoding="utf-8"))
        for item in answers:
            item["subject_key"] = subject_key(item.get("subject") or "")
        print(f"[answers] loaded cached index: {len(answers)} subject PDFs", flush=True)
    else:
        answers = []
    # Search the public model-answer index only for years that have questions,
    # lack complete official scoring documents and are not yet represented in
    # the cached answer index.  An empty current-year result is retried by the
    # next scheduled run, which is how newly published model answers arrive.
    cached_answer_years = {int(item.get("year") or 0) for item in answers}
    needs_reference_years = {
        int(paper["year"]) for paper in paper_rows
        if not official_rubric_descriptor(int(paper["year"]), str(paper["exam_kind"]), str(paper["subject_key"]))
    }
    if any(year not in cached_answer_years for year in needs_reference_years):
        print("[answers] discovering public Highpoint model-answer PDFs", flush=True)
        discovered_answers = highpoint_answers(builder)
        by_url = {str(item.get("url") or ""): item for item in answers if str(item.get("url") or "")}
        by_url.update({str(item.get("url") or ""): item for item in discovered_answers if str(item.get("url") or "")})
        answers = list(by_url.values())
        answers_cache.write_text(json.dumps(answers, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    known_answer_urls = {str(item.get("url") or "") for item in answers}
    answers.extend(dict(item) for item in SUPPLEMENTAL_REFERENCE_ANSWERS if item["url"] not in known_answer_urls)
    answers = [item for item in answers if int(item.get("year") or 0) in YEARS]
    print(f"[answers] discovered {len(answers)} subject PDFs for selected years", flush=True)

    # Deduplicate combined-year repeated papers while preserving the distinct
    # judicial and lawyer papers used before the unified second stage.
    unique_papers: dict[tuple, dict] = {}
    for paper in paper_rows:
        key = (paper["year"], paper["exam_kind"], paper["subject_key"])
        # Later lawyer pages repeat the common subjects once for every chosen
        # elective group.  They are the same subject paper, so expose one copy.
        unique_papers.setdefault(key, paper)
    print(f"[questions] collapsed repeated elective-track links: {len(paper_rows)} -> {len(unique_papers)} subject PDFs", flush=True)

    def work_priority(paper: dict) -> tuple:
        candidates = [item for item in answers if compatible_answer(paper, item)]
        answer = candidates[0] if candidates else None
        archived = bool(answer and builder.manifest["files"].get(answer["url"], {}).get("status") == "saved")
        descriptor = official_rubric_descriptor(paper["year"], paper["exam_kind"], paper["subject_key"])
        official_archived = bool(descriptor and builder.manifest["files"].get(descriptor["url"], {}).get("status") == "saved")
        # Resume missing reference material first when no usable official rubric
        # is already archived.  This keeps an interrupted/rate-limited refresh
        # moving forward instead of redownloading or re-OCRing old material.
        missing_required_reference = bool(answer and not archived and not official_archived)
        return (0 if missing_required_reference else 1, paper["year"], paper["exam_kind"], paper["subject_key"])

    for paper in sorted(unique_papers.values(), key=work_priority):
        year = paper["year"]
        key = paper["subject_key"]
        kind = paper["exam_kind"]
        label = paper["subject"]
        print(f"[{year}] {kind} {label}: official question", flush=True)
        question_path = builder.archive_pdf(
            paper["question_url"], category="moex/questions", stem=f"{year}-{kind}-{key}-question",
            metadata={"year": year, "exam_kind": kind, "subject": label, "document_kind": "question"},
        )
        question_text = pdf_text(question_path)
        question_sections = split_top_level_questions(question_text)
        if not question_sections:
            print(f"WARN {year} {kind} {label}: unable to split; keeping whole paper", file=sys.stderr)
            question_sections = [question_text.replace("<<<PAGE_BREAK>>>", "\n")]
        question_sha = builder.manifest["files"][paper["question_url"]]["sha256"]

        rubric_sections: list[str] = []
        rubric_url = ""
        rubric_path: Path | None = None
        rubric_descriptor = official_rubric_descriptor(year, kind, key)
        if rubric_descriptor:
            rubric_url = rubric_descriptor["url"]
            try:
                rubric_path = builder.archive_pdf(
                    rubric_url,
                    category="moex/rubrics",
                    stem=f"{year}-{kind}-{key}-official-rubric",
                    metadata={
                        "year": year, "exam_kind": kind, "subject": label,
                        "document_kind": "official_rubric",
                        "official_news_url": official_news_url(year),
                    },
                    retrieval_url=rubric_descriptor.get("retrieval_url") or "",
                )
                rubric_sections = split_rubric_questions(pdf_text(rubric_path))
            except Exception as exc:
                # A removed official attachment must never abort the complete
                # bank.  Its questions remain visible and fall back to a
                # precompiled reference rubric until the exact file is found.
                rubric_path = None
                rubric_sections = []
                print(f"WARN official rubric unavailable {year} {kind} {label}: {exc}", file=sys.stderr)

        candidates = [item for item in answers if compatible_answer(paper, item)]
        answer_item = candidates[0] if candidates else None
        reference_sections: list[str] = []
        answer_path: Path | None = None
        if answer_item:
            try:
                answer_path = builder.archive_pdf(
                    answer_item["url"], category="references/highpoint", stem=f"{year}-{kind}-{key}-highpoint",
                    metadata={"year": year, "exam_kind": kind, "subject": label, "document_kind": "reference_answer", "source_name": answer_item["source"]},
                )
                reference_sections = align_reference_questions(pdf_text(answer_path), question_sections)
            except Exception as exc:
                print(f"WARN reference download/split failed {year} {kind} {label}: {exc}", file=sys.stderr)

        for index, question in enumerate(question_sections, start=1):
            max_score, score_parts = question_score(question)
            suffix = f"-{kind}" if kind != "combined" else ""
            uid = f"moex-{year}{suffix}-{key}-q{index}"
            official_section = rubric_sections[index - 1] if index <= len(rubric_sections) else ""
            reference_section = reference_sections[index - 1] if index <= len(reference_sections) else ""
            basis = "official" if official_section else ("reference_derived" if reference_section else "pending_reference")
            source_section = official_section or reference_section
            source_url = rubric_url if official_section else (answer_item["url"] if reference_section and answer_item else "")
            source_sha = (
                builder.manifest["files"][rubric_url]["sha256"] if official_section
                else builder.manifest["files"][answer_item["url"]]["sha256"] if reference_section and answer_item
                else ""
            )
            locked = stored_rubric(source_section, basis=basis, max_score=float(max_score), source_url=source_url, source_sha256=source_sha) if basis != "pending_reference" else None
            entry = {
                "uid": uid, "year": year,
                "exam_name": "司法官／律師第二試" if kind == "combined" else ("司法官第二試" if kind == "judicial" else "律師第二試"),
                "exam_family": "judicial_bar", "subject_key": key,
                "subject": SUBJECT_LABELS.get(key, label), "question_number": index,
                "title": f"{year} 年 {SUBJECT_LABELS.get(key, label)}・第 {index} 題" + ("（司法官）" if kind == "judicial" else "（律師）" if kind == "lawyer" else ""),
                "max_score": max_score, "score_parts": score_parts,
                "question_text": question, "question_url": paper["question_url"],
                "question_archive_sha256": question_sha,
                "official_rubric_text": official_section,
                "official_rubric_url": rubric_url if official_section else "",
                "official_rubric_original_url": rubric_url,
                "official_rubric_announced": bool(rubric_descriptor),
                "official_attachment_status": (
                    "saved" if official_section else
                    "announced_but_original_attachment_unavailable" if rubric_descriptor else
                    "not_published"
                ),
                "official_news_url": official_news_url(year) if rubric_descriptor else "",
                "official_numeric_scoring": False,
                "official_numeric_note": "考選部評分要點未明列逐爭點數字；不另行虛構官方配分。" if official_section else "",
                "rubric_basis": basis,
                "reference_answer_text": reference_section,
                "reference_answer_url": answer_item["url"] if reference_section and answer_item else "",
                "reference_answer_source": answer_item["source"] if reference_section and answer_item else "",
                "stored_rubric": locked,
                "source_authority": "moex", "institution": "中華民國考選部",
            }
            entries.append(entry)
        documents.append({"year": year, "subject": label, "exam_kind": kind, "kind": "question", "url": paper["question_url"], "sha256": question_sha})
        if rubric_path:
            documents.append({"year": year, "subject": label, "exam_kind": kind, "kind": "official_rubric", "url": rubric_url, "sha256": builder.manifest["files"][rubric_url]["sha256"]})
        if answer_path and answer_item:
            documents.append({"year": year, "subject": label, "exam_kind": kind, "kind": "reference_answer", "url": answer_item["url"], "sha256": builder.manifest["files"][answer_item["url"]]["sha256"], "source": answer_item["source"]})

    # Keep runtime years that were not present on today's official index.  This
    # prevents a temporary MOEX outage or a pre-exam check from erasing an
    # already verified overlay.
    existing_path = output_root / "essay_bank.json"
    refreshed_years = {int(item.get("year") or 0) for item in paper_rows}
    if existing_path.is_file():
        try:
            existing_payload = json.loads(existing_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            existing_payload = {}
        preserved_entries = [
            dict(item) for item in existing_payload.get("entries") or []
            if isinstance(item, dict) and int(item.get("year") or 0) not in refreshed_years
        ]
        preserved_documents = [
            dict(item) for item in existing_payload.get("documents") or []
            if isinstance(item, dict) and int(item.get("year") or 0) not in refreshed_years
        ]
        entries = preserved_entries + entries
        documents = preserved_documents + documents

    subjects = sorted({(item["subject_key"], item["subject"]) for item in entries})
    payload_years = sorted({int(item["year"]) for item in entries}, reverse=True)
    payload = {
        "schema_version": 2, "source_authority": "中華民國考選部",
        "source_policy": "MOEX questions are authoritative. Official scoring points take priority. Where absent, a fixed offline compiler prepares a locked, non-official rubric from an archived public model answer; MAGI cannot create or alter it at review time.",
        "generated_at": now_iso(), "updated_through_year": max(payload_years) if payload_years else None,
        "years": payload_years,
        "subjects": [{"key": key, "label": label} for key, label in subjects],
        "entries": entries, "documents": documents,
        "coverage": {
            "entry_count": len(entries),
            "grading_ready": sum(bool(item.get("stored_rubric")) for item in entries),
            "official_rubric": sum(item.get("rubric_basis") == "official" and bool(item.get("stored_rubric")) for item in entries),
            "reference_derived": sum(item.get("rubric_basis") == "reference_derived" and bool(item.get("stored_rubric")) for item in entries),
            "pending_reference": sum(not bool(item.get("stored_rubric")) for item in entries),
        },
    }
    output_root.mkdir(parents=True, exist_ok=True)
    temporary_bank = (output_root / "essay_bank.json").with_suffix(".json.partial")
    temporary_bank.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    weight_builder = Path(__file__).resolve().parent / "ops" / "build_exam_practice_weights.py"
    weight_output = output_root / "curated_practice_weights.json"
    temporary_weight_output = output_root / "curated_practice_weights.json.next"
    weight_result = subprocess.run(
        [
            sys.executable,
            str(weight_builder),
            "--bank", str(temporary_bank),
            "--output", str(temporary_weight_output),
            "--previous", str(weight_output),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if weight_result.returncode != 0:
        raise RuntimeError(f"practice-weight compiler failed: {weight_result.stderr[-800:]}")
    weight_payload = json.loads(temporary_weight_output.read_text(encoding="utf-8"))
    official_without_points = sum(
        item.get("rubric_basis") == "official"
        and bool(item.get("stored_rubric"))
        and not bool((item.get("stored_rubric") or {}).get("official_rubric", {}).get("numeric_scoring_available"))
        for item in entries
    )
    if len(weight_payload.get("entries") or {}) != official_without_points:
        raise RuntimeError("practice-weight compiler did not cover every official rubric")
    payload["coverage"]["fixed_practice_scoring"] = len(weight_payload.get("entries") or {})
    temporary_bank.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary_bank.replace(output_root / "essay_bank.json")
    temporary_weight_output.replace(weight_output)
    builder.write_manifest()
    return payload


def main() -> None:
    global YEARS
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--highpoint-delay", type=float, default=4.0)
    parser.add_argument(
        "--years",
        help="Comma-separated ROC years. Scheduled updates normally pass only the newly published year.",
    )
    args = parser.parse_args()
    if args.years:
        try:
            selected = tuple(sorted({int(value.strip()) for value in args.years.split(",") if value.strip()}))
        except ValueError as exc:
            parser.error(f"invalid ROC year list: {exc}")
        if not selected or any(year < 100 or year > 200 for year in selected):
            parser.error("ROC years must be between 100 and 200")
        YEARS = selected
    payload = build(args.output_root, highpoint_delay=args.highpoint_delay)
    print(json.dumps({"output_root": str(args.output_root.resolve()), "years": payload["years"], **payload["coverage"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
