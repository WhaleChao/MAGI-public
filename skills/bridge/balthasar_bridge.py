import requests
import logging
import os
import sys
import glob
import json
import re
import shutil
import subprocess
import tempfile
from typing import List

from api.model_config import SUMMARY_MODEL, TEXT_REVIEW_MODEL
from skills.bridge.http_pool import get_session as _get_session

# Configuration
try:
    from api.routing.node_registry import get_node_ip as _get_node_ip
    BALTHASAR_HOST = os.environ.get("BALTHASAR_HOST") or _get_node_ip("balthasar") or "100.118.235.126"
except Exception:
    BALTHASAR_HOST = os.environ.get("BALTHASAR_HOST", "100.118.235.126")
BALTHASAR_PORT = os.environ.get("BALTHASAR_PORT", "5002")
BALTHASAR_URL = f"http://{BALTHASAR_HOST}:{BALTHASAR_PORT}"
_fallback_hosts_raw = os.environ.get("BALTHASAR_FALLBACK_HOSTS", "")
BALTHASAR_FALLBACK_HOSTS = [h.strip() for h in _fallback_hosts_raw.split(",") if h.strip()]
BALTHASAR_REMOTE_ENABLED = os.environ.get("BALTHASAR_REMOTE_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
BALTHASAR_REMOTE_TIMEOUT_SEC = int(os.environ.get("BALTHASAR_REMOTE_TIMEOUT_SEC", "6"))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("BalthasarBridge")


def _format_hhmmss(seconds: float) -> str:
    try:
        total = int(max(0.0, float(seconds)))
    except Exception:
        total = 0
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _normalize_time_scale_if_needed(rows: List[dict]) -> List[dict]:
    """
    Heuristic guard for models that return timestamps in milliseconds.
    """
    if not rows:
        return rows
    try:
        threshold = float(os.environ.get("MAGI_TRANSCRIBE_MS_THRESHOLD", "20000") or "20000")
    except Exception:
        threshold = 20000.0
    max_end = max(float(r.get("end", 0.0) or 0.0) for r in rows)
    if max_end >= max(10000.0, threshold):
        out = []
        for r in rows:
            rr = dict(r)
            rr["start"] = max(0.0, float(rr.get("start", 0.0) or 0.0) / 1000.0)
            rr["end"] = max(rr["start"], float(rr.get("end", rr["start"]) or rr["start"]) / 1000.0)
            out.append(rr)
        return out
    return rows


def _normalize_segments(raw) -> List[dict]:
    out: List[dict] = []
    if not isinstance(raw, list):
        return out
    for seg in raw:
        if not isinstance(seg, dict):
            continue
        txt = str(seg.get("text") or "").strip()
        if not txt:
            continue
        try:
            st = float(seg.get("start", 0.0) or 0.0)
        except Exception:
            st = 0.0
        try:
            ed = float(seg.get("end", st) or st)
        except Exception:
            ed = st
        row = {
            "start": max(0.0, st),
            "end": max(st, ed),
            "text": txt,
        }
        speaker = str(seg.get("speaker") or "").strip()
        if speaker:
            row["speaker"] = speaker
        out.append(row)
    out = _normalize_time_scale_if_needed(out)
    try:
        out.sort(key=lambda x: float(x.get("start", 0.0) or 0.0))
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 91, exc_info=True)
    return out


def _segments_to_timestamp_text(segments: List[dict]) -> str:
    if not segments:
        return ""
    show_end = os.environ.get("MAGI_TRANSCRIBE_TIMESTAMP_SHOW_END", "1").strip().lower() in {"1", "true", "yes", "on"}
    lines = []
    for seg in segments:
        st = float(seg.get("start", 0.0) or 0.0)
        ed = float(seg.get("end", st) or st)
        if show_end and ed > st + 0.35:
            lines.append(f"[{_format_hhmmss(st)}-{_format_hhmmss(ed)}] {seg.get('text', '')}")
        else:
            lines.append(f"[{_format_hhmmss(st)}] {seg.get('text', '')}")
    return "\n".join(lines).strip()


