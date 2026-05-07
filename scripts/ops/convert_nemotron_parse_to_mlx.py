#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

import mlx.core as mx
from safetensors import safe_open

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.engine.ocr.nemotron_mlx.config import DEFAULT_HF_MODEL_DIR, DEFAULT_MLX_MODEL_DIR
from skills.engine.ocr.nemotron_mlx.weight_map import (
    WEIGHT_MAP_VERSION,
    conv_tensor_names,
    map_tensor_name,
    transpose_conv_weight,
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024 * 8), b""):
            h.update(chunk)
    return h.hexdigest()


def _target_dtype(name: str):
    if name.endswith("summary_idxs"):
        return mx.int32
    return None


def _cast_float(arr: mx.array, dtype: str) -> mx.array:
    if dtype == "fp32":
        return arr.astype(mx.float32)
    if dtype == "bf16":
        return arr.astype(mx.bfloat16)
    raise ValueError(f"unsupported dtype: {dtype}")


def convert(src: Path, dst: Path, dtype: str, *, dry_run: bool = False) -> dict:
    started = time.monotonic()
    src = src.expanduser().resolve()
    dst = dst.expanduser().resolve()
    safetensors_path = src / "model.safetensors"
    out_dir = dst / dtype
    out_weights = out_dir / "model.safetensors"
    out_manifest = out_dir / "conversion_manifest.json"

    if not safetensors_path.is_file():
        raise FileNotFoundError(safetensors_path)

    tensors: dict[str, mx.array] = {}
    mapped: list[dict] = []
    skipped: list[str] = []
    transposed: list[str] = []

    with safe_open(str(safetensors_path), framework="numpy") as f:
        keys = list(f.keys())
        convs = set(conv_tensor_names(keys))
        for idx, key in enumerate(keys, start=1):
            try:
                mapped_name = map_tensor_name(key)
            except KeyError:
                skipped.append(key)
                continue
            np_arr = f.get_tensor(key)
            original_shape = list(np_arr.shape)
            if key in convs:
                np_arr = transpose_conv_weight(key, np_arr)
                transposed.append(key)
            arr = mx.array(np_arr)
            if key.endswith("summary_idxs"):
                arr = arr.astype(mx.int32)
            elif str(np_arr.dtype).startswith("float"):
                arr = _cast_float(arr, dtype)
            tensors[mapped_name] = arr
            mapped.append({
                "source": key,
                "target": mapped_name,
                "source_shape": original_shape,
                "target_shape": list(arr.shape),
                "dtype": str(arr.dtype),
                "transposed": key in convs,
            })
            if idx % 50 == 0:
                print(f"[convert] mapped {idx}/{len(keys)} tensors", flush=True)

    manifest = {
        "source_model": "nvidia/NVIDIA-Nemotron-Parse-v1.2",
        "source_dir": str(src),
        "source_sha256": _sha256(safetensors_path),
        "source_safetensors_bytes": safetensors_path.stat().st_size,
        "source_dtype": "F32",
        "target_dtype": dtype,
        "tensor_count": len(mapped),
        "source_tensor_count": len(mapped) + len(skipped),
        "skipped_tensors": skipped,
        "transposed_conv_tensors": transposed,
        "mapped_tensors": mapped,
        "conversion_time_iso": datetime.now(timezone.utc).isoformat(),
        "conversion_duration_sec": 0.0,
        "weight_map_version": WEIGHT_MAP_VERSION,
        "notes": [
            "patch_generator.embedder.weight is a flattened linear patch projection in this HF snapshot; it is not transposed as Conv2d.",
            "Albumentations padding quirk is handled in nemotron_mlx.image_processor, not in weights.",
        ],
    }

    if skipped:
        manifest["conversion_duration_sec"] = round(time.monotonic() - started, 3)
        raise RuntimeError(f"unmapped tensors: {skipped[:5]} (total={len(skipped)})")

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        mx.save_safetensors(str(out_weights), tensors)
        manifest["output_weights"] = str(out_weights)
        manifest["output_safetensors_bytes"] = out_weights.stat().st_size
        manifest["output_manifest"] = str(out_manifest)
        manifest["conversion_duration_sec"] = round(time.monotonic() - started, 3)
        out_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        manifest["conversion_duration_sec"] = round(time.monotonic() - started, 3)
        manifest["dry_run"] = True

    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert Nemotron Parse HF safetensors to MLX safetensors.")
    parser.add_argument("--src", default=str(DEFAULT_HF_MODEL_DIR))
    parser.add_argument("--dst", default=str(DEFAULT_MLX_MODEL_DIR))
    parser.add_argument("--dtype", choices=["bf16", "fp32"], default="bf16")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    manifest = convert(Path(args.src), Path(args.dst), args.dtype, dry_run=args.dry_run)
    print(json.dumps({
        "ok": True,
        "tensor_count": manifest["tensor_count"],
        "skipped_tensors": manifest["skipped_tensors"],
        "transposed_conv_tensors": manifest["transposed_conv_tensors"],
        "target_dtype": manifest["target_dtype"],
        "output_weights": manifest.get("output_weights"),
        "output_safetensors_bytes": manifest.get("output_safetensors_bytes"),
        "dry_run": manifest.get("dry_run", False),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

