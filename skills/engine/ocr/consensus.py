# -*- coding: utf-8 -*-
"""
OCR 共識引擎：Tesseract + Apple Vision 並行執行，加權選出最佳文字。

設計原則：
  - 雙 provider 並行執行（ThreadPoolExecutor max_workers=2），共享 wall-clock timeout
  - 單邊 timeout：使用另一邊的結果 + warning
  - 雙邊 timeout / 雙邊失敗：回 success=False
  - confidence 由四項加權計算（非 tesseract 原生信心值）
  - writable=True 僅在 confidence >= 0.75 且無 critical_conflict
  - 不呼叫 LLM；不改呼叫端（pdf_bridge / laf_vision / tools_api 均不動）

Feature flags:
  MAGI_OCR_CONSENSUS_TIMEOUT_SEC=60   (整體 wall-clock timeout)
  MAGI_TESSERACT_ENABLE=1             (轉發給 tesseract_provider)
  MAGI_APPLE_VISION_OCR_ENABLE=1      (轉發給 apple_vision_provider)

Confidence 計算公式（總上限 1.0）：
  base   = (tess_quality + vision_quality) / 2  * 0.4
  agree  = case_numbers 集合完全相等         ? +0.3 : 0
         + roc_dates 集合交集非空           ? +0.2 : 0
         + courts 集合交集非空              ? +0.1 : 0
  confidence = min(base + agree, 1.0)

Critical conflict 判定（任一成立即 True）：
  1. 兩邊都有案號，且集合完全不交集
  2. 兩邊都有日期，且日期差 > 30 天（解析第一個日期比較）

Python 3.9 + 3.14 相容。
"""

from __future__ import annotations

import concurrent.futures
import os
import re
import time
from typing import Dict, List, Optional, Tuple

from skills.engine.ocr.ocr_schema import (
    OCRConsensusResult,
    OCREntities,
    OCRProviderResult,
)
from skills.engine.ocr.legal_entities import extract_entities
from skills.engine.ocr.legal_corrector import correct_legal_text
from skills.engine.ocr import tesseract_provider, apple_vision_provider


# --- 環境變數 ---------------------------------------------------------------

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)).strip())
    except (ValueError, AttributeError):
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


# --- 信心度計算 -------------------------------------------------------------

def _compute_confidence(
    tess: OCRProviderResult,
    vision: OCRProviderResult,
) -> Tuple[float, bool, List[str]]:
    """計算 confidence、critical_conflict 與 warnings。

    Returns:
        (confidence, critical_conflict, warnings)
    """
    warnings: List[str] = []
    critical_conflict = False

    # 兩個都失敗 → 最低分
    if not tess.success and not vision.success:
        return 0.0, False, ["both providers failed"]

    # 只有一邊成功 → 部分分數
    if not tess.success:
        warnings.append(f"tesseract unavailable: {tess.error}")
        qs = vision.quality_score
        conf = min(qs * 0.4, 0.4)
        return round(conf, 4), False, warnings
    if not vision.success:
        warnings.append(f"apple_vision unavailable: {vision.error}")
        qs = tess.quality_score
        conf = min(qs * 0.4, 0.4)
        return round(conf, 4), False, warnings

    # 兩邊都成功
    base = ((tess.quality_score + vision.quality_score) / 2.0) * 0.4

    # Entity agreement
    tess_ents = tess.entities or OCREntities()
    vis_ents = vision.entities or OCREntities()

    agree = 0.0

    # case_numbers: 集合完全相等 +0.3
    tess_cn = set(tess_ents.case_numbers)
    vis_cn = set(vis_ents.case_numbers)
    if tess_cn and vis_cn:
        if tess_cn == vis_cn:
            agree += 0.3
        elif tess_cn.isdisjoint(vis_cn):
            # 完全不交集 → critical conflict
            critical_conflict = True
            warnings.append(
                f"critical: case_number mismatch {sorted(tess_cn)} vs {sorted(vis_cn)}"
            )
        else:
            agree += 0.15  # 部分相同
    elif tess_cn == vis_cn:
        # 兩邊都空 → 中性
        pass

    # roc_dates: 交集非空 +0.2
    tess_dt = set(tess_ents.roc_dates)
    vis_dt = set(vis_ents.roc_dates)
    if tess_dt and vis_dt:
        inter_dt = tess_dt & vis_dt
        if inter_dt:
            agree += 0.2
        else:
            # 日期差 > 30 天檢查
            date_diff_conflict = _check_date_conflict(
                list(tess_dt), list(vis_dt)
            )
            if date_diff_conflict:
                critical_conflict = True
                warnings.append(
                    f"critical: date conflict tess={sorted(tess_dt)} "
                    f"vs vision={sorted(vis_dt)}"
                )
            else:
                warnings.append("roc_dates differ between providers")
    elif tess_dt or vis_dt:
        warnings.append("roc_dates only from one provider")

    # courts: 交集非空 +0.1
    tess_ct = set(tess_ents.courts)
    vis_ct = set(vis_ents.courts)
    if tess_ct and vis_ct and (tess_ct & vis_ct):
        agree += 0.1
    elif tess_ct != vis_ct and (tess_ct or vis_ct):
        warnings.append("courts differ between providers")

    confidence = min(base + agree, 1.0)
    return round(confidence, 4), critical_conflict, warnings