def _segments_to_speaker_text(segments: List[dict]) -> str:
    if not segments:
        return ""
    lines = []
    for seg in segments:
        spk = str(seg.get("speaker") or "").strip()
        if spk:
            lines.append(f"[{_format_hhmmss(seg.get('start', 0.0))}] {spk}: {seg.get('text', '')}")
        else:
            lines.append(f"[{_format_hhmmss(seg.get('start', 0.0))}] {seg.get('text', '')}")
    return "\n".join(lines).strip()


def _infer_speaker_from_text(text: str) -> str:
    s = str(text or "").strip()
    if not s:
        return ""
    m = re.search(r"\bSpeaker\s*([A-Z0-9]+)\b", s, re.IGNORECASE)
    if m:
        return f"SPEAKER_{m.group(1).upper()}"
    m2 = re.match(r"^\s*([甲乙丙丁])\s*[：:]", s)
    if m2:
        return f"SPEAKER_{m2.group(1)}"
    if any(k in s for k in ("原告", "被告", "法官", "書記官", "檢察官")):
        if "原告" in s:
            return "SPEAKER_原告"
        if "被告" in s:
            return "SPEAKER_被告"
        if "法官" in s:
            return "SPEAKER_法官"
        if "書記官" in s:
            return "SPEAKER_書記官"
        if "檢察官" in s:
            return "SPEAKER_檢察官"
    return ""


def _annotate_speakers(segments: List[dict]) -> List[dict]:
    if not segments:
        return segments
    auto_toggle = os.environ.get("MAGI_TRANSCRIBE_AUTO_SPEAKER", "1").strip().lower() in {"1", "true", "yes", "on"}
    out: List[dict] = []
    prev_label = ""
    for i, seg in enumerate(segments):
        row = dict(seg)
        if str(row.get("speaker") or "").strip():
            label = str(row.get("speaker")).strip()
        else:
            label = _infer_speaker_from_text(row.get("text", ""))
        if not label and auto_toggle:
            if i == 0:
                label = "SPEAKER_1"
            else:
                prev = segments[i - 1]
                gap = float(row.get("start", 0.0) or 0.0) - float(prev.get("end", prev.get("start", 0.0)) or 0.0)
                if gap >= 1.0 and prev_label in {"SPEAKER_1", "SPEAKER_2"}:
                    label = "SPEAKER_2" if prev_label == "SPEAKER_1" else "SPEAKER_1"
                elif prev_label:
                    label = prev_label
                else:
                    label = "SPEAKER_1"
        if label:
            row["speaker"] = label
            prev_label = label
        out.append(row)
    return out


def _split_text_units(text: str) -> List[str]:
    s = str(text or "").strip()
    if not s:
        return []
    raw = re.split(r"(?:\r?\n+|(?<=[。！？!?；;\.]))", s)
    out: List[str] = []
    for part in raw:
        t = str(part or "").strip()
        if t:
            out.append(t)
    return out


