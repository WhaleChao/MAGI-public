# -*- coding: utf-8 -*-
"""Sidecar entrypoint for PKUSeg on a Python-compatible interpreter."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pkuseg

_ROOT = Path(__file__).resolve().parent
_LEGAL_DICT = _ROOT / "legal_dict.txt"
_PKUSEG_MODEL = str(os.environ.get("MAGI_PKUSEG_MODEL", "") or "").strip()
_SEGMENTER_KWARGS = {"user_dict": str(_LEGAL_DICT) if _LEGAL_DICT.exists() else None}
if _PKUSEG_MODEL:
    _SEGMENTER_KWARGS["model_name"] = _PKUSEG_MODEL
_SEGMENTER = pkuseg.pkuseg(**_SEGMENTER_KWARGS)


def main() -> int:
    raw = sys.stdin.read()
    payload = json.loads(raw or "[]")
    if not isinstance(payload, list):
        raise ValueError("payload must be a JSON list of texts")
    results = []
    for item in payload:
        text = str(item or "").strip()
        if not text:
            results.append([])
            continue
        results.append([tok for tok in _SEGMENTER.cut(text) if str(tok or "").strip()])
    sys.stdout.write(json.dumps(results, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
