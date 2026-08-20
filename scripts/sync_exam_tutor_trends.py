#!/usr/bin/env python3
"""Refresh the exam-tutor trend page from cited public sources via NVIDIA only.

The job deliberately has no local-model fallback.  A failed fetch, unavailable
NVIDIA route, invalid JSON, or uncited topic leaves the previous snapshot in
place and writes a receipt explaining why no new analysis was published.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
HEADERS = {"User-Agent": "MAGI-private-exam-trend/1.0 (public legal source verification)"}
SUBJECTS = {
    "憲法", "行政法", "刑法", "刑事訴訟法", "民法", "民事訴訟法",
    "民法（總則）", "民法（債）", "民法（物權）", "民法（親屬）", "民法（繼承）",
    "商事法與金融法", "公司法", "保險法", "證券交易法", "勞動法",
    "財稅法", "國際公法", "國際私法", "少年事件處理法", "其他法律爭議",
}

DETAIL_SOURCES = {"full_text", "legislative_record", "academic_fulltext"}


def _document_text(payload: bytes, content_type: str, url: str) -> str:
    """Extract readable text without treating a PDF as mojibake HTML."""
    is_pdf = payload.startswith(b"%PDF") or "application/pdf" in content_type.lower() or url.lower().endswith(".pdf")
    if is_pdf:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(payload))
        return re.sub(
            r"\s+",
            " ",
            " ".join((page.extract_text() or "") for page in reader.pages[:80]),
        ).strip()
    parser = _VisibleText()
    parser.feed(payload.decode("utf-8", errors="replace"))
    return parser.text()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON root must be an object: {path}")
    return payload


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False) as handle:
        handle.write(data)
        temp_path = Path(handle.name)
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, path)


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth and data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.parts)).strip()


def fetch_sources(config: dict[str, Any], *, timeout: int = 35) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    session = requests.Session()
    session.headers.update(HEADERS)
    for source in config.get("sources") or []:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        fetch_url = str(source.get("fetch_url") or url).strip()
        source_id = str(source.get("source_id") or "").strip()
        if not source_id or not url.startswith("https://") or not fetch_url.startswith("https://"):
            continue
        row = {
            "source_id": source_id,
            "name": str(source.get("name") or source_id),
            "tier": int(source.get("tier") or 0),
            "source_type": str(source.get("source_type") or ""),
            "detail_level": str(source.get("detail_level") or "index"),
            "url": url,
            "subjects": source.get("subjects") or [],
            "policy": str(source.get("policy") or ""),
            "fetched_at": now_iso(),
        }
        try:
            transport = "python_requests"
            try:
                response = session.get(fetch_url, timeout=timeout, allow_redirects=True)
                response.raise_for_status()
                payload = response.content
                if not payload or len(payload) > 8_000_000:
                    raise RuntimeError("source returned an empty or oversized document")
                content_type = str(response.headers.get("Content-Type") or "")
                resolved_url = str(response.url)
            except requests.exceptions.RequestException as requests_error:
                # Several Taiwan government sites use certificate chains or TLS
                # renegotiation that Python 3.14/OpenSSL rejects even though the
                # macOS trust stack validates them.  Use the system curl trust
                # stack as a verified HTTPS fallback; never pass --insecure.
                command = [
                    "/usr/bin/curl",
                    "--silent",
                    "--show-error",
                    "--location",
                    "--fail",
                    "--proto", "=https",
                    "--connect-timeout", "12",
                    "--max-time", str(timeout),
                    "--max-filesize", "8000000",
                    "--user-agent", HEADERS["User-Agent"],
                    fetch_url,
                ]
                curl = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=timeout + 5,
                    check=False,
                )
                if curl.returncode != 0:
                    detail = curl.stderr.decode("utf-8", errors="replace").strip()[:240]
                    raise RuntimeError(f"requests={requests_error}; system_curl={detail or curl.returncode}")
                if not curl.stdout or len(curl.stdout) > 8_000_000:
                    raise RuntimeError("system curl returned an empty or oversized document")
                payload = curl.stdout
                content_type = "application/pdf" if payload.startswith(b"%PDF") else "text/html"
                resolved_url = fetch_url
                transport = "macos_system_curl_verified_tls"
            text = _document_text(payload, content_type, resolved_url)
            if len(text) < 80:
                raise RuntimeError("page has too little readable text")
            row.update({
                "fetch_state": "ok",
                "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "excerpt": text[:40_000],
                "resolved_url": resolved_url,
                "fetch_transport": transport,
            })
        except Exception as exc:
            row.update({"fetch_state": "failed", "error": str(exc)[:300], "excerpt": ""})
        rows.append(row)
    return rows


def _extract_json_object(value: str) -> dict[str, Any]:
    text = str(value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("NVIDIA response does not contain a JSON object")
    payload = json.loads(text[start:end + 1])
    if not isinstance(payload, dict):
        raise RuntimeError("NVIDIA response root is not an object")
    return payload


def fetch_mcp_law_evidence(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve every proposed statutory article against MAGI's Taiwan Law MCP."""
    from api.osc.legaltech_taiwan_law_mcp import search_laws_via_legaltech

    requested: set[tuple[str, int]] = set()
    for item in analysis.get("items") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("event_kind") or "").strip().lower()
        title = str(item.get("title") or "")
        if kind not in {"amendment", "new_law"} and not re.search(r"修正|增訂|刪除|制定", title):
            continue
        law_name = str(item.get("law_name") or "").strip()
        if not law_name:
            continue
        provisions = [str(value or "").strip() for value in item.get("provisions") or [] if str(value or "").strip()]
        for provision in provisions:
            match = re.match(r"^(.+?)第\s*[0-9〇零一二三四五六七八九十百千]+\s*條", provision)
            provision_law = str(match.group(1) if match else law_name).strip()
            for number in _article_numbers([provision]):
                requested.add((provision_law, number))
        if not provisions:
            for number in _article_numbers([title]):
                requested.add((law_name, number))
    rows: list[dict[str, Any]] = []
    for law_name, number in sorted(requested):
        result = search_laws_via_legaltech(law_name, article_number=str(number), limit=3)
        if not result.get("success"):
            continue
        for law in result.get("results") or []:
            if not isinstance(law, dict) or str(law.get("name") or "").strip() != law_name:
                continue
            article = next((row for row in law.get("articles") or [] if isinstance(row, dict) and number in _article_numbers([str(row.get("number") or "")])), None)
            if not article:
                continue
            official_text = re.sub(r"\s+", " ", str(article.get("text") or "")).strip()
            url = str(law.get("url") or result.get("source_url") or "").strip()
            if not official_text or not url.startswith("https://"):
                continue
            pcode = re.sub(r"[^A-Za-z0-9]+", "", str(law.get("pcode") or "law"))
            rows.append({
                "source_id": f"taiwan_law_mcp_{pcode}_{number}",
                "name": f"全國法規資料庫・{law_name}第 {number} 條現行本文",
                "tier": 1,
                "source_type": "official_law_mcp",
                "detail_level": "full_text",
                "url": url,
                "subjects": [law_name],
                "policy": "由 MAGI Taiwan Law MCP 逐條查得；用於核對現行本文與法典分類，不代替立法院修正說明。",
                "fetched_at": now_iso(),
                "fetch_state": "ok",
                "content_sha256": hashlib.sha256(official_text.encode("utf-8")).hexdigest(),
                "excerpt": official_text,
                "law_name": law_name,
                "article_number": number,
                "official_text": official_text,
                "resolved_url": url,
                "fetch_transport": "magi_taiwan_law_mcp",
            })
            break
    return rows


