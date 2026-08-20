from pathlib import Path

from scripts.ops.prepare_omlx_gemma4_unified_runtime import _verify


def test_default_preflight_does_not_import_mlx_core(tmp_path: Path) -> None:
    root = tmp_path / "overlay"
    for relative in (
        "src/omlx/omlx/model_discovery.py",
        "src/mlx-lm/mlx_lm/models/gemma4.py",
        "src/mlx-vlm/mlx_vlm/models/gemma4_unified.py",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("raise AssertionError('must not import')\n", encoding="utf-8")
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    python.chmod(0o755)
    model = tmp_path / "model"
    model.mkdir()

    report = _verify(root, python, model)

    assert '"metal_probe": "not_run_requires_explicit_live_context"' in report
    assert '"mlx_vlm_gemma4_unified": true' in report
