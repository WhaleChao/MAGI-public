# -*- coding: utf-8 -*-
"""
OCR dataclass schema.

NOTE: 命名為 ocr_schema.py 而非 schema.py，避免遮蔽 third-party `schema` 套件。

三個核心 dataclass:
  OCREntities        — 從 OCR 原始文字抽取的法律實體（全純函式，不呼叫 LLM）
  OCRProviderResult  — 單一 provider（Tesseract/Apple Vision）執行結果
  OCRConsensusResult — 兩 provider 並行的共識結果

欄位命名原則（避免語義混淆）：
  raw_text       — provider 直接輸出的原始字串（未修正）
  corrected_text — legal_corrector 套用後的字串（僅做 deterministic 字元修正）
  selected_text  — consensus 選出或加權合成的最終文字
  confidence     — MAGI 自算：quality_score + entity agreement 加權，≠ tesseract 原生信心值

Python 3.9 + 3.14 相容：使用 typing.Optional / List / Dict 而非 str|None / dict[str, Any]。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class OCREntities:
    """從 OCR 文字抽取的法律實體（純 regex / deterministic，無 LLM）。

    所有欄位均為字串或字串列表，值都是**繁體中文正規化**後的結果。
    """
    # 法院案號（ROC 格式如 114年度訴字第123號）
    case_numbers: List[str] = field(default_factory=list)
    # ROC 民國日期（如 114年3月15日 / 114/3/15）
    roc_dates: List[str] = field(default_factory=list)
    # 法院全名（如 臺灣花蓮地方法院）
    courts: List[str] = field(default_factory=list)
    # 當事人姓名（以空格或換行分隔的 OCR 人名候選）
    parties: List[str] = field(default_factory=list)
    # 法扶案號（如 1141121-E-005）
    laf_case_numbers: List[str] = field(default_factory=list)

    def to_counts(self) -> Dict[str, int]:
        """回傳各欄位數量（用於 metrics，不含字串內容）。"""
        return {
            "case_numbers_found": len(self.case_numbers),
            "roc_dates_found": len(self.roc_dates),
            "courts_found": len(self.courts),
            "parties_found": len(self.parties),
            "laf_case_numbers_found": len(self.laf_case_numbers),
        }


@dataclass
class OCRProviderResult:
    """單一 provider（Tesseract 或 Apple Vision）執行結果。"""
    success: bool
    provider: str                           # "tesseract" | "apple_vision"
    raw_text: str = ""                      # provider 直接輸出（未修正）
    corrected_text: str = ""               # deterministic legal_corrector 套用後
    quality_score: float = 0.0             # quality.py 計算，0.0~1.0
    entities: Optional[OCREntities] = None  # legal_entities.py 抽取結果
    error: Optional[str] = None            # 失敗時的錯誤訊息
    duration_sec: float = 0.0             # 執行耗時
    psm: Optional[int] = None             # Tesseract PSM 策略（其他 provider 為 None）
    timed_out: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "provider": self.provider,
            "raw_text_len": len(self.raw_text),
            "corrected_text_len": len(self.corrected_text),
            "quality_score": round(self.quality_score, 4),
            "entities_counts": self.entities.to_counts() if self.entities else {},
            "error": self.error,
            "duration_sec": round(self.duration_sec, 3),
            "psm": self.psm,
            "timed_out": self.timed_out,
        }

    @classmethod
    def failure(
        cls,
        provider: str,
        error: str,
        timed_out: bool = False,
    ) -> "OCRProviderResult":
        """建立失敗結果的便捷方法。"""
        return cls(
            success=False,
            provider=provider,
            error=error,
            timed_out=timed_out,
        )


@dataclass
class OCRConsensusResult:
    """Tesseract + Apple Vision 並行共識結果。

    confidence 由以下加權計算（非 tesseract 原生信心值）：
      - quality_score 平均: 權重 0.4
      - case_numbers 集合完全相等: +0.3
      - roc_dates 集合交集非空: +0.2
      - courts 集合交集非空: +0.1
    """
    success: bool
    selected_text: str = ""          # consensus 選出或加權合成的最終文字
    corrected_text: str = ""         # legal_corrector 套用在 selected_text 上
    confidence: float = 0.0          # MAGI 自算，0.0~1.0
    writable: bool = False           # confidence >= 0.75 且無 critical conflict
    warnings: List[str] = field(default_factory=list)
    critical_conflict: bool = False  # 案號非空且完全不交集，或日期差 > 30 天
    provider_results: Dict[str, "OCRProviderResult"] = field(default_factory=dict)
    entities: Optional[OCREntities] = None  # 從 selected_text 抽取
    error: Optional[str] = None
    duration_sec: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "selected_text_len": len(self.selected_text),
            "corrected_text_len": len(self.corrected_text),
            "confidence": round(self.confidence, 4),
            "writable": self.writable,
            "warnings": self.warnings,
            "critical_conflict": self.critical_conflict,
            "providers": {
                name: r.to_dict() for name, r in self.provider_results.items()
            },
            "entities_counts": self.entities.to_counts() if self.entities else {},
            "error": self.error,
            "duration_sec": round(self.duration_sec, 3),
        }

    @classmethod
    def failure(cls, error: str) -> "OCRConsensusResult":
        return cls(success=False, error=error)
