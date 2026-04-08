import base64
import json
import logging
import os
import re
from typing import Callable, Dict, Optional

import requests

logger = logging.getLogger("pdf-namer-vision")

# ── oMLX vision helper ──────────────────────────────────────────────────
_omlx_chat = None  # lazy-loaded


def _get_omlx_chat():
    """Lazy-import melchior_client oMLX functions to avoid circular imports."""
    global _omlx_chat
    if _omlx_chat is not None:
        return _omlx_chat
    try:
        from skills.bridge import melchior_client as _mc
        _omlx_chat = {
            "chat": getattr(_mc, "_chat_omlx", None),
            "avail": getattr(_mc, "_omlx_available", None),
            "vision_avail": getattr(_mc, "_omlx_vision_available", None),
            "vision_model": getattr(_mc, "OMLX_VISION_MODEL", None) or os.environ.get("MAGI_OMLX_VISION_MODEL", ""),
            "ocr_model": getattr(_mc, "OMLX_OCR_MODEL", None) or os.environ.get("MAGI_OMLX_OCR_MODEL", ""),
            "vision_base": getattr(_mc, "OMLX_VISION_BASE", None) or os.environ.get("MAGI_OMLX_VISION_URL", "http://127.0.0.1:8082"),
            "vision_circuit": getattr(_mc, "_OMLX_VISION_CIRCUIT", None),
            "vision_lock": getattr(_mc, "_OMLX_VISION_LOCK", None),
        }
    except Exception:
        _omlx_chat = {}
    return _omlx_chat


def _ask_omlx_vision(
    prompt: str,
    b64: str,
    timeout_sec: int,
    expect_json: bool = False,
    validate_fn=None,
    prefer_ocr: bool = False,
) -> Optional[str]:
    """Try oMLX vision (GLM-OCR on port 8082) before Ollama."""
    ctx = _get_omlx_chat()
    chat_fn = ctx.get("chat")
    avail_fn = ctx.get("avail")
    if not callable(chat_fn) or not callable(avail_fn) or not avail_fn():
        return None
    model = ctx.get("ocr_model") if prefer_ocr else ctx.get("vision_model")
    # Route to vision server (port 8082) for GLM-OCR
    vision_base = ctx.get("vision_base", "")
    vision_circuit = ctx.get("vision_circuit")
    vision_lock = ctx.get("vision_lock")
    try:
        r = chat_fn(
            prompt=prompt,
            model=model,
            timeout=max(8, int(timeout_sec)),
            temperature=0.0,
            max_tokens=2048,
            images=[b64],
            base_url=vision_base,
            circuit=vision_circuit,
            lock=vision_lock,
        )
        if not r.get("success") or not r.get("response"):
            return None
        content = r["response"].strip()
        if not content:
            return None
        if validate_fn is not None and not validate_fn(content):
            logger.debug("oMLX vision returned content that failed validation (model=%s)", model)
            return None
        return content
    except Exception as e:
        logger.debug("oMLX vision failed: %s", e)
        return None


_OLLAMA_META_CACHE: Optional[dict] = None


def _load_ollama_meta() -> dict:
    global _OLLAMA_META_CACHE
    if _OLLAMA_META_CACHE is not None:
        return _OLLAMA_META_CACHE
    _vision_base = (os.environ.get("MAGI_OMLX_VISION_URL") or "http://127.0.0.1:8082").rstrip("/")
    tags_url = (os.environ.get("MAGI_OLLAMA_TAGS_URL") or f"{_vision_base}/v1/models").strip()
    try:
        r = requests.get(tags_url, timeout=8)
        if r.status_code == 200:
            _OLLAMA_META_CACHE = r.json() or {}
            return _OLLAMA_META_CACHE
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 85, exc_info=True)
    _OLLAMA_META_CACHE = {}
    return _OLLAMA_META_CACHE


