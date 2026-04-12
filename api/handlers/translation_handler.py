import os
import re
import json
import hashlib
import math
import logging
import time
import urllib.parse
import urllib.request
from pathlib import Path

from api.model_config import TEXT_PRIMARY_MODEL, TEXT_REVIEW_MODEL
from skills.bridge.inference_gateway import InferenceGateway

logger = logging.getLogger(__name__)


def _doc_run_root(subdir: str) -> Path:
    configured = str(os.environ.get("MAGI_DOC_RUN_ROOT", "")).strip()
    root = Path(configured) if configured else (Path(__file__).resolve().parents[2] / ".magi_doc_runs")
    path = root / subdir
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _translation_checkpoint_state_path(text: str, source_lang: str, target_lang: str) -> Path:
    h = hashlib.sha1()
    h.update(str(source_lang or "auto").encode("utf-8", "ignore"))
    h.update(b"|")
    h.update(str(target_lang or "").encode("utf-8", "ignore"))
    h.update(b"|")
    h.update(str(len(text or "")).encode("ascii", "ignore"))
    h.update(b"|")
    h.update(str(text or "").encode("utf-8", "ignore"))
    return _doc_run_root("translation") / f"{h.hexdigest()[:16]}" / "state.json"


def _build_document_glossary(text: str, target_lang: str = "繁體中文") -> str:
    """從文件首段掃描法律術語，建立 per-document glossary 確保翻譯一致性。"""
    if "中文" not in target_lang and "chinese" not in str(target_lang).lower():
        return ""
    # 靜態法律術語對照表（常見國際法/判決術語）
    _LEGAL_GLOSSARY = {
        "Preliminary Objections": "初步異議",
        "Application": "申請案",
        "Applicant": "申請人",
        "Respondent": "被申請人",
        "Judgment": "判決",
        "Advisory Opinion": "諮詢意見",
        "jurisdiction": "管轄權",
        "admissibility": "可受理性",
        "merits": "案件實體",
        "provisional measures": "臨時措施",
        "counter-claim": "反請求",
        "intervening party": "參加訴訟之當事人",
        "dissenting opinion": "不同意見書",
        "separate opinion": "個別意見書",
        "res judicata": "既判力",
        "jus cogens": "強行法",
        "erga omnes": "對世義務",
        "prima facie": "初步證據",
        "amicus curiae": "法庭之友",
        "standing": "當事人適格",
        "exhaustion of local remedies": "用盡當地救濟",
        "State responsibility": "國家責任",
        "due process": "正當法律程序",
        "fair trial": "公平審判",
        "right to counsel": "受辯護人協助之權利",
    }
    sample = (text or "")[:8000]
    found = []
    for en_term, zh_term in _LEGAL_GLOSSARY.items():
        if en_term.lower() in sample.lower():
            found.append(f"- {en_term} → 「{zh_term}」")
    if not found:
        return ""
    return "\n【術語對照表（全文必須統一使用）】\n" + "\n".join(found[:15])


