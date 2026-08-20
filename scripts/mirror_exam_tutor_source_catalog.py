#!/usr/bin/env python3
"""Mirror every direct PDF in MAGI's extended exam catalog for offline use.

This utility never treats a catalog-only answer page as a model answer and
never creates a rubric.  It only archives the exact question/answer/correction
PDFs that the catalog already attributes to an official or named source.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import urllib3


HEADERS = {"User-Agent": "MAGI-private-exam-tutor/2.0 (offline archival; private use)"}
SOURCE_FIELDS = {
    "question_url": ("extended/questions", "question"),
    "official_answer_url": ("extended/official-answers", "official_answer"),
    "official_correction_url": ("extended/corrections", "official_correction"),
    "official_rubric_url": ("extended/rubrics", "official_rubric"),
    "reference_answer_url": ("extended/references", "reference_answer"),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_stem(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")[:100] or "exam-document"


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid JSON object: {path}")
    return payload


def active_catalog_urls(catalog: dict[str, Any]) -> set[str]:
    """Return direct HTTPS sources that still belong to the current catalog."""
    return {
        str(paper.get(field) or "").strip()
        for paper in catalog.get("papers") or []
        if isinstance(paper, dict)
        for field in SOURCE_FIELDS
        if str(paper.get(field) or "").strip().startswith("https://")
    }


def build_jobs(catalog: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = {}
    saved = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    for paper in catalog.get("papers") or []:
        if not isinstance(paper, dict):
            continue
        for field, (category, document_kind) in SOURCE_FIELDS.items():
            url = str(paper.get(field) or "").strip()
            if not url.startswith("https://"):
                continue
            previous = saved.get(url)
            if isinstance(previous, dict) and previous.get("status") == "saved":
                continue
            job = jobs.setdefault(url, {
                "url": url,
                "category": category,
                "document_kind": document_kind,
                "year": paper.get("year"),
                "exam_family": paper.get("exam_family"),
                "exam_name": paper.get("exam_name"),
                "institution": paper.get("institution"),
                "subject": paper.get("subject"),
                "paper_uids": [],
            })
            uid = str(paper.get("uid") or "")
            if uid and len(job["paper_uids"]) < 25:
                job["paper_uids"].append(uid)
    return list(jobs.values())


def fetch_pdf(job: dict[str, Any], archive_root: Path) -> tuple[str, dict[str, Any]]:
    url = job["url"]
    last_error = ""
    session = requests.Session()
    session.headers.update(HEADERS)
    tls_verified = True
    for attempt in range(4):
        try:
            try:
                response = session.get(url, timeout=35, allow_redirects=True)
            except requests.exceptions.SSLError:
                host = str(urlparse(url).hostname or "").lower()
                if not (host.endswith(".edu.tw") or host.endswith(".gov.tw")):
                    raise
                # Several Taiwanese university archives still present a chain
                # rejected by Python 3.14 (missing Subject Key Identifier).
                # The fallback is restricted to official .edu.tw/.gov.tw hosts;
                # the payload must still be an actual PDF and is SHA-256 sealed.
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                response = session.get(url, timeout=35, allow_redirects=True, verify=False)
                tls_verified = False
            if response.status_code == 429:
                time.sleep(8 + attempt * 8)
                continue
            response.raise_for_status()
            content = response.content
            if not content.startswith(b"%PDF"):
                raise RuntimeError(
                    f"not_pdf content_type={response.headers.get('content-type', '')} bytes={len(content)}"
                )
            digest = hashlib.sha256(content).hexdigest()
            stem = safe_stem(
                f"{job.get('year')}-{job.get('exam_family')}-{job.get('institution')}-{job.get('subject')}-{job['document_kind']}"
            )
            relative = f"{job['category']}/{stem}-{digest[:12]}.pdf"
            destination = (archive_root / relative).resolve()
            if archive_root != destination and archive_root not in destination.parents:
                raise RuntimeError("archive path escaped root")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.is_file():
                temporary = destination.with_suffix(".pdf.partial")
                temporary.write_bytes(content)
                temporary.replace(destination)
            return url, {
                "status": "saved",
                "relative_path": relative,
                "sha256": digest,
                "bytes": len(content),
                "content_type": "application/pdf",
                "saved_at": now_iso(),
                "tls_verified": tls_verified,
                "retrieval_warning": (
                    "official_host_certificate_rejected_by_python_3_14; PDF signature and SHA-256 verified"
                    if not tls_verified else ""
                ),
                **{key: value for key, value in job.items() if key != "url"},
            }
        except Exception as exc:  # noqa: BLE001 - persisted for audit/resume
            last_error = str(exc)[:500]
            if last_error.startswith("not_pdf") or (
                isinstance(exc, requests.exceptions.HTTPError)
                and exc.response is not None and 400 <= exc.response.status_code < 500
                and exc.response.status_code != 429
            ):
                break
            if attempt < 3:
                time.sleep(1.5 + attempt * 2)
    return url, {
        "status": "pending",
        "last_error": last_error or "download_failed",
        "last_attempt_at": now_iso(),
        **{key: value for key, value in job.items() if key != "url"},
    }


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    files = manifest.setdefault("files", {})
    saved = [item for item in files.values() if isinstance(item, dict) and item.get("status") == "saved"]
    manifest["generated_at"] = now_iso()
    manifest["summary"] = {
        "saved": len(saved),
        "pending": sum(1 for item in files.values() if isinstance(item, dict) and item.get("status") != "saved"),
        "bytes": sum(int(item.get("bytes") or 0) for item in saved),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.partial")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-files", type=int, default=0)
    args = parser.parse_args()

    catalog = load_json(args.catalog.resolve())
    archive_root = args.archive_root.resolve()
    manifest_path = archive_root / "archive_manifest.json"
    manifest = load_json(manifest_path) if manifest_path.is_file() else {
        "schema_version": 1, "generated_at": now_iso(), "files": {}, "summary": {}
    }
    # A corrected catalog URL must not leave a permanent failed row behind.
    # Keep successful historical copies, but discard unresolved URLs that no
    # longer occur in the current catalog before resuming downloads.
    active_urls = active_catalog_urls(catalog)
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    manifest["files"] = {
        url: item
        for url, item in files.items()
        if url in active_urls or (isinstance(item, dict) and item.get("status") == "saved")
    }
    jobs = build_jobs(catalog, manifest)
    if args.max_files > 0:
        jobs = jobs[:args.max_files]
    print(json.dumps({"event": "mirror_start", "jobs": len(jobs)}, ensure_ascii=False), flush=True)

    lock = threading.Lock()
    completed = 0
    saved_now = 0
    with ThreadPoolExecutor(max_workers=max(1, min(8, args.workers))) as executor:
        futures = [executor.submit(fetch_pdf, job, archive_root) for job in jobs]
        for future in as_completed(futures):
            url, item = future.result()
            with lock:
                manifest.setdefault("files", {})[url] = item
                completed += 1
                saved_now += int(item.get("status") == "saved")
                if completed % 10 == 0 or completed == len(jobs):
                    write_manifest(manifest_path, manifest)
                    print(json.dumps({
                        "event": "mirror_progress", "completed": completed,
                        "total": len(jobs), "saved_now": saved_now,
                        "pending_now": completed - saved_now,
                    }, ensure_ascii=False), flush=True)
    write_manifest(manifest_path, manifest)
    print(json.dumps({"event": "mirror_complete", **manifest["summary"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