def _is_vision_model(model_name: str) -> bool:
    m = str(model_name or "").strip()
    if not m:
        return False
    low = m.lower()
    # Fast-path by common vision model naming.
    if any(k in low for k in ("llava", "minicpm-v", "glm-ocr", "gemma3", "gemma-4", "gemma4", "qwen", "taide")):
        return True
    meta = _load_ollama_meta()
    for it in (meta.get("models") or []):
        if str(it.get("name") or "") != m:
            continue
        det = it.get("details") or {}
        fams = [str(x).lower() for x in (det.get("families") or [])]
        fam = str(det.get("family") or "").lower()
        if ("clip" in fams) or ("clip" == fam) or ("glmocr" in fams) or ("glmocr" == fam):
            return True
    return False


def _vision_models() -> list[str]:
    chain = (
        os.environ.get("MAGI_PDF_NAMER_VISION_MODELS")
        or os.environ.get("MAGI_PDF_NAMER_VISION_MODEL")
        or os.environ.get("MAGI_VISION_MODEL")
        or os.environ.get("MAGI_OMLX_VISION_MODEL", "")
    )
    models: list[str] = []
    for raw in str(chain or "").split(","):
        m = raw.strip()
        if m and m not in models:
            models.append(m)
    # Keep only vision-capable models; avoid text-only hallucination.
    vision_models = [m for m in models if _is_vision_model(m)]
    if vision_models:
        return vision_models
    # Last fallback: pick any known vision model from local tags.
    fallback: list[str] = []
    meta = _load_ollama_meta()
    for it in (meta.get("models") or []):
        nm = str(it.get("name") or "")
        if _is_vision_model(nm) and nm not in fallback:
            fallback.append(nm)
    return fallback or [os.environ.get("MAGI_TEXT_PRIMARY_MODEL", "")]


def _ask_openai_compatible(messages: list, timeout_sec: int) -> Optional[str]:
    """
    Optional external gateway.
    Only used when MAGI_VISION_URL is explicitly configured.
    """
    url = (os.environ.get("MAGI_VISION_URL") or "").strip()
    if not url:
        return None
    model = os.environ.get("MAGI_VISION_MODEL", os.environ.get("MAGI_OMLX_VISION_MODEL", ""))
    payload = {"model": model, "messages": messages, "temperature": 0.1}
    try:
        r = requests.post(url, json=payload, timeout=timeout_sec)
        if r.status_code != 200:
            logger.warning(f"Vision gateway returned {r.status_code}: {r.text[:160]}")
            return None
        return (r.json().get("choices", [{}])[0].get("message", {}) or {}).get("content", "")
    except Exception as e:
        logger.warning(f"Vision gateway request failed: {e}")
        return None


def _ask_ollama_vision(
    prompt: str,
    b64: str,
    timeout_sec: int,
    expect_json: bool = False,
    validate_fn: Optional[Callable[[str], bool]] = None,
) -> Optional[str]:
    """
    Fallback local path: oMLX OpenAI-compatible /v1/chat/completions with image.

    If *validate_fn* is provided, a non-empty response is only accepted when
    ``validate_fn(content)`` returns True.  Otherwise the response is kept as a
    fallback candidate and the next model is tried.  If no model passes
    validation, the best (first) non-empty candidate is returned.
    """
    base_url = (os.environ.get("MAGI_OMLX_VISION_URL") or os.environ.get("MAGI_OLLAMA_API_URL") or os.environ.get("OMLX_URL") or "http://127.0.0.1:8082").strip().rstrip("/")
    url = f"{base_url}/v1/chat/completions"
    max_retries = max(1, int(os.environ.get("MAGI_OLLAMA_BUSY_RETRIES", "3") or "3"))
    retry_sleep_base = float(os.environ.get("MAGI_OLLAMA_BUSY_RETRY_SEC", "0.8") or "0.8")

    best_candidate: Optional[str] = None

    for model in _vision_models():
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }
        ]
        payload: Dict = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": 0.0,
        }
        if expect_json:
            payload["response_format"] = {"type": "json_object"}
        for attempt in range(max_retries):
            try:
                r = requests.post(url, json=payload, timeout=timeout_sec)
                if r.status_code == 200:
                    body = r.json() or {}
                    choices = body.get("choices") or []
                    content = ""
                    if choices:
                        content = ((choices[0].get("message") or {}).get("content") or "").strip()
                    if content:
                        if validate_fn is None or validate_fn(content):
                            return content
                        logger.debug(
                            "Model %s returned content that failed validation, trying next model",
                            model,
                        )
                        if best_candidate is None:
                            best_candidate = content
                    break
                txt = (r.text or "")
                if (r.status_code == 503) and ("server busy" in txt.lower() or "maximum pending requests exceeded" in txt.lower()):
                    import time, random
                    # Exponential backoff with jitter to avoid thundering herd
                    backoff = retry_sleep_base * (2 ** attempt) * (0.8 + 0.4 * random.random())
                    logger.warning(
                        "oMLX busy for model=%s (attempt %d/%d), backing off %.1fs",
                        model,
                        attempt + 1,
                        max_retries,
                        backoff,
                    )
                    time.sleep(backoff)
                    continue
                break
            except Exception:
                break

    # No model passed validation – return best non-empty candidate (or None).
    return best_candidate


