#!/usr/bin/env python3
"""
Merge TAIDE text weights into Gemma-3 VLM.

Strategy:
  - Base: Gemma-3-12b-it-4bit (complete VLM with working vision pipeline)
  - Replace: all language_model.* weights with TAIDE's
  - Keep: vision_tower.* + multi_modal_projector.* from Gemma-3
  - Tokenizer: use TAIDE's (larger vocab, 318080)
  - Output name: TAIDE-12b-Chat-mlx-4bit (same as original, direct replacement)

Result: TAIDE with Gemma-3's vision capability, no retraining needed.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

# ── Paths ──
TAIDE_DIR = Path(os.path.expanduser("~/.omlx/models/TAIDE-12b-Chat-mlx-4bit"))
GEMMA_DIR = Path(os.path.expanduser("~/.omlx/models-chat/gemma-3-12b-it-4bit"))
OUTPUT_DIR = TAIDE_DIR  # overwrite in-place after backup
BACKUP_DIR = Path(os.path.expanduser("~/.omlx/models/TAIDE-12b-Chat-mlx-4bit-textonly-backup"))


def main():
    # Add omlx site-packages for mlx
    omlx_sp = "/opt/homebrew/opt/omlx/libexec/lib/python3.11/site-packages"
    if omlx_sp not in sys.path:
        sys.path.insert(0, omlx_sp)
    import mlx.core as mx

    # ── Step 1: Backup original TAIDE ──
    if not BACKUP_DIR.exists():
        print(f"Step 1: Backing up original TAIDE to {BACKUP_DIR.name}...")
        shutil.copytree(TAIDE_DIR, BACKUP_DIR)
        print(f"  Backup complete ({sum(f.stat().st_size for f in BACKUP_DIR.rglob('*') if f.is_file()) / 1e9:.1f} GB)")
    else:
        print(f"Step 1: Backup already exists at {BACKUP_DIR.name}, skipping")

    # ── Step 2: Load weight indices ──
    print("\nStep 2: Loading weight indices...")

    with open(TAIDE_DIR / "model.safetensors.index.json") as f:
        taide_index = json.load(f)
    with open(GEMMA_DIR / "model.safetensors.index.json") as f:
        gemma_index = json.load(f)

    # Classify Gemma-3 weights
    gemma_vision_keys = [k for k in gemma_index["weight_map"]
                         if k.startswith(("vision_tower.", "multi_modal_projector."))]
    gemma_text_keys = [k for k in gemma_index["weight_map"]
                       if k.startswith("language_model.")]
    taide_text_keys = list(taide_index["weight_map"].keys())

    print(f"  Gemma-3 vision keys: {len(gemma_vision_keys)}")
    print(f"  Gemma-3 text keys:   {len(gemma_text_keys)}")
    print(f"  TAIDE text keys:     {len(taide_text_keys)}")

    # ── Step 3: Load all weights ──
    print("\nStep 3: Loading weights...")

    # Load Gemma-3 vision weights
    gemma_shards = set(gemma_index["weight_map"][k] for k in gemma_vision_keys)
    vision_weights = {}
    for shard in sorted(gemma_shards):
        print(f"  Loading Gemma-3 {shard} (vision)...")
        w = mx.load(str(GEMMA_DIR / shard))
        for k in gemma_vision_keys:
            if gemma_index["weight_map"][k] == shard and k in w:
                vision_weights[k] = w[k]
    print(f"  Loaded {len(vision_weights)} vision tensors")

    # Load TAIDE text weights (use backup to avoid conflicts)
    taide_source = BACKUP_DIR if BACKUP_DIR.exists() else TAIDE_DIR
    taide_shards = set(taide_index["weight_map"].values())
    text_weights = {}
    for shard in sorted(taide_shards):
        print(f"  Loading TAIDE {shard} (text)...")
        w = mx.load(str(taide_source / shard))
        for k in taide_text_keys:
            if taide_index["weight_map"].get(k) == shard and k in w:
                text_weights[k] = w[k]
    print(f"  Loaded {len(text_weights)} text tensors")

    # ── Step 4: Merge and save ──
    print("\nStep 4: Merging weights...")

    all_weights = {}
    all_weights.update(text_weights)    # TAIDE text
    all_weights.update(vision_weights)  # Gemma-3 vision

    # Add lm_head from embed_tokens (weight tying, needed for mlx_vlm loading)
    embed_key = "language_model.model.embed_tokens.weight"
    if embed_key in all_weights and "language_model.lm_head.weight" not in all_weights:
        for suffix in ["weight", "scales", "biases"]:
            src = f"language_model.model.embed_tokens.{suffix}"
            dst = f"language_model.lm_head.{suffix}"
            if src in all_weights:
                all_weights[dst] = all_weights[src]
                print(f"  Tied {dst} -> {src}")

    total_keys = len(all_weights)
    print(f"  Total merged tensors: {total_keys}")

    # Split into 2 shards: text (shard 1) + vision (shard 2)
    shard1_weights = {k: v for k, v in all_weights.items()
                      if k.startswith("language_model.")}
    shard2_weights = {k: v for k, v in all_weights.items()
                      if not k.startswith("language_model.")}

    shard1_file = "model-00001-of-00002.safetensors"
    shard2_file = "model-00002-of-00002.safetensors"

    print(f"\n  Saving {shard1_file} ({len(shard1_weights)} tensors, text)...")
    mx.save_safetensors(str(OUTPUT_DIR / shard1_file), shard1_weights)

    print(f"  Saving {shard2_file} ({len(shard2_weights)} tensors, vision)...")
    mx.save_safetensors(str(OUTPUT_DIR / shard2_file), shard2_weights)

    # ── Step 5: Build new index ──
    weight_map = {}
    for k in shard1_weights:
        weight_map[k] = shard1_file
    for k in shard2_weights:
        weight_map[k] = shard2_file

    index = {
        "metadata": {"total_size": sum(v.nbytes for v in all_weights.values())},
        "weight_map": dict(sorted(weight_map.items())),
    }
    with open(OUTPUT_DIR / "model.safetensors.index.json", "w") as f:
        json.dump(index, f, indent=2)
    print(f"  Updated index ({len(weight_map)} entries)")

    # ── Step 6: Merge config ──
    print("\nStep 5: Merging config...")

    with open(BACKUP_DIR / "config.json") as f:
        taide_config = json.load(f)
    with open(GEMMA_DIR / "config.json") as f:
        gemma_config = json.load(f)

    # Start from TAIDE config, add vision
    merged_config = taide_config.copy()
    merged_config["vision_config"] = gemma_config["vision_config"]
    # Ensure mm_tokens_per_image is set
    if "mm_tokens_per_image" not in merged_config:
        merged_config["mm_tokens_per_image"] = 256
    # Ensure image_token_index
    if "image_token_index" not in merged_config:
        merged_config["image_token_index"] = 262144

    with open(OUTPUT_DIR / "config.json", "w") as f:
        json.dump(merged_config, f, indent=2)
    print(f"  Config saved with vision_config + vocab_size={merged_config.get('vocab_size')}")

    # ── Step 7: Copy Gemma-3's vision processor files ──
    print("\nStep 6: Copying vision processor files...")
    for fname in ["preprocessor_config.json", "processor_config.json",
                   "chat_template.json", "special_tokens_map.json"]:
        src = GEMMA_DIR / fname
        if src.exists():
            shutil.copy2(src, OUTPUT_DIR / fname)
            print(f"  Copied {fname}")

    # Keep TAIDE's tokenizer (larger vocab)
    print("  Keeping TAIDE tokenizer (vocab_size=318080)")

    # ── Step 8: Verify ──
    print("\nStep 7: Verifying...")
    shard1_size = (OUTPUT_DIR / shard1_file).stat().st_size / 1e9
    shard2_size = (OUTPUT_DIR / shard2_file).stat().st_size / 1e9
    print(f"  {shard1_file}: {shard1_size:.1f} GB (text)")
    print(f"  {shard2_file}: {shard2_size:.1f} GB (vision)")
    print(f"  Total: {shard1_size + shard2_size:.1f} GB")

    # Clean up old TAIDE-Vision model if exists
    old_vision = Path(os.path.expanduser("~/.omlx/models/TAIDE-12b-Vision-mlx-4bit"))
    if old_vision.exists():
        print(f"\n  Note: Old TAIDE-12b-Vision-mlx-4bit still exists at {old_vision}")

    print("\n✓ Merge complete!")
    print(f"  Model: {OUTPUT_DIR}")
    print("  重新啟動 oMLX 即可使用 TAIDE-12b-Chat-mlx-4bit（含視覺能力）")


if __name__ == "__main__":
    main()
