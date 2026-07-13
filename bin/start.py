from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from bin._runtime import resolve_python, resolve_release_root, root_error_message, runtime_env


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = resolve_release_root()
    if root is None:
        print(f"[ERROR] {root_error_message()}", file=sys.stderr)
        return 1

    env = runtime_env(root)

    if os.name == "nt":
        launcher = root / "start_magi.bat"
        if not launcher.exists():
            print(f"[ERROR] Missing launcher: {launcher}", file=sys.stderr)
            return 1
        command = ["cmd", "/c", str(launcher), *args]
        return subprocess.call(command, cwd=root, env=env)

    launcher = root / "bin" / "start"
    if launcher.exists():
        return subprocess.call(["bash", str(launcher), *args], cwd=root, env=env)

    daemon_script = root / "daemon.py"
    if not daemon_script.exists():
        print(f"[ERROR] Missing daemon script: {daemon_script}", file=sys.stderr)
        return 1

    python = resolve_python(root)
    return subprocess.call([str(python), str(daemon_script), *args], cwd=root, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
