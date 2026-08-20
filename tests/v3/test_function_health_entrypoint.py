from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ops" / "function_health_index.py"


def test_function_health_entrypoint_imports_release_from_foreign_cwd(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    code = """
from datetime import datetime, timezone
from pathlib import Path
import runpy
namespace = runpy.run_path(ARGV_SCRIPT, run_name="function_health_entrypoint_test")
health, expected = namespace["discover_cron_jobs"](
    Path(ARGV_ROOT),
    Path(ARGV_RUNTIME),
    datetime.now(timezone.utc),
)
assert isinstance(health, dict)
assert isinstance(expected, list)
"""
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "ARGV_SCRIPT,ARGV_ROOT,ARGV_RUNTIME=__import__('sys').argv[1:];" + code,
            str(SCRIPT),
            str(ROOT),
            str(runtime),
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
