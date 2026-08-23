"""Source-bound Taiwanese sentencing trend search.

Only independently parsed official full text can enter statistics.  MCP is
used to expand a small local archive, but every remote candidate must carry an
official JID, an allow-listed official URL and full text that passes the same
signature/main-text/appendix gates as a local judgment.
"""

from __future__ import annotations

import re
import statistics
from datetime import date
from typing import Any, Callable
from api.domains.judgment_official_source import (
    OFFICIAL_JID_RE as _OFFICIAL_JID_RE,
    is_official_judgment_url,
    normalize_judgment_date,
    official_judgment_page_url,
    validate_official_judgment_candidate,
)
from api.osc.utils import _osc_web_connect


_SPACE_RE = re.compile(r"[\t\r\f\v ]+")
_COURT_RE = re.compile(
    r"((?:臺灣)?[一-鿿]{1,10}(?:地方法院|高等法院(?:[一-鿿]{0,6}分院)?|高等行政法院|最高法院|最高行政法院|智慧財產及商業法院))"
)
_SIGNED_NAME_AT_LINE_END_RE = re.compile(
    r"法\s*官\s*((?:[一-鿿]\s*){2,4})$"
)
_SENTENCE_RE = re.compile(
    r"(?:處|應執行|定應執行)"
    r"(?P<kind>死刑|無期徒刑|有期徒刑|拘役|罰金)"
    r"(?P<term>[^\n。；;]{0,42})"
)
_EXECUTION_RE = re.compile(
    r"(?:定其)?應執行(?P<kind>死刑|無期徒刑|有期徒刑|拘役|罰金)"
    r"(?P<term>[^\n。；;]{0,42})"
)
_CN_DIGITS = {"零": 0, "○": 0, "〇": 0, "一": 1, "壹": 1, "貳": 2, "二": 2, "兩": 2, "參": 3, "三": 3, "肆": 4, "四": 4, "伍": 5, "五": 5, "陸": 6, "六": 6, "柒": 7, "七": 7, "捌": 8, "八": 8, "玖": 9, "九": 9}
_CHAT_COMMAND_RE = re.compile(
    r"(?:法官量刑(?:與判決)?趨勢|量刑(?:與判決)?趨勢|判決趨勢|趨勢分析|"
    r"案由分析|判決分析|案由統計|判決統計|見解趨勢|裁判趨勢|查詢|搜尋|分析|幫我|請|麻煩)"
)
_CHAT_FULL_COURT_RE = re.compile(
    r"(最高法院|最高行政法院|憲法法庭|智慧財產及商業法院|"
    r"臺灣高等法院(?:[一-鿿]{1,6}分院)?|"
    r"(?:臺灣)?[一-鿿]{1,6}地方法院|"
    r"[一-鿿]{1,8}高等行政法院)"
)
_CHAT_COURT_ALIAS_RE = re.compile(r"([一-鿿]{1,6})(地院|高分院)")
_CHAT_JUDGE_GENERIC = {"某", "某某", "這位", "該名", "承辦", "審理"}

_PUBLIC_EXCLUSION_REASONS = {
    "missing_official_jid": "缺少可核對的官方 JID",
    "missing_or_invalid_official_jid": "缺少或無法核對官方 JID",
    "missing_official_fulltext": "尚未取得可獨立核對的官方全文",
    "official_origin_not_verified": "來源尚未證明為司法院官方裁判",
    "unofficial_source_url": "來源網址不是司法院官方裁判網址",
    "official_url_jid_mismatch": "官方網址與裁判 JID 不一致",
    "signature_block_unrecognized": "全文簽署區未辨識出法官",
    "main_section_unrecognized": "全文主文區段無法辨識",
    "sentence_not_found": "主文未辨識出可統計刑度",
    "appendix_incomplete": "主文引用附表，但附表刑度尚未完整取得",
    "judgment_date_invalid": "判決日期格式無法核對",
    "judgment_date_missing": "裁判未提供可核對的判決日期",
    "judge_mismatch": "裁判簽署區未列出查詢的法官",
    "court_mismatch": "裁判法院與查詢法院不符",
    "offense_mismatch": "裁判案由或主文與查詢案由不符",
    "date_before_range": "判決日期早於查詢期間",
    "date_after_range": "判決日期晚於查詢期間",
    "duplicate_local_jid": "本機已有同一官方 JID，未重複計入 MCP",
}


def _public_exclusion_reason(code: str, *, requested_judge: str = "", item: dict[str, Any] | None = None) -> str:
    if code == "judge_mismatch" and requested_judge:
        parsed = item or {}
        listed = "、".join(str(value) for value in parsed.get("participating_judges") or [] if value)
        last = str(parsed.get("last_listed_judge") or "").strip()
        observed = f"；簽署區：{listed or '未辨識'}；末位列名：{last or '未辨識'}"
        return f"查詢法官「{requested_judge}」與裁判簽署區不符{observed}"
    return _PUBLIC_EXCLUSION_REASONS.get(str(code), "未通過裁判品質核對")


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).replace("台", "臺")


