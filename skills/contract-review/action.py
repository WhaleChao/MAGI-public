#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
contract-review — 合約審閱 / NDA 分流 / 法律文件摘要 / 供應商查核

Tasks:
  review        合約審閱：找出不利條款、風險點、缺漏條款
  nda           NDA 分流：快速判斷保密協議風險並給出「可簽/需修改/拒絕」
  summarize     法律文件摘要：輸出重點 + 義務 + 風險清單
  vendor_check  供應商查核：與標準模板比對落差
  help          顯示說明
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

_MAGI_ROOT = Path(
    os.environ.get("MAGI_ROOT_DIR")
    or os.environ.get("MAGI_ROOT")
    or Path(__file__).resolve().parents[2]
).resolve()
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

logger = logging.getLogger("contract_review")

_MAX_CHARS = int(os.environ.get("CONTRACT_REVIEW_MAX_CHARS", "12000"))
_LLM_TIMEOUT = int(os.environ.get("CONTRACT_REVIEW_TIMEOUT", "120"))


# ---------------------------------------------------------------------------
# Document loader
# ---------------------------------------------------------------------------

def _load_text(path: str) -> str:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"找不到檔案：{path}")

    # MarkItDown path (feature-flagged, default OFF)
    if os.environ.get("MAGI_USE_MARKITDOWN", "0").strip() == "1":
        try:
            from skills.engine.document_reader import read_document
            r = read_document(str(p))
            if r.success and r.text:
                # Prefer markdown for contract review (preserves headings/structure)
                return r.markdown or r.text
        except Exception:
            pass  # fall through to legacy

    suffix = p.suffix.lower()
    if suffix == ".txt":
        return p.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(str(p))
            parts = []
            for page in doc:
                parts.append(page.get_text())
            doc.close()
            return "\n".join(parts)
        except ImportError:
            raise RuntimeError("讀取 PDF 需要 PyMuPDF：pip install pymupdf")
    if suffix in (".docx", ".doc"):
        try:
            import docx
            d = docx.Document(str(p))
            return "\n".join(para.text for para in d.paragraphs)
        except ImportError:
            raise RuntimeError("讀取 DOCX 需要 python-docx：pip install python-docx")
    # fallback: try raw text
    return p.read_text(encoding="utf-8", errors="replace")