def _parse_json_object(raw: str) -> dict:
    txt = (raw or "").replace("```json", "").replace("```", "").strip()
    if not txt:
        return {}
    try:
        obj = json.loads(txt)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 244, exc_info=True)
    m = re.search(r"\{[\s\S]*\}", txt)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}

def _parse_date_from_text(raw: str) -> Optional[str]:
    txt = str(raw or "").strip()
    if not txt:
        return None
    # AD compact: 20260306
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", txt)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}{mo:02d}{d:02d}"
    # AD separated: 2026/3/6 or 2026-03-06
    m = re.search(r"(20\d{2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{1,2})", txt)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}{mo:02d}{d:02d}"
    # ROC stamp format: 114.9.04 or 115.1.30 or 115. 4，-3 (dot/comma/dash, OCR artifacts)
    # Also handle OCR misreads: 175→115, 145→115 (digit confusion on stamp ink)
    m = re.search(r"(\d{3})\s*[.\s，,]+\s*(\d{1,2})\s*[.\s，,\-]+\s*(\d{1,2})", txt)
    if m:
        ry, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        # Fix common OCR misreads of ROC year on stamps (blurry blue ink)
        if 170 <= ry <= 179:  # 17x → 11x (7 misread from 1)
            ry = ry - 60
        elif 140 <= ry <= 149:  # 14x → 11x (4 misread from 1)
            ry = ry - 30
        y = ry + 1911
        if 1 <= mo <= 12 and 1 <= d <= 31 and 2000 <= y <= 2099:
            return f"{y:04d}{mo:02d}{d:02d}"
    # ROC compact: 1140306
    m = re.search(r"\b(1\d{2}|0?\d{2})(\d{2})(\d{2})\b", txt)
    if m:
        ry, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = ry + 1911
        if 1 <= mo <= 12 and 1 <= d <= 31 and 2000 <= y <= 2099:
            return f"{y:04d}{mo:02d}{d:02d}"
    # ROC separated: 114年3月6日 / 114/03/06
    m = re.search(r"(1\d{2}|0?\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*日?", txt)
    if m:
        ry, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = ry + 1911
        if 1 <= mo <= 12 and 1 <= d <= 31 and 2000 <= y <= 2099:
            return f"{y:04d}{mo:02d}{d:02d}"
    return None