def translate_text_complete(text: str, source_lang: str = "auto", target_lang: str = "繁體中文") -> dict:
    from skills.bridge import melchior_client

    from api.handlers import document_handler as _dh
    source_text = str(text or "")
    target_lower = str(target_lang or "").strip().lower()
    target_is_zh = ("中文" in target_lang or target_lower.startswith("zh"))
    target_is_en = ("英文" in target_lang or target_lower in {"en", "en-us", "english"})
    doc_src_latin = len(re.findall(r"[A-Za-z]", source_text))
    doc_src_cjk = len(re.findall(r"[\u4e00-\u9fff]", source_text))
    try:
        translate_chunk_chars = int(os.environ.get("MAGI_FILE_TRANSLATE_CHUNK_CHARS", "4000") or "4000")
    except Exception:
        translate_chunk_chars = 4000
    if target_is_zh and doc_src_latin > max(600, doc_src_cjk * 3):
        if len(source_text) >= 100000:
            translate_chunk_chars = min(translate_chunk_chars, 2800)
        elif len(source_text) >= 40000:
            translate_chunk_chars = min(translate_chunk_chars, 3200)
    translate_chunk_chars = max(1600, min(translate_chunk_chars, 4800))
    chunks = _dh.split_translate_chunks(source_text, chunk_chars=translate_chunk_chars)
    if not chunks:
        return {"success": False, "error": "empty text"}
    # Codex route disabled: Gemma 4 (256K context) handles all translation locally.
    # Keeping code for reference but skipping execution.
    # To re-enable: set MAGI_CODEX_TRANSLATE_ENABLED=1
    if os.environ.get("MAGI_CODEX_TRANSLATE_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
        try:
            from api.handlers import text_processing_handler as _tp
            from skills.bridge.llm_direct import feature_enabled as _codex_feature_enabled, translate_with_codex

            codex_max_chars = int(os.environ.get("MAGI_CODEX_TRANSLATE_MAX_CHARS", "12000") or "12000")
            if _codex_feature_enabled("translate") and len((text or "").strip()) <= max(800, codex_max_chars):
                codex_res = translate_with_codex(
                    text,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    timeout_sec=int(os.environ.get("MAGI_CODEX_TRANSLATE_TIMEOUT_SEC", "240") or "240"),
                )
                codex_text = str(codex_res.get("text") or "").strip()
                if codex_res.get("success") and codex_text and (not _tp.output_guard_issues(codex_text, mode="translation")):
                    return {
                        "success": True,
                        "text": codex_text,
                        "provider": "openclaw_codex",
                        "route": "openclaw_codex",
                        "model": codex_res.get("model", "gpt-5.4"),
                        "agent": codex_res.get("agent_id", "codex-distributed"),
                    }
                if codex_res.get("error"):
                    logger.warning("translate_text_complete: codex route failed: %s", codex_res.get("error"))
        except Exception as codex_err:
            logger.warning("translate_text_complete: codex route skipped: %s", codex_err)

    # 建立 document-level glossary（確保全文術語一致）
    doc_glossary = _build_document_glossary(source_text, target_lang)

    try:
        timeout_sec = int(os.environ.get("MAGI_FILE_TRANSLATE_TIMEOUT_SEC", "120") or "120")
    except Exception:
        timeout_sec = 120
    try:
        remote_cap = int(os.environ.get("MAGI_FILE_TRANSLATE_REMOTE_TIMEOUT_CAP_SEC", "180") or "180")
    except Exception:
        remote_cap = 180
    remote_cap = max(20, min(remote_cap, 300))
    remote_timeout = max(18, min(timeout_sec, remote_cap))
    try:
        quick_timeout = int(
            os.environ.get(
                "MAGI_FILE_TRANSLATE_QUICK_TIMEOUT_SEC",
                str(max(15, min(60, remote_timeout))),
            )
            or str(max(15, min(60, remote_timeout)))
        )
    except Exception:
        quick_timeout = max(15, min(60, remote_timeout))
    quick_timeout = max(15, min(quick_timeout, 90))
    try:
        retries = int(os.environ.get("MAGI_FILE_TRANSLATE_RETRIES", "2") or "2")
    except Exception:
        retries = 2
    retries = max(0, min(retries, 5))
    try:
        translate_workers = int(os.environ.get("MAGI_FILE_TRANSLATE_WORKERS", "1") or "1")
    except Exception:
        translate_workers = 1  # MLX Metal is NOT thread-safe — concurrent eval → SIGSEGV
    translate_workers = max(1, min(translate_workers, 4))
    # Detect if melchior_client already does local-first internally.
    _mc_local_first = False
    try:
        from skills.bridge import melchior_client as _mc_check
        _mc_local_first = bool(getattr(_mc_check, 'MELCHIOR_LOCAL_FIRST_DEFAULT', False))
    except Exception as _e:
        logging.getLogger("magi.translate").debug("melchior_client check skipped: %s", _e)
    # Only do orchestrator-level local-first if melchior_client doesn't already do it.
    prefer_local_first = (not _mc_local_first) and os.environ.get("MAGI_TRANSLATE_LOCAL_FIRST", "1").strip().lower() in {"1", "true", "yes", "on"}
    fallback_model = (
        os.environ.get("MAGI_TRANSLATE_LOCAL_MODEL")
        or os.environ.get("MAGI_TIMEOUT_FAST_MODEL")
        or TEXT_PRIMARY_MODEL
    ).strip() or TEXT_PRIMARY_MODEL

    translated = []
    failed_chunks = 0
    last_model = ""
    total = len(chunks)

    try:
        quick_sub_chars = int(os.environ.get("MAGI_FILE_TRANSLATE_FALLBACK_SUBCHARS", "900") or "900")
    except Exception:
        quick_sub_chars = 900
    quick_sub_chars = max(400, min(quick_sub_chars, 1800))
    try:
        split_retry_depth = int(os.environ.get("MAGI_FILE_TRANSLATE_SPLIT_RETRY_DEPTH", "1") or "1")
    except Exception:
        split_retry_depth = 1
    try:
        split_retry_chars = int(
            os.environ.get(
                "MAGI_FILE_TRANSLATE_SPLIT_RETRY_CHARS",
                str(max(1600, quick_sub_chars * 2)),
            )
            or str(max(1600, quick_sub_chars * 2))
        )
    except Exception:
        split_retry_chars = max(1600, quick_sub_chars * 2)
    split_retry_depth = max(0, min(split_retry_depth, 2))
    split_retry_chars = max(900, min(split_retry_chars, 3200))
    gtx_enabled = os.environ.get("MAGI_FILE_TRANSLATE_GTX_FALLBACK", "1").strip().lower() in {"1", "true", "yes", "on"}
    try:
        gtx_primary_mode = str(os.environ.get("MAGI_FILE_TRANSLATE_GTX_PRIMARY", "auto") or "auto").strip().lower()
    except Exception:
        gtx_primary_mode = "auto"
    use_gtx_primary = False
    if gtx_enabled:
        if gtx_primary_mode in {"1", "true", "yes", "on"}:
            use_gtx_primary = True
        elif gtx_primary_mode == "auto":
            use_gtx_primary = (
                (target_is_zh and doc_src_latin > max(200, doc_src_cjk * 2))
                or (target_is_en and doc_src_cjk > max(200, doc_src_latin * 2))
            )
    try:
        gtx_primary_workers = int(os.environ.get("MAGI_FILE_TRANSLATE_GTX_PRIMARY_WORKERS", "4") or "4")
    except Exception:
        gtx_primary_workers = 4
    if use_gtx_primary:
        translate_workers = max(translate_workers, max(1, min(gtx_primary_workers, 8)))
    try:
        verify_max_chunks = int(os.environ.get("MAGI_FILE_TRANSLATE_VERIFY_MAX_CHUNKS", "12") or "12")
    except Exception:
        verify_max_chunks = 12
    if use_gtx_primary:
        if total >= 40:
            verify_max_chunks = 0
        elif total >= 30:
            verify_max_chunks = min(verify_max_chunks, 4)
        elif total >= 12:
            verify_max_chunks = min(verify_max_chunks, 6)
    verify_max_chunks = max(0, min(verify_max_chunks, 64))
    try:
        gtx_segment_chars = int(os.environ.get("MAGI_FILE_TRANSLATE_GTX_SEGMENT_CHARS", "1800") or "1800")
    except Exception:
        gtx_segment_chars = 1800
    gtx_segment_chars = max(600, min(gtx_segment_chars, 2800))
    checkpoint_enabled = os.environ.get("MAGI_FILE_TRANSLATE_CHECKPOINT_ENABLE", "1").strip().lower() in {"1", "true", "yes", "on"}
    try:
        checkpoint_threshold = int(os.environ.get("MAGI_FILE_TRANSLATE_CHECKPOINT_THRESHOLD_CHUNKS", "12") or "12")
    except Exception:
        checkpoint_threshold = 12
    checkpoint_threshold = max(1, min(checkpoint_threshold, 2000))
    bilingual_table_enabled = os.environ.get("MAGI_FILE_TRANSLATE_BILINGUAL_TABLE", "1").strip().lower() in {"1", "true", "yes", "on"}
    bilingual_table_active = bilingual_table_enabled and target_is_zh and doc_src_latin > max(20, doc_src_cjk * 2)

    def _should_verify_chunk(idx: int) -> bool:
        if verify_max_chunks <= 0:
            return False
        if total <= verify_max_chunks:
            return True
        stride = max(1, math.ceil(total / verify_max_chunks))
        return idx == 1 or idx == total or ((idx - 1) % stride == 0)

    def _normalize_gtx_lang(target: str) -> str:
        t = str(target or "").strip().lower()
        if ("繁體" in str(target or "")) or ("traditional" in t) or (t in {"zh-tw", "zh_tw", "zh-hant", "tw"}):
            return "zh-TW"
        if ("簡體" in str(target or "")) or (t in {"zh-cn", "zh_cn", "zh-hans", "cn"}):
            return "zh-CN"
        if ("英文" in str(target or "")) or (t in {"en", "en-us", "english"}):
            return "en"
        if ("日文" in str(target or "")) or (t in {"ja", "jp", "japanese"}):
            return "ja"
        if re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", t):
            return t
        return "zh-TW"

    def _translate_via_gtx(text_part: str) -> str:
        if (not gtx_enabled) or (not str(text_part or "").strip()):
            return ""
        tl = _normalize_gtx_lang(target_lang)
        pieces = []
        part = str(text_part or "").strip()
        step = gtx_segment_chars
        for i in range(0, len(part), step):
            seg = part[i : i + step]
            if not seg:
                continue
            q = urllib.parse.quote(seg)
            url = (
                "https://translate.googleapis.com/translate_a/single"
                f"?client=gtx&sl=auto&tl={urllib.parse.quote(tl)}&dt=t&q={q}"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            data = json.loads(raw)
            seg_parts = []
            if isinstance(data, list) and data and isinstance(data[0], list):
                for row in data[0]:
                    if isinstance(row, list) and row and row[0]:
                        seg_parts.append(str(row[0]))
            got = "".join(seg_parts).strip()
            pieces.append(got or seg)
        return "\n\n".join(pieces).strip()

    def _translation_needs_rescue(src_part: str, translated_part: str) -> bool:
        from api.handlers import text_processing_handler as _tp
        src = str(src_part or "").strip()
        out = str(translated_part or "").strip()
        if not out:
            return True
        if "翻譯失敗" in out or "⚠️" in out or "系統降級" in out:
            return True
        if _tp.output_guard_issues(out, mode="translation"):
            return True
        src_latin = len(re.findall(r"[A-Za-z]", src))
        src_cjk = len(re.findall(r"[\u4e00-\u9fff]", src))
        out_latin = len(re.findall(r"[A-Za-z]", out))
        out_cjk = len(re.findall(r"[\u4e00-\u9fff]", out))
        long_latin_words = len(re.findall(r"\b[A-Za-z]{4,}\b", out))
        mostly_non_cjk_src = src_latin > max(80, src_cjk * 2)
        if mostly_non_cjk_src:
            # GTX legal translations may legitimately preserve party names, citations,
            # and case labels in Latin script. Treat them as acceptable once the output
            # already contains substantial Chinese text.
            if out_cjk >= 120 and out_latin <= max(220, int(out_cjk * 1.15)):
                return False
            if out_cjk < 40:
                return True
            if out_latin > max(80, int(out_cjk * 0.85)):
                return True
            if long_latin_words >= 18 and out_latin > out_cjk * 0.55:
                return True
        return False

    def _quick_translate(text_part: str, label: str = "") -> tuple[str, str]:
        glossary = doc_glossary  # 使用 document-level glossary 確保全文術語一致
        pp = (
            "你是專業法律翻譯員，擅長精確翻譯法律文件。\n\n"
            "【任務】\n"
            f"將以下內容從{source_lang}完整翻譯為{target_lang}。\n\n"
            "【翻譯規則】\n"
            "1. 逐句完整翻譯，禁止摘要、省略或增添內容\n"
            "2. 保留原文段落結構和清單格式\n"
            "3. 法律專有名詞必須精確翻譯，不確定時保留原文並加括號註記\n"
            "4. 忽略印刷殘字（ISBN、頁碼、*.indb 等）\n"
            "5. 只輸出譯文，不要任何說明或解釋\n"
            f"{glossary}\n"
            f"{label}\n\n"
            "【待翻譯內容】\n"
            f"{text_part}"
        )
        _tr_ctx = min(16384, max(6144, len(text_part) * 3))
        qq = InferenceGateway().chat(
            pp, task_type="translate", timeout=quick_timeout, model=fallback_model,
            num_ctx=_tr_ctx, num_predict=min(4096, max(1536, len(text_part) * 2)),
            allow_synthetic_fallback=False,
        )
        out = str((qq or {}).get("response") or "").strip()
        model_used = str((qq or {}).get("model") or "").strip()
        if qq.get("success") and out:
            return out, model_used
        return "", model_used

    def _split_retry_parts(text_part: str) -> list[str]:
        sub_chunk_chars = max(800, min(split_retry_chars, max(800, len(text_part) // 2)))
        parts = _dh.split_translate_chunks(text_part, chunk_chars=sub_chunk_chars)
        if len(parts) >= 2:
            return parts
        out = []
        for i in range(0, len(text_part), sub_chunk_chars):
            piece = str(text_part[i : i + sub_chunk_chars] or "").strip()
            if piece:
                out.append(piece)
        return out

    def _process_chunk(idx, part):
        def _translate_piece(text_part: str, *, label: str, depth: int) -> tuple[str, str, int]:
            glossary = doc_glossary  # 使用 document-level glossary 確保全文術語一致
            prompt = (
                "你是專業法律翻譯員，擅長精確翻譯法律文件。\n\n"
                "【任務】\n"
                f"將以下內容從{source_lang}完整翻譯為{target_lang}。\n\n"
                "【翻譯規則】\n"
                "1. 逐句完整翻譯，禁止摘要、省略或增添內容\n"
                "2. 保留原文段落結構和清單格式\n"
                "3. 法律專有名詞必須精確翻譯，不確定時保留原文並加括號註記\n"
                "4. 忽略印刷殘字（ISBN、頁碼、*.indb 等）\n"
                "5. 只輸出譯文，不要任何說明或解釋\n"
                f"{glossary}\n"
                f"{label}\n\n"
                "【待翻譯內容】\n"
                f"{text_part}"
            )

            piece = ""
            cb_open = False
            try:
                st = melchior_client.get_circuit_breaker_status() or {}
                cb_open = bool(st.get("open"))
            except Exception:
                cb_open = False

            used_model = ""

            if use_gtx_primary:
                try:
                    gtx_piece = _translate_via_gtx(text_part)
                except Exception:
                    gtx_piece = ""
                if gtx_piece and not _translation_needs_rescue(text_part, gtx_piece):
                    # GTX-first + TAIDE post-edit: 用 TAIDE 修正 GTX 的術語錯誤
                    if glossary and len(gtx_piece) > 60:
                        try:
                            _pe_prompt = (
                                "你是法律翻譯審校員。以下是機器翻譯的初稿，請只修正法律術語錯誤，不要重新翻譯。\n"
                                "規則：保持原文結構，只替換錯誤的專有名詞。若無需修改直接輸出原文。\n"
                                f"{glossary}\n\n"
                                f"【機器翻譯初稿】\n{gtx_piece[:2000]}"
                            )
                            _pe_r = melchior_client._chat_omlx(
                                prompt=_pe_prompt, model=TEXT_REVIEW_MODEL,
                                timeout=45, temperature=0.1, max_tokens=min(1024, len(gtx_piece)),
                            )
                            _pe_out = str((_pe_r or {}).get("response") or "").strip()
                            if _pe_r.get("success") and _pe_out and len(_pe_out) > len(gtx_piece) * 0.5:
                                piece = _pe_out
                                used_model = "google_gtx+taide_postedit"
                            else:
                                piece = gtx_piece
                                used_model = "google_gtx"
                        except Exception:
                            piece = gtx_piece
                            used_model = "google_gtx"
                    else:
                        piece = gtx_piece
                        used_model = "google_gtx"

            if (not piece) and prefer_local_first:
                q_text, q_model = _quick_translate(text_part, label=label)
                if q_text:
                    piece = q_text
                    used_model = q_model or used_model

            if (not piece) and (not cb_open):
                _gw = InferenceGateway()
                for _ in range(retries + 1):
                    r = _gw.chat(
                        prompt,
                        task_type="translate",
                        timeout=remote_timeout,
                        allow_synthetic_fallback=False,
                    )
                    if r.get("success") and str(r.get("response") or "").strip():
                        piece = str(r.get("response") or "").strip()
                        used_model = str(r.get("model") or "")
                        break

            if (not piece) and (not prefer_local_first):
                q_text, q_model = _quick_translate(text_part, label=label)
                if q_text:
                    piece = q_text
                    used_model = q_model or used_model

            if piece and _translation_needs_rescue(text_part, piece):
                try:
                    gtx_piece = _translate_via_gtx(text_part)
                except Exception:
                    gtx_piece = ""
                if gtx_piece and not _translation_needs_rescue(text_part, gtx_piece):
                    piece = gtx_piece
                    used_model = "google_gtx"

            if piece and not _translation_needs_rescue(text_part, piece) and _should_verify_chunk(idx):
                try:
                    _verify_prompt = (
                        "你是翻譯品質審查員。請判斷以下翻譯是否忠實傳達原文意思。\n"
                        "只回答「正確」或「有問題：（簡述）」，不要其他內容。\n\n"
                        f"原文（前300字）：{text_part[:300]}\n\n"
                        f"譯文（前300字）：{piece[:300]}"
                    )
                    _vr = InferenceGateway().chat(
                        _verify_prompt, task_type="tc_review", timeout=12, model=TEXT_REVIEW_MODEL,
                        num_ctx=2048, num_predict=100,
                        allow_synthetic_fallback=False,
                    )
                    _vr_out = str((_vr or {}).get("response") or "").strip()
                    if _vr.get("success") and "有問題" in _vr_out:
                        logger.warning("translate_verify: taide-12b flagged chunk %d/%d: %s", idx, total, _vr_out[:120])
                        try:
                            gtx_rescue = _translate_via_gtx(text_part)
                        except Exception:
                            gtx_rescue = ""
                        if gtx_rescue and not _translation_needs_rescue(text_part, gtx_rescue):
                            piece = gtx_rescue
                            used_model = "google_gtx_verified"
                except Exception as _e:
                    logging.getLogger("magi.translate").debug("GTX rescue failed: %s", _e)

            if (not piece) and len(text_part) > quick_sub_chars:
                sub_parts = []
                j = 0
                while j < len(text_part):
                    sub_parts.append(text_part[j : j + quick_sub_chars])
                    j += quick_sub_chars

                sub_out = []
                sub_failed = 0
                for sidx, sp in enumerate(sub_parts, start=1):
                    st, sm = _quick_translate(sp, label=f"子段：{sidx}/{len(sub_parts)}（{label}）")
                    if st:
                        sub_out.append(st)
                        if sm:
                            used_model = sm
                    else:
                        gtx_piece = ""
                        try:
                            gtx_piece = _translate_via_gtx(sp)
                        except Exception:
                            gtx_piece = ""
                        if gtx_piece:
                            sub_out.append(gtx_piece)
                            used_model = used_model or "google_gtx"
                        else:
                            sub_failed += 1

                if sub_out and sub_failed < len(sub_parts):
                    piece = "\n\n".join(sub_out).strip()

            if not piece:
                try:
                    gtx_piece = _translate_via_gtx(text_part)
                except Exception:
                    gtx_piece = ""
                if gtx_piece:
                    piece = gtx_piece
                    used_model = used_model or "google_gtx"

            if (not piece) and depth > 0 and len(text_part) >= split_retry_chars:
                sub_parts = _split_retry_parts(text_part)
                if len(sub_parts) >= 2:
                    sub_out = []
                    sub_failed = 0
                    for sidx, sp in enumerate(sub_parts, start=1):
                        sub_piece, sub_model, sub_errs = _translate_piece(
                            sp,
                            label=f"子段：{sidx}/{len(sub_parts)}（{label}）",
                            depth=depth - 1,
                        )
                        if sub_piece:
                            sub_out.append(sub_piece)
                        if sub_model and not used_model:
                            used_model = sub_model
                        sub_failed += sub_errs
                    if sub_out and sub_failed < len(sub_parts):
                        return "\n\n".join(sub_out).strip(), used_model, sub_failed

            failed = 0
            if not piece:
                failed = 1
                piece = f"（⚠️ 第 {idx}/{total} 段翻譯失敗，先保留原文）\n{text_part}"

            return piece, used_model, failed

        return _translate_piece(part, label=f"目前段落：{idx}/{total}", depth=split_retry_depth)

    try:
        translate_idle_timeout = int(
            os.environ.get(
                "MAGI_FILE_TRANSLATE_IDLE_TIMEOUT_SEC",
                str(max(90, min(600, max(remote_timeout, quick_timeout) + 30))),
            )
            or str(max(90, min(600, max(remote_timeout, quick_timeout) + 30)))
        )
    except Exception:
        translate_idle_timeout = max(90, min(600, max(remote_timeout, quick_timeout) + 30))

    from concurrent.futures import FIRST_COMPLETED, wait
    from api.thread_pools import inference_pool
    checkpoint_version = 3
    checkpoint_active = checkpoint_enabled and total >= checkpoint_threshold
    checkpoint_path = _translation_checkpoint_state_path(text, source_lang, target_lang) if checkpoint_active else None
    result_buffer = [None] * total

    def _rebuild_translated_text_from_cached_results(cached_results) -> str:
        if not isinstance(cached_results, list):
            return ""
        rebuilt_parts = []
        for result in cached_results:
            if not isinstance(result, dict):
                continue
            cached_text = str(result.get("text") or "").strip()
            if not cached_text or bool(result.get("timed_out")) or int(result.get("failed") or 0) > 0:
                continue
            rebuilt_parts.append(_dh.polish_translated_document_text(cached_text) or cached_text)
        rebuilt = "\n\n".join(rebuilt_parts).strip()
        if not rebuilt:
            return ""
        return _dh.polish_translated_document_text(rebuilt) or rebuilt

    if checkpoint_path is not None:
        cached = _read_json(checkpoint_path)
        if isinstance(cached, dict):
            cached_version = int(cached.get("version") or 0)
            cached_total = int(cached.get("chunks_total") or 0)
            cached_source = str(cached.get("source_lang") or "")
            cached_target = str(cached.get("target_lang") or "")
            cached_results = cached.get("results") or []
            if cached_version in {2, checkpoint_version} and cached_source == str(source_lang or "auto") and cached_target == str(target_lang or ""):
                cached_final = str(cached.get("final_text") or "").strip()
                cached_translated = str(cached.get("translated_text") or "").strip()
                if not cached_translated and isinstance(cached_results, list) and cached_results:
                    cached_translated = _rebuild_translated_text_from_cached_results(cached_results)
                cache_has_plain_translation = bool(cached_translated)
                if bool(cached.get("complete")) and cached_final and ((not bilingual_table_active) or cache_has_plain_translation):
                    return {
                        "success": True,
                        "text": cached_final,
                        "translated_text": cached_translated or cached_final,
                        "provider": "melchior_chunk_complete",
                        "model": str(cached.get("model") or ""),
                        "chunks_total": int(cached.get("chunks_total") or total or 0),
                        "chunks_failed": int(cached.get("chunks_failed") or 0),
                    }
                if isinstance(cached_results, list) and len(cached_results) == total:
                    for idx, result in enumerate(cached_results):
                        if not isinstance(result, dict):
                            continue
                        cached_text = str(result.get("text") or "").strip()
                        cached_failed = int(result.get("failed") or 0)
                        if cached_text and cached_failed == 0 and not bool(result.get("timed_out")):
                            result_buffer[idx] = {
                                "text": cached_text,
                                "model": str(result.get("model") or ""),
                                "failed": 0,
                                "timed_out": False,
                            }

    def _persist_checkpoint(
        *,
        final_text: str = "",
        translated_text: str = "",
        complete: bool = False,
        chunks_failed: Optional[int] = None,
        model: str = "",
    ) -> None:
        if checkpoint_path is None:
            return
        serializable = []
        for result in result_buffer:
            if isinstance(result, dict):
                serializable.append(
                    {
                        "text": str(result.get("text") or ""),
                        "model": str(result.get("model") or ""),
                        "failed": int(result.get("failed") or 0),
                        "timed_out": bool(result.get("timed_out")),
                    }
                )
            else:
                serializable.append(None)
        payload = {
            "version": checkpoint_version,
            "source_lang": str(source_lang or "auto"),
            "target_lang": str(target_lang or ""),
            "chunks_total": total,
            "chunks_failed": int(chunks_failed or 0),
            "model": str(model or ""),
            "complete": bool(complete),
            "updated_at": time.time(),
            "results": serializable,
        }
        if final_text:
            payload["final_text"] = str(final_text)
        if translated_text:
            payload["translated_text"] = str(translated_text)
        _atomic_write_json(checkpoint_path, payload)

    pending_indices = [i for i, result in enumerate(result_buffer) if not isinstance(result, dict)]

    try:
        fut_map = {inference_pool.submit(_process_chunk, i + 1, chunks[i]): i for i in pending_indices}
        pending = set(fut_map.keys())
        last_progress = time.monotonic()
        while pending:
            done, pending = wait(
                pending,
                timeout=min(5, max(1, translate_idle_timeout)),
                return_when=FIRST_COMPLETED,
            )
            if not done:
                if time.monotonic() - last_progress >= translate_idle_timeout:
                    logger.warning(
                        "translate_text_complete: idle timeout reached with %d pending chunks",
                        len(pending),
                    )
                    break
                continue
            last_progress = time.monotonic()
            for f in done:
                i = fut_map[f]
                try:
                    p, m, errs = f.result(timeout=1)
                    result_buffer[i] = {"text": p, "model": m, "failed": errs}
                except Exception as e:
                    result_buffer[i] = {
                        "text": f"（⚠️ 第 {i+1}/{total} 段處理發生系統錯誤：{e}）",
                        "model": "",
                        "failed": 1,
                        "timed_out": False,
                    }
                _persist_checkpoint(model=last_model)
        for f in pending:
            i = fut_map[f]
            f.cancel()
            if not isinstance(result_buffer[i], dict):
                # 逾時 chunk 嘗試 Google fallback 兜底，避免直接保留原文
                _gtx_fallback = ""
                try:
                    _gtx_fallback = _translate_via_gtx(chunks[i])
                except Exception:
                    pass
                if _gtx_fallback and len(_gtx_fallback.strip()) > 10:
                    result_buffer[i] = {
                        "text": _gtx_fallback,
                        "model": "google_gtx_timeout_fallback",
                        "failed": 0,
                        "timed_out": True,
                    }
                    logger.info("translate_text_complete: chunk %d/%d timed out, Google fallback OK", i + 1, total)
                else:
                    result_buffer[i] = {
                        "text": f"（⚠️ 第 {i+1}/{total} 段翻譯逾時，先保留原文）\n{chunks[i]}",
                        "model": "",
                        "failed": 1,
                        "timed_out": True,
                    }
        _persist_checkpoint(model=last_model)
    finally:
        pass  # shared inference_pool — do not shut down

    failed_indices = [
        i for i, result in enumerate(result_buffer)
        if ((not isinstance(result, dict)) or int(result.get("failed") or 0) > 0)
        and not (isinstance(result, dict) and bool(result.get("timed_out")))
    ]
    if failed_indices:
        logger.info("translate_text_complete: retrying %d failed chunks sequentially", len(failed_indices))
        for i in failed_indices:
            try:
                p, m, errs = _process_chunk(i + 1, chunks[i])
            except Exception as e:
                logger.warning("translate_text_complete: retry chunk %d/%d failed: %s", i + 1, total, e)
                continue
            prev = result_buffer[i] if isinstance(result_buffer[i], dict) else {}
            prev_failed = int(prev.get("failed") or 1)
            if errs < prev_failed:
                result_buffer[i] = {"text": p, "model": m or prev.get("model") or "", "failed": errs}
                _persist_checkpoint(model=str(result_buffer[i].get("model") or ""))

    translated = []
    translated_chunks = []
    failed_chunks = 0
    for i, result in enumerate(result_buffer):
        if not isinstance(result, dict):
            translated_chunks.append("")
            continue
        text_part = str(result.get("text") or "").strip()
        if text_part:
            polished_part = _dh.polish_translated_document_text(text_part) or text_part
            translated.append(polished_part)
            translated_chunks.append(polished_part)
        else:
            # Keep chunk aligned even on failure (use source as placeholder)
            translated_chunks.append(chunks[i] if i < len(chunks) else "")
        if result.get("model"):
            last_model = str(result.get("model") or "")
        failed_chunks += int(result.get("failed") or 0)
    raw_joined = "\n\n".join(translated).strip()
    final_translation_text = _dh.polish_translated_document_text(raw_joined) or raw_joined
    from api.handlers import text_processing_handler as _tp
    final_issues = _tp.output_guard_issues(final_translation_text, mode="translation")
    if final_issues:
        _persist_checkpoint(
            final_text=final_translation_text,
            translated_text=final_translation_text,
            complete=False,
            chunks_failed=failed_chunks,
            model=last_model,
        )
        return {
            "success": False,
            "error": "translation_off_topic:" + ",".join(sorted(set(final_issues))),
            "provider": "melchior_chunk_complete",
            "model": last_model,
            "chunks_total": total,
            "chunks_failed": failed_chunks,
        }

    final_text = final_translation_text
    if bilingual_table_active:
        final_text = _dh.build_bilingual_translation_table(
            chunks,
            translated_chunks,
            left_header="原文",
            right_header="中文",
        ) or final_translation_text

    _persist_checkpoint(
        final_text=final_text,
        translated_text=final_translation_text,
        complete=(failed_chunks == 0),
        chunks_failed=failed_chunks,
        model=last_model,
    )

    return {
        "success": True,
        "text": final_text,
        "translated_text": final_translation_text,
        "source_chunks": list(chunks),
        "translated_chunks": list(translated_chunks),
        "provider": "melchior_chunk_complete",
        "model": last_model,
        "chunks_total": total,
        "chunks_failed": failed_chunks,
    }