def _truncate(text: str, max_chars: int = _MAX_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n\n[…文件過長，已截取前後段…]\n\n" + text[-half:]


def _get_gateway():
    from skills.bridge.inference_gateway import InferenceGateway
    return InferenceGateway()


def _llm_json(prompt: str, fallback: dict) -> dict:
    """Call local LLM and extract JSON from the response."""
    try:
        gw = _get_gateway()
        r = gw.chat(prompt=prompt, task_type="general", timeout=_LLM_TIMEOUT)
        if not r.get("success"):
            logger.warning("LLM call failed: %s", r.get("error"))
            return fallback
        raw = str(r.get("response") or "").strip()
        # Extract JSON block
        m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", raw)
        if m:
            raw = m.group(1).strip()
        else:
            # Try to find first { ... } spanning the whole response
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end != -1 and end > start:
                raw = raw[start:end + 1]
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("JSON parse failed: %s", e)
        return fallback
    except Exception as e:
        logger.error("_llm_json error: %s", e)
        return fallback


def _sentences(text: str) -> list[str]:
    raw = re.split(r"[\n。！？!?；;]+", str(text or ""))
    return [part.strip() for part in raw if part and part.strip()]


def _shorten(text: str, limit: int = 40) -> str:
    value = re.sub(r"\s+", " ", str(text or "").strip())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _detect_doc_type(text: str) -> str:
    checks = [
        ("保密", "保密協議"),
        ("NDA", "保密協議"),
        ("供應商", "供應商合約"),
        ("採購", "採購合約"),
        ("顧問", "顧問服務合約"),
        ("租賃", "租賃合約"),
        ("授權", "授權合約"),
        ("合約", "一般合約"),
        ("契約", "一般契約"),
    ]
    for needle, label in checks:
        if needle.lower() in str(text or "").lower():
            return label
    return "法律文件"


def _extract_parties(text: str) -> list[str]:
    parties: list[str] = []
    for label in ("甲方", "乙方", "丙方", "立約人", "委託人", "受任人"):
        match = re.search(rf"{label}[：:\s]*([^\n，。,；;（）(){{}}]{2,24})", text)
        if match:
            parties.append(f"{label} {match.group(1).strip()}")
    if not parties and "雙方" in text:
        parties = ["甲方", "乙方"]
    deduped: list[str] = []
    for party in parties:
        if party not in deduped:
            deduped.append(party)
    return deduped[:4]


def _find_sentence(text: str, keywords: list[str]) -> str:
    for sentence in _sentences(text):
        if all(keyword in sentence for keyword in keywords):
            return _shorten(sentence, 80)
    for sentence in _sentences(text):
        if any(keyword in sentence for keyword in keywords):
            return _shorten(sentence, 80)
    return ""


def _extract_obligations(text: str, party_markers: list[str]) -> list[str]:
    obligations: list[str] = []
    for sentence in _sentences(text):
        if not any(marker in sentence for marker in party_markers):
            continue
        if any(marker in sentence for marker in ["應", "須", "不得", "負責", "提供", "支付", "保密"]):
            obligations.append(_shorten(sentence, 48))
        if len(obligations) >= 4:
            break
    return obligations


def _extract_dates(text: str) -> list[str]:
    hits = re.findall(r"(?:19|20)?\d{2}年\d{1,2}月\d{1,2}日", text)
    unique: list[str] = []
    for hit in hits:
        if hit not in unique:
            unique.append(hit)
    return unique[:3]


def _risk_candidates(text: str) -> list[dict]:
    catalog = [
        ("違約金", "違約責任條款可能過重或需明確上限", "高", "確認違約金計算方式與是否過高"),
        ("損害賠償", "損害賠償責任範圍需確認是否無上限", "高", "建議增列合理賠償上限"),
        ("保密", "保密義務需確認範圍、期限與例外", "中", "補足例外情形與保密期限"),
        ("自動續約", "自動續約條款可能造成長期綁約", "中", "加上提前終止或通知機制"),
        ("單方", "片面權利義務安排可能失衡", "高", "改成雙方對等條件"),
        ("片面", "片面條款需檢查是否對一方過度不利", "高", "調整為雙方對等權利義務"),
        ("終止", "終止條款需確認通知期與已履約部分如何處理", "中", "補足通知期與清算方式"),
        ("管轄", "管轄法院與準據法需明確", "中", "確認是否採中華民國法律與合理法院"),
        ("準據法", "準據法未明確可能增加爭議", "中", "補列適用法律"),
    ]
    results: list[dict] = []
    for keyword, issue, severity, suggestion in catalog:
        sentence = _find_sentence(text, [keyword])
        if not sentence:
            continue
        results.append(
            {
                "clause_text": sentence,
                "issue": issue,
                "risk": severity,
                "severity": severity,
                "point": issue,
                "suggestion": suggestion,
            }
        )
    return results


def _risk_level_from_items(items: list[dict]) -> str:
    severities = [str(item.get("severity") or item.get("risk") or "") for item in items]
    if any(level == "高" for level in severities):
        return "高"
    if any(level == "中" for level in severities):
        return "中"
    return "低"


def _missing_clause_entries(text: str, names: list[tuple[str, str]]) -> list[dict]:
    lowered = str(text or "")
    missing: list[dict] = []
    for keyword, reason in names:
        if keyword not in lowered:
            missing.append({"clause": keyword, "reason": reason})
    return missing


def _fallback_summary(text: str) -> dict:
    risks = _risk_candidates(text)
    dates = _extract_dates(text)
    dispute_resolution = _find_sentence(text, ["管轄"]) or _find_sentence(text, ["仲裁"]) or "文件中未明確辨識爭議解決條款。"
    governing_law = _find_sentence(text, ["準據法"]) or _find_sentence(text, ["適用法律"]) or "文件中未明確辨識準據法。"
    payment_terms = _find_sentence(text, ["付款"]) or _find_sentence(text, ["匯款"]) or "文件中未明確辨識付款條件。"

    return {
        "doc_type": _detect_doc_type(text),
        "parties": _extract_parties(text),
        "effective_date": dates[0] if dates else "",
        "expiry_date": dates[1] if len(dates) > 1 else "",
        "key_terms": [
            {"term": "文件主題", "content": _shorten(_sentences(text)[0] if _sentences(text) else str(text), 40)},
            {"term": "保密或義務", "content": _shorten(_find_sentence(text, ["保密"]) or _find_sentence(text, ["應"]), 40)},
        ],
        "obligations": {
            "party_a": _extract_obligations(text, ["甲方", "委託人", "買方"]),
            "party_b": _extract_obligations(text, ["乙方", "受任人", "供應商", "賣方"]),
        },
        "payment_terms": payment_terms,
        "risk_points": [
            {"point": item["point"], "severity": item["severity"]}
            for item in risks[:4]
        ],
        "dispute_resolution": dispute_resolution,
        "governing_law": governing_law,
        "summary": _shorten("；".join(_sentences(text)[:3]) or str(text), 120),
        "fallback_used": True,
        "fallback_reason": "llm_unavailable",
    }


def _fallback_review(text: str) -> dict:
    risks = _risk_candidates(text)
    missing = _missing_clause_entries(
        text,
        [
            ("保密", "合約常需界定保密義務與例外情形。"),
            ("終止", "應明確約定終止條件、通知期與效果。"),
            ("管轄", "應約定爭議解決法院或仲裁方式。"),
            ("準據法", "應明確約定適用法律。"),
        ],
    )
    one_sided_terms = [
        _shorten(sentence, 48)
        for sentence in _sentences(text)
        if ("乙方" in sentence and "應" in sentence) or "單方" in sentence or "片面" in sentence
    ][:4]
    recommendations = [item["suggestion"] for item in risks[:3]]
    recommendations.extend(f"補列「{item['clause']}」條款。" for item in missing[:2])

    return {
        "doc_type": _detect_doc_type(text),
        "parties": _extract_parties(text),
        "risk_level": _risk_level_from_items(risks),
        "flagged_clauses": [
            {
                "clause_text": item["clause_text"],
                "issue": item["issue"],
                "risk": item["risk"],
                "suggestion": item["suggestion"],
            }
            for item in risks[:4]
        ],
        "missing_clauses": missing[:4],
        "one_sided_terms": one_sided_terms,
        "penalty_liability": _find_sentence(text, ["違約"]) or _find_sentence(text, ["損害賠償"]) or "未明確辨識違約責任條款。",
        "termination_terms": _find_sentence(text, ["終止"]) or "未明確辨識終止條款。",
        "recommendations": recommendations[:5],
        "summary": _shorten("；".join(_sentences(text)[:3]) or str(text), 100),
        "fallback_used": True,
        "fallback_reason": "llm_unavailable",
    }


def _fallback_nda(text: str) -> dict:
    risks = _risk_candidates(text)
    confidentiality_scope = "過廣" if any(token in text for token in ["所有資訊", "任何資訊", "一切資訊"]) else "清楚"
    exclusions = "是" if any(token in text for token in ["公開資訊", "已知資訊", "第三方", "法令要求"]) else "否"
    duration = _find_sentence(text, ["保密", "年"]) or _find_sentence(text, ["期間"]) or ""
    return_obligation = "是" if any(token in text for token in ["銷毀", "歸還"]) else "否"
    penalty_clause = "過重" if "違約金" in text else ("是" if "違約" in text else "否")
    verdict = "可簽"
    risk_level = _risk_level_from_items(risks)
    if risk_level == "中":
        verdict = "需修改"
    elif risk_level == "高":
        verdict = "建議拒絕"

    missing_protections = [
        item
        for item in [
            "公開資訊/既有資訊例外" if exclusions == "否" else "",
            "資料返還或銷毀機制" if return_obligation == "否" else "",
        ]
        if item
    ]
    return {
        "is_nda": any(token.lower() in text.lower() for token in ["nda", "保密"]),
        "nda_type": "雙向" if "雙方" in text or "互相" in text else "單向",
        "verdict": verdict,
        "verdict_reason": _shorten("；".join(item["issue"] for item in risks[:2]) or "未發現明顯重大異常，但仍建議人工複核。", 50),
        "risk_level": risk_level,
        "confidentiality_scope": confidentiality_scope,
        "duration": duration,
        "exclusions": exclusions,
        "return_obligation": return_obligation,
        "penalty_clause": penalty_clause,
        "jurisdiction": _find_sentence(text, ["管轄"]) or "未明確辨識管轄條款。",
        "risk_items": [
            {"item": item["issue"], "severity": item["severity"], "suggestion": item["suggestion"]}
            for item in risks[:4]
        ],
        "missing_protections": missing_protections,
        "summary": _shorten("；".join(_sentences(text)[:3]) or str(text), 80),
        "fallback_used": True,
        "fallback_reason": "llm_unavailable",
    }


def _fallback_vendor_check(contract_text: str, standard_text: str) -> dict:
    risks = _risk_candidates(contract_text)
    missing = _missing_clause_entries(
        contract_text,
        [
            ("驗收", "建議加入驗收程序與不合格處理機制。"),
            ("保固", "建議明確保固期間與責任範圍。"),
            ("智慧財產", "建議約定智財歸屬與授權範圍。"),
            ("保密", "建議加入保密義務。"),
        ],
    )
    recommendations = [item["suggestion"] for item in risks[:3]]
    recommendations.extend(f"補列「{item['clause']}」條款。" for item in missing[:2])
    return {
        "doc_type": _detect_doc_type(contract_text),
        "vendor": _extract_parties(contract_text)[0] if _extract_parties(contract_text) else "",
        "risk_level": _risk_level_from_items(risks),
        "present_clauses": [
            keyword
            for keyword in ["交貨", "付款", "保密", "保固", "終止", "智財"]
            if keyword in contract_text
        ],
        "missing_clauses": [
            {"clause": item["clause"], "importance": "中", "suggestion": item["reason"]}
            for item in missing[:4]
        ],
        "unfavorable_deviations": [
            {"clause": item["clause_text"], "issue": item["issue"], "risk": item["risk"]}
            for item in risks[:4]
        ],
        "payment_terms": {
            "payment_days": _find_sentence(contract_text, ["付款"]) or "",
            "assessment": "不明確" if "付款" not in contract_text else "合理",
        },
        "termination_notice": _find_sentence(contract_text, ["終止"]) or "未明確辨識終止通知期。",
        "liability_cap": _find_sentence(contract_text, ["損害賠償"]) or "未明確辨識賠償上限。",
        "ip_ownership": _find_sentence(contract_text, ["智慧財產"]) or "未明確辨識智財歸屬。",
        "recommendations": recommendations[:5],
        "summary": _shorten(_shorten(standard_text, 40) + "；" + "；".join(_sentences(contract_text)[:2]), 100),
        "fallback_used": True,
        "fallback_reason": "llm_unavailable",
    }


# ---------------------------------------------------------------------------
# Task 1 — 合約審閱 (Contract Review)
# ---------------------------------------------------------------------------
_REVIEW_PROMPT = """你是臺灣資深合約審閱律師，請審閱以下合約文件，嚴格依照臺灣法律（民法、公司法等）觀點分析。

合約全文：
{text}

請輸出以下 JSON（不要任何其他文字）：
{{
  "doc_type": "合約類型（如：勞務合約、租賃合約、採購合約等）",
  "parties": ["當事人1", "當事人2"],
  "risk_level": "高/中/低",
  "flagged_clauses": [
    {{
      "clause_text": "原文片段（30字內）",
      "issue": "問題說明",
      "risk": "高/中/低",
      "suggestion": "修改建議"
    }}
  ],
  "missing_clauses": [
    {{
      "clause": "缺漏的條款名稱",
      "reason": "為何應有此條款"
    }}
  ],
  "one_sided_terms": ["對乙方不利的條款摘要"],
  "penalty_liability": "違約責任與賠償條款評估",
  "termination_terms": "終止條款評估",
  "recommendations": ["建議修改項目1", "建議修改項目2"],
  "summary": "整體合約評估（100字內）"
}}"""


def review(text: str) -> dict:
    text = _truncate(text)
    fallback = {"error": "LLM 無回應", "risk_level": "未知", "flagged_clauses": [], "missing_clauses": []}
    result = _llm_json(_REVIEW_PROMPT.format(text=text), fallback)
    if result.get("error"):
        result = _fallback_review(text)
    result["task"] = "review"
    return result


# ---------------------------------------------------------------------------
# Task 2 — NDA 分流 (NDA Triage)
# ---------------------------------------------------------------------------
_NDA_PROMPT = """你是臺灣資深律師，專門審閱保密協議（NDA / 保密合約）。請分析以下文件：

文件全文：
{text}

請輸出以下 JSON（不要任何其他文字）：
{{
  "is_nda": true,
  "nda_type": "單向/雙向/多方",
  "verdict": "可簽/需修改/建議拒絕",
  "verdict_reason": "判斷理由（50字內）",
  "risk_level": "高/中/低",
  "confidentiality_scope": "保密範圍評估（清楚/模糊/過廣）",
  "duration": "保密期限",
  "exclusions": "例外情形是否完整（是/否/部分）",
  "return_obligation": "是否要求歸還或銷毀資料（是/否）",
  "penalty_clause": "違約罰則是否合理（是/否/過重）",
  "jurisdiction": "適用法律與管轄法院",
  "risk_items": [
    {{
      "item": "風險條款說明",
      "severity": "高/中/低",
      "suggestion": "修改建議"
    }}
  ],
  "missing_protections": ["我方缺少的保護條款"],
  "summary": "NDA 整體評估（80字內）"
}}"""


def nda(text: str) -> dict:
    text = _truncate(text)
    fallback = {"error": "LLM 無回應", "verdict": "未知", "risk_level": "未知", "risk_items": []}
    result = _llm_json(_NDA_PROMPT.format(text=text), fallback)
    if result.get("error"):
        result = _fallback_nda(text)
    result["task"] = "nda"
    return result


# ---------------------------------------------------------------------------
# Task 3 — 法律文件摘要 (Legal Document Summary)
# ---------------------------------------------------------------------------
_SUMMARY_PROMPT = """你是臺灣法律專家，請將以下法律文件整理為結構化摘要。

文件全文：
{text}

請輸出以下 JSON（不要任何其他文字）：
{{
  "doc_type": "文件類型",
  "parties": ["當事人1（角色）", "當事人2（角色）"],
  "effective_date": "生效日期（若有）",
  "expiry_date": "終止日期（若有）",
  "key_terms": [
    {{"term": "條款名稱", "content": "重點說明（40字內）"}}
  ],
  "obligations": {{
    "party_a": ["甲方主要義務"],
    "party_b": ["乙方主要義務"]
  }},
  "payment_terms": "付款條件摘要（若有）",
  "risk_points": [
    {{"point": "風險點說明", "severity": "高/中/低"}}
  ],
  "dispute_resolution": "爭議解決方式",
  "governing_law": "準據法",
  "summary": "文件重點整理（120字內）"
}}"""


def summarize(text: str) -> dict:
    text = _truncate(text)
    fallback = {"error": "LLM 無回應", "key_terms": [], "risk_points": []}
    result = _llm_json(_SUMMARY_PROMPT.format(text=text), fallback)
    if result.get("error"):
        result = _fallback_summary(text)
    result["task"] = "summarize"
    return result


# ---------------------------------------------------------------------------
# Task 4 — 供應商查核 (Vendor / Supplier Contract Check)
# ---------------------------------------------------------------------------

def _load_standard_template(template_path: Optional[str]) -> str:
    if template_path:
        try:
            return _load_text(template_path)
        except Exception as e:
            logger.warning("無法讀取標準範本：%s", e)
    # Built-in minimal standard clauses for Taiwanese vendor contracts
    refs = Path(__file__).parent / "references" / "vendor_standard.txt"
    if refs.exists():
        return refs.read_text(encoding="utf-8")
    return _DEFAULT_VENDOR_STANDARD


_DEFAULT_VENDOR_STANDARD = """
臺灣供應商合約標準條款清單：
1. 交貨期限與逾期罰則
2. 品質規格與驗收程序
3. 付款條件（發票、期限、匯款方式）
4. 保固期限與責任範圍
5. 智慧財產權歸屬
6. 保密義務（雙向）
7. 不可抗力條款
8. 違約責任與損害賠償上限
9. 終止與解除條款（含提前通知期）
10. 爭議解決（仲裁/訴訟）與準據法
11. 轉包/下包限制
12. 個人資料保護義務
"""

_VENDOR_PROMPT = """你是臺灣採購法務專家，請審閱以下供應商合約，並對照標準條款清單找出落差。

供應商合約：
{contract_text}

標準條款清單：
{standard_text}

請輸出以下 JSON（不要任何其他文字）：
{{
  "doc_type": "合約類型",
  "vendor": "供應商名稱（若有）",
  "risk_level": "高/中/低",
  "present_clauses": ["已包含的標準條款"],
  "missing_clauses": [
    {{"clause": "缺漏條款名稱", "importance": "高/中/低", "suggestion": "建議加入的條文方向"}}
  ],
  "unfavorable_deviations": [
    {{"clause": "條款名稱", "issue": "與標準的落差說明", "risk": "高/中/低"}}
  ],
  "payment_terms": {{
    "payment_days": "付款天數",
    "assessment": "合理/偏短/偏長/不明確"
  }},
  "termination_notice": "終止通知期（天數或評估）",
  "liability_cap": "損害賠償上限（若有）",
  "ip_ownership": "智財權歸屬評估",
  "recommendations": ["建議談判或修改項目"],
  "summary": "供應商合約整體評估（100字內）"
}}"""


def vendor_check(contract_text: str, template_path: Optional[str] = None) -> dict:
    contract_text = _truncate(contract_text)
    standard_text = _load_standard_template(template_path)
    fallback = {"error": "LLM 無回應", "missing_clauses": [], "unfavorable_deviations": []}
    result = _llm_json(_VENDOR_PROMPT.format(
        contract_text=contract_text,
        standard_text=standard_text,
    ), fallback)
    if result.get("error"):
        result = _fallback_vendor_check(contract_text, standard_text)
    result["task"] = "vendor_check"
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="MAGI contract-review — 合約審閱工具")
    parser.add_argument("--task", required=True,
                        choices=["review", "nda", "summarize", "vendor_check", "help"],
                        help="執行任務")
    parser.add_argument("--file", default="", help="合約檔案路徑 (.txt/.pdf/.docx)")
    parser.add_argument("--text", default="", help="直接輸入合約文字（與 --file 二擇一）")
    parser.add_argument("--template", default="", help="[vendor_check] 標準合約範本路徑")
    parser.add_argument("--output", default="", help="輸出結果至指定 JSON 檔（選用）")
    args = parser.parse_args()

    if args.task == "help":
        print(json.dumps({
            "skill": "contract-review",
            "tasks": {
                "review": "合約審閱 — 標出不利條款、風險點、缺漏條款",
                "nda": "NDA 分流 — 判斷保密協議風險，給出可簽/需修改/拒絕",
                "summarize": "法律文件摘要 — 重點 + 義務 + 風險清單",
                "vendor_check": "供應商查核 — 與標準模板比對落差",
            },
            "flags": {
                "--file": "合約檔案路徑 (.txt / .pdf / .docx)",
                "--text": "直接貼入合約文字",
                "--template": "[vendor_check] 自訂標準範本路徑",
                "--output": "輸出 JSON 至指定路徑",
            },
        }, ensure_ascii=False, indent=2))
        return 0

    # Load document
    try:
        if args.file:
            text = _load_text(args.file)
        elif args.text:
            text = args.text
        else:
            parser.error("請提供 --file 或 --text")
            return 2
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1

    if not text.strip():
        print(json.dumps({"ok": False, "error": "文件內容為空"}, ensure_ascii=False))
        return 1

    # Dispatch
    try:
        if args.task == "review":
            result = review(text)
        elif args.task == "nda":
            result = nda(text)
        elif args.task == "summarize":
            result = summarize(text)
        elif args.task == "vendor_check":
            result = vendor_check(text, template_path=args.template or None)
        else:
            result = {"error": f"未知任務: {args.task}"}
    except Exception as e:
        logger.exception("task failed")
        result = {"ok": False, "error": str(e)}

    result["ok"] = "error" not in result
    output = json.dumps(result, ensure_ascii=False, indent=2)
    print(output)

    if args.output:
        try:
            Path(args.output).write_text(output, encoding="utf-8")
            logger.info("結果已儲存：%s", args.output)
        except Exception as e:
            logger.warning("輸出檔案寫入失敗：%s", e)

    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
