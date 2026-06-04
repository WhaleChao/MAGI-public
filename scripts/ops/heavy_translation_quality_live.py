#!/usr/bin/env python3
"""HEAVY translation live gate for Taiwan legal/academic PDF quality.

This gate keeps @heavy honest without re-translating a full thesis on every
run.  It verifies the live NVIDIA route with a short synthetic prompt, then
uses a real PDF fixture to validate extraction, DOI/header cleanup, Taiwan
legal terminology, source-term visibility, and DOCX export/readback quality.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_FIXTURE = Path("/Users/ai/Desktop/司法通譯語言風格如何影響國民法官對被告的印象.pdf")

BAD_TERMS = (
    "doi:",
    "doi：",
    "公民法官",
    "法庭翻譯",
    "法庭口譯員",
    "法院翻譯",
    "演講風格",
    "言語風格",
    "無能為力組",
    "無權組",
    "強大組",
    "有權組",
    "辯護人的印象",
)

REQUIRED_TERMS = (
    "國民法官法",
    "國民法官",
    "司法通譯",
    "被告",
    "無力風格",
    "有力風格",
    "假冒配對測試法",
)

REQUIRED_SOURCE_ANNOTATIONS = (
    "Citizen Judges Act",
    "court interpreters",
    "defendant",
    "powerless style",
    "Powerless Group",
    "Powerful Group",
    "matched guise technique",
)


def _load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _check(checks: list[dict[str, Any]], name: str, ok: bool, detail: str = "", **extra: Any) -> None:
    item: dict[str, Any] = {"name": name, "ok": bool(ok), "detail": detail}
    item.update(extra)
    checks.append(item)


def _text_of(result: dict[str, Any]) -> str:
    return str(result.get("response") or result.get("translated_text") or result.get("text") or "").strip()


def _extract_fixture(pdf_path: Path) -> dict[str, str]:
    from pypdf import PdfReader
    from api.handlers.document_handler import prepare_document_text_for_llm

    reader = PdfReader(str(pdf_path))
    page_texts = [(page.extract_text() or "") for page in reader.pages]
    return {
        "title": prepare_document_text_for_llm(page_texts[0] if page_texts else ""),
        "zh_abstract": prepare_document_text_for_llm("\n".join(page_texts[3:5])),
        "en_abstract": prepare_document_text_for_llm("\n".join(page_texts[5:7])),
        "pages": str(len(reader.pages)),
    }


def _run_nim_route_check(timeout: int) -> dict[str, Any]:
    from skills.bridge.inference_gateway import InferenceGateway

    started = time.monotonic()
    old_retries = os.environ.get("MAGI_HEAVY_STRICT_NIM_RETRIES")
    old_fallback = os.environ.get("MAGI_HEAVY_STRICT_NIM_ALLOW_FALLBACK")
    try:
        os.environ["MAGI_HEAVY_STRICT_NIM_RETRIES"] = "0"
        os.environ["MAGI_HEAVY_STRICT_NIM_ALLOW_FALLBACK"] = "0"
        result = InferenceGateway().chat(
            "@heavy 請用臺灣繁體中文回答：court interpreter 在司法文件中應譯為什麼？只回答一行。",
            task_type="translate",
            timeout=timeout,
            allow_synthetic_fallback=False,
        )
    finally:
        if old_retries is None:
            os.environ.pop("MAGI_HEAVY_STRICT_NIM_RETRIES", None)
        else:
            os.environ["MAGI_HEAVY_STRICT_NIM_RETRIES"] = old_retries
        if old_fallback is None:
            os.environ.pop("MAGI_HEAVY_STRICT_NIM_ALLOW_FALLBACK", None)
        else:
            os.environ["MAGI_HEAVY_STRICT_NIM_ALLOW_FALLBACK"] = old_fallback
    return {
        "elapsed_sec": round(time.monotonic() - started, 2),
        "success": bool(result.get("success")),
        "route": str(result.get("route") or ""),
        "model": str(result.get("model") or ""),
        "provider": str(result.get("provider") or ""),
        "text_len": len(_text_of(result)),
        "error": str(result.get("error") or "")[:240],
    }


def _read_docx_text(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    parts: list[str] = []
    for p in doc.paragraphs:
        if p.text:
            parts.append(p.text)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text:
                    parts.append(cell.text)
    return "\n".join(parts)


def run_gate(*, pdf_path: Path, run_live_nim: bool, timeout: int) -> dict[str, Any]:
    from api.handlers.document_handler import (
        build_translation_term_glossary,
        ensure_translation_terms_visible,
        normalize_tw_legal_translation_terms,
        polish_translated_document_text,
    )
    from api.handlers.translation_handler import translate_text_complete
    from skills.ops.export_docx import export_bilingual_docx

    checks: list[dict[str, Any]] = []

    if run_live_nim:
        route = _run_nim_route_check(timeout)
        _check(
            checks,
            "heavy_nvidia_route",
            route["success"] and route["route"] == "nvidia_nim",
            f"route={route['route']} model={route['model']}",
            result=route,
        )

    if not pdf_path.exists():
        _check(checks, "fixture_pdf_exists", False, str(pdf_path))
        return {"success": False, "pdf": str(pdf_path), "checks": checks}
    _check(checks, "fixture_pdf_exists", True, str(pdf_path))

    extracted = _extract_fixture(pdf_path)
    joined_source = "\n\n".join(extracted.values())
    _check(checks, "pdf_extract_pages", int(extracted["pages"]) >= 100, f"pages={extracted['pages']}")
    _check(checks, "pdf_doi_cleaned", "doi:" not in joined_source.lower() and "doi：" not in joined_source.lower())
    _check(
        checks,
        "pdf_title_preserved",
        "司法通譯語言風格如何影響國民法官對被告的印象" in extracted["title"]
        and "辯護人的印象" not in extracted["title"],
    )
    zh_compact = re.sub(r"\s+", "", extracted["zh_abstract"])
    _check(
        checks,
        "pdf_official_zh_terms",
        all(term in zh_compact for term in ("國民法官法", "司法通譯", "無力風格", "假冒配對測試法")),
    )

    title_res = translate_text_complete(extracted["title"], target_lang="繁體中文", heavy=True)
    title_out = polish_translated_document_text(_text_of(title_res))
    _check(
        checks,
        "heavy_title_identity_preserve",
        bool(title_res.get("success"))
        and "source_zh_preserved" == str(title_res.get("model") or "")
        and "司法通譯語言風格如何影響國民法官對被告的印象" in title_out
        and "辯護人的印象" not in title_out,
        f"model={title_res.get('model')}",
    )

    glossary = build_translation_term_glossary(extracted["en_abstract"], max_terms=40)
    old_bad_translation = (
        "隨著2023年1月1日《公民法官法》的實施，台灣的司法制度進入新時代。"
        "對法庭翻譯的需求增加。許多外國被告展現無權風格。"
        "本研究使用配對偽裝技術，將參與者分成無權組與強大組。"
        "參與者評估被告的智力、可信度、說服力。"
    )
    corrected = ensure_translation_terms_visible(
        extracted["en_abstract"],
        old_bad_translation,
        term_glossary=glossary,
        target_lang="繁體中文",
    )
    corrected = normalize_tw_legal_translation_terms(corrected)
    lowered_corrected = corrected.lower()
    _check(checks, "tw_term_normalization", all(term in corrected for term in REQUIRED_TERMS))
    _check(checks, "source_terms_inline", all(term.lower() in lowered_corrected for term in REQUIRED_SOURCE_ANNOTATIONS))
    _check(checks, "bad_terms_removed", not any(term.lower() in lowered_corrected for term in BAD_TERMS))

    export = export_bilingual_docx(
        [
            {"page": "標題", "source": extracted["title"], "target": title_out},
            {"page": "中文摘要", "source": extracted["zh_abstract"], "target": polish_translated_document_text(extracted["zh_abstract"])},
            {"page": "英文摘要節錄", "source": extracted["en_abstract"][:1800], "target": corrected},
        ],
        title="HEAVY 翻譯品質 Live Gate",
        subtitle=pdf_path.name,
        header_text="MAGI @heavy translation live gate",
        prefix="heavy_translation_live_gate",
        hide_page_column=True,
        col_labels={"col2": "原文", "col3": "譯文 / 品質校正"},
    )
    docx_path = Path(str(export.get("path") or "")) if export.get("success") else Path()
    _check(checks, "docx_export", bool(export.get("success")) and docx_path.exists(), str(docx_path))
    if docx_path.exists():
        docx_text = _read_docx_text(docx_path)
        docx_lower = docx_text.lower()
        _check(checks, "docx_no_doi", "doi:" not in docx_lower and "doi：" not in docx_lower)
        _check(checks, "docx_good_title", "司法通譯語言風格如何影響國民法官對被告的印象" in docx_text)
        _check(checks, "docx_bad_terms_removed", not any(term.lower() in docx_lower for term in BAD_TERMS))
        _check(checks, "docx_source_terms_inline", all(term.lower() in docx_lower for term in REQUIRED_SOURCE_ANNOTATIONS))

    return {
        "success": all(item["ok"] for item in checks),
        "pdf": str(pdf_path),
        "docx_path": str(docx_path) if docx_path else "",
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", default=os.environ.get("MAGI_HEAVY_TRANSLATION_FIXTURE_PDF", str(DEFAULT_FIXTURE)))
    parser.add_argument("--skip-live-nim", action="store_true")
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("MAGI_HEAVY_TRANSLATION_LIVE_TIMEOUT", "90") or "90"))
    parser.add_argument("--json-out", default=str(ROOT / ".runtime" / "heavy_translation_quality_latest.json"))
    args = parser.parse_args()

    _load_env()
    result = run_gate(pdf_path=Path(args.pdf).expanduser(), run_live_nim=not args.skip_live_nim, timeout=args.timeout)
    out_path = Path(args.json_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for item in result["checks"]:
        print(("✅" if item["ok"] else "❌") + f" {item['name']} {item.get('detail','')}".rstrip())
    print(f"JSON: {out_path}")
    if result.get("docx_path"):
        print(f"DOCX: {result['docx_path']}")
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
