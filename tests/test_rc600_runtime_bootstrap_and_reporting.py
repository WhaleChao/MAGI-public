from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from skills.ops import self_repair_reporter as reporter


ROOT = Path(__file__).resolve().parents[1]


def test_file_review_action_bootstraps_release_root_without_pythonpath():
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import runpy; "
                f"runpy.run_path({str(ROOT / 'skills/file-review-orchestrator/action.py')!r}, "
                "run_name='rc600_import_probe')"
            ),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_self_repair_reporter_maps_legacy_job_and_hides_trace_codes(monkeypatch):
    assert reporter._job_label("weekend_resummary") == "job_weekend_resummary"
    monkeypatch.setattr(
        reporter,
        "_load_cron_job_map",
        lambda: {
            "job_weekend_resummary": {
                "desc": "判決高品質重摘（每日受控分批）"
            }
        },
    )
    report = reporter._build_report(
        {
            "one": {
                "job": "job_weekend_resummary",
                "error_label": "GeneralError",
                "trace": "internal-trace-code",
                "count": 1,
                "status": "active",
            }
        }
    )

    assert "判決高品質重摘" in report
    assert "一般錯誤" in report
    assert "追蹤碼" not in report
    assert "internal-trace-code" not in report
