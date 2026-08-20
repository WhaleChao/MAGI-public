#!/usr/bin/env python3
"""Content-level LIVE certification for MAGI's bounded generative outputs.

The fixtures are synthetic and contain no client data.  Reports retain only
hashes, lengths, routes and rubric results; generated text is never persisted.
Availability alone is not a pass: every output must satisfy its production
content gate and fixture-specific factual anchors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DRAFT_FACTS = (
    "民國115年7月1日，測試被告乙駕車不慎造成測試原告甲受傷，"
    "醫療費為新臺幣50,000元，相關單據編為甲證1號。"
)


def _load_live_environment() -> str:
    """Load the same hash-bound external environment as the LIVE server.

    The gateway LaunchAgent deliberately carries only immutable path bindings;
    credentials and feature switches live in ``MAGI_ENV_FILE``.  A standalone
    quality process must load that canonical file before invoking production
    providers, otherwise it certifies an empty shell environment instead of
    the deployed path.
    """

    from dotenv import load_dotenv

    from api.runtime_paths import dotenv_override_allowed, get_env_file

    env_file = get_env_file()
    loaded = load_dotenv(str(env_file), override=dotenv_override_allowed())
    return str(env_file) if loaded or Path(env_file).is_file() else ""


DRAFT_PROMPT = f"""請依下列唯一資料產出完整臺灣民事起訴狀，不得新增任何事實、日期、金額、證據、法條或裁判：
文書類型：民事起訴狀
法院：臺灣臺北地方法院
案號：115年度訴字第123號
原告：測試原告甲
被告：測試被告乙
案由：損害賠償
案件事實：{DRAFT_FACTS}
可用法條：民法第184條
請包含聲明事項、事實及理由、此致、具狀人及日期；直接輸出書狀，不要解釋。
"""

SUMMARY_SOURCE = """臺灣臺北地方法院民事判決
主文
被告應給付原告新臺幣五萬元。
理由
按民法第184條規定，侵權行為損害賠償請求權之成立，應以行為人有故意或過失、權利受侵害及相當因果關係為要件。
本院認為，被告駕車未注意前方路況，且醫療單據足以證明原告受有損害，二者具有相當因果關係，故原告請求為有理由。
中華民國115年8月9日
"""

TRANSLATION_SOURCE = (
    "Under Taiwan's Citizen Judges Act, the court interpreter must accurately interpret "
    "the defendant's statement. The court ordered payment of NT$50,000 by September 8, 2026."
)


def _digest(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest() if text else ""


def _quality_codes(quality: dict[str, Any]) -> list[str]:
    return sorted(
        {
            str(item.get("code") or "")
            for item in quality.get("issues") or []
            if isinstance(item, dict) and str(item.get("code") or "")
        }
    )


def _is_nvidia_model(model: Any) -> bool:
    normalized = str(model or "").strip().lower()
    return bool(normalized) and any(
        marker in normalized for marker in ("nvidia", "nemotron", "nim_", "nim-")
    )


def certify_draft(
    provider: Callable[[str], tuple[str, str]] | None = None,
) -> dict[str, Any]:
    from api.osc.drafts import _osc_clean_draft_output, _osc_generate_draft_with_nvidia
    from api.osc.saas_workbench import quality_check

    generate = provider or _osc_generate_draft_with_nvidia
    raw, model = generate(DRAFT_PROMPT)
    text = _osc_clean_draft_output(raw)
    quality = quality_check(
        {
            "mode": "draft",
            "strict_export": True,
            "draft_text": text,
            "doc_type": "民事起訴狀",
            "case_number": "115年度訴字第123號",
            "court_name": "臺灣臺北地方法院",
            "reason": "損害賠償",
            "plaintiff": "測試原告甲",
            "defendant": "測試被告乙",
            "case_facts": DRAFT_FACTS,
            "grounding_text": DRAFT_PROMPT,
            "citation_validation": {"ok": True},
        }
    )
    anchors = {
        "plaintiff": "測試原告甲" in text,
        "defendant": "測試被告乙" in text,
        "amount": "50,000" in text or "50000" in text or "五萬元" in text,
        "event_date": "115年7月1日" in text or "民國115年7月1日" in text,
        "evidence": "甲證1號" in text.replace(" ", ""),
    }
    nvidia_provenance = _is_nvidia_model(model)
    passed = bool(
        text and nvidia_provenance and quality.get("pass") and all(anchors.values())
    )
    return {
        "passed": passed,
        "provider": "nvidia" if nvidia_provenance else "unverified",
        "model": str(model or ""),
        "nvidia_provenance": nvidia_provenance,
        "output_chars": len(text),
        "output_sha256": _digest(text),
        "quality_score": quality.get("score"),
        "quality_issue_codes": _quality_codes(quality),
        "anchors": anchors,
    }


def certify_summary(provider: Callable[..., Any] | None = None) -> dict[str, Any]:
    from api.domains.judgment_nvidia_summary import summarize_with_nvidia
    from api.domains.judgment_summary_quality import evaluate_practice_ready_summary

    select = provider or summarize_with_nvidia
    result = select(SUMMARY_SOURCE, "侵權行為損害賠償")
    summary = str(getattr(result, "summary", "") or "")
    quality = evaluate_practice_ready_summary(
        summary,
        SUMMARY_SOURCE,
        "侵權行為損害賠償",
        "臺灣臺北地方法院",
    )
    model = str(getattr(result, "model", "") or "")
    nvidia_provenance = _is_nvidia_model(model)
    pii_scrubbed = getattr(result, "pii_scrubbed", None) is True
    passed = bool(
        getattr(result, "success", False)
        and nvidia_provenance
        and pii_scrubbed
        and quality.ok
    )
    return {
        "passed": passed,
        "provider": "nvidia" if nvidia_provenance else "unverified",
        "model": model,
        "nvidia_provenance": nvidia_provenance,
        "output_chars": len(summary),
        "output_sha256": _digest(summary),
        "source_supported_spans": quality.source_supported_spans,
        "rule_spans": quality.rule_spans,
        "application_spans": quality.application_spans,
        "quality_score": quality.score,
        "quality_reason": quality.reason,
        "pii_scrubbed": pii_scrubbed,
    }


def certify_translation(provider: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    from api.handlers.document_handler import normalize_tw_legal_translation_terms
    from api.handlers.output_quality_handler import run_output_quality_gate
    from api.handlers.translation_handler import translate_text_complete

    translate = provider or translate_text_complete
    result = translate(TRANSLATION_SOURCE, target_lang="繁體中文", heavy=True)
    text = normalize_tw_legal_translation_terms(
        str(result.get("response") or result.get("translated_text") or result.get("text") or "")
    )
    quality = run_output_quality_gate(
        "translation",
        text,
        source_text=TRANSLATION_SOURCE,
        instruction="English -> 臺灣繁體中文",
    )
    anchors = {
        "citizen_judges_act": "國民法官法" in text,
        "court_interpreter": "司法通譯" in text,
        "defendant": "被告" in text,
        "amount": "50,000" in text or "50000" in text or "五萬元" in text,
        "date": ("2026" in text and "9" in text and "8" in text),
    }
    provider_name = str(result.get("provider") or "").lower()
    model_name = str(result.get("model") or "").lower()
    nvidia_provenance = bool(
        "nvidia" in provider_name and _is_nvidia_model(model_name)
    )
    passed = bool(
        result.get("success")
        and result.get("route") == "nvidia_nim"
        and nvidia_provenance
        and quality.get("ok")
        and all(anchors.values())
    )
    return {
        "passed": passed,
        "route": str(result.get("route") or ""),
        "provider": str(result.get("provider") or ""),
        "model": str(result.get("model") or ""),
        "nvidia_provenance": nvidia_provenance,
        "output_chars": len(text),
        "output_sha256": _digest(text),
        "quality_score": quality.get("score"),
        "quality_issue": str(quality.get("issue") or ""),
        "anchors": anchors,
    }


def run(*, draft_provider=None, summary_provider=None, translation_provider=None) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    for name, callable_ in (
        ("legal_draft", lambda: certify_draft(draft_provider)),
        ("judgment_summary", lambda: certify_summary(summary_provider)),
        ("legal_translation", lambda: certify_translation(translation_provider)),
    ):
        try:
            checks[name] = callable_()
        except Exception as exc:
            checks[name] = {
                "passed": False,
                "error_type": type(exc).__name__,
                "error": str(exc)[:240],
            }
    return {
        "schema": "magi.generative-quality-live/v1",
        "generated_at": datetime.now().astimezone().isoformat(),
        "synthetic_non_client_fixture": True,
        "raw_outputs_persisted": False,
        "passed": all(bool(item.get("passed")) for item in checks.values()),
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", required=True)
    args = parser.parse_args()
    _load_live_environment()
    report = run()
    output = Path(args.json_out).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
