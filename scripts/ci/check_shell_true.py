#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CI gate：阻止新增 shell=True 與 os.system(f"...")；
老的在 shell_true_grandfather.txt 列冊放行。"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import List, Tuple

REPO = Path(__file__).resolve().parents[2]
GRANDFATHER = REPO / "scripts" / "ci" / "shell_true_grandfather.txt"

_SCAN_ROOTS = ["api", "skills", "scripts", "casper_ecosystem", "daemon.py"]
_EXCLUDE_SUBSTR = (".runtime/", "venv/", "__pycache__/", ".git/",
                   "scripts/ci/check_shell_true.py",
                   "api/platforms/safe_process.py")  # 本身就是取代 shell=True 的模組，docstring/comment 會誤觸

_PATTERNS = (
    re.compile(r"\bshell\s*=\s*True\b"),
    re.compile(r"\bos\.system\s*\(\s*f['\"]"),
    re.compile(r"\bos\.popen\s*\("),
)


def _load_grandfather() -> set:
    if not GRANDFATHER.exists():
        return set()
    allowed = set()
    for ln in GRANDFATHER.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        allowed.add(ln)
    return allowed


def _iter_py_files() -> List[Path]:
    out = []
    for root in _SCAN_ROOTS:
        p = REPO / root
        if p.is_file() and p.suffix == ".py":
            out.append(p)
            continue
        if not p.exists():
            continue
        for f in p.rglob("*.py"):
            s = str(f)
            if any(x in s for x in _EXCLUDE_SUBSTR):
                continue
            out.append(f)
    return out


def scan() -> List[Tuple[str, int, str]]:
    hits = []
    for f in _iter_py_files():
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for pat in _PATTERNS:
                if pat.search(line):
                    rel = str(f.relative_to(REPO))
                    hits.append((rel, i, line.rstrip()))
                    break
    return hits


def main() -> int:
    allowed = _load_grandfather()
    hits = scan()
    violations = []
    for rel, lineno, line in hits:
        key = f"{rel}:{lineno}"
        if key in allowed or rel in allowed:
            continue
        violations.append((rel, lineno, line))
    if violations:
        print("❌ shell=True / os.system(f...) / os.popen() 新增違規：")
        for rel, lineno, line in violations:
            print(f"  {rel}:{lineno}  {line[:120]}")
        print(f"\n合計 {len(violations)} 筆。若屬 legacy 必要，請加入 shell_true_grandfather.txt 並註明原因。")
        return 1
    print(f"✅ check_shell_true PASS（grandfather {len(allowed)} 筆）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