def build_mcp_audit_prompt(candidate: dict[str, Any], mcp_sources: list[dict[str, Any]], source_prompt: str) -> str:
    evidence = [{key: row.get(key) for key in (
        "source_id", "name", "tier", "source_type", "detail_level", "url", "law_name",
        "article_number", "official_text", "excerpt",
    )} for row in mcp_sources]
    return (
        source_prompt
        + "\n\n第二階段法條稽核：下列 mcp_evidence 是 MAGI 直接向全國法規資料庫逐條查得的現行本文。"
        "請重新輸出完整 JSON，修正 candidate 中任何法典編別、條號或條文內容錯誤。"
        "每個修法 amendment_details 除立法全文／議事錄外，source_ids 還必須引用對應的 taiwan_law_mcp_*。"
        "MCP 現行本文與三讀後文字不同時，previous_rule 寫現行本文，new_rule 只依議事錄；不得把三讀通過寫成已公布生效。"
        "沒有對應 MCP 條文或逐條立法來源的修法項目必須刪除。\n"
        f"candidate：{json.dumps(candidate, ensure_ascii=False)}\n"
        f"mcp_evidence：{json.dumps(evidence, ensure_ascii=False)}"
    )


def build_prompt(sources: list[dict[str, Any]], seed: dict[str, Any]) -> str:
    usable = [row for row in sources if row.get("fetch_state") == "ok"]
    prior = []
    for item in seed.get("items") or []:
        if not isinstance(item, dict):
            continue
        prior.append({
            key: item.get(key)
            for key in (
                "uid", "title", "subject", "status", "event_date", "fact_summary",
                "issue_points", "why_exam_relevant", "answer_outline", "related_keywords",
                "source_ids", "risk_note", "event_kind", "law_name", "code_division",
                "provisions", "amendment_details", "controversies", "viewpoints",
            )
        })
    evidence = []
    for row in usable:
        excerpt = str(row.get("excerpt") or "")
        detail_level = str(row.get("detail_level") or "index")
        source_type = str(row.get("source_type") or "")
        if detail_level == "academic_fulltext":
            # Preserve the introduction and conclusions; long research PDFs
            # otherwise crowd the actual statutory evidence out of context.
            excerpt = excerpt if len(excerpt) <= 14_000 else excerpt[:4_000] + " …〔中段省略〕… " + excerpt[-10_000:]
        elif source_type == "official_judgment":
            excerpt = excerpt[:10_000]
        elif detail_level == "legislative_record":
            excerpt = excerpt[:14_000]
        elif detail_level == "full_text":
            excerpt = excerpt[:12_000]
        else:
            excerpt = excerpt[:6_000]
        evidence.append({
            **{key: row.get(key) for key in (
                "source_id", "name", "tier", "source_type", "detail_level", "url", "subjects", "policy",
                "fetched_at", "content_sha256",
            )},
            "excerpt": excerpt,
        })
    return (
        "請依下列已抓取的台灣法律公開來源，更新法律國考『趨勢分析』資料。"
        "這不是預測命中率，不得產生機率；也不得產生、修改或暗示任何考題評分尺。\n"
        "規則：\n"
        "1. 只可使用 evidence 內文；不得補寫內文沒有的日期、裁判字號、法條號、修法內容、學者姓名或結論。\n"
        "2. status=verified 至少要引用一個 tier=1 source_id；新聞、律師貼文、Facebook 或專業觀察只能 status=radar。\n"
        "3. source_ids 必須完全來自 evidence；每項至少一個。\n"
        "4. 保留仍具時效的既有項目，刪除已失去時效或無法由本次來源支持者；最多 18 項。\n"
        "5. attention_level 只能 high/medium/low，它表示複習注意度，不表示命題機率。\n"
        "6. subject 從常見台灣法律考科中選一個；所有文字使用台灣繁體中文。\n"
        "7. answer_outline 只提供答題分析順序，不得虛構官方爭點或配分。\n"
        "8. event_kind 只能 judgment/amendment/new_law/practice。修法或新法必須逐條填 amendment_details，"
        "而且每一筆須有 provision、new_rule、practical_effect 與自己的 source_ids；只引用 detail_level=index 的索引頁一律不得輸出。\n"
        "9. controversies 的每個立場及 viewpoints 的每個見解都要有自己的 source_ids。學者見解只能來自 source_type=academic_commentary 且 detail_level=academic_fulltext，"
        "並寫明 attribution；實務見解只能來自官方裁判全文。找不到就留空，不得模擬正反說。\n"
        "10. 民法須依法條編別分類：1-152總則、153-756債、757-966物權、967-1137親屬、1138-1225繼承。"
        "例如第1223條必須是民法（繼承），絕不可連結第758條或登記生效。\n"
        "只輸出 JSON 物件，格式：{\"items\":[{\"uid\":\"...\",\"title\":\"...\","
        "\"subject\":\"...\",\"status\":\"verified|radar\",\"attention_level\":\"high|medium|low\","
        "\"event_kind\":\"judgment|amendment|new_law|practice\",\"law_name\":\"...\","
        "\"code_division\":\"...\",\"provisions\":[\"法律第N條\"],"
        "\"event_date\":\"YYYY-MM-DD 或空字串\",\"fact_summary\":\"...\","
        "\"issue_points\":[\"...\"],\"why_exam_relevant\":\"...\","
        "\"answer_outline\":[\"...\"],\"related_keywords\":[\"...\"],"
        "\"amendment_details\":[{\"provision\":\"...\",\"previous_rule\":\"...\",\"new_rule\":\"...\","
        "\"practical_effect\":\"...\",\"source_ids\":[\"...\"]}],"
        "\"controversies\":[{\"question\":\"...\",\"positions\":[{\"label\":\"...\",\"statement\":\"...\","
        "\"attribution\":\"...\",\"source_ids\":[\"...\"]}],\"exam_tip\":\"...\"}],"
        "\"viewpoints\":[{\"kind\":\"practice|academic|concurring|dissenting|institutional\","
        "\"attribution\":\"...\",\"statement\":\"...\",\"source_ids\":[\"...\"]}],"
        "\"source_ids\":[\"...\"],\"risk_note\":\"...\"}]}。\n\n"
        f"既有項目：{json.dumps(prior, ensure_ascii=False)}\n\n"
        f"evidence：{json.dumps(evidence, ensure_ascii=False)}"
    )


