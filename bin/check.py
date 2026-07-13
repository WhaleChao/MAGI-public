from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from bin._runtime import resolve_python, resolve_release_root, root_error_message, runtime_env


def _check_service(port: int) -> str:
    try:
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
            status = getattr(response, "status", 200)
            if 200 <= status < 400:
                return "pass"
    except URLError:
        return "warn"
    except Exception:
        return "warn"
    return "warn"


def _run_python(root: Path, code: str) -> subprocess.CompletedProcess[str]:
    python = resolve_python(root)
    return subprocess.run(
        [str(python), "-c", code],
        cwd=root,
        env=runtime_env(root),
        capture_output=True,
        text=True,
    )


def _python_fallback(root: Path) -> int:
    print("======================================")
    print("MAGI Health Check")
    print("======================================")
    print("")
    print(f"MAGI_ROOT: {root}")
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("")

    errors = 0
    warnings = 0

    def check_pass(message: str) -> None:
        print(f"  OK {message}")

    def check_warn(message: str) -> None:
        nonlocal warnings
        warnings += 1
        print(f"  WARN {message}")

    def check_fail(message: str) -> None:
        nonlocal errors
        errors += 1
        print(f"  FAIL {message}")

    python = resolve_python(root)
    has_local_venv = any(
        candidate.exists()
        for candidate in (
            root / "venv" / "bin" / "python3",
            root / "venv" / "bin" / "python",
            root / ".venv" / "bin" / "python3",
            root / ".venv" / "bin" / "python",
            root / "venv" / "Scripts" / "python.exe",
            root / ".venv" / "Scripts" / "python.exe",
        )
    )
    print("--- Python ---")
    if has_local_venv and python.exists():
        check_pass(f"python: {python}")
    elif python.exists():
        check_warn(f"No venv found (using system python: {python})")
    else:
        check_fail("No Python runtime found")

    print("")
    print("--- Config ---")
    if (root / ".env").exists():
        check_pass(".env exists")
    elif (root / ".env.example").exists():
        check_warn(".env missing (copy from .env.example during setup)")
    else:
        check_fail(".env missing")
    if (root / ".env.example").exists():
        check_pass(".env.example exists")
    else:
        check_warn(".env.example missing")

    print("")
    print("--- Config Validation ---")
    validate_code = """
import os, sys
root = os.environ['MAGI_ROOT']
sys.path.insert(0, root)
from dotenv import load_dotenv
load_dotenv(os.path.join(root, '.env'))
from skills.ops.config import validate_config
warnings = validate_config()
for item in warnings:
    print(f'WARN:{item}')
print('OK')
"""
    validate_result = _run_python(root, validate_code)
    if validate_result.returncode == 0:
        lines = [line.strip() for line in validate_result.stdout.splitlines() if line.strip()]
        if any(line == "OK" for line in lines):
            check_pass("Core config valid")
        for line in lines:
            if line.startswith("WARN:"):
                check_warn(line[5:])
    else:
        detail = (validate_result.stderr or validate_result.stdout or "validation failed").strip().splitlines()[-1]
        check_fail(detail)

    print("")
    print("--- Database ---")
    db_code = """
import os, sys
root = os.environ['MAGI_ROOT']
sys.path.insert(0, root)
from dotenv import load_dotenv
load_dotenv(os.path.join(root, '.env'))
import mysql.connector
conn = mysql.connector.connect(
    host=os.environ.get('DB_HOST', '127.0.0.1'),
    port=int(os.environ.get('DB_PORT', '3306')),
    user=os.environ.get('DB_USER', 'magi'),
    password=os.environ.get('DB_PASSWORD', ''),
    database=os.environ.get('DB_NAME', 'magi_brain'),
    connection_timeout=5,
    use_pure=True,
)
cursor = conn.cursor()
cursor.execute('SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE()')
print(cursor.fetchone()[0])
conn.close()
"""
    db_result = _run_python(root, db_code)
    if db_result.returncode == 0:
        check_pass(f"Database connected ({db_result.stdout.strip()} tables)")
    else:
        detail = (db_result.stderr or db_result.stdout or "database probe failed").strip().splitlines()[-1]
        check_warn(detail)

    print("")
    print("--- Core Modules ---")
    import_code = """
import os, sys
root = os.environ['MAGI_ROOT']
sys.path.insert(0, root)
for name in ('api.runtime_paths', 'api.product_runtime', 'skills.ops.config'):
    __import__(name)
    print(name)
"""
    import_result = _run_python(root, import_code)
    loaded = {line.strip() for line in import_result.stdout.splitlines() if line.strip()}
    for module in ("api.runtime_paths", "api.product_runtime", "skills.ops.config"):
        if module in loaded:
            check_pass(f"import {module}")
        else:
            check_fail(f"import {module} failed")

    print("")
    print("--- Services ---")
    for port, name in ((5002, "MAGI Server"), (5003, "Tools API")):
        state = _check_service(port)
        if state == "pass":
            check_pass(f"{name} (port {port}) responding")
        else:
            check_warn(f"{name} (port {port}) not running")

    print("")
    print("--- Release Hygiene ---")
    hardcoded_count = 0
    workspace_marker = "/".join(("", "Users", "ai", "Desktop", "MAGI"))
    skip_dirs = {"backups", "archive", "__pycache__", ".git", "venv", ".venv", "build", "dist"}
    for path in root.rglob("*.py"):
        if any(part in skip_dirs for part in path.parts):
            continue
        try:
            if workspace_marker in path.read_text(encoding="utf-8", errors="ignore"):
                hardcoded_count += 1
        except Exception:
            continue
    if hardcoded_count == 0:
        check_pass("No hardcoded paths")
    elif hardcoded_count < 10:
        check_warn(f"{hardcoded_count} hardcoded paths remaining")
    else:
        check_fail(f"{hardcoded_count} hardcoded paths remaining")

    print("")
    print("===================================")
    if errors == 0 and warnings == 0:
        print("  OK All checks passed")
    elif errors == 0:
        print(f"  WARN {warnings} warning(s), 0 errors")
    else:
        print(f"  FAIL {errors} error(s), {warnings} warning(s)")
    print("===================================")
    return errors


def main(argv: list[str] | None = None) -> int:
    del argv
    root = resolve_release_root()
    if root is None:
        print(f"[ERROR] {root_error_message()}", file=sys.stderr)
        return 1

    env = runtime_env(root)

    if os.name != "nt":
        launcher = root / "bin" / "check"
        if launcher.exists():
            return subprocess.call(["bash", str(launcher)], cwd=root, env=env)

    return _python_fallback(root)


if __name__ == "__main__":
    raise SystemExit(main())
