# -*- coding: utf-8 -*-
"""Process-wide OCR engines for long-running MAGI services.

ONNX Runtime creates a native worker pool for every inference session.  The
long-running V2 API used to construct fresh captcha solvers repeatedly, so the
otherwise short-lived OCR sessions accumulated hundreds of native threads and
several gigabytes of compressed memory.  This module keeps one lazy engine of
each kind per process and serialises access because the upstream engines mutate
working buffers during inference.

Keep this module Python 3.9 compatible; V2 utilities still exercise that
runtime in isolated jobs.
"""

from __future__ import annotations

import os
import json
import subprocess
import sys
import threading
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, Optional


_INIT_LOCK = threading.RLock()
_RAPID_CALL_LOCK = threading.RLock()
_DDDD_CALL_LOCK = threading.RLock()
_RAPID_ENGINE = None
_DDDD_ENGINE = None


def _bounded_thread_count(name: str, default: int = 1) -> int:
    try:
        value = int(os.environ.get(name, str(default)) or str(default))
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, 2))


def _legacy_rapid_result(output: Any):
    """Convert RapidOCR 3.x output to the tuple returned by the legacy API."""
    if isinstance(output, tuple):
        return output

    texts_value = getattr(output, "txts", None)
    boxes_value = getattr(output, "boxes", None)
    scores_value = getattr(output, "scores", None)
    texts = list(texts_value) if texts_value is not None else []
    boxes = list(boxes_value) if boxes_value is not None else []
    scores = list(scores_value) if scores_value is not None else []
    rows = []
    for index, text in enumerate(texts):
        box = boxes[index] if index < len(boxes) else []
        if hasattr(box, "tolist"):
            box = box.tolist()
        elif isinstance(box, (list, tuple)):
            box = [list(point) if isinstance(point, (list, tuple)) else point for point in box]
        score = scores[index] if index < len(scores) else 0.0
        rows.append([box, str(text), str(score)])
    elapsed = getattr(output, "elapse_list", None)
    return (rows or None, elapsed)


class _LockedRapidOCR(object):
    def __init__(self, engine: Any):
        self._engine = engine

    def __call__(self, *args: Any, **kwargs: Any):
        with _RAPID_CALL_LOCK:
            return _legacy_rapid_result(self._engine(*args, **kwargs))


class _LockedDdddOCR(object):
    def __init__(self, engine: Any):
        self._engine = engine

    def classification(self, *args: Any, **kwargs: Any):
        with _DDDD_CALL_LOCK:
            return self._engine.classification(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._engine, name)


def _build_capped_legacy_engine(intra: int, inter: int):
    from rapidocr_onnxruntime import RapidOCR
    from rapidocr_onnxruntime import utils as rapid_utils

    original_session_options = rapid_utils.SessionOptions

    def capped_session_options():
        options = original_session_options()
        options.intra_op_num_threads = intra
        options.inter_op_num_threads = inter
        return options

    rapid_utils.SessionOptions = capped_session_options
    try:
        return RapidOCR()
    finally:
        rapid_utils.SessionOptions = original_session_options


def _rapidocr_input_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, (str, Path)):
        return Path(value).expanduser().read_bytes()
    if hasattr(value, "save"):
        buffer = BytesIO()
        value.save(buffer, format="PNG")
        return buffer.getvalue()
    try:
        from PIL import Image

        buffer = BytesIO()
        Image.fromarray(value).save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception as exc:
        raise TypeError("unsupported RapidOCR input: %s" % type(value).__name__) from exc


class _RapidOCRSubprocess(object):
    """Run the high-watermark OCR model outside the persistent API process."""

    def __init__(self, intra: int, inter: int):
        self.intra = intra
        self.inter = inter
        self.root = Path(__file__).resolve().parents[3]

    def __call__(self, value: Any, **_kwargs: Any):
        payload = _rapidocr_input_bytes(value)
        if len(payload) > 20 * 1024 * 1024:
            raise ValueError("RapidOCR input exceeds 20 MiB")
        env = os.environ.copy()
        env["MAGI_RAPIDOCR_INTRA_THREADS"] = str(self.intra)
        env["MAGI_RAPIDOCR_INTER_THREADS"] = str(self.inter)
        current_path = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = str(self.root) + (os.pathsep + current_path if current_path else "")
        timeout = max(10, min(120, int(env.get("MAGI_RAPIDOCR_WORKER_TIMEOUT_SEC", "30") or "30")))
        completed = subprocess.run(
            [sys.executable, "-m", "skills.engine.ocr.rapidocr_worker"],
            input=payload,
            capture_output=True,
            cwd=str(self.root),
            env=env,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace")[-500:]
            raise RuntimeError("RapidOCR worker failed: %s" % detail)
        result = json.loads(completed.stdout.decode("utf-8"))
        return result.get("rows"), result.get("elapsed")


def get_shared_rapidocr(log: Optional[Callable[[str], None]] = None):
    """Return one lightweight proxy; model memory lives in a short-lived worker."""
    global _RAPID_ENGINE
    if _RAPID_ENGINE is not None:
        return _RAPID_ENGINE

    with _INIT_LOCK:
        if _RAPID_ENGINE is not None:
            return _RAPID_ENGINE

        intra = _bounded_thread_count("MAGI_RAPIDOCR_INTRA_THREADS", 1)
        inter = _bounded_thread_count("MAGI_RAPIDOCR_INTER_THREADS", 1)
        in_process = os.environ.get("MAGI_RAPIDOCR_IN_PROCESS", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if not in_process:
            _RAPID_ENGINE = _RapidOCRSubprocess(intra, inter)
            return _RAPID_ENGINE

        def build_modern():
            from rapidocr import RapidOCR

            params: Dict[str, Any] = {
                "EngineConfig.onnxruntime.intra_op_num_threads": intra,
                "EngineConfig.onnxruntime.inter_op_num_threads": inter,
            }
            return RapidOCR(params=params)

        use_modern = os.environ.get("MAGI_RAPIDOCR_USE_MODERN", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        try:
            # Keep V2's proven OCR models/output by default.  The newer package
            # retains a much larger arena after first inference on this Mac.
            engine = build_modern() if use_modern else _build_capped_legacy_engine(intra, inter)
        except Exception as primary_error:
            try:
                engine = _build_capped_legacy_engine(intra, inter) if use_modern else build_modern()
            except Exception:
                if log:
                    log("RapidOCR 初始化失敗: %s" % primary_error)
                return None

        _RAPID_ENGINE = _LockedRapidOCR(engine)
        return _RAPID_ENGINE


def get_shared_ddddocr(
    factory: Callable[..., Any],
    kwargs: Optional[Dict[str, Any]] = None,
    log: Optional[Callable[[str], None]] = None,
):
    """Return one lazy ddddocr model for this process."""
    global _DDDD_ENGINE
    if _DDDD_ENGINE is not None:
        return _DDDD_ENGINE

    with _INIT_LOCK:
        if _DDDD_ENGINE is not None:
            return _DDDD_ENGINE
        try:
            engine = factory(**dict(kwargs or {}))
        except Exception as exc:
            if log:
                log("ddddocr 初始化失敗: %s" % exc)
            return None
        _DDDD_ENGINE = _LockedDdddOCR(engine)
        return _DDDD_ENGINE


def _reset_shared_ocr_for_tests() -> None:
    global _RAPID_ENGINE, _DDDD_ENGINE
    with _INIT_LOCK:
        _RAPID_ENGINE = None
        _DDDD_ENGINE = None
