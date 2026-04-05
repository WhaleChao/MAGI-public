import base64
import json
import logging
import math
import os
import re
import struct
import time
import wave
from datetime import datetime

import requests

from api.model_config import TEXT_PRIMARY_MODEL
from skills.bridge.http_pool import get_session as _get_session
from skills.bridge import balthasar_bridge, melchior_bridge, melchior_client
from skills.documents.vector_pipeline import ingest_sections_to_vector_memory, ingest_text_to_vector_memory
import hashlib
from concurrent.futures import ThreadPoolExecutor

_bg_executor = ThreadPoolExecutor(max_workers=3)
from skills.research.web_research import fetch_url_sections
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

try:
    from api.routing.node_registry import get_node_ip as _get_node_ip
    MELCHIOR_HOST = os.environ.get("MELCHIOR_HOST") or _get_node_ip("melchior") or "100.116.54.16"
except Exception:
    MELCHIOR_HOST = os.environ.get("MELCHIOR_HOST", "100.116.54.16")
MELCHIOR_PORT = int(os.environ.get("MELCHIOR_PORT", "5002"))
MELCHIOR_BASE = f"http://{MELCHIOR_HOST}:{MELCHIOR_PORT}"
logger = logging.getLogger("TriSageCollab")


def _safe_name(prefix: str, ext: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    token = os.urandom(3).hex()
    return f"{prefix}_{stamp}_{token}.{ext}"


def _ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def _extract_first_url(text: str) -> str:
    m = re.search(r"https?://\S+", text or "", flags=re.IGNORECASE)
    if not m:
        return ""
    url = (m.group(0) or "").strip().rstrip(").,;\\]}>\"'")
    return url


def _chunk_text(text: str, chunk_size: int, max_chunks: int) -> list[str]:
    s = (text or "").strip()
    if not s:
        return []
    out = []
    i = 0
    while i < len(s) and len(out) < max(1, int(max_chunks)):
        out.append(s[i : i + int(chunk_size)])
        i += int(chunk_size)
    return out


def _sample_chunks_evenly(chunks: list[str], max_samples: int) -> list[tuple[int, str]]:
    """
    Pick up to max_samples chunks spread across the full document.
    Returns list of (1-based index, chunk_text).
    """
    parts = [c for c in (chunks or []) if (c or "").strip()]
    if not parts:
        return []
    n = len(parts)
    k = max(1, min(int(max_samples), n))
    if k >= n:
        return [(i + 1, parts[i]) for i in range(n)]
    # Evenly spaced indices including endpoints.
    idxs = sorted({int(round(i * (n - 1) / (k - 1))) for i in range(k)}) if k > 1 else [0]
    out = []
    for i in idxs:
        i = max(0, min(n - 1, i))
        out.append((i + 1, parts[i]))
    return out


def _translate_workers() -> int:
    try:
        workers = int(os.environ.get("TRI_SAGE_TRANSLATE_WORKERS", "5") or "5")
    except Exception:
        workers = 5
    return max(1, min(workers, 8))


def _bounded_translate_timeout(base_timeout: int, *, scale: float = 1.0) -> int:
    try:
        floor = int(os.environ.get("TRI_SAGE_TRANSLATE_TIMEOUT_FLOOR_SEC", "16") or "16")
    except Exception:
        floor = 16
    try:
        ceiling = int(os.environ.get("TRI_SAGE_TRANSLATE_TIMEOUT_CEIL_SEC", "3600") or "3600")
    except Exception:
        ceiling = 3600
    if ceiling < floor:
        ceiling = floor
    try:
        requested = int(round(float(base_timeout) * float(scale)))
    except Exception:
        requested = int(base_timeout or 0)
    return max(floor, min(ceiling, max(1, requested)))


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
    return _PREAMBLE_RE.sub("", s, count=1).strip()


def _translate_llm_call(prompt: str, timeout_sec: int) -> dict:
    """
    Translation-specialized resilient call with retry:
    - try quick local model first for responsiveness
    - then fallback to normal local-first chat routing
    - retry up to MAGI_TRANSLATE_RETRY_ATTEMPTS times with exponential backoff
    """
    import logging as _logging
    _log = _logging.getLogger("tri_sage_translate")
    t = max(8, int(timeout_sec or 0))
    quick_model = (os.environ.get("TRI_SAGE_TRANSLATE_LOCAL_MODEL") or TEXT_PRIMARY_MODEL).strip() or TEXT_PRIMARY_MODEL
    max_retries = int(os.environ.get("MAGI_TRANSLATE_RETRY_ATTEMPTS", "3") or "3")

    from skills.bridge.inference_gateway import InferenceGateway
    _gw = InferenceGateway()

    for attempt in range(1, max_retries + 1):
        try:
            r = _gw.chat(prompt, task_type="translate", timeout=t, model=quick_model)
            if r.get("success") and str(r.get("response") or "").strip():
                r["response"] = _strip_translation_preamble(str(r.get("response") or ""))
                return r
        except Exception as e:
            _log.warning("gateway translate attempt %d/%d failed: %s", attempt, max_retries, e)

        # Exponential backoff before retry (2s, 4s, 8s)
        if attempt < max_retries:
            backoff = min(16, 2 ** attempt)
            _log.info("Translate retry %d/%d in %ds...", attempt, max_retries, backoff)
            time.sleep(backoff)

    # All retries exhausted
    return {"success": False, "error": f"all {max_retries} translation attempts failed"}


def translate_text(
    text: str,
    target_lang: str = "繁體中文",
    source_lang: str = "auto",
    mode: str = "auto",
    timeout: int | None = None,
) -> dict:
    content = (text or "").strip()
    if not content:
        return {"success": False, "error": "missing text"}
    try:
        timeout = int(timeout if timeout is not None else os.environ.get("TRI_SAGE_TRANSLATE_TIMEOUT", "600"))
    except Exception:
        timeout = 600
    timeout = max(16, min(timeout, 7200))
    max_full_chars = int(os.environ.get("TRI_SAGE_TRANSLATE_MAX_FULL_CHARS", "9000"))
    chunk_chars = int(os.environ.get("TRI_SAGE_TRANSLATE_CHUNK_CHARS", "3200"))
    max_chunks = int(os.environ.get("TRI_SAGE_TRANSLATE_MAX_CHUNKS", "6"))
    vector_chunk_chars = int(os.environ.get("TRI_SAGE_VECTOR_CHUNK_CHARS", "1200"))
    vector_overlap = int(os.environ.get("TRI_SAGE_VECTOR_OVERLAP", "120"))
    vector_max_chunks = int(os.environ.get("TRI_SAGE_VECTOR_MAX_CHUNKS", "220"))
    url = _extract_first_url(content)
    try:
        from skills.bridge.llm_direct import feature_enabled as _codex_feature_enabled, translate_with_codex

        codex_max_chars = int(os.environ.get("MAGI_CODEX_TRANSLATE_MAX_CHARS", "12000") or "12000")
        if (not url) and _codex_feature_enabled("translate") and len(content) <= max(800, codex_max_chars):
            codex_res = translate_with_codex(
                content,
                source_lang=source_lang,
                target_lang=target_lang,
                timeout_sec=int(os.environ.get("MAGI_CODEX_TRANSLATE_TIMEOUT_SEC", "240") or "240"),
            )
            codex_text = _strip_translation_preamble(str(codex_res.get("text") or ""))
            if codex_res.get("success") and codex_text:
                return {
                    "success": True,
                    "text": codex_text,
                    "provider": "openclaw_codex",
                    "route": "openclaw_codex",
                    "model": codex_res.get("model", "gpt-5.4"),
                    "agent": codex_res.get("agent_id", "codex-distributed"),
                }
            if codex_res.get("error"):
                logger.warning("tri_sage translate: codex route failed: %s", codex_res.get("error"))
    except Exception as codex_err:
        logger.warning("tri_sage translate: codex route skipped: %s", codex_err)

    # User intent hints: "不要摘要" means full translation is required.
    low = content.lower()
    want_full = str(mode or "").strip().lower() in {"full", "complete", "全文", "全文翻譯", "full_translation"}
    if not want_full:
        if any(k in content for k in ("不要摘要", "不需要摘要", "完整翻譯", "全文翻譯", "不要精簡", "不要改寫")):
            want_full = True
    # Default rule (per MAGI product spec): if user asks for translation but did NOT ask for summary,
    # we should do full translation (no summary) by default.
    if not want_full:
        explicit_summary = any(k in content for k in ("摘要", "總結", "懶人包", "重點整理", "summary"))
        explicit_translate = any(k in content for k in ("翻譯", "翻成", "translate", "translation"))
        if explicit_translate and not explicit_summary:
            want_full = True

    # If user provided a URL, fetch sections (tabs/panels) first (Iron Dome checked).
    # Treat "請翻譯這個網頁：<url>" as URL-mode too (not only pure url).
    url_residual = (content.replace(url, " ").strip()) if url else ""
    url_onlyish = bool(url) and (len(url_residual) <= 80)
    if url and (len(content) <= len(url) + 20 or url_onlyish):
        fetched = fetch_url_sections(url, max_length=max_full_chars, max_sections=8)
        if not fetched.get("success"):
            return {"success": False, "error": f"fetch failed: {fetched.get('error', 'unknown')}"}
        page_title = (fetched.get("title") or "").strip()
        sections = [s for s in (fetched.get("sections") or []) if isinstance(s, dict)]
        sections = [s for s in sections if (s.get("content") or "").strip()]
        if not sections:
            return {"success": False, "error": "fetched page has empty content"}

        # Compute doc_key deterministically
        doc_key = f"url-{hashlib.sha1((url or '').encode('utf-8')).hexdigest()[:12]}"
        try:
            # Offload vector DB ingestion to the background executor so we don't block the API
            _bg_executor.submit(
                ingest_sections_to_vector_memory,
                url=url,
                title=page_title,
                sections=sections,
                chunk_chars=vector_chunk_chars,
                overlap=vector_overlap,
                max_chunks_total=vector_max_chunks,
            )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 251, exc_info=True)

        # Translate per section to avoid context overflows and "single huge prompt" stalls.
        from concurrent.futures import ThreadPoolExecutor, as_completed
        last_meta = {}

        def _translate_section(idx, s):
            sec_title = (s.get("title") or s.get("id") or f"Section {idx}").strip()
            body = (s.get("content") or "").strip()
            if not body:
                return idx, None, {}
            if want_full:
                prompt = (
                    "You are a professional translator.\n"
                    f"Source language: {source_lang}\n"
                    f"Target language: {target_lang}\n"
                    "The input is ONE tab/section of a webpage.\n"
                    "IMPORTANT: The user explicitly requested FULL translation.\n"
                    "- Do NOT write any summary.\n"
                    "- Do NOT shorten or omit parts.\n"
                    "- Preserve structure (headings, lists) as much as possible.\n"
                    "\n"
                    f"[Webpage title]\n{page_title}\n"
                    f"[URL]\n{url}\n"
                    f"[Section]\n{sec_title}\n\n"
                    f"{body}"
                )
            else:
                prompt = (
                    "You are a professional translator.\n"
                    f"Source language: {source_lang}\n"
                    f"Target language: {target_lang}\n"
                    "The input is ONE tab/section of a webpage.\n"
                    "Output in the target language with this format:\n"
                    "1) 摘要（3-7 點，條列）\n"
                    "2) 翻譯（精準但允許適度精簡重複內容；保留法律/數字/日期/關鍵結論；若太長請翻譯關鍵段落並說明已精簡）\n"
                    "\n"
                    f"[Webpage title]\n{page_title}\n"
                    f"[URL]\n{url}\n"
                    f"[Section]\n{sec_title}\n\n"
                    f"{body}"
                )
            r = _translate_llm_call(prompt, _bounded_translate_timeout(timeout, scale=1.0))
            if not r.get("success"):
                return idx, None, r
            return idx, f"=== {sec_title} ===\n{(r.get('response') or '').strip()}", r

        section_results = [None] * len(sections)
        with ThreadPoolExecutor(max_workers=_translate_workers()) as executor:
            fut_map = {executor.submit(_translate_section, i+1, s): i for i, s in enumerate(sections)}
            for f in as_completed(fut_map):
                i = fut_map[f]
                try:
                    _, rendered, meta = f.result()
                    section_results[i] = rendered
                    if meta:
                        last_meta = meta
                    if meta and not meta.get("success") and rendered is None:
                        return {"success": False, "error": meta.get("error", "translate failed")}
                except Exception as e:
                    return {"success": False, "error": str(e)}
        rendered_sections = [r for r in section_results if r is not None]

        overall = ""
        if not want_full:
            # Overall combined summary based on the section summaries/translations we produced.
            combined_prompt = (
                "請用繁體中文給出「整體摘要」(5-10 點，條列)。\n"
                "你可以參考下方各分頁的摘要/翻譯結果，但不要編造未出現的內容。\n\n"
                + "\n\n".join(rendered_sections[-8:])
            )
            combined = _translate_llm_call(combined_prompt, _bounded_translate_timeout(timeout, scale=0.85))
            overall = (combined.get("response") or "").strip() if combined.get("success") else ""

        head = f"📄 {page_title}\n{url}\n\n" if (page_title or url) else ""
        foot = ""
        if doc_key and os.environ.get("MAGI_VECTOR_NOTE", "0") == "1":
            foot = f"\n\n[向量索引] doc_key={doc_key}"
        out = head + "\n\n".join(rendered_sections)
        if overall:
            out += "\n\n=== 整體摘要 ===\n" + overall
        out += foot

        return {
            "success": True,
            "text": out,
            "provider": (last_meta.get("route", "melchior") or "melchior"),
            "model": (last_meta.get("model", "") or ""),
            "tabs": [s.get("title") or s.get("id") for s in sections],
            "doc_key": doc_key,
        }

    # Non-URL: For long content, default to translated summary; but if user requests "不要摘要", do full translation (chunked).
    if len(content) > max_full_chars:
        # Compute doc_key deterministically
        doc_key = f"text-{hashlib.sha1((content or '').encode('utf-8')).hexdigest()[:12]}"
        try:
            # Offload vector DB ingestion to the background executor so we don't block the API
            _bg_executor.submit(
                ingest_text_to_vector_memory,
                kind="text",
                primary=content,  # hashed into doc_key internally
                title="pasted_text",
                text=content,
                chunk_chars=vector_chunk_chars,
                overlap=vector_overlap,
                max_chunks_total=vector_max_chunks,
            )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 360, exc_info=True)

        all_chunks = _chunk_text(content, chunk_chars, max_chunks=10_000)
        if want_full:
            # Full translation path.
            # Default to completion-first behavior (no summary, no truncation) unless explicitly disabled.
            force_complete = os.environ.get("TRI_SAGE_FORCE_FULL_COMPLETE", "1").strip().lower() in {"1", "true", "yes", "on"}
            time_budget = int(os.environ.get("TRI_SAGE_FORCE_FULL_TIME_BUDGET_SEC", "1800"))
            max_full_chunks = int(os.environ.get("TRI_SAGE_FORCE_FULL_MAX_CHUNKS", "2000"))
            chunks_to_run = all_chunks[: max(1, max_full_chunks)]
            t0 = time.time()
            last_meta = {}

            def _translate_full_chunk(idx, part):
                prompt = (
                    "You are a professional translator.\n"
                    f"Source language: {source_lang}\n"
                    f"Target language: {target_lang}\n"
                    "IMPORTANT: The user explicitly requested FULL translation.\n"
                    "Do NOT summarize. Do NOT omit.\n"
                    f"This is chunk {idx}/{len(chunks_to_run)}. Translate ONLY this chunk.\n\n"
                    f"{part}"
                )
                r = _translate_llm_call(prompt, _bounded_translate_timeout(timeout, scale=1.0))
                return idx, r

            import logging as _logging
            _tlog = _logging.getLogger("tri_sage_translate")
            from concurrent.futures import ThreadPoolExecutor, as_completed
            translated_buf = [None] * len(chunks_to_run)
            failed_chunk_indices = []
            with ThreadPoolExecutor(max_workers=_translate_workers()) as executor:
                fut_map = {executor.submit(_translate_full_chunk, i+1, p): i for i, p in enumerate(chunks_to_run)}
                for f in as_completed(fut_map):
                    i = fut_map[f]
                    try:
                        _, r = f.result()
                        if not r.get("success"):
                            # Graceful degradation: preserve original text instead of aborting
                            _tlog.warning("Chunk %d/%d failed: %s — keeping original text", i+1, len(chunks_to_run), r.get("error", "unknown"))
                            translated_buf[i] = chunks_to_run[i].strip()
                            failed_chunk_indices.append(i+1)
                        else:
                            translated_buf[i] = (r.get("response") or "").strip()
                            last_meta = r
                    except Exception as e:
                        _tlog.warning("Chunk %d/%d exception: %s — keeping original text", i+1, len(chunks_to_run), e)
                        translated_buf[i] = chunks_to_run[i].strip()
                        failed_chunk_indices.append(i+1)

            # ── AUTO-RETRY PASS: sequential retry with longer timeout on failed chunks ──
            if failed_chunk_indices:
                _tlog.info("Auto-retry pass: %d failed chunks, retrying sequentially with 1.5× timeout...", len(failed_chunk_indices))
                retry_timeout = _bounded_translate_timeout(timeout, scale=1.5)
                still_failed = []
                for cidx in failed_chunk_indices:
                    i = cidx - 1  # Convert 1-based to 0-based
                    try:
                        _, r = _translate_full_chunk(cidx, chunks_to_run[i])
                        if r.get("success") and (r.get("response") or "").strip():
                            translated_buf[i] = (r.get("response") or "").strip()
                            last_meta = r
                            _tlog.info("Auto-retry chunk %d/%d succeeded", cidx, len(chunks_to_run))
                        else:
                            still_failed.append(cidx)
                    except Exception as e:
                        _tlog.warning("Auto-retry chunk %d failed again: %s", cidx, e)
                        still_failed.append(cidx)
                failed_chunk_indices = still_failed

            # ── GOOGLE GTX FALLBACK for any still-failed chunks ──
            if failed_chunk_indices:
                _tlog.info("GTX fallback: %d chunks still failed, trying Google Translate...", len(failed_chunk_indices))
                try:
                    from skills.translator.action import _translate_via_google_gtx
                    gtx_still_failed = []
                    for cidx in failed_chunk_indices:
                        i = cidx - 1
                        try:
                            gtx_out = _translate_via_google_gtx(chunks_to_run[i], target_lang=target_lang, timeout_sec=12)
                            if gtx_out and gtx_out.strip():
                                translated_buf[i] = gtx_out.strip()
                                _tlog.info("GTX fallback chunk %d succeeded", cidx)
                            else:
                                gtx_still_failed.append(cidx)
                        except Exception as e:
                            _tlog.warning("GTX fallback chunk %d failed: %s", cidx, e)
                            gtx_still_failed.append(cidx)
                    failed_chunk_indices = gtx_still_failed
                except ImportError:
                    _tlog.warning("GTX fallback unavailable (import error)")

            translated = [t for t in translated_buf if t]

            out = "\n\n".join([t for t in translated if t]).strip()
            if failed_chunk_indices:
                out += (
                    f"\n\n⚠️ 有 {len(failed_chunk_indices)} 個段落翻譯失敗，已先保留原文，稍後可針對該段重跑。"
                )
            if len(all_chunks) > len(chunks_to_run):
                out += (
                    "\n\n（提示：內容超過安全分段上限，已翻譯前 "
                    f"{len(chunks_to_run)} 段；若需完整一次處理，請提高 TRI_SAGE_FORCE_FULL_MAX_CHUNKS。）"
                )
            elif (not force_complete) and len(translated) < len(chunks_to_run):
                out += "\n\n（提示：已在時間預算內完成可翻譯段落；如需剩餘段落，請回覆「繼續翻譯」。）"
            foot = ""
            if doc_key and os.environ.get("MAGI_VECTOR_NOTE", "0") == "1":
                foot = f"\n\n[向量索引] doc_key={doc_key}"
            return {
                "success": True,
                "text": (out + foot).strip(),
                "provider": "tri-sage",
                "model": (last_meta.get("model", "") or ""),
                "note": "full translation (chunked)",
                "doc_key": doc_key,
                "failed_chunks": len(failed_chunk_indices),
                "total_chunks": len(chunks_to_run),
            }

        # Summary translation path (default for very long text)
        sampled = _sample_chunks_evenly(all_chunks, max_samples=max_chunks)
        payload = []
        for i, part in sampled:
            payload.append(f"[Chunk {i}/{len(all_chunks)}]\n{part}")
        joined = "\n\n".join(payload)[: max_full_chars]
        prompt = (
            "You are a professional translator.\n"
            f"Source language: {source_lang}\n"
            f"Target language: {target_lang}\n"
            "Task:\n"
            "1) Provide a concise translated summary.\n"
            "2) Provide translated key bullet points.\n"
            "3) If the text is long, do NOT attempt full translation; focus on key facts and conclusions.\n"
            "4) Keep formatting clean.\n\n"
            f"{joined}"
        )
        result = _translate_llm_call(prompt, _bounded_translate_timeout(timeout, scale=0.9))
        if result.get("success"):
            foot = ""
            if doc_key and os.environ.get("MAGI_VECTOR_NOTE", "0") == "1":
                foot = f"\n\n[向量索引] doc_key={doc_key}"
            return {
                "success": True,
                "text": (result.get("response", "") or "") + foot,
                "provider": result.get("route", "melchior"),
                "model": result.get("model", ""),
                "note": "content too long; returned summary translation",
                "doc_key": doc_key,
            }
        return {"success": False, "error": result.get("error", "translate failed")}

    # Chunked translation for medium text to keep per-request latency bounded.
    chunks = _chunk_text(content, chunk_chars, max_chunks)
    if not chunks:
        return {"success": False, "error": "empty text after normalization"}

    # Optionally store medium text too (helps later follow-ups without re-pasting).
    doc_key = ""
    try:
        if len(content) >= int(os.environ.get("TRI_SAGE_VECTOR_MIN_CHARS", "2500")):
            ing = ingest_text_to_vector_memory(
                kind="text",
                primary=content,
                title="pasted_text",
                text=content,
                chunk_chars=vector_chunk_chars,
                overlap=vector_overlap,
                max_chunks_total=vector_max_chunks,
            )
            if ing.get("success"):
                doc_key = ing.get("doc_key", "") or ""
    except Exception:
        doc_key = ""

    last_meta = {}

    def _translate_med_chunk(idx, part):
        prompt = (
            "You are a professional translator.\n"
            f"Source language: {source_lang}\n"
            f"Target language: {target_lang}\n"
            "Keep meaning accurate and preserve bullets/format.\n"
            f"This is chunk {idx}/{len(chunks)}. Translate ONLY this chunk.\n\n"
            f"{part}"
        )
        r = _translate_llm_call(prompt, _bounded_translate_timeout(timeout, scale=1.0))
        return idx, r

    import logging as _logging
    _tlog2 = _logging.getLogger("tri_sage_translate")
    from concurrent.futures import ThreadPoolExecutor, as_completed
    translated_buf = [None] * len(chunks)
    failed_med_chunks = []
    with ThreadPoolExecutor(max_workers=_translate_workers()) as executor:
        fut_map = {executor.submit(_translate_med_chunk, i+1, p): i for i, p in enumerate(chunks)}
        for f in as_completed(fut_map):
            i = fut_map[f]
            try:
                _, r = f.result()
                if not r.get("success"):
                    _tlog2.warning("Med chunk %d/%d failed: %s — keeping original", i+1, len(chunks), r.get("error", "unknown"))
                    translated_buf[i] = chunks[i].strip()
                    failed_med_chunks.append(i+1)
                else:
                    translated_buf[i] = (r.get("response") or "").strip()
                    last_meta = r
            except Exception as e:
                _tlog2.warning("Med chunk %d/%d exception: %s — keeping original", i+1, len(chunks), e)
                translated_buf[i] = chunks[i].strip()
                failed_med_chunks.append(i+1)

    # ── AUTO-RETRY PASS for medium chunks ──
    if failed_med_chunks:
        _tlog2.info("Auto-retry pass: %d med chunks failed, retrying sequentially...", len(failed_med_chunks))
        retry_timeout = _bounded_translate_timeout(timeout, scale=1.5)
        still_failed = []
        for cidx in failed_med_chunks:
            i = cidx - 1
            try:
                _, r = _translate_med_chunk(cidx, chunks[i])
                if r.get("success") and (r.get("response") or "").strip():
                    translated_buf[i] = (r.get("response") or "").strip()
                    last_meta = r
                    _tlog2.info("Auto-retry med chunk %d succeeded", cidx)
                else:
                    still_failed.append(cidx)
            except Exception as e:
                _tlog2.warning("Auto-retry med chunk %d failed: %s", cidx, e)
                still_failed.append(cidx)
        failed_med_chunks = still_failed

    # ── GOOGLE GTX FALLBACK for still-failed med chunks ──
    if failed_med_chunks:
        _tlog2.info("GTX fallback: %d med chunks still failed...", len(failed_med_chunks))
        try:
            from skills.translator.action import _translate_via_google_gtx
            gtx_still = []
            for cidx in failed_med_chunks:
                i = cidx - 1
                try:
                    gtx_out = _translate_via_google_gtx(chunks[i], target_lang=target_lang, timeout_sec=12)
                    if gtx_out and gtx_out.strip():
                        translated_buf[i] = gtx_out.strip()
                        _tlog2.info("GTX fallback med chunk %d succeeded", cidx)
                    else:
                        gtx_still.append(cidx)
                except Exception:
                    gtx_still.append(cidx)
            failed_med_chunks = gtx_still
        except ImportError:
            pass

    translated = [t for t in translated_buf if t]

    foot = ""
    if doc_key and os.environ.get("MAGI_VECTOR_NOTE", "0") == "1":
        foot = f"\n\n[向量索引] doc_key={doc_key}"
    final_text = "\n\n".join([t for t in translated if t])
    if failed_med_chunks:
        final_text += f"\n\n⚠️ 有 {len(failed_med_chunks)} 個段落翻譯失敗，已先保留原文，稍後可針對該段重跑。"
    return {
        "success": True,
        "text": final_text + foot,
        "provider": "tri-sage",
        "model": (last_meta.get("model", "") or ""),
        "doc_key": doc_key,
        "failed_chunks": len(failed_med_chunks),
        "total_chunks": len(chunks),
    }


