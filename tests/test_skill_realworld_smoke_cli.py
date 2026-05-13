from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


MAGI_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = MAGI_ROOT / "scripts" / "ops" / "skill_realworld_smoke.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("skill_realworld_smoke_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_help_exits_without_running_smoke(monkeypatch):
    mod = _load_module()

    def _should_not_run():
        raise AssertionError("run_matrix should not be called when --help is requested")

    monkeypatch.setattr(mod, "run_matrix", _should_not_run)

    with pytest.raises(SystemExit) as exc:
        mod.main(["--help"])

    assert exc.value.code == 0
