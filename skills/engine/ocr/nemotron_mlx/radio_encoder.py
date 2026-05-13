from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

import mlx.core as mx
import numpy as np

from .config import DEFAULT_HF_MODEL_DIR, DEFAULT_MLX_MODEL_DIR
from .image_processor import make_test_image, preprocess_image


PATCH_SIZE = 16
RADIO_LAYERS = 32
RADIO_HIDDEN = 1280
RADIO_HEADS = 16
RADIO_HEAD_DIM = 80
NECK_HIDDEN = 1024


def _linear(x: mx.array, weight: mx.array, bias: mx.array | None = None) -> mx.array:
    y = x @ mx.transpose(weight)
    if bias is not None:
        y = y + bias
    return y


def _layer_norm(x: mx.array, weight: mx.array, bias: mx.array, eps: float = 1e-6) -> mx.array:
    mean = mx.mean(x, axis=-1, keepdims=True)
    var = mx.mean(mx.square(x - mean), axis=-1, keepdims=True)
    return ((x - mean) * mx.rsqrt(var + eps)) * weight + bias


def _gelu(x: mx.array) -> mx.array:
    return 0.5 * x * (1.0 + mx.erf(x / math.sqrt(2.0)))


def _free_memory_mb() -> float | None:
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.check_output(["vm_stat"], text=True)
    except Exception:
        return None
    page_size = 16384
    free_pages = 0
    for line in out.splitlines():
        if "page size of" in line:
            try:
                page_size = int(line.split("page size of", 1)[1].split("bytes", 1)[0].strip())
            except Exception:
                pass
        if line.startswith("Pages free:") or line.startswith("Pages inactive:"):
            digits = "".join(ch for ch in line.split(":", 1)[1] if ch.isdigit())
            if digits:
                free_pages += int(digits)
    return free_pages * page_size / 1024 / 1024


def _patches_from_nchw(x: mx.array) -> mx.array:
    # x: [B, 3, H, W] -> [B, H/16*W/16, 3*16*16]
    b, c, h, w = x.shape
    py = h // PATCH_SIZE
    px = w // PATCH_SIZE
    x = mx.reshape(x, (b, c, py, PATCH_SIZE, px, PATCH_SIZE))
    x = mx.transpose(x, (0, 2, 4, 1, 3, 5))
    return mx.reshape(x, (b, py * px, c * PATCH_SIZE * PATCH_SIZE))


def _pos_embed_for_input(pos_embed: mx.array, input_hw: tuple[int, int]) -> mx.array:
    # Snapshot pos_embed is [1, 128*128, 1280]. For the golden input
    # 2048x1664 => 128x104 patches. Eval path crops top-left after optional
    # bilinear resize to max(input_dims), which is already 128 here.
    h = input_hw[0] // PATCH_SIZE
    w = input_hw[1] // PATCH_SIZE
    pe = mx.reshape(pos_embed, (1, 128, 128, RADIO_HIDDEN))
    pe = pe[:, :h, :w, :]
    return mx.reshape(pe, (1, h * w, RADIO_HIDDEN))


