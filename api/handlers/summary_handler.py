"""
Summary handler — extracted from orchestrator.py.

Standalone functions for text summarisation (resilient multi-strategy).
"""

from __future__ import annotations

import logging
import math
import os
import re
import time

from api.model_config import SUMMARY_MODEL, TEXT_PRIMARY_MODEL
from skills.bridge.inference_gateway import InferenceGateway

logger = logging.getLogger("SummaryHandler")


# ---------------------------------------------------------------------------
# summary_length_prompt
# ---------------------------------------------------------------------------

def summary_length_prompt(length: str) -> tuple[str, str]:
    """Return (chunk_prompt_hint, reduce_prompt_hint) for the given length."""
    if length == "short":
        return ("輸出 **3-5 點**精簡條列，每點一句話", "先給 **3-5 點**核心重點，每點一句話")
    if length == "long":
        return (
            "輸出 **10-15 點**詳細條列，每點 2-3 句話（先寫結論，再補充背景或數據）",
            "先給 **12-18 點**詳細重點，每點 2-3 句話（先寫結論，再補充背景、數據或法條依據）",
        )
    return ("輸出 **5-8 點**條列，每點 1-2 句話（包含關鍵事實）", "先給 **8-12 點**重點，每點 1-2 句話")


def _is_synthetic_timeout_fallback(text: str, result: Optional[dict] = None) -> bool:
    t = str(text or "").strip()
    if isinstance(result, dict) and bool(result.get("synthetic_fallback")):
        return True
    if not t:
        return False
    if t.startswith("（系統降級回覆）"):
        return True
    return "本機模型逾時" in t and "請稍後重試" in t


def _summary_chunk_usable(text: str, result: Optional[dict] = None) -> bool:
    from api.handlers import text_processing_handler as _tp

    t = str(text or "").strip()
    if len(t) < 24:
        return False
    if _is_synthetic_timeout_fallback(t, result):
        return False
    if any(marker in t for marker in ("摘要失敗", "段摘要逾時", "先略過")):
        return False
    return not _tp.output_guard_issues(t, mode="summary")


# ---------------------------------------------------------------------------
# summarize_text_resilient
# ---------------------------------------------------------------------------

