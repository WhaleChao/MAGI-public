#!/usr/bin/env python3
"""
方案A: Graft SigLIP vision encoder from gemma-3-12b-it-4bit onto TAIDE-12b-Chat-mlx-4bit.

Both models share the Gemma3ForConditionalGeneration architecture.
SigLIP was frozen during Gemma-3 training, so the weights are directly compatible.

Steps:
1. Extract vision_tower + multi_modal_projector weights from gemma-3-12b-it-4bit
2. Create a new safetensors file containing these weights
3. Update TAIDE's model.safetensors.index.json to include them
4. Add vision_config to TAIDE's config.json

Usage:
    python3 scripts/graft_siglip_to_taide.py [--dry-run]
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

TAIDE_DIR = Path(os.environ.get(
    "TAIDE_MODEL_DIR",
    os.path.expanduser("~/.omlx/models/TAIDE-12b-Chat-mlx-4bit"),
))
GEMMA_DIR = Path(os.environ.get(
    "GEMMA_MODEL_DIR",
    os.path.expanduser("~/.omlx/models-chat/gemma-3-12b-it-4bit"),
))
OUTPUT_DIR = Path(os.environ.get(
    "OUTPUT_MODEL_DIR",
    os.path.expanduser("~/.omlx/models/TAIDE-12b-Vision-mlx-4bit"),
))


def main():
    parser = argparse.ArgumentParser(description="Graft SigLIP onto TAIDE")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    args = parser.parse_args()

    # Validate source directories
    for d, name in [(TAIDE_DIR, "TAIDE"), (GEMMA_DIR, "Gemma-3")]:
        if not d.exists():
            print(f"ERROR: {name} directory not found: {d}")
            sys.exit(1)

    # Load index files
    taide_index_path = TAIDE_DIR / "model.safetensors.index.json"
    gemma_index_path = GEMMA_DIR / "model.safetensors.index.json"

    with open(taide_index_path) as f:
        taide_index = json.load(f)
    with open(gemma_index_path) as f:
        gemma_index = json.load(f)

    # Identify vision weights in Gemma-3
    gemma_wm = gemma_index.get("weight_map", {})
    vision_keys = sorted(
        k for k in gemma_wm
        if k.startswith("vision_tower.") or k.startswith("multi_modal_projector.")
    )

    if not vision_keys:
        print("ERROR: No vision weights found in Gemma-3 model")
        sys.exit(1)

    # Identify which safetensors files contain vision weights
    vision_files = sorted(set(gemma_wm[k] for k in vision_keys))
    print(f"Found {len(vision_keys)} vision weights in {len(vision_files)} file(s): {vision_files}")

    # Load Gemma-3 config for vision_config
    with open(GEMMA_DIR / "config.json") as f:
        gemma_config = json.load(f)

    vision_config = gemma_config.get("vision_config")
    if not vision_config:
        print("ERROR: No vision_config found in Gemma-3 config.json")
        sys.exit(1)

    print(f"Vision config: {json.dumps(vision_config, indent=2)}")

    if args.dry_run:
        print(f"\n[DRY RUN] Would create output dir: {OUTPUT_DIR}")
        print(f"[DRY RUN] Would copy TAIDE model files to output dir")
        print(f"[DRY RUN] Would extract {len(vision_keys)} vision weights from Gemma-3")
        print(f"[DRY RUN] Would update config.json with vision_config")
        print(f"[DRY RUN] Would update model.safetensors.index.json")
        return

    # Create output directory
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Copy TAIDE files to output (preserving originals)
    print(f"\nCopying TAIDE base model to {OUTPUT_DIR}...")
    for item in TAIDE_DIR.iterdir():
        dest = OUTPUT_DIR / item.name
        if item.is_file() and not dest.exists():
            shutil.copy2(item, dest)
            print(f"  Copied {item.name}")
        elif item.is_file():
            print(f"  Skipped {item.name} (already exists)")

    # Extract vision weights using mlx (supports bfloat16 natively on Apple Silicon)
    import mlx.core as mx

    # Load vision weights from Gemma-3
    print(f"\nExtracting vision weights from Gemma-3...")
    vision_tensors = {}
    for shard_file in vision_files:
        shard_path = GEMMA_DIR / shard_file
        print(f"  Loading {shard_file} (vision keys only)...")
        from safetensors import safe_open
        with safe_open(str(shard_path), framework="numpy") as f:
            shard_keys = set(f.keys())
            for key in vision_keys:
                if key in shard_keys:
                    # Load as mlx array to preserve bfloat16
                    pass  # Will use mx.load below

    # Use mx.load which handles bfloat16 natively
    for shard_file in vision_files:
        shard_path = GEMMA_DIR / shard_file
        print(f"  mx.load {shard_file}...")
        all_tensors = mx.load(str(shard_path))
        for key in vision_keys:
            if key in all_tensors:
                vision_tensors[key] = all_tensors[key]
        del all_tensors  # Free memory

    print(f"  Extracted {len(vision_tensors)}/{len(vision_keys)} vision tensors")

    if len(vision_tensors) != len(vision_keys):
        missing = set(vision_keys) - set(vision_tensors.keys())
        print(f"  WARNING: Missing {len(missing)} tensors: {list(missing)[:5]}...")

    # Save vision weights as a new safetensors file using mx
    vision_shard_name = "model-vision.safetensors"
    vision_shard_path = OUTPUT_DIR / vision_shard_name
    print(f"\nSaving vision weights to {vision_shard_name}...")
    mx.save_safetensors(str(vision_shard_path), vision_tensors)
    vision_size = vision_shard_path.stat().st_size
    print(f"  Saved {vision_size / 1024 / 1024:.1f} MB")

    # Update model.safetensors.index.json
    print("\nUpdating model.safetensors.index.json...")
    taide_wm = taide_index.get("weight_map", {})
    for key in vision_keys:
        taide_wm[key] = vision_shard_name
    taide_index["weight_map"] = taide_wm

    # Recalculate total_size
    # Count all safetensor files
    total_size = 0
    shard_files = set(taide_wm.values())
    for sf in shard_files:
        sp = OUTPUT_DIR / sf
        if sp.exists():
            total_size += sp.stat().st_size
    taide_index["metadata"] = taide_index.get("metadata", {})
    taide_index["metadata"]["total_size"] = total_size

    with open(OUTPUT_DIR / "model.safetensors.index.json", "w") as f:
        json.dump(taide_index, f, indent=2, ensure_ascii=False)
    print(f"  Total weights: {len(taide_wm)}")

    # Update config.json with vision_config
    print("\nUpdating config.json with vision_config...")
    taide_config_path = OUTPUT_DIR / "config.json"
    with open(taide_config_path) as f:
        taide_config = json.load(f)

    # Add vision config — keep skip_vision=true because vision weights are bf16 (not quantized)
    # mlx_vlm uses skip_vision to skip quantization of vision layers, NOT to skip loading them
    vision_config_copy = dict(vision_config)
    vision_config_copy["skip_vision"] = True

    taide_config["vision_config"] = vision_config_copy
    # Ensure special tokens for vision are present
    taide_config.setdefault("boi_token_index", gemma_config.get("boi_token_index", 255999))
    taide_config.setdefault("eoi_token_index", gemma_config.get("eoi_token_index", 256000))
    taide_config.setdefault("image_token_index", gemma_config.get("image_token_index", 262144))
    taide_config.setdefault("mm_tokens_per_image", gemma_config.get("mm_tokens_per_image", 256))

    with open(taide_config_path, "w") as f:
        json.dump(taide_config, f, indent=4, ensure_ascii=False)
    print(f"  Added vision_config with {vision_config_copy.get('num_hidden_layers', '?')} SigLIP layers")

    # Copy preprocessor_config.json if exists (needed for image processing)
    for extra_file in ["preprocessor_config.json", "processor_config.json"]:
        src = GEMMA_DIR / extra_file
        if src.exists():
            shutil.copy2(src, OUTPUT_DIR / extra_file)
            print(f"  Copied {extra_file} from Gemma-3")

    print(f"\n✅ Done! TAIDE+Vision model saved to: {OUTPUT_DIR}")
    print(f"   Total model size: {total_size / 1024 / 1024 / 1024:.1f} GB")
    print(f"   Vision weights: {vision_size / 1024 / 1024:.1f} MB")
    print(f"\nTo test: set MAGI_OMLX_VISION_MODEL=TAIDE-12b-Vision-mlx-4bit in .env")
    print("NOTE: This is experimental. If oMLX fails to load the model,")
    print("      the vision_config architecture must match oMLX's Gemma3 implementation.")


if __name__ == "__main__":
    main()
