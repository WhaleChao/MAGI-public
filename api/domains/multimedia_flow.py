"""
Multimedia routing (image / audio / file) extracted from Orchestrator.

All functions accept `orch` (the Orchestrator instance) instead of `self`.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import sys
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Optional

from api.model_config import TEXT_PRIMARY_MODEL, VISION_MODEL as _VISION_MODEL
from api.thread_pools import io_pool, inference_pool
from skills.bridge.melchior_bridge import analyze_image

logger = logging.getLogger("Orchestrator")


# ---------------------------------------------------------------------------
# Vision-based image classification and routing
# ---------------------------------------------------------------------------

def vision_classify_and_route_image(orch, user_id, image_path: str, prompt: Optional[str]) -> Optional[str]:
    """
    Use Gemma 4 multimodal to classify the image content.
    If it looks like a payment receipt, route to payment upload automatically.
    Returns a response string if handled, or None to fall through to default.
    """
    try:
        if not os.path.isfile(image_path):
            return None

        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()

        classify_prompt = (
            "請判斷這張圖片的類型，只回答一個類別：\n"
            "A) 法院繳費收據/繳費憑證/繳費截圖\n"
            "B) 法律文件（判決書、裁定、起訴狀等）\n"
            "C) 其他圖片\n"
            "只回答 A、B 或 C，不要其他文字。"
        )

        from skills.bridge.http_pool import get_session as _get_session
        _vision_base = os.environ.get(
            "MAGI_OMLX_VISION_URL",
            os.environ.get("MAGI_OMLX_CHAT_URL", "http://127.0.0.1:8080"),
        )

        payload = {
            "model": os.environ.get("MAGI_OMLX_VISION_MODEL", _VISION_MODEL),
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": classify_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                ],
            }],
            "max_tokens": 20,
            "temperature": 0.1,
            "stream": False,
        }

        resp = _get_session().post(
            f"{_vision_base.rstrip('/')}/v1/chat/completions",
            json=payload, timeout=30,
        )
        if resp.status_code != 200:
            logger.debug(f"Vision classify returned {resp.status_code}, falling through")
            return None

        choices = resp.json().get("choices") or []
        answer = (choices[0].get("message", {}).get("content", "") if choices else "").strip().upper()
        logger.info(f"🔍 Vision classify result: '{answer}' for {image_path}")

        if answer.strip().upper() == "A":
            logger.info(f"💰 Payment proof detected via vision classification: {image_path}")
            try:
                return handle_payment_proof_from_channel(orch, image_path)
            except Exception as pay_err:
                logger.error(f"Payment proof upload (vision-routed) failed: {pay_err}")
                return f"❌ 繳費憑證上傳失敗：{str(pay_err)[:200]}"

        return None

    except Exception as e:
        logger.debug(f"Vision classify failed: {e}, falling through to default")
        return None


# ---------------------------------------------------------------------------
# Payment proof upload from channel images (LINE/DC/TG)
# ---------------------------------------------------------------------------

def handle_payment_proof_from_channel(orch, image_path: str) -> str:
    """
    接收從 LINE/Discord/Telegram 傳來的繳費截圖，
    自動解析案號並上傳至 OLA。
    """
    action_script = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "skills",
        "file-review-orchestrator", "action.py",
    ))
    if not os.path.exists(action_script):
        return "❌ 找不到閱卷模組 action.py"

    py = os.environ.get("MAGI_SKILL_PYTHON", "").strip()
    if not py or not os.path.exists(py):
        py = sys.executable or "python3"

    cmd_json = json.dumps({"cmd": "upload_payment_proof_from_image", "image_path": image_path})
    logger.info("💰 Calling action.py for payment proof: %s", image_path)

    try:
        def _run_payment_subprocess():
            return subprocess.run(
                [py, action_script, "--json-cmd"],
                input=cmd_json,
                capture_output=True,
                text=True,
                timeout=180,
                cwd=os.path.dirname(action_script),
            )
        proc = io_pool.submit(_run_payment_subprocess).result(timeout=190)
    except (subprocess.TimeoutExpired, FuturesTimeoutError):
        return "❌ 繳費憑證上傳逾時（超過 3 分鐘）"

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    try:
        result = json.loads(stdout)
        return result.get("message") or str(result)
    except Exception:
        logger.warning("Payment subprocess returned non-JSON stdout: %s", stdout[:200], exc_info=True)

    if stdout:
        return stdout
    if proc.returncode != 0:
        err = stderr[:200] if stderr else f"exit code {proc.returncode}"
        return f"❌ 繳費憑證上傳失敗：{err}"
    return "⚠️ 繳費憑證上傳完成但無回傳結果"


# ---------------------------------------------------------------------------
# Main multimedia router
# ---------------------------------------------------------------------------

def handle_multimedia(orch, user_id, prompt, attachment) -> str:
    """Routes file attachments to appropriate skills."""
    msg_type = attachment['type']
    path = attachment['path']

    if msg_type == "image":
        prompt_lower = (prompt or "").lower()
        _payment_kw = ["繳費", "繳款", "繳費憑證", "繳費單", "繳費截圖", "payment proof",
                       "上傳繳費", "銷帳", "入帳", "收據", "裁判費", "上傳閱卷",
                       "上傳收據", "費用憑證"]
        if any(kw in prompt_lower for kw in _payment_kw):
            logger.info(f"💰 Payment proof detected via keyword in prompt: {path}")
            try:
                return handle_payment_proof_from_channel(orch, path)
            except Exception as pay_err:
                logger.error(f"Payment proof upload from channel failed: {pay_err}")
                return f"❌ 繳費憑證上傳失敗：{str(pay_err)[:200]}"

        _vision_routed = vision_classify_and_route_image(orch, user_id, path, prompt)
        if _vision_routed is not None:
            return _vision_routed

        logger.info(f"👁️ Routing Image to Melchior: {path}")
        description = analyze_image(path, prompt=prompt)
        return f"👁️ Melchior: {description}"

    elif msg_type == "audio":
        logger.info(f"🎙️ Routing Audio to unified transcription pipeline: {path}")
        _transcribe_task_id = f"transcribe_{id(path)}_{time.time():.0f}"
        orch.register_heavy_task(_transcribe_task_id, "逐字稿")
        try:
            prompt_lower = (prompt or "").lower()
            wants_translate = any(k in prompt_lower for k in ["translate", "翻譯", "翻成"])
            wants_summary = any(k in prompt_lower for k in ["summary", "摘要", "重點"])
            no_summary = any(k in prompt_lower for k in ["不要摘要", "不用摘要", "不需要摘要", "不要總結", "不用總結", "不需要總結"])
            if no_summary:
                wants_summary = False
            summary_length = orch._detect_summary_length(prompt or "")
            summary_pref = orch._detect_summary_target_pref(prompt_lower)
            disable_txt = any(k in prompt_lower for k in ["不要txt", "不需要txt", "no txt", "no file"])
            disable_timestamps = any(k in prompt_lower for k in ["不要時間戳", "不要時間碼", "no timestamp", "without timestamp", "純文字"])
            wants_txt = not disable_txt
            wants_timestamps = not disable_timestamps
            taigi_hint = any(k in prompt_lower for k in ["台語", "臺語", "閩南語", "hokkien", "taigi", "tai-gi"])
            force_non_zh = any(k in prompt_lower for k in [" english", "英文", "en-us", "en-uk", "日文", "japanese", "日本語"])
            has_cjk_prompt = bool(re.search(r"[\u4e00-\u9fff]", prompt or ""))
            language_hint = None if force_non_zh else ("zh" if (taigi_hint or has_cjk_prompt or not prompt_lower.strip()) else None)
            initial_prompt_hint = ""
            if language_hint == "zh":
                initial_prompt_hint = (
                    "這段音訊可能包含華語與臺灣口語，請盡量以繁體中文準確轉寫，必要時保留台語詞彙。"
                    "常見用語：原告、被告、聲請人、相對人、法院、法官、檢察官、律師、"
                    "委任狀、起訴狀、答辯狀、準備書狀、調解、和解、判決、裁定、"
                    "民事、刑事、行政訴訟、強制執行、假扣押、假處分、"
                    "勞動基準法、民法、刑法、公司法、著作權法、"
                    "當事人、證人、鑑定人、書記官、庭期、開庭、筆錄、"
                    "損害賠償、違約金、利息、遲延利息、訴訟費用。"
                )
            if taigi_hint:
                initial_prompt_hint = (
                    "這段音訊可能包含台語（臺灣閩南語）與華語，請盡量以繁體中文準確轉寫。"
                    "常見用語：原告、被告、法院、律師、判決、調解、和解。"
                )

            from skills.bridge.balthasar_bridge import transcribe as transcribe_audio
            tr = transcribe_audio(
                path,
                language=language_hint,
                initial_prompt=initial_prompt_hint or None,
                taigi_hint=taigi_hint,
            )
            transcript = str((tr or {}).get("text") or "").strip()
            if not transcript:
                err = str((tr or {}).get("error") or "transcription_failed").strip()[:300]
                logger.warning(f"Audio transcription failed: {err}")
                return "⚠️ 語音已接收，但目前無法完成轉錄。請稍後再試，或在訊息加上「台語」再重試。"
            force_txt = (
                "full translation without summary" in prompt_lower
                or "完整翻譯不摘要" in prompt_lower
                or wants_txt
            )

            segments = tr.get("segments") if isinstance(tr, dict) else []
            timestamp_text = str((tr or {}).get("timestamp_text") or "").strip()
            if (not timestamp_text) and isinstance(segments, list) and segments:
                def _normalize_ts_sec(v: float) -> float:
                    try:
                        x = float(v)
                    except Exception:
                        return 0.0
                    if x >= 20000.0:
                        x = x / 1000.0
                    return max(0.0, x)

                def _fmt_hhmmss(sec: float) -> str:
                    try:
                        total = int(_normalize_ts_sec(sec))
                    except Exception:
                        total = 0
                    hh = total // 3600
                    mm = (total % 3600) // 60
                    ss = total % 60
                    return f"{hh:02d}:{mm:02d}:{ss:02d}"
                lines = []
                for seg in segments:
                    if not isinstance(seg, dict):
                        continue
                    st = _normalize_ts_sec(seg.get("start", 0.0))
                    txt = str(seg.get("text") or "").strip()
                    if txt:
                        lines.append(f"[{_fmt_hhmmss(st)}] {txt}")
                timestamp_text = "\n".join(lines).strip()

            if language_hint == "zh" and len(transcript) > 30:
                try:
                    from skills.bridge import melchior_client as _pp_mc
                    _pp_prompt = (
                        "你是中文標點修正工具。請修正以下逐字稿的標點符號與斷句，"
                        "只修標點和段落分隔，不要更改任何用詞或內容。"
                        "直接輸出修正後的全文，不要加任何說明。\n\n"
                        f"{transcript}"
                    )
                    _pp_ctx = min(16384, max(4096, len(transcript) * 2))
                    _pp = _pp_mc.quick_local_chat(
                        _pp_prompt, timeout=30, model_hint=TEXT_PRIMARY_MODEL,
                        num_ctx=_pp_ctx, num_predict=min(4096, max(1024, len(transcript) + 200)),
                    )
                    if _pp.get("success") and _pp.get("response"):
                        _pp_out = str(_pp["response"]).strip()
                        if 0.7 < len(_pp_out) / max(1, len(transcript)) < 1.4:
                            transcript = _pp_out
                            logger.info("Transcript punctuation corrected by taide-12b")
                except Exception as _pp_err:
                    logger.debug("Transcript punctuation correction skipped: %s", _pp_err)

            if len(transcript) > 30:
                try:
                    from skills.bridge.openclaw_codex_bridge import feature_enabled as _codex_feature_enabled, polish_transcript_with_codex

                    codex_max_chars = int(os.environ.get("MAGI_CODEX_TRANSCRIPT_MAX_CHARS", "14000") or "14000")
                    if _codex_feature_enabled("transcript") and len(transcript) <= max(1200, codex_max_chars):
                        codex_res = polish_transcript_with_codex(
                            transcript,
                            timeout_sec=int(os.environ.get("MAGI_CODEX_TRANSCRIPT_TIMEOUT_SEC", "240") or "240"),
                        )
                        codex_text = str(codex_res.get("text") or "").strip()
                        if codex_res.get("success") and codex_text:
                            ratio = len(codex_text) / max(1, len(transcript))
                            if 0.7 < ratio < 1.6:
                                transcript = codex_text
                                logger.info("Transcript polished by Codex")
                        elif codex_res.get("error"):
                            logger.warning("Transcript Codex polish failed: %s", codex_res.get("error"))
                except Exception as codex_err:
                    logger.debug("Transcript Codex polish skipped: %s", codex_err)

            final_text = transcript
            title = "🎙️ 語音逐字稿"

            _audio_can_parallel = wants_translate and wants_summary and summary_pref != "translated"

            if _audio_can_parallel:
                _tr_future = inference_pool.submit(
                    orch._translate_text_complete,
                    transcript,
                    source_lang="auto",
                    target_lang="繁體中文",
                )
                _sm_future = inference_pool.submit(
                    orch._summarize_text_resilient,
                    transcript,
                    summary_length=summary_length,
                    progress_callback=getattr(orch, "_progress_callback", None),
                )

                try:
                    rr = _tr_future.result(timeout=300)
                    if isinstance(rr, dict) and rr.get("success"):
                        t = str(rr.get("text") or "").strip()
                        if t:
                            final_text = t
                            title = "🌐 語音翻譯結果"
                except Exception as translate_err:
                    logger.warning(f"Audio translation skipped due to error: {translate_err}")

                summary_text = ""
                summary_source_label = "逐字稿原文"
                try:
                    summary_res = _sm_future.result(timeout=300)
                    if isinstance(summary_res, dict) and summary_res.get("success"):
                        summary_text = str(summary_res.get("text") or summary_res.get("summary") or "").strip()
                except Exception as summarize_err:
                    logger.warning(f"Audio summary fallback due to error: {summarize_err}")
            else:
                if wants_translate:
                    try:
                        rr = orch._translate_text_complete(
                            transcript,
                            source_lang="auto",
                            target_lang="繁體中文",
                        )
                        if isinstance(rr, dict) and rr.get("success"):
                            t = str(rr.get("text") or "").strip()
                            if t:
                                final_text = t
                                title = "🌐 語音翻譯結果"
                    except Exception as translate_err:
                        logger.warning(f"Audio translation skipped due to error: {translate_err}")

                summary_text = ""
                summary_source_label = ""
                if wants_summary:
                    try:
                        summary_target_text = final_text
                        if summary_pref == "source":
                            summary_target_text = transcript
                            summary_source_label = "逐字稿原文"
                        elif summary_pref == "translated":
                            summary_target_text = final_text
                            summary_source_label = "翻譯結果" if wants_translate else "逐字稿原文"
                        else:
                            summary_source_label = "翻譯結果" if wants_translate else "逐字稿原文"
                        summary_res = orch._summarize_text_resilient(
                            summary_target_text,
                            summary_length=summary_length,
                            progress_callback=getattr(orch, "_progress_callback", None),
                        )
                        if summary_res.get("success"):
                            summary_text = str(summary_res.get("text") or summary_res.get("summary") or "").strip()
                    except Exception as summarize_err:
                        logger.warning(f"Audio summary fallback due to error: {summarize_err}")

            export_text = final_text
            if wants_timestamps and timestamp_text:
                export_text = f"【時間戳記】\n{timestamp_text}\n\n【全文】\n{final_text}".strip()

            if force_txt or len(export_text) > 2500:
                try:
                    from skills.ops.export_text import export_txt
                    exported = export_txt(export_text, prefix="audio_transcription")
                    if exported.get("success"):
                        path_out = str(exported.get("path") or "").strip()
                        url_out = str(exported.get("url") or "").strip()
                        head = "📄 已輸出逐字稿 TXT 檔案。"
                        if wants_timestamps:
                            head = "📄 已輸出含時間戳記的逐字稿 TXT 檔案。"
                        if url_out:
                            head = f"{head}\n{url_out}"
                        if summary_text:
                            head = f"📝 語音重點摘要（來源：{summary_source_label}）：\n{summary_text}\n\n{head}"
                        if orch._is_file_protocol_user(str(user_id or "")) and path_out:
                            return f"{head}|||FILE_PATH|||{path_out}"
                        return f"{head}\n{path_out}".strip()
                except Exception as e:
                    logger.error(f"TXT Export error in orchestrator audio: {e}")
            if summary_text:
                return f"📝 語音重點摘要（來源：{summary_source_label}）：\n{summary_text}\n\n{title}：\n{final_text[:1200]}"

            if wants_timestamps and timestamp_text:
                preview_lines = timestamp_text.splitlines()
                preview = "\n".join(preview_lines[:24]).strip()
                if len(preview_lines) > 24:
                    preview += "\n…（其餘內容可加上「請給我TXT」取得完整檔案）"
                return f"{title}（含時間戳記）：\n{preview}"

            return f"{title}：\n{final_text}"
        except Exception as e:
            logger.error(f"❌ Audio routing error: {e}")
            return "❌ 語音處理失敗：音訊模組執行異常（已記錄）。請稍後再試。"
        finally:
            orch.unregister_heavy_task(_transcribe_task_id)

    elif msg_type == "file":
        filename = attachment.get('filename', '')
        logger.info(f"📄 Routing File: {filename}")
        prompt_lower = (prompt or "").lower()
        wants_translate = any(k in prompt_lower for k in ["翻譯", "translate", "翻成"])
        wants_summary = any(k in prompt_lower for k in ["摘要", "總結", "重點", "summary", "summarize"])
        no_summary = any(k in prompt_lower for k in ["不要摘要", "不用摘要", "不需要摘要", "不要總結", "不用總結", "不需要總結"])
        if no_summary:
            wants_summary = False
        summary_length = orch._detect_summary_length(prompt or "")
        summary_pref = orch._detect_summary_target_pref(prompt_lower)
        disable_txt = any(k in prompt_lower for k in ["不要txt", "不需要txt", "no txt", "inline", "直接貼上"])
        explicit_txt = any(k in prompt_lower for k in ["txt", "文字檔", "檔案", "download", "下載"])
        try:
            summary_txt_default = os.environ.get("MAGI_FILE_SUMMARY_EXPORT_TXT_DEFAULT", "1").strip().lower() in {"1", "true", "yes", "on"}
        except Exception:
            summary_txt_default = True
        summary_force_txt = (not disable_txt) and (explicit_txt or summary_txt_default)

        if wants_translate:
            extracted = orch._extract_text_from_uploaded_file(path, filename=filename)
            if not extracted.get("success"):
                return (
                    f"📄 檔案 `{filename or os.path.basename(path)}` 已接收，但目前無法做全文翻譯：{extracted.get('error')}\n"
                    "已支援：PDF、EPUB、TXT、MD、LOG、CSV、JSON、DOCX。"
                )

            src_text = orch._prepare_document_text_for_llm(str(extracted.get("text") or ""))
            src_text, was_capped = orch._cap_translation_source_text(src_text)
            if not src_text:
                return "⚠️ 檔案內容為空，無法翻譯。"

            try:
                auto_ingest = os.environ.get("MAGI_DOC_AUTO_INGEST", "1").strip().lower() in {"1", "true", "yes", "on"}
            except Exception:
                auto_ingest = True
            ingest_queued = False
            if auto_ingest:
                ingest_queued = orch._ingest_uploaded_text_async(
                    kind=str(extracted.get("kind") or "file"),
                    primary=path,
                    title=str(extracted.get("title") or filename or os.path.basename(path)),
                    text=src_text,
                )

            _can_parallel_summary = wants_summary and summary_pref != "translated"

            if _can_parallel_summary:
                _translate_future = inference_pool.submit(
                    orch._translate_text_complete, src_text,
                    source_lang="auto", target_lang="繁體中文",
                )
                _summary_future = inference_pool.submit(
                    orch._summarize_text_resilient, src_text,
                    summary_length, progress_callback=getattr(orch, '_progress_callback', None),
                )
                try:
                    rr = _translate_future.result(timeout=300)
                except Exception as e:
                    rr = {"success": False, "error": str(e)}
                try:
                    sr = _summary_future.result(timeout=300)
                except Exception as e:
                    sr = {"success": False, "error": str(e)}
            else:
                try:
                    rr = orch._translate_text_complete(src_text, source_lang="auto", target_lang="繁體中文")
                except Exception as e:
                    rr = {"success": False, "error": str(e)}
                sr = None

            if not rr.get("success"):
                err = str(rr.get("error") or "translate_failed").strip()[:260]
                if err.startswith("translation_off_topic:"):
                    err = "偵測到翻譯結果偏題，已中止回傳以避免送出錯誤內容"
                base = f"❌ 檔案翻譯失敗：{err}"
                if ingest_queued:
                    base += "\n🧠 文件內容已排入背景吸收。"
                return base

            _plain_translated = str(rr.get("translated_text") or rr.get("text") or "").strip()
            translated_text = orch._polish_translated_document_text(_plain_translated)
            if not translated_text:
                return "⚠️ 檔案翻譯結果為空。請稍後再試。"
            _src_chunks = rr.get("source_chunks") or []
            _tgt_chunks = rr.get("translated_chunks") or []
            summary_text = ""
            summary_note = ""
            summary_source_label = "翻譯結果"
            if wants_summary:
                if _can_parallel_summary:
                    summary_source_label = "原文"
                    if sr.get("success"):
                        summary_text = str(sr.get("text") or "").strip()
                    else:
                        summary_note = f"⚠️ 摘要產生失敗：{str(sr.get('error') or 'summary_failed')[:120]}"
                else:
                    summary_target_text = translated_text
                    summary_source_label = "翻譯結果"
                    sr = orch._summarize_text_resilient(summary_target_text, summary_length=summary_length, progress_callback=getattr(orch, '_progress_callback', None))
                    if sr.get("success"):
                        summary_text = str(sr.get("text") or "").strip()
                    else:
                        summary_note = f"⚠️ 摘要產生失敗：{str(sr.get('error') or 'summary_failed')[:120]}"

            ingest_note = ""
            if ingest_queued:
                ingest_note = "🧠 文件內容已排入背景吸收。"
            fail_cnt = int(rr.get("chunks_failed") or 0)
            fail_note = ""
            if fail_cnt > 0:
                fail_note = f"⚠️ 有 {fail_cnt} 個段落翻譯失敗，已先保留原文，稍後可針對該段重跑。"
            export_body = translated_text
            if summary_text:
                _sl_label = {"short": "精簡", "long": "詳細"}.get(summary_length, "")
                _sl_tag = f"{_sl_label}摘要" if _sl_label else "摘要"
                export_body = f"【{_sl_tag}（來源：{summary_source_label}）】\n{summary_text}\n\n【全文翻譯】\n{translated_text}".strip()

            if not disable_txt:
                exported_reply = orch._export_translation_docx(
                    source_text=src_text,
                    translated_text=translated_text,
                    source_chunks=_src_chunks,
                    translated_chunks=_tgt_chunks,
                    title=(filename or os.path.basename(path)),
                    prefix="file_translate",
                    user_id=str(user_id or ""),
                )
                if not exported_reply:
                    exported_reply = orch._export_translation_txt(
                        translated_text=export_body,
                        source=(filename or os.path.basename(path)),
                        provider=str(rr.get("provider") or "tri-sage"),
                        mode="file_translate_with_summary" if wants_summary else "file_full_translation",
                        prefix="file_translate",
                        user_id=str(user_id or ""),
                    )
                if exported_reply:
                    extra_notes = "\n".join([n for n in [summary_note, fail_note, ingest_note] if n]).strip()
                    if "|||FILE_PATH|||" in exported_reply:
                        if extra_notes:
                            head, tail = exported_reply.split("|||FILE_PATH|||", 1)
                            return f"{head}\n{extra_notes}|||FILE_PATH|||{tail}"
                        return exported_reply
                    if extra_notes:
                        return f"{exported_reply}\n{extra_notes}"
                    return exported_reply

            prefix = "🌐 檔案翻譯結果：\n"
            if was_capped:
                prefix = "🌐 檔案翻譯結果（內容過長，已截斷後翻譯）：\n"
            out = prefix + export_body
            if summary_note:
                out += f"\n\n{summary_note}"
            if fail_note:
                out += f"\n\n{fail_note}"
            if ingest_note:
                out += f"\n\n{ingest_note}"
            return out

        # PDF Processing
        if filename.lower().endswith('.pdf'):
            logger.info(f"📄 Processing PDF: {path}")
            from skills.documents.pdf_bridge import summarize_pdf
            out = str(
                summarize_pdf(
                    path,
                    progress_callback=getattr(orch, '_progress_callback', None),
                    summary_length=summary_length,
                )
                or ""
            ).strip()
            if summary_force_txt and out:
                exported_reply = orch._export_summary_docx_or_txt(
                    out, prefix="pdf_summary", title=(filename or "PDF 摘要"),
                    user_id=str(user_id or ""), source_path=path,
                )
                if exported_reply:
                    return exported_reply
            return out

        elif filename.lower().endswith('.epub'):
            logger.info(f"📚 Processing EPUB: {path}")
            from skills.documents.epub_bridge import summarize_epub
            out = str(summarize_epub(path) or "").strip()
            if summary_force_txt and out:
                exported_reply = orch._export_summary_docx_or_txt(
                    out, prefix="epub_summary", title=(filename or "EPUB 摘要"),
                    user_id=str(user_id or ""), source_path=path,
                )
                if exported_reply:
                    return exported_reply
            return out

        elif any(filename.lower().endswith(ext) for ext in [".txt", ".md", ".log", ".csv", ".json", ".docx"]):
            from skills.documents.file_bridge import summarize_file
            out = str(summarize_file(path, filename=filename) or "").strip()
            if summary_force_txt and out:
                exported_reply = orch._export_summary_docx_or_txt(
                    out, prefix="doc_summary", title=(filename or "檔案摘要"),
                    user_id=str(user_id or ""), source_path=path,
                )
                if exported_reply:
                    return exported_reply
            return out

        else:
            return (
                f"📄 檔案 '{filename}' 已接收，但目前不支援此格式摘要。\n"
                "已支援：PDF、EPUB、TXT、MD、LOG、CSV、JSON、DOCX。"
            )

    return (
        "⚠️ 不支援此附件類型。\n"
        "目前支援的格式：PDF、EPUB、TXT、MD、LOG、CSV、JSON、DOCX。\n"
        "圖片（PNG/JPG）請直接傳送，不要以檔案方式上傳。"
    )