def _clean_text(value: Any) -> str:
    return "\n".join(_SPACE_RE.sub(" ", line).strip() for line in str(value or "").splitlines()).strip()


def _normalized_iso_date_filter(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError(f"{label}不正確，請重新選擇民國年、月、日。")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label}不正確，請重新選擇民國年、月、日。") from exc
    return text


def format_roc_date(value: Any) -> str:
    """Format a display date in Taiwanese ROC notation without changing its key.

    ``judgment_date`` stays in ISO form throughout storage, filtering and sort
    operations.  This helper is deliberately output-only, so desktop, mobile,
    chat and JSON consumers can show a consistent Taiwanese date without
    changing any query semantics.
    """

    text = str(value or "").strip()
    if not text:
        return ""
    iso_match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:\b|T|\s)", text)
    if iso_match:
        year, month, day = (int(part) for part in iso_match.groups())
    else:
        roc_match = re.match(r"^(?:民國)?\s*(\d{1,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?$", text)
        if roc_match:
            year, month, day = (int(part) for part in roc_match.groups())
            try:
                date(year + 1911, month, day)
            except ValueError:
                return text
            return f"民國{year}年{month}月{day}日"
        return text
    try:
        parsed = date(year, month, day)
    except ValueError:
        return text
    return f"民國{parsed.year - 1911}年{parsed.month}月{parsed.day}日"