def summarize_text_resilient(text: str, summary_length: str = "medium", *, progress_callback=None) -> dict:
    s = (text or "").strip()
    if not s:
        return {"success": False, "error": "empty text"}
    chunk_hint, reduce_hint = summary_length_prompt(summary_length)

    try:
        max_chars = int(os.environ.get("MAGI_FILE_SUMMARY_MAX_CHARS", "120000") or "120000")
    except Exception:
        max_chars = 120000
    payload = s if len(s) <= max_chars else (s[: max_chars // 2] + "\n...\n" + s[-max_chars // 2 :])
    try:
        summary_timeout = int(os.environ.get("MAGI_FILE_SUMMARY_TIMEOUT_SEC", "120") or "120")
    except Exception:
        summary_timeout = 45
    summary_timeout = max(12, min(summary_timeout, 180))

    def _chunk_by_paragraph(txt: str, limit_chars: int) -> list[str]:
        t = (txt or "").strip()
        if not t:
            return []
        pieces = re.split(r"\n\s*\n", t)
        chunks = []
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

    def _sample_evenly_chunks(chunks: list[str], max_samples: int) -> list[tuple[int, str]]:
        parts = [c for c in (chunks or []) if str(c or "").strip()]
        if not parts:
            return []
        n = len(parts)
        k = max(1, min(int(max_samples), n))
        if k >= n:
            return [(i + 1, parts[i]) for i in range(n)]
        idxs = sorted({int(round(i * (n - 1) / (k - 1))) for i in range(k)}) if k > 1 else [0]
        out = []
        for i in idxs:
            ii = max(0, min(n - 1, i))
            out.append((ii + 1, parts[ii]))
        return out

    def _summary_output_usable(out: str) -> bool:
        from api.handlers import text_processing_handler as _tp
        t = str(out or "").strip()
        if len(t) < 48:
            return False
        if _is_synthetic_timeout_fallback(t):
            return False
        lowered = t.lower()
        if "降級摘要" in t[:20]:
            return False
        if len(re.findall(r"摘要失敗|先略過", t)) >= 2:
            return False
        refusal_markers = [
            "對不起，我無法找到您要的內容",
            "無法找到您要的內容",
            "是否可以提供更多資訊",
            "請提供更多資訊",
            "提供更多的資訊",
            "沒有提供任何有關的內容",
            "沒有提供任何相關內容",
            "目前沒有足夠資訊",
            "請提供您需要我分析",
            "請提供您需要我摘要",
            "請提供需要我",
            "請提供需要摘要",
            "請提供檔案內容",
            "請提供文件內容",
            "我無法直接存取",
            "很抱歉",
            "i cannot find",
            "please provide more information",
        ]
        if any(marker.lower() in lowered for marker in refusal_markers):
            return False
        return not _tp.output_guard_issues(t, mode="summary")

    def _extractive_fallback_summary(txt: str) -> str:
        body = str(txt or "").strip()
        if not body:
            return ""

        def _clean_sentence(s: str) -> str:
            out = re.sub(r"--- 第\s*\d+\s*頁(?:\s*\(OCR\))? ---", " ", str(s or ""))
            out = re.sub(r"\s+", " ", out).strip(" -\t\r\n")
            out = re.sub(r"\b\w+\.indb\s+\d+\b", " ", out, flags=re.IGNORECASE)
            out = re.sub(r"\.{4,}|…{2,}", " ", out)
            out = re.sub(r"\s+", " ", out).strip()
            return out

        page_count = len(re.findall(r"--- 第\s*\d+\s*頁(?:\s*\(OCR\))? ---", body))
        lines = [re.sub(r"\s+", " ", str(line or "").strip()) for line in body.splitlines()]
        title_candidates = []
        seen_titles = set()
        for line in lines[:180]:
            if not line or line.startswith("--- 第 ") or len(line) < 6 or len(line) > 140:
                continue
            if re.fullmatch(r"[\d\W_]+", line):
                continue
            alpha_words = re.findall(r"[A-Za-z]{3,}", line)
            has_cjk = bool(re.search(r"[\u4e00-\u9fff]", line))
            uppercase_ratio = 0.0
            letters = re.findall(r"[A-Za-z]", line)
            if letters:
                uppercase_ratio = sum(1 for ch in letters if ch.isupper()) / max(1, len(letters))
            looks_like_heading = (
                has_cjk
                or uppercase_ratio >= 0.55
                or len(alpha_words) >= 3
                or any(k in line.upper() for k in ("JUDGMENT", "CASE", "COURT", "APPLICATION", "OBJECTIONS", "CONVENTION"))
            )
            norm = re.sub(r"[^\w\u4e00-\u9fff]+", "", line).lower()
            if looks_like_heading and norm and norm not in seen_titles:
                seen_titles.add(norm)
                title_candidates.append(line)
            if len(title_candidates) >= 4:
                break

        keyword_priority = [
            "judgment", "objection", "objections", "jurisdiction", "requests",
            "declares", "finds", "holds", "concludes", "committee", "convention",
            "appends", "dissenting", "separate opinion",
            "判決", "聲請", "主文", "理由", "法院", "法官", "條約", "管轄",
        ]
        merged_body = _clean_sentence(body)
        candidate_sentences = []
        if merged_body:
            candidate_sentences = [
                _clean_sentence(sent)
                for sent in re.split(r"(?<=[。！？!?；;\.])\s+", merged_body)
            ]

        snippets = []
        seen_snippets = set()

        def _extract_section_candidates(pattern: str, limit: int = 2) -> list[str]:
            try:
                m = re.search(pattern, body, flags=re.IGNORECASE | re.DOTALL)
            except Exception:
                m = None
            if not m:
                return []
            block = _clean_sentence(m.group(1))
            if not block:
                return []
            picked = []
            for sent in re.split(r"(?<=[。！？!?；;])\s+", block):
                clean = _clean_sentence(sent)
                if len(clean) < 28:
                    continue
                picked.append(clean[:200])
                if len(picked) >= limit:
                    break
            if picked:
                return picked
            return [block[:200]] if block else []

        def _push_snippet(text_value: str) -> None:
            clean_value = _clean_sentence(text_value)
            if not clean_value or len(clean_value) < 42:
                return
            if re.fullmatch(r"[\d\W_]+", clean_value):
                return
            if re.search(r"\b(?:indb|page\s+\d+|no de vente)\b", clean_value, re.IGNORECASE):
                return
            if clean_value.startswith("關鍵字") or clean_value.startswith("Keywords"):
                return
            if "目錄" in clean_value:
                return
            if len(re.findall(r"[一二三四五六七八九十壹貳參肆伍陸柒捌玖拾][、.]", clean_value)) >= 3 and len(re.findall(r"\d+", clean_value)) >= 4:
                return
            norm_value = re.sub(r"[^A-Za-z\u4e00-\u9fff0-9]+", "", clean_value).lower()
            if not norm_value:
                return
            for seen_value in seen_snippets:
                if norm_value == seen_value or norm_value in seen_value or seen_value in norm_value:
                    return
            seen_snippets.add(norm_value)
            snippets.append(clean_value[:400])

        abstract_candidates = _extract_section_candidates(
            r"(?:^|\n)\s*(?:摘要|abstract)\s*[:：]?\s*(.+?)(?=\n\s*(?:關鍵字|关键词|keywords?|目錄|table of contents|壹、|一、|前言|緒論|introduction)\b)",
            limit=2,
        )
        conclusion_candidates = _extract_section_candidates(
            r"(?:^|\n)\s*(?:伍、結論|陸、結論|柒、結論|結論|研究結論|conclusion)\s*[:：]?\s*(.+?)(?=\n\s*(?:參考文獻|references|附錄|謝詞|致謝)\b)",
            limit=2,
        )
        for sent in abstract_candidates + conclusion_candidates:
            _push_snippet(sent)
            if len(snippets) >= 8:
                break

        for sent in candidate_sentences:
            lowered = sent.lower()
            if any(key in lowered for key in keyword_priority):
                _push_snippet(sent)
            if len(snippets) >= 8:
                break

        sample_source = _chunk_by_paragraph(body, 2200)
        sampled_chunks = _sample_evenly_chunks(sample_source, 10)
        for _, chunk in sampled_chunks:
            clean = _clean_sentence(chunk)
            if not clean:
                continue
            sentence_candidates = re.split(r"(?<=[。！？!?；;\.])\s+", clean)
            chosen = ""
            for sent in sentence_candidates:
                sent = _clean_sentence(sent)
                if len(sent) < 48:
                    continue
                if re.fullmatch(r"[\d\W_]+", sent):
                    continue
                chosen = sent[:260]
                break
            if not chosen:
                chosen = clean[:260]
            if chosen:
                _push_snippet(chosen)
            if len(snippets) >= 8:
                break

        translated_snippets = []
        translated_titles = []
        translation_inputs = []
        if title_candidates:
            translation_inputs.extend(title_candidates[:3])
        translation_inputs.extend(snippets[:8])
        if translation_inputs:
            snippet_block = "\n".join(f"- {s}" for s in translation_inputs[:10])
            mostly_non_cjk = len(re.findall(r"[A-Za-z]", snippet_block)) > max(40, len(re.findall(r"[\u4e00-\u9fff]", snippet_block)) * 2)
            translate_fallback = os.environ.get("MAGI_FILE_SUMMARY_TRANSLATE_EXTRACTIVE_FALLBACK", "1").strip().lower() in {"1", "true", "yes", "on"}
            if mostly_non_cjk and translate_fallback:
                try:
                    from api.handlers.translation_handler import translate_text_complete
                    tr = translate_text_complete(snippet_block, source_lang="auto", target_lang="繁體中文")
                    translated = str((tr or {}).get("text") or "").strip()
                    if tr.get("success") and translated:
                        translated_lines = [
                            re.sub(r"^[-•\d\.\s]+", "", line).strip()
                            for line in translated.splitlines()
                            if re.sub(r"^[-•\d\.\s]+", "", line).strip()
                        ]
                        translated_titles = translated_lines[: min(2, len(title_candidates))]
                        translated_snippets = translated_lines[len(translated_titles) :]
                except Exception:
                    translated_titles = []
                    translated_snippets = []

        usable_snippets = translated_snippets or snippets
        usable_snippets = [s for s in usable_snippets if str(s or "").strip()][:8]
        if not usable_snippets and title_candidates:
            usable_snippets = title_candidates[:3]

        parts = ["【文件概況】"]
        if page_count:
            parts.append(f"- 可辨識頁數：約 {page_count} 頁")
        title_block = translated_titles or title_candidates
        if title_block:
            parts.append("- 可能案名/主題：")
            for item in title_block[:4]:
                parts.append(f"  - {item}")
        if usable_snippets:
            parts.append("")
            parts.append("【重點摘要】")
            for i, item in enumerate(usable_snippets, 1):
                parts.append(f"{i}. {item}")
        return "\n".join(parts).strip()

    # --- Strategy: direct-first, map-reduce only for very large docs ---
    # Local oMLX model already pinned in memory. Try direct summary first
    # to avoid expensive model-swapping in map-reduce chunks.
    # Only fall back to map-reduce if text exceeds direct model capacity.
    try:
        direct_max_chars = int(os.environ.get("MAGI_FILE_SUMMARY_DIRECT_MAX_CHARS", "8000") or "8000")
    except Exception:
        direct_max_chars = 8000

    if len(payload) <= direct_max_chars:
        # Direct path: feed full text to primary model in one shot.
        try:
            from skills.bridge.balthasar_bridge import summarize_text
            if len(payload) <= 2500:
                direct_timeout = max(20, min(summary_timeout, 45))
            elif len(payload) <= 8000:
                direct_timeout = max(30, min(summary_timeout, 75))
            else:
                direct_timeout = max(45, min(summary_timeout, 120))
            rr = summarize_text(payload, timeout_sec=direct_timeout, summary_length=summary_length)
            if isinstance(rr, dict) and rr.get("success"):
                out = str(rr.get("text") or rr.get("summary") or "").strip()
                if _summary_output_usable(out):
                    return {"success": True, "text": out, "provider": f"{str(rr.get('provider') or 'balthasar')}_direct"}
            logger.warning("_summarize_text_resilient: direct summary failed, trying map-reduce")
        except Exception as e:
            logger.warning("_summarize_text_resilient: direct summary exception: %s", e)

    # Long-document summary map-reduce (multi-threaded) — for texts > direct_max_chars or direct failure.
    try:
        parallel_enabled = os.environ.get("MAGI_FILE_SUMMARY_PARALLEL", "1").strip().lower() in {"1", "true", "yes", "on"}
        parallel_threshold = int(os.environ.get("MAGI_FILE_SUMMARY_PARALLEL_THRESHOLD_CHARS", "6000") or "6000")
        summary_chunk_chars = int(os.environ.get("MAGI_FILE_SUMMARY_CHUNK_CHARS", "5000") or "5000")
        summary_workers = int(os.environ.get("MAGI_FILE_SUMMARY_WORKERS", "1") or "1")
        summary_max_samples = int(os.environ.get("MAGI_FILE_SUMMARY_MAX_SAMPLES", "10") or "10")
        summary_chunk_timeout = int(os.environ.get("MAGI_FILE_SUMMARY_CHUNK_TIMEOUT_SEC", "120") or "120")
        summary_reduce_timeout = int(os.environ.get("MAGI_FILE_SUMMARY_REDUCE_TIMEOUT_SEC", "90") or "90")
    except Exception:
        parallel_enabled, parallel_threshold, summary_chunk_chars, summary_workers = True, 18000, 5000, 3
        summary_max_samples, summary_chunk_timeout, summary_reduce_timeout = 10, 60, 90
    summary_chunk_chars = max(1200, min(summary_chunk_chars, 12000))
    summary_workers = max(1, min(summary_workers, 4))
    summary_max_samples = max(2, min(summary_max_samples, 16))
    summary_chunk_timeout = max(10, min(summary_chunk_timeout, 180))
    summary_reduce_timeout = max(10, min(summary_reduce_timeout, 180))
    try:
        summary_chunk_retries = int(os.environ.get("MAGI_FILE_SUMMARY_CHUNK_RETRIES", "2") or "2")
    except Exception:
        summary_chunk_retries = 2
    try:
        summary_split_retry_depth = int(os.environ.get("MAGI_FILE_SUMMARY_SPLIT_RETRY_DEPTH", "1") or "1")
    except Exception:
        summary_split_retry_depth = 1
    try:
        summary_split_retry_chars = int(
            os.environ.get(
                "MAGI_FILE_SUMMARY_SPLIT_RETRY_CHARS",
                str(max(1800, summary_chunk_chars // 2)),
            )
            or str(max(1800, summary_chunk_chars // 2))
        )
    except Exception:
        summary_split_retry_chars = max(1800, summary_chunk_chars // 2)
    summary_chunk_retries = max(0, min(summary_chunk_retries, 4))
    summary_split_retry_depth = max(0, min(summary_split_retry_depth, 2))
    summary_split_retry_chars = max(1200, min(summary_split_retry_chars, summary_chunk_chars))
    try:
        ultra_threshold_chars = int(os.environ.get("MAGI_FILE_SUMMARY_ULTRA_THRESHOLD_CHARS", "100000") or "100000")
    except Exception:
        ultra_threshold_chars = 100000
    try:
        ultra_threshold_chunks = int(os.environ.get("MAGI_FILE_SUMMARY_ULTRA_THRESHOLD_CHUNKS", "24") or "24")
    except Exception:
        ultra_threshold_chunks = 24

    estimated_chunks = max(1, math.ceil(len(payload) / max(1, summary_chunk_chars)))
    if len(payload) >= ultra_threshold_chars or estimated_chunks >= ultra_threshold_chunks:
        try:
            from skills.documents.pdf_bridge import summarize_ultra_large_text

            ultra_text = summarize_ultra_large_text(
                payload,
                source_hint="summary-text",
                progress_callback=progress_callback,
                summary_length=summary_length,
            )
            if ultra_text and _summary_output_usable(ultra_text):
                return {"success": True, "text": ultra_text, "provider": "hierarchical_ultra"}
            logger.warning("_summarize_text_resilient: ultra-large summary path returned unusable text")
        except Exception as e:
            logger.warning("_summarize_text_resilient: ultra-large summary exception: %s", e)

    if parallel_enabled and len(payload) >= parallel_threshold:
        chunks = _chunk_by_paragraph(payload, summary_chunk_chars)
        if len(chunks) >= 2:
            try:
                from skills.bridge import melchior_client
                from concurrent.futures import wait
                from api.thread_pools import inference_pool

                # Large documents: process ALL chunks (map-reduce).
                # Short documents: sample evenly (existing behavior).
                _mr_threshold = int(os.environ.get("MAGI_PDF_MR_THRESHOLD_CHARS", "8000") or "8000")
                _mr_full_max = int(os.environ.get("MAGI_PDF_MR_FULL_MAX_CHUNKS", "16") or "16")
                if len(payload) > _mr_threshold and len(chunks) <= _mr_full_max:
                    sampled_chunks = [(i + 1, c) for i, c in enumerate(chunks)]
                else:
                    sampled_chunks = _sample_evenly_chunks(chunks, summary_max_samples)
                summaries = [""] * len(sampled_chunks)

                # Chunks use oMLX local model (primary, fast Apple Silicon).
                # Falls back to Ollama if oMLX unavailable.

                def _run_chunk_prompt(
                    chunk_text: str,
                    *,
                    label: str,
                    total_chunks: int,
                    timeout_sec: int,
                    max_tokens: int = 512,
                    merge_mode: bool = False,
                ) -> str:
                    if merge_mode:
                        prompt = (
                            "你是專業文件分析師。以下是同一段長文件拆小後的子段摘要。\n\n"
                            "請合併為一份不重複、保留關鍵事實的繁體中文條列摘要。\n"
                            "要求：\n"
                            f"- {chunk_hint}\n"
                            "- 保留數字、日期、人名、法條關鍵詞\n"
                            "- 只輸出摘要\n\n"
                            f"{chunk_text}"
                        )
                    else:
                        prompt = (
                            "你是專業文件分析師。請用繁體中文整理以下段落的重點。\n\n"
                            "要求：\n"
                            f"- {chunk_hint}\n"
                            "- 保留數字、日期、人名、法條關鍵詞\n"
                            "- 以條列格式輸出，只輸出摘要\n"
                            f"- 這是分段 {label}/{total_chunks}，只摘要該段內容\n\n"
                            f"{chunk_text}"
                        )
                    # Try oMLX first
                    _omlx_chat = getattr(melchior_client, "_chat_omlx", None)
                    _omlx_avail = getattr(melchior_client, "_omlx_available", None)
                    if callable(_omlx_chat) and callable(_omlx_avail) and _omlx_avail():
                        q = _omlx_chat(
                            prompt=prompt,
                            model=os.environ.get("MAGI_OMLX_SUMMARY_MODEL", SUMMARY_MODEL),
                            timeout=timeout_sec,
                            temperature=0.2,
                            max_tokens=max_tokens,
                        )
                        out = str((q or {}).get("response") or "").strip()
                        if q.get("success") and _summary_chunk_usable(out, q):
                            return out
                    # Fallback to Ollama
                    _chunk_ctx = min(16384, max(4096, len(chunk_text) * 2))
                    q = InferenceGateway().chat(
                        prompt,
                        task_type="summary",
                        timeout=timeout_sec,
                        model=TEXT_PRIMARY_MODEL,
                        num_ctx=_chunk_ctx,
                        num_predict=max_tokens,
                        allow_synthetic_fallback=False,
                    )
                    out = str((q or {}).get("response") or "").strip()
                    if q.get("success") and _summary_chunk_usable(out, q):
                        return out
                    return ""

                def _summarize_chunk_text(
                    chunk_text: str,
                    *,
                    label: str,
                    total_chunks: int,
                    split_depth: int,
                ) -> str:
                    for attempt in range(summary_chunk_retries + 1):
                        timeout_sec = min(180, summary_chunk_timeout + attempt * 15)
                        out = _run_chunk_prompt(
                            chunk_text,
                            label=label,
                            total_chunks=total_chunks,
                            timeout_sec=timeout_sec,
                        )
                        if out:
                            return out
                        if attempt < summary_chunk_retries:
                            time.sleep(min(1.5, 0.5 * (attempt + 1)))

                    if split_depth <= 0 or len(chunk_text) < summary_split_retry_chars:
                        return ""

                    sub_chunks = _chunk_by_paragraph(
                        chunk_text,
                        max(1200, min(summary_split_retry_chars, max(1200, len(chunk_text) // 2))),
                    )
                    if len(sub_chunks) < 2:
                        return ""

                    sub_summaries = []
                    for sub_idx, sub_chunk in enumerate(sub_chunks, start=1):
                        sub_out = _summarize_chunk_text(
                            sub_chunk,
                            label=f"{label}.{sub_idx}",
                            total_chunks=total_chunks,
                            split_depth=split_depth - 1,
                        )
                        if sub_out:
                            sub_summaries.append(sub_out)
                    if not sub_summaries:
                        return ""
                    merged = _run_chunk_prompt(
                        "\n\n".join(sub_summaries),
                        label=label,
                        total_chunks=total_chunks,
                        timeout_sec=min(180, max(summary_chunk_timeout, 45)),
                        max_tokens=768,
                        merge_mode=True,
                    )
                    return merged or "\n".join(sub_summaries)

                def _summ_chunk(idx: int, total_chunks: int, chunk_text: str) -> tuple[int, str]:
                    out = _summarize_chunk_text(
                        chunk_text,
                        label=str(idx),
                        total_chunks=total_chunks,
                        split_depth=summary_split_retry_depth,
                    )
                    if out:
                        return idx, out
                    return idx, f"（第 {idx}/{total_chunks} 段摘要失敗，先略過）"

                try:
                    fut_map = {
                        inference_pool.submit(_summ_chunk, chunk_idx, len(chunks), chunk): sample_idx
                        for sample_idx, (chunk_idx, chunk) in enumerate(sampled_chunks)
                    }
                    rounds = max(1, (len(sampled_chunks) + max(1, summary_workers) - 1) // max(1, summary_workers))
                    overall_wait = max(summary_chunk_timeout * rounds + 5, summary_chunk_timeout + 10)
                    # For large docs with many chunks, use as_completed for progress tracking.
                    _is_large_mr = len(payload) > _mr_threshold
                    if _is_large_mr and len(sampled_chunks) > 12:
                        from concurrent.futures import as_completed as _as_completed
                        import time as _mr_time
                        _progress_throttle = [0.0]
                        _done_count = [0]
                        _deadline = _mr_time.monotonic() + overall_wait
                        for fut in _as_completed(set(fut_map.keys()), timeout=overall_wait):
                            i = fut_map[fut]
                            try:
                                _, out = fut.result(timeout=5)
                                summaries[i] = out
                            except Exception as e:
                                chunk_idx = sampled_chunks[i][0]
                                summaries[i] = f"（第 {chunk_idx}/{len(chunks)} 段摘要發生錯誤：{e}）"
                            _done_count[0] += 1
                            now = _mr_time.monotonic()
                            if progress_callback and now - _progress_throttle[0] >= 15:
                                _progress_throttle[0] = now
                                progress_callback("map", _done_count[0], len(sampled_chunks),
                                                  f"⏳ 正在分析文件... ({_done_count[0]}/{len(sampled_chunks)})")
                        # Mark any not-done as timeout (shouldn't happen with as_completed)
                        for fi, fut in enumerate(fut_map):
                            if not summaries[fut_map[fut]]:
                                chunk_idx = sampled_chunks[fut_map[fut]][0]
                                summaries[fut_map[fut]] = f"（第 {chunk_idx}/{len(chunks)} 段摘要逾時）"
                    else:
                        done, not_done = wait(set(fut_map.keys()), timeout=overall_wait)
                        for fut in done:
                            i = fut_map[fut]
                            try:
                                _, out = fut.result()
                                summaries[i] = out
                            except Exception as e:
                                chunk_idx = sampled_chunks[i][0]
                                summaries[i] = f"（第 {chunk_idx}/{len(chunks)} 段摘要發生錯誤：{e}）"
                        for fut in not_done:
                            i = fut_map[fut]
                            chunk_idx = sampled_chunks[i][0]
                            fut.cancel()
                            summaries[i] = f"（第 {chunk_idx}/{len(chunks)} 段摘要逾時，先略過）"
                finally:
                    pass  # shared inference_pool — do not shut down

                usable_summaries = [str(part).strip() for part in summaries if _summary_chunk_usable(part)]
                merged_source = "\n\n".join(
                    f"[段落 {sampled_chunks[i][0]}/{len(chunks)}]\n{part}"
                    for i, part in enumerate(summaries)
                    if _summary_chunk_usable(part)
                ).strip()
                if merged_source:
                    # For large docs: use recursive reduce from pdf_bridge
                    # For small docs: single reduce via balthasar (existing)
                    _is_large = len(payload) > _mr_threshold
                    if _is_large and len(merged_source) > 6000:
                        try:
                            from skills.documents.pdf_bridge import _mr_reduce_summaries
                            reduce_batch = int(os.environ.get("MAGI_PDF_MR_REDUCE_BATCH", "8") or "8")
                            raw_parts = [str(s).strip() for s in summaries if _summary_chunk_usable(s)]
                            out = _mr_reduce_summaries(
                                raw_parts,
                                batch_size=reduce_batch,
                                reduce_timeout=max(summary_reduce_timeout, 90),
                            )
                            if out and _summary_output_usable(out):
                                return {
                                    "success": True,
                                    "text": out,
                                    "provider": "omlx_full_map_reduce",
                                    "chunks_total": len(chunks),
                                    "chunks_sampled": len(sampled_chunks),
                                }
                        except Exception as e:
                            logger.warning("_mr_reduce_summaries failed: %s, falling back", e)

                    from skills.bridge.balthasar_bridge import summarize_text

                    rr = summarize_text(merged_source, timeout_sec=max(summary_reduce_timeout, 90), summary_length=summary_length)
                    out = str((rr or {}).get("text") or (rr or {}).get("summary") or "").strip()
                    if rr.get("success") and _summary_output_usable(out):
                        return {
                            "success": True,
                            "text": out,
                            "provider": f"{str(rr.get('provider') or 'balthasar')}_parallel_map_reduce",
                            "chunks_total": len(chunks),
                            "chunks_sampled": len(sampled_chunks),
                        }
                    fallback_text = _extractive_fallback_summary(payload)
                    if fallback_text:
                        return {
                            "success": True,
                            "text": fallback_text,
                            "provider": "extractive_fallback",
                            "chunks_total": len(chunks),
                            "chunks_sampled": len(sampled_chunks),
                        }
                    return {
                        "success": True,
                        "text": "\n\n".join(usable_summaries).strip(),
                        "provider": "chunk_fallback",
                        "chunks_total": len(chunks),
                        "chunks_sampled": len(sampled_chunks),
                    }
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 713, exc_info=True)

    try:
        from skills.bridge.balthasar_bridge import summarize_text

        rr = summarize_text(payload, summary_length=summary_length)
        if isinstance(rr, dict) and rr.get("success"):
            out = str(rr.get("text") or rr.get("summary") or "").strip()
            if _summary_output_usable(out):
                return {"success": True, "text": out, "provider": str(rr.get("provider") or "balthasar")}
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 724, exc_info=True)

    try:
        from skills.bridge import melchior_client

        prompt = (
            "你是專業文件分析師。請用繁體中文條列摘要以下內容：\n\n"
            "要求：\n"
            f"1. {reduce_hint}\n"
            "2. 保留關鍵數字、日期、法院/法條名稱\n"
            "3. 以條列格式輸出，只輸出摘要\n\n"
            f"{payload}"
        )
        _fb_ctx = min(32768, max(8192, len(payload) * 2))
        q = InferenceGateway().chat(
            prompt, task_type="summary",
            timeout=max(summary_timeout, 90),
            model=os.environ.get("MAGI_SUMMARIZE_LOCAL_MODEL", TEXT_PRIMARY_MODEL),
            num_ctx=_fb_ctx, num_predict=2048,
            allow_synthetic_fallback=False,
        )
        out = str((q or {}).get("response") or "").strip()
        if q.get("success") and _summary_output_usable(out):
            return {"success": True, "text": out, "provider": str(q.get("route") or "local_quick_ollama")}
        fallback_text = _extractive_fallback_summary(payload)
        if fallback_text:
            return {"success": True, "text": fallback_text, "provider": "extractive_fallback"}
        return {"success": False, "error": str(q.get("error") or "summary_failed")}
    except Exception as e:
        fallback_text = _extractive_fallback_summary(payload)
        if fallback_text:
            return {"success": True, "text": fallback_text, "provider": "extractive_fallback"}
        return {"success": False, "error": str(e)}
