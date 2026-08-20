#!/usr/bin/env python3
"""Synchronize the newest MAGI exam-tutor sources without editing a release.

The scheduled path writes verified MOEX PDFs, parsed choice questions and a
machine-readable receipt below the persistent exam-tutor data directory.  A
separate bundle mode is used while preparing a release snapshot.  Both modes
are idempotent and require a complete question/answer pair before publishing a
subject.

Essay updates are deliberately source-gated: the second-stage question page,
MOEX scoring-point announcement and public model-answer index are collected by
the offline builder.  Its resulting rubric is stored before MAGI can grade it;
the live model never creates or reallocates a rubric.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.blueprints.exam_tutor import (  # noqa: E402
    CHOICE_SUBJECTS,
    choice_question_uid,
    import_choice_pdf_pair,
    parse_official_choice_answers,
    parse_official_choice_questions,
)


MOEX_INDEX = "https://wwwq.moex.gov.tw/exam/wFrmExamQandASearch.aspx"
MOEX_RSS = "https://wwwc.moex.gov.tw/main/news/wfrmNewsRSSdetail.aspx?Kind=3"
HEADERS = {"User-Agent": "MAGI-private-exam-tutor/3.0 (official-source synchronization; private use)"}
SUBJECT_CODES = {
    "0101": "1B",
    "0201": "2A",
    "0202": "2B",
    "0301": "1A",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_roc_year() -> int:
    return datetime.now(ZoneInfo("Asia/Taipei")).year - 1911


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def atomic_json(path: Path, payload: dict) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def load_json(path: Path, fallback: dict | None = None) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return dict(fallback or {})
    return payload if isinstance(payload, dict) else dict(fallback or {})


def fetch(session: requests.Session, url: str, *, params: dict | None = None) -> requests.Response:
    error: Exception | None = None
    for attempt in range(4):
        try:
            response = session.get(url, params=params, timeout=60, allow_redirects=True)
            response.raise_for_status()
            return response
        except Exception as exc:
            error = exc
            time.sleep(1 + attempt * 1.5)
    raise RuntimeError(f"official source request failed: {url}: {error}")


def discover_exam_code(index_html: str, year: int, *, stage: str) -> str:
    soup = BeautifulSoup(index_html, "html.parser")
    matches: list[str] = []
    for option in soup.select("option[value]"):
        title = option.get_text(" ", strip=True)
        code = str(option.get("value") or "").strip()
        if not code or str(year) not in title or "司法官" not in title or "律師" not in title:
            continue
        is_first = "第一試" in title
        is_second = "第二試" in title and not is_first
        if (stage == "first" and is_first) or (stage == "second" and is_second):
            matches.append(code)
    return sorted(set(matches), reverse=True)[0] if matches else ""


def _subject_key(label: str, subject_code: str) -> str:
    compact = re.sub(r"\s+", "", str(label or ""))
    if "憲法" in compact and "行政法" in compact:
        return "1B"
    if "民法" in compact and "民事訴訟" in compact:
        return "2A"
    if "公司法" in compact and "保險法" in compact:
        return "2B"
    if "刑法" in compact and "刑事訴訟" in compact:
        return "1A"
    return SUBJECT_CODES.get(subject_code, "")


def discover_choice_papers(page_html: str, page_url: str) -> list[dict]:
    """Return one complete four-subject category, preferring the combined row."""
    soup = BeautifulSoup(page_html, "html.parser")
    groups: dict[str, dict[str, dict]] = {}
    for label in soup.select("label.exam-title"):
        row = label.find_parent("tr")
        if row is None:
            continue
        subject_label = label.get_text(" ", strip=True)
        for anchor in row.select("a[href]"):
            href = str(anchor.get("href") or "")
            if "wHandExamQandA_File" not in href:
                continue
            query = parse_qs(urlparse(urljoin(page_url, href)).query)
            document_type = str((query.get("t") or [""])[0]).upper()
            if document_type not in {"Q", "S"}:
                continue
            category = str((query.get("c") or [""])[0])
            subject_code = str((query.get("s") or [""])[0])
            key = _subject_key(subject_label, subject_code)
            if not category or key not in CHOICE_SUBJECTS:
                continue
            item = groups.setdefault(category, {}).setdefault(key, {
                "subject_key": key,
                "subject_label": CHOICE_SUBJECTS[key],
                "subject_code": subject_code,
                "category": category,
            })
            item["question_url" if document_type == "Q" else "answer_url"] = urljoin(page_url, href)
    complete = {
        category: rows for category, rows in groups.items()
        if set(rows) == set(CHOICE_SUBJECTS)
        and all(row.get("question_url") and row.get("answer_url") for row in rows.values())
    }
    if not complete:
        return []
    # Current MOEX pages list judicial, lawyer and combined categories in that
    # order.  The combined category has the greatest category code; content is
    # still hash-validated, so a future layout change remains fail-closed.
    category = sorted(complete, key=lambda value: (int(value) if value.isdigit() else -1, value))[-1]
    return [complete[category][key] for key in CHOICE_SUBJECTS]


def pdf_text(content: bytes) -> str:
    converter = shutil.which("pdftotext")
    if not converter:
        for candidate in (Path("/opt/homebrew/bin/pdftotext"), Path("/usr/local/bin/pdftotext")):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                converter = str(candidate)
                break
    if not converter:
        raise RuntimeError("pdftotext is unavailable; official PDF parsing cannot be verified")
    with tempfile.TemporaryDirectory(prefix="magi-yearly-choice-") as directory:
        source = Path(directory) / "source.pdf"
        source.write_bytes(content)
        result = subprocess.run(
            [converter, "-layout", str(source), "-"],
            check=False,
            capture_output=True,
            text=True,
            timeout=75,
        )
    text = str(result.stdout or "").strip()
    if result.returncode != 0 or not text:
        raise RuntimeError(f"official PDF text extraction failed: {result.stderr.strip()[:240]}")
    return text


def download_pdf(session: requests.Session, url: str) -> tuple[bytes, str]:
    response = fetch(session, url)
    content = bytes(response.content)
    if not content.startswith(b"%PDF") or len(content) < 1_000:
        raise RuntimeError(f"official source did not return a PDF: {url}")
    return content, sha256(content)


def parse_choice_pair(paper: dict, question_content: bytes, answer_content: bytes) -> dict:
    question_text = pdf_text(question_content)
    answer_text = pdf_text(answer_content)
    questions = parse_official_choice_questions(question_text)
    answers = parse_official_choice_answers(answer_text)
    matched = [item for item in questions if item["number"] in answers]
    minimum = max(10, int(len(questions) * 0.8))
    if not questions or len(matched) < minimum:
        raise RuntimeError(
            f"{paper['subject_key']}: parsed {len(questions)} questions but only {len(matched)} official answers"
        )
    return {
        **paper,
        "question_content": question_content,
        "answer_content": answer_content,
        "question_sha256": sha256(question_content),
        "answer_sha256": sha256(answer_content),
        "question_text": question_text,
        "answer_text": answer_text,
        "questions": questions,
        "answers": answers,
        "matched": matched,
    }


def _database_pair_count(database: Path, year: int, subject_key: str) -> int:
    if not database.is_file():
        return 0
    try:
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM exam_choice_questions WHERE exam_year = ? AND subject_key = ?",
                (year, subject_key),
            ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] if row else 0)


def update_bundle(bank_path: Path, pdf_root: Path, *, year: int, exam_code: str, parsed: list[dict]) -> int:
    bank = load_json(bank_path)
    if not isinstance(bank.get("subjects"), list):
        raise RuntimeError(f"invalid bundled choice bank: {bank_path}")
    pdf_root.mkdir(parents=True, exist_ok=True)
    documents: list[dict] = [
        dict(item) for item in bank.get("documents") or []
        if isinstance(item, dict) and int(item.get("year") or 0) != year
    ]
    parsed_by_key = {item["subject_key"]: item for item in parsed}
    total = 0
    for subject in bank["subjects"]:
        key = str(subject.get("key") or "")
        item = parsed_by_key.get(key)
        if not item:
            continue
        questions = [dict(raw) for raw in subject.get("questions") or [] if int(raw.get("year") or 0) != year]
        for raw in item["matched"]:
            number = int(raw["number"])
            questions.append({
                "number": number,
                "question": raw["question"],
                "options": raw["options"],
                "uid": choice_question_uid(subject_key=key, year=year, number=number, question=raw["question"]),
                "year": year,
                "answer": item["answers"][number],
                "exam_label": "司法官／律師第一試",
            })
            total += 1
        subject["questions"] = sorted(questions, key=lambda raw: (int(raw.get("year") or 0), int(raw.get("number") or 0)))
        question_name = f"{exam_code}_{item['subject_code']}_question.pdf"
        answer_name = f"{exam_code}_{item['subject_code']}_answer.pdf"
        for filename, content in ((question_name, item["question_content"]), (answer_name, item["answer_content"])):
            destination = (pdf_root / filename).resolve()
            if pdf_root.resolve() not in destination.parents:
                raise RuntimeError("bundle PDF path escaped its root")
            temporary = destination.with_suffix(".pdf.partial")
            temporary.write_bytes(content)
            os.replace(temporary, destination)
        documents.append({
            "year": year,
            "year_label": f"{year}年",
            "subject": item["subject_label"],
            "question_file": question_name,
            "answer_file": answer_name,
            "modification_file": None,
            "question_url": item["question_url"],
            "answer_url": item["answer_url"],
            "question_sha256": item["question_sha256"],
            "answer_sha256": item["answer_sha256"],
        })
    bank.update({
        "generated_at": datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat(),
        "updated_through_year": max(int(bank.get("updated_through_year") or 0), year),
        "documents": sorted(documents, key=lambda item: (-int(item.get("year") or 0), str(item.get("subject") or ""))),
        "annual_sync": {
            "mode": "official_moex_verified",
            "exam_code": exam_code,
            "updated_at": now_iso(),
            "subject_count": len(parsed),
            "question_count": total,
        },
    })
    atomic_json(bank_path, bank)
    return total


def sync_choice_year(
    session: requests.Session,
    *,
    year: int,
    database: Path,
    previous_receipts: dict,
    bundle_bank: Path | None = None,
    bundle_pdf_root: Path | None = None,
) -> dict:
    index = fetch(session, MOEX_INDEX, params={"y": year + 1911})
    exam_code = discover_exam_code(index.text, year, stage="first")
    if not exam_code:
        return {"year": year, "state": "not_published", "exam_code": "", "subjects": []}
    page = fetch(session, MOEX_INDEX, params={"e": exam_code, "y": year + 1911})
    papers = discover_choice_papers(page.text, page.url)
    if len(papers) != len(CHOICE_SUBJECTS):
        return {"year": year, "state": "incomplete_official_page", "exam_code": exam_code, "subjects": []}

    parsed: list[dict] = []
    subjects: list[dict] = []
    imported = 0
    for paper in papers:
        question_content, question_digest = download_pdf(session, paper["question_url"])
        answer_content, answer_digest = download_pdf(session, paper["answer_url"])
        item = parse_choice_pair(paper, question_content, answer_content)
        parsed.append(item)
        key = f"{year}:{paper['subject_key']}"
        previous = previous_receipts.get(key) if isinstance(previous_receipts.get(key), dict) else {}
        unchanged = (
            previous.get("question_sha256") == question_digest
            and previous.get("answer_sha256") == answer_digest
            and _database_pair_count(database, year, paper["subject_key"]) == len(item["matched"])
        )
        if not unchanged:
            result = import_choice_pdf_pair(
                year=year,
                subject_key=paper["subject_key"],
                question_text=item["question_text"],
                answer_text=item["answer_text"],
                question_content=question_content,
                answer_content=answer_content,
                question_filename=f"{exam_code}_{paper['subject_code']}_question.pdf",
                answer_filename=f"{exam_code}_{paper['subject_code']}_answer.pdf",
                question_source_url=paper["question_url"],
                answer_source_url=paper["answer_url"],
                source_type="moex_official_auto",
                question_meta={"method": "pdftotext_layout", "sha256": question_digest},
                answer_meta={"method": "pdftotext_layout", "sha256": answer_digest},
            )
            imported += int(result["imported"])
        subjects.append({
            "subject_key": paper["subject_key"],
            "question_count": len(item["matched"]),
            "question_url": paper["question_url"],
            "answer_url": paper["answer_url"],
            "question_sha256": question_digest,
            "answer_sha256": answer_digest,
            "state": "unchanged" if unchanged else "updated",
        })
    bundled = 0
    if bundle_bank and bundle_pdf_root:
        bundled = update_bundle(bundle_bank, bundle_pdf_root, year=year, exam_code=exam_code, parsed=parsed)
    return {
        "year": year,
        "state": "ready",
        "exam_code": exam_code,
        "official_page_url": page.url,
        "subjects": subjects,
        "subject_count": len(subjects),
        "question_count": sum(item["question_count"] for item in subjects),
        "imported": imported,
        "bundled": bundled,
    }


def sync_choice_offline_fixture(
    fixture_root: Path,
    *,
    year: int,
    database: Path,
) -> dict:
    """Exercise the real PDF parser/import path without external network I/O.

    The formal schedule-body certification uses the already sealed official
    PDFs as deterministic inputs and writes only to its disposable fixture.
    Production never supplies this option; its normal path still downloads and
    verifies the current MOEX pages and PDFs.
    """
    root = fixture_root.expanduser().resolve()
    exam_code = f"{year}110"
    parsed: list[dict] = []
    for subject_code, subject_key in SUBJECT_CODES.items():
        question_path = (root / f"{exam_code}_{subject_code}_question.pdf").resolve()
        answer_path = (root / f"{exam_code}_{subject_code}_answer.pdf").resolve()
        if root not in question_path.parents or root not in answer_path.parents:
            raise RuntimeError("offline fixture path escaped its root")
        question_content = question_path.read_bytes()
        answer_content = answer_path.read_bytes()
        if not question_content.startswith(b"%PDF") or not answer_content.startswith(b"%PDF"):
            raise RuntimeError(f"offline fixture is not an official PDF pair: {subject_code}")
        base_url = (
            "https://wwwq.moex.gov.tw/exam/wFrmExamQandASearch.aspx"
            f"?e={exam_code}&y={year + 1911}"
        )
        paper = {
            "subject_key": subject_key,
            "subject_label": CHOICE_SUBJECTS[subject_key],
            "subject_code": subject_code,
            "category": "303",
            "question_url": f"{base_url}#fixture-question-{subject_code}",
            "answer_url": f"{base_url}#fixture-answer-{subject_code}",
        }
        item = parse_choice_pair(paper, question_content, answer_content)
        result = import_choice_pdf_pair(
            year=year,
            subject_key=subject_key,
            question_text=item["question_text"],
            answer_text=item["answer_text"],
            question_content=question_content,
            answer_content=answer_content,
            question_filename=question_path.name,
            answer_filename=answer_path.name,
            question_source_url=paper["question_url"],
            answer_source_url=paper["answer_url"],
            source_type="moex_official_schedule_fixture",
            question_meta={"method": "pdftotext_layout", "sha256": item["question_sha256"]},
            answer_meta={"method": "pdftotext_layout", "sha256": item["answer_sha256"]},
        )
        parsed.append({
            "subject_key": subject_key,
            "question_count": len(item["matched"]),
            "question_sha256": item["question_sha256"],
            "answer_sha256": item["answer_sha256"],
            "imported": int(result["imported"]),
            "state": "updated",
        })
    total = sum(int(item["question_count"]) for item in parsed)
    return {
        "schema_version": 1,
        "state": "ok",
        "started_at": now_iso(),
        "checked_at": now_iso(),
        "message": f"離線排程驗證完成：民國 {year} 年四科共 {total} 題。",
        "years_checked": [year],
        "latest_choice_year": year,
        "choice_imported": sum(int(item["imported"]) for item in parsed),
        "choice_subjects_ready": len(parsed),
        "choice_runs": [{
            "year": year,
            "state": "ready",
            "exam_code": exam_code,
            "subject_count": len(parsed),
            "question_count": total,
            "subjects": parsed,
        }],
        "choice_receipts": {
            f"{year}:{item['subject_key']}": {
                "question_sha256": item["question_sha256"],
                "answer_sha256": item["answer_sha256"],
                "question_count": item["question_count"],
            }
            for item in parsed
        },
        "essay": {"state": "skipped_for_offline_schedule_fixture", "years": []},
        "fixture": {"network_accessed": False, "database": str(database)},
    }


def discover_rubric_news(session: requests.Session, years: set[int], cache_path: Path) -> dict:
    cache = load_json(cache_path, {"schema_version": 1, "years": {}})
    rows = cache.get("years") if isinstance(cache.get("years"), dict) else {}
    try:
        response = fetch(session, MOEX_RSS)
        soup = BeautifulSoup(response.content, "xml")
        for item in soup.find_all("item"):
            title = item.title.get_text(" ", strip=True) if item.title else ""
            link = item.link.get_text(" ", strip=True) if item.link else ""
            match = re.search(r"(\d{3})年.*司法官.*律師.*第二試.*評分要點", title)
            if match and int(match.group(1)) in years and link:
                rows[str(int(match.group(1)))] = {"title": title, "url": link, "discovered_at": now_iso()}
    finally:
        cache.update({"updated_at": now_iso(), "years": rows})
        atomic_json(cache_path, cache)
    return rows


def _combined_essay_entries(bundle_path: Path, runtime_path: Path) -> list[dict]:
    merged: dict[str, dict] = {}
    for path in (bundle_path, runtime_path):
        for item in load_json(path).get("entries") or []:
            if isinstance(item, dict) and str(item.get("uid") or ""):
                merged[str(item["uid"])] = item
    return list(merged.values())


def sync_essay_pipeline(
    session: requests.Session,
    *,
    years: list[int],
    database: Path,
    bundle_essay_bank: Path,
    skip_build: bool,
) -> dict:
    runtime_root = database.parent
    rubric_news = discover_rubric_news(session, set(years), runtime_root / "rubric_news_sources.json")
    existing = _combined_essay_entries(bundle_essay_bank, runtime_root / "essay_bank.json")
    requested: list[int] = []
    year_states: list[dict] = []
    for year in years:
        index = fetch(session, MOEX_INDEX, params={"y": year + 1911})
        code = discover_exam_code(index.text, year, stage="second")
        entries = [item for item in existing if int(item.get("year") or 0) == year and str(item.get("exam_family") or "judicial_bar") == "judicial_bar"]
        official_ready = bool(entries) and all(item.get("rubric_basis") == "official" and item.get("stored_rubric") for item in entries)
        grading_ready = bool(entries) and all(bool(item.get("stored_rubric")) for item in entries)
        announced = str(year) in rubric_news
        needs_refresh = bool(code) and (not grading_ready or (announced and not official_ready))
        if needs_refresh:
            requested.append(year)
        year_states.append({
            "year": year,
            "exam_code": code,
            "state": (
                "waiting_for_second_stage" if not code else
                "official_ready" if official_ready else
                "grading_ready" if grading_ready else
                "sources_pending"
            ),
            "entry_count": len(entries),
            "official_rubric_announced": announced,
        })
    builder_result: dict = {}
    if requested and not skip_build:
        command = [
            sys.executable,
            str(REPO_ROOT / "scripts" / "refresh_moex_judicial_bar_bank.py"),
            "--output-root", str(runtime_root),
            "--years", ",".join(str(year) for year in sorted(set(requested))),
        ]
        result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=3300, check=False)
        builder_result = {
            "returncode": result.returncode,
            "stdout_tail": result.stdout[-2000:],
            "stderr_tail": result.stderr[-2000:],
        }
        if result.returncode != 0:
            raise RuntimeError(f"essay source builder failed: {result.stderr[-800:]}")
    return {
        "state": "updated" if requested and not skip_build else "checked",
        "requested_years": requested,
        "years": year_states,
        "builder": builder_result,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", dest="years", action="append", type=int, help="ROC year; repeatable")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--archive-root", type=Path)
    parser.add_argument("--status-path", type=Path)
    parser.add_argument("--bundle-choice-bank", type=Path)
    parser.add_argument("--bundle-pdf-root", type=Path)
    parser.add_argument("--bundle-essay-bank", type=Path, default=REPO_ROOT / "static" / "exam_tutor" / "essay_bank.json")
    parser.add_argument("--skip-essay", action="store_true")
    parser.add_argument("--skip-essay-build", action="store_true")
    parser.add_argument("--skip-trends", action="store_true")
    parser.add_argument(
        "--offline-fixture-root",
        type=Path,
        help="sealed official PDF directory used only by isolated schedule certification",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    from api.runtime_paths import get_agent_dir

    years = sorted(set(args.years or [current_roc_year(), current_roc_year() - 1]), reverse=True)
    database = (args.database or (get_agent_dir() / "exam-tutor" / "exam_tutor.sqlite3")).expanduser().resolve()
    archive_root = (args.archive_root or (database.parent / "archive")).expanduser().resolve()
    status_path = (args.status_path or (database.parent / "yearly_sync_status.json")).expanduser().resolve()
    os.environ["MAGI_EXAM_TUTOR_DB_PATH"] = str(database)
    os.environ["MAGI_EXAM_TUTOR_ARCHIVE_DIR"] = str(archive_root)
    previous_status = load_json(status_path)
    previous_receipts = previous_status.get("choice_receipts") if isinstance(previous_status.get("choice_receipts"), dict) else {}
    session = requests.Session()
    session.headers.update(HEADERS)
    started_at = now_iso()
    try:
        if args.offline_fixture_root:
            status = sync_choice_offline_fixture(
                args.offline_fixture_root,
                year=years[0],
                database=database,
            )
            atomic_json(status_path, status)
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return
        choice_runs = [
            sync_choice_year(
                session,
                year=year,
                database=database,
                previous_receipts=previous_receipts,
                bundle_bank=args.bundle_choice_bank.expanduser().resolve() if args.bundle_choice_bank else None,
                bundle_pdf_root=args.bundle_pdf_root.expanduser().resolve() if args.bundle_pdf_root else None,
            )
            for year in years
        ]
        receipts = dict(previous_receipts)
        for run in choice_runs:
            for item in run.get("subjects") or []:
                receipts[f"{run['year']}:{item['subject_key']}"] = {
                    key: item[key] for key in (
                        "question_sha256", "answer_sha256", "question_url", "answer_url", "question_count"
                    )
                }
        ready = [run for run in choice_runs if run.get("state") == "ready"]
        latest = max((int(run["year"]) for run in ready), default=None)
        imported = sum(int(run.get("imported") or 0) for run in ready)
        essay = {"state": "skipped", "years": []}
        if not args.skip_essay:
            essay = sync_essay_pipeline(
                session,
                years=years,
                database=database,
                bundle_essay_bank=args.bundle_essay_bank.expanduser().resolve(),
                skip_build=args.skip_essay_build,
            )
        trends: dict[str, Any] = {"state": "skipped"}
        if not args.skip_trends:
            trend_command = [
                sys.executable,
                str(REPO_ROOT / "scripts" / "sync_exam_tutor_trends.py"),
                "--output", str(database.parent / "trend_analysis.json"),
                "--receipt", str(database.parent / "trend_sync_status.json"),
            ]
            trend_result = subprocess.run(
                trend_command,
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
            )
            trends = {
                "state": "updated" if trend_result.returncode == 0 else "failed_preserved_previous",
                "returncode": trend_result.returncode,
                "stdout_tail": trend_result.stdout[-1200:],
                "stderr_tail": trend_result.stderr[-1200:],
                "local_model_fallback": False,
            }
        message = (
            f"已核對考選部至民國 {latest} 年；四科共 {sum(int(run.get('question_count') or 0) for run in ready if int(run['year']) == latest)} 題可離線練習。"
            if latest else "本年度司法官／律師題答尚未在考選部公開，已保留排程下次再檢查。"
        )
        status = {
            "schema_version": 1,
            "state": "ok",
            "started_at": started_at,
            "checked_at": now_iso(),
            "message": message,
            "years_checked": years,
            "latest_choice_year": latest,
            "choice_imported": imported,
            "choice_subjects_ready": sum(int(run.get("subject_count") or 0) for run in ready if int(run["year"]) == latest),
            "choice_runs": choice_runs,
            "choice_receipts": receipts,
            "essay": essay,
            "trends": trends,
        }
        atomic_json(status_path, status)
        print(json.dumps(status, ensure_ascii=False, indent=2))
    except Exception as exc:
        status = {
            "schema_version": 1,
            "state": "failed",
            "started_at": started_at,
            "checked_at": now_iso(),
            "message": f"最近一次自動檢查失敗，已保留現有題庫：{exc}",
            "years_checked": years,
            "latest_choice_year": previous_status.get("latest_choice_year"),
            "choice_imported": 0,
            "choice_subjects_ready": previous_status.get("choice_subjects_ready", 0),
            "choice_receipts": previous_receipts,
            "essay": {"state": "failed", "error": str(exc)},
        }
        atomic_json(status_path, status)
        print(json.dumps(status, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