def _secondary_split_segments(segments: List[dict], full_text: str = "") -> List[dict]:
    """
    Second-pass segmentation when first pass fails to separate speakers.
    """
    enabled = os.environ.get("MAGI_TRANSCRIBE_SECONDARY_SPLIT", "1").strip().lower() in {"1", "true", "yes", "on"}
    if (not enabled) or (not segments):
        return segments

    def _speaker_count(rows: List[dict]) -> int:
        return len({str(x.get("speaker") or "").strip() for x in rows if str(x.get("speaker") or "").strip()})

    if _speaker_count(segments) >= 2:
        return segments

    min_chars = int(os.environ.get("MAGI_TRANSCRIBE_SECONDARY_SPLIT_MIN_CHARS", "22") or "22")
    min_duration = float(os.environ.get("MAGI_TRANSCRIBE_SECONDARY_SPLIT_MIN_DURATION_SEC", "2.5") or "2.5")
    forced_dual = os.environ.get("MAGI_TRANSCRIBE_FORCE_DUAL_SPLIT", "1").strip().lower() in {"1", "true", "yes", "on"}

    changed = False
    out: List[dict] = []
    for seg in segments:
        row = dict(seg)
        txt = str(row.get("text") or "").strip()
        st = float(row.get("start", 0.0) or 0.0)
        ed = float(row.get("end", st) or st)
        dur = max(0.0, ed - st)
        units = _split_text_units(txt)
        if len(units) >= 2 and len(txt) >= min_chars and dur >= min_duration:
            changed = True
            total_chars = max(1, sum(len(u) for u in units))
            cur = st
            for i, u in enumerate(units):
                if i == len(units) - 1:
                    nxt = ed
                else:
                    frac = max(0.05, float(len(u)) / float(total_chars))
                    nxt = min(ed, cur + max(0.15, dur * frac))
                out.append({"start": cur, "end": max(cur, nxt), "text": u})
                cur = max(cur, nxt)
        else:
            out.append(row)

    if not changed:
        return segments

    out = _annotate_speakers(out)
    if _speaker_count(out) < 2 and forced_dual and len(out) >= 2:
        for i, row in enumerate(out):
            row["speaker"] = "SPEAKER_1" if i % 2 == 0 else "SPEAKER_2"
        if _speaker_count(out) < 2:
            # Last resort: split whole text into two halves and force 2 speakers.
            body = " ".join([str(x.get("text") or "").strip() for x in out]).strip() or str(full_text or "").strip()
            if body:
                mid = max(1, len(body) // 2)
                a = body[:mid].strip()
                b = body[mid:].strip()
                if a and b:
                    base_st = float(out[0].get("start", 0.0) or 0.0)
                    base_ed = float(out[-1].get("end", base_st + 2.0) or (base_st + 2.0))
                    cut = base_st + max(0.5, (base_ed - base_st) / 2.0)
                    out = [
                        {"start": base_st, "end": cut, "text": a, "speaker": "SPEAKER_1"},
                        {"start": cut, "end": base_ed, "text": b, "speaker": "SPEAKER_2"},
                    ]
    return out


def _summary_postprocess(text: str) -> str:
    s = str(text or "").strip()
    if not s:
        return ""
    # Normalize spaces/newlines first.
    s = re.sub(r"[ \t]+", " ", s).strip()
    s = re.sub(r"\n{3,}", "\n\n", s)
    # Sentence-level dedupe to reduce repeated loops from small local models.
    parts = re.split(r"(?<=[。！？!?；;])", s)
    seen = set()
    dedup = []
    for p in parts:
        t = p.strip()
        if not t:
            continue
        key = re.sub(r"\s+", "", t).lower()
        if len(key) > 4 and key in seen:
            continue
        seen.add(key)
        dedup.append(t)
    if not dedup:
        dedup = [s]
    out = " ".join(dedup).strip()
    max_chars = int(os.environ.get("MAGI_SUMMARY_MAX_CHARS", "1200") or "1200")
    if len(out) > max_chars:
        out = out[: max_chars - 1].rstrip() + "…"
    return out


def _candidate_urls() -> List[str]:
    hosts = [BALTHASAR_HOST] + BALTHASAR_FALLBACK_HOSTS
    urls = []
    seen = set()
    for host in hosts:
        key = (host or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        urls.append(f"http://{host}:{BALTHASAR_PORT}")
    return urls

def check_health(force_remote: bool = False):
    """
    Checks if remote Balthasar node is online.
    Normal mode: Balthasar is council-only, so remote checks are disabled unless force_remote=True.
    """
    if (not force_remote) and (not BALTHASAR_REMOTE_ENABLED):
        return False, "Council-only (remote disabled)"
    last_error = "unknown error"
    for base in _candidate_urls():
        try:
            response = _get_session().get(f"{base}/health", timeout=5)
            if response.status_code == 200:
                return True, f"Online ({base})"
            last_error = f"{base} status {response.status_code}"
        except Exception as e:
            last_error = f"{base} {e}"
    return False, last_error

def call_apple_ai(endpoint, payload):
    """Generic caller for Apple AI endpoints."""
    if not BALTHASAR_REMOTE_ENABLED:
        return {"success": False, "error": "Balthasar remote disabled (council-only)"}
    logger.info(f"🍏 Calling Balthasar: {endpoint}")
    errors = []
    for base in _candidate_urls():
        url = f"{base}/apple/{endpoint}"
        try:
            response = _get_session().post(url, json=payload, timeout=max(3, BALTHASAR_REMOTE_TIMEOUT_SEC))
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict):
                    data.setdefault("provider", "balthasar")
                    data.setdefault("endpoint", base)
                return data
            msg = f"{base} HTTP {response.status_code}: {response.text}"
            logger.warning(f"⚠️ Balthasar Error ({endpoint}): {msg}")
            errors.append(msg)
        except Exception as e:
            msg = f"{base} connection error: {e}"
            logger.warning(f"⚠️ {msg}")
            errors.append(msg)
    return {"success": False, "error": " | ".join(errors)[:1000]}

def _tc_review_pass(text: str, timeout: int = 30) -> str:
    """
    Run taide-12b 繁體中文校正 on text (fix simplified Chinese → 正體).
    Returns corrected text, or original if review fails.
    """
    if not text or len(text.strip()) < 10:
        return text
    try:
        prompt = (
            "請將以下文字中的簡體中文用語轉換為台灣正體中文（繁體），"
            "包括法律專有名詞（如「信息」→「資訊」、「軟件」→「軟體」、「數據」→「資料」）。"
            "只修改用語，不改變原文內容和結構。直接輸出修改後的文字，不要加任何說明。\n\n"
            + text
        )
        # Primary: oMLX TAIDE-12b MLX for 繁體校正
        _chat_omlx = getattr(melchior_client, "_chat_omlx", None)
        _omlx_avail = getattr(melchior_client, "_omlx_available", None)
        _taide_model = getattr(melchior_client, "OMLX_TAIDE_MODEL", TEXT_REVIEW_MODEL)
        if callable(_chat_omlx) and callable(_omlx_avail) and _omlx_avail():
            r = _chat_omlx(
                prompt=prompt,
                model=_taide_model,
                timeout=max(15, timeout),
                temperature=0.2,
                max_tokens=min(2048, max(512, len(text))),
            )
        else:
            # Fallback via InferenceGateway
            from skills.bridge.inference_gateway import InferenceGateway
            _gw = InferenceGateway()
            r = _gw.chat(prompt, task_type="tc_review", timeout=max(15, timeout), model=TEXT_REVIEW_MODEL)
        if r.get("success") and r.get("response") and len(r["response"].strip()) > len(text) * 0.5:
            return r["response"].strip()
    except Exception as e:
        logger.warning("tc_review_pass failed: %s", e)
    return text


def summarize_text(text, timeout_sec=None, summary_length="medium"):
    """
    Local-first summary using oMLX/TAIDE-12b (primary):
    - Fast path: oMLX/TAIDE-12b (直接繁中輸出)
    - Quality path: taide-12b via melchior_client.chat (Ollama fallback)
    - Fallback: truncated text if all models fail
    - summary_length: "short" (3-5 pts), "medium" (4-8 pts), "long" (12-15 pts)
    """
    content = (text or "").strip()
    if not content:
        return {"success": False, "error": "missing text"}
    try:
        from skills.bridge.llm_direct import feature_enabled as _codex_feature_enabled, summarize_with_codex

        codex_max_chars = int(os.environ.get("MAGI_CODEX_SUMMARY_MAX_CHARS", "12000") or "12000")
        if _codex_feature_enabled("summary") and len(content) <= max(1200, codex_max_chars):
            codex_res = summarize_with_codex(
                content,
                summary_length=summary_length,
                timeout_sec=int(os.environ.get("MAGI_CODEX_SUMMARY_TIMEOUT_SEC", "240") or "240"),
            )
            codex_text = _summary_postprocess(str(codex_res.get("text") or ""))
            if codex_res.get("success") and codex_text:
                return {
                    "success": True,
                    "text": codex_text,
                    "provider": "openclaw_codex",
                    "model": codex_res.get("model", "gpt-5.4"),
                    "agent": codex_res.get("agent_id", "codex-distributed"),
                    "error": "",
                }
            if codex_res.get("error"):
                logger.warning("summarize_text: codex route failed: %s", codex_res.get("error"))
    except Exception as codex_err:
        logger.warning("summarize_text: codex route skipped: %s", codex_err)

    _length_prompts = {
        "short": "最多 5 點，每點一句話",
        "medium": "保留決策與行動重點，5-8 點，每點 1-2 句話（包含關鍵事實）",
        "long": "保留決策與行動重點、關鍵數字與法條，12-15 點，每點 2-3 句話（先寫結論，再補充背景或數據）",
    }
    length_instruction = _length_prompts.get(summary_length, _length_prompts["medium"])

    # --- 文件類型偵測 ---
    def _detect_doc_type(text: str) -> str:
        t = (text or "")[:3000]
        import re as _re
        if _re.search(r"主\s*文|理\s*由|判\s*決|裁\s*定|JUDGMENT|OPINION", t):
            return "judgment"
        if _re.search(r"契約|合約|協議書|甲方|乙方|CONTRACT|AGREEMENT", t):
            return "contract"
        if _re.search(r"摘要|abstract|關鍵字|keywords|壹、前言|緒論|introduction", t, _re.IGNORECASE):
            return "academic"
        if _re.search(r"專案報告|調查報告|建議書|白皮書|REPORT", t, _re.IGNORECASE):
            return "report"
        return "general"

    _doc_type = _detect_doc_type(content)

    _type_prompts = {
        "judgment": (
            "【文件類型：法院裁判書】\n"
            "請依以下結構整理：\n"
            "1. 案由與案號\n"
            "2. 當事人（原告/被告/聲請人）\n"
            "3. 主文（判決結論）\n"
            "4. 爭點（雙方主張）\n"
            "5. 法院見解與理由\n"
            "6. 適用法條\n"
        ),
        "contract": (
            "【文件類型：契約/協議】\n"
            "請依以下結構整理：\n"
            "1. 當事人（甲方/乙方）\n"
            "2. 契約標的\n"
            "3. 主要條件與義務\n"
            "4. 期限與終止條款\n"
            "5. 違約責任\n"
            "6. 特約條款\n"
        ),
        "academic": (
            "【文件類型：學術論文/研究報告】\n"
            "請依以下結構整理：\n"
            "1. 研究主題與目的\n"
            "2. 研究方法\n"
            "3. 主要發現\n"
            "4. 結論與建議\n"
        ),
        "report": (
            "【文件類型：專案/調查報告】\n"
            "請依以下結構整理：\n"
            "1. 報告主題與背景\n"
            "2. 調查範圍與方法\n"
            "3. 核心發現\n"
            "4. 問題分析\n"
            "5. 結論與建議\n"
        ),
        "general": (
            "【一般文件】\n"
            "若為法院文書，依序整理：案由→主文→事實→理由→結論\n"
            "若為契約/協議，依序整理：當事人→標的→條件→期限→違約責任\n"
        ),
    }
    type_instruction = _type_prompts.get(_doc_type, _type_prompts["general"])

    # 1) Casper-provided summarization (prefer Melchior agent /api/chat for responsiveness)
    try:
        from skills.bridge import melchior_client

        prompt = (
            "你是專業法律文件分析師。請仔細閱讀以下文件，用繁體中文產出結構化重點摘要。\n\n"
            f"{type_instruction}\n"
            "【輸出規則】\n"
            f"1. {length_instruction}\n"
            "2. 每點以「- 」開頭，一點一個完整概念\n"
            "3. 必須保留：當事人姓名、日期、金額、法條編號、案號\n"
            "4. 禁止：重複內容、開場白、結尾語、「以上是摘要」等廢話\n"
            "5. 直接輸出重點，不要複述以下格式說明\n\n"
            f"【文件內容】\n{content}"
        )
        timeout = int(timeout_sec or os.environ.get("MAGI_SUMMARIZE_TIMEOUT_SEC", "120") or "120")
        timeout = max(20, min(timeout, 240))
        # Determine ctx/predict based on content length for proper model utilisation.
        _summ_num_ctx = 4096
        _summ_num_predict = 0  # 0 = use default in quick_local_chat
        content_len = len(content)
        if content_len > 2000:
            _summ_num_ctx = min(32768, max(8192, content_len * 2))
            _summ_num_predict = 2048
        elif content_len > 800:
            _summ_num_ctx = 8192
            _summ_num_predict = 1024

        # Primary: oMLX/TAIDE-12b — 繁體中文原生輸出，免 TC review 省一輪推理。
        _omlx_chat = getattr(melchior_client, "_chat_omlx", None)
        _omlx_avail = getattr(melchior_client, "_omlx_available", None)
        if callable(_omlx_chat) and callable(_omlx_avail) and _omlx_avail():
            _omlx_model = os.environ.get("MAGI_OMLX_SUMMARY_MODEL", SUMMARY_MODEL)
            q = _omlx_chat(
                prompt=prompt,
                model=_omlx_model,
                timeout=timeout,
                temperature=0.2,
                max_tokens=min(4096, max(2048, _summ_num_predict)),
            )
            if q.get("success") and q.get("response"):
                raw_summary = q.get("response", "")
                # TAIDE 直接輸出繁體中文，無需 _tc_review_pass
                clean = _summary_postprocess(raw_summary)
                return {
                    "success": True,
                    "text": clean,
                    "provider": "omlx",
                    "model": _omlx_model,
                    "tc_reviewed": False,
                    "error": "",
                }
            logger.warning("summarize_text: oMLX (%s) failed: %s", _omlx_model, q.get("error", "unknown"))

        # Fallback: InferenceGateway (handles oMLX→Ollama→remote routing)
        from skills.bridge.inference_gateway import InferenceGateway
        _gw = InferenceGateway()
        fb = _gw.chat(prompt, task_type="summary", timeout=max(30, timeout), allow_synthetic_fallback=False)
        if fb.get("success") and fb.get("response"):
            clean = _summary_postprocess(fb.get("response", ""))
            return {
                "success": True,
                "text": clean,
                "provider": fb.get("route", "gateway_fallback"),
                "model": fb.get("model", ""),
                "error": "",
            }
        logger.warning("summarize_text: gateway fallback failed: %s", fb.get("error", "unknown"))
    except Exception as exc:
        logger.warning("summarize_text: melchior path exception: %s", exc)

    # 2) Optional: remote Balthasar (council-only)
    result = call_apple_ai("summarize", {"text": content})
    if isinstance(result, dict) and result.get("success", True):
        return result
    # 3) Last-resort local fallback — structured extraction instead of blind truncation.
    import re as _re
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    # Grab title-like lines (short, near top)
    title_lines = [l for l in lines[:20] if 6 < len(l) < 120 and not _re.fullmatch(r"[\d\W_]+", l)][:3]
    # Grab first substantive paragraphs
    body_lines = [l for l in lines if len(l) > 40]
    first_paras = body_lines[:5]
    # Sample from middle and end
    mid_idx = len(body_lines) // 2
    mid_sample = body_lines[max(0, mid_idx - 1): mid_idx + 2] if len(body_lines) > 10 else []
    tail_sample = body_lines[-3:] if len(body_lines) > 6 else []
    parts = []
    if title_lines:
        parts.append("【可能標題】")
        parts.extend(f"  {t}" for t in title_lines)
    parts.append("\n【文件節錄】")
    seen = set()
    for chunk in first_paras + mid_sample + tail_sample:
        norm = chunk[:60]
        if norm in seen:
            continue
        seen.add(norm)
        parts.append(f"- {chunk[:260]}")
        if len(parts) > 14:
            break
    fallback_text = "\n".join(parts).strip()
    if len(fallback_text) < 60:
        # absolute last resort
        compact = " ".join(content.replace("\n", " ").split())
        fallback_text = compact[:800] + ("…" if len(compact) > 800 else "")
    return {
        "success": True,
        "text": _summary_postprocess(f"（降級摘要）\n{fallback_text}"),
        "provider": "local_fallback",
        "error": (result.get("error") if isinstance(result, dict) else "summarize failed"),
    }

def ocr_request(image_url):
    """
    Calls Balthasar to OCR an image URL.
    """
    return call_apple_ai("ocr", {"image_url": image_url})


def _resolve_whisper_bin() -> str:
    """
    Resolve whisper executable in daemon-friendly way.
    """
    env_bin = (os.environ.get("MAGI_WHISPER_BIN") or "").strip()
    candidates = [
        env_bin,
        shutil.which("whisper") or "",
        "/opt/homebrew/bin/whisper",
        "/usr/local/bin/whisper",
    ]
    for c in candidates:
        p = (c or "").strip()
        if p and os.path.exists(p) and os.access(p, os.X_OK):
            return p
    return ""


def _transcribe_with_whisper_cli(audio_path: str, language: str | None = None, initial_prompt: str | None = None) -> dict:
    """
    Fallback transcription using OpenAI Whisper CLI.
    """
    whisper_bin = _resolve_whisper_bin()
    if not whisper_bin:
        return {"success": False, "error": "whisper_cli_not_found"}

    model = (os.environ.get("MAGI_WHISPER_MODEL") or "medium").strip() or "medium"
    timeout_sec = int(os.environ.get("MAGI_WHISPER_TIMEOUT_SEC", "900") or "900")
    timeout_sec = max(30, min(timeout_sec, 3600))
    forced_language = (language or os.environ.get("MAGI_WHISPER_LANGUAGE") or "").strip()
    word_timestamps = os.environ.get("MAGI_WHISPER_WORD_TIMESTAMPS", "1").strip().lower() in {"1", "true", "yes", "on"}

    # launchd/service PATH often misses Homebrew dirs.
    run_env = os.environ.copy()
    run_env["PATH"] = (
        run_env.get("PATH", "")
        + os.pathsep
        + "/opt/homebrew/bin"
        + os.pathsep
        + "/usr/local/bin"
    )

    with tempfile.TemporaryDirectory(prefix="magi_whisper_") as outdir:
        stem = os.path.splitext(os.path.basename(audio_path))[0]
        cmd = [
            whisper_bin,
            audio_path,
            "--model",
            model,
            "--output_format",
            "json",
            "--output_dir",
            outdir,
        ]
        if forced_language:
            cmd += ["--language", forced_language]
        if initial_prompt:
            cmd += ["--initial_prompt", initial_prompt.strip()]
        if word_timestamps:
            cmd += ["--word_timestamps", "True"]
        try:
            cp = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                env=run_env,
            )
        except Exception as e:
            return {"success": False, "error": f"whisper_cli_exec_failed: {e}"}

        json_path = os.path.join(outdir, f"{stem}.json")
        if not os.path.exists(json_path):
            # Whisper sometimes normalizes output filename.
            matches = sorted(glob.glob(os.path.join(outdir, "*.json")))
            if matches:
                json_path = matches[0]

        if not os.path.exists(json_path):
            err_tail = (cp.stderr or cp.stdout or "").strip()[-400:]
            if cp.returncode != 0 and err_tail:
                return {"success": False, "error": f"whisper_cli_failed: {err_tail}"}
            return {"success": False, "error": "whisper_cli_empty_output"}

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                obj = json.load(f) or {}
        except Exception as e:
            return {"success": False, "error": f"whisper_cli_read_failed: {e}"}

        text = str(obj.get("text") or "").strip()
        segments = _annotate_speakers(_normalize_segments(obj.get("segments")))
        segments = _secondary_split_segments(segments, full_text=text)
        timestamp_text = _segments_to_timestamp_text(segments)
        speaker_text = _segments_to_speaker_text(segments)
        speaker_count = len({str(x.get("speaker") or "").strip() for x in segments if str(x.get("speaker") or "").strip()})
        if not text:
            return {"success": False, "error": "whisper_cli_empty_text"}

        return {
            "success": True,
            "text": text,
            "segments": segments,
            "timestamp_text": timestamp_text,
            "speaker_text": speaker_text,
            "speaker_count_estimate": speaker_count,
            "provider": "openai_whisper_cli",
            "model": model,
            "route": "whisper_cli_fallback",
        }

