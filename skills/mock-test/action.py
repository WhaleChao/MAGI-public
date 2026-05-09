#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
skills/mock-test/action.py
==========================
模擬站技能全套測試 (Mock Skill Test)

對 eefile_mock (port 17001) + laf_mock (port 17002) 執行完整功能測試：
  - file-review-orchestrator (閱卷)
  - laf-orchestrator (法扶)
  - laf-portal-automation

TG/DC 呼叫方式：
  @MAGI 模擬測試
  @MAGI 模擬測試 閱卷
  @MAGI 模擬測試 法扶
  @MAGI mock test
  @MAGI mock test all

CLI：
  python action.py --task 'all'
  python action.py --task 'file_review'
  python action.py --task 'laf'
  python action.py --task 'notify'   # 結果發 TG
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from api.runtime_paths import get_skill_python

# ---------------------------------------------------------------------------
# Re-exec in venv if needed
# ---------------------------------------------------------------------------
_VENV_PY = str(get_skill_python())
if os.path.exists(_VENV_PY) and os.path.realpath(sys.executable) != os.path.realpath(_VENV_PY):
    os.execv(_VENV_PY, [_VENV_PY, __file__] + sys.argv[1:])

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
MOCK_TEST_SCRIPT = Path(__file__).resolve().parent.parent.parent / \
    "casper_ecosystem" / "law_firm_orchestrators" / "mock_skill_test.py"


def run_mock_test(skills: str = "all", notify: bool = False) -> dict:
    """
    Delegate to mock_skill_test.py and return structured result.

    skills: "all" | "file_review" | "laf" | "portal"
    """
    if not MOCK_TEST_SCRIPT.exists():
        return {
            "success": False,
            "error": f"mock_skill_test.py not found at {MOCK_TEST_SCRIPT}",
        }

    cmd = [sys.executable, str(MOCK_TEST_SCRIPT), "--skills", skills, "--no-stop"]
    if notify:
        cmd.append("--notify")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )
        stdout = result.stdout
        stderr = result.stderr

        # Parse summary line: "結果: X PASS / Y FAIL / Z SKIP / N 共"
        passed = failed = skipped = total = 0
        for line in stdout.splitlines():
            if "PASS" in line and "FAIL" in line and "共" in line:
                import re
                m = re.search(r"(\d+) PASS.*?(\d+) FAIL.*?(\d+) SKIP.*?(\d+) 共", line)
                if m:
                    passed, failed, skipped, total = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))

        # Collect failure lines
        failures = []
        in_fail = False
        for line in stdout.splitlines():
            if "失敗項目" in line:
                in_fail = True
            elif in_fail and line.strip().startswith("-"):
                failures.append(line.strip()[2:].strip())
            elif in_fail and line.strip() == "=" * 20:
                break

        icon = "✅" if failed == 0 else ("⚠️" if failed <= 3 else "❌")
        summary = f"{icon} Mock 技能測試 {datetime.now().strftime('%H:%M')} — {passed}✓ {failed}✗ {skipped}⏭ / 共{total}"

        return {
            "success": failed == 0,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "total": total,
            "failures": failures,
            "summary": summary,
            "stdout_tail": stdout[-800:],
        }

    except subprocess.TimeoutExpired:
        return {"success": False, "error": "timeout (>600s)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _notify(msg: str):
    """Send result to Telegram via red_phone."""
    try:
        magi_dir = Path(__file__).resolve().parent.parent.parent
        if str(magi_dir) not in sys.path:
            sys.path.insert(0, str(magi_dir))
        from skills.ops.red_phone import send_telegram_push_with_status  # type: ignore
        send_telegram_push_with_status(
            msg,
            severity="info",
            source="mock_test",
            topic_key="check",
            queue_on_fail=True,
        )
    except Exception as e:
        print(f"[notify] 發送失敗: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description="mock-test skill")
    ap.add_argument("--task", default="help")
    args = ap.parse_args()

    task = (args.task or "").strip().lower()

    if task in ("help", "list", ""):
        result = {
            "success": True,
            "description": "模擬站全套技能測試",
            "commands": [
                "all         — 測試所有技能 (閱卷 + 法扶)",
                "file_review — 只測試 file-review-orchestrator",
                "laf         — 只測試 laf-orchestrator",
                "notify      — 測試所有並將結果發送到 TG",
            ],
            "tg_triggers": [
                "@MAGI 模擬測試",
                "@MAGI 模擬測試 閱卷",
                "@MAGI 模擬測試 法扶",
                "@MAGI mock test",
            ],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    # Map task aliases
    notify = False
    if task in ("notify", "all notify", "notify all"):
        task = "all"
        notify = True

    skill_map = {
        "all": "all",
        "閱卷": "file_review",
        "file_review": "file_review",
        "file-review": "file_review",
        "法扶": "laf",
        "laf": "laf",
        "portal": "portal",
    }
    skills = skill_map.get(task, "all")

    print(f"[mock-test] 開始測試 skills={skills} notify={notify}")
    r = run_mock_test(skills=skills, notify=notify)

    print(json.dumps(r, ensure_ascii=False, indent=2))

    if notify and not r.get("success"):
        # Also notify inline
        msg = r.get("summary", "❌ 模擬測試失敗")
        if r.get("failures"):
            msg += "\n失敗：\n" + "\n".join(f"  ✗ {f}" for f in r["failures"][:5])
        _notify(msg)

    return 0 if r.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