def extract_date_with_vision(png_bytes: bytes, timeout_sec: int = 30) -> str:
    """
    Receipt-date extraction via local Ollama vision.
    Returns YYYYMMDD or None.
    """
    if not png_bytes:
        return None

    b64 = base64.b64encode(png_bytes).decode("utf-8")
    prompt_transcribe = (
        "請逐字轉錄這張文件圖片中所有可見文字與數字，不要推論、不用解釋。\n"
        "特別注意以下章戳（常出現在信封頁或文件右上角/右下角）：\n"
        "- 事務所收文章：藍色圓形章，內含事務所名稱（如「○○法律事務所」）、民國日期（如114.9.04）、「收文章」字樣\n"
        "- 法院收文章：紅色或藍色方形/圓形章，含法院名稱與收文日期\n"
        "- 郵戳：圓形郵戳含郵局名稱與日期\n"
        "章戳日期格式通常為民國年.月.日（如115.1.30表示民國115年1月30日）。\n"
        "若看不到任何文字才回覆 NONE。"
    )
    prompt_date = (
        "請從這張台灣法院/機關/律師事務所文件判斷「收件日期」。\n"
        "優先尋找事務所收文章（藍色圓形章，含「收文章」字樣與民國日期如114.9.04）。\n"
        "其次尋找法院收文章、收文/收件/收發/到達章戳。\n"
        "章戳日期通常為民國年格式（如114.8.12 = 西元2025年8月12日，115.1.30 = 西元2026年1月30日）。\n"
        "只回覆一行：YYYYMMDD（西元年）；完全看不到章戳日期才回覆 NONE。\n"
        "不要加其他文字，不要猜。"
    )

    # Pass 1: OCR-like transcription is usually more stable than direct date reasoning.
    def _date_validate(text: str) -> bool:
        """Return True only if the text contains a parseable date."""
        if "NONE" in text.upper():
            return False
        return _parse_date_from_text(text) is not None

    # Try oMLX first (fastest), then OpenAI gateway, then Ollama
    content = _ask_omlx_vision(
        prompt_transcribe, b64, timeout_sec=timeout_sec, validate_fn=_date_validate, prefer_ocr=True,
    )
    if not content:
        content = _ask_openai_compatible(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_transcribe},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    ],
                }
            ],
            timeout_sec=timeout_sec,
        )
    if not content:
        content = _ask_ollama_vision(
            prompt_transcribe, b64, timeout_sec=timeout_sec, expect_json=False,
            validate_fn=_date_validate,
        )
    if content and ("NONE" not in content.upper()):
        d = _parse_date_from_text(content)
        if d:
            return d

    # Pass 2: direct date extraction fallback.
    content = _ask_omlx_vision(
        prompt_date, b64, timeout_sec=timeout_sec, validate_fn=_date_validate, prefer_ocr=True,
    )
    if not content:
        content = _ask_openai_compatible(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_date},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    ],
                }
            ],
            timeout_sec=timeout_sec,
        )
    if not content:
        content = _ask_ollama_vision(
            prompt_date, b64, timeout_sec=timeout_sec, expect_json=False,
            validate_fn=_date_validate,
        )
    if not content:
        return None
    if "NONE" in content.upper():
        return None
    return _parse_date_from_text(content)

