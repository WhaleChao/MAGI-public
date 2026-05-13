# -*- coding: utf-8 -*-
"""Chinese NLP helpers for MAGI memory/vector pipelines.

Primary path:
- Use native ``pkuseg`` when available in the current interpreter.

Compatibility path:
- If MAGI runs on Python 3.14 and native ``pkuseg`` cannot be imported,
  delegate segmentation to a Python 3.11 sidecar environment when present.

Fallback path:
- Fall back to macOS NaturalLanguage tokenization or a minimal regex splitter.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

logger = logging.getLogger("chinese_nlp")

_NLP_DIR = Path(__file__).resolve().parent
_LEGAL_DICT = _NLP_DIR / "legal_dict.txt"
_STOPWORDS = _NLP_DIR / "stopwords_zh.txt"
_SIDECAR_SCRIPT = _NLP_DIR / "chinese_nlp_sidecar.py"
_MAGI_ROOT = _NLP_DIR.parent.parent
_DEFAULT_SIDECAR_PYTHON = _MAGI_ROOT / ".runtime" / "pkuseg_py311" / "bin" / "python"
_SIDECAR_TIMEOUT_SEC = int(os.environ.get("MAGI_PKUSEG_SIDECAR_TIMEOUT_SEC", "20") or "20")
_PKUSEG_MODEL = str(os.environ.get("MAGI_PKUSEG_MODEL", "") or "").strip()

_segmenter = None
_seg_lock = threading.Lock()
_cached_stopwords = None

# Global slot limit for chinese_nlp_sidecar.py across ALL processes (including
# parallel pytest workers and Codex agent sessions).  threading.BoundedSemaphore
# only protects within one process; we use a tmp-dir slot approach so that even
# 200 parallel pytest subprocesses combined never exceed GLOBAL_MAX sidecars.
_SIDECAR_MAX_CONCURRENT = int(os.environ.get("MAGI_PKUSEG_SIDECAR_MAX_CONCURRENT", "4") or "4")
_SIDECAR_GLOBAL_MAX = int(os.environ.get("MAGI_PKUSEG_SIDECAR_GLOBAL_MAX", "4") or "4")
_sidecar_sem = threading.BoundedSemaphore(_SIDECAR_MAX_CONCURRENT)
_SLOT_DIR = Path("/tmp/magi_nlp_slots")


def _acquire_global_slot(timeout_sec: float = 2.0) -> Optional[Path]:
    """Atomically claim a slot file under /tmp/magi_nlp_slots/.
    Returns the slot Path on success, None if global cap already reached."""
    import time as _time
    try:
        _SLOT_DIR.mkdir(parents=True, exist_ok=True)
        # Purge stale slots (older than sidecar timeout * 3)
        stale_age = _SIDECAR_TIMEOUT_SEC * 3
        now = _time.time()
        for p in list(_SLOT_DIR.glob("*.slot")):
            try:
                if now - p.stat().st_mtime > stale_age:
                    p.unlink(missing_ok=True)
            except Exception:
                pass
        # Count live slots
        live = len(list(_SLOT_DIR.glob("*.slot")))
        if live >= _SIDECAR_GLOBAL_MAX:
            return None
        slot = _SLOT_DIR / f"{os.getpid()}_{threading.get_ident()}_{int(now*1000)}.slot"
        slot.touch()
        return slot
    except Exception:
        return None


def _release_global_slot(slot: Optional[Path]) -> None:
    if slot:
        try:
            slot.unlink(missing_ok=True)
        except Exception:
            pass


def _looks_chinese(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", str(text or "")))


class _FallbackSegmenter:
    """Minimal fallback when neither pkuseg nor Apple NL is available."""

    def cut(self, text: str) -> List[str]:
        return [w for w in re.split(r"[\s，。、；：！？（）《》「」【】『』〔〕\n\r\t]+", text) if w.strip()]

    def cut_many(self, texts: Sequence[str]) -> List[List[str]]:
        return [self.cut(text) for text in texts]


class _AppleSegmenter:
    """Use macOS NaturalLanguage.framework as a better-than-regex fallback."""

    def __init__(self):
        from skills.apple import natural_language

        self._nl = natural_language

    def cut(self, text: str) -> List[str]:
        tokens = self._nl.tokenize(text)
        return [tok.strip() for tok in tokens if str(tok or "").strip()]

    def cut_many(self, texts: Sequence[str]) -> List[List[str]]:
        return [self.cut(text) for text in texts]


class _NativePKUSegSegmenter:
    def __init__(self):
        import pkuseg

        user_dict = str(_LEGAL_DICT) if _LEGAL_DICT.exists() else None
        kwargs = {"user_dict": user_dict}
        if _PKUSEG_MODEL:
            kwargs["model_name"] = _PKUSEG_MODEL
        self._seg = pkuseg.pkuseg(**kwargs)
        logger.info(
            "PKUSeg native segmenter initialized (model=%s, dict=%s)",
            _PKUSEG_MODEL or "default",
            "legal_dict" if user_dict else "none",
        )

    def cut(self, text: str) -> List[str]:
        return [tok for tok in self._seg.cut(text) if str(tok or "").strip()]

    def cut_many(self, texts: Sequence[str]) -> List[List[str]]:
        return [self.cut(text) for text in texts]


class _SidecarPKUSegSegmenter:
    def __init__(self, python_path: str):
        self._python_path = python_path
        logger.info("PKUSeg sidecar enabled via %s", python_path)

    def _run(self, texts: Sequence[str]) -> List[List[str]]:
        if not texts:
            return []
        slot = _acquire_global_slot()
        if slot is None:
            raise RuntimeError("sidecar global cap reached — falling back to regex")
        with _sidecar_sem:
            try:
                proc = subprocess.run(
                    [self._python_path, str(_SIDECAR_SCRIPT)],
                    input=json.dumps(list(texts), ensure_ascii=False),
                    text=True,
                    capture_output=True,
                    timeout=max(5, _SIDECAR_TIMEOUT_SEC),
                    check=False,
                )
            finally:
                _release_global_slot(slot)
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            raise RuntimeError(stderr or "sidecar returned non-zero exit status")
        payload = json.loads(proc.stdout or "[]")
        if not isinstance(payload, list):
            raise RuntimeError("sidecar output is not a list")
        normalized = []
        for item in payload:
            if not isinstance(item, list):
                normalized.append([])
                continue
            normalized.append([str(tok).strip() for tok in item if str(tok).strip()])
        return normalized

    def cut(self, text: str) -> List[str]:
        results = self._run([text])
        return results[0] if results else []

    def cut_many(self, texts: Sequence[str]) -> List[List[str]]:
        return self._run(texts)


def _sidecar_python_candidates() -> Iterable[str]:
    env_path = str(os.environ.get("MAGI_PKUSEG_SIDECAR_PYTHON", "") or "").strip()
    if env_path:
        yield env_path
    yield str(_DEFAULT_SIDECAR_PYTHON)


def _build_segmenter():
    try:
        return _NativePKUSegSegmenter()
    except Exception as exc:
        logger.info("Native pkuseg unavailable in current interpreter: %s", exc)

    for candidate in _sidecar_python_candidates():
        if not candidate or not os.path.exists(candidate):
            continue
        try:
            return _SidecarPKUSegSegmenter(candidate)
        except Exception as exc:
            logger.warning("PKUSeg sidecar unavailable via %s: %s", candidate, exc)

    try:
        return _AppleSegmenter()
    except Exception as exc:
        logger.info("Apple NaturalLanguage tokenizer unavailable: %s", exc)

    logger.warning("Chinese NLP degraded to regex fallback segmenter")
    return _FallbackSegmenter()


def get_segmenter():
    """Return a singleton segmenter with lazy initialization."""
    global _segmenter
    if _segmenter is None:
        with _seg_lock:
            if _segmenter is None:
                _segmenter = _build_segmenter()
    return _segmenter


def segment(text: str) -> List[str]:
    if not text or not str(text).strip():
        return []
    seg = get_segmenter()
    return seg.cut(str(text).strip())


def segment_many(texts: Sequence[str]) -> List[List[str]]:
    if not texts:
        return []
    seg = get_segmenter()
    if hasattr(seg, "cut_many"):
        return seg.cut_many(texts)
    return [seg.cut(str(text or "").strip()) for text in texts]


def _load_stopwords() -> set:
    global _cached_stopwords
    if _cached_stopwords is not None:
        return _cached_stopwords

    words = set()
    if _STOPWORDS.exists():
        with open(_STOPWORDS, "r", encoding="utf-8") as handle:
            for line in handle:
                word = line.strip()
                if word:
                    words.add(word)
    _cached_stopwords = words
    return words


def _dedupe_keep_order(tokens: Sequence[str]) -> List[str]:
    out = []
    seen = set()
    for tok in tokens:
        clean = str(tok or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def extract_keywords(text: str, max_keywords: int = 20) -> List[str]:
    if not text or not str(text).strip():
        return []
    stopwords = _load_stopwords()
    keywords = []
    for word in segment(text):
        clean = str(word or "").strip()
        if not clean or clean in stopwords or len(clean) <= 1:
            continue
        keywords.append(clean)
    return _dedupe_keep_order(keywords)[: max(1, int(max_keywords))]


def segment_for_indexing(text: str, max_length: int = 5000) -> str:
    if not text or not str(text).strip():
        return ""

    truncated = str(text)[: max(1, int(max_length))]
    if not _looks_chinese(truncated):
        return truncated

    stopwords = _load_stopwords()
    words = []
    for token in segment(truncated):
        clean = str(token or "").strip()
        if not clean or clean in stopwords:
            continue
        words.append(clean)
    compact = " ".join(words).strip()
    return compact or truncated


def segment_for_indexing_many(texts: Sequence[str], max_length: int = 5000) -> List[str]:
    if not texts:
        return []

    stopwords = _load_stopwords()
    normalized = [str(text or "") for text in texts]
    truncated = [text[: max(1, int(max_length))] for text in normalized]
    chinese_indexes = [idx for idx, text in enumerate(truncated) if _looks_chinese(text)]
    output = list(truncated)

    if chinese_indexes:
        chinese_texts = [truncated[idx] for idx in chinese_indexes]
        tokenized = segment_many(chinese_texts)
        for idx, tokens in zip(chinese_indexes, tokenized):
            filtered = []
            for token in tokens:
                clean = str(token or "").strip()
                if not clean or clean in stopwords:
                    continue
                filtered.append(clean)
            output[idx] = " ".join(filtered).strip() or truncated[idx]

    return output
