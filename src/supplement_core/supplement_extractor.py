# -*- coding: utf-8 -*-
"""
supplement_extractor.py — M3：從補正裁定文字抽取 case_meta + 補件項目

公開 API:
    extract(ruling_text, *, max_text_chars=60000) -> dict
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from .exceptions import SupplementError

logger = logging.getLogger("supplement_extractor")


# ── Post-processing helpers ───────────────────────────────────────────────────

_ROC_DATE_RE = re.compile(r'(\d{2,3})年\s*(\d{1,2})月\s*(\d{1,2})日')
_ROC_DOTTED_DATE_RE = re.compile(r'(?<!\d)(\d{2,3})[./．](\d{1,2})[./．](\d{1,2})(?!\d)')


def _roc_to_iso(s: str) -> str:
    """民國『115年4月29日』→『2026-04-29』。轉不出來回空字串。"""
    if not s or not isinstance(s, str):
        return s or ""
    m = _ROC_DATE_RE.search(s)
    if not m:
        # 已是 ISO 格式或空就原樣
        if re.match(r'^\d{4}-\d{2}-\d{2}$', s.strip()):
            return s.strip()
        return ""
    roc_y, mo, d = m.groups()
    return f"{int(roc_y)+1911:04d}-{int(mo):02d}-{int(d):02d}"


def _extract_notice_date_from_text(text: str, case_no: str) -> str:
    """從法院通知文字抽與案號年度一致的民國日期，作為 LLM 日期失準時的 fallback。"""
    case_year = _extract_case_year_western(case_no)
    for regex in (_ROC_DATE_RE, _ROC_DOTTED_DATE_RE):
        for m in regex.finditer(text[:5000]):
            roc_y, mo, d = m.groups()
            try:
                western_year = int(roc_y) + 1911
                if case_year and abs(western_year - case_year) > 1:
                    continue
                return f"{western_year:04d}-{int(mo):02d}-{int(d):02d}"
            except ValueError:
                continue
    return ""


_PARTY_PREFIX_RE = re.compile(r'^(?:聲請人|債務人)\s*[:：]?\s*')

_PARTY_EXCLUDE_KEYWORDS = ['債權人', '銀行', '股份', '公司', '機關', '法人']


def _clean_party(name: str) -> str | None:
    """從『債務人李嘉玲』抽『李嘉玲』。
    若是公司/銀行/債權人 → return None（會被 caller 過濾掉）。
    若去掉前綴後為空（即輸入本身只是「聲請人」等前綴詞） → return None。
    """
    if not name:
        return None
    name = name.strip()
    # 過濾明顯非聲請人
    if any(kw in name for kw in _PARTY_EXCLUDE_KEYWORDS):
        return None
    # 移除「聲請人」「債務人」前綴
    cleaned = _PARTY_PREFIX_RE.sub('', name).strip()
    # 「等」結尾移除
    cleaned = re.sub(r'等$', '', cleaned).strip()
    if not cleaned:
        return None
    # 若清洗後仍是純前綴詞（e.g. 「聲請人」本身），也過濾
    if cleaned in {'聲請人', '債務人', '申請人'}:
        return None
    return cleaned


# ── 日期 sanity check helpers ──────────────────────────────────────────────

_CASE_YEAR_RE = re.compile(r'(\d{2,3})年度')


def _extract_case_year_western(case_no: str) -> int | None:
    """從案號『115年度司消債調字第393號』抽案件年度（西元）。"""
    if not case_no:
        return None
    m = _CASE_YEAR_RE.search(case_no)
    if not m:
        return None
    try:
        return int(m.group(1)) + 1911
    except ValueError:
        return None


def _validate_date_against_case_year(date_iso: str, case_no: str,
                                      tolerance_years: int = 1) -> str:
    """若 ruling_date 年度 < 案件年度 - tolerance → 視為抽錯，回空字串。

    寬容 tolerance_years 是因為前置調解 → 補正裁定可能跨年度，
    但相差 > 2 年幾乎一定是抽錯。

    Args:
        date_iso: "YYYY-MM-DD" 或空字串
        case_no: 案號
        tolerance_years: 允許的早於案件年度的年數
    """
    if not date_iso or len(date_iso) < 4:
        return date_iso
    try:
        date_year = int(date_iso[:4])
    except ValueError:
        return ""
    case_year = _extract_case_year_western(case_no)
    if case_year is None:
        return date_iso  # 無案件年度可比，留著
    if date_year < case_year - tolerance_years:
        return ""  # 明顯抽錯，棄掉
    if date_year > case_year + tolerance_years:
        return ""  # 也明顯抽錯（未來日期）
    return date_iso


_GENERIC_CATEGORY = {'補正資料', '補件', '資料', '檔案', '補件項目', '補正項目'}

_SUPPLEMENT_KEYWORDS = ['補正', '補件', '應提出', '請提出', '附件所示']

# 案號 regex：如「114年度消債更字第512號」「115年度司消債調字第393號」
_CASE_NO_RE = re.compile(
    r'\d{2,3}年度[^\s「」\r\n]{1,20}字第\d+號'
)

# 補正條款 regex：如「三、請補正...」「四、說明...」「(一)...」
_SUPPLEMENT_ITEM_RE = re.compile(
    r'(?:^|\n)\s*(?:[一二三四五六七八九十]+[、。]|[（(][一二三四五六七八九十]+[）)]|\d+[.、])\s*'
    r'(?:請(?:補正|說明|提出|陳報)|說明|補正|應(?:補正|提出|說明)|請(?:務必)?補正)'
    r'(.{5,60})',
    re.MULTILINE
)


def _has_supplement_content(text: str) -> bool:
    """判斷文字中是否明顯含有補正事項的關鍵詞。"""
    return any(kw in text for kw in _SUPPLEMENT_KEYWORDS)


def _extract_supplement_items_from_text(text: str) -> list[dict]:
    """當 LLM items 為空時，從 OCR 文字直接 regex 抽取補件條款作為 fallback。"""
    matches = _SUPPLEMENT_ITEM_RE.findall(text)
    if not matches:
        return []
    items = []
    for i, match in enumerate(matches[:20], start=1):  # 最多 20 條
        raw = match.strip()
        # 去除換行和多餘空白
        raw = re.sub(r'\s+', ' ', raw)[:60]
        # 移除末尾的非字符
        raw = re.sub(r'[^\w）)。]+$', '', raw).strip()
        if not raw or len(raw) < 3:
            continue
        item = {
            "item_id": i,
            "category": _improve_category("", raw),
            "issuer": "",
            "period": "",
            "mandatory": True,
            "quote": raw[:30],
            "keywords": [raw[:8]] if raw else [],
            "verified": False,
        }
        items.append(item)
    return items


def _improve_category(category: str, quote: str) -> str:
    """category 太籠統時，從 quote 取前 N 字當 fallback category。"""
    if not category:
        category = ""
    category = category.strip()
    if category in _GENERIC_CATEGORY or len(category) <= 2:
        # 用 quote 前 10 字當 category（去除標點）
        if quote:
            fallback = re.sub(r'[，,。、；;:：]', '', quote.strip())[:10]
            if fallback:
                return fallback
        return category or "其他補正"
    return category


# ── Prompt 模板 ───────────────────────────────────────────────────────────────

_EXTRACT_PROMPT_TMPL = """\
你是消債更生案件助理。從以下程序裁定原文中嚴謹抽取「聲請人應補正之項目」。

