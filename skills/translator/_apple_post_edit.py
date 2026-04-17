"""
Apple Translation + LLM Post-Editing (APE) pipeline.

For short-to-medium legal text (zh↔en), this path produces a baseline via
Apple's on-device Translation framework, then asks a local LLM (Casper /
Gemma-E4B via Ollama) to rewrite the baseline using correct legal
terminology. The edited output is validated; on failure we fall back to the
baseline.

Return shape mirrors the translator's other fast paths so callers can drop
the result into the same `translate()` return.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Heuristic legal markers. A single strong hit is enough.
_LEGAL_MARKERS = [
    r"訴之聲明", r"答辯", r"起訴", r"上訴", r"抗告", r"聲請",
    r"原告|被告|上訴人|被上訴人|聲請人|相對人|抗告人",
    r"民法\s*第", r"刑法\s*第", r"刑事訴訟法", r"民事訴訟法",
    r"\d+年度\S+?字第\d+號", r"第\s*\d+\s*條",  # 第N條 (statute/treaty article)
    r"公約|條約|憲法|法規", r"酷刑|人權|歧視|自由權",
    r"新臺幣[\d,]+元?", r"NT\$[\d,]+",
    r"prayer for relief", r"plaintiff", r"defendant", r"pursuant to",
    r"Article \d+", r"Civil Code", r"Criminal Code",
    r"Convention|Covenant|Treaty|Charter", r"ICCPR|CAT|ECHR",
]
_LEGAL_RE = re.compile("|".join(_LEGAL_MARKERS), re.IGNORECASE)


def is_legal_text(text: str) -> bool:
    if not text:
        return False
    return bool(_LEGAL_RE.search(text))


def _apple_translate(text: str, source_lang: str, target_lang: str,
                     timeout_sec: float = 10.0) -> Dict[str, object]:
    from skills.engine.apple_translation import translate as _t
    return _t(text, source_lang=source_lang, target_lang=target_lang,
              timeout_sec=timeout_sec, auto_build=True)


def _lang_label(code: str) -> str:
    c = (code or "").lower()
    if c.startswith("zh-hant") or c in {"zh-tw", "zh", "繁體中文", "繁中", "中文"}:
        return "Traditional Chinese (繁體中文)"
    if c.startswith("zh-hans") or c in {"zh-cn", "簡體中文", "簡中"}:
        return "Simplified Chinese"
    if c == "en" or c == "english":
        return "English"
    if c == "ja":
        return "Japanese"
    if c == "ko":
        return "Korean"
    return code


def _post_edit_prompt(source: str, baseline: str, target_lang: str,
                      tier1: str = "", tier2: str = "") -> str:
    tgt_label = _lang_label(target_lang)
    glossary = ""
    if tier1 or tier2:
        parts = []
        if tier1:
            parts.append("Official bilingual terms:\n" + tier1.strip())
        if tier2:
            parts.append("Academic/case-law terms:\n" + tier2.strip())
        glossary = "\n\n".join(parts) + "\n\n"
    return (
        f"TASK: Polish a machine-translated legal draft. Output language MUST be {tgt_label}.\n"
        f"Do NOT output any other language. Do NOT repeat the source.\n\n"
        "RULES:\n"
        "1. Preserve ALL numbers, monetary amounts, dates, case numbers, and party names EXACTLY.\n"
        "2. Replace generic phrasing with authoritative legal terminology "
        "(e.g. '訴之聲明' ↔ 'prayer for relief'; '被告應給付' ↔ 'the defendant shall pay').\n"
        "3. Keep meaning faithful — no added facts, no dropped clauses.\n"
        "4. Output ONLY the final polished text. No preamble, no commentary, no quotes.\n"
        f"5. If target is Traditional Chinese, emit ONLY Traditional characters.\n\n"
        f"{glossary}"
        f"SOURCE ({_lang_label(_detect_src_lang(source))}):\n{source}\n\n"
        f"DRAFT (needs polish, in {tgt_label}):\n{baseline}\n\n"
        f"POLISHED ({tgt_label} only):\n"
    )


def _detect_src_lang(text: str) -> str:
    for ch in text or "":
        if "\u4e00" <= ch <= "\u9fff":
            return "zh-Hant"
    return "en"


def _run_local_llm(prompt: str, timeout: int = 45) -> str:
    try:
        from skills.bridge.grounded_ai import _generate_local  # type: ignore
        out = _generate_local(prompt, temperature=0.1, timeout=timeout, num_ctx=4096)
        return (out or "").strip()
    except Exception:
        logger.debug("ape: _generate_local unavailable", exc_info=True)
        return ""


_PREAMBLE_RE = re.compile(
    r"^(?:\[?POLISHED\]?\s*[:：]?\s*|Here(?:'s| is).*?[:：]\s*|"
    r"以下是.*?[:：]\s*|潤飾後.*?[:：]\s*)",
    re.IGNORECASE,
)


def _strip_preamble(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return s
    s = _PREAMBLE_RE.sub("", s, count=1).strip()
    # If the model wrapped output in quotes/backticks, peel them.
    if len(s) >= 2 and s[0] in "\"'“「`" and s[-1] in "\"'”」`":
        s = s[1:-1].strip()
    return s


def translate_with_ape(
    text: str,
    *,
    source_lang: str = "zh-Hant",
    target_lang: str = "en",
    llm_timeout: int = 45,
    apple_timeout: float = 10.0,
    tier1: str = "",
    tier2: str = "",
) -> Dict[str, object]:
    """
    Full APE pipeline. Returns a dict with at least:
      success, text, provider, baseline, validator, degraded, elapsed_ms
    """
    import time
    t0 = time.monotonic()

    apple = _apple_translate(text, source_lang, target_lang, timeout_sec=apple_timeout)
    if not apple.get("success"):
        return {
            "success": False,
            "text": "",
            "provider": "apple_translation_failed",
            "error": apple.get("error") or "apple_translation_failed",
            "apple_error": apple.get("error"),
            "apple_stderr": apple.get("stderr"),
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        }

    baseline = str(apple.get("text") or "").strip()
    if not baseline:
        return {
            "success": False,
            "text": "",
            "provider": "apple_translation_empty",
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        }

    # Post-edit
    prompt = _post_edit_prompt(text, baseline, target_lang, tier1=tier1, tier2=tier2)
    edited_raw = _run_local_llm(prompt, timeout=llm_timeout)
    edited = _strip_preamble(edited_raw)

    if not edited:
        return {
            "success": True,
            "text": baseline,
            "provider": "apple_translation_baseline",
            "baseline": baseline,
            "degraded": True,
            "reason": "llm_unavailable",
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        }

    # Validate
    try:
        from skills.translator._post_edit_validator import validate_post_edit
        report = validate_post_edit(
            source=text, baseline=baseline, edited=edited,
            target_lang=target_lang,
        )
    except Exception:
        logger.debug("ape: validator crashed", exc_info=True)
        report = {"valid": False, "reasons": ["validator_exception"], "stats": {}}

    if report.get("valid"):
        return {
            "success": True,
            "text": edited,
            "provider": "apple_translation_ape",
            "baseline": baseline,
            "validator": report,
            "degraded": False,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        }

    # Rejected — return baseline with provenance.
    return {
        "success": True,
        "text": baseline,
        "provider": "apple_translation_baseline",
        "baseline": baseline,
        "rejected_edit": edited,
        "validator": report,
        "degraded": True,
        "reason": "post_edit_rejected",
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
    }


__all__ = ["translate_with_ape", "is_legal_text"]
