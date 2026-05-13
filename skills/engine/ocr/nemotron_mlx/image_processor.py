from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image, ImageDraw

from .config import DEFAULT_HF_MODEL_DIR


TASK_PROMPT = "</s><s><predict_bbox><predict_classes><output_markdown><predict_no_text_in_pic>"


def make_test_image() -> Image.Image:
    """Deterministic document-like image copied from NVIDIA's golden test."""
    img = Image.new("RGB", (400, 600), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 380, 80], fill=(210, 210, 210))
    draw.rectangle([20, 100, 380, 480], fill=(245, 245, 245))
    for y in range(120, 470, 18):
        draw.line([(40, y), (360, y)], fill=(170, 170, 170), width=1)
    draw.rectangle([20, 500, 380, 580], fill=(200, 220, 200))
    for x in range(80, 380, 80):
        draw.line([(x, 500), (x, 580)], fill=(100, 140, 100), width=1)
    for y in range(520, 580, 20):
        draw.line([(20, y), (380, y)], fill=(100, 140, 100), width=1)
    return img


def _resize_with_aspect_ratio(img: Image.Image, target_hw: tuple[int, int]) -> Image.Image:
    target_h, target_w = target_hw
    width, height = img.size
    aspect_ratio = width / height
    new_h = height
    new_w = width
    if height > target_h:
        new_h = target_h
        new_w = int(new_h * aspect_ratio)
    if new_w > target_w:
        new_w = target_w
        new_h = int(new_w / aspect_ratio)
    if (new_w, new_h) == (width, height):
        return img
    return img.resize((new_w, new_h), resample=Image.Resampling.BILINEAR)


def preprocess_image(image: Image.Image, target_hw: tuple[int, int] = (2048, 1664)) -> np.ndarray:
    """Return pixel_values with shape [1, 3, H, W] and values in [0, 1].

    Mirrors the HF snapshot's processor:
    - RGB conversion
    - LongestMaxSizeHW-style resize only when image exceeds target dimensions
    - Albumentations PadIfNeeded default center placement
    - zero/black padding because the snapshot's ``value=[255,255,255]`` argument
      is ignored by the installed albumentations API, as captured by golden stats.
    - torchvision ToTensor layout/range conversion
    """
    target_h, target_w = target_hw
    resized = _resize_with_aspect_ratio(image.convert("RGB"), target_hw)
    new_w, new_h = resized.size

    canvas = Image.new("RGB", (target_w, target_h), color=(0, 0, 0))
    left = max(0, (target_w - new_w) // 2)
    top = max(0, (target_h - new_h) // 2)
    canvas.paste(resized, (left, top))

    arr = np.asarray(canvas, dtype=np.float32) / 255.0
    chw = np.transpose(arr, (2, 0, 1))
    return chw[None, :, :, :]


def load_golden(model_dir: str | Path = DEFAULT_HF_MODEL_DIR) -> dict:
    return json.loads((Path(model_dir) / "golden_outputs.json").read_text(encoding="utf-8"))


def self_test(model_dir: str | Path = DEFAULT_HF_MODEL_DIR) -> dict:
    golden = load_golden(model_dir)
    pv = preprocess_image(make_test_image())
    g = golden["image_processing"]
    first = pv.reshape(-1)[:20].tolist()
    result = {
        "ok": True,
        "shape": list(pv.shape),
        "mean": float(pv.mean()),
        "std": float(pv.std()),
        "first_20_values": first,
        "golden": g,
        "errors": [],
    }
    if result["shape"] != g["shape"]:
        result["errors"].append(f"shape {result['shape']} != {g['shape']}")
    if abs(result["mean"] - g["mean"]) >= 1e-4:
        result["errors"].append(f"mean {result['mean']} != {g['mean']}")
    if abs(result["std"] - g["std"]) >= 1e-4:
        result["errors"].append(f"std {result['std']} != {g['std']}")
    for i, (a, e) in enumerate(zip(first, g["first_20_values"])):
        if abs(a - e) >= 1e-5:
            result["errors"].append(f"first_20[{i}] {a} != {e}")
            break
    result["ok"] = not result["errors"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--model-dir", default=str(DEFAULT_HF_MODEL_DIR))
    args = parser.parse_args()
    if args.self_test:
        result = self_test(args.model_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    parser.error("no action requested")


if __name__ == "__main__":
    raise SystemExit(main())