【原文】（OCR 結果，可能有錯字）
{ruling_text}

【嚴格規則】
1. parties 只能填「聲請人 / 債務人」這個身份的姓名（單人，不是公司）。
   絕對不要填債權人、銀行、機關。原文若寫「債務人 OOO」「聲請人 OOO」，
   就抽 OOO 的姓名（去掉前綴）。
2. 日期一律輸出西元 ISO 格式 YYYY-MM-DD。
   原文若寫民國「115年4月29日」就轉「2026-04-29」（民國年 + 1911）。
   抽不到就回空字串「」。
3. category 必須是具體的證據類別，不能寫籠統的「補正資料」「補件」「資料」。
   範例好的 category：「綜所稅清單」「勞保異動明細」「戶籍謄本」「居住事實證明」「銀行存摺」
   範例不好的 category：「補正資料」「補件項目」「資料」「檔案」
   若原文要求多個證據合併達成同一目的，category 命名應反映該目的
   （如要求戶籍+租賃契約+水電帳單共證居住事實 → category="居住事實證明"）。

【輸出】純 JSON（無 markdown、無前後綴）：
{{
  "case_meta": {{
    "court": "...",
    "case_no": "...",
    "parties": ["..."],         // 只一個聲請人姓名
    "ruling_date": "YYYY-MM-DD",
    "deadline_date": "YYYY-MM-DD"
  }},
  "items": [{{
    "item_id": 1,
    "category": "具體類別",
    "issuer": "...",
    "period": "...",
    "mandatory": true,
    "quote": "...",
    "keywords": ["..."]
  }}]
}}

