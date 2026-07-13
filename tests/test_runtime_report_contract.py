from __future__ import annotations

import json
from pathlib import Path

from scripts.ops import run_test_suite


def _assert_runtime_report_contract(payload: dict) -> None:
    assert isinstance(payload.get("ok"), bool)
    assert isinstance(payload.get("total"), int)
    assert isinstance(payload.get("passed"), int)
    assert isinstance(payload.get("failed"), int)
    assert isinstance(payload.get("results"), list)
    assert payload.get("generated_at") or payload.get("timestamp")
    assert payload["total"] == payload["passed"] + payload["failed"] + int(payload.get("skipped", 0))

    for result in payload["results"]:
        assert isinstance(result, dict)
        assert isinstance(result.get("id"), str) and result["id"]
        assert isinstance(result.get("name"), str) and result["name"]
        assert isinstance(result.get("ok"), bool)
        assert isinstance(result.get("command"), list)


def test_run_test_suite_json_out_keeps_machine_readable_runtime_contract(tmp_path: Path):
    matrix = {
        "suites": {
            "contract": {
                "checks": [
                    {
                        "id": "noop",
                        "name": "No-op dry run",
                        "command": ["{python}", "-c", "print('ok')"],
                    }
                ]
            }
        }
    }
    matrix_path = tmp_path / "matrix.json"
    report_path = tmp_path / ".runtime" / "contract_latest.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

    rc = run_test_suite.main([
        "--matrix",
        str(matrix_path),
        "--suite",
        "contract",
        "--dry-run",
        "--json-out",
        str(report_path),
    ])

    assert rc == 0
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    _assert_runtime_report_contract(payload)
    assert payload["ok"] is True
    assert payload["total"] == 1
    assert payload["passed"] == 0
    assert payload["failed"] == 0
    assert payload["results"][0]["skipped"] is True


def test_runtime_report_contract_accepts_timestamp_alias_for_generated_at():
    payload = {
        "ok": True,
        "timestamp": "2026-06-23T00:00:00",
        "total": 1,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
        "results": [
            {
                "id": "sample",
                "name": "Sample",
                "ok": True,
                "command": ["python", "-c", "pass"],
            }
        ],
    }

    _assert_runtime_report_contract(payload)
