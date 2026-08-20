#!/usr/bin/env python3
"""Expand MAGI's official exam catalog to every MOE-listed law institution.

The registry is complete; individual papers are added only from official pages.
Third-party model-answer catalogs are links, never copied into the data file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup


MOE_URL = "https://udb.moe.edu.tw/ulist/ISCED/042"
NCKU_URL = "https://exam.lib.ncku.edu.tw/master_subject.php?department_code=HC02"
CCU_URL = "https://deptflaw.ccu.edu.tw/p/404-1125-15590.php?Lang=zh-tw"
NCCU_ARCHIVE = "https://ah.lib.nccu.edu.tw/department?collection=%E8%80%83%E5%8F%A4%E9%A1%8C&department=%E6%B3%95%E5%BE%8B%E5%AD%B8%E7%B3%BB&locale=zh_TW"
HIGHPOINT_ANSWER_CATALOG = "https://publish.get.com.tw/event/Law-institute-lawdata/"
HIGHPOINT_CURRENT_CATALOG = "https://publish.get.com.tw/Event/lawyer-master/"


def institution(name: str, programs: list[str], *, archive_url: str = "", status: str = "no_public_archive_verified", notes: str = "", answer_catalog: bool = False) -> dict:
    return {
        "name": name,
        "programs": programs,
        "official_archive_url": archive_url,
        "coverage_status": status,
        "notes": notes,
        "reference_answer_status": "catalog_available_not_ingested" if answer_catalog else "not_publicly_verified",
        "reference_answer_catalog_url": HIGHPOINT_ANSWER_CATALOG if answer_catalog else "",
        "reference_answer_source": "高點法研所歷屆經典試題解析" if answer_catalog else "",
    }


INSTITUTIONS = [
    institution("國立政治大學", ["法律學系", "法律科際整合研究所", "法學院碩士在職專班"], archive_url=NCCU_ARCHIVE, status="direct_papers", answer_catalog=True),
    institution("國立清華大學", ["科技法律研究所"], archive_url="https://www.lst.nthu.edu.tw/NationalTsingHuaUniversityScienceandTechnologyLaw/cate/%E6%8B%9B%E7%94%9F%E5%85%AC%E5%91%8A/", status="official_admissions_page_only"),
    institution("國立臺灣大學", ["法律學系", "科際整合法律研究所", "事業經營法務碩士在職專班"], archive_url="https://exam.lib.ntu.edu.tw/graduate/term/323%20319%2063%2058%2061%2059%2062%2060%2065%2064%2066", status="direct_papers", answer_catalog=True),
    institution("國立成功大學", ["法律學系"], archive_url=NCKU_URL, status="direct_papers", answer_catalog=True),
    institution("國立中興大學", ["法律學系"], archive_url="https://law.nchu.edu.tw/front/Admissions/g24Admissions/news.php?ID=4361422a63d48550e9d9675c90338c4306e780238a868d9fe1d0e4c2837e533f", status="official_admissions_page_only"),
    institution("國立陽明交通大學", ["科技法律研究所"], status="no_public_archive_verified"),
    institution("國立中央大學", ["法律與政府研究所"], archive_url="https://rapid.lib.ncu.edu.tw/cexamn/", status="official_archive_pending_index", notes="校方以年度壓縮檔提供；110 學年度標示無筆試。"),
    institution("國立臺灣海洋大學", ["海洋法律研究所", "海洋法政學士學位學程"], status="no_public_archive_verified"),
    institution("國立中正大學", ["法律學系", "財經法律學系", "法學院高階主管法律碩士在職專班"], archive_url=CCU_URL, status="direct_papers", answer_catalog=True),
    institution("國立臺北大學", ["法律學系", "法律專業研究所"], archive_url="https://library.ntpu.edu.tw/singlehtml/f72100f62a06421cb0dc3107c6970ec3?cntId=2eef132cc6b144c39b918a5701f7de77", status="official_archive_pending_index", answer_catalog=True),
    institution("國立高雄大學", ["法律學系", "政治法律學系", "財經法律學系", "法學院博士班"], status="official_papers_pending_index", answer_catalog=False),
    institution("國立東華大學", ["法律學系"], status="no_public_archive_verified"),
    institution("國立臺灣科技大學", ["專利研究所"], status="no_public_archive_verified"),
    institution("國立雲林科技大學", ["科技法律研究所"], status="no_public_archive_verified"),
    institution("國立臺北科技大學", ["智慧財產權研究所"], status="no_public_archive_verified"),
    institution("國立臺北教育大學", ["文教法律研究所"], status="no_public_archive_verified"),
    institution("國立金門大學", ["海洋與邊境管理學系"], status="no_public_archive_verified"),
    institution("國立高雄科技大學", ["科技法律研究所"], status="no_public_archive_verified"),
    institution("東海大學", ["法律學系"], status="no_public_archive_verified"),
    institution("輔仁大學", ["法律學系", "財經法律學系", "學士後法律學系"], status="no_public_archive_verified", answer_catalog=True),
    institution("東吳大學", ["法律學系"], archive_url="https://entrance.exam.scu.edu.tw/exam", status="official_archive_pending_index", answer_catalog=True),
    institution("中原大學", ["財經法律學系"], archive_url="https://cycuir.lib.cycu.edu.tw/handle/310900400/14583?locale=zh-TW", status="official_archive_pending_index", answer_catalog=True),
    institution("中國文化大學", ["法律學系"], archive_url="https://irlib.pccu.edu.tw/handle/987654321/33", status="official_archive_pending_index", answer_catalog=True),
    institution("逢甲大學", ["財經法律研究所"], status="no_public_archive_verified"),
    institution("靜宜大學", ["法律學系"], status="no_public_archive_verified"),
    institution("世新大學", ["法律學系"], archive_url="https://lib.shu.edu.tw/search_taskpaper.htm", status="official_archive_pending_index"),
    institution("銘傳大學", ["法律學系", "財金法律學系"], status="no_public_archive_verified"),
    institution("實踐大學", ["法律學系"], status="undergraduate_only", notes="教育部清冊未列碩士班。"),
    institution("真理大學", ["法律學系"], status="no_public_archive_verified"),
    institution("南臺科技大學", ["財經法律研究所"], status="no_public_archive_verified"),
    institution("臺北醫學大學", ["醫療暨生物科技法律研究所"], status="no_public_archive_verified"),
    institution("中國醫藥大學", ["科技法律碩士學位學程"], status="no_public_archive_verified"),
    institution("玄奘大學", ["法律學系"], status="no_public_archive_verified"),
    institution("亞洲大學", ["財經法律學系"], status="no_public_archive_verified"),
    institution("開南大學", ["法律學系"], status="no_public_archive_verified"),
    institution("僑光科技大學", ["財經法律系"], status="undergraduate_only", notes="教育部清冊未列碩士班。"),
    institution("中信金融管理學院", ["財經法律學系"], status="no_public_archive_verified"),
    institution("高雄市立空中大學", ["法律學系"], status="undergraduate_only", notes="教育部清冊未列碩士班。"),
]


def fetch(url: str) -> str:
    completed = subprocess.run(
        ["curl", "--fail", "--silent", "--show-error", "--location", "--max-time", "90", url],
        check=True,
        capture_output=True,
        text=True,
        timeout=100,
    )
    return completed.stdout


def paper_uid(prefix: str, year: int, subject: str, url: str) -> str:
    digest = hashlib.sha256(f"{prefix}|{year}|{subject}|{url}".encode()).hexdigest()[:16]
    return f"{prefix}-{year}-{digest}"


def answer_fields(institution_name: str) -> dict:
    covered = institution_name in {
        "國立臺灣大學", "國立政治大學", "國立臺北大學", "輔仁大學", "東吳大學",
        "中國文化大學", "中原大學", "國立中正大學", "國立成功大學",
    }
    return {
        "reference_answer_url": "",
        "reference_answer_source": "",
        "reference_answer_catalog_url": HIGHPOINT_ANSWER_CATALOG if covered else "",
        "reference_answer_catalog_source": "高點法研所歷屆經典試題解析（逐題內容未匯入）" if covered else "",
        "reference_answer_status": "catalog_only_not_grading_ready" if covered else "not_publicly_verified",
    }


def base_paper(*, uid: str, year: int, institution_name: str, exam_name: str, track: str, subject: str, question_url: str, official_page_url: str) -> dict:
    row = {
        "uid": uid,
        "source_authority": "university_official",
        "source_tier": "official_question",
        "exam_family": "law_graduate_school",
        "year": year,
        "gregorian_year": year + 1911,
        "exam_name": exam_name,
        "level": "碩士班",
        "track": track,
        "institution": institution_name,
        "subject": subject,
        "paper_type": "essay_or_mixed",
        "question_url": question_url,
        "question_link_type": "official_pdf",
        "official_answer_url": "",
        "official_correction_url": "",
        "official_page_url": official_page_url,
        "actual_score_status": "parse_from_official_question",
        "max_score": None,
        "score_parts": [],
        "rubric_basis": "unresolved",
        "official_rubric_url": "",
    }
    row.update(answer_fields(institution_name))
    return row


def parse_ncku(html: str, start_year: int, end_year: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    papers: list[dict] = []
    for table in soup.select("div.table"):
        header = table.select_one("div.row.header div.cell")
        if not header:
            continue
        digits = "".join(ch for ch in header.get_text(strip=True) if ch.isdigit())
        if not digits:
            continue
        year = int(digits)
        if not start_year <= year <= end_year:
            continue
        for row in table.select("div.row:not(.header)"):
            cells = row.select(":scope > div.cell")
            link = row.find("a", href=True)
            if len(cells) < 2 or not link:
                continue
            subject = cells[0].get_text(" ", strip=True)
            if not subject or subject == "考試科目":
                continue
            url = urljoin(NCKU_URL, str(link["href"]))
            papers.append(base_paper(
                uid=paper_uid("ncku-law", year, subject, url),
                year=year,
                institution_name="國立成功大學",
                exam_name="國立成功大學法律學系碩士班招生考試",
                track="法律學系碩士班",
                subject=subject,
                question_url=url,
                official_page_url=NCKU_URL,
            ))
    return papers


def parse_ccu(html: str, start_year: int, end_year: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    papers: list[dict] = []
    seen: set[str] = set()
    seen_semantic: set[tuple[int, str, str]] = set()
    for link in soup.find_all("a", href=True):
        href = str(link["href"])
        filename = href.rsplit("/", 1)[-1].lower()
        if not filename.endswith(".pdf") or "/img/751/" not in href:
            continue
        stem = filename[:-4]
        match = re.fullmatch(r"(\d{3})(?:[-_](\d))?", stem)
        if not match:
            continue
        year = int(match.group(1))
        if not start_year <= year <= end_year:
            continue
        url = urljoin(CCU_URL, href)
        if url in seen:
            continue
        seen.add(url)
        group = match.group(2) or ""
        if year == 100:
            track, subject = "財經法律學系碩士班", "民法、商事法、行政法總論"
        elif group == "1":
            track, subject = "財經法組", "民法、商事法"
        else:
            track = "財稅法組"
            subject = "憲法、行政程序法" if year <= 103 else ("憲法、行政法" if year <= 105 else "行政程序法、行政救濟法")
        semantic_key = (year, track, subject)
        if semantic_key in seen_semantic:
            continue
        seen_semantic.add(semantic_key)
        papers.append(base_paper(
            uid=paper_uid("ccu-felaw", year, subject + track, url),
            year=year,
            institution_name="國立中正大學",
            exam_name="國立中正大學財經法律學系碩士班招生考試",
            track=track,
            subject=subject,
            question_url=url,
            official_page_url=CCU_URL,
        ))
    return papers


def nccu_papers(start_year: int, end_year: int) -> list[dict]:
    papers: list[dict] = []
    for year in range(start_year, end_year + 1):
        url = f"https://www.lib.nccu.edu.tw/var/file/0/1000/img/7/master_law{year}.pdf"
        papers.append(base_paper(
            uid=paper_uid("nccu-law", year, "法律學系碩士班年度合卷", url),
            year=year,
            institution_name="國立政治大學",
            exam_name="國立政治大學法律學系碩士班招生考試",
            track="法律學系碩士班各組（年度合卷）",
            subject="法律學系碩士班年度合卷",
            question_url=url,
            official_page_url=NCCU_ARCHIVE,
        ))
    return papers


def refresh(base: dict, ncku_html: str, ccu_html: str, start_year: int, end_year: int) -> dict:
    existing = [dict(item) for item in base.get("papers") or [] if isinstance(item, dict)]
    for row in existing:
        row.setdefault("question_link_type", "official_pdf")
        if row.get("exam_family") == "law_graduate_school":
            row.update({key: value for key, value in answer_fields(str(row.get("institution") or "")).items() if not row.get(key)})
    new_papers = parse_ncku(ncku_html, start_year, end_year) + parse_ccu(ccu_html, start_year, end_year) + nccu_papers(start_year, end_year)
    deduped = {str(item.get("uid") or ""): item for item in existing + new_papers if item.get("uid")}
    papers = sorted(deduped.values(), key=lambda item: (-int(item.get("year") or 0), str(item.get("exam_family") or ""), str(item.get("institution") or ""), str(item.get("track") or ""), str(item.get("subject") or "")))

    paper_counts: dict[str, int] = {}
    years_by_institution: dict[str, set[int]] = {}
    for row in papers:
        name = str(row.get("institution") or "")
        if row.get("exam_family") != "law_graduate_school" or not name:
            continue
        paper_counts[name] = paper_counts.get(name, 0) + 1
        years_by_institution.setdefault(name, set()).add(int(row.get("year") or 0))
    registry = []
    for raw in INSTITUTIONS:
        row = dict(raw)
        row["paper_count"] = paper_counts.get(row["name"], 0)
        row["years"] = sorted(years_by_institution.get(row["name"], set()), reverse=True)
        registry.append(row)

    sources = [dict(item) for item in base.get("sources") or [] if isinstance(item, dict)]
    additions = [
        {"authority": "教育部大專校院校務資訊公開平台", "type": "official_law_discipline_registry", "url": MOE_URL},
        {"authority": "國立成功大學圖書館", "type": "official_question_archive", "url": NCKU_URL},
        {"authority": "國立政治大學圖書館", "type": "official_question_archive", "url": NCCU_ARCHIVE},
        {"authority": "國立中正大學財經法律學系", "type": "official_question_archive", "url": CCU_URL},
        {"authority": "高點文化／高點法律網", "type": "third_party_reference_answer_catalog_link_only", "url": HIGHPOINT_ANSWER_CATALOG, "usage_policy": "僅保存可公開查證的來源目錄；逐題擬答未取得前不得供 MAGI 評分"},
        {"authority": "高點文化／高點法律網", "type": "current_reference_answer_catalog_link_only", "url": HIGHPOINT_CURRENT_CATALOG, "usage_policy": "僅保存來源連結，不鏡像付費內容"},
    ]
    source_key = {(item.get("authority"), item.get("type"), item.get("url")) for item in sources}
    for item in additions:
        key = (item.get("authority"), item.get("type"), item.get("url"))
        if key not in source_key:
            sources.append(item)
            source_key.add(key)

    return {
        **{key: value for key, value in base.items() if key not in {"schema_version", "generated_at", "policy", "sources", "years", "subjects", "paper_count", "papers", "institutions", "institution_count", "coverage_summary", "answer_discovery"}},
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "policy": {
            "official_rubric_precedence": True,
            "reference_derivation_only_when_official_missing": True,
            "score_allocation": "official_question_actual_only",
            "default_total_score": None,
            "official_question_required_for_random_practice": True,
            "third_party_answer_catalog_is_not_grading_material": True,
            "grading_requires_ingested_answer_or_official_rubric": True,
        },
        "sources": sources,
        "answer_discovery": {
            "policy": "擬答來源目錄只用於查找；必須取得合法、可追溯的逐題擬答內容後，MAGI 才可從中推導爭點。",
            "catalogs": [
                {"publisher": "高點文化／高點法律網", "url": HIGHPOINT_ANSWER_CATALOG, "coverage": ["國立臺灣大學", "國立政治大學", "國立臺北大學", "輔仁大學", "東吳大學", "中國文化大學", "中原大學", "國立中正大學", "國立成功大學"], "content_status": "link_only"},
                {"publisher": "高點文化／高點法律網", "url": HIGHPOINT_CURRENT_CATALOG, "coverage": ["近期法研所解析出版品"], "content_status": "link_only"},
            ],
        },
        "institution_count": len(registry),
        "institutions": registry,
        "coverage_summary": {
            "registry_total": len(registry),
            "direct_paper_institutions": sum(1 for item in registry if item["paper_count"] > 0),
            "official_archive_pending": sum(1 for item in registry if item["coverage_status"] in {"official_archive_pending_index", "official_papers_pending_index"}),
            "undergraduate_only": sum(1 for item in registry if item["coverage_status"] == "undergraduate_only"),
            "no_public_archive_verified": sum(1 for item in registry if item["coverage_status"] in {"no_public_archive_verified", "official_admissions_page_only"}),
        },
        "years": sorted({int(item.get("year") or 0) for item in papers if item.get("year")}, reverse=True),
        "subjects": sorted({str(item.get("subject") or "") for item in papers if str(item.get("subject") or "")}),
        "paper_count": len(papers),
        "papers": papers,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start-year", type=int, default=100)
    parser.add_argument("--end-year", type=int, default=115)
    args = parser.parse_args()
    if len(INSTITUTIONS) != 38 or len({item["name"] for item in INSTITUTIONS}) != 38:
        raise RuntimeError("教育部法律學門校系清冊必須恰為 38 校")
    base = json.loads(args.base.read_text(encoding="utf-8"))
    payload = refresh(base, fetch(NCKU_URL), fetch(CCU_URL), args.start_year, args.end_year)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "paper_count": payload["paper_count"],
        "institution_count": payload["institution_count"],
        "coverage_summary": payload["coverage_summary"],
        "law_graduate_papers": sum(1 for item in payload["papers"] if item.get("exam_family") == "law_graduate_school"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
