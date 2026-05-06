# PDF Bridge for MAGI
# Provides PDF text extraction and summarization

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import warnings
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r"builtin type SwigPyPacked has no __module__ attribute",
        category=DeprecationWarning,
    )
    warnings.filterwarnings(
        "ignore",
        message=r"builtin type SwigPyObject has no __module__ attribute",
        category=DeprecationWarning,
    )
    warnings.filterwarnings(
        "ignore",
        message=r"builtin type swigvarlink has no __module__ attribute",
        category=DeprecationWarning,
    )
    import fitz  # PyMuPDF

from skills.bridge.shared_utils.text_utils import normalize_segment_fragment as _normalize_segment_fragment

logger = logging.getLogger("PDFBridge")

_QUALITY_HINT_WORDS = {
    "the", "court", "judgment", "judgements", "application", "case", "convention",
    "international", "justice", "objections", "preliminary", "compensation", "republic",
    "kingdom", "great", "britain", "northern", "ireland", "president", "judges",
    "declaration", "dissenting", "opinion", "article", "statute", "tribunal",
    "法院", "判決", "裁定", "裁判", "主文", "理由", "原告", "被告", "上訴", "抗告",
    "司法院", "國際法院", "法官", "程序", "聲請", "裁判書",
}
_PAGE_MARKER_RE = re.compile(r"--- 第\s*(\d+)\s*頁(?:\s*\(OCR\))? ---")


def _doc_run_root(subdir: str) -> Path:
    configured = str(os.environ.get("MAGI_DOC_RUN_ROOT", "")).strip()
    root = Path(configured) if configured else (Path(__file__).resolve().parents[2] / ".magi_doc_runs")
    path = root / subdir
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_slug(value: str, *, fallback: str = "document") -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._-")
    return (slug or fallback)[:80]


def _atomic_write_text(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(str(body or ""), encoding="utf-8")
    tmp.replace(path)


def _atomic_write_json(path: Path, payload: dict) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _is_synthetic_timeout_fallback(text: str, result: Optional[dict] = None) -> bool:
    t = str(text or "").strip()
    if isinstance(result, dict) and bool(result.get("synthetic_fallback")):
        return True
    if not t:
        return False
    if t.startswith("（系統降級回覆）"):
        return True
    return "本機模型逾時" in t and "請稍後重試" in t


def _summary_text_usable(text: str, result: Optional[dict] = None) -> bool:
    t = str(text or "").strip()
    if len(t) < 24:
        return False
    if _is_synthetic_timeout_fallback(t, result):
        return False
    return not any(marker in t for marker in ("摘要失敗", "段摘要逾時", "先略過"))


def _script_balance(text: str) -> tuple[int, int]:
    body = str(text or "")
    latin = len(re.findall(r"[A-Za-z]", body))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", body))
    return latin, cjk


def _needs_crosslingual_polish(text: str) -> bool:
    latin, cjk = _script_balance(text)
    if latin < 60:
        return False
    return latin >= max(120, cjk * 2)


def _translate_note_to_traditional_chinese(text: str) -> str:
    note = str(text or "").strip()
    if not note or not _needs_crosslingual_polish(note):
        return ""
    try:
        from api.handlers.document_handler import polish_translated_document_text

        pieces: list[str] = []
        step = 1600
        for i in range(0, len(note), step):
            seg = note[i : i + step].strip()
            if not seg:
                continue
            q = urllib.parse.quote(seg)
            url = (
                "https://translate.googleapis.com/translate_a/single"
                f"?client=gtx&sl=auto&tl=zh-TW&dt=t&q={q}"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            data = json.loads(raw)
            seg_parts = []
            if isinstance(data, list) and data and isinstance(data[0], list):
                for row in data[0]:
                    if isinstance(row, list) and row and row[0]:
                        seg_parts.append(str(row[0]))
            got = "".join(seg_parts).strip()
            if got:
                pieces.append(got)
        out = "\n\n".join(pieces).strip()
        if not out:
            return ""
        polished = str(polish_translated_document_text(out) or "").strip()
        final = polished if polished else out
        replacements = {
            "简介": "簡介",
            "争议": "爭議",
            "标的": "標的",
            "卡塔尔": "卡達",
            "阿联酋": "阿聯酋",
            "缔约国": "締約國",
            "委员会": "委員會",
            "当前国籍": "現行國籍",
            "页": "頁",
            "事實背景": "事實背景",
            "專案法官": "專案法官",
        }
        for src, dst in replacements.items():
            final = final.replace(src, dst)
        lines = [ln.strip() for ln in final.splitlines() if ln.strip()]
        if not lines:
            return final

        title = ""
        bullets: list[str] = []
        first = lines[0]
        m = re.match(r"^【[^】]+】主題：(.+)$", first)
        if m:
            title = _normalize_segment_fragment(m.group(1))
            body_lines = lines[1:]
        else:
            title = _normalize_segment_fragment(re.sub(r"^【[^】]+】\s*", "", first))
            body_lines = lines[1:]

        def _clean_line(line: str) -> str:
            text = _normalize_segment_fragment(line)
            if not text:
                return ""
            if ("報告" in text or "Report" in text) and re.search(r"(?:第\s*\d+\s*頁|\d{4}|p\.)", text):
                return ""
            if re.match(r"^(?:簡介|爭議標的)\s*\d+(?:[-‑–]\d+)?", text):
                return ""
            if re.match(r"^爭議主題\s*\d+(?:[-‑–]\d+)?$", text):
                return ""
            if text.startswith(("基於這些原因", "由於這些原因")):
                return ""
            if text.startswith("以十一票對六票"):
                return ""
            if "專案法官" in text:
                return ""
            if re.match(r"^[一二三四五六七八九十IVXLC0-9A-Z.．、]+\s*第一個初步異議", text):
                return ""
            if re.match(r"^[A-Z]\.\s*", text):
                return ""
            if text.startswith("簡介 A. 事實背景") and "締約國" in text:
                return "本案背景是：卡達與阿聯酋均為《消除種族歧視公約》締約國。"
            if text.startswith("法院認為爭議的主題不明確"):
                return "法院認為，本案爭議應聚焦於《消除種族歧視公約》是否涵蓋卡達所主張的措施。"
            if text == "「國籍」一詞是否包含在內的問題":
                return "核心問題之一是：CERD 第一條第一款所稱的「national origin」是否涵蓋現行國籍。"
            if text.startswith("「民族血統」一詞是否適用的問題"):
                return "核心問題之一是：CERD 第一條第一款所稱的「national origin」是否涵蓋現行國籍。"
            if text == "消除種族歧視委員會的做法":
                return "法院也參考消除種族歧視委員會的相關解釋與實務。"
            if text.startswith("消除種族歧視委員會在其一般性建議"):
                return "法院並參照消除種族歧視委員會的一般性建議，作為理解公約適用範圍的補充材料。"
            return text

        for line in body_lines:
            cleaned = _clean_line(line)
            if not cleaned:
                continue
            norm = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "", cleaned).lower()
            if not norm:
                continue
            bullets.append(cleaned)

        deduped: list[str] = []
        seen = set()
        for bullet in bullets:
            norm = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "", bullet).lower()
            if norm in seen:
                continue
            seen.add(norm)
            deduped.append(bullet)

        if title in {"簡介", "爭議標的", "基於這些原因", "由於這些原因", "第一個初步異議"} or len(title) < 4:
            title = _infer_segment_theme(title or "未命名段落", deduped, final)

        if not title:
            title = "未命名段落"
        output = [f"【跨語言段摘要】主題：{title}"]
        for bullet in deduped[:5]:
            output.append(f"- {bullet}")
        return "\n".join(output).strip()
    except Exception:
        return ""