補充規則：
- 原文未明示之欄位用空字串；items 為空陣列代表此份非補正裁定。
- quote 必須是原文中可定位的連續片段，最多 30 字。
- keywords 用於後續比對附件檔名，至少 2 個。
- 案號格式維持原文（含「年度」「字第」「號」）。
- 法院名稱用全名（如「新北地方法院」）。
"""

_VERIFY_PROMPT_TMPL = """\
原文：
{ruling_text}

請對以下補件項逐項判斷是否能在原文找到依據：
{items_text}

僅回 JSON: {{"verifications": [{{"item_id": 1, "verified": true}}, ...]}}
"""


# ── LLM 呼叫工具 ─────────────────────────────────────────────────────────────

def _get_gateway():
    """延遲載入 InferenceGateway，避免 import 時觸發副作用。"""
    from skills.bridge.inference_gateway import InferenceGateway  # noqa: PLC0415
    return InferenceGateway()


def _call_llm(prompt: str, *, timeout: int = 120) -> str:
    """呼叫 LLM，回傳原始文字。失敗時 raise SupplementError。"""
    if os.environ.get("SUPPLEMENT_DISABLE_LLM") == "1":
        raise SupplementError("LLM disabled by SUPPLEMENT_DISABLE_LLM")
    timeout = int(os.environ.get("SUPPLEMENT_LLM_TIMEOUT", timeout))
    gw = _get_gateway()
    result = gw.chat(prompt, task_type="legal_analysis", timeout=timeout)
    if not result.get("success"):
        err_msg = result.get("error") or result.get("message") or "LLM call failed"
        raise SupplementError(f"InferenceGateway 呼叫失敗：{err_msg}")
    response = result.get("response", "")
    if not response:
        raise SupplementError("InferenceGateway 回傳空白 response")
    return response


# ── JSON 容錯解析 ─────────────────────────────────────────────────────────────

def _parse_json_loose(text: str) -> Any:
    """容錯解析 LLM 輸出的 JSON。

    策略：
    1. 直接 json.loads
    2. 去除 markdown 圍欄後再試
    3. regex 掃描第一個完整 {...} 區塊（括號計數）
    4. 全部失敗 → raise SupplementError
    """
    # 1. 直接嘗試
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 2. 去除 ```json ... ``` 或 ``` ... ```
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    # 3. 括號計數找第一個完整 {...}
    start = stripped.find("{")
    if start != -1:
        depth = 0
        for i, ch in enumerate(stripped[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = stripped[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break

    raise SupplementError("LLM JSON parse failed")


# ── 內部：Stage 1 抽取 ────────────────────────────────────────────────────────

_EXTRACT_RETRY_PROMPT_SUFFIX = """

請注意：請直接輸出 JSON，不要說任何其他話。
輸出格式必須是純 JSON，從 { 開始到 } 結束。"""


def _stage1_extract(ruling_text: str, *, max_retries: int = 1) -> tuple[dict, str]:
    """呼叫 LLM 做 Stage 1 抽取，回傳 (parsed_dict, raw_response)。

    若 JSON 解析失敗，自動重試（最多 max_retries 次）。
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        suffix = _EXTRACT_RETRY_PROMPT_SUFFIX if attempt > 0 else ""
        prompt = _EXTRACT_PROMPT_TMPL.format(ruling_text=ruling_text) + suffix
        try:
            raw = _call_llm(prompt, timeout=90)
            parsed = _parse_json_loose(raw)
            return parsed, raw
        except SupplementError as exc:
            last_exc = exc
            logger.warning("Stage1 extract attempt %d/%d failed: %s", attempt + 1, max_retries, exc)

    raise last_exc  # type: ignore[misc]


# ── 內部：Stage 2 逆向驗證 ────────────────────────────────────────────────────