# ROC 日期解析（最小實作，不依賴 dateutil / calendar）
_ROC_DATE_RE = re.compile(r"(\d{2,3})[年/](\d{1,2})[月/](\d{1,2})")


def _parse_roc_date_to_days(date_str: str) -> Optional[int]:
    """將 ROC 日期字串轉成「自 ROC 元年起的大略天數」（只用於差值比較）。

    失敗回 None。
    """
    m = _ROC_DATE_RE.search(date_str)
    if not m:
        return None
    try:
        y = int(m.group(1))
        mo = int(m.group(2))
        d = int(m.group(3))
        return y * 365 + mo * 30 + d  # 近似計算，足夠判斷 30 天差異
    except (ValueError, AttributeError):
        return None


def _check_date_conflict(tess_dates: List[str], vis_dates: List[str]) -> bool:
    """檢查兩個日期列表的第一個元素差是否 > 30 天。"""
    if not tess_dates or not vis_dates:
        return False
    t_days = _parse_roc_date_to_days(tess_dates[0])
    v_days = _parse_roc_date_to_days(vis_dates[0])
    if t_days is None or v_days is None:
        return False
    return abs(t_days - v_days) > 30


# --- 文字選取 ----------------------------------------------------------------

def _select_text(
    tess: OCRProviderResult,
    vision: OCRProviderResult,
) -> str:
    """選出品質較高的 provider 文字（或合成）。

    - 只有一邊成功 → 直接用那邊
    - 兩邊都成功 → 用 quality_score 較高的那邊
    - 品質差距 < 0.05 → 用 corrected_text 字元數較長的（通常含更多資訊）
    """
    if not tess.success and not vision.success:
        return ""
    if not tess.success:
        return vision.corrected_text or vision.raw_text
    if not vision.success:
        return tess.corrected_text or tess.raw_text

    t_score = tess.quality_score
    v_score = vision.quality_score

    if abs(t_score - v_score) < 0.05:
        # 品質相近 → 用字元數較長的
        t_text = tess.corrected_text or tess.raw_text
        v_text = vision.corrected_text or vision.raw_text
        return t_text if len(t_text) >= len(v_text) else v_text

    if t_score >= v_score:
        return tess.corrected_text or tess.raw_text
    return vision.corrected_text or vision.raw_text


def _select_best_text(results: Dict[str, OCRProviderResult]) -> str:
    """Select the highest-quality successful provider output."""
    successful = [r for r in results.values() if r and r.success]
    if not successful:
        return ""
    successful.sort(
        key=lambda r: (
            float(r.quality_score or 0.0),
            len(r.corrected_text or r.raw_text or ""),
        ),
        reverse=True,
    )
    best = successful[0]
    return best.corrected_text or best.raw_text


# --- 主入口 -----------------------------------------------------------------

