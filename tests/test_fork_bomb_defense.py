"""測試 P2-0 defense-in-depth 防禦層（2026-04-19）

三道防禦：
1. translator/action.py fork-depth sentinel — 第 2 層 child 啟動時自殺
2. statutes-vdb/action.py 同款 sentinel
3. statutes-vdb 的 background_fill overlap guard（lock file 偵測已跑中的 subprocess）

+ daemon reaper 有 translator grace period（非 unit test，改看原始碼）
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


MAGI_ROOT = Path(__file__).resolve().parents[1]


class TestTranslatorForkSentinel:
    """translator/action.py 啟動時 depth >= 2 會 sys.exit(87)。"""

    def _run_translator(self, env_depth: int) -> tuple[int, str]:
        """以給定的 _MAGI_TRANSLATOR_FORK_DEPTH 執行 translator self_test task。"""
        env = os.environ.copy()
        env["_MAGI_TRANSLATOR_FORK_DEPTH"] = str(env_depth)
        cp = subprocess.run(
            [sys.executable, str(MAGI_ROOT / "skills/translator/action.py"), "--task", "help"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            cwd=str(MAGI_ROOT),
        )
        return cp.returncode, (cp.stderr or "")

    def test_depth_0_runs_normally(self):
        """Depth=0 是 parent，應正常執行。"""
        rc, stderr = self._run_translator(env_depth=0)
        # help task 應該 exit 0 或至少不是 sentinel 的 87
        assert rc != 87, f"Depth=0 不該被 sentinel 擋下，但 got rc=87"

    def test_depth_1_runs_normally(self):
        """Depth=1 是第一層 child，也該合法。"""
        rc, stderr = self._run_translator(env_depth=1)
        assert rc != 87

    def test_depth_2_aborts_with_87(self):
        """Depth=2（孫層）必須被 sentinel 擋下並 exit(87)。"""
        rc, stderr = self._run_translator(env_depth=2)
        assert rc == 87, f"Depth=2 應被 sentinel 擋（rc=87），實際 rc={rc}"
        assert "Fork-depth sentinel triggered" in stderr

    def test_depth_99_aborts(self):
        """極大 depth 也必定擋下。"""
        rc, _ = self._run_translator(env_depth=99)
        assert rc == 87


class TestStatutesVdbForkSentinel:
    """statutes-vdb/action.py 同款 sentinel。"""

    def _run(self, env_depth: int) -> int:
        env = os.environ.copy()
        env["_MAGI_STATUTES_VDB_FORK_DEPTH"] = str(env_depth)
        cp = subprocess.run(
            [sys.executable, str(MAGI_ROOT / "skills/statutes-vdb/action.py"), "--task", "help"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            cwd=str(MAGI_ROOT),
        )
        return cp.returncode

    def test_depth_2_aborts(self):
        rc = self._run(2)
        assert rc == 87, f"statutes-vdb sentinel 該擋 depth=2，實際 rc={rc}"


class TestReaperGracePeriods:
    """daemon.py 的 REAPER_GRACE_PERIODS 有 translator entry（防孤兒累積 30 分鐘才清）。"""

    def test_translator_has_grace_period(self):
        from daemon import REAPER_GRACE_PERIODS
        assert "skills/translator/action.py" in REAPER_GRACE_PERIODS
        # grace 應該 < 600s（比預設 stale timeout 短很多）
        assert REAPER_GRACE_PERIODS["skills/translator/action.py"] <= 600

    def test_translator_inner_has_shorter_grace(self):
        """_translate_inner subprocess 更短 grace（<180s）。"""
        from daemon import REAPER_GRACE_PERIODS
        inner_key = "skills/translator/action.py --task _translate_inner"
        assert inner_key in REAPER_GRACE_PERIODS
        assert REAPER_GRACE_PERIODS[inner_key] <= 180


class TestStatutesVdbOverlapGuard:
    """task_update_cases 的 background_fill spawn 有 PID-lock overlap guard。"""

    def test_overlap_guard_code_present(self):
        """原始碼要有 lock file 檢查邏輯。"""
        import inspect
        src_path = MAGI_ROOT / "skills/statutes-vdb/action.py"
        src = src_path.read_text(encoding="utf-8")
        assert "statutes_vdb_bg_fill.pid" in src, (
            "background_fill overlap guard 應用 statutes_vdb_bg_fill.pid lock file"
        )
        assert "os.kill(_existing_pid, 0)" in src, (
            "lock guard 要用 signal 0 檢查 PID 是否還活著"
        )


class TestTranslatorRecursionGuard:
    """P2-0 regression：_translate_inner 不得 import/呼叫 tri_sage_collab.translate_text"""

    def test_translate_inner_no_tri_sage_call(self):
        import inspect
        import re
        from skills.translator.action import _translate_inner
        src = inspect.getsource(_translate_inner)
        # 砍 comment / docstring
        code_only = re.sub(r"#[^\n]*", "", src)
        code_only = re.sub(r'"""[\s\S]*?"""', "", code_only)
        code_only = re.sub(r"'''[\s\S]*?'''", "", code_only)
        assert "from skills.bridge.tri_sage_collab" not in code_only
        assert not re.search(r"\btranslate_text\s*\(", code_only)