def _stage2_verify(ruling_text: str, items: list[dict], errors: list[str]) -> list[dict]:
    """批次逆向驗證，回傳補上 verified 欄位的 items。

    失敗時 verified 預設 False，並寫 errors，不拋例外。
    """
    if not items:
        return items

    # 建立 items 說明文字
    lines = []
    for item in items:
        lines.append(
            f"  {item['item_id']}. category={item.get('category','')}, "
            f"quote={item.get('quote','')}"
        )
    items_text = "\n".join(lines)

    # 截取原文用於驗證（最多 30000 字，避免 prompt 太長）
    verify_text = ruling_text[:30000]

    prompt = _VERIFY_PROMPT_TMPL.format(
        ruling_text=verify_text,
        items_text=items_text,
    )

    try:
        raw = _call_llm(prompt, timeout=45)
        parsed = _parse_json_loose(raw)
        verifications = parsed.get("verifications", [])
        # 建立 item_id → verified 映射
        verify_map: dict[int, bool] = {}
        for v in verifications:
            iid = v.get("item_id")
            if iid is not None:
                verify_map[iid] = bool(v.get("verified", False))
    except SupplementError as exc:
        errors.append(f"reverse_verify LLM error: {exc}")
        verify_map = {}

    # 補上 verified 欄位
    result = []
    for item in items:
        item = dict(item)  # shallow copy
        iid = item.get("item_id")
        item["verified"] = verify_map.get(iid, False)
        result.append(item)

    return result


# ── 公開 API ──────────────────────────────────────────────────────────────────

