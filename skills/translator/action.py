#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
translator / action.py

Full translation helper:
- Default: full translation (no summary).
- Long output: export to /static/exports as TXT and return path/url.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import List, Optional

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import get_magi_root_dir, get_orch_dir, get_skill_python

_MAGI_ROOT = str(get_magi_root_dir())

# ── Preamble stripping for LLM translation output ──────────────────────
_PREAMBLE_RE = re.compile(
    r"^(?:"
    r"[Hh]ere\s+is\s+the\s+translat(?:ion|ed).{0,60}[:\n]"
    r"|[Tt]he\s+translat(?:ion|ed\s+(?:text|version|content)).{0,40}[:\n]"
    r"|[Tt]ranslat(?:ion|ed\s+(?:text|version|content))\s*[:\n]"
    r"|以下是.{0,20}翻譯.{0,10}[：:\n]"
    r"|翻譯(?:結果|如下).{0,10}[：:\n]"
    r")\s*",
    re.DOTALL,
)


def _strip_translation_preamble(text: str) -> str:
    """Remove common LLM preamble lines before the actual translation."""
    s = (text or "").strip()
    if not s:
        return s
    s = _PREAMBLE_RE.sub("", s, count=1).strip()
    return s
CODE_DIR = str(get_orch_dir())
_VENV_PY = str(get_skill_python())


def _maybe_reexec_venv() -> None:
    if os.environ.get("MAGI_TRANSLATOR_NO_VENV", "").strip() == "1":
        return
    try:
        if os.path.exists(_VENV_PY) and os.path.realpath(sys.executable) != os.path.realpath(_VENV_PY):
            os.execv(_VENV_PY, [_VENV_PY, __file__, *sys.argv[1:]])
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 63, exc_info=True)


def _load_jsonish(text: str) -> dict:
    t = (text or "").strip()
    if not t:
        return {}
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else {"value": v}
    except Exception:
        return {"value": t}