def run_consensus(
    image_path: str,
    task_type: str = "legal",
    timeout_sec: Optional[float] = None,
) -> OCRConsensusResult:
    """對 image_path 同時執行 Tesseract + Apple Vision，回傳共識結果。

    Args:
        image_path: PNG/JPEG/TIFF 圖片路徑。
        task_type:  傳給兩個 provider；"captcha" 時 bypass legal_corrector。
        timeout_sec: 整體 wall-clock timeout。預設讀 MAGI_OCR_CONSENSUS_TIMEOUT_SEC (60s)。

    Returns:
        OCRConsensusResult，失敗時 success=False + error 說明。
        **禁止 raise**，所有例外封裝在 error 欄位。
    """
    t0 = time.monotonic()

    if timeout_sec is None:
        timeout_sec = _env_float("MAGI_OCR_CONSENSUS_TIMEOUT_SEC", 60.0)

    if not image_path or not os.path.isfile(image_path):
        return OCRConsensusResult.failure(
            f"image file not found: {image_path!r}"
        )

    nemotron_enabled = _env_bool("MAGI_NEMOTRON_PARSE_ENABLE", False) and task_type != "captcha"

    # 並行執行 provider。Nemotron Parse 是明確啟用後的第三 provider。
    tess_result: Optional[OCRProviderResult] = None
    vision_result: Optional[OCRProviderResult] = None
    nemotron_result: Optional[OCRProviderResult] = None

    def _run_tess() -> OCRProviderResult:
        return tesseract_provider.run(
            image_path,
            task_type=task_type,
            timeout_sec=max(timeout_sec * 0.85, 10.0),
        )

    def _run_vision() -> OCRProviderResult:
        return apple_vision_provider.run(
            image_path,
            task_type=task_type,
            timeout_sec=max(timeout_sec * 0.85, 10.0),
        )

    def _run_nemotron() -> OCRProviderResult:
        from skills.engine.ocr import nemotron_parse_provider

        return nemotron_parse_provider.run(
            image_path,
            task_type=task_type,
            timeout_sec=max(timeout_sec * 0.95, 10.0),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=3 if nemotron_enabled else 2) as executor:
        fut_tess = executor.submit(_run_tess)
        fut_vision = executor.submit(_run_vision)
        fut_nemotron = executor.submit(_run_nemotron) if nemotron_enabled else None

        remaining = timeout_sec - (time.monotonic() - t0)

        try:
            tess_result = fut_tess.result(timeout=max(remaining, 1.0))
        except concurrent.futures.TimeoutError:
            tess_result = OCRProviderResult.failure(
                "tesseract",
                f"consensus timeout after {timeout_sec}s",
                timed_out=True,
            )
        except Exception as e:
            tess_result = OCRProviderResult.failure(
                "tesseract",
                f"executor error: {type(e).__name__}: {e}",
            )

        remaining = timeout_sec - (time.monotonic() - t0)

        try:
            vision_result = fut_vision.result(timeout=max(remaining, 1.0))
        except concurrent.futures.TimeoutError:
            vision_result = OCRProviderResult.failure(
                "apple_vision",
                f"consensus timeout after {timeout_sec}s",
                timed_out=True,
            )
        except Exception as e:
            vision_result = OCRProviderResult.failure(
                "apple_vision",
                f"executor error: {type(e).__name__}: {e}",
            )

        if fut_nemotron is not None:
            remaining = timeout_sec - (time.monotonic() - t0)
            try:
                nemotron_result = fut_nemotron.result(timeout=max(remaining, 1.0))
            except concurrent.futures.TimeoutError:
                nemotron_result = OCRProviderResult.failure(
                    "nemotron_parse_mlx",
                    f"consensus timeout after {timeout_sec}s",
                    timed_out=True,
                )
            except Exception as e:
                nemotron_result = OCRProviderResult.failure(
                    "nemotron_parse_mlx",
                    f"executor error: {type(e).__name__}: {e}",
                )

    # 至此兩個結果均非 None
    provider_results: Dict[str, OCRProviderResult] = {
        "tesseract": tess_result,
        "apple_vision": vision_result,
    }
    if nemotron_result is not None:
        provider_results["nemotron_parse_mlx"] = nemotron_result

    # 全部失敗
    if not any(r.success for r in provider_results.values()):
        duration = time.monotonic() - t0
        all_timed_out = all(r.timed_out for r in provider_results.values())
        return OCRConsensusResult(
            success=False,
            provider_results=provider_results,
            error=(
                "consensus timeout: all providers timed out"
                if all_timed_out
                else "all providers failed: "
                + "; ".join(f"{name}={r.error!r}" for name, r in provider_results.items())
            ),
            duration_sec=round(duration, 3),
        )

    # 計算 confidence / critical_conflict / warnings
    confidence, critical_conflict, warnings = _compute_confidence(
        tess_result, vision_result
    )

    # 選出最佳文字。未啟用 Nemotron 時保留原兩-provider 行為。
    if nemotron_result is not None and nemotron_result.success:
        selected_text = _select_best_text(provider_results)
        confidence = max(confidence, round(float(nemotron_result.quality_score or 0.0), 4))
        warnings = [w for w in warnings if w != "both providers failed"]
        warnings.append("nemotron_parse_mlx enabled")
    else:
        selected_text = _select_text(tess_result, vision_result)

    # 對 selected_text 再做一次修正（不依賴任何 provider 的 corrected_text）
    if task_type == "captcha":
        final_corrected = selected_text
    else:
        correction = correct_legal_text(selected_text, task_type=task_type)
        final_corrected = correction.corrected_text

    # 實體抽取
    entities: Optional[OCREntities] = None
    if task_type != "captcha":
        entities = extract_entities(final_corrected)

    # writable 判定
    writable = confidence >= 0.75 and not critical_conflict

    # 0.55~0.75 警告
    if 0.55 <= confidence < 0.75 and not critical_conflict:
        warnings.append(
            f"confidence {confidence:.2f} < 0.75: result usable but not auto-writable"
        )

    # < 0.55
    if confidence < 0.55:
        warnings.append(
            f"confidence {confidence:.2f} < 0.55: low reliability"
        )

    duration = time.monotonic() - t0

    return OCRConsensusResult(
        success=True,
        selected_text=selected_text,
        corrected_text=final_corrected,
        confidence=confidence,
        writable=writable,
        warnings=warnings,
        critical_conflict=critical_conflict,
        provider_results=provider_results,
        entities=entities,
        duration_sec=round(duration, 3),
    )