def extract(ruling_text: str, *, max_text_chars: int = 60000) -> dict:
    """從補正裁定文字抽取 case_meta + 補件項目（含逆向驗證）。

    Returns: {
        "success": bool,
        "case_meta": {
            "court": str,
            "case_no": str,
            "parties": list[str],
            "ruling_date": str,
            "deadline_date": str,
        },
        "items": [{
            "item_id": int,
            "category": str,
            "issuer": str,
            "period": str,
            "mandatory": bool,
            "quote": str,
            "keywords": list[str],
            "verified": bool,
        }],
        "raw_response": str,
        "errors": list[str],
    }

    致命錯誤（兩階段 LLM 全失敗 / JSON 解析不出）→ raise SupplementError
    """
    errors: list[str] = []

    # ── 文字截斷 ──
    if len(ruling_text) > max_text_chars:
        ruling_text = ruling_text[:max_text_chars]
        errors.append("text_truncated")

    # ── Stage 1：抽取（失敗時 fallback 到 regex，不 raise）──
    raw_response = ""
    parsed: dict = {}
    stage1_failed = False
    try:
        parsed, raw_response = _stage1_extract(ruling_text)
    except SupplementError as exc:
        stage1_failed = True
        errors.append(f"stage1_llm_failed: {exc}")
        # 仍繼續，後面 regex fallback 會補救

    # ── 解析 case_meta ──
    raw_meta = parsed.get("case_meta", {}) if parsed else {}

    # 過濾 LLM 照抄模板佔位符的無效值
    _TEMPLATE_PLACEHOLDERS = {"...", "姓名", "具體類別", "YYYY-MM-DD"}

    def _clean_field(val: object, default: str = "") -> str:
        s = str(val or "").strip()
        return default if s in _TEMPLATE_PLACEHOLDERS else s

    case_meta_dict: dict = {
        "court": _clean_field(raw_meta.get("court")),
        "case_no": _clean_field(raw_meta.get("case_no")),
        "parties": list(raw_meta.get("parties") or []),
        "ruling_date": _clean_field(raw_meta.get("ruling_date")),
        "deadline_date": _clean_field(raw_meta.get("deadline_date")),
    }

    # ── Post-processing：case_no 若空從原文 regex 抽取 ──
    if not case_meta_dict["case_no"]:
        m = _CASE_NO_RE.search(ruling_text)
        if m:
            case_meta_dict["case_no"] = m.group(0)
            errors.append("case_no_from_regex")

    # ── Post-processing：parties 清洗 ──
    raw_parties = case_meta_dict.get("parties", []) or []
    cleaned_parties = []
    for p in raw_parties:
        c = _clean_party(str(p))
        if c and c not in cleaned_parties:
            cleaned_parties.append(c)
    case_meta_dict["parties"] = cleaned_parties

    # ── Post-processing：日期 ISO 轉換 ──
    case_meta_dict["ruling_date"] = _roc_to_iso(case_meta_dict.get("ruling_date", ""))
    case_meta_dict["deadline_date"] = _roc_to_iso(case_meta_dict.get("deadline_date", ""))

    # ── Post-processing：日期 sanity check（防年度錯亂）──
    case_no_for_check = case_meta_dict.get("case_no", "")
    orig_ruling = case_meta_dict["ruling_date"]
    orig_deadline = case_meta_dict["deadline_date"]
    case_meta_dict["ruling_date"] = _validate_date_against_case_year(
        case_meta_dict.get("ruling_date", ""), case_no_for_check
    )
    case_meta_dict["deadline_date"] = _validate_date_against_case_year(
        case_meta_dict.get("deadline_date", ""), case_no_for_check
    )
    if orig_ruling and not case_meta_dict["ruling_date"]:
        errors.append(f"ruling_date_sanity_rejected:{orig_ruling}")
    if not case_meta_dict["ruling_date"]:
        fallback_ruling_date = _extract_notice_date_from_text(ruling_text, case_no_for_check)
        if fallback_ruling_date:
            case_meta_dict["ruling_date"] = fallback_ruling_date
            errors.append("ruling_date_from_text_regex")
    if orig_deadline and not case_meta_dict["deadline_date"]:
        errors.append(f"deadline_date_sanity_rejected:{orig_deadline}")

    case_meta = case_meta_dict

    # ── 解析 items ──
    raw_items = (parsed.get("items") or []) if parsed else []
    items: list[dict] = []
    for raw_item in raw_items:
        item = {
            "item_id": int(raw_item.get("item_id", 0)),
            "category": str(raw_item.get("category") or ""),
            "issuer": str(raw_item.get("issuer") or ""),
            "period": str(raw_item.get("period") or ""),
            "mandatory": bool(raw_item.get("mandatory", True)),
            "quote": str(raw_item.get("quote") or ""),
            "keywords": list(raw_item.get("keywords") or []),
            "verified": False,  # 待 Stage 2 填入
        }
        items.append(item)

    # ── Post-processing：category 改善 + 過濾佔位符 items ──
    items = [item for item in items if item.get("category", "") not in _TEMPLATE_PLACEHOLDERS]
    for item in items:
        item["category"] = _improve_category(item.get("category", ""), item.get("quote", ""))

    # ── 若 items 為空但原文明顯有補正內容，截短重試 ──
    if not items and _has_supplement_content(ruling_text):
        errors.append("items_empty_retry_with_shorter_text")
        short_text = ruling_text[:20000]  # 截短到 20000 字
        try:
            parsed2, raw_response2 = _stage1_extract(short_text, max_retries=1)
            raw_items2 = parsed2.get("items") or []
            items2: list[dict] = []
            for raw_item in raw_items2:
                item = {
                    "item_id": int(raw_item.get("item_id", 0)),
                    "category": str(raw_item.get("category") or ""),
                    "issuer": str(raw_item.get("issuer") or ""),
                    "period": str(raw_item.get("period") or ""),
                    "mandatory": bool(raw_item.get("mandatory", True)),
                    "quote": str(raw_item.get("quote") or ""),
                    "keywords": list(raw_item.get("keywords") or []),
                    "verified": False,
                }
                item["category"] = _improve_category(item.get("category", ""), item.get("quote", ""))
                items2.append(item)
            if items2:
                items = items2
                raw_response = raw_response2
        except SupplementError as exc:
            errors.append(f"items_empty_retry_failed: {exc}")

    # ── 若 items 仍為空，用 regex 從文字直接抽取補正條款（最後手段）──
    if not items and _has_supplement_content(ruling_text):
        fallback_items = _extract_supplement_items_from_text(ruling_text)
        if fallback_items:
            items = fallback_items
            errors.append("items_from_regex_fallback")

    # ── Stage 2：逆向驗證 ──
    if items and stage1_failed:
        errors.append("reverse_verify_skipped_after_stage1_fallback")
    else:
        items = _stage2_verify(ruling_text, items, errors)

    return {
        "success": True,
        "case_meta": case_meta,
        "items": items,
        "raw_response": raw_response,
        "errors": errors,
    }