def _ok(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("success") else 1


def _now_hint() -> str:
    now = datetime.now()
    weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    return f"{now.strftime('%Y/%m/%d')} ({weekdays[now.weekday()]})"


def _export_txt(text: str, prefix: str = "translate") -> dict:
    try:
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
        from ops.export_text import export_txt  # type: ignore
    except Exception:
        export_txt = None  # type: ignore

    if not export_txt:
        return {"success": False, "error": "export_txt_not_available"}
    return export_txt(text, prefix=(prefix or "translate").strip() or "translate")


def _export_docx_bilingual(
    source_text: str,
    translated_text: str,
    *,
    title: str = "",
    subtitle: str = "",
    prefix: str = "translate",
) -> dict:
    """
    將原文與翻譯結果以雙語對照 docx 表格輸出。
    自動按段落配對，每段一列。
    """
    try:
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
        from ops.export_docx import export_bilingual_docx  # type: ignore
    except Exception:
        return {"success": False, "error": "export_docx_not_available"}

    # Split both texts into fine-grained segments. MarkItDown / LLM output
    # often uses single newlines (or markdown table markers) instead of blank
    # lines, so splitting only on \n{2,} produces a handful of massive blobs
    # and the bilingual table collapses into one row. Split on single \n,
    # drop markdown noise, and merge very-short fragments back into their
    # predecessor so each row holds a readable chunk.
    def _segments(raw: str) -> List[str]:
        if not raw:
            return []
        md_noise = re.compile(r"^\s*\|?\s*[-:| ]+\s*\|?\s*$")
        sent_split = re.compile(r"(?<=[。！？!?])\s+|(?<=\.)\s{2,}")
        out: List[str] = []
        for line in raw.splitlines():
            s = line.strip()
            if not s or md_noise.match(s):
                continue
            # Explode markdown table rows ("| a | b | c |") into separate cells.
            if "|" in s and s.count("|") >= 2:
                pieces = [p.strip() for p in s.split("|") if p.strip()]
            else:
                pieces = [s]
            for piece in pieces:
                # Further split long pieces on bullets / sentence boundaries.
                if len(piece) > 180:
                    # Split on TOC bullets and dot-leaders first.
                    bullet_parts = re.split(r"\s*[•·]\s*|\.{3,}", piece)
                    parts: List[str] = []
                    for bp in bullet_parts:
                        bp = bp.strip(" .")
                        if not bp:
                            continue
                        if len(bp) > 180:
                            parts.extend(sent_split.split(bp))
                        else:
                            parts.append(bp)
                else:
                    parts = [piece]
                for part in parts:
                    t = part.strip()
                    if not t:
                        continue
                    if out and len(t) < 8:
                        out[-1] = (out[-1] + " " + t).strip()
                        continue
                    out.append(t)
        return out

    src_lines = _segments(source_text)
    tgt_lines = _segments(translated_text)

    # Drop obvious LLM preamble / apology lines from target.
    _preamble_re = re.compile(
        r"^(?:好的|以下是|以下為|這是|這裡是|作為專業|身為|我將|我會|我是|Sure|Here('s| is)|As an? |I('ll| will) )",
        re.IGNORECASE,
    )
    tgt_lines = [t for t in tgt_lines if not _preamble_re.match(t)]

    # Align the two sequences by proportional interleave when lengths differ.
    n = max(len(src_lines), len(tgt_lines), 1)
    def _stretch(seq: List[str], target_len: int) -> List[str]:
        if not seq:
            return ["" for _ in range(target_len)]
        if len(seq) == target_len:
            return seq
        out: List[str] = []
        for i in range(target_len):
            j = int(i * len(seq) / target_len)
            out.append(seq[j])
        # Dedupe consecutive duplicates produced by stretching, keep position.
        cleaned: List[str] = []
        last = None
        for v in out:
            if v == last:
                cleaned.append("")
            else:
                cleaned.append(v)
                last = v
        return cleaned

    src_rows = _stretch(src_lines, n)
    tgt_rows = _stretch(tgt_lines, n)

    pages = []
    for i, (s, t) in enumerate(zip(src_rows, tgt_rows)):
        if not s and not t:
            continue
        pages.append({"page": i + 1, "source": s, "target": t})

    return export_bilingual_docx(
        pages,
        title=title,
        subtitle=subtitle,
        header_text=title,
        prefix=prefix,
    )


def _load_text_from_file(path: str) -> str:
    p = (path or "").strip()
    if not p:
        return ""
    if not os.path.exists(p) or not os.path.isfile(p):
        return ""
    max_chars = int(os.environ.get("MAGI_TRANSLATOR_MAX_FILE_CHARS", "220000") or "220000")
    low = p.lower()
    # PDF: use pdf_bridge to extract text instead of reading raw binary
    if low.endswith(".pdf"):
        try:
            from skills.documents.pdf_bridge import extract_text
            data = extract_text(p)
            return (data or "")[:max_chars]
        except Exception as e:
            logging.getLogger(__name__).warning(f"PDF extract failed for {p}: {e}")
            return ""
    # Office / markup / ebook formats → MarkItDown via document_reader
    if low.endswith((".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
                     ".html", ".htm", ".xml", ".epub", ".odt", ".rtf",
                     ".csv", ".json")):
        try:
            from skills.engine.document_reader import read_document
            r = read_document(p, max_chars=max_chars, ocr_fallback=False)
            if r.success and r.text:
                return (r.text or "")[:max_chars]
        except Exception as e:
            logging.getLogger(__name__).warning(f"document_reader failed for {p}: {e}")
        # Fallback for docx: try python-docx directly
        if low.endswith(".docx"):
            try:
                import docx  # type: ignore
                doc = docx.Document(p)
                paras = [para.text for para in doc.paragraphs if para.text.strip()]
                return "\n\n".join(paras)[:max_chars]
            except Exception as e:
                logging.getLogger(__name__).warning(f"python-docx fallback failed for {p}: {e}")
        return ""
    # Plain text-like files
    for enc in ("utf-8", "utf-8-sig", "cp950", "big5", "latin-1"):
        try:
            with open(p, "r", encoding=enc, errors="ignore") as f:
                data = f.read(max_chars + 1)
            if data:
                return data[:max_chars]
        except Exception:
            continue
    return ""


def _split_chunks(text: str, chunk_size: int = 1200, overlap: int = 80) -> List[str]:
    # Semantic chunking: prefer splitting by paragraphs, then sentences
    s = (text or "").strip()
    if not s:
        return []
        
    paragraphs = re.split(r'(\n{2,}|\r\n\r\n)', s)
    out: List[str] = []
    current_chunk = ""
    
    for p in paragraphs:
        if not p.strip():
            if current_chunk:
                current_chunk += p
            continue
            
        if len(current_chunk) + len(p) <= chunk_size:
            current_chunk += p
        else:
            if len(p) > chunk_size:
                # If a single paragraph is still too big, try splitting by sentences
                if current_chunk:
                    out.append(current_chunk.strip())
                    current_chunk = ""
                sentences = re.split(r'(?<=[。！？.!?])\s+', p)
                for sent in sentences:
                    if len(current_chunk) + len(sent) <= chunk_size:
                        current_chunk += sent + " "
                    else:
                        if current_chunk:
                            out.append(current_chunk.strip())
                        current_chunk = sent + " "
            else:
                if current_chunk:
                    out.append(current_chunk.strip())
                current_chunk = p
                
    if current_chunk.strip():
        out.append(current_chunk.strip())
        
    return out


def _normalize_target_lang_code(target_lang: str) -> str:
    t = (target_lang or "").strip().lower()
    if not t:
        return "zh-TW"
    if ("繁體" in target_lang) or ("traditional" in t) or (t in {"zh-tw", "zh_tw", "zh-hant", "tw"}):
        return "zh-TW"
    if ("簡體" in target_lang) or (t in {"zh-cn", "zh_cn", "zh-hans", "cn"}):
        return "zh-CN"
    if ("英文" in target_lang) or (t in {"en", "en-us", "english"}):
        return "en"
    if ("日文" in target_lang) or (t in {"ja", "jp", "japanese"}):
        return "ja"
    if re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", t):
        return t
    return "zh-TW"


def _translate_via_google_gtx(text: str, target_lang: str, timeout_sec: int = 8) -> str:
    """
    Lightweight non-LLM fallback translation via Google gtx endpoint.
    This keeps translation available when local LLM routes are unstable.
    """
    s = (text or "").strip()
    if not s:
        return ""
    tl = _normalize_target_lang_code(target_lang)
    chunks = _split_chunks(s, chunk_size=1100, overlap=0)
    if not chunks:
        return ""

    translated: List[str] = []
    for c in chunks[:120]:
        try:
            q = urllib.parse.quote(c)
            url = (
                "https://translate.googleapis.com/translate_a/single"
                f"?client=gtx&sl=auto&tl={urllib.parse.quote(tl)}&dt=t&q={q}"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=max(3, timeout_sec)) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            data = json.loads(raw)
            parts = []
            if isinstance(data, list) and data and isinstance(data[0], list):
                for seg in data[0]:
                    if isinstance(seg, list) and seg and seg[0]:
                        parts.append(str(seg[0]))
            piece = "".join(parts).strip()
            translated.append(piece or c)
        except Exception:
            # On per-chunk failure, keep original text and continue
            translated.append(c)
    return "\n\n".join(translated).strip()


def _non_llm_fallback(payload: dict, *, reason: str) -> dict:
    text = (payload.get("text") or "").strip()
    if not text:
        input_path = str(payload.get("input_path") or "").strip()
        if input_path:
            text = _load_text_from_file(input_path).strip()
    if not text:
        return {"success": False, "error": reason}

    target_lang = str(payload.get("target_lang") or "繁體中文")
    mode = str(payload.get("mode") or "full")
    export = str(payload.get("export") or "auto").strip().lower()
    export_prefix = str(payload.get("export_prefix") or "translate")
    max_inline_chars = int(payload.get("max_inline_chars") or 3000)

    try:
        out = _translate_via_google_gtx(text, target_lang=target_lang, timeout_sec=8)
    except Exception as e:
        return {"success": False, "error": f"{reason}; fallback_failed:{type(e).__name__}"}

    if not out:
        return {"success": False, "error": reason}

    need_export = export in {"1", "true", "yes", "on"} or (export not in {"0", "false", "no", "off"} and len(out) > max_inline_chars)
    if need_export:
        exported = _export_txt(out, prefix=export_prefix)
        if exported.get("success"):
            return {
                "success": True,
                "mode": mode,
                "target_lang": target_lang,
                "source_lang": "auto",
                "exported": True,
                "export_path": exported.get("path", ""),
                "download_url": exported.get("url", ""),
                "preview": out[: min(800, len(out))],
                "provider": "google_gtx_fallback",
                "degraded": True,
                "note": reason,
            }

    return {
        "success": True,
        "mode": mode,
        "target_lang": target_lang,
        "source_lang": "auto",
        "exported": False,
        "text": out,
        "provider": "google_gtx_fallback",
        "degraded": True,
        "note": reason,
    }


def _translate_chunks_local(
    text: str,
    source_lang: str,
    target_lang: str,
    timeout_per_chunk: int = 60,
    max_chunks: int = 60,
    legal_tier1: str = "",
    legal_tier2: str = "",
) -> str:
    chunks = _split_chunks(text, chunk_size=int(os.environ.get("MAGI_TRANSLATOR_CHUNK_SIZE", "1500") or "1500"))
    if not chunks:
        return ""
    if len(chunks) > max_chunks:
        chunks = chunks[:max_chunks]

    translated: List[str] = [""] * len(chunks)
    failed_indices: List[int] = []
    
    import logging as _tlog
    _chunk_logger = _tlog.getLogger("translator_chunk")
    
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    def _is_degraded_response(text: str) -> bool:
        """Detect degradation markers that indicate the model timed out or failed."""
        t = (text or "").strip()
        if not t:
            return True
        markers = ("系統降級回覆", "本機模型逾時", "請稍後重試", "降級摘要")
        return any(m in t for m in markers)

    # ── Per-chunk APE eligibility (legal zh↔en, feature-flagged) ──
    _ape_chunks_enabled = str(os.environ.get("MAGI_TRANSLATOR_APE_CHUNKS", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
    _ape_chunk_max = int(os.environ.get("MAGI_TRANSLATOR_APE_CHUNK_MAX_CHARS", "1800") or "1800")
    _ape_chunk_eligible = False
    _ape_chunk_src = ""
    _ape_chunk_tgt = ""
    if _ape_chunks_enabled:
        try:
            from skills.engine.apple_translation import normalize_lang, is_available as _apple_avail_fn
            _src_code = normalize_lang(source_lang or "auto")
            _tgt_code = normalize_lang(target_lang or "en")
            # auto source: detect via first chunk sample
            _auto_is_en = False
            if _src_code == "auto":
                _sample = "".join(chunks[:2])[:2000] if chunks else ""
                if _sample:
                    _ratio = sum(1 for ch in _sample if ord(ch) < 128) / max(1, len(_sample))
                    _auto_is_en = _ratio > 0.6
            _zh_en = (
                (_src_code.startswith("zh") and _tgt_code == "en") or
                (_src_code == "en" and _tgt_code.startswith("zh")) or
                (_src_code == "auto" and _tgt_code in {"en"}) or
                (_src_code == "auto" and _tgt_code.startswith("zh") and _auto_is_en)
            )
            _apple_ok, _ = _apple_avail_fn()
            if _apple_ok and _zh_en:
                _ape_chunk_eligible = True
                if _src_code == "auto":
                    _ape_chunk_src = "en" if _auto_is_en else "zh-Hant"
                else:
                    _ape_chunk_src = _src_code
                _ape_chunk_tgt = _tgt_code
        except Exception:
            _ape_chunk_eligible = False

    def process_chunk(idx: int, c: str) -> tuple[int, str, bool]:
        c = c.strip()
        if not c:
            return idx, "", False

        max_chunk_retries = int(os.environ.get("MAGI_TRANSLATOR_CHUNK_RETRIES", "3") or "3")

        # ── APE per-chunk pass (legal zh↔en, short/medium) ──
        if _ape_chunk_eligible and len(c) <= _ape_chunk_max:
            try:
                from skills.translator._apple_post_edit import translate_with_ape, is_legal_text
                if is_legal_text(c):
                    ape_res = translate_with_ape(
                        c,
                        source_lang=_ape_chunk_src,
                        target_lang=_ape_chunk_tgt,
                        llm_timeout=min(60, max(15, timeout_per_chunk)),
                        apple_timeout=float(os.environ.get("MAGI_APPLE_TRANSLATION_TIMEOUT_SEC", "10.0") or "10.0"),
                        tier1=legal_tier1,
                        tier2=legal_tier2,
                    )
                    if ape_res.get("success") and str(ape_res.get("text") or "").strip():
                        _chunk_logger.info("Chunk %d via APE (degraded=%s)", idx+1, ape_res.get("degraded"))
                        return idx, str(ape_res.get("text") or "").strip(), False
            except Exception as e:
                _chunk_logger.debug("Chunk %d APE errored, falling through: %s", idx+1, e)

        for attempt in range(1, max_chunk_retries + 1):
            piece = ""
            # Try finding grounded_ai first
            _generate_local = None
            try:
                from skills.bridge.grounded_ai import _generate_local  # type: ignore
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 353, exc_info=True)

            if _generate_local:
                try:
                    if legal_tier1 or legal_tier2:
                        try:
                            from skills.translator.legal_termbase import build_legal_prompt
                            prompt = build_legal_prompt(c, target_lang, legal_tier1, legal_tier2)
                        except Exception:
                            prompt = (
                                "你是專業翻譯員。\n"
                                f"來源語言：{source_lang}\n"
                                f"目標語言：{target_lang}\n"
                                "規則：完整翻譯，不摘要，不省略，不補充。請確保語法連貫。\n\n"
                                f"{c}"
                            )
                    else:
                        prompt = (
                            "你是專業翻譯員。\n"
                            f"來源語言：{source_lang}\n"
                            f"目標語言：{target_lang}\n"
                            "規則：完整翻譯，不摘要，不省略，不補充。請確保語法連貫。\n\n"
                            f"{c}"
                        )
                    piece = _strip_translation_preamble((_generate_local(prompt, temperature=0.1, timeout=timeout_per_chunk, num_ctx=4096) or "").strip())
                    if _is_degraded_response(piece):
                        _chunk_logger.warning("Chunk %d attempt %d _generate_local returned degraded response", idx+1, attempt)
                        piece = ""
                except Exception as e:
                    _chunk_logger.warning("Chunk %d attempt %d _generate_local failed: %s", idx+1, attempt, e)
                    piece = ""

            if not piece:
                try:
                    from skills.bridge.inference_gateway import InferenceGateway
                    prompt2 = (
                        f"請把下列內容完整翻譯成{target_lang}，不要摘要，不要補充：\n\n{c}"
                    )
                    _gw = InferenceGateway()
                    qr = _gw.chat(prompt2, task_type="translate", timeout=max(15, timeout_per_chunk))
                    piece = _strip_translation_preamble((qr.get("response") or "").strip()) if isinstance(qr, dict) else ""
                    if _is_degraded_response(piece):
                        _chunk_logger.warning("Chunk %d attempt %d gateway returned degraded response", idx+1, attempt)
                        piece = ""
                except Exception as e:
                    _chunk_logger.warning("Chunk %d attempt %d gateway failed: %s", idx+1, attempt, e)
                    piece = ""

            if piece:
                return idx, piece, False
            
            # Backoff before retry
            if attempt < max_chunk_retries:
                import time as _time
                backoff = min(8, 2 ** attempt)
                _chunk_logger.info("Chunk %d retry %d/%d in %ds...", idx+1, attempt, max_chunk_retries, backoff)
                _time.sleep(backoff)
        
        # All retries exhausted — keep original text
        _chunk_logger.warning("Chunk %d/%d: all %d retries exhausted, keeping original text", idx+1, len(chunks), max_chunk_retries)
        return idx, c, True

    max_workers = int(os.environ.get("MAGI_TRANSLATOR_WORKERS", "1") or "1")  # oMLX max_num_seqs=1, keep sequential
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        fut_map = {executor.submit(process_chunk, i, chunk): i for i, chunk in enumerate(chunks)}
        for fut in as_completed(fut_map):
            i = fut_map[fut]
            try:
                idx, piece, failed = fut.result()
                translated[idx] = (piece or chunks[idx]).strip()
                if failed:
                    failed_indices.append(idx)
            except Exception as e:
                _chunk_logger.warning("Chunk %d/%d raised exception: %s — keeping original text", i + 1, len(chunks), e)
                translated[i] = chunks[i].strip()
                failed_indices.append(i)
    
    # ── AUTO-RETRY PASS: sequential retry on failed chunks with longer timeout ──
    if failed_indices:
        _chunk_logger.info("Auto-retry pass: %d chunks failed, retrying sequentially...", len(failed_indices))
        import time as _time2
        still_failed = []
        for idx in failed_indices:
            c = chunks[idx].strip()
            if not c:
                continue
            piece = ""
            try:
                _generate_local2 = None
                try:
                    from skills.bridge.grounded_ai import _generate_local as _generate_local2  # type: ignore
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 432, exc_info=True)
                if _generate_local2:
                    prompt = (
                        "你是專業翻譯員。\n"
                        f"來源語言：{source_lang}\n"
                        f"目標語言：{target_lang}\n"
                        "規則：完整翻譯，不摘要，不省略，不補充。請確保語法連貫。\n\n"
                        f"{c}"
                    )
                    piece = _strip_translation_preamble((_generate_local2(prompt, temperature=0.1, timeout=int(timeout_per_chunk * 1.5), num_ctx=4096) or "").strip())
                    if _is_degraded_response(piece):
                        piece = ""
                if not piece:
                    from skills.bridge import melchior_client as _mcr  # type: ignore
                    prompt2 = f"請把下列內容完整翻譯成{target_lang}，不要摘要，不要補充：\n\n{c}"
                    qr = _mcr.quick_local_chat(prompt2, timeout=max(20, int(timeout_per_chunk * 1.5)), model_hint=os.environ.get("MAGI_TEXT_PRIMARY_MODEL", ""))
                    piece = _strip_translation_preamble((qr.get("response") or "").strip()) if isinstance(qr, dict) else ""
                    if _is_degraded_response(piece):
                        piece = ""
            except Exception as e:
                _chunk_logger.warning("Auto-retry chunk %d failed: %s", idx+1, e)
            if piece and not _is_degraded_response(piece):
                translated[idx] = piece
                _chunk_logger.info("Auto-retry chunk %d succeeded", idx+1)
            else:
                still_failed.append(idx)
        failed_indices = still_failed

    # ── GOOGLE GTX FALLBACK for any still-failed chunks ──
    if failed_indices:
        _chunk_logger.info("GTX fallback: %d chunks still failed, trying Google Translate...", len(failed_indices))
        gtx_still = []
        for idx in failed_indices:
            try:
                gtx_out = _translate_via_google_gtx(chunks[idx], target_lang=target_lang, timeout_sec=12)
                if gtx_out and gtx_out.strip():
                    translated[idx] = gtx_out.strip()
                    _chunk_logger.info("GTX fallback chunk %d succeeded", idx+1)
                else:
                    gtx_still.append(idx)
            except Exception:
                gtx_still.append(idx)
        failed_indices = gtx_still

    result_text = "\n\n".join(t for t in translated if t.strip()).strip()
    if failed_indices:
        result_text += f"\n\n⚠️ 有 {len(failed_indices)} 個段落翻譯失敗，已先保留原文，稍後可針對該段重跑。"
    return result_text


def _translate_inner(payload: dict) -> dict:
    sys.path.insert(0, _MAGI_ROOT)
    # IMPORTANT: 不得 import translate_text 或 translate_core — 2026-04-17 重構後 tri_sage_collab.translate_text
    # 內部改走 translate_core → translate() → subprocess(__file__ _translate_inner) 會形成無限遞迴 fork 炸彈。
    # 這裡是 inner worker，**只能呼叫具體底層實作**（InferenceGateway / _translate_chunks_local / apple_translation），
    # 絕對不能呼叫回 translator public entry point。(2026-04-19 P2-0 修)

    text = (payload.get("text") or payload.get("value") or "").strip()
    if not text:
        input_path = str(payload.get("input_path") or payload.get("path") or "").strip()
        if input_path:
            text = _load_text_from_file(input_path).strip()
    if not text:
        return {"success": False, "error": "missing text"}

    target_lang = (payload.get("target_lang") or "繁體中文").strip() or "繁體中文"
    source_lang = (payload.get("source_lang") or "auto").strip() or "auto"
    mode = (payload.get("mode") or "full").strip() or "full"
    export = str(payload.get("export") or "auto").strip().lower()
    export_prefix = (payload.get("export_prefix") or "translate").strip() or "translate"
    max_inline_chars = int(payload.get("max_inline_chars") or 3000)
    try:
        llm_timeout = int(payload.get("llm_timeout") or os.environ.get("MAGI_TRANSLATOR_LLM_TIMEOUT_SEC", "180") or "180")
    except Exception:
        llm_timeout = 600
    llm_timeout = max(20, min(7200, llm_timeout))

    # Legal termbase context (passed through from translate_core)
    _legal_tier1 = str(payload.get("_legal_tier1") or "").strip()
    _legal_tier2 = str(payload.get("_legal_tier2") or "").strip()

    # For long inputs, use chunked local mode first to avoid long blocking calls.
    direct_chunk_min = int(os.environ.get("MAGI_TRANSLATOR_DIRECT_CHUNK_MIN", "1200") or "1200")
    if len(text) >= direct_chunk_min:
        chunked = _translate_chunks_local(
            text=text,
            source_lang=source_lang,
            target_lang=target_lang,
            timeout_per_chunk=max(60, llm_timeout // 3),
            max_chunks=int(os.environ.get("MAGI_TRANSLATOR_MAX_CHUNKS", "200") or "200"),
            legal_tier1=_legal_tier1,
            legal_tier2=_legal_tier2,
        )
        if chunked:
            if export in {"1", "true", "yes", "on"} or len(chunked) > max_inline_chars:
                exported = _export_txt(chunked, prefix=export_prefix)
                if exported.get("success"):
                    return {
                        "success": True,
                        "mode": mode,
                        "target_lang": target_lang,
                        "source_lang": source_lang,
                        "exported": True,
                        "export_path": exported.get("path", ""),
                        "download_url": exported.get("url", ""),
                        "preview": chunked[: min(800, len(chunked))],
                        "doc_key": "",
                        "tabs": [],
                        "provider": "casper_chunk_local_direct",
                        "model": os.environ.get("CASPER_LOCAL_MODEL", os.environ.get("MAGI_TEXT_PRIMARY_MODEL", "")),
                    }
            return {
                "success": True,
                "mode": mode,
                "target_lang": target_lang,
                "source_lang": source_lang,
                "exported": False,
                "text": chunked,
                "doc_key": "",
                "tabs": [],
                "provider": "casper_chunk_local_direct",
                "model": os.environ.get("CASPER_LOCAL_MODEL", os.environ.get("MAGI_TEXT_PRIMARY_MODEL", "")),
            }

    def _looks_refusal(s: str) -> bool:
        t = (s or "").strip().lower()
        if not t:
            return True
        flags = [
            "cannot translate",
            "can't translate",
            "無法翻譯",
            "不能翻譯",
            "無法提供源語言",
            "未提供任何內容",
            "請提供您想要翻譯",
            "what's the source text",
            "what is the source text",
            "ready to translate",
            "i cannot",
            "i can't",
            "抱歉",
        ]
        if any(k in t for k in flags):
            return True
        if re.search(r"(請提供.*(文本|文字|內容|source text)|沒有提供.*(文本|內容)|無法.*翻譯)", t):
            return True
        return False

    # Fast path can hang on some local model stacks; default to disabled for stability.
    if (len(text) <= int(os.environ.get("MAGI_TRANSLATOR_FAST_MAX_CHARS", "0") or "0")) and (not re.search(r"https?://", text, flags=re.IGNORECASE)):
        try:
            from skills.bridge.grounded_ai import _generate_local  # type: ignore

            prompt = (
                "You are a professional translator.\n"
                f"Source language: {source_lang}\n"
                f"Target language: {target_lang}\n"
                "IMPORTANT: Full translation only. No summary, no omissions.\n\n"
                f"{text}"
            )
            out = _strip_translation_preamble((_generate_local(prompt, temperature=0.2, timeout=llm_timeout, num_ctx=4096) or "").strip())
            if out and not _looks_refusal(out):
                if export in {"1", "true", "yes", "on"}:
                    exported = _export_txt(out, prefix=export_prefix)
                    if exported.get("success"):
                        return {
                            "success": True,
                            "mode": mode,
                            "target_lang": target_lang,
                            "source_lang": source_lang,
                            "exported": True,
                            "export_path": exported.get("path", ""),
                            "download_url": exported.get("url", ""),
                            "preview": out[: min(800, len(out))],
                            "doc_key": "",
                            "tabs": [],
                            "provider": "casper_local_ollama",
                            "model": os.environ.get("CASPER_LOCAL_MODEL", os.environ.get("MAGI_TEXT_PRIMARY_MODEL", "")),
                            "fast_path": True,
                        }
                return {
                    "success": True,
                    "mode": mode,
                    "target_lang": target_lang,
                    "source_lang": source_lang,
                    "exported": False,
                    "text": out,
                    "doc_key": "",
                    "tabs": [],
                    "provider": "casper_local_ollama",
                    "model": os.environ.get("CASPER_LOCAL_MODEL", os.environ.get("MAGI_TEXT_PRIMARY_MODEL", "")),
                    "fast_path": True,
                }
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 618, exc_info=True)
        # Secondary fast fallback via InferenceGateway.
        try:
            from skills.bridge.inference_gateway import InferenceGateway

            prompt2 = (
                f"請將以下內容完整翻譯成{target_lang}。\n"
                "僅輸出翻譯結果，不要補充說明。\n\n"
                + text
            )
            _gw = InferenceGateway()
            qr = _gw.chat(prompt2, task_type="translate", timeout=min(25, max(10, llm_timeout // 2)))
            qtxt = _strip_translation_preamble((qr.get("response") or "").strip()) if isinstance(qr, dict) else ""
            if qtxt and (not _looks_refusal(qtxt)):
                if export in {"1", "true", "yes", "on"}:
                    exported = _export_txt(qtxt, prefix=export_prefix)
                    if exported.get("success"):
                        return {
                            "success": True,
                            "mode": mode,
                            "target_lang": target_lang,
                            "source_lang": source_lang,
                            "exported": True,
                            "export_path": exported.get("path", ""),
                            "download_url": exported.get("url", ""),
                            "preview": qtxt[: min(800, len(qtxt))],
                            "doc_key": "",
                            "tabs": [],
                            "provider": "casper_quick_local_fallback",
                            "model": str(qr.get("model") or os.environ.get("MAGI_TEXT_PRIMARY_MODEL", "")),
                            "fast_path": True,
                        }
                return {
                    "success": True,
                    "mode": mode,
                    "target_lang": target_lang,
                    "source_lang": source_lang,
                    "exported": False,
                    "text": qtxt,
                    "doc_key": "",
                    "tabs": [],
                    "provider": "casper_quick_local_fallback",
                    "model": str(qr.get("model") or os.environ.get("MAGI_TEXT_PRIMARY_MODEL", "")),
                    "fast_path": True,
                }
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 664, exc_info=True)
    # P2-0 修（2026-04-19）：原先此處呼叫 `translate_text(...)` 會經由
    # tri_sage_collab → translate_core → translate() → subprocess(_translate_inner) 形成
    # 無限遞迴 fork 炸彈（單次觸發可 spawn 100+ 子進程，吃爆 24GB RAM 讓整台 Mac 卡死）。
    # 改為空 result 直接 fall through 到下面的 chunked_local 與 InferenceGateway fallback，
    # 不再繞回 tri_sage_collab 這條會造成遞迴的路徑。
    r = {"success": False, "error": "_translate_inner: tri_sage_collab path disabled (recursion guard)"}

    if not isinstance(r, dict) or not r.get("success"):
        # Chunked local fallback for longer text to reduce hard timeouts.
        if len(text) >= 1200:
            try:
                chunked = _translate_chunks_local(
                    text=text,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    timeout_per_chunk=max(60, llm_timeout // 2),
                    max_chunks=int(os.environ.get("MAGI_TRANSLATOR_MAX_CHUNKS", "200") or "200"),
                    legal_tier1=_legal_tier1,
                    legal_tier2=_legal_tier2,
                )
                if chunked:
                    if export in {"1", "true", "yes", "on"} or len(chunked) > max_inline_chars:
                        exported = _export_txt(chunked, prefix=export_prefix)
                        if exported.get("success"):
                            return {
                                "success": True,
                                "mode": mode,
                                "target_lang": target_lang,
                                "source_lang": source_lang,
                                "exported": True,
                                "export_path": exported.get("path", ""),
                                "download_url": exported.get("url", ""),
                                "preview": chunked[: min(800, len(chunked))],
                                "doc_key": "",
                                "tabs": [],
                                "fallback_provider": "casper_chunk_local",
                            }
                    return {
                        "success": True,
                        "mode": mode,
                        "target_lang": target_lang,
                        "source_lang": source_lang,
                        "exported": False,
                        "text": chunked,
                        "doc_key": "",
                        "tabs": [],
                        "fallback_provider": "casper_chunk_local",
                    }
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 716, exc_info=True)

        # Fallback: ask CASPER local path to avoid hard timeout failures.
        try:
            from skills.bridge.grounded_ai import ask_casper  # type: ignore

            prompt = (
                f"請把下列內容完整翻譯成{target_lang}。"
                "不要摘要、不要省略、保留原本段落與條列結構：\n\n"
                + text
            )
            fb = _strip_translation_preamble((ask_casper(prompt) or "").strip())
            if fb:
                return {
                    "success": True,
                    "mode": mode,
                    "target_lang": target_lang,
                    "source_lang": source_lang,
                    "exported": False,
                    "text": fb,
                    "doc_key": "",
                    "tabs": [],
                    "fallback_provider": "casper_local",
                }
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 741, exc_info=True)
        return {"success": False, "error": (r.get("error") if isinstance(r, dict) else "translate failed")}

    out = _strip_translation_preamble((r.get("text") or "").strip())
    if not out:
        return {"success": False, "error": "empty translation"}

    need_export = False
    if export in {"1", "true", "yes", "on"}:
        need_export = True
    elif export in {"0", "false", "no", "off"}:
        need_export = False
    else:
        need_export = len(out) > max_inline_chars

    exported = {}
    if need_export:
        exported = _export_txt(out, prefix=export_prefix)
        if exported.get("success"):
            return {
                "success": True,
                "mode": mode,
                "target_lang": target_lang,
                "source_lang": source_lang,
                "exported": True,
                "export_path": exported.get("path", ""),
                "download_url": exported.get("url", ""),
                "preview": out[: min(800, len(out))],
                "doc_key": r.get("doc_key", ""),
                "tabs": r.get("tabs") or [],
                "note": f"文字過長已輸出 TXT（{_now_hint()}）",
            }

    # inline return (short enough)
    return {
        "success": True,
        "mode": mode,
        "target_lang": target_lang,
        "source_lang": source_lang,
        "exported": False,
        "text": out,
        "doc_key": r.get("doc_key", ""),
        "tabs": r.get("tabs") or [],
    }


def translate(payload: dict) -> dict:
    try:
        llm_timeout = int(payload.get("llm_timeout") or os.environ.get("MAGI_TRANSLATOR_LLM_TIMEOUT_SEC", "600") or "600")
    except Exception:
        llm_timeout = 600
    llm_timeout = max(20, min(7200, llm_timeout))
    try:
        timeout_sec = int(payload.get("timeout_sec") or os.environ.get("MAGI_TRANSLATOR_TIMEOUT_SEC", str(llm_timeout + 300)) or str(llm_timeout + 300))
    except Exception:
        timeout_sec = llm_timeout + 300
    timeout_sec = max(5, timeout_sec)

    inner_payload = {
        "text": payload.get("text") or payload.get("value") or "",
        "input_path": payload.get("input_path") or payload.get("path") or "",
        "target_lang": payload.get("target_lang") or "繁體中文",
        "source_lang": payload.get("source_lang") or "auto",
        "mode": payload.get("mode") or "full",
        "export": payload.get("export") or "auto",
        "export_prefix": payload.get("export_prefix") or "translate",
        "max_inline_chars": payload.get("max_inline_chars") or 3000,
        "llm_timeout": llm_timeout,
    }
    # Thread legal termbase context if present
    _t1 = str(payload.get("_legal_tier1") or "").strip()
    _t2 = str(payload.get("_legal_tier2") or "").strip()
    if _t1:
        inner_payload["_legal_tier1"] = _t1
    if _t2:
        inner_payload["_legal_tier2"] = _t2

    short_text = str(inner_payload.get("text") or "").strip()
    # If text empty but input_path provided, load file contents for APE/stable-primary eligibility checks.
    if not short_text:
        _ip = str(inner_payload.get("input_path") or "").strip()
        if _ip and os.path.isfile(_ip):
            try:
                short_text = _load_text_from_file(_ip).strip()
                if short_text:
                    inner_payload["text"] = short_text
            except Exception:
                short_text = ""

    # ── Apple Translation + LLM Post-Edit (APE) ───────────────────────────
    # Opt-in via MAGI_TRANSLATOR_APE=1. Only for legal text, short-to-medium
    # length, and zh↔en pair. Produces Apple baseline → LLM polish →
    # validator, falls back to baseline if the edit is rejected.
    ape_enabled = str(os.environ.get("MAGI_TRANSLATOR_APE", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
    ape_max_chars = int(os.environ.get("MAGI_TRANSLATOR_APE_MAX_CHARS", "1200") or "1200")
    if ape_enabled and short_text and len(short_text) <= ape_max_chars:
        try:
            from skills.engine.apple_translation import normalize_lang, is_available as _apple_avail
            from skills.translator._apple_post_edit import translate_with_ape, is_legal_text
            src_code = normalize_lang(str(inner_payload.get("source_lang") or "zh-Hant"))
            tgt_code = normalize_lang(str(inner_payload.get("target_lang") or "en"))
            # Only run APE on zh↔en pairs where on-device pack is likely installed.
            # Auto-source: detect ASCII-heavy text as English.
            auto_src_is_en = False
            if src_code == "auto":
                sample = short_text[:2000]
                ascii_ratio = sum(1 for c in sample if ord(c) < 128) / max(1, len(sample))
                auto_src_is_en = ascii_ratio > 0.6
            zh_en_pair = (
                (src_code.startswith("zh") and tgt_code == "en") or
                (src_code == "en" and tgt_code.startswith("zh")) or
                (src_code == "auto" and tgt_code in {"en"} and is_legal_text(short_text)) or
                (src_code == "auto" and tgt_code.startswith("zh") and auto_src_is_en and is_legal_text(short_text))
            )
            # Resolve effective source code for APE call
            if src_code == "auto":
                eff_src = "en" if auto_src_is_en else "zh-Hant"
            else:
                eff_src = src_code
            apple_ok, _apple_reason = _apple_avail()
            if apple_ok and zh_en_pair and is_legal_text(short_text):
                ape_res = translate_with_ape(
                    short_text,
                    source_lang=eff_src,
                    target_lang=tgt_code,
                    llm_timeout=min(60, max(15, llm_timeout // 3)),
                    apple_timeout=float(os.environ.get("MAGI_APPLE_TRANSLATION_TIMEOUT_SEC", "10.0") or "10.0"),
                    tier1=str(inner_payload.get("_legal_tier1") or ""),
                    tier2=str(inner_payload.get("_legal_tier2") or ""),
                )
                if ape_res.get("success") and str(ape_res.get("text") or "").strip():
                    _res = {
                        "success": True,
                        "mode": inner_payload.get("mode") or "full",
                        "target_lang": inner_payload.get("target_lang") or "繁體中文",
                        "source_lang": inner_payload.get("source_lang") or "auto",
                        "exported": False,
                        "text": str(ape_res.get("text") or "").strip(),
                        "provider": str(ape_res.get("provider") or "apple_translation_ape"),
                        "degraded": bool(ape_res.get("degraded") or False),
                        "baseline": ape_res.get("baseline"),
                        "validator": ape_res.get("validator"),
                        "elapsed_ms": ape_res.get("elapsed_ms"),
                    }
                    return _maybe_export_docx(payload, inner_payload, _res)
        except Exception:
            logging.getLogger(__name__).debug("ape: path errored, falling through", exc_info=True)

    stable_primary_enabled = str(
        os.environ.get("MAGI_TRANSLATOR_STABLE_PRIMARY", "1") or "1"
    ).strip().lower() in {"1", "true", "yes", "on"}
    stable_primary_max_chars = int(os.environ.get("MAGI_TRANSLATOR_STABLE_PRIMARY_MAX_CHARS", "1600") or "1600")
    if stable_primary_enabled and short_text and len(short_text) <= max(120, stable_primary_max_chars):
        try:
            out = _translate_via_google_gtx(
                short_text,
                target_lang=str(inner_payload.get("target_lang") or "繁體中文"),
                timeout_sec=min(12, max(5, timeout_sec // 3)),
            )
            if out.strip():
                _sp_res = {
                    "success": True,
                    "mode": inner_payload.get("mode") or "full",
                    "target_lang": inner_payload.get("target_lang") or "繁體中文",
                    "source_lang": inner_payload.get("source_lang") or "auto",
                    "exported": False,
                    "text": out.strip(),
                    "provider": "google_gtx_primary",
                    "degraded": False,
                }
                return _maybe_export_docx(payload, inner_payload, _sp_res)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 824, exc_info=True)

    cmd = [sys.executable, __file__, "--task", "_translate_inner"]
    env = os.environ.copy()
    env["MAGI_TRANSLATOR_NO_VENV"] = "1"
    try:
        cp = subprocess.run(
            cmd,
            input=json.dumps(inner_payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=timeout_sec,
            env=env,
        )
    except subprocess.TimeoutExpired:
        fb = _non_llm_fallback(inner_payload, reason=f"timeout after {timeout_sec}s")
        return fb if fb.get("success") else {"success": False, "error": f"timeout after {timeout_sec}s"}
    except Exception as e:
        fb = _non_llm_fallback(inner_payload, reason=f"subprocess error: {type(e).__name__}")
        return fb if fb.get("success") else {"success": False, "error": f"subprocess error: {type(e).__name__}: {e}"}

    out = (cp.stdout or "").strip()
    if cp.returncode != 0 and not out:
        return {"success": False, "error": f"translate subprocess failed (rc={cp.returncode})"}
    try:
        inner_res = _load_jsonish(out)
    except Exception:
        inner_res = {}
    if not isinstance(inner_res, dict) or not inner_res.get("success"):
        err = inner_res.get("error") if isinstance(inner_res, dict) else ""
        if not err:
            err = (cp.stderr or "").strip()[:300] or f"translate subprocess failed (rc={cp.returncode})"
        fb = _non_llm_fallback(inner_payload, reason=str(err))
        return fb if fb.get("success") else {"success": False, "error": err}

    inner_res = _maybe_export_docx(payload, inner_payload, inner_res)
    return inner_res


def _maybe_export_docx(payload: dict, inner_payload: dict, inner_res: dict) -> dict:
    """Attach bilingual DOCX if export_format is docx (default) and result has text."""
    try:
        export_format = str(payload.get("export_format") or "docx").strip().lower()
        if export_format in {"txt", "none", "off", "0"} or not inner_res.get("success"):
            return inner_res
        source_text = inner_payload.get("text") or ""
        if not source_text:
            input_path = str(inner_payload.get("input_path") or "").strip()
            if input_path:
                source_text = _load_text_from_file(input_path)
        translated_text = inner_res.get("text") or ""
        # Prefer full exported TXT over truncated preview
        exp_path = inner_res.get("export_path") or ""
        if exp_path and os.path.isfile(exp_path):
            try:
                with open(exp_path, "r", encoding="utf-8") as f:
                    file_text = f.read().strip()
                if file_text:
                    translated_text = file_text
            except Exception:
                pass
        if not translated_text:
            translated_text = inner_res.get("preview") or ""
        if translated_text:
            docx_res = _export_docx_bilingual(
                source_text=source_text,
                translated_text=translated_text,
                title=str(payload.get("docx_title") or ""),
                subtitle=str(payload.get("docx_subtitle") or ""),
                prefix=str(payload.get("export_prefix") or "translate"),
            )
            if docx_res.get("success"):
                inner_res["docx_exported"] = True
                inner_res["docx_path"] = docx_res.get("path", "")
                inner_res["docx_filename"] = docx_res.get("filename", "")
                inner_res["docx_url"] = docx_res.get("url", "")
    except Exception:
        logging.getLogger(__name__).debug("maybe_export_docx failed", exc_info=True)
    return inner_res


def translate_core(
    text: str,
    *,
    target_lang: str = "繁體中文",
    source_lang: Optional[str] = None,
    mode: str = "full",
    export: bool = False,
    timeout_sec: int = 60,
    llm_timeout: int = 25,
) -> dict:
    """
    Single authoritative translation entry point.

    Used by skills.bridge.tri_sage_collab.translate_text and /collab/translate.
    APE (Apple Translation + LLM post-edit) path is activated when
    MAGI_TRANSLATOR_APE=1, Apple sidecar is available, and text is legal.

    Schema returned:
    {
      "success": bool,
      "text": str,
      "provider": str,   # google_gtx_primary / google_gtx_fallback / llm / extractive_fallback / apple_ape
      "degraded": bool,
      "elapsed_sec": float,
      "export_path": str | None,
      "error": str | None,
    }
    """
    ape_enabled = os.environ.get("MAGI_TRANSLATOR_APE", "0").strip().lower() in {"1", "true", "yes", "on"}
    if ape_enabled:
        try:
            from skills.engine.apple_translation import is_available
            from skills.translator._apple_post_edit import is_legal_text, translate_with_ape
            avail, _ = is_available()
            ape_max = int(os.environ.get("MAGI_TRANSLATOR_APE_MAX_CHARS", "5000") or "5000")
            if avail and is_legal_text(text) and len(text) <= ape_max:
                return translate_with_ape(
                    text,
                    target_lang=target_lang,
                    source_lang=source_lang or "auto",
                )
        except Exception:
            logging.getLogger(__name__).debug("APE routing failed, falling through", exc_info=True)

    payload: dict = {
        "text": text,
        "target_lang": target_lang,
        "source_lang": source_lang or "auto",
        "mode": mode,
        "export": "1" if export else "0",
        "timeout_sec": timeout_sec,
        "llm_timeout": llm_timeout,
    }
    try:
        from skills.translator.legal_termbase import should_use_legal_mode, build_glossary_for_chunk
        if should_use_legal_mode(text):
            _t1, _t2 = build_glossary_for_chunk(text, target_lang)
            if _t1:
                payload["_legal_tier1"] = _t1
            if _t2:
                payload["_legal_tier2"] = _t2
    except Exception:
        pass  # graceful: legal termbase unavailable
    return translate(payload)


def main() -> int:
    _maybe_reexec_venv()
    ap = argparse.ArgumentParser(description="translator skill")
    ap.add_argument("--task", required=True, help="help|self_test|translate {..json..}")
    args = ap.parse_args()
    task = (args.task or "").strip()

    if task in {"help", "list"}:
        return _ok({"success": True, "commands": ["help", "self_test", "translate {..json..}"]})

    if task == "self_test":
        try:
            st_timeout = int(os.environ.get("MAGI_TRANSLATOR_SELF_TEST_TIMEOUT_SEC", "55") or "55")
        except Exception:
            st_timeout = 55
        res = translate({
            "text": "請翻譯：Hello world",
            "mode": "full",
            "export": "0",
            "timeout_sec": max(20, st_timeout),
            "llm_timeout": int(os.environ.get("MAGI_TRANSLATOR_SELF_TEST_LLM_TIMEOUT_SEC", "18") or "18"),
        })
        ok = bool(res.get("success"))
        if not ok and "timeout" in str(res.get("error", "")).lower():
            return _ok(
                {
                    "success": True,
                    "degraded": True,
                    "preview": "",
                    "note": "translator self_test timeout; service marked degraded",
                    "result": {"error": res.get("error", "")},
                }
            )
        return _ok({"success": ok, "preview": (res.get("text") or res.get("preview") or "")[:200], "result": res if not ok else None})

    if task == "_translate_inner":
        payload = _load_jsonish((sys.stdin.read() or "").strip())
        return _ok(_translate_inner(payload))

    if task.startswith("translate"):
        payload = _load_jsonish(task[len("translate") :].strip())
        return _ok(translate(payload))

    return _ok({"success": False, "error": f"unknown task: {task}"})


if __name__ == "__main__":
    raise SystemExit(main())
