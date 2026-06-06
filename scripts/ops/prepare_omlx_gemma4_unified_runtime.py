#!/usr/bin/env python3
"""Prepare a pinned oMLX source overlay for Gemma 4 unified models.

This does not replace the Homebrew oMLX installation.  It creates a
reproducible source overlay under ~/.omlx/runtime-src/gemma4-unified and a
wrapper that runs oMLX with source versions of omlx/mlx-lm/mlx-vlm known to
support Gemma 4 unified model configs.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_OMLX_REF = "bac678ec72c97e497d05c3c6d637fa54f1b3d7e3"
DEFAULT_MLX_LM_REF = "04a19108d4a7fd6606319784d07c5be3017b073a"
DEFAULT_MLX_VLM_REF = "d02eee1d51170e8d46e4266261445134c0535979"


@dataclass(frozen=True)
class SourceRepo:
    name: str
    url: str
    ref: str


def _run(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        capture_output=True,
    )


def _ensure_repo(dest: Path, repo: SourceRepo) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not (dest / ".git").exists():
        _run(["git", "clone", repo.url, str(dest)])
    _run(["git", "fetch", "--all", "--tags", "--prune"], cwd=dest)
    _run(["git", "checkout", "--detach", repo.ref], cwd=dest)


def _patch_omlx_model_discovery(omlx_dir: Path) -> bool:
    target = omlx_dir / "omlx" / "model_discovery.py"
    text = target.read_text(encoding="utf-8")
    original = text
    if '"gemma4_unified"' not in text:
        text = text.replace('    "gemma4",\n', '    "gemma4",\n    "gemma4_unified",\n')
    if '"Gemma4UnifiedForConditionalGeneration"' not in text:
        text = text.replace(
            '    "Gemma4ForConditionalGeneration",\n',
            '    "Gemma4ForConditionalGeneration",\n    "Gemma4UnifiedForConditionalGeneration",\n',
        )
    if text != original:
        target.write_text(text, encoding="utf-8")
        return True
    return False


def _write_wrapper(root: Path, python_bin: Path, wrapper: Path) -> None:
    src = root / "src"
    omlx = src / "omlx"
    mlx_lm = src / "mlx-lm"
    mlx_vlm = src / "mlx-vlm"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    content = f"""#!/usr/bin/env bash
set -euo pipefail
MAGI_OMLX_GEMMA4_PYTHON="${{MAGI_OMLX_GEMMA4_PYTHON:-{shlex.quote(str(python_bin))}}}"
export PYTHONPATH="{shlex.quote(str(omlx))}:{shlex.quote(str(mlx_lm))}:{shlex.quote(str(mlx_vlm))}:${{PYTHONPATH:-}}"
exec "$MAGI_OMLX_GEMMA4_PYTHON" -m omlx.cli "$@"
"""
    wrapper.write_text(content, encoding="utf-8")
    wrapper.chmod(0o755)


def _verify(root: Path, python_bin: Path, model_dir: Path | None = None) -> str:
    src = root / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = ":".join(
        [
            str(src / "omlx"),
            str(src / "mlx-lm"),
            str(src / "mlx-vlm"),
            env.get("PYTHONPATH", ""),
        ]
    )
    code = r"""
import importlib.util
import json
import sys
from pathlib import Path
import mlx.core as mx

checks = {
    "mlx_new_thread_local_stream": hasattr(mx, "new_thread_local_stream"),
    "mlx_lm_gemma4": bool(importlib.util.find_spec("mlx_lm.models.gemma4")),
    "mlx_vlm_gemma4_unified": bool(importlib.util.find_spec("mlx_vlm.models.gemma4_unified")),
}
model_path = Path(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] else None
if model_path:
    from omlx.model_discovery import detect_model_type
    checks["model_path_exists"] = model_path.exists()
    checks["detected_type"] = detect_model_type(model_path) if model_path.exists() else "missing"
print(json.dumps(checks, ensure_ascii=False, indent=2))
if not checks["mlx_new_thread_local_stream"]:
    raise SystemExit("MLX core is too old for current mlx-lm")
if not checks["mlx_lm_gemma4"] or not checks["mlx_vlm_gemma4_unified"]:
    raise SystemExit("Gemma 4 unified runtime modules are missing")
if model_path and checks.get("detected_type") != "vlm":
    raise SystemExit(f"Expected VLM detection for Gemma 4 unified, got {checks.get('detected_type')}")
"""
    cmd = [str(python_bin), "-c", code, str(model_dir or "")]
    proc = subprocess.run(cmd, env=env, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stdout + proc.stderr).strip())
    return proc.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare Gemma 4 unified oMLX source overlay.")
    parser.add_argument("--root", default=str(Path.home() / ".omlx" / "runtime-src" / "gemma4-unified"))
    parser.add_argument("--python", default=str(Path.cwd() / "venv" / "bin" / "python3"))
    parser.add_argument("--wrapper", default=str(Path.home() / ".omlx" / "bin" / "omlx-gemma4-unified-serve"))
    parser.add_argument("--model-dir", default=str(Path.home() / ".omlx" / "models" / "gemma-4-12B-it-4bit"))
    parser.add_argument("--omlx-ref", default=DEFAULT_OMLX_REF)
    parser.add_argument("--mlx-lm-ref", default=DEFAULT_MLX_LM_REF)
    parser.add_argument("--mlx-vlm-ref", default=DEFAULT_MLX_VLM_REF)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser()
    src = root / "src"
    python_bin = Path(args.python).expanduser()
    wrapper = Path(args.wrapper).expanduser()
    model_dir = Path(args.model_dir).expanduser() if args.model_dir else None

    if not python_bin.exists():
        raise SystemExit(f"Python not found: {python_bin}")

    if not args.verify_only:
        repos = [
            SourceRepo("omlx", "https://github.com/jundot/omlx.git", args.omlx_ref),
            SourceRepo("mlx-lm", "https://github.com/ml-explore/mlx-lm.git", args.mlx_lm_ref),
            SourceRepo("mlx-vlm", "https://github.com/Blaizzy/mlx-vlm.git", args.mlx_vlm_ref),
        ]
        for repo in repos:
            _ensure_repo(src / repo.name, repo)
        patched = _patch_omlx_model_discovery(src / "omlx")
        _write_wrapper(root, python_bin, wrapper)
        print(f"overlay_root={root}")
        print(f"wrapper={wrapper}")
        print(f"omlx_model_discovery_patched={patched}")

    print(_verify(root, python_bin, model_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
