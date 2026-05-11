#!/usr/bin/env python3
"""Live check OSC pleading export and optional local AI draft quality.

The check intentionally writes only to .runtime. It never edits case folders on
LUMI/NAS.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_FINAL_PDF = (
    "/Volumes/homes/lumi63181107/01_案件/一般案件/民事/"
    "2025-0028-鑫源企業社-一審-給付工程款/02_我方歷次書狀/"
    "20260112 民事聲請調查證據狀/20260114_民事聲請調查證據狀(鑫源企業社)清稿.pdf"
)
DEFAULT_FINAL_DOCX = (
    "/Volumes/homes/lumi63181107/01_案件/一般案件/民事/"
    "2025-0028-鑫源企業社-一審-給付工程款/02_我方歷次書狀/"
    "20260112 民事聲請調查證據狀/20260114_民事聲請調查證據狀(鑫源企業社)清稿.docx"
)

KEY_TERMS = [
    "民事聲請調查證據狀",
    "114年度建字第16號",
    "鑫源企業社",
    "中華電信股份有限公司花蓮營運處",
    "國營臺灣鐵路股份有限公司",
    "高國碩建築師事務所",
    "施工日誌",
    "施工照片",
]


def _run(cmd: list[str], timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def _extract_pdf_text(path: str) -> str:
    result = _run(["pdftotext", "-layout", path, "-"], timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"pdftotext failed: {result.stderr.strip()}")
    return result.stdout or ""


def _extract_docx_text(path: str) -> str:
    from docx import Document  # type: ignore
    from docx.oxml.table import CT_Tbl  # type: ignore
    from docx.oxml.text.paragraph import CT_P  # type: ignore
    from docx.table import Table  # type: ignore
    from docx.text.paragraph import Paragraph  # type: ignore

    doc = Document(path)
    lines: list[str] = []
    body = doc.element.body
    for child in body.iterchildren():
        if isinstance(child, CT_P):
            text = Paragraph(child, doc).text.strip()
            if text:
                lines.append(text)
        elif isinstance(child, CT_Tbl):
            table = Table(child, doc)
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                cells = [re.sub(r"\s+", "", c) if i < 2 else re.sub(r"\s+", " ", c).strip() for i, c in enumerate(cells)]
                if len(cells) >= 3 and cells[0] and cells[1] in {":", "："}:
                    lines.append(f"{cells[0]}：{cells[2]}")
                else:
                    joined = "　".join(c for c in cells if c)
                    if joined:
                        lines.append(joined)
    return "\n".join(lines)


def _pdf_page_count(path: str) -> int:
    result = _run(["pdfinfo", path], timeout=30)
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.startswith("Pages:"):
                try:
                    return int(line.split(":", 1)[1].strip())
                except ValueError:
                    break
    return _page_count_from_text(_extract_pdf_text(path))


def _strip_pdf_artifacts(text: str) -> str:
    lines = []
    for raw in str(text or "").replace("\f", "\n").splitlines():
        line = raw.strip()
        if re.search(r"第\s*\d+\s*頁\s*[，,]?\s*共\s*\d+\s*頁", line):
            continue
        lines.append(raw.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _normalize(text: str) -> str:
    text = str(text or "")
    text = text.replace("\u3000", "")
    text = re.sub(r"\s+", "", text)
    return text


def _page_count_from_text(text: str) -> int:
    return max(1, (text or "").count("\f") + 1)


def _key_hits(text: str) -> dict[str, bool]:
    normalized = _normalize(text)
    return {term: (_normalize(term) in normalized) for term in KEY_TERMS}


def _similarity(a: str, b: str) -> float:
    # Use a bounded prefix so one accidental huge file cannot make the check slow.
    return difflib.SequenceMatcher(None, _normalize(a)[:12000], _normalize(b)[:12000]).ratio()


def _build_ai_prompt(final_text: str) -> str:
    facts = "\n".join(line.strip() for line in final_text.splitlines() if line.strip())
    facts = facts[:2600]
    return (
        "你是台灣律師書狀助理。請根據下列資料草擬一份民事聲請調查證據狀。\n"
        "限制：不得杜撰日期、金額、證據編號或裁判字號；不足處標示（待確認）。"
        "請直接輸出書狀內文，不要 Markdown。\n\n"
        "案件資訊：臺灣花蓮地方法院 114年度建字第16號 日股；"
        "原告鑫源企業社，被告中華電信股份有限公司花蓮營運處；"
        "案由請求給付報酬。\n\n"
        "需要聲請調查：一、向國營臺灣鐵路股份有限公司調閱水電、消防施工日誌及施工照片；"
        "二、傳喚高國碩建築師事務所監造驗收人員說明驗收情形。\n\n"
        f"參考完稿片段（供格式與必要事實對照）：\n{facts}"
    )


def _run_ai(prompt: str, model: str, url: str) -> dict:
    from api.osc.drafts import _osc_clean_draft_output, _osc_generate_draft_with_ollama

    try:
        text = _osc_clean_draft_output(_osc_generate_draft_with_ollama(prompt, model, url))
        hits = _key_hits(text)
        return {
            "ok": bool(text and len(text) >= 300 and sum(hits.values()) >= 5),
            "chars": len(text),
            "key_hits": hits,
            "text": text,
            "text_preview": text[:1200],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--final-pdf", default=DEFAULT_FINAL_PDF)
    parser.add_argument("--final-docx", default=DEFAULT_FINAL_DOCX)
    parser.add_argument("--ai", action="store_true", help="also call the local OpenAI-compatible model")
    parser.add_argument("--model", default=os.environ.get("MAGI_TEXT_PRIMARY_MODEL") or "gemma-4-e4b-it-4bit")
    parser.add_argument("--url", default=os.environ.get("MAGI_OMLX_CHAT_URL") or "http://127.0.0.1:8090")
    args = parser.parse_args()

    final_pdf = Path(args.final_pdf)
    if not final_pdf.exists():
        raise SystemExit(f"missing final pdf: {final_pdf}")
    final_docx = Path(args.final_docx) if args.final_docx else None

    from api import startup

    out_dir = ROOT / ".runtime" / "osc_draft_live_exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    startup.EXPORTS_DIR = str(out_dir)

    final_text = _extract_pdf_text(str(final_pdf))
    source_text = ""
    if final_docx and final_docx.exists():
        source_text = _strip_pdf_artifacts(_extract_docx_text(str(final_docx)))
    if not source_text:
        source_text = _strip_pdf_artifacts(final_text)
    export = startup._export_osc_form_files(
        "民事聲請調查證據狀",
        source_text,
        "live_compare_鑫源企業社_民事聲請調查證據狀",
    )
    if not export.get("success"):
        raise SystemExit(f"export failed: {export}")

    generated_pdf = str((export.get("export_pdf") or {}).get("path") or "")
    generated_text = _extract_pdf_text(generated_pdf) if generated_pdf else ""
    final_pages = _pdf_page_count(str(final_pdf))
    generated_pages = _pdf_page_count(generated_pdf) if generated_pdf else 0
    hits = _key_hits(generated_text)
    sim = _similarity(source_text, generated_text)

    report = {
        "ok": bool(export.get("success") and sim >= 0.78 and abs(final_pages - generated_pages) <= 1 and sum(hits.values()) >= 7),
        "sample": "鑫源企業社 民事聲請調查證據狀",
        "final_pdf": str(final_pdf),
        "final_docx": str(final_docx) if final_docx else "",
        "generated_docx": str((export.get("export_docx") or {}).get("path") or ""),
        "generated_pdf": generated_pdf,
        "final_pages": final_pages,
        "generated_pages": generated_pages,
        "similarity": round(sim, 4),
        "key_hits": hits,
        "export": export,
    }
    if args.ai:
        report["ai"] = _run_ai(_build_ai_prompt(source_text), args.model, args.url)
        ai_text = str(report["ai"].get("text") or "")
        if ai_text:
            ai_export = startup._export_osc_form_files(
                "民事聲請調查證據狀",
                ai_text,
                "live_ai_鑫源企業社_民事聲請調查證據狀",
            )
            ai_pdf = str((ai_export.get("export_pdf") or {}).get("path") or "")
            report["ai"]["export"] = {
                "success": bool(ai_export.get("success")),
                "docx": str((ai_export.get("export_docx") or {}).get("path") or ""),
                "pdf": ai_pdf,
                "pages": _pdf_page_count(ai_pdf) if ai_pdf else 0,
            }
        report["ai"].pop("text", None)
        report["ok"] = bool(report["ok"] and report["ai"].get("ok"))

    json_path = ROOT / ".runtime" / "osc_draft_live_compare_latest.json"
    md_path = ROOT / ".runtime" / "osc_draft_live_compare_latest.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(
        "\n".join(
            [
                "# OSC Draft Live Compare",
                "",
                f"- ok: {report['ok']}",
                f"- sample: {report['sample']}",
                f"- final_pages: {final_pages}",
                f"- generated_pages: {generated_pages}",
                f"- similarity: {report['similarity']}",
                f"- key_hits: {sum(hits.values())}/{len(hits)}",
                f"- generated_docx: {report['generated_docx']}",
                f"- generated_pdf: {report['generated_pdf']}",
                f"- ai_ok: {report.get('ai', {}).get('ok', 'not_run')}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
