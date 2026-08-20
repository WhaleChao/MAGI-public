"""Minimal pytest plugin that records collection and phase outcomes as JSON."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


_COLLECTED: list[str] = []
_REPORTS: list[dict[str, Any]] = []


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pytest_collection_finish(session: Any) -> None:
    global _COLLECTED
    _COLLECTED = [str(item.nodeid) for item in session.items]


def pytest_runtest_logreport(report: Any) -> None:
    _REPORTS.append(
        {
            "nodeid": str(report.nodeid),
            "when": str(report.when),
            "outcome": str(report.outcome),
            "wasxfail": bool(getattr(report, "wasxfail", False)),
            "longrepr_sha256": _digest(str(report.longrepr or "")),
        }
    )


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    target_text = os.environ.get("MAGI_V3_PYTEST_TRANSCRIPT", "")
    if not target_text:
        return
    target = Path(target_text)
    payload = {
        "schema_version": 1,
        "pytest_exitstatus": int(exitstatus),
        "python_runtime_sha256": _file_digest(Path(sys.executable)),
        "python_runtime_realpath_sha256": _file_digest(
            Path(sys.executable).resolve(strict=True)
        ),
        "collected_nodeids": _COLLECTED,
        "phase_reports": _REPORTS,
    }
    data = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    temporary = target.with_suffix(target.suffix + ".tmp")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_bytes(data)
    os.replace(temporary, target)
