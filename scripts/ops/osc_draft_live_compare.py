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
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_SAMPLE_NAS_USER = (os.environ.get("MAGI_NAS_HOME_USER") or os.environ.get("MAGI_NAS_USER") or "home").strip().strip("/\\") or "home"
_SAMPLE_ACTIVE_CIVIL_ROOT = os.environ.get(
    "MAGI_OSC_DRAFT_SAMPLE_ROOT",
    f"/Volumes/homes/{_SAMPLE_NAS_USER}/01_案件/一般案件/民事",
).rstrip("/")

DEFAULT_FINAL_PDF = (
    _SAMPLE_ACTIVE_CIVIL_ROOT + "/"
    "2025-0028-鑫源企業社-一審-給付工程款/02_我方歷次書狀/"
    "20260112 民事聲請調查證據狀/20260114_民事聲請調查證據狀(鑫源企業社)清稿.pdf"
)
DEFAULT_FINAL_DOCX = (
    _SAMPLE_ACTIVE_CIVIL_ROOT + "/"
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

DEFAULT_SAMPLES = [
    {
        "id": "xinyuan_evidence_motion",
        "sample": "鑫源企業社 民事聲請調查證據狀",
        "title": "民事聲請調查證據狀",
        "final_pdf": DEFAULT_FINAL_PDF,
        "final_docx": DEFAULT_FINAL_DOCX,
        "key_terms": KEY_TERMS,
        "ai": True,
        "ai_facts": (
            "臺灣花蓮地方法院 114年度建字第16號 日股；原告鑫源企業社，"
            "被告中華電信股份有限公司花蓮營運處；聲請調閱國營臺灣鐵路股份有限公司"
            "施工日誌、施工照片，並傳喚高國碩建築師事務所監造驗收人員。"
        ),
    },
    {
        "id": "huang_labor_complaint",
        "sample": "黃語玲 民事起訴狀",
        "title": "民事起訴狀",
        "final_pdf": (
            _SAMPLE_ACTIVE_CIVIL_ROOT + "/"
            "2026-0019-黃語玲-一審-給付資遣費等/02_我方歷次書狀/"
            "20260312 民事起訴狀/20260317 民事起訴狀(黃語玲)清稿.pdf"
        ),
        # 清稿 PDF 內含最終換頁；v1.docx 的版面與清稿頁數不同，故以清稿 PDF 為回歸基準。
        "final_docx": "",
        "key_terms": ["民事起訴狀", "黃語玲", "給付資遣費", "加班費", "職業災害"],
    },
    {
        "id": "zhang_resolution_nullity_long",
        "sample": "張國賢 民事起訴狀長篇",
        "title": "民事起訴狀",
        "final_pdf": (
            _SAMPLE_ACTIVE_CIVIL_ROOT + "/"
            "2025-0122-張國賢-一審-確認決議無效/02_我方歷次書狀/"
            "20251204 民事起訴狀/20251212 民事起訴狀(張國賢)"
            "存底暨自行收納款項收據（聲請費新台幣3000元）.pdf"
        ),
        "key_terms": ["民事起訴狀", "張國賢", "花蓮區漁會", "決議無效", "理事長"],
        "long": True,
        "ocr_wrap_chars": 45,
    },
    {
        "id": "xie_partition_estate_long",
        "sample": "謝光明 民事起訴暨聲請調解狀長篇",
        "title": "民事起訴暨聲請調解狀",
        "final_pdf": (
            _SAMPLE_ACTIVE_CIVIL_ROOT + "/"
            "2025-0109-謝光明-一審-分割遺產/02_我方歷次書狀/"
            "20241225 民事起訴暨聲請調解狀/20241225 民事起訴暨聲請調解狀(謝光明)存底.pdf"
        ),
        "key_terms": ["民事起訴", "聲請調解", "謝光明", "分割共有物", "遺產"],
        "long": True,
        "ocr_wrap_chars": 45,
    },
    {
        "id": "lei_defense_mediation_long",
        "sample": "雷順祥 民事答辯暨聲請調解狀長篇",
        "title": "民事答辯暨聲請調解狀",
        "final_pdf": (
            _SAMPLE_ACTIVE_CIVIL_ROOT + "/"
            "2025-0107-雷順祥-一審-侵權行為/02_我方歷次書狀/"
            "20240513 民事答辯暨聲請調解狀/"
            "20240513 113年度花簡字第103號民事答辯暨聲請調解狀(雷順祥)存底.pdf"
        ),
        "key_terms": ["民事答辯", "聲請調解", "雷順祥", "113年度花簡字第103號"],
        "long": True,
        "ocr_compact": False,
    },
]


def _run(cmd: list[str], timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def _extract_pdf_text(path: str) -> str:
    result = _run(["pdftotext", "-layout", path, "-"], timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"pdftotext failed: {result.stderr.strip()}")
    return result.stdout or ""


def _ocr_pdf_text(path: str, *, max_pages: int | None = None) -> str:
    import hashlib
    import fitz  # type: ignore

    pdf_path = Path(path)
    stat = pdf_path.stat()
    cache_dir = ROOT / ".runtime" / "osc_draft_ocr_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{pdf_path}|{stat.st_mtime_ns}|{stat.st_size}".encode("utf-8")).hexdigest()[:16]
    cache_path = cache_dir / f"{key}.txt"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8", errors="ignore")
    tesseract = os.environ.get("MAGI_TESSERACT_BIN") or "tesseract"
    langs = os.environ.get("MAGI_OSC_DRAFT_OCR_LANGS") or "chi_tra+eng"
    dpi = int(os.environ.get("MAGI_OSC_DRAFT_OCR_DPI", "150") or "150")
    if max_pages is None:
        max_pages = int(os.environ.get("MAGI_OSC_DRAFT_OCR_MAX_PAGES", "40") or "40")
    chunks: list[str] = []
    with fitz.open(str(pdf_path)) as doc:
        total = min(len(doc), max_pages)
        for idx in range(total):
            page = doc.load_page(idx)
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
                pix.save(tmp.name)
                result = _run([tesseract, tmp.name, "stdout", "-l", langs, "--psm", "6"], timeout=90)
            if result.returncode == 0 and result.stdout.strip():
                chunks.append(result.stdout.strip())
    text = "\n\n".join(chunks).strip()
    cache_path.write_text(text, encoding="utf-8")
    return text


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


def _looks_like_paragraph_boundary(line: str) -> bool:
    text = re.sub(r"\s+", "", str(line or ""))
    if not text:
        return True
    if len(text) <= 28 and text.endswith("狀"):
        return True
    if re.match(r"^(案號|股別|案由|原告|被告|聲請人|相對人|法定代理人|訴訟代理人|住|設|電話|傳真|手機)[:：]", text):
        return True
    if re.match(r"^(壹|貳|參|肆|伍|陸|柒|捌|玖|拾)[、.．]", text):
        return True
    if re.match(r"^[一二三四五六七八九十]+[、.．]", text):
        return True
    if re.match(r"^[（(][一二三四五六七八九十0-9]+[）)]", text):
        return True
    if re.match(r"^(謹|此致|中華民國|具狀人|撰狀人|訴訟代理人)", text):
        return True
    return False


def _compact_ocr_text(text: str) -> str:
    compacted: list[str] = []
    current = ""
    for raw in str(text or "").splitlines():
        line = raw.strip().strip("|")
        line = re.sub(r"^[\s\dIl|_.,，。:：;；\-–—]+", "", line).strip()
        line = re.sub(r"\s+", " ", line)
        if not line:
            if current:
                compacted.append(current.strip())
                current = ""
            continue
        if _looks_like_paragraph_boundary(line):
            if current:
                compacted.append(current.strip())
            current = line
            continue
        if current:
            current += line if re.search(r"[，、；：:「（(]$", current) else line
        else:
            current = line
    if current:
        compacted.append(current.strip())
    return "\n".join(compacted)


def _wrap_ocr_text(text: str, width: int) -> str:
    if width <= 0:
        return text
    out: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            out.append("")
            continue
        while len(line) > width:
            out.append(line[:width])
            line = line[width:]
        out.append(line)
    return "\n".join(out)


def _normalize(text: str) -> str:
    text = str(text or "")
    text = text.replace("\u3000", "")
    text = re.sub(r"\s+", "", text)
    return text


def _page_count_from_text(text: str) -> int:
    return max(1, (text or "").count("\f") + 1)


def _key_hits(text: str, terms: list[str] | None = None) -> dict[str, bool]:
    normalized = _normalize(text)
    return {term: (_normalize(term) in normalized) for term in (terms or KEY_TERMS)}


def _similarity(a: str, b: str) -> float:
    # Use a bounded prefix so one accidental huge file cannot make the check slow.
    return difflib.SequenceMatcher(None, _normalize(a)[:12000], _normalize(b)[:12000]).ratio()


def _build_ai_prompt(final_text: str, sample: dict) -> str:
    facts = "\n".join(line.strip() for line in final_text.splitlines() if line.strip())
    facts = facts[:2600]
    title = str(sample.get("title") or "民事書狀")
    ai_facts = str(sample.get("ai_facts") or "").strip()
    return (
        f"你是台灣律師書狀助理。請根據下列資料草擬一份{title}。\n"
        "限制：不得杜撰日期、金額、證據編號或裁判字號；不足處標示（待確認）。"
        "請直接輸出書狀內文，不要 Markdown。\n\n"
        f"案件資訊：{ai_facts or '請依參考完稿片段整理，不足處標示（待確認）。'}\n\n"
        f"參考完稿片段（供格式與必要事實對照）：\n{facts}"
    )


def _run_ai(prompt: str, model: str, url: str, key_terms: list[str]) -> dict:
    from api.osc.drafts import _osc_clean_draft_output, _osc_generate_draft_with_ollama

    try:
        text = _osc_clean_draft_output(_osc_generate_draft_with_ollama(prompt, model, url))
        hits = _key_hits(text, key_terms)
        min_hits = max(1, min(5, int(len(key_terms) * 0.6)))
        return {
            "ok": bool(text and len(text) >= 300 and sum(hits.values()) >= min_hits),
            "chars": len(text),
            "key_hits": hits,
            "text": text,
            "text_preview": text[:1200],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _sample_from_args(args: argparse.Namespace) -> list[dict]:
    if args.final_pdf:
        return [
            {
                "id": "custom",
                "sample": args.sample_name or Path(args.final_pdf).stem,
                "title": args.title or Path(args.final_pdf).stem,
                "final_pdf": args.final_pdf,
                "final_docx": args.final_docx or "",
                "key_terms": [x.strip() for x in str(args.key_terms or "").split(",") if x.strip()] or KEY_TERMS,
                "ai": True,
            }
        ]
    selected = DEFAULT_SAMPLES
    if args.long_only:
        selected = [x for x in selected if x.get("long")]
    if args.sample:
        wanted = {x.strip() for x in args.sample.split(",") if x.strip()}
        selected = [x for x in selected if x.get("id") in wanted or x.get("sample") in wanted]
    return list(selected)


def _compare_sample(sample: dict, startup, out_dir: Path, args: argparse.Namespace, index: int) -> dict:
    final_pdf = Path(str(sample.get("final_pdf") or ""))
    if not final_pdf.exists():
        return {"ok": False, "sample": sample.get("sample") or sample.get("id"), "error": f"missing final pdf: {final_pdf}"}
    final_docx_raw = str(sample.get("final_docx") or "").strip()
    final_docx = Path(final_docx_raw) if final_docx_raw else None
    key_terms = list(sample.get("key_terms") or KEY_TERMS)
    title = str(sample.get("title") or sample.get("sample") or "書狀草稿")
    sample_name = str(sample.get("sample") or sample.get("id") or final_pdf.stem)
    safe_name = re.sub(r'[\\/:*?"<>|]+', "_", sample_name)

    final_text = _extract_pdf_text(str(final_pdf))
    source_text = ""
    source_kind = "pdf"
    if final_docx and final_docx.exists():
        source_text = _strip_pdf_artifacts(_extract_docx_text(str(final_docx)))
        source_kind = "docx"
    if not source_text:
        source_text = _strip_pdf_artifacts(final_text)
    if not source_text:
        raw_ocr = _strip_pdf_artifacts(_ocr_pdf_text(str(final_pdf), max_pages=sample.get("ocr_max_pages")))
        if sample.get("ocr_compact") is False:
            source_text = raw_ocr
        else:
            source_text = _compact_ocr_text(raw_ocr)
        source_text = _wrap_ocr_text(source_text, int(sample.get("ocr_wrap_chars") or 0))
        source_kind = "ocr"
    export = startup._export_osc_form_files(title, source_text, f"live_compare_{index:02d}_{safe_name}")
    if not export.get("success"):
        return {"ok": False, "sample": sample_name, "error": f"export failed: {export}"}

    generated_pdf = str((export.get("export_pdf") or {}).get("path") or "")
    generated_text = _extract_pdf_text(generated_pdf) if generated_pdf else ""
    final_pages = _pdf_page_count(str(final_pdf))
    generated_pages = _pdf_page_count(generated_pdf) if generated_pdf else 0
    hits = _key_hits(generated_text, key_terms)
    sim = _similarity(source_text, generated_text)
    min_hits = max(1, int(len(key_terms) * 0.7 + 0.999))
    page_delta_limit = int(sample.get("page_delta_limit") or (2 if final_pages >= 10 else 1))
    report = {
        "ok": bool(export.get("success") and sim >= float(sample.get("min_similarity") or 0.72) and abs(final_pages - generated_pages) <= page_delta_limit and sum(hits.values()) >= min_hits),
        "sample": sample_name,
        "id": sample.get("id") or "",
        "source_kind": source_kind,
        "source_chars": len(source_text),
        "final_pdf": str(final_pdf),
        "final_docx": str(final_docx) if final_docx else "",
        "generated_docx": str((export.get("export_docx") or {}).get("path") or ""),
        "generated_pdf": generated_pdf,
        "final_pages": final_pages,
        "generated_pages": generated_pages,
        "page_delta": generated_pages - final_pages,
        "similarity": round(sim, 4),
        "key_hits": hits,
        "key_hit_count": sum(hits.values()),
        "key_term_count": len(hits),
        "export_success": bool(export.get("success")),
    }
    should_ai = bool(args.ai and (sample.get("ai") or args.ai_all or index == 1))
    if should_ai:
        report["ai"] = _run_ai(_build_ai_prompt(source_text, sample), args.model, args.url, key_terms)
        ai_text = str(report["ai"].get("text") or "")
        if ai_text:
            ai_export = startup._export_osc_form_files(title, ai_text, f"live_ai_{index:02d}_{safe_name}")
            ai_pdf = str((ai_export.get("export_pdf") or {}).get("path") or "")
            report["ai"]["export"] = {
                "success": bool(ai_export.get("success")),
                "docx": str((ai_export.get("export_docx") or {}).get("path") or ""),
                "pdf": ai_pdf,
                "pages": _pdf_page_count(ai_pdf) if ai_pdf else 0,
            }
        report["ai"].pop("text", None)
        report["ok"] = bool(report["ok"] and report["ai"].get("ok"))
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--final-pdf", default="", help="Run one custom sample instead of the built-in set.")
    parser.add_argument("--final-docx", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--sample-name", default="")
    parser.add_argument("--key-terms", default="", help="Comma-separated key terms for a custom sample.")
    parser.add_argument("--sample", default="", help="Comma-separated built-in sample ids/names to run.")
    parser.add_argument("--long-only", action="store_true", help="Run only built-in samples with >=10-page source PDFs.")
    parser.add_argument("--ai", action="store_true", help="also call the local OpenAI-compatible model for selected sample(s)")
    parser.add_argument("--ai-all", action="store_true", help="with --ai, call the local model for every sample")
    parser.add_argument("--model", default=os.environ.get("MAGI_TEXT_PRIMARY_MODEL") or "gemma-4-e4b-it-4bit")
    parser.add_argument("--url", default=os.environ.get("MAGI_OMLX_CHAT_URL") or "http://127.0.0.1:8090")
    args = parser.parse_args()

    from api import startup

    out_dir = ROOT / ".runtime" / "osc_draft_live_exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    startup.EXPORTS_DIR = str(out_dir)

    samples = _sample_from_args(args)
    results = [_compare_sample(sample, startup, out_dir, args, idx) for idx, sample in enumerate(samples, 1)]
    report = {
        "ok": all(r.get("ok") for r in results) if results else False,
        "sample_count": len(results),
        "passed": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok")),
        "results": results,
    }

    json_path = ROOT / ".runtime" / "osc_draft_live_compare_latest.json"
    md_path = ROOT / ".runtime" / "osc_draft_live_compare_latest.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(
        "\n".join(
            [
                "# OSC Draft Live Compare",
                "",
                f"- ok: {report['ok']}",
                f"- samples: {report['passed']}/{report['sample_count']}",
                "",
                *[
                    (
                        f"- {r.get('sample')}: ok={r.get('ok')} pages={r.get('final_pages')}->{r.get('generated_pages')} "
                        f"similarity={r.get('similarity')} key_hits={r.get('key_hit_count')}/{r.get('key_term_count')} "
                        f"ai_ok={r.get('ai', {}).get('ok', 'not_run')}"
                    )
                    for r in results
                ],
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