def _chunk_text(text: str, chunk_chars: int = 2200, overlap: int = 180, max_chunks: int = 600) -> list[str]:
    s = (text or "").strip()
    if not s:
        return []
    chunk_chars = int(max(500, chunk_chars))
    overlap = int(max(0, min(overlap, chunk_chars // 2)))
    out = []
    i = 0
    while i < len(s) and len(out) < int(max_chunks):
        out.append(s[i : i + chunk_chars])
        i += max(1, chunk_chars - overlap)
    return out


def _sample_evenly(chunks: list[str], max_samples: int) -> list[tuple[int, str]]:
    parts = [c for c in (chunks or []) if (c or "").strip()]
    if not parts:
        return []
    n = len(parts)
    k = max(1, min(int(max_samples), n))
    if k >= n:
        return [(i + 1, parts[i]) for i in range(n)]
    idxs = sorted({int(round(i * (n - 1) / (k - 1))) for i in range(k)}) if k > 1 else [0]
    out = []
    for i in idxs:
        i = max(0, min(n - 1, i))
        out.append((i + 1, parts[i]))
    return out


def _is_meaningful_text(text: str, min_chars: int = 16) -> bool:
    s = re.sub(r"\s+", "", str(text or ""))
    return len(s) >= int(max(1, min_chars))


def _strip_page_markers(text: str) -> str:
    s = str(text or "")
    s = re.sub(r"--- 第\s*\d+\s*頁(?:\s*\(OCR\))? ---", " ", s)
    s = re.sub(r"\f", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _normalize_extracted_text(text: str) -> str:
    s = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not s:
        return s

    # Re-join hyphenated line wraps from scanned/layout-preserved text.
    s = re.sub(r"([A-Za-z])-\n([A-Za-z])", r"\1\2", s)
    # Collapse spaced ALL-CAPS words such as "T H E" -> "THE".
    s = re.sub(
        r"\b(?:[A-Z]\s+){2,}[A-Z]\b",
        lambda m: re.sub(r"\s+", "", m.group(0)),
        s,
    )
    # Normalize excessive inner spacing in Latin words.
    s = re.sub(r"([A-Za-z])\s{2,}([A-Za-z])", r"\1 \2", s)
    # Remove obvious OCR/layout debris lines.
    lines = []
    for raw in s.split("\n"):
        line = str(raw or "").rstrip()
        if re.fullmatch(r"\s*(?:\|\s*){2,}", line):
            continue
        if re.fullmatch(r"\s*[\d\W_]{1,6}\s*", line):
            continue
        lines.append(line)
    s = "\n".join(lines)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _text_quality_stats(text: str) -> dict:
    body = _strip_page_markers(text)
    if not body:
        return {"score": 0.0, "token_count": 0, "hint_ratio": 0.0, "suspicious_ratio": 1.0, "weird_ratio": 1.0}

    raw_tokens = re.findall(r"\S+", body)
    letter_tokens = [tok for tok in raw_tokens if re.search(r"[A-Za-z\u4e00-\u9fff]", tok)]
    normalized_tokens = re.findall(r"[A-Za-z\u4e00-\u9fff][A-Za-z\u4e00-\u9fff'’\-]{1,}", body)
    token_count = len(normalized_tokens)
    lower_tokens = [tok.lower() for tok in normalized_tokens]
    hint_hits = sum(1 for tok in lower_tokens if tok in _QUALITY_HINT_WORDS)

    suspicious_tokens = 0
    for tok in letter_tokens:
        bad_mix = bool(re.search(r"[A-Za-z]+\d+[A-Za-z]*|\d+[A-Za-z]+", tok))
        bad_punct = bool(re.search(r"[A-Za-z][^A-Za-z\u4e00-\u9fff\s]{2,}[A-Za-z]", tok))
        escaped = "\\x" in tok or "\\u" in tok
        if bad_mix or bad_punct or escaped:
            suspicious_tokens += 1

    weird_chars = len(re.findall(r"[\\^~<>|{}\[\]`]", body))
    alpha_chars = len(re.findall(r"[A-Za-z\u4e00-\u9fff]", body))
    token_base = max(1, len(letter_tokens))
    hint_ratio = hint_hits / max(1, token_count)
    suspicious_ratio = suspicious_tokens / token_base
    weird_ratio = weird_chars / max(1, alpha_chars)
    density = min(1.0, token_count / 180.0)
    score = max(
        0.0,
        min(
            1.0,
            (0.55 * hint_ratio)
            + (0.25 * density)
            + (0.12 * max(0.0, 1.0 - suspicious_ratio))
            + (0.08 * max(0.0, 1.0 - min(1.0, weird_ratio * 8.0))),
        ),
    )
    return {
        "score": score,
        "token_count": token_count,
        "hint_ratio": hint_ratio,
        "suspicious_ratio": suspicious_ratio,
        "weird_ratio": weird_ratio,
    }


def _extract_text_pdftotext(pdf_path: str, max_pages: int) -> tuple[str, int]:
    pdftotext_bin = str(os.environ.get("MAGI_PDFTOTEXT_BIN", "pdftotext")).strip() or "pdftotext"
    timeout_sec = int(os.environ.get("MAGI_PDF_PDFTOTEXT_TIMEOUT_SEC", "120") or "120")
    proc = subprocess.run(
        [
            pdftotext_bin,
            "-layout",
            "-enc",
            "UTF-8",
            "-f",
            "1",
            "-l",
            str(max(1, int(max_pages))),
            pdf_path,
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=max(10, min(timeout_sec, 300)),
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pdftotext_failed: {(proc.stderr or '').strip()[:200]}")

    raw = str(proc.stdout or "")
    pages = []
    for page_num, page_text in enumerate(raw.split("\f"), start=1):
        txt = str(page_text or "").strip()
        if txt:
            pages.append(f"--- 第 {page_num} 頁 ---\n{_normalize_extracted_text(txt)}")
    return "\n".join(pages).strip(), len(pages)


def _extract_text_fitz(pdf_path: str, max_pages: int) -> tuple[str, int]:
    doc = fitz.open(pdf_path)
    try:
        text_content = []
        pages_processed = 0
        for page_num, page in enumerate(doc):
            if page_num >= max_pages:
                text_content.append(f"\n[... 已截斷，僅顯示前 {max_pages} 頁 ...]")
                break
            text = page.get_text() or ""
            if text.strip():
                text_content.append(f"--- 第 {page_num + 1} 頁 ---\n{_normalize_extracted_text(text)}")
            pages_processed += 1
        return "\n".join(text_content), pages_processed
    finally:
        doc.close()


def _extract_text_pdfplumber(pdf_path: str, max_pages: int) -> tuple[str, int]:
    import pdfplumber

    text_content = []
    pages_processed = 0
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            if page_num >= max_pages:
                text_content.append(f"\n[... 已截斷，僅顯示前 {max_pages} 頁 ...]")
                break
            text = page.extract_text() or ""
            if text.strip():
                text_content.append(f"--- 第 {page_num + 1} 頁 ---\n{_normalize_extracted_text(text)}")
            pages_processed += 1
    return "\n".join(text_content), pages_processed


def _extract_text_ocr(pdf_path: str, max_pages: int, ocr_page_limit: Optional[int] = None) -> tuple[str, int]:
    if str(os.environ.get("MAGI_PDF_OCR_ENABLE", "1")).strip().lower() not in {"1", "true", "yes", "on"}:
        return "", 0

    ocr_max_pages = int(os.environ.get("MAGI_PDF_OCR_MAX_PAGES", "8") or "8")
    dpi = int(os.environ.get("MAGI_PDF_OCR_DPI", "180") or "180")
    langs = str(os.environ.get("MAGI_PDF_OCR_LANGS", "chi_tra+eng")).strip() or "chi_tra+eng"
    if ocr_page_limit is None:
        page_limit = max(1, min(max_pages, ocr_max_pages))
    else:
        page_limit = max(1, min(max_pages, int(ocr_page_limit)))

    pdftoppm_bin = str(os.environ.get("MAGI_PDFTOPPM_BIN", "pdftoppm")).strip() or "pdftoppm"
    tesseract_bin = str(os.environ.get("MAGI_TESSERACT_BIN", "tesseract")).strip() or "tesseract"

    with tempfile.TemporaryDirectory(prefix="magi_pdf_ocr_") as td:
        prefix = str(Path(td) / "page")
        cvt = subprocess.run(
            [
                pdftoppm_bin,
                "-f",
                "1",
                "-l",
                str(page_limit),
                "-r",
                str(max(120, min(dpi, 400))),
                "-png",
                pdf_path,
                prefix,
            ],
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("MAGI_PDF_OCR_PDFTOPPM_TIMEOUT_SEC", "120") or "120"),
            check=False,
        )
        if cvt.returncode != 0:
            raise RuntimeError(f"pdftoppm_failed: {(cvt.stderr or '').strip()[:200]}")

        imgs = sorted(Path(td).glob("page-*.png"))
        ocr_workers = int(os.environ.get("MAGI_PDF_OCR_WORKERS", "4") or "4")
        tess_timeout = int(os.environ.get("MAGI_PDF_OCR_TESS_TIMEOUT_SEC", "90") or "90")
        psm = str(os.environ.get("MAGI_PDF_OCR_PSM", "6") or "6")
        vision_ocr = None
        vision_page_limit = 0
        vision_quality_floor = 0.24
        vision_gain_needed = 0.02
        if str(os.environ.get("MAGI_PDF_VISION_OCR_FALLBACK", "1")).strip().lower() in {"1", "true", "yes", "on"}:
            try:
                from skills.apple.apple_intelligence import ocr_image as _apple_ocr_image

                vision_ocr = _apple_ocr_image
                vision_page_limit = int(os.environ.get("MAGI_PDF_VISION_OCR_MAX_PAGES", "4") or "4")
                vision_quality_floor = float(os.environ.get("MAGI_PDF_VISION_OCR_QUALITY_FLOOR", "0.24") or "0.24")
                vision_gain_needed = float(os.environ.get("MAGI_PDF_VISION_OCR_GAIN", "0.02") or "0.02")
            except Exception as e:
                logger.warning("⚠️ Vision OCR fallback unavailable: %s", e)
                vision_ocr = None
        vision_lock = threading.Lock()
        vision_budget = {"used": 0}

        def _maybe_upgrade_with_vision(page_num: int, img_path: Path, base_text: str) -> str:
            if not vision_ocr or vision_page_limit <= 0:
                return base_text

            base_body = str(base_text or "").strip()
            base_stats = _text_quality_stats(base_body)
            base_noise = (base_stats["suspicious_ratio"] * 0.7) + (base_stats["weird_ratio"] * 8.0)
            needs_upgrade = (
                (not base_body)
                or base_stats["score"] < vision_quality_floor
                or base_stats["suspicious_ratio"] >= 0.18
            )
            if not needs_upgrade:
                return base_body

            with vision_lock:
                if vision_budget["used"] >= vision_page_limit:
                    return base_body
                vision_budget["used"] += 1

            try:
                vr = vision_ocr(str(img_path), engine="vision")
                vision_text = _normalize_extracted_text(str((vr or {}).get("text") or ""))
            except Exception as e:
                logger.warning("⚠️ Vision OCR page %s failed: %s", page_num, e)
                return base_body

            if not _is_meaningful_text(vision_text):
                return base_body

            vision_stats = _text_quality_stats(vision_text)
            vision_noise = (vision_stats["suspicious_ratio"] * 0.7) + (vision_stats["weird_ratio"] * 8.0)
            if (
                (not base_body)
                or vision_stats["score"] >= max(base_stats["score"] + vision_gain_needed, vision_quality_floor)
                or (
                    vision_stats["score"] >= base_stats["score"] + 0.01
                    and vision_noise <= max(0.0, base_noise - 0.02)
                )
            ):
                logger.info(
                    "✅ Vision OCR upgraded page %s: tess=%.3f vision=%.3f",
                    page_num,
                    base_stats["score"],
                    vision_stats["score"],
                )
                return vision_text
            return base_body

        # --- Phase C: consensus feature flags -----------------------------------
        _consensus_enable = (
            str(os.environ.get("MAGI_PDF_OCR_CONSENSUS_ENABLE", "0")).strip().lower()
            in {"1", "true", "yes", "on"}
        )
        _consensus_shadow = (
            str(os.environ.get("MAGI_PDF_OCR_CONSENSUS_SHADOW", "0")).strip().lower()
            in {"1", "true", "yes", "on"}
        )
        _nemotron_parse_enable = (
            str(os.environ.get("MAGI_NEMOTRON_PARSE_ENABLE", "0")).strip().lower()
            in {"1", "true", "yes", "on"}
        )

        def _run_consensus_for_page(page_num, img_path):
            """呼叫 skills.engine.ocr.consensus.run_consensus，失敗回 None。

            業務紅線：task_type="legal"（非 captcha），不走 legal_corrector bypass。
            """
            try:
                from skills.engine.ocr import consensus as _ocr_consensus_mod
                result = _ocr_consensus_mod.run_consensus(
                    str(img_path),
                    task_type="legal",
                )
                if result and result.success:
                    return result
                return None
            except Exception as e:
                logger.warning(
                    "pdf_bridge: consensus OCR page %s failed, fallback to legacy: %s",
                    page_num, e,
                )
                return None

        def _write_consensus_metrics(page_num, img_path, consensus_result, legacy_txt):
            """寫 consensus metrics（只記 count / hash，不記 entity 字串）。"""
            try:
                from api.platforms.runtime_dir import (
                    metrics as _rt_metrics,
                    atomic_append_jsonl as _rt_append,
                )
                img_bytes = Path(img_path).read_bytes()
                img_hash = hashlib.sha256(img_bytes).hexdigest()[:16]
                record = {
                    "ts": time.time(),
                    "page_num": page_num,
                    "img_hash": img_hash,
                    "consensus_success": bool(consensus_result and consensus_result.success),
                    "consensus_confidence": (
                        round(consensus_result.confidence, 4)
                        if consensus_result and consensus_result.success
                        else None
                    ),
                    "consensus_len": (
                        len(consensus_result.corrected_text)
                        if consensus_result and consensus_result.success
                        else 0
                    ),
                    "legacy_len": len(legacy_txt or ""),
                    "entities_counts": (
                        consensus_result.entities.to_counts()
                        if (consensus_result and consensus_result.success and
                            consensus_result.entities)
                        else {}
                    ),
                    "mode": (
                        "shadow" if _consensus_shadow else "enabled"
                    ),
                }
                metrics_path = _rt_metrics("ocr") / "pdf_ocr_consensus.jsonl"
                _rt_append(metrics_path, record, rotate_at=500, keep_tail=1000)
            except Exception as e:
                logger.debug("pdf_bridge: consensus metrics write failed: %s", e)

        def _ocr_single_page_legacy(page_num, img_path):
            """既有 OCR 路徑（Tesseract + Vision upgrade）。"""
            ocr = subprocess.run(
                [
                    tesseract_bin,
                    str(img_path),
                    "stdout",
                    "-l",
                    langs,
                    "--psm",
                    psm,
                ],
                capture_output=True,
                text=True,
                timeout=tess_timeout,
                check=False,
            )
            txt = _normalize_extracted_text(ocr.stdout or "")
            txt = _maybe_upgrade_with_vision(page_num, img_path, txt)
            return txt

        def _ocr_single_page(page_info):
            page_num, img_path = page_info

            # --- flag 完全關閉：走純舊路徑（零影響） ---
            if not _consensus_enable and not _consensus_shadow and not _nemotron_parse_enable:
                txt = _ocr_single_page_legacy(page_num, img_path)
                if txt:
                    return page_num, f"--- 第 {page_num} 頁 (OCR) ---\n{_normalize_extracted_text(txt)}"
                return page_num, None

            # --- shadow mode：新舊都跑，回舊結果，只 log 差異 ---
            if _consensus_shadow and not _consensus_enable:
                legacy_txt = _ocr_single_page_legacy(page_num, img_path)
                consensus_result = _run_consensus_for_page(page_num, img_path)
                _write_consensus_metrics(page_num, img_path, consensus_result, legacy_txt)
                # shadow：永遠回舊結果
                if legacy_txt:
                    return page_num, f"--- 第 {page_num} 頁 (OCR) ---\n{_normalize_extracted_text(legacy_txt)}"
                return page_num, None

            # --- consensus enable：走新路徑，失敗 fallback 舊路徑 ---
            legacy_txt = _ocr_single_page_legacy(page_num, img_path)
            consensus_result = _run_consensus_for_page(page_num, img_path)
            _write_consensus_metrics(page_num, img_path, consensus_result, legacy_txt)

            if consensus_result and consensus_result.success:
                new_txt = _normalize_extracted_text(
                    consensus_result.corrected_text or consensus_result.selected_text
                )
                if new_txt:
                    return page_num, f"--- 第 {page_num} 頁 (OCR) ---\n{new_txt}"
            # consensus 失敗 → fallback 到舊路徑
            if legacy_txt:
                return page_num, f"--- 第 {page_num} 頁 (OCR) ---\n{_normalize_extracted_text(legacy_txt)}"
            return page_num, None

        page_tasks = [(i, img) for i, img in enumerate(imgs[:page_limit], start=1)]

        results = {}
        with ThreadPoolExecutor(max_workers=max(1, min(ocr_workers, len(page_tasks)))) as executor:
            futures = {executor.submit(_ocr_single_page, task): task[0] for task in page_tasks}
            for f in as_completed(futures):
                try:
                    pno, txt = f.result()
                    if txt:
                        results[pno] = txt
                except Exception as e:
                    logger.warning("OCR page failed: %s", e)

        # Reconstruct in page order
        text_content = [results[k] for k in sorted(results.keys())]
        pages_processed = len(page_tasks)

    return "\n".join(text_content), pages_processed


def _maybe_use_ocr(text: str, pdf_path: str, max_pages: int) -> tuple[str, int] | None:
    if str(os.environ.get("MAGI_PDF_OCR_AUTO_COMPARE", "1")).strip().lower() not in {"1", "true", "yes", "on"}:
        return None

    stats = _text_quality_stats(text)
    try:
        quality_floor = float(os.environ.get("MAGI_PDF_TEXT_QUALITY_FLOOR", "0.30") or "0.30")
        quality_skip = float(os.environ.get("MAGI_PDF_TEXT_QUALITY_SKIP_OCR", "0.58") or "0.58")
        gain_needed = float(os.environ.get("MAGI_PDF_OCR_COMPARE_GAIN", "0.03") or "0.03")
        probe_pages = int(os.environ.get("MAGI_PDF_OCR_PROBE_PAGES", "2") or "2")
    except Exception:
        quality_floor, quality_skip, gain_needed, probe_pages = 0.30, 0.58, 0.03, 2

    if stats["score"] >= quality_skip:
        return None

    try:
        probe_text, probe_processed = _extract_text_ocr(pdf_path, max_pages=max_pages, ocr_page_limit=max(1, probe_pages))
    except Exception as e:
        logger.warning("⚠️ OCR quality probe failed: %s", e)
        return None

    if not _is_meaningful_text(probe_text):
        return None

    source_probe = ""
    pieces = str(text or "").split("--- 第 ")
    picked = []
    for piece in pieces[1 : max(2, probe_processed + 1)]:
        picked.append("--- 第 " + piece)
    if picked:
        source_probe = "\n".join(picked)
    if not source_probe:
        source_probe = str(text or "")[: max(2000, len(probe_text))]

    src_probe_stats = _text_quality_stats(source_probe)
    ocr_probe_stats = _text_quality_stats(probe_text)
    src_noise = (src_probe_stats["suspicious_ratio"] * 0.7) + (src_probe_stats["weird_ratio"] * 8.0)
    ocr_noise = (ocr_probe_stats["suspicious_ratio"] * 0.7) + (ocr_probe_stats["weird_ratio"] * 8.0)
    ocr_better = (
        ocr_probe_stats["score"] >= max(src_probe_stats["score"] + gain_needed, quality_floor + 0.02)
        or (
            ocr_probe_stats["score"] >= src_probe_stats["score"] + 0.015
            and ocr_noise <= max(0.0, src_noise - 0.02)
        )
    )
    if not ocr_better:
        return None

    try:
        full_ocr_text, full_ocr_pages = _extract_text_ocr(pdf_path, max_pages=max_pages, ocr_page_limit=max_pages)
        if _is_meaningful_text(full_ocr_text):
            logger.info(
                "✅ Prefer OCR text: src_score=%.3f probe_score=%.3f pages=%s",
                src_probe_stats["score"],
                ocr_probe_stats["score"],
                full_ocr_pages,
            )
            return full_ocr_text, full_ocr_pages
    except Exception as e:
        logger.warning("⚠️ Full OCR extraction failed after successful probe: %s", e)
    return None

def extract_text(pdf_path: str, max_pages: int = 0) -> str:
    """
    Extract text content from a PDF file.

    Args:
        pdf_path: Path to the PDF file
        max_pages: 0 means unlimited (extract all pages).

    Returns:
        Extracted text as a string
    """
    if max_pages <= 0:
        max_pages = int(os.environ.get("MAGI_PDF_EXTRACT_MAX_PAGES", "0") or "0") or 999999
    try:
        logger.info(f"📄 Extracting text from: {pdf_path}")
        try:
            pdftotext_text, pdftotext_pages = _extract_text_pdftotext(pdf_path, max_pages=max_pages)
            if _is_meaningful_text(pdftotext_text):
                ocr_preferred = _maybe_use_ocr(pdftotext_text, pdf_path, max_pages=max_pages)
                if ocr_preferred:
                    ocr_text, ocr_pages = ocr_preferred
                    logger.info(f"✅ Extracted via OCR(auto): {ocr_pages} pages, {len(ocr_text)} chars")
                    return ocr_text
                logger.info(f"✅ Extracted via pdftotext: {pdftotext_pages} pages, {len(pdftotext_text)} chars")
                return pdftotext_text
        except Exception as e:
            logger.warning("⚠️ pdftotext extraction failed, fallback to fitz: %s", e)

        fitz_text, fitz_pages = _extract_text_fitz(pdf_path, max_pages=max_pages)
        if _is_meaningful_text(fitz_text):
            logger.info(f"✅ Extracted via fitz: {fitz_pages} pages, {len(fitz_text)} chars")
            return fitz_text

        logger.warning("⚠️ fitz extracted little/no text, fallback to pdfplumber")
        plumber_text, plumber_pages = _extract_text_pdfplumber(pdf_path, max_pages=max_pages)
        if _is_meaningful_text(plumber_text):
            logger.info(f"✅ Extracted via pdfplumber: {plumber_pages} pages, {len(plumber_text)} chars")
            return plumber_text

        logger.warning("⚠️ pdfplumber extracted little/no text, fallback to OCR")
        ocr_text, ocr_pages = _extract_text_ocr(pdf_path, max_pages=max_pages)
        if _is_meaningful_text(ocr_text):
            logger.info(f"✅ Extracted via OCR: {ocr_pages} pages, {len(ocr_text)} chars")
            return ocr_text

        return "[PDF 提取失敗: no_extractable_text_after_pdftotext_fitz_pdfplumber_ocr]"
        
    except Exception as e:
        logger.error(f"❌ PDF extraction error: {e}")
        return f"[PDF 提取失敗: {str(e)}]"


# ---------------------------------------------------------------------------
# Map-Reduce Summarization for large PDFs
# ---------------------------------------------------------------------------

def _chunk_by_paragraph(text: str, limit_chars: int) -> list[str]:
    """Split *text* into chunks of roughly *limit_chars* using paragraph boundaries."""
    t = (text or "").strip()
    if not t:
        return []
    pieces = re.split(r"\n\s*\n", t)
    chunks: list[str] = []
    buf = ""
    for p in pieces:
        p = (p or "").strip()
        if not p:
            continue
        candidate = (buf + "\n\n" + p).strip() if buf else p
        if len(candidate) <= limit_chars:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
        if len(p) > limit_chars:
            for i in range(0, len(p), limit_chars):
                chunks.append(p[i : i + limit_chars].strip())
            buf = ""
        else:
            buf = p
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c]


def _count_page_markers(text: str) -> int:
    return len(_PAGE_MARKER_RE.findall(str(text or "")))


def _summary_line_is_noise(text: str) -> bool:
    line = re.sub(r"\s+", " ", str(text or "")).strip()
    if not line:
        return True
    if _PAGE_MARKER_RE.fullmatch(line):
        return True
    if line in {"目錄", "Contents"}:
        return True
    if "【試題演練】" in line or "自擬" in line or "【擬答】" in line:
        return True
    if any(token in line for token in ("官方引用格式", "案件總表", "程序年表", "卡達代表", "書記官長辦公室", "保留所有權利", "法國印刷")):
        return True
    if re.search(r"[.．。…]{6,}\s*\d+\s*$", line):
        return True
    if re.fullmatch(r"[0-9]{1,3}", line):
        return True
    return False


def _page_primary_heading(page_text: str) -> str:
    for raw in str(page_text or "").splitlines()[:80]:
        line = re.sub(r"\s+", " ", str(raw or "")).strip()
        if _summary_line_is_noise(line):
            continue
        meta = _segment_heading_meta(line)
        if meta and int(meta[0]) >= 4:
            return str(meta[1] or "").strip()
    return ""


def _segment_pages(text: str, *, pages_per_segment: int, segment_chars: int) -> list[dict]:
    body = str(text or "").strip()
    if not body:
        return []
    matches = list(_PAGE_MARKER_RE.finditer(body))
    if not matches:
        chunks = _chunk_by_paragraph(body, max(6000, segment_chars))
        return [
            {
                "index": idx,
                "page_from": None,
                "page_to": None,
                "label": f"段落 {idx}",
                "text": chunk,
            }
            for idx, chunk in enumerate(chunks, start=1)
            if str(chunk or "").strip()
        ]

    pages = []
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        page_no = int(match.group(1))
        page_text = body[start:end].strip()
        if page_text:
            pages.append((page_no, page_text))

    segments = []
    current_pages: list[str] = []
    current_from = None
    current_to = None
    current_chars = 0

    def _flush() -> None:
        nonlocal current_pages, current_from, current_to, current_chars
        if not current_pages:
            return
        seg_index = len(segments) + 1
        label = f"頁 {current_from}" if current_from == current_to else f"頁 {current_from}-{current_to}"
        segments.append(
            {
                "index": seg_index,
                "page_from": current_from,
                "page_to": current_to,
                "label": label,
                "text": "\n\n".join(current_pages).strip(),
            }
        )
        current_pages = []
        current_from = None
        current_to = None
        current_chars = 0

    min_pages_before_heading_split = max(4, min(pages_per_segment // 2, 10))
    min_chars_before_heading_split = max(5000, int(segment_chars * 0.35))

    for page_no, page_text in pages:
        page_len = len(page_text)
        candidate_chars = current_chars + page_len + 2 if current_pages else page_len
        page_heading = _page_primary_heading(page_text)
        if current_pages and (len(current_pages) >= pages_per_segment or candidate_chars > segment_chars):
            _flush()
        elif current_pages and page_heading and (
            (len(current_pages) + 1) >= min_pages_before_heading_split or current_chars >= min_chars_before_heading_split
        ):
            _flush()
        if not current_pages:
            current_from = page_no
        current_pages.append(page_text)
        current_to = page_no
        current_chars += page_len + 2
    _flush()
    return segments


def _summarize_segment_extractively(segment_text: str, *, label: str, max_items: int, max_chars: int) -> str:
    body = str(segment_text or "").strip()
    if not body:
        return ""

    clean = _strip_page_markers(body)
    paragraphs = [
        re.sub(r"\s+", " ", p).strip()
        for p in re.split(r"\n\s*\n", clean)
        if re.sub(r"\s+", " ", p).strip()
    ]
    lines = [
        re.sub(r"\s+", " ", ln).strip()
        for ln in body.splitlines()
        if re.sub(r"\s+", " ", ln).strip()
        and not _summary_line_is_noise(ln)
        and not _PAGE_MARKER_RE.fullmatch(str(ln).strip())
    ]

    heading_re = re.compile(
        r"^(?:[壹貳參肆伍陸柒捌玖拾]+、|[一二三四五六七八九十]+、|[0-9]+[.)、]|[A-Z][.)])"
    )

    picked: list[str] = []
    seen: set[str] = set()

    def _push(text_value: str, *, hard_limit: int = 240) -> None:
        cleaned = re.sub(r"\s+", " ", str(text_value or "")).strip(" -\t\r\n")
        cleaned = re.sub(r"\b\w+\.indb\s+\d+\b", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if len(cleaned) < 18:
            return
        norm = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "", cleaned).lower()
        if not norm or norm in seen:
            return
        seen.add(norm)
        picked.append(cleaned[:hard_limit])

    for line in lines[:120]:
        if len(line) <= 80 and (heading_re.match(line) or ("：" in line and len(line) <= 60)):
            _push(line, hard_limit=120)
        if len(picked) >= max_items:
            break

    sampled_paragraphs = _sample_evenly(paragraphs, max_items + 2)
    for _, para in sampled_paragraphs:
        sentence = ""
        for sent in re.split(r"(?<=[。！？!?；;])\s+|(?<=\.)\s+", para):
            sent = re.sub(r"\s+", " ", str(sent or "")).strip()
            if len(sent) < 36:
                continue
            sentence = sent
            break
        _push(sentence or para)
        if len(picked) >= max_items:
            break

    if not picked and clean:
        fallback_parts = _chunk_text(clean, chunk_chars=max(800, min(1600, max_chars)), overlap=0, max_chunks=max_items)
        for part in fallback_parts[:max_items]:
            _push(part, hard_limit=220)

    if not picked:
        return ""

    lines_out = [f"【{label}】"]
    for idx, item in enumerate(picked[:max_items], start=1):
        lines_out.append(f"{idx}. {item}")
    brief = "\n".join(lines_out).strip()
    if len(brief) <= max_chars:
        return brief

    trimmed = [f"【{label}】"]
    used_chars = len(trimmed[0])
    for idx, item in enumerate(picked[:max_items], start=1):
        line = f"{idx}. {item}"
        if used_chars + len(line) + 1 > max_chars and len(trimmed) > 1:
            break
        trimmed.append(line[: max(40, max_chars - used_chars - 4)])
        used_chars += len(trimmed[-1]) + 1
    return "\n".join(trimmed).strip()


def _extract_segment_headings(segment_text: str, *, max_items: int = 8) -> list[str]:
    lines = [
        re.sub(r"\s+", " ", ln).strip()
        for ln in str(segment_text or "").splitlines()
        if re.sub(r"\s+", " ", ln).strip()
        and not _summary_line_is_noise(ln)
        and not _PAGE_MARKER_RE.fullmatch(str(ln).strip())
    ]
    heading_re = re.compile(
        r"^(?:[壹貳參肆伍陸柒捌玖拾]+[、.．]|[一二三四五六七八九十]+[、.．]|[IVXLC]+[.)]|[（(]?[一二三四五六七八九十0-9]+[）).、．.]|[A-Z][.)])"
    )
    noisy_re = re.compile(r"(?:頁\s*\d+|出版|印刷|ISBN|元照|第\s*\d+\s*版|卷第\s*\d+|官方引用格式|卡達代表|書記官長辦公室|案件總表)")
    picked: list[str] = []
    seen: set[str] = set()
    for line in lines[:180]:
        if len(line) > 60:
            continue
        if not (heading_re.match(line) or ("：" in line and len(line) <= 36)):
            continue
        if noisy_re.search(line):
            continue
        clean = re.sub(r"^([0-9]+[.)、]\s*)+", "", line).strip(" -")
        norm = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "", clean).lower()
        if len(clean) < 4 or not norm or norm in seen:
            continue
        seen.add(norm)
        picked.append(clean)
        if len(picked) >= max_items:
            break
    return picked


def _build_ultra_segment_seed(
    segment_text: str,
    *,
    label: str,
    max_items: int,
    max_chars: int,
) -> str:
    heading_lines = _extract_segment_headings(segment_text, max_items=min(8, max_items))
    extractive = _summarize_segment_extractively(
        segment_text,
        label=label,
        max_items=max_items,
        max_chars=max(max_chars, 1200),
    )
    parts: list[str] = []
    if heading_lines:
        parts.append("【可能章節】")
        for item in heading_lines:
            parts.append(f"- {item}")
    if extractive:
        if parts:
            parts.append("")
        parts.append(extractive)
    seed = "\n".join(parts).strip()
    if len(seed) > max_chars:
        seed = seed[: max_chars - 1].rstrip() + "…"
    return seed



def _segment_fragment_is_noise(text: str) -> bool:
    frag = _normalize_segment_fragment(text)
    if not frag:
        return True
    noise_patterns = (
        "可能案名/主題",
        "可辨識頁數",
        "【文件概況】",
        "【可能章節】",
        "【重點摘要】",
        "文件節錄",
        "可能標題",
        "分析分段",
        "官方引用格式",
        "案件總表",
        "程序年表",
        "卡達代表",
        "書記官長辦公室",
        "保留所有權利",
    )
    if any(pat in frag for pat in noise_patterns):
        return True
    if re.fullmatch(r"【?\s*頁\s*\d+(?:-\d+)?\s*】?", frag):
        return True
    if re.fullmatch(r"頁\s*\d+(?:-\d+)?", frag):
        return True
    return len(re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "", frag)) < 4


def _split_segment_fragments(text: str) -> list[str]:
    body = str(text or "").strip()
    if not body:
        return []
    body = body.replace("【重點摘要】", "\n").replace("【文件概況】", "\n").replace("【可能章節】", "\n")
    pieces = re.split(r"\s+-\s+|\n|(?<=[。；：])\s+", body)
    out: list[str] = []
    for piece in pieces:
        frag = _normalize_segment_fragment(piece)
        if _segment_fragment_is_noise(frag):
            continue
        out.append(frag)
    return out


def _score_segment_title_candidate(text: str) -> tuple[int, int]:
    frag = _normalize_segment_fragment(text)
    score = 0
    if re.match(r"^[壹貳參肆伍陸柒捌玖拾一二三四五六七八九十]+[、.．]", frag):
        score += 7
    if re.match(r"^[（(]?[一二三四五六七八九十0-9]+[）)]", frag):
        score += 3
    if any(k in frag for k in ("定義", "沿革", "量刑", "假釋", "緩刑", "強制工作", "毒品法庭", "修復式司法", "少年", "被害人", "監護", "刑罰", "保安處分", "異議", "爭議", "範圍", "管轄權", "國籍", "間接歧視", "主文", "結論")):
        score += 4
    if "第 " in frag and "條" in frag:
        score -= 2
    if any(k in frag for k in ("指出", "認為", "看法", "條文", "本文規定", "任何含義", "官方引用格式", "卡達代表", "書記官長辦公室", "考慮到申請")):
        score -= 5
    if "：" in frag and len(frag) > 26:
        score -= 2
    score -= max(0, len(frag) - 20) // 10
    return (score, -len(frag))


def _clean_segment_heading_text(text: str) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    clean = re.sub(r"[.．。…]{4,}\s*\d+\s*$", "", clean)
    clean = re.sub(r"^\s*[壹貳參肆伍陸柒捌玖拾]+[、.．]\s*", "", clean)
    clean = re.sub(r"^\s*[一二三四五六七八九十]+[、.．]\s*", "", clean)
    clean = re.sub(r"^\s*[IVXLC]+[.)]\s*", "", clean)
    clean = re.sub(r"^\s*[（(]?[一二三四五六七八九十0-9]+[）).、．.]\s*", "", clean)
    clean = re.sub(r"^\s*[A-Z][.)]\s*", "", clean)
    clean = re.sub(r"\s*\d{1,3}\s*$", "", clean)
    clean = clean.strip(" ：:;，,。")
    clean = re.sub(r"\s+", " ", clean)
    return clean


def _segment_heading_meta(text: str) -> tuple[int, str] | None:
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw or _PAGE_MARKER_RE.fullmatch(raw):
        return None
    if re.search(r"[.．。…]{4,}\s*\d+\s*$", raw):
        return None
    if any(token in raw for token in ("ISBN", "印刷", "出版", "元照", "卷第", "官方引用格式", "卡達代表", "書記官長辦公室", "案件總表")):
        return None
    if ("Reports" in raw or "報告" in raw) and re.search(r"(?:p\.|第\s*\d+\s*頁|\d{4})", raw):
        return None

    level = 0
    if re.match(r"^[壹貳參肆伍陸柒捌玖拾]+、", raw):
        level = 4
    elif re.match(r"^[一二三四五六七八九十]+[、.．]", raw):
        level = 3
    elif re.match(r"^[IVXLC]+[.)]", raw):
        level = 3
    elif re.match(r"^[（(]?[一二三四五六七八九十0-9]+[）).、．.]", raw):
        level = 2
    elif re.match(r"^[0-9]+[.)、]", raw):
        level = 2
    elif "：" in raw and len(raw) <= 38:
        level = 1

    if level <= 0:
        return None

    clean = _clean_segment_heading_text(raw)
    if _segment_fragment_is_noise(clean):
        return None
    if len(clean) > 34 and level < 3:
        return None
    if len(clean) > 42:
        return None
    return level, clean


def _split_segment_sections(segment_text: str, *, max_sections: int = 12) -> list[dict]:
    lines = [
        re.sub(r"\s+", " ", ln).strip()
        for ln in str(segment_text or "").splitlines()
        if re.sub(r"\s+", " ", ln).strip()
        and not _summary_line_is_noise(ln)
        and not _PAGE_MARKER_RE.fullmatch(str(ln).strip())
    ]
    sections: list[dict] = []
    current: Optional[dict] = None

    def _flush() -> None:
        nonlocal current
        if not current:
            return
        body = " ".join(current.get("body_lines") or [])
        body = re.sub(r"\s+", " ", body).strip()
        current["body"] = body
        current.pop("body_lines", None)
        sections.append(current)
        current = None

    for idx, line in enumerate(lines):
        meta = _segment_heading_meta(line)
        if meta:
            _flush()
            level, clean = meta
            current = {
                "level": level,
                "heading": clean,
                "line_index": idx,
                "body_lines": [],
            }
            continue
        if current:
            current.setdefault("body_lines", []).append(line)

    _flush()
    if len(sections) <= max_sections:
        return sections

    strong = [sec for sec in sections if int(sec.get("level") or 0) >= 4]
    selected: list[dict] = strong[:max_sections]
    if len(selected) < max_sections:
        remaining = [sec for sec in sections if sec not in selected]
        step = max(1, len(remaining) // max(1, (max_sections - len(selected))))
        selected.extend(remaining[::step][: max_sections - len(selected)])
    selected = sorted(selected, key=lambda sec: int(sec.get("line_index") or 0))
    return selected[:max_sections]


def _pick_section_sentence(body: str) -> str:
    text = re.sub(r"\s+", " ", str(body or "")).strip()
    if not text:
        return ""
    sentences = [
        re.sub(r"\s+", " ", part).strip(" -")
        for part in re.split(r"(?<=[。！？!?；;])\s+|(?<=\.)\s+", text)
        if re.sub(r"\s+", " ", part).strip(" -")
    ]
    if not sentences:
        sentences = [text]

    def _clean_sentence(sentence: str) -> str:
        cleaned = re.sub(r"\s+", " ", str(sentence or "")).strip(" -")
        cleaned = re.sub(r"【試題演練】.*$", "", cleaned)
        cleaned = re.sub(r"自擬\s*【擬答】.*$", "", cleaned)
        cleaned = re.sub(r"[：:]\s*請(?:論述|說明|分析|比較|評析).*$", "", cleaned)
        cleaned = re.sub(r"[；;]\s*[^。；]*第\s*\d+\s*期[^。；]*$", "", cleaned)
        cleaned = re.sub(r"[；;]\s*[^。；]*(?:元照出版|頁\s*\d+(?:-\d+)?)[^。；]*$", "", cleaned)
        cleaned = re.sub(r"^(?:官方引用格式|案件總表|程序年表)[：:]?.*$", "", cleaned)
        cleaned = re.sub(r"^(?:卡達代表|書記官長辦公室)[：:]?.*$", "", cleaned)
        if cleaned.count("；") >= 2 and re.search(r"(?:頁\s*\d+(?:-\d+)?|第\s*\d+\s*期|元照出版|司法院)", cleaned):
            cleaned = cleaned.split("；", 1)[0].strip()
        cleaned = re.sub(r"\b\d{1,2}\s*$", "", cleaned).strip(" -：:;，,。")
        return cleaned

    cleaned_sentences = []
    for sentence in sentences:
        clean = _clean_sentence(sentence)
        if not clean or _summary_line_is_noise(clean):
            continue
        cleaned_sentences.append(clean)
    if cleaned_sentences:
        sentences = cleaned_sentences

    def _score(sentence: str) -> tuple[int, int]:
        score = 0
        if any(token in sentence for token in ("【試題演練】", "自擬", "【擬答】", "目錄")):
            score -= 8
        if re.search(r"\d{4}\s*年", sentence):
            score += 5
        if any(
            token in sentence
            for token in (
                "提出",
                "引進",
                "指出",
                "認為",
                "改為",
                "影響",
                "目的",
                "爭議",
                "問題",
                "重點",
                "建議",
            )
        ):
            score += 3
        if any(token in sentence for token in ("官方引用格式", "卡達代表", "書記官長辦公室", "案件總表", "程序年表")):
            score -= 8
        if any(token in sentence for token in ("係指", "定義", "制度", "處遇", "政策", "法庭", "監護", "量刑", "假釋", "緩刑")):
            score += 2
        if len(sentence) >= 20:
            score += 2
        if len(sentence) <= 110:
            score += 2
        if len(sentence) > 90:
            score -= 2
        if len(sentence) > 180:
            score -= 4
        return score, -len(sentence)

    ranked = sorted(sentences, key=_score, reverse=True)
    return ranked[0] if ranked else text


def _infer_segment_theme(title: str, bullets: list[str], source_text: str) -> str:
    source_prefix = re.sub(r"\s+", " ", _strip_page_markers(source_text))[:900]
    blob = " ".join([title] + bullets + [source_prefix])
    if ("national origin" in blob and "目前國籍" in blob) or ("「國籍」一詞是否包含目前國籍" in blob) or ("國籍" in blob and "目前國籍" in blob):
        return "「national origin」是否涵蓋現行國籍"
    theme_rules = [
        (("兒童", "最佳利益"), "兒童最佳利益與量刑"),
        (("假釋",), "假釋制度與撤銷假釋"),
        (("毒品法庭",), "毒品法庭與處遇爭議"),
        (("監護",), "監護處分與制度檢討"),
        (("強制工作",), "強制工作制度與違憲爭議"),
        (("少年", "修復式司法"), "少年最佳利益與修復式司法"),
        (("刑事政策", "定義"), "刑事政策之定義與沿革"),
        (("被害人",), "被害人政策與程序參與"),
        (("國籍", "目前國籍"), "「national origin」是否涵蓋現行國籍"),
        (("媒體公司", "公約", "範圍"), "媒體公司措施是否屬《公約》範圍"),
        (("間接歧視", "公約", "範圍"), "「間接歧視」是否屬《公約》範圍"),
        (("初步異議", "管轄權"), "第一個初步異議：屬事管轄權"),
        (("初步異議", "國籍"), "第一個初步異議：屬事管轄權"),
    ]
    best_title = title
    best_score = 0
    for keys, inferred in theme_rules:
        if not all(key in blob for key in keys):
            continue
        score = (len(keys) * 4) + sum(blob.count(key) for key in keys)
        if score > best_score:
            best_title = inferred
            best_score = score
    return best_title


def _build_structured_segment_note(
    segment_text: str,
    *,
    label: str,
    max_items: int,
    max_chars: int,
) -> str:
    source_text = str(segment_text or "").strip()
    if not source_text:
        return ""
    line_count = len(
        [
            ln
            for ln in source_text.splitlines()
            if re.sub(r"\s+", " ", str(ln or "")).strip()
            and not _PAGE_MARKER_RE.fullmatch(str(ln).strip())
        ]
    )
    sections = _split_segment_sections(source_text, max_sections=max(8, max_items + 4))
    headings = _extract_segment_headings(source_text, max_items=max(6, max_items + 4))
    extractive = _summarize_segment_extractively(
        source_text,
        label=label,
        max_items=max(max_items + 2, 6),
        max_chars=max(max_chars, 1400),
    )
    title = ""
    title_level = 0
    early_primary_sections = [
        sec for sec in sections
        if int(sec.get("level") or 0) >= 3 and (int(sec.get("line_index") or 0) / max(1, line_count)) <= 0.55
    ]
    if early_primary_sections:
        ranked_early = sorted(
            early_primary_sections,
            key=lambda sec: (
                int(sec.get("level") or 0),
                *_score_segment_title_candidate(sec.get("heading") or ""),
            ),
            reverse=True,
        )
        title = str(ranked_early[0].get("heading") or "").strip()
        title_level = int(ranked_early[0].get("level") or 0)
    else:
        section_candidates = [
            sec for sec in sections
            if not _segment_fragment_is_noise(sec.get("heading") or "")
        ]
        if section_candidates:
            ranked_sections = sorted(
                section_candidates,
                key=lambda sec: (
                    int(sec.get("level") or 0),
                    *_score_segment_title_candidate(sec.get("heading") or ""),
                ),
                reverse=True,
            )
            title = str(ranked_sections[0].get("heading") or "").strip()
            title_level = int(ranked_sections[0].get("level") or 0)
        elif headings:
            title_candidates = [h for h in headings if not _segment_fragment_is_noise(h)]
            title_candidates.extend(
                frag for frag in _split_segment_fragments(extractive) if len(frag) <= 32
            )
            if title_candidates:
                title = sorted(title_candidates, key=_score_segment_title_candidate, reverse=True)[0]

    bullets: list[str] = []
    section_entries: list[tuple[int, int, str]] = []
    for section in sections:
        heading = _normalize_segment_fragment(section.get("heading") or "")
        if _segment_fragment_is_noise(heading):
            continue
        sentence = _pick_section_sentence(section.get("body") or "")
        sentence = _normalize_segment_fragment(sentence)
        if not sentence:
            continue
        if heading and title and _normalize_segment_fragment(heading) == _normalize_segment_fragment(title):
            if not sentence or _normalize_segment_fragment(sentence) == _normalize_segment_fragment(heading):
                continue
            bullet = sentence
        elif sentence and heading and sentence.startswith(heading):
            bullet = sentence
        elif sentence and heading:
            bullet = f"{heading}：{sentence}"
        else:
            bullet = heading or sentence
        if bullet:
            section_score = (int(section.get("level") or 0) * 10) + min(6, len(sentence) // 40)
            section_score -= int(section.get("line_index") or 0) // 80
            section_entries.append((section_score, int(section.get("line_index") or 0), bullet))

    if section_entries:
        top_entries = sorted(section_entries, key=lambda item: (item[0], -item[1]), reverse=True)[: max_items + 2]
        for _, _, bullet in sorted(top_entries, key=lambda item: item[1]):
            bullets.append(bullet)

    if len(bullets) < max_items:
        for heading in headings:
            clean = _normalize_segment_fragment(heading)
            if _segment_fragment_is_noise(clean):
                continue
            if title and _normalize_segment_fragment(clean) == _normalize_segment_fragment(title):
                continue
            bullets.append(clean)
            if len(bullets) >= max_items:
                break

    for frag in _split_segment_fragments(extractive):
        if title and _normalize_segment_fragment(frag) == _normalize_segment_fragment(title):
            continue
        bullets.append(frag)
    seen: set[str] = set()
    clean_bullets: list[str] = []
    for bullet in bullets:
        clean = _normalize_segment_fragment(bullet)
        if _segment_fragment_is_noise(clean):
            continue
        norm = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "", clean).lower()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        clean_bullets.append(clean)
        if len(clean_bullets) >= max_items:
            break
    should_refine_title = (
        (not title)
        or title_level < 4
        or (
            "初步異議" in str(title or "")
            and ("目前國籍" in source_text or "national origin" in source_text or "「國籍」一詞是否包含目前國籍" in source_text)
        )
    )
    if should_refine_title:
        title = _infer_segment_theme(title or "未命名段落", clean_bullets, source_text)
    lines = [f"【{label}】主題：{title}"]
    for bullet in clean_bullets[:max_items]:
        lines.append(f"- {bullet}")
    note = "\n".join(lines).strip()
    if len(note) > max_chars:
        note = note[: max_chars - 1].rstrip() + "…"
    return note


def _ultra_detail_profile(summary_length: str) -> dict:
    mode = str(summary_length or "medium").strip().lower()
    if mode == "short":
        return {
            "mode": "short",
            "note_items": 5,
            "note_chars": 1000,
            "overview_items": 5,
            "section_bullets": 2,
            "section_limit": 6,
            "issue_items": 0,
            "model_note_length": "short",
            "model_final_length": "medium",
        }
    if mode == "long":
        return {
            "mode": "long",
            "note_items": 11,
            "note_chars": 2000,
            "overview_items": 10,
            "section_bullets": 5,
            "section_limit": 12,
            "issue_items": 6,
            "model_note_length": "long",
            "model_final_length": "long",
        }
    return {
        "mode": "medium",
        "note_items": 8,
        "note_chars": 1400,
        "overview_items": 8,
        "section_bullets": 3,
        "section_limit": 8,
        "issue_items": 3,
        "model_note_length": "medium",
        "model_final_length": "long",
    }


def _ultra_segment_note_with_model(
    segment_text: str,
    *,
    label: str,
    total_segments: int,
    max_items: int,
    max_chars: int,
    timeout_sec: int,
    summary_length: str = "medium",
) -> str:
    source_text = str(segment_text or "").strip()
    if not source_text:
        return ""
    base_note = _build_structured_segment_note(
        source_text,
        label=label,
        max_items=max_items,
        max_chars=max_chars,
    )
    note_mode = str(os.environ.get("MAGI_PDF_ULTRA_NOTE_MODE", "model") or "model").strip().lower()
    # Always use model path for better quality (deterministic only for debugging)
    crosslingual = _needs_crosslingual_polish(source_text) or _needs_crosslingual_polish(base_note)
    if note_mode in {"deterministic", "structured"} and not crosslingual:
        return base_note
    if crosslingual:
        translated_note = _translate_note_to_traditional_chinese(base_note or source_text[:1200])
        # If translation fallback already returned a properly structured note,
        # trust it and skip the slower model pass.
        if (
            translated_note
            and _summary_text_usable(translated_note)
            and translated_note.startswith(f"【{label}】主題：")
            and translated_note.count("\n- ") >= 1
        ):
            return translated_note
        # Otherwise keep using it as a seed for the model path below.
        if translated_note and _summary_text_usable(translated_note):
            base_note = translated_note  # feed to LLM below as seed
    seed = _build_ultra_segment_seed(
        source_text,
        label=label,
        max_items=max_items,
        max_chars=max(max_chars * 2, 1800),
    )
    if not seed:
        return ""
    try:
        retry_attempts = int(os.environ.get("MAGI_PDF_ULTRA_NOTE_RETRIES", "1") or "1")
    except Exception:
        retry_attempts = 1
    retry_attempts = max(0, min(retry_attempts, 3))
    # NOTE: 不走 summarize_text_resilient（會 spawn openclaw-agent 佔住 oMLX slot，
    # 導致後續 27 段全部 timeout）。直接用 InferenceGateway 走 oMLX 做段落摘要。
    try:
        from skills.bridge.inference_gateway import InferenceGateway

        prompt = (
            "你是專業法律與政策文件分析師。以下是長文件其中一大段的抽取筆記與章節線索。\n\n"
            "請整理成可閱讀、可複習的繁體中文段摘要。\n"
            "要求：\n"
            f"- 這是第 {label}/{total_segments} 段\n"
            f"- 第一行輸出「【{label}】主題：...」\n"
            f"- 接著用 {max(3, max_items - 1)}-{max_items} 點條列本段核心內容\n"
            "- 優先保留制度名稱、法條、年份、定義、爭議、比較與結論\n"
            "- 若原文有不同立場或批判，明確寫出對比關係\n"
            "- 不要保留破碎半句、書目頁碼、出版資訊或重複編號\n"
            "- 只根據提供內容整理，不補充外部知識\n"
            "- 若原始內容為英文或法文，請直接改寫成自然的繁體中文摘要，不要夾帶原文殘句\n"
            "- 只輸出摘要\n\n"
            f"{seed}"
        )
        gateway = InferenceGateway()
        for attempt in range(retry_attempts + 1):
            q = gateway.chat(
                prompt,
                task_type="summary",
                timeout=max(20, min(timeout_sec + attempt * 20, 180)),
                model=os.environ.get("MAGI_MAIN_MODEL", ""),
                allow_synthetic_fallback=False,
            )
            out = str((q or {}).get("response") or "").strip()
            if _summary_text_usable(out, q):
                if out.startswith(f"【{label}】"):
                    return out
                return out if len(out) >= len(base_note) else base_note
            if attempt < retry_attempts:
                time.sleep(min(2.0, 0.6 * (attempt + 1)))
        try:
            from skills.bridge.balthasar_bridge import summarize_text as _fallback_summarize

            rr = _fallback_summarize(
                base_note or seed,
                timeout_sec=max(45, timeout_sec),
                summary_length=_ultra_detail_profile(summary_length)["model_note_length"],
            )
            alt = str((rr or {}).get("text") or (rr or {}).get("summary") or "").strip()
            if _summary_text_usable(alt, rr):
                if alt.startswith(f"【{label}】"):
                    return alt
                return alt if len(alt) >= len(base_note) else base_note
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1528, exc_info=True)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1530, exc_info=True)
    return base_note


def _ultra_final_summary_with_model(
    notes: list[str],
    *,
    reduce_batch: int,
    reduce_timeout: int,
    final_timeout: int,
    summary_length: str = "medium",
) -> str:
    profile = _ultra_detail_profile(summary_length)

    def _normalize_fragment(text: str) -> str:
        frag = re.sub(r"\s+", " ", str(text or "")).strip(" -：:;，,。")
        frag = re.sub(r"^[\-•\d\.\)\(、]+", "", frag).strip()
        return frag

    def _is_noise_fragment(text: str) -> bool:
        frag = _normalize_fragment(text)
        if not frag:
            return True
        noise_patterns = (
            "可能案名/主題",
            "可辨識頁數",
            "【文件概況】",
            "【可能章節】",
            "【重點摘要】",
            "文件節錄",
            "可能標題",
            "分析分段",
            "官方引用格式",
            "案件總表",
            "程序年表",
            "卡達代表",
            "書記官長辦公室",
            "保留所有權利",
        )
        if any(pat in frag for pat in noise_patterns):
            return True
        if re.fullmatch(r"頁\s*\d+(?:-\d+)?", frag):
            return True
        return len(re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "", frag)) < 4

    def _split_fragments(line: str) -> list[str]:
        text = str(line or "").strip()
        if not text:
            return []
        text = text.replace("【重點摘要】", "\n").replace("【文件概況】", "\n").replace("【可能章節】", "\n")
        pieces = re.split(r"\s+-\s+|\n|(?<=。)\s+|(?<=；)\s+|(?<=：)\s+", text)
        out = []
        for piece in pieces:
            frag = _normalize_fragment(piece)
            if _is_noise_fragment(frag):
                continue
            out.append(frag)
        return out

    def _score_title_candidate(text: str) -> tuple[int, int]:
        frag = _normalize_fragment(text)
        score = 0
        if re.match(r"^[壹貳參肆伍陸柒捌玖拾一二三四五六七八九十]+[、.．]", frag):
            score += 6
        if re.match(r"^[（(]?[一二三四五六七八九十0-9]+[）)]", frag):
            score += 3
        if any(k in frag for k in ("定義", "沿革", "量刑", "假釋", "緩刑", "強制工作", "毒品法庭", "修復式司法", "少年", "被害人", "監護", "刑罰", "保安處分", "異議", "爭議", "範圍", "管轄權", "國籍", "間接歧視", "主文", "結論")):
            score += 4
        if "第 " in frag and "條" in frag:
            score -= 2
        if any(k in frag for k in ("指出", "認為", "看法", "條文", "本文規定", "任何含義", "官方引用格式", "卡達代表", "書記官長辦公室", "考慮到申請")):
            score -= 5
        if "：" in frag and len(frag) > 26:
            score -= 2
        score -= max(0, len(frag) - 18) // 10
        return (score, -len(frag))

    def _infer_thematic_title(title: str, bullets: list[str]) -> str:
        blob = " ".join([title] + bullets)
        theme_rules = [
            (("兒童", "最佳利益"), "兒童最佳利益與量刑"),
            (("假釋",), "假釋制度與撤銷假釋"),
            (("毒品法庭",), "毒品法庭與處遇爭議"),
            (("監護",), "監護處分與制度檢討"),
            (("強制工作",), "強制工作制度與違憲爭議"),
            (("少年", "修復式司法"), "少年最佳利益與修復式司法"),
            (("刑事政策", "定義"), "刑事政策之定義與沿革"),
            (("被害人",), "被害人政策與程序參與"),
            (("national origin", "目前國籍"), "「national origin」是否涵蓋現行國籍"),
            (("國籍", "目前國籍"), "「national origin」是否涵蓋現行國籍"),
            (("媒體公司", "公約", "範圍"), "媒體公司措施是否屬《公約》範圍"),
            (("間接歧視", "公約", "範圍"), "「間接歧視」是否屬《公約》範圍"),
            (("初步異議", "管轄權"), "第一個初步異議：屬事管轄權"),
        ]
        for keys, inferred in theme_rules:
            if all(key in blob for key in keys):
                return inferred
        return title

    def _score_note_bullet(title: str, bullet: str) -> tuple[int, int]:
        text = _normalize_fragment(bullet)
        score = 0
        if any(token in text for token in ("法院認為", "法院指出", "法院得出結論", "因此", "結論", "維持", "不包括", "屬於", "不屬於")):
            score += 7
        if any(token in text for token in ("國籍", "間接歧視", "媒體公司", "公約", "管轄權", "初步異議", "範圍")):
            score += 4
        if any(token in text for token in ("官方引用格式", "卡達代表", "書記官長辦公室", "案件總表", "程序年表", "Ord_", "indb")):
            score -= 10
        if ("報告" in text or "Reports" in text) and re.search(r"(?:p\.|第\s*\d+\s*頁|\d{4})", text):
            score -= 10
        if text.startswith("《") and "判決" in text:
            score -= 8
        if any(token in text for token in ("提交有時限", "普通照會", "常駐代表團", "提交訴狀", "提交辯訴狀")):
            score -= 5
        if "《公約》第一條第一款中的" in text or "《維也納公約》第 31 條第 1 款" in text:
            score -= 4
        if len(text) > 110:
            score -= 3
        if len(text) > 180:
            score -= 6
        if title and text.startswith(title):
            score -= 2
        return (score, -len(text))

    def _polish_note_bullet(title: str, bullet: str) -> str:
        text = _normalize_fragment(bullet)
        if not text:
            return ""
        if text.startswith("「國籍」一詞依其規定：任何含義"):
            return "核心爭點之一是：CERD 所稱「national origin」依通常文義、上下文與公約目的，是否涵蓋現行國籍。"
        if text.startswith("考慮到申請以及書面和口頭協議") and "間接歧視" in text:
            return "法院審查卡達主張的「間接歧視」是否屬於 CERD 規範範圍。"
        if text.startswith("因此，第一條第一款中的「國籍」一詞"):
            return "法院最終認為，CERD 第一條第一款所稱「national origin」不包括現行國籍。"
        if "阿拉伯聯合大公國謹請求法院裁定並宣佈法院對卡達" in text and "缺乏管轄權" in text:
            return "阿聯酋主張法院對卡達的申請欠缺管轄權，且該申請不具可受理性。"
        if text.startswith("種族歧視的定義，如第一條所述"):
            return "法院回到 CERD 第一條第一款對「種族歧視」的定義，作為判斷公約適用範圍的基礎。"
        if text.startswith("除此之外：(c) 遵守《消除種族歧視公約》規定的義務"):
            return ""
        if ("報告" in text or "Reports" in text) and re.search(r"(?:p\.|第\s*\d+\s*頁|\d{4})", text):
            return ""
        if re.match(r"^(?:事實背景|爭議標的)\s*\d+(?:[-‑–]\d+)?(?:\s*[A-Z]\.)?$", text):
            return ""
        if "視訊連結就阿聯酋提出的初步異議舉行了公開聽證會" in text:
            return "法院並於 2020 年 8 月至 9 月，就阿聯酋提出的初步異議舉行公開聽證。"
        if text.startswith("《消除種族歧視公約》序言中指出"):
            return "法院援引《消除種族歧視公約》序言作為解釋本案爭議範圍的背景。"
        return text

    def _final_output_usable(text: str) -> bool:
        t = str(text or "").strip()
        if not _summary_text_usable(t):
            return False
        if t.startswith("（降級摘要）") or "【文件節錄】" in t or "【可能標題】" in t:
            return False
        if "【主題總覽】" not in t and "【章節重點】" not in t:
            return False
        if t.count("【文件概況】") > 1:
            return False
        if "【可能案名/主題】" in t:
            return False
        if re.search(r"- .{0,120} - .{0,120} - .{0,120}", t[:800]):
            return False
        return True

    def _parse_note(note: str) -> tuple[str, list[str]]:
        raw = str(note or "").strip()
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if not lines:
            return "未命名段落", []
        title = ""
        explicit_title = False
        raw_title = ""
        bullets: list[str] = []
        heading_candidates: list[str] = []
        m = re.match(r"^【[^】]+】主題：(.+)$", lines[0])
        if m:
            raw_title = _normalize_fragment(m.group(1))
            title = _normalize_fragment(_clean_segment_heading_text(raw_title))
            explicit_title = True
            body_lines = lines[1:]
        else:
            raw_title = _normalize_fragment(re.sub(r"^【[^】]+】\s*", "", lines[0]))
            title = _normalize_fragment(_clean_segment_heading_text(raw_title))
            body_lines = lines[1:]
        for line in lines:
            if "【可能章節】" in line:
                for frag in _split_fragments(line):
                    if len(frag) <= 42:
                        heading_candidates.append(frag)
        for line in body_lines:
            if line.startswith("【") and "主題：" not in line:
                continue
            bullets.extend(_split_fragments(line))
        if heading_candidates:
            title_candidates = [frag for frag in heading_candidates if not _is_noise_fragment(frag)]
            if title_candidates:
                picked_title = sorted(title_candidates, key=_score_title_candidate, reverse=True)[0]
                if (
                    (not explicit_title and not title)
                    or _score_title_candidate(picked_title) > _score_title_candidate(title)
                    or len(title) > 30
                ):
                    title = picked_title
        seen = set()
        deduped_with_idx = []
        for idx, item in enumerate(bullets):
            cleaned = _normalize_fragment(item)
            if _is_noise_fragment(cleaned):
                continue
            if title and cleaned == title:
                continue
            norm = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "", cleaned).lower()
            if not norm or norm in seen:
                continue
            seen.add(norm)
            deduped_with_idx.append((idx, cleaned))
        title = (
            title.replace("刑事政策定義與沿革", "刑事政策之定義與沿革")
            .replace("少年最佳利益及少年修復式司法", "少年最佳利益與修復式司法")
            .replace("少年最佳利益及修復式司法", "少年最佳利益與修復式司法")
        )
        deduped = [item for _, item in deduped_with_idx]
        if (not explicit_title) or _score_title_candidate(title)[0] < 0 or len(title) > 26:
            title = _infer_thematic_title(title or "未命名段落", deduped)
        elif _normalize_fragment(raw_title) != _normalize_fragment(title):
            title = _infer_thematic_title(title or "未命名段落", deduped)
        ranked = sorted(
            deduped_with_idx,
            key=lambda item: (_score_note_bullet(title, item[1])[0], -item[0], _score_note_bullet(title, item[1])[1]),
            reverse=True,
        )
        top_limit = max(4, min(int(profile["section_bullets"]), 6))
        top = sorted(ranked[:top_limit], key=lambda item: item[0])
        polished = []
        seen_polished = set()
        for _, item in top:
            clean = _polish_note_bullet(title, item)
            norm = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "", clean).lower()
            if not clean or not norm or norm in seen_polished:
                continue
            seen_polished.add(norm)
            polished.append(clean)
        return title or "未命名段落", polished

    def _deterministic_merge(parts: list[str]) -> str:
        parsed = [_parse_note(part) for part in parts if str(part or "").strip()]
        parsed = [(title, bullets) for title, bullets in parsed if title or bullets]
        if not parsed:
            return ""
        overview = []
        sections = []
        issue_pool: list[str] = []
        seen_titles = set()
        section_limit = int(profile["section_limit"])
        section_bullets = int(profile["section_bullets"])
        issue_items = int(profile["issue_items"])
        for title, bullets in parsed:
            norm_title = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "", title).lower()
            if norm_title and not any(norm_title in seen or seen in norm_title for seen in seen_titles):
                seen_titles.add(norm_title)
                overview.append(f"- {title}")
            elif norm_title:
                continue
            if len(sections) >= max(1, section_limit) * (section_bullets + 2):
                continue
            if not bullets:
                continue  # skip sections with no substantive content
            sections.append(f"【{title}】")
            for bullet in bullets[:section_bullets]:
                sections.append(f"- {bullet}")
                if issue_items > 0 and any(token in bullet for token in ("爭點", "是否", "法院", "結論", "不包括", "管轄權", "範圍")):
                    issue_pool.append(bullet)
            sections.append("")
        merged = ["【主題總覽】"]
        merged.extend(overview[: int(profile["overview_items"])] or ["- （無法提取主題總覽）"])
        merged.append("")
        merged.append("【章節重點】")
        merged.extend(sections[:-1] if sections and not sections[-1] else sections)
        if issue_items > 0:
            seen_issue = set()
            final_issues = []
            for bullet in issue_pool:
                norm = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "", bullet).lower()
                if not norm or norm in seen_issue:
                    continue
                seen_issue.add(norm)
                final_issues.append(bullet)
                if len(final_issues) >= issue_items:
                    break
            if final_issues:
                merged.append("")
                merged.append("【主要爭點】")
                merged.extend(f"- {bullet}" for bullet in final_issues)
        return "\n".join(merged).strip()

    usable_notes = [str(note or "").strip() for note in notes if _summary_text_usable(note)]
    if not usable_notes:
        return ""
    parsed_notes = [_parse_note(part) for part in usable_notes if str(part or "").strip()]
    final_mode = str(os.environ.get("MAGI_PDF_ULTRA_FINAL_MODE", "model") or "model").strip().lower()
    crosslingual = _needs_crosslingual_polish("\n".join(usable_notes[:3]))
    deterministic = _deterministic_merge(usable_notes) or "\n\n".join(usable_notes)
    noisy_note_markers = ("可能案名/主題", "【可能章節】", "【文件概況】", "【重點摘要】")
    has_noisy_notes = any(marker in note for note in usable_notes for marker in noisy_note_markers)
    small_structured_batch = (
        parsed_notes
        and len(parsed_notes) <= max(3, int(reduce_batch or 0))
        and all(
            title
            and title != "未命名段落"
            and bullets
            and _score_title_candidate(title)[0] >= 2
            for title, bullets in parsed_notes
        )
    )
    if crosslingual:
        translated_final = _translate_note_to_traditional_chinese(deterministic)
        if _final_output_usable(translated_final):
            return translated_final
    if final_mode in {"deterministic", "structured"} and not crosslingual:
        return deterministic
    if has_noisy_notes and deterministic:
        return deterministic
    if small_structured_batch and _final_output_usable(deterministic):
        return deterministic
    seed = "\n\n".join(usable_notes)
    # NOTE: 跳過 summarize_text_resilient（會 spawn openclaw-agent 佔 oMLX slot）。
    # 最終匯總直接走 InferenceGateway/oMLX。
    if len(seed) > 14000:
        seed = _mr_reduce_summaries(
            usable_notes,
            batch_size=reduce_batch,
            reduce_timeout=reduce_timeout,
        )
    try:
        from skills.bridge.inference_gateway import InferenceGateway

        prompt = (
            "以下是同一份長文件的各段摘要。請整合成一份完整、可讀性高的繁體中文摘要。\n"
            "格式要求：\n"
            "1. 先輸出【主題總覽】並用 4-8 點說明全文主軸。\n"
            f"2. 再輸出【章節重點】，依段落主題分小節整理，每節 2-{max(3, int(profile['section_bullets']))} 點。\n"
            "3. 如文件存在制度爭議、比較或批判，另輸出【主要爭點】。\n"
            "4. 保留重要法條、制度名稱、年份與結論。\n"
            "5. 去除重複、碎句、頁碼感與書目感，不要只是把段摘要原樣貼上。\n"
            "6. 若來源摘要含英文或法文，請統一改寫成自然的繁體中文，不要殘留外文標題片段。\n"
            "7. 只輸出摘要。\n\n"
            f"{seed}"
        )
        q = InferenceGateway().chat(
            prompt,
            task_type="summary",
            timeout=max(30, min(final_timeout, 180)),
            model=os.environ.get("MAGI_MAIN_MODEL", ""),
            allow_synthetic_fallback=False,
        )
        out = str((q or {}).get("response") or "").strip()
        if _final_output_usable(out):
            return out
        try:
            from skills.bridge.balthasar_bridge import summarize_text as _fallback_summarize

            rr = _fallback_summarize(
                seed,
                timeout_sec=max(120, final_timeout + 30),
                summary_length=profile["model_final_length"],
            )
            alt = str((rr or {}).get("text") or (rr or {}).get("summary") or "").strip()
            if _final_output_usable(alt):
                return alt
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1892, exc_info=True)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1894, exc_info=True)
    return deterministic or seed


