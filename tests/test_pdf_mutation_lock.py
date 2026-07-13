from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_pdf_mutation_lock_reports_active_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_BACKGROUND_LOCK_DIR", str(tmp_path))
    from scripts.ops.pdf_mutation_lock import (
        PdfMutationLockBusy,
        pdf_in_place_mutation_lock,
        pdf_in_place_mutation_lock_path,
    )

    out = tmp_path / "child.json"
    repo = Path(__file__).resolve().parents[1]
    env = {**os.environ.copy(), "PYTHONPATH": str(repo)}

    with pdf_in_place_mutation_lock(owner="first", pdf_path=tmp_path / "a.pdf", blocking=False) as first:
        code = (
            "import json, pathlib; "
            "from scripts.ops.pdf_mutation_lock import PdfMutationLockBusy, pdf_in_place_mutation_lock; "
            "payload={}; "
            "\ntry:\n"
            "    with pdf_in_place_mutation_lock(owner='second', pdf_path='b.pdf', blocking=False):\n"
            "        payload={'acquired': True}\n"
            "except PdfMutationLockBusy as exc:\n"
            "    payload=exc.lock.as_dict()\n"
            f"pathlib.Path({str(out)!r}).write_text(json.dumps(payload), encoding='utf-8')\n"
        )
        subprocess.run([sys.executable, "-c", code], cwd=str(repo), env=env, check=True)
        second = json.loads(out.read_text(encoding="utf-8"))

        assert first.acquired is True
        assert pdf_in_place_mutation_lock_path() == tmp_path / "pdf_in_place_mutation.lock"
        assert second["acquired"] is False
        assert second["active_owner"]["owner"] == "first:a.pdf"
        assert second["active_owner"]["pid"] == os.getpid()

    try:
        raise PdfMutationLockBusy(first)
    except PdfMutationLockBusy as exc:
        assert exc.lock is first
