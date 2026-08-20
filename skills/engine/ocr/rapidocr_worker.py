# -*- coding: utf-8 -*-
"""One-shot RapidOCR worker used by the long-running MAGI API."""

from __future__ import annotations

import json
import sys

from skills.engine.ocr.shared_runtime import (
    _bounded_thread_count,
    _build_capped_legacy_engine,
)


def run_payload(payload: bytes):
    if not payload:
        raise ValueError("empty RapidOCR payload")
    if len(payload) > 20 * 1024 * 1024:
        raise ValueError("RapidOCR payload exceeds 20 MiB")
    intra = _bounded_thread_count("MAGI_RAPIDOCR_INTRA_THREADS", 1)
    inter = _bounded_thread_count("MAGI_RAPIDOCR_INTER_THREADS", 1)
    engine = _build_capped_legacy_engine(intra, inter)
    rows, elapsed = engine(payload)
    return {"rows": rows, "elapsed": elapsed}


def main() -> int:
    try:
        payload = sys.stdin.buffer.read(20 * 1024 * 1024 + 1)
        result = run_payload(payload)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:
        sys.stderr.write("%s: %s\n" % (type(exc).__name__, exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