def _cn_number(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    arabic = re.match(r"\d+", text.replace(",", ""))
    if arabic:
        return int(arabic.group())
    total = 0
    current = 0
    used = False
    units = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
    for char in text:
        if char in _CN_DIGITS:
            current = _CN_DIGITS[char]
            used = True
        elif char in units:
            total += (current or 1) * units[char]
            current = 0
            used = True
        else:
            break
    return total + current if used else None


def _duration_months(kind: str, term: str) -> float | None:
    if kind != "有期徒刑":
        return None
    number = r"[0-9零○〇一二兩三四五六七八九十壹貳參肆伍陸柒捌玖拾]+"
    # Only accept a duration immediately following the sentence kind.  A later
    # ROC year (for example "徒刑5月確定，於民國110年5月…") must never be
    # interpreted as a 110-year sentence.
    match = re.match(
        rf"\s*(?:(?P<years>{number})年)?\s*(?:又\s*)?(?:(?P<months>{number})(?:個)?月)?",
        term,
    )
    if not match or (not match.group("years") and not match.group("months")):
        return None
    years = _cn_number(match.group("years")) if match.group("years") else 0
    months = _cn_number(match.group("months")) if match.group("months") else 0
    if years is None or months is None:
        return None
    return float(years * 12 + months)


def _main_text(full_text: str) -> str:
    # The official MCP sometimes returns a visually spaced judgment as one
    # long line.  Do not require a newline before the next section; require a
    # legal section heading instead so flattened official text remains
    # parseable without broadening the boundary into the reasons.
    match = re.search(
        # Official pages can flatten ``事 實`` directly into an anonymised
        # full-width identifier (for example ``事 實Ａ０７…``).  The old
        # look-ahead restricted the first content character to a tiny heading
        # alphabet and therefore rejected a perfectly valid main section.
        # Keep the boundary structural instead: unspaced multi-character
        # headings are exact, while the otherwise ambiguous short headings
        # must retain the official visual spacing between their characters.
        r"主\s*文\s*(.+?)(?=\s+(?:犯罪事實(?:及理由)?|事實及理由|事實及證據|事\s+實|理\s+由|理由(?=\s|$)))",
        full_text,
        re.S,
    )
    return _clean_text(match.group(1)) if match else ""


def _signature_judges(full_text: str) -> list[dict[str, str]]:
    judgment = full_text.split("以上正本證明與原本無異", 1)[0]
    tail = judgment[-2400:]
    found: list[dict[str, str]] = []
    for raw_line in tail.splitlines():
        line = raw_line.strip()
        match = _SIGNED_NAME_AT_LINE_END_RE.search(line)
        if not match:
            continue
        name = re.sub(r"\s+", "", match.group(1))
        prefix = line[: match.start()]
        role_match = re.search(r"(審判長|受命|陪席)\s*$", prefix)
        role = role_match.group(1) if role_match else ""
        item = {"name": name, "role": f"{role}法官" if role else "法官"}
        if item not in found:
            found.append(item)
    if len(found) == 1 and found[0]["role"] == "法官":
        found[0]["role"] = "獨任法官"
    return found


def _sentence_items(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for match in _SENTENCE_RE.finditer(text):
        phrase = _clean_text(match.group(0))
        item = {"kind": match.group("kind"), "text": phrase, "months": _duration_months(match.group("kind"), match.group("term"))}
        if item not in items:
            items.append(item)
    return items


def _execution_item(main_text: str) -> dict[str, Any] | None:
    match = _EXECUTION_RE.search(main_text)
    if not match:
        return None
    return {
        "kind": match.group("kind"),
        "text": _clean_text(match.group(0)),
        "months": _duration_months(match.group("kind"), match.group("term")),
    }


def _official_issue(full_text: str, fallback: str = "") -> str:
    head = _clean_text(full_text[:1800])
    patterns = (
        r"上列.{0,18}?因(.{1,40}?)案件",
        r"因犯(.{1,32}?)罪",
    )
    for pattern in patterns:
        match = re.search(pattern, head, re.S)
        if match:
            return re.sub(r"\s+", "", match.group(1)).strip("，。；;")[:80]
    return str(fallback or "").strip()


def parse_sentencing_judgment(row: dict[str, Any]) -> dict[str, Any]:
    full_text = str(row.get("full_text") or "")
    main = _main_text(full_text)
    judges = _signature_judges(full_text)
    participating_judges = [entry["name"] for entry in judges if entry.get("name")]
    last_listed_judge = participating_judges[-1] if participating_judges else ""
    appendix_referenced = "如附表" in main or "如附表" in full_text[:2200]
    appendix_pos = max(full_text.rfind("【附表"), full_text.rfind("\n附表"))
    appendix_text = full_text[appendix_pos:] if appendix_pos >= 0 else ""
    appendix_sentences = _sentence_items(appendix_text)
    appendix_complete = (not appendix_referenced) or bool(appendix_sentences)
    main_sentences = _sentence_items(main)
    execution = _execution_item(main)
    if execution:
        main_sentences = [item for item in main_sentences if item["text"] != execution["text"]]
    raw_source_url = str(row.get("source_url") or "").strip()
    jid = str(row.get("jid") or "").strip()
    source_url = official_judgment_page_url(jid, raw_source_url)
    court_match = _COURT_RE.search(full_text[:500])
    court = court_match.group(1) if court_match else str(row.get("court_name") or "").strip()
    judgment_date = normalize_judgment_date(row.get("judgment_date"))
    exclusion_codes: list[str] = []
    if not jid or not _OFFICIAL_JID_RE.fullmatch(jid):
        exclusion_codes.append("missing_official_jid")
    if not full_text:
        exclusion_codes.append("missing_official_fulltext")
    if not judges:
        exclusion_codes.append("signature_block_unrecognized")
    if not main:
        exclusion_codes.append("main_section_unrecognized")
    if main and not (main_sentences or execution):
        exclusion_codes.append("sentence_not_found")
    if appendix_referenced and not appendix_complete:
        exclusion_codes.append("appendix_incomplete")
    if row.get("judgment_date") and not judgment_date:
        exclusion_codes.append("judgment_date_invalid")
    complete = not exclusion_codes
    return {
        "id": row.get("id"),
        "jid": jid,
        "court": court,
        "case_number": str(row.get("case_number") or "").strip(),
        "case_type": str(row.get("case_type") or "").strip(),
        "issue": _official_issue(full_text, str(row.get("case_type") or "")),
        # Keep the ISO canonical value for database/filter/sort semantics and
        # provide an explicit display-only value for every presentation path.
        "judgment_date": judgment_date,
        "judgment_date_display": format_roc_date(judgment_date),
        "judges": judges,
        "participating_judges": participating_judges,
        "last_listed_judge": last_listed_judge,
        "main_text": main[:1800],
        "sentences": main_sentences,
        "execution_sentence": execution,
        "appendix_referenced": appendix_referenced,
        "appendix_complete": appendix_complete,
        "appendix_sentences": appendix_sentences[:60],
        "statistics_eligible": complete,
        "exclusion_codes": exclusion_codes,
        "exclusion_reason": "；".join(_public_exclusion_reason(code) for code in exclusion_codes),
        "source_url": source_url,
        "source_bound": bool(jid and (_official_judgment_url(raw_source_url) or full_text)),
        "source": str(row.get("source") or "local_official_archive"),
        "external_verified": bool(row.get("external_verified")),
    }


def _official_judgment_url(value: Any) -> bool:
    return is_official_judgment_url(value)


def search_public_judgment_candidates(query: str, **kwargs: Any) -> dict[str, Any]:
    """Search the deployed MCP chain with a privacy-safe query.

    Results remain discovery candidates.  ``search_sentencing_trends`` applies
    the independent official-JID/full-text/signature/sentence gates before any
    result can enter statistics.
    """

    from api.legal_research_quality import prepare_external_legal_query

    privacy = prepare_external_legal_query(query)
    court = str(kwargs.pop("court", "") or "").strip()
    if privacy.external_allowed and privacy.safe_query:
        try:
            from api.osc.legaltech_taiwan_law_mcp import (
                search_practical_judgments_via_legaltech,
            )

            result = search_practical_judgments_via_legaltech(
                privacy.safe_query,
                **({"court": court} if court else {}),
                **kwargs,
            )
            if result.get("success"):
                return result
        except (ImportError, ModuleNotFoundError):
            pass
    try:
        from api.osc.taiwan_legal_mcp import search_practical_judgments_via_mcp

        return search_practical_judgments_via_mcp(
            privacy.safe_query if privacy.external_allowed else "",
            **({"court": court} if court else {}),
            **kwargs,
        )
    except (ImportError, ModuleNotFoundError):
        return {"ok": False, "success": False, "error": "public_judgment_search_unavailable"}


def _chat_year(value: str) -> int:
    year = int(value)
    if year <= 300:
        year += 1911
    if not 1912 <= year <= 2100:
        raise ValueError("年份超出可查詢範圍。")
    return year


def _chat_date(year: str, month: str, day: str) -> str:
    return date(_chat_year(year), int(month), int(day)).isoformat()


def _normalise_chat_court(value: str) -> str:
    court = re.sub(r"\s+", "", str(value or "")).replace("台", "臺")
    if court.endswith("地院"):
        place = court[:-2]
        if place.startswith("臺灣"):
            place = place[2:]
        return f"臺灣{place}地方法院"
    if court.endswith("高分院"):
        place = court[:-3]
        if place.startswith("臺灣"):
            place = place[2:]
        return f"臺灣高等法院{place}分院"
    if court.endswith("地方法院") and not court.startswith("臺灣"):
        return f"臺灣{court}"
    return court


def parse_sentencing_trend_chat_query(message: str) -> tuple[dict[str, str], str]:
    """Turn a natural-language chat request into the web search filters.

    The parser is deliberately deterministic.  Missing or ambiguous criteria
    produce a clarification instead of an invented court, judge or offence.
    """

    text = str(message or "").strip().replace("台", "臺")
    text = re.sub(r"^@(?:MAGI|重型|heavy)\s*", "", text, flags=re.I).strip()
    working = _CHAT_COMMAND_RE.sub(" ", text)
    working = re.sub(r"^\s*(?:查|找|看|比較)\s*", "", working)
    date_from = ""
    date_to = ""

    full_range = re.search(
        r"(?:民國)?(\d{2,4})年(\d{1,2})月(\d{1,2})日?\s*"
        r"(?:至|到|－|—|~|～)\s*(?:民國)?(\d{2,4})年(\d{1,2})月(\d{1,2})日?",
        working,
    )
    iso_range = re.search(
        r"(\d{4})-(\d{1,2})-(\d{1,2})\s*(?:至|到|－|—|~|～)\s*"
        r"(\d{4})-(\d{1,2})-(\d{1,2})",
        working,
    )
    year_range = re.search(
        r"(?:民國)?(\d{2,4})\s*年?\s*(?:至|到|－|—|~|～)\s*"
        r"(?:民國)?(\d{2,4})\s*年(?!度)",
        working,
    )
    try:
        if full_range:
            date_from = _chat_date(*full_range.groups()[:3])
            date_to = _chat_date(*full_range.groups()[3:])
            working = working.replace(full_range.group(0), " ")
        elif iso_range:
            date_from = _chat_date(*iso_range.groups()[:3])
            date_to = _chat_date(*iso_range.groups()[3:])
            working = working.replace(iso_range.group(0), " ")
        elif year_range:
            start = _chat_year(year_range.group(1))
            end = _chat_year(year_range.group(2))
            date_from, date_to = f"{start:04d}-01-01", f"{end:04d}-12-31"
            working = working.replace(year_range.group(0), " ")
        else:
            single_year = re.search(r"(?:民國)?(\d{2,4})年(?!度)", working)
            if single_year:
                year = _chat_year(single_year.group(1))
                date_from, date_to = f"{year:04d}-01-01", f"{year:04d}-12-31"
                working = working.replace(single_year.group(0), " ")
    except (TypeError, ValueError):
        return {}, "請確認日期，例如：`民國112年至115年`或`2023-01-01至2026-12-31`。"
    if date_from and date_to and date_from > date_to:
        return {}, "查詢的起始日期晚於結束日期，請重新確認期間。"

    court = ""
    court_match = _CHAT_FULL_COURT_RE.search(working)
    if court_match:
        court = _normalise_chat_court(court_match.group(1))
        working = working.replace(court_match.group(0), " ", 1)
    else:
        alias_match = _CHAT_COURT_ALIAS_RE.search(working)
        if alias_match:
            court = _normalise_chat_court(alias_match.group(0))
            working = working.replace(alias_match.group(0), " ", 1)

    judge = ""
    judge_match = re.search(r"([一-鿿○〇]{2,4})\s*(?:法官|審判長)", working)
    if not judge_match:
        judge_match = re.search(
            r"(?:法官|審判長)\s*([一-鿿○〇]{2,4})(?=\s|[，,、；;。]|的|$)",
            working,
        )
    if judge_match:
        candidate = re.sub(r"\s+", "", judge_match.group(1))
        if candidate not in _CHAT_JUDGE_GENERIC and "某" not in candidate:
            judge = candidate
        working = working.replace(judge_match.group(0), " ", 1)

    offense = ""
    explicit_offense = re.search(
        r"(?:案由|罪名)\s*(?:為|是|[:：])?\s*"
        r"([一-鿿A-Za-z0-9、，及與暨・\-]{1,50}?)(?=\s*(?:案件|案|，|,|。|；|;|$))",
        working,
    )
    if explicit_offense:
        offense = explicit_offense.group(1).strip(" ，,。；;")
        working = working.replace(explicit_offense.group(0), " ", 1)
    if not offense:
        residual = re.sub(
            r"(?:關於|有關|相關|刑事|民事|案件|裁判|判決|資料|結果|樣本|的|案)",
            " ",
            working,
        )
        residual = re.sub(r"[：:，,。；;？?（）()\[\]{}]", " ", residual)
        residual = re.sub(r"\s+", " ", residual).strip()
        if residual and residual not in {"趨勢", "量刑", "法院", "法官"} and len(residual) <= 60:
            offense = residual

    filters = {
        "court": court,
        "judge": judge,
        "offense": offense,
        "date_from": date_from,
        "date_to": date_to,
    }
    if not any((court, judge, offense)):
        return filters, (
            "請至少提供法院、法官或案由其中一項。\n"
            "例如：`查臺灣花蓮地方法院王小明法官詐欺案件，民國112年至115年的量刑趨勢`"
        )
    return filters, ""


def _format_month_value(value: Any) -> str:
    if value is None:
        return "—"
    number = float(value)
    return f"{int(number) if number.is_integer() else number:g} 個月"


def _format_stat_group(label: str, group: dict[str, Any]) -> str:
    count = int(group.get("count") or 0)
    if not count:
        return f"- {label}：沒有可統計刑期"
    return (
        f"- {label}：{count} 筆；中位數 {_format_month_value(group.get('median_months'))}；"
        f"四分位 {_format_month_value(group.get('q1_months'))}–{_format_month_value(group.get('q3_months'))}；"
        f"範圍 {_format_month_value(group.get('min_months'))}–{_format_month_value(group.get('max_months'))}"
    )


def format_sentencing_trend_chat_result(result: dict[str, Any]) -> str:
    """Render the same source-bound result used by the web UI for chat."""

    filters = dict(result.get("filters") or {})
    judge_filter_label = (
        "參與判決法官"
        if filters.get("judge_scope") == "participating"
        else "末位列名法官"
    )
    conditions = [
        value for value in (
            f"法院：{filters.get('court')}" if filters.get("court") else "",
            f"{judge_filter_label}：{filters.get('judge')}" if filters.get("judge") else "",
            f"案由：{filters.get('offense')}" if filters.get("offense") else "",
            (
                f"期間：{format_roc_date(filters.get('date_from')) or '不限'} 至 {format_roc_date(filters.get('date_to')) or '不限'}"
                if filters.get("date_from") or filters.get("date_to") else ""
            ),
        ) if value
    ]
    lines = [
        "⚖️ **法官量刑與判決趨勢**",
        "條件｜" + "｜".join(conditions),
        "",
        (
            f"可核對樣本：{int(result.get('eligible_count') or 0)} 筆"
            f"（本機 {int(result.get('local_eligible_count') or 0)}；"
            f"MCP 官方全文核實 {int(result.get('mcp_verified_count') or 0)}）"
        ),
        f"本機候選 {int(result.get('candidate_count') or 0)}；排除未通過品質閘門 {int(result.get('excluded_count') or 0)}。",
        "",
        _format_stat_group("個別宣告刑（含完整附表）", dict(result.get("statistics", {}).get("declared_terms") or {})),
        _format_stat_group("最後定應執行刑", dict(result.get("statistics", {}).get("execution_terms") or {})),
    ]
    eligible = [item for item in result.get("items") or [] if item.get("statistics_eligible")]
    if eligible:
        lines.extend(["", "**可核對裁判**"])
        for index, item in enumerate(eligible[:5], 1):
            judges = "、".join(item.get("participating_judges") or [])
            last_listed = str(item.get("last_listed_judge") or "").strip()
            title = " ".join(
                part for part in (
                    str(item.get("court") or "").strip(),
                    str(item.get("case_number") or item.get("jid") or "").strip(),
                ) if part
            )
            detail = []
            declared = list(item.get("sentences") or []) + list(item.get("appendix_sentences") or [])
            if declared:
                detail.append("宣告刑：" + "；".join(str(entry.get("text") or "") for entry in declared[:3]))
            if item.get("execution_sentence"):
                detail.append("定執行刑：" + str(item["execution_sentence"].get("text") or ""))
            lines.append(
                f"{index}. {title}｜{item.get('judgment_date_display') or format_roc_date(item.get('judgment_date')) or '日期未載'}｜"
                f"參與判決法官：{judges or '簽署區未載'}｜"
                f"末位列名法官：{last_listed or '無法確認'}"
            )
            if detail:
                lines.append("   " + "；".join(detail))
            source_url = str(item.get("source_url") or "").strip()
            if _official_judgment_url(source_url):
                lines.append(f"   官方來源：{source_url}")
    else:
        lines.extend(["", "沒有裁判同時通過官方來源、簽署法官、主文刑度及附表完整性核對；MAGI 不會用未核實候選推算量刑。"])
    mcp = dict(result.get("mcp") or {})
    if mcp.get("status") == "unavailable":
        lines.append("MCP 本輪不可用；以上只反映本機可核對資料，未假裝完成外部查詢。")
    elif mcp.get("items") and not result.get("mcp_verified_count"):
        lines.append(f"MCP 找到 {len(mcp.get('items') or [])} 筆候選，但本輪沒有候選通過官方全文複核，因此未納入統計。")
    lines.extend(["", "資料品質：MCP 只負責擴充候選；沒有官方 JID、司法院網址、全文、簽署區或完整附表的裁判一律不納入統計。"])
    return "\n".join(lines)


def _matches_filters(
    item: dict[str, Any],
    *,
    court: str,
    judge: str,
    offense: str,
    date_from: str,
    date_to: str,
    judge_scope: str = "last_listed",
) -> bool:
    return not _filter_exclusion_codes(
        item,
        court=court,
        judge=judge,
        offense=offense,
        date_from=date_from,
        date_to=date_to,
        judge_scope=judge_scope,
    )


def _filter_exclusion_codes(
    item: dict[str, Any],
    *,
    court: str,
    judge: str,
    offense: str,
    date_from: str,
    date_to: str,
    judge_scope: str = "last_listed",
) -> list[str]:
    codes: list[str] = []
    if judge:
        if judge_scope == "participating":
            matched_judge = any(
                _compact(entry.get("name")) == _compact(judge)
                for entry in item.get("judges") or []
            )
        else:
            matched_judge = _compact(item.get("last_listed_judge")) == _compact(judge)
        if not matched_judge:
            codes.append("judge_mismatch")
    if court and _compact(court) not in _compact(item.get("court")):
        codes.append("court_mismatch")
    if offense and _compact(offense) not in _compact(str(item.get("issue") or "") + str(item.get("main_text") or "")):
        codes.append("offense_mismatch")
    judgment_date = str(item.get("judgment_date") or "")
    if (date_from or date_to) and not judgment_date:
        codes.append("judgment_date_missing")
    elif date_from and judgment_date < date_from:
        codes.append("date_before_range")
    elif date_to and judgment_date > date_to:
        codes.append("date_after_range")
    return codes


def _evaluate_mcp_candidates(
    candidates: list[dict[str, Any]],
    *,
    existing_jids: set[str],
    court: str,
    judge: str,
    offense: str,
    date_from: str,
    date_to: str,
    judge_scope: str,
) -> list[dict[str, Any]]:
    evaluations: list[dict[str, Any]] = []
    seen = set(existing_jids)
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        verified = validate_official_judgment_candidate(raw)
        codes = list(verified["exclusion_codes"])
        jid = str(verified.get("jid") or "")
        parsed: dict[str, Any] | None = None
        if jid and jid in seen:
            codes.append("duplicate_local_jid")
        if verified["ok"] and "duplicate_local_jid" not in codes:
            parsed = parse_sentencing_judgment(
                {
                    "id": None,
                    "jid": jid,
                    "court_name": raw.get("court"),
                    "case_number": raw.get("title") or raw.get("citation_text"),
                    "case_type": raw.get("case_reason"),
                    "judgment_date": verified.get("judgment_date"),
                    "full_text": verified.get("full_text"),
                    "source_url": verified.get("source_url"),
                    "source": "legaltech_taiwan_law_mcp_verified_official",
                    "external_verified": True,
                }
            )
            codes.extend(code for code in parsed.get("exclusion_codes") or [] if code not in codes)
            codes.extend(
                code for code in _filter_exclusion_codes(
                    parsed,
                    court=court,
                    judge=judge,
                    offense=offense,
                    date_from=date_from,
                    date_to=date_to,
                    judge_scope=judge_scope,
                ) if code not in codes
            )
        included = parsed is not None and not codes
        if included:
            seen.add(jid)
        evaluations.append({
            "raw": raw,
            "parsed": parsed,
            "included": included,
            "exclusion_codes": codes,
        })
    return evaluations


def _public_mcp_candidates(
    evaluations: list[dict[str, Any]],
    *,
    requested_judge: str,
) -> list[dict[str, Any]]:
    """Project MCP results to a bounded, useful, no-full-text UI contract."""
    public: list[dict[str, Any]] = []
    for evaluation in evaluations:
        raw = evaluation["raw"]
        parsed = evaluation.get("parsed") or {}
        jid = str(raw.get("jid") or raw.get("doc_id") or "").strip()
        full_text_available = bool(str(raw.get("full_text") or "").strip())
        included = evaluation.get("included") is True
        exclusion_codes = list(evaluation.get("exclusion_codes") or [])
        exclusion_reasons = [
            _public_exclusion_reason(code, requested_judge=requested_judge, item=parsed)
            for code in exclusion_codes
        ]
        if included:
            state = "verified_official_fulltext"
            note = "官方全文、簽署區、主文刑度與查詢條件均已核對，已納入統計。"
        elif full_text_available:
            state = "official_fulltext_not_eligible"
            note = "已取得官方全文，但簽署區、主文刑度、附表或查詢條件未完整通過，未納入統計。"
        else:
            state = "discovery_candidate"
            note = "MCP 已找到公開候選；尚未取得可獨立核對的官方全文，未納入統計。"
        public.append(
            {
                "title": str(raw.get("title") or raw.get("citation_text") or "待核對裁判").strip()[:240],
                "court": str(raw.get("court") or "").strip()[:80],
                "case_reason": str(raw.get("case_reason") or "").strip()[:100],
                "judgment_date": str(raw.get("judgment_date") or "").strip()[:20],
                "judgment_date_display": format_roc_date(raw.get("judgment_date")),
                "source_url": official_judgment_page_url(
                    jid,
                    raw.get("source_url") or raw.get("url"),
                ),
                "full_text_available": full_text_available,
                "included_in_statistics": included,
                "verification_state": state,
                "verification_note": note,
                "exclusion_codes": exclusion_codes,
                "exclusion_reasons": exclusion_reasons,
            }
        )
    return public


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    pos = (len(values) - 1) * ratio
    lower = int(pos)
    upper = min(len(values) - 1, lower + 1)
    fraction = pos - lower
    return round(values[lower] * (1 - fraction) + values[upper] * fraction, 2)


def _stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    declared = [float(s["months"]) for item in items for s in item["sentences"] + item["appendix_sentences"] if s.get("months") is not None]
    executions = [float(item["execution_sentence"]["months"]) for item in items if item.get("execution_sentence") and item["execution_sentence"].get("months") is not None]
    def group(values: list[float]) -> dict[str, Any]:
        return {
            "count": len(values),
            "median_months": round(statistics.median(values), 2) if values else None,
            "q1_months": _percentile(values, .25),
            "q3_months": _percentile(values, .75),
            "min_months": min(values) if values else None,
            "max_months": max(values) if values else None,
        }
    return {"declared_terms": group(declared), "execution_terms": group(executions)}


def search_sentencing_trends(
    *,
    court: str = "",
    judge: str = "",
    offense: str = "",
    date_from: str = "",
    date_to: str = "",
    judge_scope: str = "last_listed",
    include_mcp: bool = True,
    limit: int = 100,
    connector: Callable[[], Any] = _osc_web_connect,
    mcp_search: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    court, judge, offense = (str(x or "").strip() for x in (court, judge, offense))
    date_from = _normalized_iso_date_filter(date_from, "判決日起")
    date_to = _normalized_iso_date_filter(date_to, "判決日迄")
    if date_from and date_to and date_from > date_to:
        raise ValueError("判決日起不得晚於判決日迄。")
    judge_scope = str(judge_scope or "last_listed").strip().lower()
    if judge_scope not in {"last_listed", "participating"}:
        raise ValueError("法官條件不正確，請選擇末位列名法官或任一參與判決法官。")
    if not any((court, judge, offense)):
        raise ValueError("請至少輸入法院、法官或案由其中一項。")
    limit = max(1, min(300, int(limit or 100)))
    sql = """
        SELECT id,jid,court_name,case_number,case_type,judgment_date,full_text,source_url
        FROM court_judgments
        WHERE TRIM(COALESCE(full_text,''))<>''
          AND (%s='' OR REPLACE(court_name,'台','臺') LIKE %s OR REPLACE(LEFT(full_text,500),'台','臺') LIKE %s)
          AND (%s='' OR full_text LIKE %s)
          AND (%s='' OR full_text LIKE %s OR case_type LIKE %s)
          AND (%s='' OR judgment_date >= %s)
          AND (%s='' OR judgment_date <= %s)
        ORDER BY judgment_date DESC,id DESC LIMIT %s
    """
    court_like = f"%{_compact(court)}%"
    rows: list[dict[str, Any]] = []
    conn, _cfg = connector()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(sql, (court, court_like, court_like, judge, f"%{judge}%", offense, f"%{offense}%", f"%{offense}%", date_from, date_from, date_to, date_to, limit))
        rows = list(cur.fetchall() or [])
    finally:
        cur.close()
        conn.close()
    parsed = [parse_sentencing_judgment(row) for row in rows]
    parsed = [
        item for item in parsed
        if _matches_filters(
            item,
            court=court,
            judge=judge,
            offense=offense,
            date_from=date_from,
            date_to=date_to,
            judge_scope=judge_scope,
        )
    ]
    local_eligible = [item for item in parsed if item["statistics_eligible"]]
    external: list[dict[str, Any]] = []
    verified_external: list[dict[str, Any]] = []
    mcp_evaluations: list[dict[str, Any]] = []
    result: dict[str, Any] = {}
    mcp_status = "not_requested"
    if include_mcp and mcp_search is None:
        mcp_search = search_public_judgment_candidates
    if include_mcp and mcp_search:
        # The provider has a dedicated court parameter.  Its discovery index
        # reliably exposes the case reason, but judge names usually appear
        # only after the official full text is fetched.  Including a judge in
        # the first-stage query therefore produced an empty MCP panel.  Search
        # by offense first, then enforce the requested judge against the
        # independently parsed signature block in ``_verified_mcp_items``.
        # A judge-only query retains the name because no broader legal subject
        # was supplied by the user.
        query = (
            " ".join(x for x in (offense, "量刑") if x)
            if offense
            else (" ".join(x for x in (judge, "法官", "量刑") if x) or court)
        )
        try:
            result = mcp_search(query, court=court, case_type="刑事", limit=10, fulltext_limit=10)
            mcp_status = "ok" if result.get("success") else "unavailable"
            # Keep the verification budget aligned with the MCP request.  A
            # smaller second slice silently discarded official candidates and
            # made sparse local datasets look even thinner than necessary.
            external = list(result.get("items") or [])[:10] if result.get("success") else []
            mcp_evaluations = _evaluate_mcp_candidates(
                external,
                existing_jids={str(item.get("jid") or "") for item in parsed},
                court=court,
                judge=judge,
                offense=offense,
                date_from=date_from,
                date_to=date_to,
                judge_scope=judge_scope,
            )
            verified_external = [
                evaluation["parsed"]
                for evaluation in mcp_evaluations
                if evaluation.get("included") is True
            ]
        except Exception:
            mcp_status = "unavailable"
    eligible = local_eligible + verified_external
    displayed_items = parsed + verified_external
    public_mcp_items = _public_mcp_candidates(
        mcp_evaluations,
        requested_judge=judge,
    )
    return {
        "ok": True,
        "filters": {
            "court": court,
            "judge": judge,
            "judge_scope": judge_scope,
            "offense": offense,
            "date_from": date_from,
            "date_to": date_to,
        },
        "candidate_count": len(rows),
        "matched_count": len(displayed_items),
        "eligible_count": len(eligible),
        "local_eligible_count": len(local_eligible),
        "mcp_verified_count": len(verified_external),
        "excluded_count": len(parsed) - len(local_eligible),
        "appendix_complete_count": sum(1 for item in displayed_items if item["appendix_referenced"] and item["appendix_complete"]),
        "appendix_incomplete_count": sum(1 for item in displayed_items if item["appendix_referenced"] and not item["appendix_complete"]),
        "statistics": _stats(eligible),
        "items": displayed_items,
        "mcp": {
            "status": mcp_status,
            "items": public_mcp_items,
            "candidate_count": len(public_mcp_items),
            "fulltext_count": sum(1 for item in public_mcp_items if item["full_text_available"]),
            "verified_count": len(verified_external),
            "included_in_statistics": bool(verified_external),
            "source": str(result.get("source") or "") if include_mcp and mcp_search else "",
            "source_label": str(result.get("source_label") or "") if include_mcp and mcp_search else "",
        },
        "notice": "本機裁判庫與 MCP 會共同搜尋；法官條件預設比對裁判簽署區末位列名法官。MCP 裁判必須具官方 JID、司法院網址與完整全文，並重新通過簽署區、主文刑度及附表核對後，才會納入統計。",
    }