def transcribe(audio_path, language: str | None = None, initial_prompt: str | None = None, taigi_hint: bool = False):
    """
    Transcribes audio.
    If running on Casper (Mac), uses local mlx-whisper.
    Otherwise, calls Balthasar remote endpoint (if implemented).
    """
    if not os.path.exists(audio_path):
        return {"success": False, "error": f"Audio file not found: {audio_path}"}

    local_error = ""
    cli_error = ""

    # 1) Prefer local mlx-whisper when available.
    try:
        from skills.hearing import balthasar_local
        logger.info(f"👂 Using Local Balthasar (Casper) for {audio_path}")
        local_res = balthasar_local.transcribe_audio(
            audio_path,
            language=language,
            initial_prompt=initial_prompt,
            taigi_hint=taigi_hint,
        )
        if isinstance(local_res, dict) and local_res.get("success") and (local_res.get("text") or "").strip():
            local_res.setdefault("provider", "balthasar_local_mlx")
            segs = _annotate_speakers(_normalize_segments(local_res.get("segments")))
            segs = _secondary_split_segments(segs, full_text=str(local_res.get("text") or ""))
            if segs and (not local_res.get("timestamp_text")):
                local_res["segments"] = segs
                local_res["timestamp_text"] = _segments_to_timestamp_text(segs)
            if segs and (not local_res.get("speaker_text")):
                local_res["speaker_text"] = _segments_to_speaker_text(segs)
            if segs and ("speaker_count_estimate" not in local_res):
                local_res["speaker_count_estimate"] = len({str(x.get("speaker") or "").strip() for x in segs if str(x.get("speaker") or "").strip()})
            return local_res
        local_error = (local_res or {}).get("error", "local_mlx_failed")
    except Exception as e:
        local_error = str(e)
        logger.warning("⚠️ Local MLX Whisper unavailable (%s), falling back to CLI/remote", local_error)

    # 2) Fallback to Whisper CLI so channel daemons keep working without mlx_whisper.
    try:
        cli_res = _transcribe_with_whisper_cli(audio_path, language=language, initial_prompt=initial_prompt)
        if cli_res.get("success"):
            return cli_res
        cli_error = cli_res.get("error", "whisper_cli_failed")
    except Exception as e:
        cli_error = str(e)

    # 3) Optional remote (council-only usually disabled).
    if BALTHASAR_REMOTE_ENABLED:
        remote = call_apple_ai("transcribe", {"audio_path": audio_path})
        if isinstance(remote, dict) and remote.get("success"):
            remote.setdefault("provider", "balthasar_remote")
            return remote

    merged = " | ".join([x for x in [local_error, cli_error] if x])[:1000]
    return {"success": False, "error": merged or "transcription_failed"}