class RadioEncoder:
    def __init__(self, weights: dict[str, mx.array]):
        self.w = weights

    @classmethod
    def load(cls, weights_dir: str | Path | None = None) -> "RadioEncoder":
        if weights_dir is None:
            weights_dir = DEFAULT_MLX_MODEL_DIR / "bf16"
        weights_path = Path(weights_dir).expanduser() / "model.safetensors"
        return cls(mx.load(str(weights_path)))

    def _radio_backbone(self, pixel_values: mx.array) -> tuple[mx.array, mx.array]:
        # pixel_values: [B,3,H,W] in [0,1]
        w = self.w
        x = (pixel_values - w["radio.input_conditioner.norm_mean"]) / w["radio.input_conditioner.norm_std"]

        patches = _patches_from_nchw(x)
        x = _linear(patches, w["radio.patch_embed.embedder.weight"])
        x = x + _pos_embed_for_input(w["radio.pos_embed"], (pixel_values.shape[-2], pixel_values.shape[-1]))
        cls = mx.broadcast_to(w["radio.cls_token"][None, :, :], (x.shape[0], w["radio.cls_token"].shape[0], RADIO_HIDDEN))
        x = mx.concatenate([cls, x], axis=1)

        for i in range(RADIO_LAYERS):
            prefix = f"radio.blocks.{i}"
            h = _layer_norm(x, w[f"{prefix}.norm1.weight"], w[f"{prefix}.norm1.bias"])
            qkv = _linear(h, w[f"{prefix}.attn.qkv.weight"], w[f"{prefix}.attn.qkv.bias"])
            qkv = mx.reshape(qkv, (qkv.shape[0], qkv.shape[1], 3, RADIO_HEADS, RADIO_HEAD_DIM))
            qkv = mx.transpose(qkv, (2, 0, 3, 1, 4))
            q, k, v = qkv[0], qkv[1], qkv[2]
            attn = mx.fast.scaled_dot_product_attention(q, k, v, scale=RADIO_HEAD_DIM ** -0.5)
            attn = mx.transpose(attn, (0, 2, 1, 3))
            attn = mx.reshape(attn, (x.shape[0], x.shape[1], RADIO_HIDDEN))
            x = x + _linear(attn, w[f"{prefix}.attn.proj.weight"], w[f"{prefix}.attn.proj.bias"])

            h = _layer_norm(x, w[f"{prefix}.norm2.weight"], w[f"{prefix}.norm2.bias"])
            h = _linear(h, w[f"{prefix}.mlp.fc1.weight"], w[f"{prefix}.mlp.fc1.bias"])
            h = _gelu(h)
            h = _linear(h, w[f"{prefix}.mlp.fc2.weight"], w[f"{prefix}.mlp.fc2.bias"])
            x = x + h

            if i in {0, 7, 15, 23, 31}:
                mx.eval(x)
                print(f"[radio] block {i + 1}/{RADIO_LAYERS} done", flush=True)

        if "radio.norm.weight" in w and "radio.norm.bias" in w:
            x = _layer_norm(x, w["radio.norm.weight"], w["radio.norm.bias"])
        summary = mx.take(x, w["radio.summary_idxs"], axis=1)
        feature = x[:, w["radio.cls_token"].shape[0]:, :]
        return summary, feature

    def __call__(self, pixel_values: mx.array) -> mx.array:
        w = self.w
        summary, feature = self._radio_backbone(pixel_values)

        # Conv1d 1x1 as linear over features.
        out = _linear(feature, w["neck.conv1.weight"][:, 0, :], w["neck.conv1.bias"])
        out = _layer_norm(out, w["neck.ln1.weight"], w["neck.ln1.bias"])

        b = out.shape[0]
        h = pixel_values.shape[-2] // PATCH_SIZE
        wd = pixel_values.shape[-1] // PATCH_SIZE
        out = mx.reshape(out, (b, h, wd, NECK_HIDDEN))
        # Conv2d kernel=(1,4), stride=(1,4), no bias.
        out = mx.reshape(out, (b, h, wd // 4, 4, NECK_HIDDEN))
        out = mx.reshape(out, (b, h, wd // 4, 4 * NECK_HIDDEN))
        conv2 = mx.reshape(w["neck.conv2.weight"], (NECK_HIDDEN, 4 * NECK_HIDDEN))
        out = _linear(out, conv2)
        out = mx.reshape(out, (b, h * (wd // 4), NECK_HIDDEN))
        out = _layer_norm(out, w["neck.ln2.weight"], w["neck.ln2.bias"])

        summary = mx.reshape(summary, (b, -1))
        summary = _linear(summary, w["neck.sum_proj.weight"], w["neck.sum_proj.bias"])
        summary = _layer_norm(summary, w["neck.ln3.weight"], w["neck.ln3.bias"])
        return mx.concatenate([out, summary[:, None, :]], axis=1)


def self_test(weights_dir: str | Path | None = None, model_dir: str | Path = DEFAULT_HF_MODEL_DIR) -> dict:
    free = _free_memory_mb()
    if free is not None and free < 2048:
        return {"ok": False, "error": f"free memory too low: {free:.0f} MB"}

    golden = json.loads((Path(model_dir) / "golden_outputs.json").read_text(encoding="utf-8"))
    pixels = mx.array(preprocess_image(make_test_image()))
    enc = RadioEncoder.load(weights_dir)
    started = time.monotonic()
    out = enc(pixels)
    mx.eval(out)
    arr = np.array(out.astype(mx.float32))
    g = golden["encoder_output"]
    result = {
        "ok": True,
        "shape": list(arr.shape),
        "mean": float(arr.mean()),
        "std": float(arr.std()),
        "token0_first16": arr[0, 0, :16].tolist(),
        "duration_sec": round(time.monotonic() - started, 3),
        "errors": [],
    }
    if result["shape"] != g["shape"]:
        result["errors"].append(f"shape {result['shape']} != {g['shape']}")
    if abs(result["mean"] - g["mean"]) >= 0.05:
        result["errors"].append(f"mean {result['mean']} != {g['mean']}")
    if abs(result["std"] - g["std"]) >= 0.05:
        result["errors"].append(f"std {result['std']} != {g['std']}")
    for i, (a, e) in enumerate(zip(result["token0_first16"], g["token0_first16"])):
        if abs(a - e) >= 0.1:
            result["errors"].append(f"token0_first16[{i}] {a} != {e}")
            break
    result["ok"] = not result["errors"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--weights", default=str(DEFAULT_MLX_MODEL_DIR / "bf16"))
    parser.add_argument("--model-dir", default=str(DEFAULT_HF_MODEL_DIR))
    args = parser.parse_args()
    if args.self_test:
        result = self_test(args.weights, args.model_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    parser.error("no action requested")


if __name__ == "__main__":
    raise SystemExit(main())
