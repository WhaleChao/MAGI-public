from __future__ import annotations

from pathlib import Path

import pytest

from skills.engine.ocr.nemotron_mlx.image_processor import self_test
from skills.engine.ocr.nemotron_mlx.runtime import NemotronRuntime
from skills.engine.ocr.nemotron_mlx.weight_map import map_tensor_name, transpose_conv_weight
from scripts.serve_nemotron_parse_omlx import create_app
from skills.engine.ocr import nemotron_parse_provider


HF_MODEL_DIR = Path.home() / ".omlx/models-vision/nemotron-parse-v1.2-hf"
MLX_BF16_DIR = Path.home() / ".omlx/models-vision/nemotron-parse-v1.2-mlx/bf16"


def test_nemotron_mlx_image_processor_matches_golden():
    if not (HF_MODEL_DIR / "golden_outputs.json").is_file():
        pytest.skip("Nemotron Parse golden_outputs.json not installed locally")
    result = self_test(HF_MODEL_DIR)
    assert result["ok"], result["errors"]


def test_nemotron_weight_map_covers_key_tensor_groups():
    assert map_tensor_name("encoder.model_encoder.radio_model.model.patch_generator.embedder.weight") == (
        "radio.patch_embed.embedder.weight"
    )
    assert map_tensor_name("encoder.model_encoder.radio_model.model.patch_generator.cls_token.token") == (
        "radio.cls_token"
    )
    assert map_tensor_name("encoder.model_encoder.radio_model.input_conditioner.norm_mean") == (
        "radio.input_conditioner.norm_mean"
    )
    assert map_tensor_name("encoder.conv1.weight") == "neck.conv1.weight"
    assert map_tensor_name("decoder.layers.0.encoder_attn.q_proj.weight") == (
        "decoder.layers.0.cross_attn.q_proj.weight"
    )
    assert map_tensor_name("decoder.layers.0.self_attn_layer_norm.weight") == (
        "decoder.layers.0.ln_self.weight"
    )
    assert map_tensor_name("lm_head.weight") == "lm_head.weight"


def test_nemotron_mlx_bf16_conversion_manifest_when_present():
    manifest = MLX_BF16_DIR / "conversion_manifest.json"
    weights = MLX_BF16_DIR / "model.safetensors"
    if not manifest.is_file() or not weights.is_file():
        pytest.skip("Nemotron Parse MLX bf16 conversion not present locally")

    import json

    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["tensor_count"] == 667
    assert data["skipped_tensors"] == []
    assert data["target_dtype"] == "bf16"
    assert data["output_safetensors_bytes"] == weights.stat().st_size
    assert "encoder.conv1.weight" in data["transposed_conv_tensors"]
    assert "encoder.conv2.weight" in data["transposed_conv_tensors"]


def test_nemotron_runtime_post_process_generation_matches_hf_cleaning():
    decoded = "</s><s><predict_bbox>hello</s>"
    assert NemotronRuntime.post_process_generation(decoded) == "<predict_bbox>hello"


def test_nemotron_parse_sidecar_health_reports_weight_file():
    app = create_app(MLX_BF16_DIR, HF_MODEL_DIR)
    client = app.test_client()
    data = client.get("/health").get_json()
    assert "ok" in data
    assert data["loaded"] is False
    assert data["weights"].endswith("model.safetensors")


def test_nemotron_parse_sidecar_parse_requires_image():
    app = create_app(MLX_BF16_DIR, HF_MODEL_DIR)
    client = app.test_client()
    resp = client.post("/parse", json={})
    assert resp.status_code == 500
    data = resp.get_json()
    assert data["ok"] is False
    assert "image_path or image_base64 is required" in data["error"]


def test_nemotron_parse_provider_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MAGI_NEMOTRON_PARSE_ENABLE", raising=False)
    result = nemotron_parse_provider.run("/tmp/missing.png")
    assert result.success is False
    assert result.provider == "nemotron_parse_mlx"
    assert "not enabled" in (result.error or "")


def test_ocr_consensus_uses_nemotron_provider_when_enabled(monkeypatch, tmp_path):
    from skills.engine.ocr import apple_vision_provider, consensus, tesseract_provider
    from skills.engine.ocr.ocr_schema import OCRProviderResult

    image = tmp_path / "page.png"
    image.write_bytes(b"fake")
    monkeypatch.setenv("MAGI_NEMOTRON_PARSE_ENABLE", "1")
    monkeypatch.setattr(
        tesseract_provider,
        "run",
        lambda *a, **k: OCRProviderResult.failure("tesseract", "disabled in test"),
    )
    monkeypatch.setattr(
        apple_vision_provider,
        "run",
        lambda *a, **k: OCRProviderResult.failure("apple_vision", "disabled in test"),
    )
    monkeypatch.setattr(
        nemotron_parse_provider,
        "run",
        lambda *a, **k: OCRProviderResult(
            success=True,
            provider="nemotron_parse_mlx",
            raw_text="raw",
            corrected_text="臺灣臺北地方法院 114年度訴字第123號",
            quality_score=0.8,
        ),
    )

    result = consensus.run_consensus(str(image), task_type="legal", timeout_sec=5)

    assert result.success is True
    assert "臺灣臺北地方法院" in result.corrected_text
    assert "nemotron_parse_mlx" in result.provider_results
    assert result.provider_results["nemotron_parse_mlx"].success is True