if __name__ == "__main__":
    # Test CLI
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "health":
            status, msg = check_health()
            print(f"Balthasar Status: {msg}")
        elif cmd == "summary":
            txt = sys.argv[2] if len(sys.argv) > 2 else "Hello World"
            print(summarize_text(txt))
    else:
        print("Usage: python balthasar_bridge.py [health|summary <text>]")


def sync_skills(zip_path, force_remote: bool = False):
    """
    Uploads a ZIP file of skills to Balthasar.
    Endpoint: POST /api/skills/sync
    """
    if not os.path.exists(zip_path):
        return {"success": False, "error": "ZIP file not found"}

    if (not force_remote) and (not BALTHASAR_REMOTE_ENABLED):
        return {"success": False, "error": "Balthasar remote disabled (council-only)"}

    url = f"{BALTHASAR_URL}/api/skills/sync"
    logger.info(f"📤 Syncing skills to Balthasar: {zip_path}...")

    try:
        with open(zip_path, 'rb') as f:
            files = {'file': (os.path.basename(zip_path), f, 'application/zip')}
            response = _get_session().post(url, files=files, timeout=300)

        if response.status_code == 200:
            logger.info("✅ Skills Synced to Balthasar Successfully.")
            return response.json()
        else:
            logger.error(f"❌ Skill Sync Failed: {response.text}")
            # Identify purely as info for now since Balthasar might not be ready
            return {"success": False, "error": f"HTTP {response.status_code}"}

    except Exception as e:
        logger.error(f"❌ Skill Sync Error: {e}")
        return {"success": False, "error": str(e)}