def _source_ids(values: Any, registry: dict[str, dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(
        str(value or "").strip() for value in values or []
        if str(value or "").strip() in registry
    ))


def _chinese_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    digits = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100, "千": 1000}
    total = current = 0
    for char in value:
        if char in digits:
            current = digits[char]
        elif char in units:
            total += (current or 1) * units[char]
            current = 0
        else:
            return None
    return total + current


def _article_numbers(values: list[str]) -> list[int]:
    numbers: list[int] = []
    for value in values:
        for token in re.findall(r"第\s*([0-9〇零一二三四五六七八九十百千]+)\s*條", value):
            number = _chinese_number(token)
            if number is not None:
                numbers.append(number)
    return list(dict.fromkeys(numbers))


def _civil_division(number: int) -> tuple[str, str] | None:
    for lower, upper, division, subject in (
        (1, 152, "總則編", "民法（總則）"),
        (153, 756, "債編", "民法（債）"),
        (757, 966, "物權編", "民法（物權）"),
        (967, 1137, "親屬編", "民法（親屬）"),
        (1138, 1225, "繼承編", "民法（繼承）"),
    ):
        if lower <= number <= upper:
            return division, subject
    return None


def validate_analysis(
    analysis: dict[str, Any], *, sources: list[dict[str, Any]], model: str, generated_at: str
) -> dict[str, Any]:
    registry = {
        str(row.get("source_id") or ""): row
        for row in sources
        if row.get("fetch_state") == "ok" and str(row.get("source_id") or "")
    }
    if not registry:
        raise RuntimeError("no fetched source is available for validation")
    output_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in analysis.get("items") or []:
        if not isinstance(raw, dict):
            continue
        uid = re.sub(r"[^a-z0-9._-]+", "-", str(raw.get("uid") or "").strip().lower()).strip("-")[:120]
        title = str(raw.get("title") or "").strip()[:180]
        if not uid or not title or uid in seen:
            continue
        source_ids = _source_ids(raw.get("source_ids"), registry)
        if not source_ids:
            continue
        status = str(raw.get("status") or "radar").strip().lower()
        if status not in {"verified", "radar"}:
            status = "radar"
        if status == "verified" and not any(int(registry[value].get("tier") or 0) == 1 for value in source_ids):
            status = "radar"
        date = str(raw.get("event_date") or "").strip()
        if date and not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", date):
            date = ""
        event_kind = str(raw.get("event_kind") or "").strip().lower()
        if event_kind not in {"judgment", "amendment", "new_law", "practice"}:
            if re.search(r"修正|增訂|刪除", title):
                event_kind = "amendment"
            elif re.search(r"制定|新法", title):
                event_kind = "new_law"
            elif re.search(r"判決|判字|裁定", title):
                event_kind = "judgment"
            else:
                event_kind = "practice"
        law_name = str(raw.get("law_name") or "").strip()[:120]
        provisions = [str(value or "").strip()[:120] for value in raw.get("provisions") or [] if str(value or "").strip()][:16]
        subject = str(raw.get("subject") or "其他法律爭議").strip()
        code_division = str(raw.get("code_division") or "").strip()[:80]
        civil_references = [value for value in provisions if re.match(r"^民法第", value)]
        if re.search(r"民法第", title):
            civil_references.append(title)
        civil_numbers = _article_numbers(civil_references) if law_name == "民法" or civil_references else []
        civil_divisions = {_civil_division(number) for number in civil_numbers if _civil_division(number)}
        if len(civil_divisions) == 1:
            code_division, subject = next(iter(civil_divisions))
        if subject not in SUBJECTS:
            subject = "其他法律爭議"
        attention = str(raw.get("attention_level") or "medium").strip().lower()
        if attention not in {"high", "medium", "low"}:
            attention = "medium"
        def strings(key: str, cap: int) -> list[str]:
            return [str(value or "").strip()[:400] for value in raw.get(key) or [] if str(value or "").strip()][:cap]
        issue_points = strings("issue_points", 6)
        outline = strings("answer_outline", 6)
        if not issue_points or not outline:
            continue
        def detailed_claim_sources(values: Any) -> list[str]:
            return [
                source_id for source_id in _source_ids(values, registry)
                if str(registry[source_id].get("detail_level") or "index") in DETAIL_SOURCES
            ]

        amendment_details: list[dict[str, Any]] = []
        for detail in raw.get("amendment_details") or []:
            if not isinstance(detail, dict):
                continue
            claim_sources = detailed_claim_sources(detail.get("source_ids"))
            provision = str(detail.get("provision") or "").strip()[:160]
            previous_rule = str(detail.get("previous_rule") or "").strip()[:1_000]
            new_rule = str(detail.get("new_rule") or "").strip()[:1_000]
            practical_effect = str(detail.get("practical_effect") or "").strip()[:1_000]
            provision_numbers = _article_numbers([provision])
            mcp_matches = [source_id for source_id, row in registry.items() if (
                str(row.get("source_type") or "") == "official_law_mcp"
                and str(row.get("law_name") or "").strip() == law_name
                and int(row.get("article_number") or 0) in provision_numbers
            )]
            change_sources = [source_id for source_id in claim_sources if str(registry[source_id].get("source_type") or "") in {
                "official_legislative_record", "official_legislation_detail", "official_policy",
            }]
            if not mcp_matches or not change_sources or not provision or not new_rule or not practical_effect:
                continue
            if event_kind == "amendment" and not previous_rule:
                continue
            claim_sources = list(dict.fromkeys(claim_sources + mcp_matches))
            amendment_details.append({
                "provision": provision,
                "previous_rule": previous_rule,
                "new_rule": new_rule,
                "practical_effect": practical_effect,
                "official_current_text": str(registry[mcp_matches[0]].get("official_text") or "")[:2_500],
                "source_ids": claim_sources,
            })
        if event_kind in {"amendment", "new_law"} and not amendment_details:
            continue

        controversies: list[dict[str, Any]] = []
        for controversy in raw.get("controversies") or []:
            if not isinstance(controversy, dict):
                continue
            positions: list[dict[str, Any]] = []
            for position in controversy.get("positions") or []:
                if not isinstance(position, dict):
                    continue
                claim_sources = detailed_claim_sources(position.get("source_ids"))
                statement = str(position.get("statement") or "").strip()[:1_200]
                if not claim_sources or not statement:
                    continue
                positions.append({
                    "label": str(position.get("label") or "見解").strip()[:80],
                    "statement": statement,
                    "attribution": str(position.get("attribution") or "來源所載見解").strip()[:180],
                    "source_ids": claim_sources,
                })
            question = str(controversy.get("question") or "").strip()[:500]
            if question and len(positions) >= 2:
                controversies.append({
                    "question": question,
                    "positions": positions[:5],
                    "exam_tip": str(controversy.get("exam_tip") or "").strip()[:700],
                })

        viewpoints: list[dict[str, Any]] = []
        for viewpoint in raw.get("viewpoints") or []:
            if not isinstance(viewpoint, dict):
                continue
            kind = str(viewpoint.get("kind") or "").strip().lower()
            if kind not in {"practice", "academic", "concurring", "dissenting", "institutional"}:
                continue
            claim_sources = detailed_claim_sources(viewpoint.get("source_ids"))
            if kind == "academic":
                claim_sources = [source_id for source_id in claim_sources if (
                    str(registry[source_id].get("source_type") or "") == "academic_commentary"
                    and str(registry[source_id].get("detail_level") or "") == "academic_fulltext"
                )]
            elif kind in {"practice", "concurring", "dissenting"}:
                claim_sources = [source_id for source_id in claim_sources if "judgment" in str(registry[source_id].get("source_type") or "")]
            statement = str(viewpoint.get("statement") or "").strip()[:1_500]
            attribution = str(viewpoint.get("attribution") or "").strip()[:220]
            if not claim_sources or not statement or not attribution:
                continue
            viewpoints.append({
                "kind": kind,
                "attribution": attribution,
                "statement": statement,
                "source_ids": claim_sources,
            })

        complete_text = " ".join([
            title, str(raw.get("fact_summary") or ""), " ".join(issue_points), " ".join(outline),
            json.dumps(amendment_details, ensure_ascii=False),
        ])
        if 1223 in civil_numbers and re.search(r"不動產物權讓與|登記生效|善意取得|第\s*758\s*條", complete_text):
            continue
        nested_source_ids = [source_id for detail in amendment_details for source_id in detail["source_ids"]]
        nested_source_ids += [source_id for controversy in controversies for position in controversy["positions"] for source_id in position["source_ids"]]
        nested_source_ids += [source_id for viewpoint in viewpoints for source_id in viewpoint["source_ids"]]
        source_ids = list(dict.fromkeys(source_ids + nested_source_ids))
        seen.add(uid)
        output_items.append({
            "uid": uid,
            "title": title,
            "subject": subject,
            "status": status,
            "attention_level": attention,
            "event_kind": event_kind,
            "law_name": law_name,
            "code_division": code_division,
            "provisions": provisions,
            "event_date": date,
            "fact_summary": str(raw.get("fact_summary") or "").strip()[:1_500],
            "issue_points": issue_points,
            "why_exam_relevant": str(raw.get("why_exam_relevant") or "").strip()[:900],
            "answer_outline": outline,
            "related_keywords": strings("related_keywords", 12),
            "amendment_details": amendment_details[:8],
            "controversies": controversies[:6],
            "viewpoints": viewpoints[:10],
            "source_ids": source_ids,
            "source_url": str(registry[source_ids[0]].get("url") or ""),
            "analysis_state": (
                "source_audited_nvidia_reviewed"
                if str(raw.get("analysis_state") or "").startswith("source_audited")
                else "nvidia_verified"
            ),
            "analysis_engine": {
                "provider": "NVIDIA",
                "model": model,
                "local_model_fallback": False,
                "generated_at": generated_at,
            },
            "risk_note": str(raw.get("risk_note") or "趨勢只供安排複習，不代表命題機率。").strip()[:600],
        })
    if not output_items:
        raise RuntimeError("NVIDIA output contains no publishable cited item")
    source_registry = [{
        "source_id": source_id,
        "name": str(row.get("name") or source_id),
        "tier": int(row.get("tier") or 0),
        "source_type": str(row.get("source_type") or ""),
        "detail_level": str(row.get("detail_level") or "index"),
        "url": str(row.get("url") or ""),
        "fetched_at": str(row.get("fetched_at") or ""),
        "content_sha256": str(row.get("content_sha256") or ""),
    } for source_id, row in registry.items()]
    return {
        "schema_version": 2,
        "project_name": "預測",
        "ui_title": "趨勢分析",
        "generated_at": generated_at,
        "analysis_policy": {
            "nvidia_required_for_cross_source_analysis": True,
            "model": model,
            "local_model_fallback": False,
            "official_source_required_for_verified_status": True,
            "radar_is_not_prediction": True,
            "rubric_generation_allowed": False,
        },
        "source_registry": source_registry,
        "items": output_items[:18],
    }


