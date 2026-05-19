import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

logger = logging.getLogger(__name__)

# Ensure we can import MAGI skills package
MAGI_DIR = Path(_MAGI_ROOT)
if str(MAGI_DIR) not in sys.path:
    sys.path.insert(0, str(MAGI_DIR))

from skills.bridge.inference_gateway import InferenceGateway


class LAFVision:
    """
    Handles local vision model integration for LAF document analysis via Melchior.
    Specifically targets extracting 'Start Date' (受任/開辦日期) from Power of Attorney files.

    OCR consensus 接入（guarded-write，三大模組保護合規）：
    - extract_start_date() 型態與行為完全不變（flag off 時 bit-for-bit 相同）
    - extract_start_date_with_metadata() 新增，提供結構化結果
    - MAGI_LAF_OCR_CONSENSUS_ENABLE=1（預設啟用 guarded-write）
    - MAGI_LAF_OCR_CONSENSUS_SHADOW=0（shadow=1 時只 log 差異不改決策）
    - 不碰 laf_automation_v2.py 的 captcha OCR（ddddocr 驗證碼，與本類別無關）
    """

    def __init__(self):
        self.gateway = InferenceGateway()

    @staticmethod
    def _env_bool(name: str, default: bool = False) -> bool:
        raw = os.environ.get(name)
        if raw is None:
            return default
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    def _consensus_enabled(self) -> bool:
        return self._env_bool("MAGI_LAF_OCR_CONSENSUS_ENABLE", True)

    def _consensus_shadow(self) -> bool:
        return self._env_bool("MAGI_LAF_OCR_CONSENSUS_SHADOW", False)

    # ------------------------------------------------------------------
    # Internal: legacy core (original logic, no new side-effects)
    # ------------------------------------------------------------------

    def _extract_via_legacy(self, image_path: str) -> Optional[str]:
        """原有 extract_start_date() 核心邏輯，bit-for-bit 不變。"""
        p = Path(image_path)
        if not p.exists() or not p.is_file():
            logger.error("Image not found at path: %s", image_path)
            return None

        # 1. Use macOS Vision to extract raw text (align with MAC VISION policy)
        ocr_text = ""
        logger.info("Extracting text via macOS Vision for date extraction...")
        try:
            from skills.apple.apple_intelligence import ocr_image
            ocr_result = ocr_image(str(p), engine="vision")
            if ocr_result.get("success") and ocr_result.get("text"):
                ocr_text = ocr_result["text"].strip()
                logger.info("macOS Vision extracted %d characters.", len(ocr_text))
            else:
                logger.warning(
                    "macOS Vision returned no text. Falling back to InferenceGateway vision route."
                )
        except Exception as e:
            logger.warning("Failed to call apple_intelligence.ocr_image: %s", e)

        prompt = (
            "這是一份法律扶助基金會(法扶)的委任狀或開辦相關資料。\n"
            "請幫我尋找文件上的日期，作為「開辦日期」的依據。\n"
            "【判斷優先順序】：\n"
            "1. 優先尋找法院或機關的「收文章章戳日期」（尤為重要，代表已遞件）。\n"
            "2. 若無收文章，請尋找律師手寫簽署的「受任日期」或落款日期。\n"
            "請「只」回傳你判斷出最符合上述優先順序的日期 (格式如 YYYYMMDD 或 YYYY-MM-DD，西元年)，不要包含任何其他文字或解釋。\n"
            "如果整份文件完全找不到任何相關日期，請回傳字串 'None'。"
        )

        timeout = int(os.environ.get("LAF_VISION_TIMEOUT_SECONDS", "45") or "45")

        if ocr_text:
            logger.info(
                "Sending OCR text to InferenceGateway chat route for date extraction..."
            )
            result_dict = self.gateway.dispatch(
                prompt="{}\n\n[文件文字內容]\n{}".format(prompt, ocr_text),
                task_type="date_extract",
                timeout=max(15, timeout),
                cross_validate=True,
                tc_review=False,
            )
        else:
            logger.info(
                "Sending image %s via InferenceGateway vision route...", p.name
            )
            result_dict = self.gateway.dispatch(
                prompt=prompt,
                image_path=str(p),
                task_type="date_extract",
                timeout=max(15, timeout),
                cross_validate=True,
                tc_review=False,
            )

        if not result_dict.get("success"):
            logger.error(
                "Vision/Chat API request failed: %s", result_dict.get("error")
            )
            return None

        result_text = str(
            result_dict.get("analysis")
            or result_dict.get("response")
            or result_dict.get("text")
            or ""
        ).strip()

        if not result_text or result_text.lower() == "none" or "none" in result_text.lower():
            logger.info("Vision model could not detect a start date.")
            return None

        logger.info(
            "Vision model extracted start date: %s (route=%s, degraded=%s, confidence=%s)",
            result_text,
            result_dict.get("route", ""),
            result_dict.get("degraded", False),
            result_dict.get("confidence", "n/a"),
        )
        return result_text

    # ------------------------------------------------------------------
    # Internal: consensus OCR runner (lazy import)
    # ------------------------------------------------------------------

    def _run_consensus_ocr(self, image_path: str) -> Optional[Any]:
        """Run skills.engine.ocr.consensus.run_consensus (lazy import).

        Returns OCRConsensusResult or None on any failure.
        Note: This is for document OCR only (task_type='legal').
        LAFVision captcha OCR (ddddocr in laf_automation_v2.py) is NOT affected.
        """
        try:
            from skills.engine.ocr import consensus as _ocr_mod
            result = _ocr_mod.run_consensus(image_path, task_type="legal")
            if result and result.success:
                return result
            return None
        except Exception as e:
            logger.warning("LAFVision: consensus OCR failed: %s", e)
            return None

    # ------------------------------------------------------------------
    # Internal: metrics writer (counts/hash only, no raw text)
    # ------------------------------------------------------------------

    def _write_consensus_metrics(
        self,
        image_path: str,
        consensus_result: Optional[Any],
        legacy_date: Optional[str],
        mode: str,
    ) -> None:
        """Write shadow/enabled comparison metrics. Never logs raw text."""
        try:
            import hashlib
            import time
            from api.platforms.runtime_dir import (
                metrics as _rt_metrics,
                atomic_append_jsonl as _rt_append,
            )
            with open(image_path, "rb") as _f:
                img_hash = hashlib.sha256(_f.read(65536)).hexdigest()[:16]

            record = {
                "ts": time.time(),
                "img_hash": img_hash,
                "mode": mode,
                "consensus_success": bool(
                    consensus_result and consensus_result.success
                ),
                "consensus_confidence": (
                    round(consensus_result.confidence, 4)
                    if consensus_result and consensus_result.success
                    else None
                ),
                "consensus_writable": (
                    consensus_result.writable
                    if consensus_result and consensus_result.success
                    else None
                ),
                "consensus_critical_conflict": (
                    consensus_result.critical_conflict
                    if consensus_result and consensus_result.success
                    else None
                ),
                "legacy_date_present": legacy_date is not None,
            }
            metrics_path = _rt_metrics("laf_vision_consensus")
            _rt_append(metrics_path, record, rotate_at=500, keep_tail=500)
        except Exception as e:
            logger.debug("LAFVision: metrics write failed: %s", e)

    # ------------------------------------------------------------------
    # Public API — existing method (bit-for-bit unchanged when flag off)
    # ------------------------------------------------------------------

    def extract_start_date(self, image_path: str) -> Optional[str]:
        """
        Extracts the start date (受任/開辦日期) from a LAF Power of Attorney image.
        Returns the extracted date string (ideally YY-MM-DD format) or None if extraction fails.

        Delegates to extract_start_date_with_metadata().
        When MAGI_LAF_OCR_CONSENSUS_ENABLE=0 and MAGI_LAF_OCR_CONSENSUS_SHADOW=0,
        the behaviour is bit-for-bit identical to the original.
        """
        result = self.extract_start_date_with_metadata(image_path)
        mode = (result.get("provider_trace") or {}).get("mode")
        if result.get("success") and result.get("date") and (mode != "enabled" or result.get("writable")):
            return result["date"]
        return None

    def extract_text(self, image_path: str) -> str:
        """Extract document text for LAF folder hints.

        This is deliberately separate from captcha OCR. In guarded consensus
        mode, high-quality consensus text is preferred; otherwise it falls back
        to macOS Vision. The returned text may be used for hints only and does
        not by itself authorize portal submission.
        """
        p = Path(image_path or "")
        if not p.exists() or not p.is_file():
            logger.error("Image not found at path: %s", image_path)
            return ""

        consensus_result = None
        if self._consensus_enabled() or self._consensus_shadow():
            consensus_result = self._run_consensus_ocr(str(p))
            self._write_consensus_metrics(
                str(p),
                consensus_result,
                legacy_date=None,
                mode="extract_text_enabled" if self._consensus_enabled() else "extract_text_shadow",
            )
            if self._consensus_enabled() and consensus_result and consensus_result.success:
                text = (consensus_result.corrected_text or consensus_result.selected_text or "").strip()
                if text:
                    return text

        try:
            from skills.apple.apple_intelligence import ocr_image

            ocr_result = ocr_image(str(p), engine="vision")
            if ocr_result.get("success") and ocr_result.get("text"):
                return str(ocr_result["text"]).strip()
        except Exception as e:
            logger.warning("LAFVision text OCR fallback failed: %s", e)
        return ""

    # ------------------------------------------------------------------
    # Public API — new metadata method (Phase F)
    # ------------------------------------------------------------------

    def extract_start_date_with_metadata(self, image_path: str) -> Dict[str, Any]:
        """
        Return structured extraction result including confidence and audit info.

        Schema:
          {
            "success": bool,
            "date": Optional[str],      # date string or None
            "confidence": float,        # 0.0–1.0
            "warnings": List[str],
            "provider_trace": Dict,     # diagnostic info (no raw text)
            "writable": bool,           # True only when confidence >= 0.75 and no conflict
          }

        Feature flags:
          MAGI_LAF_OCR_CONSENSUS_ENABLE=1
          MAGI_LAF_OCR_CONSENSUS_SHADOW=0

        Modes:
          enable=0, shadow=0 → legacy only (no new side-effects)
          enable=0, shadow=1 → both run, legacy result returned, diff logged
          enable=1           → consensus result used; conflict → date=None, writable=False
        """
        _consensus_enable = self._consensus_enabled()
        _shadow = self._consensus_shadow()

        # --- flag off: pure legacy, zero new side-effects ---
        if not _consensus_enable and not _shadow:
            legacy_date = self._extract_via_legacy(image_path)
            return {
                "success": legacy_date is not None,
                "date": legacy_date,
                "confidence": 0.5 if legacy_date is not None else 0.0,
                "warnings": [],
                "provider_trace": {"mode": "legacy"},
                "writable": legacy_date is not None,
            }

        # --- shadow or enable: always run legacy first ---
        legacy_date = self._extract_via_legacy(image_path)
        consensus_result = self._run_consensus_ocr(image_path)

        # --- shadow mode: log diff, return legacy ---
        if _shadow and not _consensus_enable:
            self._write_consensus_metrics(image_path, consensus_result, legacy_date, "shadow")
            return {
                "success": legacy_date is not None,
                "date": legacy_date,
                "confidence": 0.5 if legacy_date is not None else 0.0,
                "warnings": ["shadow_mode:result_from_legacy"],
                "provider_trace": {
                    "mode": "shadow",
                    "consensus_success": bool(
                        consensus_result and consensus_result.success
                    ),
                },
                "writable": legacy_date is not None,
            }

        # --- enable mode: use consensus ---
        self._write_consensus_metrics(image_path, consensus_result, legacy_date, "enabled")

        if not consensus_result or not consensus_result.success:
            # consensus unavailable → fallback legacy
            return {
                "success": legacy_date is not None,
                "date": legacy_date,
                "confidence": 0.4 if legacy_date is not None else 0.0,
                "warnings": ["consensus_unavailable:fallback_to_legacy"],
                "provider_trace": {"mode": "enabled", "consensus_available": False},
                "writable": False,
            }

        # consensus succeeded — check critical_conflict (date diff > 30 days)
        if consensus_result.critical_conflict:
            return {
                "success": False,
                "date": None,
                "confidence": 0.0,
                "warnings": list(consensus_result.warnings) + [
                    "critical_conflict:date_or_case_mismatch"
                ],
                "provider_trace": {
                    "mode": "enabled",
                    "consensus_confidence": consensus_result.confidence,
                    "critical_conflict": True,
                },
                "writable": False,
            }

        # Clean consensus: pass consensus text to gateway for date parsing
        consensus_text = (
            consensus_result.corrected_text or consensus_result.selected_text or ""
        ).strip()

        if not consensus_text:
            return {
                "success": legacy_date is not None,
                "date": legacy_date,
                "confidence": 0.3 if legacy_date is not None else 0.0,
                "warnings": ["consensus_empty_text:fallback_to_legacy"],
                "provider_trace": {"mode": "enabled", "consensus_text_empty": True},
                "writable": False,
            }

        timeout = int(os.environ.get("LAF_VISION_TIMEOUT_SECONDS", "45") or "45")
        prompt = (
            "這是一份法律扶助基金會(法扶)的委任狀或開辦相關資料。\n"
            "請幫我尋找文件上的日期，作為「開辦日期」的依據。\n"
            "【判斷優先順序】：\n"
            "1. 優先尋找法院或機關的「收文章章戳日期」（尤為重要，代表已遞件）。\n"
            "2. 若無收文章，請尋找律師手寫簽署的「受任日期」或落款日期。\n"
            "請「只」回傳你判斷出最符合上述優先順序的日期 (格式如 YYYYMMDD 或 YYYY-MM-DD，西元年)，不要包含任何其他文字或解釋。\n"
            "如果整份文件完全找不到任何相關日期，請回傳字串 'None'。"
        )
        result_dict = self.gateway.dispatch(
            prompt="{}\n\n[文件文字內容 (OCR共識)]\n{}".format(prompt, consensus_text),
            task_type="date_extract",
            timeout=max(15, timeout),
            cross_validate=True,
            tc_review=False,
        )

        if not result_dict.get("success"):
            logger.warning(
                "LAFVision consensus path: gateway failed, falling back to legacy date"
            )
            return {
                "success": legacy_date is not None,
                "date": legacy_date,
                "confidence": 0.35 if legacy_date is not None else 0.0,
                "warnings": ["consensus_gateway_failed:fallback_to_legacy"],
                "provider_trace": {
                    "mode": "enabled",
                    "gateway_error": result_dict.get("error", "unknown"),
                },
                "writable": False,
            }

        result_text = str(
            result_dict.get("analysis")
            or result_dict.get("response")
            or result_dict.get("text")
            or ""
        ).strip()

        if not result_text or "none" in result_text.lower():
            return {
                "success": False,
                "date": None,
                "confidence": consensus_result.confidence * 0.5,
                "warnings": ["gateway_returned_none"],
                "provider_trace": {
                    "mode": "enabled",
                    "consensus_confidence": consensus_result.confidence,
                },
                "writable": False,
            }

        return {
            "success": True,
            "date": result_text,
            "confidence": consensus_result.confidence,
            "warnings": list(consensus_result.warnings),
            "provider_trace": {
                "mode": "enabled",
                "consensus_confidence": consensus_result.confidence,
                "writable": consensus_result.writable,
                "route": result_dict.get("route", ""),
            },
            "writable": consensus_result.writable,
        }
