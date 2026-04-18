# -*- coding: utf-8 -*-
"""Compatibility shim for pkuseg in MAGI's Python 3.14 runtime.

This shim intentionally avoids importing ``skills.engine.chinese_nlp`` because
that module first tries a native ``import pkuseg`` path, which would recurse
back into this shim and hang. Instead we proxy tokenization straight to the
existing Python 3.11 sidecar, with a tiny regex fallback for resilience.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SIDECAR_SCRIPT = _ROOT / "skills" / "engine" / "chinese_nlp_sidecar.py"
_DEFAULT_SIDECAR_PYTHON = _ROOT / ".runtime" / "pkuseg_py311" / "bin" / "python"
_SIDECAR_TIMEOUT_SEC = int(os.environ.get("MAGI_PKUSEG_SIDECAR_TIMEOUT_SEC", "20") or "20")
_SIDECAR_MAX_CONCURRENT = int(os.environ.get("MAGI_PKUSEG_SIDECAR_MAX_CONCURRENT", "4") or "4")
_sidecar_sem = threading.BoundedSemaphore(_SIDECAR_MAX_CONCURRENT)


def _fallback_cut(text: str) -> list[str]:
    return [w for w in re.split(r"[\s，。、；：！？（）《》「」【】『』〔〕\n\r\t]+", text) if w.strip()]


class _ProxyPKUSeg:
    def __init__(self, *args, **kwargs):
        self._python = str(
            (os.environ.get("MAGI_PKUSEG_SIDECAR_PYTHON", "") or "").strip() or _DEFAULT_SIDECAR_PYTHON
        )

    def cut(self, text: str):
        clean = str(text or "").strip()
        if not clean:
            return []
        if not os.path.exists(self._python) or not _SIDECAR_SCRIPT.exists():
            return _fallback_cut(clean)
        try:
            with _sidecar_sem:
                proc = subprocess.run(
                    [self._python, str(_SIDECAR_SCRIPT)],
                    input=json.dumps([clean], ensure_ascii=False),
                    text=True,
                    capture_output=True,
                    timeout=max(5, _SIDECAR_TIMEOUT_SEC),
                    check=False,
                )
            if proc.returncode != 0:
                return _fallback_cut(clean)
            payload = json.loads(proc.stdout or "[]")
            if isinstance(payload, list) and payload and isinstance(payload[0], list):
                return [str(tok).strip() for tok in payload[0] if str(tok or "").strip()]
        except Exception:
            return _fallback_cut(clean)
        return _fallback_cut(clean)


def pkuseg(*args, **kwargs):
    return _ProxyPKUSeg(*args, **kwargs)
