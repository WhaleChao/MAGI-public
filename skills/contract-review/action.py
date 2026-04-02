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

_MAGI_ROOT = os.environ.get("MAGI_ROOT_DIR", os.path.expanduser("~/Desktop/MAGI"))
if _MAGI_ROOT not in sys.path:
    sys.path.insert(0, _MAGI_ROOT)

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
    """Call TAIDE and extract JSON from the response."""
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