def _save_base64_audio(audio_base64: str, output_path: str) -> str:
    raw = base64.b64decode(audio_base64.encode("utf-8"))
    with open(output_path, "wb") as f:
        f.write(raw)
    return output_path


def _render_procedural_music(prompt: str, duration_sec: int, output_path: str) -> str:
    # Minimal local fallback synthesizer to guarantee service continuity.
    sample_rate = 22050
    frames = int(sample_rate * max(6, min(180, duration_sec)))
    mood = (prompt or "").lower()
    base_freq = 220.0 if "sad" in mood else 261.63
    if "epic" in mood or "battle" in mood:
        base_freq = 329.63
    if "cute" in mood or "happy" in mood:
        base_freq = 293.66
    melody = [1.0, 1.25, 1.5, 2.0, 1.5, 1.25]
    beat = int(sample_rate * 0.3)

    with wave.open(output_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        for i in range(frames):
            idx = (i // beat) % len(melody)
            freq = base_freq * melody[idx]
            value = 0.3 * math.sin(2 * math.pi * freq * (i / sample_rate))
            value += 0.1 * math.sin(2 * math.pi * (freq / 2.0) * (i / sample_rate))
            # Soft fade to reduce click noise.
            t = i / max(1, frames - 1)
            env = min(1.0, t * 8.0, (1.0 - t) * 6.0 + 0.2)
            sample = int(max(-1.0, min(1.0, value * env)) * 32767)
            wf.writeframes(struct.pack("<h", sample))
    return output_path


def generate_music(prompt: str, duration_sec: int = 30, output_dir: str = f"{_MAGI_ROOT}/static/audio") -> dict:
    content = (prompt or "").strip()
    if not content:
        return {"success": False, "error": "missing prompt"}
    _ensure_dir(output_dir)

    # 1) Prefer Melchior GPU-side generator when available.
    endpoints = [
        f"{MELCHIOR_BASE}/api/generate_music",
        f"{MELCHIOR_BASE}/api/music/generate",
        f"{MELCHIOR_BASE}/api/audio/music",
    ]
    payload = {"prompt": content, "duration_sec": int(max(6, min(180, duration_sec)))}
    for url in endpoints:
        try:
            r = _get_session().post(url, json=payload, timeout=1800)
            if r.status_code != 200:
                continue
            data = r.json() if "application/json" in (r.headers.get("content-type", "").lower()) else {}
            if not isinstance(data, dict):
                continue
            if data.get("success") is False:
                continue

            if data.get("audio_base64"):
                out = os.path.join(output_dir, _safe_name("music", "wav"))
                _save_base64_audio(data["audio_base64"], out)
                return {"success": True, "path": out, "provider": "melchior_music_api", "endpoint": url}

            if data.get("url"):
                audio_url = data["url"]
                if audio_url.startswith("/"):
                    audio_url = f"{MELCHIOR_BASE}{audio_url}"
                rr = _get_session().get(audio_url, timeout=60)
                if rr.status_code == 200:
                    ext = "wav"
                    out = os.path.join(output_dir, _safe_name("music", ext))
                    with open(out, "wb") as f:
                        f.write(rr.content)
                    return {"success": True, "path": out, "provider": "melchior_music_api", "endpoint": url}

            if data.get("path"):
                return {"success": True, "path": data["path"], "provider": "melchior_music_api", "endpoint": url}
        except Exception:
            continue

    # 2) Local procedural fallback.
    out = os.path.join(output_dir, _safe_name("music_fallback", "wav"))
    _render_procedural_music(content, duration_sec, out)
    return {
        "success": True,
        "path": out,
        "provider": "casper_procedural_fallback",
        "note": "Melchior music endpoint unavailable, generated local fallback track.",
    }


def transcribe_audio(audio_path: str) -> dict:
    import logging as _logging
    _tlog_tr = _logging.getLogger("tri_sage_transcribe")
    path = (audio_path or "").strip()
    if not path:
        return {"success": False, "error": "missing audio_path"}
    if not os.path.exists(path):
        return {"success": False, "error": f"file not found: {path}"}

    engine = (os.environ.get("MAGI_TRANSCRIBE_ENGINE") or "auto").strip().lower()
    max_retries = int(os.environ.get("MAGI_TRANSCRIBE_RETRY_ATTEMPTS", "3") or "3")
    last_err = ""

    for attempt in range(1, max_retries + 1):
        # 1) Apple Intelligence / on-device Speech (best-effort)
        if engine in {"auto", "apple", "apple_intelligence", "speech"}:
            try:
                from skills.apple.apple_intelligence import transcribe_audio as apple_transcribe

                ar = apple_transcribe(path, engine="auto")
                if isinstance(ar, dict) and ar.get("success") and (ar.get("text") or "").strip():
                    return {
                        "success": True,
                        "text": (ar.get("text") or "").strip(),
                        "provider": "apple",
                        "engine": ar.get("engine", "auto"),
                    }
            except Exception as e:
                _tlog_tr.warning("Apple transcribe attempt %d/%d failed: %s", attempt, max_retries, e)

            if engine in {"apple", "apple_intelligence", "speech"}:
                last_err = "apple transcription failed (or not authorized) and engine forced to apple"
                if attempt < max_retries:
                    time.sleep(min(8, 2 ** attempt))
                continue

        # 2) Existing default: Balthasar (mlx-whisper)
        try:
            result = balthasar_bridge.transcribe(path)
            if isinstance(result, dict) and result.get("success"):
                return {
                    "success": True,
                    "text": result.get("text", ""),
                    "provider": "balthasar",
                    "segments": result.get("segments") or [],
                    "timestamp_text": result.get("timestamp_text", ""),
                    "speaker_text": result.get("speaker_text", ""),
                    "speaker_count_estimate": result.get("speaker_count_estimate", 0),
                }
            last_err = result.get("error", "unknown") if isinstance(result, dict) else str(result)
        except Exception as e:
            last_err = str(e)
            _tlog_tr.warning("Balthasar transcribe attempt %d/%d failed: %s", attempt, max_retries, e)

        # Backoff before retry
        if attempt < max_retries:
            backoff = min(10, 2 ** attempt)
            _tlog_tr.info("Transcribe retry %d/%d in %ds...", attempt, max_retries, backoff)
            time.sleep(backoff)

    return {"success": False, "error": f"all {max_retries} transcription attempts failed: {last_err}"}


def generate_image(prompt: str) -> dict:
    content = (prompt or "").strip()
    if not content:
        return {"success": False, "error": "missing prompt"}
    return melchior_bridge.generate_image(content)


def perform_admin_task(
    task_type: str,
    prompt: str = "",
    text: str = "",
    file_path: str = "",
    target_lang: str = "繁體中文",
) -> dict:
    kind = (task_type or "").strip().lower()
    if kind in {"translate", "translation", "翻譯"}:
        return translate_text(text or prompt, target_lang=target_lang)
    if kind in {"music", "生成音樂", "製作音樂"}:
        return generate_music(prompt or text)
    if kind in {"transcribe", "逐字稿", "stt"}:
        return transcribe_audio(file_path)
    if kind in {"image", "畫圖", "image_gen"}:
        return generate_image(prompt or text)
    return {"success": False, "error": f"unsupported task_type: {task_type}"}