def merge_source_audited_baseline(
    snapshot: dict[str, Any], *, baseline: dict[str, Any], sources: list[dict[str, Any]],
    model: str, generated_at: str,
) -> dict[str, Any]:
    """Prevent NVIDIA from deleting or rewriting facts already source-audited."""
    protected = [
        item for item in baseline.get("items") or []
        if isinstance(item, dict) and str(item.get("analysis_state") or "").startswith("source_audited")
    ]
    if not protected:
        return snapshot
    audited = validate_analysis(
        {"items": protected}, sources=sources, model=model, generated_at=generated_at,
    )
    expected_ids = {str(item.get("uid") or "") for item in protected}
    audited_ids = {str(item.get("uid") or "") for item in audited["items"]}
    if audited_ids != expected_ids:
        missing = sorted(expected_ids - audited_ids)
        raise RuntimeError(f"source-audited baseline failed current source/MCP verification: {missing}")
    protected_ids = audited_ids
    protected_provisions = {
        (str(item.get("law_name") or ""), number)
        for item in audited["items"]
        for number in _article_numbers([str(value or "") for value in item.get("provisions") or []])
    }
    retained: list[dict[str, Any]] = []
    for item in snapshot.get("items") or []:
        if str(item.get("uid") or "") in protected_ids:
            continue
        item_provisions = {
            (str(item.get("law_name") or ""), number)
            for number in _article_numbers([str(value or "") for value in item.get("provisions") or []])
        }
        if item_provisions & protected_provisions:
            continue
        retained.append(item)
    snapshot["items"] = (retained + audited["items"])[:18]
    snapshot["analysis_policy"]["source_audited_facts_model_editable"] = False
    snapshot["analysis_policy"]["source_audited_item_count"] = len(audited["items"])
    return snapshot