def extract_info_with_vision(png_bytes: bytes, timeout_sec: int = 45) -> dict:
    """
    Extract date/name/doc_type from a page image.
    Uses optional OpenAI-compatible gateway, then local Ollama fallback.
    """
    if not png_bytes:
        return {}

    b64 = base64.b64encode(png_bytes).decode("utf-8")
    prompt = '''請分析這張台灣法院/機關文件圖片，並提取以下欄位：
1. date (收文章日期): YYYYMMDD（西元年）。優先從「收文章」章戳讀取日期。
   - 事務所收文章：藍色圓形章，含事務所名稱、民國日期（如114.9.04）、「收文章」字樣。
   - 法院收文章：方形/圓形章，含法院名稱與收文日期。
   - 民國年轉換：民國114年=西元2025年，民國115年=西元2026年。
   - 如果找不到任何收文章戳就回覆 null。不要猜測。
2. name (當事人姓名): 從被告/原告/受文者/當事人欄位提取姓名。找不到回覆 null。
3. doc_type (文件類型): 判斷文件性質。若為信封/公文封頁面，請依信封上的案號及來源機關推斷內容文件類型。
   嚴格從以下挑選：訊問筆錄, 調查筆錄, 準備程序筆錄, 審判筆錄, 勘驗筆錄, 起訴書, 不起訴處分書, 聲請簡易判決處刑書, 判決, 裁定, 聲請書, 陳報狀, 答辯狀, 抗告狀, 上訴狀, 搜索票, 拘票, 押票, 提票, 通緝書, 扣押物品目錄表, 扣押物品收據, 贓證物品清單, 委任狀, 驗傷診斷書, 相驗屍體證明書, 法院通知, 函文, 公文封, 其他。
4. case_number (案號): 如「114年度偵字第3311號」。找不到回覆 null。
5. stamp_type (章戳類型): "事務所收文章"、"法院收文章"、"無章戳" 三選一。
6. sender (發文機關): 如「花蓮地方檢察署」、「花蓮地方法院」、「最高檢察署」。找不到回覆 null。

請嚴格回傳一個合法的 JSON 物件，不要有 markdown 符號或其他贅字。
格式範例：
{"date": "19990101", "name": "王小明", "doc_type": "民事起訴狀", "case_number": "88年度訴字第999號", "stamp_type": "事務所收文章", "sender": "臺北地方法院"}
'''

    # Example values used in the prompt – used by guardrail to detect parroting.
    _PROMPT_EXAMPLE_VALUES = {
        "date": "19990101",
        "name": "王小明",
        "doc_type": "民事起訴狀",
        "case_number": "88年度訴字第999號",
        "stamp_type": "事務所收文章",
        "sender": "臺北地方法院",
    }

    _PLACEHOLDER_PATTERNS = re.compile(
        r"^(X{2,}|Y{2,}|N/A|null|none|unknown|placeholder|範例|example|sample|YYYYMMDD|YYYY|MM|DD)$",
        re.IGNORECASE,
    )

    def _info_validate(text: str) -> bool:
        """Return True if text is valid JSON with non-placeholder values."""
        obj = _parse_json_object(text)
        if not obj:
            return False
        non_null = {k: v for k, v in obj.items() if v is not None and str(v).strip() != ""}
        if not non_null:
            return False
        # Reject if ALL fields match the prompt example (model parroted the example).
        example_match_count = sum(
            1 for k, v in non_null.items()
            if k in _PROMPT_EXAMPLE_VALUES and str(v).strip() == _PROMPT_EXAMPLE_VALUES[k]
        )
        if example_match_count > 0 and example_match_count == len(non_null):
            return False
        # Reject if any value is an obvious placeholder pattern.
        for v in non_null.values():
            if _PLACEHOLDER_PATTERNS.match(str(v).strip()):
                return False
        return True

    # Try oMLX first (TAIDE-12b vision), then OpenAI gateway, then Ollama
    content = _ask_omlx_vision(
        prompt, b64, timeout_sec=timeout_sec, validate_fn=_info_validate,
    )
    if not content:
        content = _ask_openai_compatible(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
                    ]
                }
            ],
            timeout_sec=timeout_sec,
        )
    if not content:
        content = _ask_ollama_vision(
            prompt, b64, timeout_sec=timeout_sec, expect_json=True,
            validate_fn=_info_validate,
        )
    if not content:
        return {}

    res = _parse_json_object(content)
    if not res:
        logger.warning(f"Vision JSON parse failed: {content[:220]}")
        return {}

    out = {k: v for k, v in res.items() if v is not None and str(v).strip() != ""}
    # Guardrail: reject if model parroted ALL prompt example values.
    example_match_count = sum(
        1 for k, v in out.items()
        if k in _PROMPT_EXAMPLE_VALUES and str(v).strip() == _PROMPT_EXAMPLE_VALUES[k]
    )
    if example_match_count > 0 and example_match_count == len(out):
        return {}
    # Reject any individual placeholder values.
    out = {k: v for k, v in out.items() if not _PLACEHOLDER_PATTERNS.match(str(v).strip())}
    if out.get("date"):
        d = _parse_date_from_text(str(out.get("date")))
        if d:
            out["date"] = d
        else:
            out.pop("date", None)
    # Normalize stamp_type values
    st = str(out.get("stamp_type") or "").strip()
    if st and "事務所" in st:
        out["stamp_type"] = "事務所收文章"
    elif st and "法院" in st:
        out["stamp_type"] = "法院收文章"
    elif st:
        out["stamp_type"] = "無章戳"
    return out