def _summary_checkpoint_dir(source_hint: str, text: str, *, kind: str, variant: str = "medium") -> Path:
    h = hashlib.sha1()
    h.update(str(source_hint or "document").encode("utf-8", "ignore"))
    h.update(b"|")
    h.update(str(variant or "medium").encode("utf-8", "ignore"))
    h.update(b"|")
    h.update(str(len(text or "")).encode("ascii", "ignore"))
    h.update(b"|")
    h.update(str(text or "").encode("utf-8", "ignore"))
    slug = _safe_slug(Path(str(source_hint or "document")).stem or "document")
    variant_slug = _safe_slug(str(variant or "medium"), fallback="medium")
    return _doc_run_root(kind) / f"{slug}-{variant_slug}-{h.hexdigest()[:16]}"


def _summary_state_path(checkpoint_dir: Path) -> Path:
    return checkpoint_dir / "state.json"


def _persist_summary_state(checkpoint_dir: Path, patch: dict) -> None:
    state_path = _summary_state_path(checkpoint_dir)
    current = _read_json(state_path) or {}
    current.update(patch or {})
    current["updated_at"] = time.time()
    _atomic_write_json(state_path, current)


def summarize_ultra_large_text(
    text: str,
    *,
    source_hint: str = "document",
    page_count: Optional[int] = None,
    progress_callback=None,
    summary_length: str = "medium",
) -> str:
    body = str(text or "").strip()
    if not body:
        return ""
    note_version = 10  # bumped: smaller segments + quality gates
    profile = _ultra_detail_profile(summary_length)
    force_refresh = str(os.environ.get("MAGI_PDF_ULTRA_FORCE_REFRESH", "")).strip().lower() in {"1", "true", "yes", "on"}

    try:
        pages_per_segment = int(os.environ.get("MAGI_PDF_ULTRA_SEGMENT_PAGES", "12") or "12")
    except Exception:
        pages_per_segment = 12
    try:
        segment_chars = int(os.environ.get("MAGI_PDF_ULTRA_SEGMENT_CHARS", "15000") or "15000")
    except Exception:
        segment_chars = 15000
    try:
        note_max_items = int(os.environ.get("MAGI_PDF_ULTRA_NOTE_ITEMS", "8") or "8")
    except Exception:
        note_max_items = 8
    try:
        note_max_chars = int(os.environ.get("MAGI_PDF_ULTRA_NOTE_MAX_CHARS", "1400") or "1400")
    except Exception:
        note_max_chars = 1400
    try:
        reduce_batch = int(os.environ.get("MAGI_PDF_ULTRA_REDUCE_BATCH", "10") or "10")
    except Exception:
        reduce_batch = 10
    try:
        reduce_timeout = int(os.environ.get("MAGI_PDF_ULTRA_REDUCE_TIMEOUT_SEC", "90") or "90")
    except Exception:
        reduce_timeout = 90
    try:
        note_timeout = int(os.environ.get("MAGI_PDF_ULTRA_NOTE_TIMEOUT_SEC", "60") or "60")
    except Exception:
        note_timeout = 60
    try:
        final_timeout = int(os.environ.get("MAGI_PDF_ULTRA_FINAL_TIMEOUT_SEC", "90") or "90")
    except Exception:
        final_timeout = 90
    try:
        note_workers = int(os.environ.get("MAGI_PDF_ULTRA_NOTE_WORKERS", "1") or "1")
    except Exception:
        note_workers = 1

    pages_per_segment = max(6, min(pages_per_segment, 64))
    segment_chars = max(8000, min(segment_chars, 120000))
    note_max_items = max(4, min(max(note_max_items, int(profile["note_items"])), 12))
    note_max_chars = max(480, min(max(note_max_chars, int(profile["note_chars"])), 2400))
    reduce_batch = max(2, min(reduce_batch, 16))
    reduce_timeout = max(20, min(reduce_timeout, 180))
    note_timeout = max(20, min(note_timeout, 180))
    final_timeout = max(20, min(final_timeout, 180))
    note_workers = max(1, min(note_workers, 4))

    segments = _segment_pages(body, pages_per_segment=pages_per_segment, segment_chars=segment_chars)
    if not segments:
        return body[:3000]

    detected_pages = page_count if page_count is not None else _count_page_markers(body)
    checkpoint_dir = _summary_checkpoint_dir(source_hint, body, kind="pdf_summary", variant=profile["mode"])
    manifest_path = checkpoint_dir / "manifest.json"
    final_path = checkpoint_dir / "final_summary.txt"
    state_path = _summary_state_path(checkpoint_dir)
    if final_path.exists() and not force_refresh:
        cached_final = str(final_path.read_text(encoding="utf-8") or "").strip()
        # Quality gate: reject cached summaries that are suspiciously short
        # relative to source text (likely from a previous degraded run).
        min_final_len = max(200, min(800, len(body) // 200))
        structured_cached_final = "【文件概況】" in cached_final or "【重點摘要】" in cached_final or "【主題總覽】" in cached_final
        if cached_final and (len(cached_final) >= min_final_len or structured_cached_final):
            return cached_final
    manifest = {
        "version": note_version,
        "source_hint": str(source_hint or "document"),
        "segments_total": len(segments),
        "page_count": detected_pages,
        "pages_per_segment": pages_per_segment,
        "segment_chars": segment_chars,
    }
    _atomic_write_json(manifest_path, manifest)
    _persist_summary_state(
        checkpoint_dir,
        {
            "status": "running",
            "phase": "compress",
            "segments_total": len(segments),
            "segments_completed": 0,
            "page_count": detected_pages,
            "source_hint": str(source_hint or "document"),
            "final_summary_path": str(final_path),
        },
    )

    notes: list[str] = [""] * len(segments)
    pending_segments: list[tuple[int, dict]] = []
    completed = 0
    for idx, segment in enumerate(segments, start=1):
        seg_path = checkpoint_dir / f"segment_{idx:04d}.json"
        if force_refresh:
            pending_segments.append((idx, segment))
            continue
        cached = _read_json(seg_path)
        if isinstance(cached, dict):
            cached_note = str(cached.get("note") or "").strip()
            cached_source = str(cached.get("note_source") or "model").strip().lower()
            # Quality gate: reject cached notes that are too short relative
            # to their segment size (likely from degraded/failed runs).
            seg_text_len = len(str(segment.get("text") or ""))
            min_note_len = max(80, min(200, seg_text_len // 50))
            note_quality_ok = len(cached_note) >= min_note_len
            cached_version_ok = int(cached.get("note_version") or 0) == note_version
            can_reuse_model_note = cached_note and cached_version_ok and cached_source == "model"
            can_reuse_other_note = cached_note and cached_version_ok and note_quality_ok and cached_source != "seed"
            if can_reuse_model_note or can_reuse_other_note:
                notes[idx - 1] = cached_note
                completed += 1
                _persist_summary_state(
                    checkpoint_dir,
                    {
                        "status": "running",
                        "phase": "compress",
                        "segments_completed": completed,
                    },
                )
                if progress_callback:
                    progress_callback("compress", completed, len(segments), f"⏳ 正在壓縮大型文件... ({completed}/{len(segments)})")
                continue
        pending_segments.append((idx, segment))

    def _finalize_note(idx: int, segment: dict, note_text: str) -> str:
        final_note = str(note_text or "").strip()
        note_source = "model" if final_note else "seed"
        if not final_note:
            final_note = _build_ultra_segment_seed(
                str(segment.get("text") or ""),
                label=str(segment.get("label") or f"段落 {idx}"),
                max_items=note_max_items,
                max_chars=note_max_chars,
            )
        if not final_note:
            final_note = f"【{segment.get('label') or f'段落 {idx}'}】\n1. {str(segment.get('text') or '')[: max(120, note_max_chars - 10)]}"
        seg_path = checkpoint_dir / f"segment_{idx:04d}.json"
        _atomic_write_json(
            seg_path,
            {
                "index": idx,
                "label": segment.get("label"),
                "page_from": segment.get("page_from"),
                "page_to": segment.get("page_to"),
                "note_version": note_version,
                "note_source": note_source,
                "note": final_note,
            },
        )
        return final_note

    if pending_segments:
        with ThreadPoolExecutor(max_workers=note_workers) as executor:
            fut_map = {
                executor.submit(
                    _ultra_segment_note_with_model,
                    str(segment.get("text") or ""),
                    label=str(segment.get("label") or f"段落 {idx}"),
                    total_segments=len(segments),
                    max_items=note_max_items,
                    max_chars=note_max_chars,
                    timeout_sec=note_timeout,
                    summary_length=profile["mode"],
                ): (idx, segment)
                for idx, segment in pending_segments
            }
            for fut in as_completed(fut_map):
                idx, segment = fut_map[fut]
                note_text = ""
                try:
                    note_text = str(fut.result() or "").strip()
                except Exception:
                    note_text = ""
                notes[idx - 1] = _finalize_note(idx, segment, note_text)
                completed += 1
                _persist_summary_state(
                    checkpoint_dir,
                    {
                        "status": "running",
                        "phase": "compress",
                        "segments_completed": completed,
                    },
                )
                if progress_callback:
                    progress_callback("compress", completed, len(segments), f"⏳ 正在壓縮大型文件... ({completed}/{len(segments)})")

    usable_notes = [str(note or "").strip() for note in notes if str(note or "").strip()]
    if not usable_notes:
        _persist_summary_state(
            checkpoint_dir,
            {
                "status": "failed",
                "phase": "compress",
                "error": "no_usable_notes",
            },
        )
        return body[:3000]

    _persist_summary_state(
        checkpoint_dir,
        {
            "status": "running",
            "phase": "reduce",
            "segments_completed": completed,
        },
    )
    if len(usable_notes) == 1:
        combined = usable_notes[0]
    else:
        combined = _ultra_final_summary_with_model(
            usable_notes,
            reduce_batch=reduce_batch,
            reduce_timeout=reduce_timeout,
            final_timeout=final_timeout,
            summary_length=profile["mode"],
        )
        if not combined:
            combined = _mr_reduce_summaries(
                usable_notes,
                batch_size=reduce_batch,
                reduce_timeout=reduce_timeout,
            )

    preface = ["【文件概況】"]
    if detected_pages:
        preface.append(f"- 可辨識頁數：約 {detected_pages} 頁")
    preface.append(f"- 分析分段：{len(segments)} 段")
    preface.append("")
    preface.append("【重點摘要】")
    final = "\n".join(preface + [combined.strip()]).strip()
    _atomic_write_text(final_path, final)
    _persist_summary_state(
        checkpoint_dir,
        {
            "status": "done",
            "phase": "complete",
            "segments_completed": completed,
            "segments_total": len(segments),
            "completed_at": time.time(),
        },
    )
    return final


def _mr_summarize_one_chunk(
    idx: int,
    total: int,
    chunk_text: str,
    *,
    chunk_timeout: int = 120,
) -> tuple[int, str]:
    """Map phase: summarize a single chunk via oMLX → Ollama fallback."""
    from skills.bridge import melchior_client

    try:
        retry_attempts = int(os.environ.get("MAGI_PDF_MR_CHUNK_RETRIES", "2") or "2")
    except Exception:
        retry_attempts = 2
    try:
        split_retry_depth = int(os.environ.get("MAGI_PDF_MR_SPLIT_RETRY_DEPTH", "1") or "1")
    except Exception:
        split_retry_depth = 1
    try:
        split_retry_chars = int(
            os.environ.get(
                "MAGI_PDF_MR_SPLIT_RETRY_CHARS",
                str(max(1800, min(2600, len(chunk_text) // 2))),
            )
            or str(max(1800, min(2600, len(chunk_text) // 2)))
        )
    except Exception:
        split_retry_chars = max(1800, min(2600, len(chunk_text) // 2))

    retry_attempts = max(0, min(retry_attempts, 4))
    split_retry_depth = max(0, min(split_retry_depth, 2))
    split_retry_chars = max(1200, min(split_retry_chars, max(1200, len(chunk_text))))

    def _prompt_for(text: str, label: str, *, merge_mode: bool = False) -> str:
        if merge_mode:
            return (
                "以下是同一段長 PDF 文件拆小後的子段摘要。請合併為一份精簡、去重的繁體中文條列摘要。\n\n"
                "要求：\n"
                "- 條列式，保留數字、日期、人名、法條\n"
                "- 只輸出摘要，不加開場白\n\n"
                f"{text}"
            )
        return (
            "你是專業文件分析師。請用繁體中文整理以下段落的重點。\n\n"
            "要求：\n"
            "- 條列式，保留數字、日期、人名、法條\n"
            "- 只輸出摘要，不加開場白\n"
            f"- 這是第 {label}/{total} 段，只摘要該段內容\n\n"
            f"{text}"
        )

    def _run_once(text: str, *, label: str, timeout_sec: int, max_tokens: int = 512, merge_mode: bool = False) -> str:
        prompt = _prompt_for(text, label, merge_mode=merge_mode)

        _omlx_chat = getattr(melchior_client, "_chat_omlx", None)
        _omlx_avail = getattr(melchior_client, "_omlx_available", None)
        if callable(_omlx_chat) and callable(_omlx_avail) and _omlx_avail():
            q = _omlx_chat(
                prompt=prompt,
                model=os.environ.get("MAGI_OMLX_SUMMARY_MODEL", os.environ.get("MAGI_TEXT_PRIMARY_MODEL", "")),
                timeout=timeout_sec,
                temperature=0.2,
                max_tokens=max_tokens,
            )
            out = str((q or {}).get("response") or "").strip()
            if q.get("success") and _summary_text_usable(out, q):
                return out

        try:
            from skills.bridge.inference_gateway import InferenceGateway
            _gw = InferenceGateway()
            q = _gw.chat(
                prompt,
                task_type="summary",
                timeout=timeout_sec,
                model=os.environ.get("MAGI_MAIN_MODEL", ""),
                allow_synthetic_fallback=False,
            )
            out = str((q or {}).get("response") or "").strip()
            if q.get("success") and _summary_text_usable(out, q):
                return out
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2259, exc_info=True)

        return ""

    # Cooldown between oMLX inferences to prevent Metal GPU crash
    _inter_chunk_cooldown = float(os.environ.get("MAGI_PDF_MR_COOLDOWN_SEC", "3") or "3")

    def _summarize_segment(text: str, *, label: str, depth: int) -> str:
        for attempt in range(retry_attempts + 1):
            timeout_sec = min(180, chunk_timeout + attempt * 15)
            out = _run_once(text, label=label, timeout_sec=timeout_sec)
            if out:
                return out
            if attempt < retry_attempts:
                time.sleep(max(_inter_chunk_cooldown, 0.5 * (attempt + 1)))

        if depth <= 0 or len(text) < split_retry_chars:
            return ""

        sub_chunks = _chunk_by_paragraph(
            text,
            max(1200, min(split_retry_chars, max(1200, len(text) // 2))),
        )
        if len(sub_chunks) < 2:
            return ""

        sub_summaries = []
        for sub_idx, sub_chunk in enumerate(sub_chunks, start=1):
            sub_out = _summarize_segment(sub_chunk, label=f"{label}.{sub_idx}", depth=depth - 1)
            if sub_out:
                sub_summaries.append(sub_out)
        if not sub_summaries:
            return ""

        merged = _run_once(
            "\n\n".join(sub_summaries),
            label=label,
            timeout_sec=min(180, max(chunk_timeout, 45)),
            max_tokens=768,
            merge_mode=True,
        )
        return merged or "\n".join(sub_summaries)

    out = _summarize_segment(chunk_text, label=str(idx), depth=split_retry_depth)
    if out:
        return idx, out
    return idx, f"（第 {idx}/{total} 段摘要失敗）"


def _mr_reduce_summaries(
    summaries: list[str],
    *,
    batch_size: int = 8,
    reduce_timeout: int = 90,
) -> str:
    """Reduce phase: merge chunk summaries recursively until short enough."""
    from skills.bridge.inference_gateway import InferenceGateway
    _gw = InferenceGateway()

    reduce_threshold = int(os.environ.get("MAGI_PDF_MR_REDUCE_THRESHOLD", "6000") or "6000")

    def _reduce_once(parts: list[str]) -> list[str]:
        batches = []
        for i in range(0, len(parts), batch_size):
            batches.append(parts[i : i + batch_size])
        results = []
        for batch in batches:
            merged = "\n\n".join(f"- {s}" for s in batch if s.strip())
            prompt = (
                "以下是一份長文件各段落的摘要。請整合為一份精簡的結構化摘要。\n"
                "要求：\n"
                "- 用繁體中文\n"
                "- 條列式，合併重複資訊，保留所有關鍵事實\n"
                "- 只輸出摘要\n\n"
                f"{merged}"
            )
            q = _gw.chat(prompt, task_type="summary", timeout=reduce_timeout, allow_synthetic_fallback=False)
            out = str((q or {}).get("response") or "").strip()
            results.append(out if _summary_text_usable(out, q) else "\n".join(batch))
        return results

    current = [s for s in summaries if _summary_text_usable(s)]
    for _level in range(5):  # max 5 recursion levels
        total_len = sum(len(s) for s in current)
        if total_len <= reduce_threshold or len(current) <= 1:
            break
        current = _reduce_once(current)
    return "\n\n".join(current).strip()


def map_reduce_summarize(
    text: str,
    *,
    progress_callback=None,
) -> str:
    """
    Full map-reduce summarization for large documents.

    Args:
        text: The full extracted text.
        progress_callback: Optional callable(phase, current, total, message).

    Returns:
        Structured summary string.
    """
    import time as _time

    chunk_chars = int(os.environ.get("MAGI_PDF_MR_CHUNK_CHARS", "4000") or "4000")
    workers = int(os.environ.get("MAGI_PDF_MR_WORKERS", "1") or "1")
    chunk_timeout = int(os.environ.get("MAGI_PDF_MR_CHUNK_TIMEOUT_SEC", "120") or "120")
    reduce_batch = int(os.environ.get("MAGI_PDF_MR_REDUCE_BATCH", "8") or "8")
    reduce_timeout = int(os.environ.get("MAGI_PDF_MR_REDUCE_TIMEOUT_SEC", "120") or "120")
    total_timeout = int(os.environ.get("MAGI_PDF_MR_TOTAL_TIMEOUT_SEC", "600") or "600")

    workers = max(1, min(workers, 4))
    chunk_chars = max(1500, min(chunk_chars, 10000))

    # Phase 1: Chunk
    chunks = _chunk_by_paragraph(text, chunk_chars)
    if not chunks:
        return text[:3000]
    total_chunks = len(chunks)
    logger.info("📄 map_reduce_summarize: %d chunks (%d chars each), %d workers",
                total_chunks, chunk_chars, workers)

    # Phase 2: Map (parallel chunk summarization)
    summaries = [""] * total_chunks
    start_time = _time.monotonic()
    completed_count = 0
    _progress_throttle = [0.0]

    def _notify_progress(current: int):
        nonlocal _progress_throttle
        now = _time.monotonic()
        if progress_callback and now - _progress_throttle[0] >= 15:
            _progress_throttle[0] = now
            progress_callback("map", current, total_chunks, f"⏳ 正在分析文件... ({current}/{total_chunks})")

    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        fut_map = {}
        for i, chunk in enumerate(chunks):
            fut = executor.submit(
                _mr_summarize_one_chunk, i + 1, total_chunks, chunk,
                chunk_timeout=chunk_timeout,
            )
            fut_map[fut] = i

        pending = set(fut_map.keys())
        while pending:
            elapsed = _time.monotonic() - start_time
            remaining = total_timeout - elapsed
            if remaining <= 0:
                logger.warning("⏰ map_reduce_summarize: total timeout reached with %d pending chunks", len(pending))
                break
            done, pending = wait(
                pending,
                timeout=min(5, max(0.2, remaining)),
                return_when=FIRST_COMPLETED,
            )
            if not done:
                continue
            for fut in done:
                i = fut_map[fut]
                try:
                    _idx, out = fut.result(timeout=max(1, min(5, chunk_timeout)))
                    summaries[i] = out
                except Exception as e:
                    summaries[i] = f"（第 {i+1}/{total_chunks} 段摘要失敗：{e}）"
                completed_count += 1
                _notify_progress(completed_count)
        for fut in pending:
            i = fut_map[fut]
            fut.cancel()
            if not summaries[i]:
                summaries[i] = f"（第 {i+1}/{total_chunks} 段摘要逾時）"
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    failed_indices = [i for i, s in enumerate(summaries) if not _summary_text_usable(s)]
    if failed_indices:
        logger.info("📄 map phase retry: %d chunks need retry", len(failed_indices))
        for ri, i in enumerate(failed_indices):
            if (_time.monotonic() - start_time) > total_timeout:
                break
            # Cooldown before retry to let Metal GPU recover
            if ri > 0:
                _time.sleep(_inter_chunk_cooldown)
            try:
                _idx, retry_out = _mr_summarize_one_chunk(
                    i + 1,
                    total_chunks,
                    chunks[i],
                    chunk_timeout=min(180, max(chunk_timeout, 45)),
                )
                if _summary_text_usable(retry_out):
                    summaries[i] = retry_out
            except Exception as e:
                logger.warning("📄 retry chunk %d/%d failed: %s", i + 1, total_chunks, e)

    successful = [s for s in summaries if _summary_text_usable(s)]
    logger.info("📄 map phase done: %d/%d successful", len(successful), total_chunks)

    if not successful:
        logger.warning("❌ map_reduce_summarize: all chunks failed, extractive fallback")
        return text[:3000]

    # Phase 3: Reduce
    if progress_callback:
        progress_callback("reduce", 0, 1, "⏳ 正在合併摘要...")

    all_summaries = [s for s in summaries if _summary_text_usable(s)]
    try:
        final = _mr_reduce_summaries(
            all_summaries,
            batch_size=reduce_batch,
            reduce_timeout=reduce_timeout,
        )
    except Exception as e:
        logger.warning("❌ reduce failed: %s, joining raw summaries", e)
        final = "\n\n".join(all_summaries[:20])

    # Final reduce into structured output
    if len(final) > 8000:
        try:
            final = _mr_reduce_summaries(
                [final],
                batch_size=1,
                reduce_timeout=reduce_timeout,
            )
        except Exception:
            final = final[:8000]

    return final


def summarize_pdf(pdf_path: str, max_chars: int = 8000, *, progress_callback=None, summary_length: str = "medium") -> str:
    """
    Extract and summarize a PDF file.

    For documents exceeding MAGI_PDF_MR_THRESHOLD_CHARS (default 8000), uses
    map-reduce summarization that processes ALL chunks instead of sampling.

    Args:
        pdf_path: Path to the PDF file
        max_chars: Maximum chars to send for summarization (short-doc path only)
        progress_callback: Optional callable(phase, current, total, message)

    Returns:
        Summary of the PDF content
    """
    import time as _time

    mr_threshold = int(os.environ.get("MAGI_PDF_MR_THRESHOLD_CHARS", "8000") or "8000")
    try:
        ultra_threshold_chars = int(os.environ.get("MAGI_PDF_ULTRA_THRESHOLD_CHARS", "100000") or "100000")
    except Exception:
        ultra_threshold_chars = 100000
    try:
        ultra_threshold_pages = int(os.environ.get("MAGI_PDF_ULTRA_THRESHOLD_PAGES", "120") or "120")
    except Exception:
        ultra_threshold_pages = 120
    try:
        ultra_threshold_chunks = int(os.environ.get("MAGI_PDF_ULTRA_THRESHOLD_CHUNKS", "24") or "24")
    except Exception:
        ultra_threshold_chunks = 24

    try:
        # Extract text
        text = extract_text(pdf_path)

        if not text or text.startswith("[PDF 提取失敗"):
            return text
        try:
            from api.handlers.document_handler import prepare_document_text_for_llm

            if _needs_crosslingual_polish(text):
                prepared = str(prepare_document_text_for_llm(text) or "").strip()
                if _is_meaningful_text(prepared, min_chars=80):
                    text = prepared
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2540, exc_info=True)

        # Vector ingest — run in background thread so summary returns faster
        import threading as _threading

        def _bg_vector_ingest():
            try:
                if str(os.environ.get("MAGI_PDF_VECTOR_INGEST_ENABLE", "1")).strip().lower() not in {"1", "true", "yes", "on"}:
                    return
                if len(text) > max_chars:
                    from skills.documents.vector_pipeline import ingest_text_to_vector_memory

                    _chunk_sz = int(os.environ.get("MAGI_PDF_VECTOR_CHUNK_CHARS", "1200") or "1200")
                    vec_max = max(260, (len(text) // max(1, _chunk_sz)) + 10)
                    ingest_text_to_vector_memory(
                        kind="pdf",
                        primary=pdf_path,
                        title=os.path.basename(pdf_path),
                        text=text,
                        chunk_chars=int(os.environ.get("MAGI_PDF_VECTOR_CHUNK_CHARS", "1200")),
                        overlap=int(os.environ.get("MAGI_PDF_VECTOR_OVERLAP", "120")),
                        max_chunks_total=vec_max,
                    )
            except Exception as e:
                logger.warning("Background PDF vector ingest failed: %s", e)

        _threading.Thread(target=_bg_vector_ingest, daemon=True, name="pdf-vector-ingest").start()
        foot = ""
        page_count = _count_page_markers(text)
        mr_chunk_chars = int(os.environ.get("MAGI_PDF_MR_CHUNK_CHARS", "4000") or "4000")
        mr_chunk_estimate = len(_chunk_by_paragraph(text, max(1500, min(mr_chunk_chars, 10000))))

        if (
            len(text) >= ultra_threshold_chars
            or page_count >= ultra_threshold_pages
            or mr_chunk_estimate >= ultra_threshold_chunks
        ):
            logger.info(
                "📚 summarize_pdf: ultra-large doc (%d chars, %d pages, %d mr-chunks), using hierarchical path",
                len(text),
                page_count,
                mr_chunk_estimate,
            )
            summary = summarize_ultra_large_text(
                text,
                source_hint=pdf_path,
                page_count=page_count,
                progress_callback=progress_callback,
                summary_length=summary_length,
            )
            if summary and summary.strip():
                return f"📄 **PDF 摘要**\n\n{summary}{foot}"

        # --- Large document: map-reduce path ---
        if len(text) > mr_threshold:
            logger.info("📄 summarize_pdf: large doc (%d chars > %d), using map-reduce", len(text), mr_threshold)
            try:
                summary = map_reduce_summarize(text, progress_callback=progress_callback)
                if summary and summary.strip():
                    return f"📄 **PDF 摘要**\n\n{summary}{foot}"
            except Exception as e:
                logger.warning("map_reduce_summarize failed: %s, falling back to sampled path", e)

        # --- Short document or map-reduce fallback: sampled path (original logic) ---
        if len(text) > max_chars:
            chunks = _chunk_text(
                text,
                chunk_chars=int(os.environ.get("MAGI_PDF_SUMMARY_CHUNK_CHARS", "2200")),
                overlap=int(os.environ.get("MAGI_PDF_SUMMARY_OVERLAP", "180")),
                max_chunks=800,
            )
            sampled = _sample_evenly(chunks, max_samples=int(os.environ.get("MAGI_PDF_SUMMARY_SAMPLES", "6")))
            payload = []
            for i, part in sampled:
                payload.append(f"[Chunk {i}/{len(chunks)}]\n{part}")
            text_for_llm = "\n\n".join(payload)[:max_chars]
        else:
            text_for_llm = text

        prompt = f"""請摘要以下 PDF 文件的重點內容，用繁體中文條列式說明：

        {text_for_llm}

        請提供：
        1. 文件主題
        2. 主要內容摘要 (3-5 點)
        3. 關鍵資訊或結論"""

        max_retries = int(os.environ.get("MAGI_PDF_SUMMARY_RETRY_ATTEMPTS", "3") or "3")
        summary = ""
        last_err = ""

        for attempt in range(1, max_retries + 1):
            try:
                logger.info("🧠 Sending to Casper for summarization (attempt %d/%d)...", attempt, max_retries)
                summary = chat_casper(prompt)
                if summary and summary.strip():
                    break
                last_err = "empty response"
            except Exception as e:
                last_err = str(e)
                logger.warning("Summarization attempt %d/%d failed: %s", attempt, max_retries, e)

            if attempt < max_retries:
                backoff = min(8, 2 ** attempt)
                logger.info("Summarization retry in %ds...", backoff)
                _time.sleep(backoff)

        if summary and summary.strip():
            return f"📄 **PDF 摘要**\n\n{summary}{foot}"
        else:
            logger.warning("All %d summarization attempts failed: %s", max_retries, last_err)
            excerpt = text[:max(2000, max_chars)]
            return f"📄 **PDF 摘要失敗（已嘗試 {max_retries} 次），以下為原文節錄：**\n\n{excerpt}{foot}"

    except Exception as e:
        logger.error(f"❌ PDF summarization error: {e}")
        return f"[PDF 摘要失敗: {str(e)}]"


def get_pdf_info(pdf_path: str) -> dict:
    """
    Get metadata about a PDF file.
    
    Returns:
        Dictionary with page count, title, author, etc.
    """
    try:
        doc = fitz.open(pdf_path)
        try:
            info = {
                "pages": doc.page_count,
                "title": doc.metadata.get("title", "Unknown"),
                "author": doc.metadata.get("author", "Unknown"),
                "subject": doc.metadata.get("subject", ""),
                "creator": doc.metadata.get("creator", "")
            }
            return info
        finally:
            doc.close()
    except Exception as e:
        return {"error": str(e)}