def run_refresh(*, config_path: Path, seed_path: Path, output_path: Path, receipt_path: Path) -> dict[str, Any]:
    from skills.bridge.nim_heavy import _model_allowed, run_nim_chat

    started_at = now_iso()
    config = load_json(config_path)
    baseline = load_json(seed_path)
    seed = load_json(output_path) if output_path.is_file() else baseline
    seed_items = [item for item in seed.get("items") or [] if isinstance(item, dict)]
    seed_ids = {str(item.get("uid") or "") for item in seed_items}
    seed = {
        **seed,
        "items": seed_items + [
            item for item in baseline.get("items") or []
            if isinstance(item, dict) and str(item.get("uid") or "") not in seed_ids
        ],
    }
    sources = fetch_sources(config)
    successful = [row for row in sources if row.get("fetch_state") == "ok"]
    official = [row for row in successful if int(row.get("tier") or 0) == 1]
    if len(official) < 2:
        raise RuntimeError(f"only {len(official)} official sources were readable; at least 2 are required")
    model = str(os.environ.get("MAGI_EXAM_TUTOR_NVIDIA_MODEL") or MODEL).strip()
    if not _model_allowed(model):
        raise RuntimeError("configured trend model is outside the non-China NVIDIA allowlist")
    prompt = build_prompt(sources, seed)
    result = run_nim_chat(
        prompt=prompt,
        model=model,
        timeout_sec=max(120, min(900, int(os.environ.get("MAGI_EXAM_TUTOR_TREND_TIMEOUT_SEC", "420") or "420"))),
        task_type="exam_tutor_trend_analysis",
        # The excerpts are fetched only from the configured public registry.
        # Keep official names/dockets/addresses verbatim so a generic address
        # detector cannot erase legal context or block the scheduled refresh.
        require_pii_scrub=False,
        data_classification="public_source",
        privacy_profile="public_source",
        restore_pii=False,
        heavy=True,
        allow_model_fallback=False,
        max_tokens=12_000,
        reasoning_effort="medium",
        reasoning_budget=4_096,
        system_prompt=(
            "你是台灣法律國考趨勢資料分析器。只能依所附公開來源整理，不得虛構事實、命題機率或評分標準；"
            "只輸出有效 JSON。"
        ),
    )
    if not result.get("success") or not str(result.get("response") or "").strip():
        raise RuntimeError(f"NVIDIA analysis unavailable: {result.get('error') or 'empty response'}")
    generated_at = now_iso()
    analysis = _extract_json_object(str(result.get("response") or ""))
    mcp_sources = fetch_mcp_law_evidence({
        "items": [item for item in analysis.get("items") or [] if isinstance(item, dict)]
        + [item for item in baseline.get("items") or [] if isinstance(item, dict)],
    })
    if mcp_sources:
        audit = run_nim_chat(
            prompt=build_mcp_audit_prompt(analysis, mcp_sources, prompt),
            model=model,
            timeout_sec=max(120, min(900, int(os.environ.get("MAGI_EXAM_TUTOR_TREND_TIMEOUT_SEC", "420") or "420"))),
            task_type="exam_tutor_trend_statutory_audit",
            require_pii_scrub=False,
            data_classification="public_source",
            privacy_profile="public_source",
            restore_pii=False,
            heavy=True,
            allow_model_fallback=False,
            max_tokens=12_000,
            reasoning_effort="medium",
            reasoning_budget=4_096,
            system_prompt=(
                "你是台灣法律資料稽核器。全國法規資料庫 MCP 現行本文優先於模型記憶；"
                "修法內容必須另有逐條立法來源，只輸出有效 JSON。"
            ),
        )
        if not audit.get("success") or not str(audit.get("response") or "").strip():
            raise RuntimeError(f"NVIDIA statutory audit unavailable: {audit.get('error') or 'empty response'}")
        analysis = _extract_json_object(str(audit.get("response") or ""))
        result = audit
        sources.extend(mcp_sources)
        generated_at = now_iso()
    snapshot = validate_analysis(analysis, sources=sources, model=str(result.get("model") or model), generated_at=generated_at)
    snapshot = merge_source_audited_baseline(
        snapshot,
        baseline=baseline,
        sources=sources,
        model=str(result.get("model") or model),
        generated_at=generated_at,
    )
    atomic_json(output_path, snapshot)
    receipt = {
        "schema_version": 1,
        "state": "ok",
        "started_at": started_at,
        "completed_at": generated_at,
        "output_path": str(output_path),
        "item_count": len(snapshot["items"]),
        "verified_count": sum(1 for item in snapshot["items"] if item["status"] == "verified"),
        "radar_count": sum(1 for item in snapshot["items"] if item["status"] == "radar"),
        "sources_fetched": len(successful),
        "official_sources_fetched": len(official),
        "model": str(result.get("model") or model),
        "local_model_fallback": False,
    }
    atomic_json(receipt_path, receipt)
    return receipt


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-config", type=Path, default=REPO_ROOT / "config" / "exam_tutor_trend_sources.json")
    parser.add_argument("--seed", type=Path, default=REPO_ROOT / "static" / "exam_tutor" / "trend_analysis.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--receipt", type=Path)
    return parser.parse_args()


def main() -> None:
    from api.runtime_paths import get_agent_dir

    args = parse_args()
    root = (get_agent_dir() / "exam-tutor").resolve()
    output = (args.output or root / "trend_analysis.json").expanduser().resolve()
    receipt = (args.receipt or root / "trend_sync_status.json").expanduser().resolve()
    try:
        result = run_refresh(
            config_path=args.source_config.expanduser().resolve(),
            seed_path=args.seed.expanduser().resolve(),
            output_path=output,
            receipt_path=receipt,
        )
    except Exception as exc:
        failure = {
            "schema_version": 1,
            "state": "failed_preserved_previous",
            "checked_at": now_iso(),
            "message": str(exc),
            "output_preserved": output.is_file(),
            "local_model_fallback": False,
        }
        atomic_json(receipt, failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
