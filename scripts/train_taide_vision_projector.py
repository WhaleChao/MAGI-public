#!/usr/bin/env python3
"""
Train TAIDE-12b-Vision's multimodal projector to align SigLIP features with TAIDE's text space.

Strategy:
  1. Use Gemma-3-12b-it as teacher to generate image descriptions
  2. Freeze vision_tower + language_model, train only multi_modal_projector
  3. ~200 iterations with learning rate 2e-4 is sufficient for 4.4M params

Usage:
    # Step 1: Generate training data (uses Gemma-3 via oMLX)
    python3 scripts/train_taide_vision_projector.py --generate-data --num-samples 200

    # Step 2: Train projector
    python3 scripts/train_taide_vision_projector.py --train --iters 200

    # Step 3: Apply trained projector to model
    python3 scripts/train_taide_vision_projector.py --apply
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import random
import sys
import time
from pathlib import Path

# ── Paths ──
MODEL_DIR = Path(os.environ.get(
    "TAIDE_VISION_DIR",
    os.path.expanduser("~/.omlx/models/TAIDE-12b-Vision-mlx-4bit"),
))
GEMMA_DIR = Path(os.environ.get(
    "GEMMA_DIR",
    os.path.expanduser("~/.omlx/models-chat/gemma-3-12b-it-4bit"),
))
TRAIN_DATA_DIR = Path(os.environ.get(
    "TRAIN_DATA_DIR",
    os.path.expanduser("~/.omlx/training/taide-vision"),
))
OMLX_URL = os.environ.get("MAGI_OMLX_CHAT_URL", "http://127.0.0.1:8080")


# ═══════════════════════════════════════════════════════════════════════════
# Phase 1: Generate training data using Gemma-3 as teacher
# ═══════════════════════════════════════════════════════════════════════════

def _download_sample_images(target_dir: Path, num_images: int = 200):
    """Download diverse sample images for training."""
    import urllib.request

    target_dir.mkdir(parents=True, exist_ok=True)
    existing = list(target_dir.glob("*.jpg")) + list(target_dir.glob("*.png"))
    if len(existing) >= num_images:
        print(f"Already have {len(existing)} images, skipping download")
        return existing[:num_images]

    # Use picsum.photos for diverse, royalty-free images
    downloaded = list(existing)
    for i in range(len(existing), num_images):
        img_path = target_dir / f"train_{i:04d}.jpg"
        if img_path.exists():
            downloaded.append(img_path)
            continue
        try:
            # Random size variation for diversity
            w = random.choice([320, 480, 640])
            h = random.choice([240, 320, 480])
            url = f"https://picsum.photos/{w}/{h}"
            urllib.request.urlretrieve(url, str(img_path))
            downloaded.append(img_path)
            if (i + 1) % 20 == 0:
                print(f"  Downloaded {i + 1}/{num_images} images")
        except Exception as e:
            print(f"  Failed to download image {i}: {e}")
            continue

    print(f"Total images: {len(downloaded)}")
    return downloaded


def _generate_description(image_path: str, timeout: int = 120) -> str:
    """Use Gemma-3 via oMLX to generate image description in Traditional Chinese."""
    import requests

    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    payload = {
        "model": "gemma-3-12b-it-4bit",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": (
                    "請用繁體中文詳細描述這張圖片。包含：\n"
                    "1. 圖片中的主要物體或場景\n"
                    "2. 顏色、光線、構圖\n"
                    "3. 任何文字或符號\n"
                    "4. 整體氛圍或情境\n"
                    "回答限 100-200 字。"
                )},
            ]
        }],
        "max_tokens": 512,
        "temperature": 0.3,
    }

    r = requests.post(
        f"{OMLX_URL}/v1/chat/completions",
        json=payload,
        timeout=timeout,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    return content.strip()


def generate_training_data(num_samples: int = 200, cooldown: float = 5.0):
    """Generate image-description pairs using Gemma-3 as teacher."""
    TRAIN_DATA_DIR.mkdir(parents=True, exist_ok=True)
    images_dir = TRAIN_DATA_DIR / "images"
    data_file = TRAIN_DATA_DIR / "train_data.jsonl"

    # Download images
    print("Step 1: Downloading sample images...")
    images = _download_sample_images(images_dir, num_samples)

    # Generate descriptions
    print(f"\nStep 2: Generating descriptions with Gemma-3 ({len(images)} images)...")

    # Load existing data to resume
    existing = {}
    if data_file.exists():
        with open(data_file) as f:
            for line in f:
                try:
                    item = json.loads(line)
                    existing[item["image"]] = item
                except Exception:
                    pass
    print(f"  Existing descriptions: {len(existing)}")

    new_count = 0
    errors = 0
    with open(data_file, "a") as fout:
        for i, img_path in enumerate(images):
            img_name = img_path.name
            if img_name in existing:
                continue

            try:
                desc = _generate_description(str(img_path))
                if not desc or len(desc) < 20:
                    errors += 1
                    continue

                item = {
                    "image": img_name,
                    "messages": [
                        {"role": "user", "content": "請用繁體中文詳細描述這張圖片。"},
                        {"role": "assistant", "content": desc},
                    ]
                }
                fout.write(json.dumps(item, ensure_ascii=False) + "\n")
                fout.flush()
                new_count += 1

                if (new_count) % 10 == 0:
                    print(f"  Generated {new_count} descriptions ({i+1}/{len(images)} images)")

                # Cooldown to prevent oMLX overload
                time.sleep(cooldown)

            except Exception as e:
                errors += 1
                print(f"  Error on {img_name}: {e}")
                time.sleep(2)

    total = len(existing) + new_count
    print(f"\nDone! Total training samples: {total} (new: {new_count}, errors: {errors})")
    print(f"Data saved to: {data_file}")


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2: Train the multimodal projector
# ═══════════════════════════════════════════════════════════════════════════

def _load_model_and_processor():
    """Load TAIDE-Vision model with mlx_vlm."""
    # Add omlx's site-packages to path
    omlx_sp = "/opt/homebrew/opt/omlx/libexec/lib/python3.11/site-packages"
    if omlx_sp not in sys.path:
        sys.path.insert(0, omlx_sp)

    import mlx.core as mx
    import mlx.nn as nn
    from mlx_vlm.utils import load as load_vlm

    print(f"Loading model from {MODEL_DIR}...")
    model, processor = load_vlm(str(MODEL_DIR))
    config = model.config

    return model, processor, config


def _freeze_except_projector(model):
    """Freeze everything except multi_modal_projector."""
    import mlx.nn as nn

    # Freeze entire model first
    model.freeze()

    # Unfreeze only projector
    model.multi_modal_projector.unfreeze()

    # Count params
    from mlx.utils import tree_flatten
    trainable = sum(v.size for _, v in tree_flatten(model.trainable_parameters()))
    total = sum(v.size for _, v in tree_flatten(model.parameters()))
    print(f"Trainable params: {trainable:,} / {total:,} ({100*trainable/total:.4f}%)")

    return model


class ProjectorDataset:
    """Simple dataset for projector training."""

    def __init__(self, data_file: Path, images_dir: Path, processor, config):
        self.items = []
        with open(data_file) as f:
            for line in f:
                try:
                    self.items.append(json.loads(line))
                except Exception:
                    pass
        self.images_dir = images_dir
        self.processor = processor
        self.config = config
        print(f"Loaded {len(self.items)} training samples")

    def __len__(self):
        return len(self.items)

    def __getitem__(self, idx):
        import mlx.core as mx
        import numpy as np
        from PIL import Image

        item = self.items[idx]
        img_path = self.images_dir / item["image"]
        image = Image.open(str(img_path)).convert("RGB")

        # Build messages with image content type for Gemma3
        messages = item["messages"]
        vision_messages = []
        for msg in messages:
            if msg["role"] == "user":
                vision_messages.append({
                    "role": "user",
                    "content": [
                        {"type": "image", "image": image},
                        {"type": "text", "text": msg["content"]},
                    ],
                })
            else:
                vision_messages.append({
                    "role": msg["role"],
                    "content": [{"type": "text", "text": msg["content"]}],
                })

        # Apply chat template to get prompt with <start_of_image> tokens
        prompt = self.processor.apply_chat_template(
            vision_messages, tokenize=False, add_generation_prompt=False
        )

        # Process through Gemma3Processor to get pixel_values
        inputs = self.processor(text=[prompt], images=[image], return_tensors="np")

        # Squeeze batch dim — iterate_batches expects 1D input_ids
        input_ids = mx.array(inputs["input_ids"]).squeeze(0)
        attention_mask = mx.array(inputs["attention_mask"]).squeeze(0)
        pixel_values = mx.array(inputs["pixel_values"]).squeeze(0) if "pixel_values" in inputs else None

        result = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "pixel_values": pixel_values,
        }

        # Include token_type_ids if present (Gemma3 uses them for image masking)
        if "token_type_ids" in inputs:
            result["token_type_ids"] = mx.array(inputs["token_type_ids"]).squeeze(0)

        return result


def train_projector(iters: int = 200, lr: float = 2e-4, batch_size: int = 1):
    """Train the multimodal projector."""
    import mlx.core as mx
    import mlx.nn as nn
    from mlx.utils import tree_flatten

    model, processor, config = _load_model_and_processor()
    model = _freeze_except_projector(model)

    # Load dataset
    data_file = TRAIN_DATA_DIR / "train_data.jsonl"
    images_dir = TRAIN_DATA_DIR / "images"
    if not data_file.exists():
        print(f"ERROR: Training data not found at {data_file}")
        print("Run with --generate-data first")
        sys.exit(1)

    dataset = ProjectorDataset(data_file, images_dir, processor, config)
    if len(dataset) < batch_size:
        print(f"ERROR: Need at least {batch_size} samples, got {len(dataset)}")
        sys.exit(1)

    # Set up optimizer
    warmup_steps = min(20, iters // 5)

    def lr_schedule(step):
        if step < warmup_steps:
            return lr * (step / max(warmup_steps, 1))
        progress = (step - warmup_steps) / max(iters - warmup_steps, 1)
        return lr * 0.5 * (1 + math.cos(math.pi * progress))

    import mlx.optimizers
    optimizer = mlx.optimizers.AdamW(learning_rate=lr)

    # Training
    output_dir = TRAIN_DATA_DIR / "checkpoints"
    output_dir.mkdir(parents=True, exist_ok=True)

    if mx.metal.is_available():
        info = mx.device_info()
        max_mem = info.get("max_recommended_working_set_size")
        if max_mem:
            mx.set_wired_limit(max_mem)

    print(f"\nTraining for {iters} iterations (lr={lr}, batch={batch_size})...")

    # Import training infrastructure
    omlx_sp = "/opt/homebrew/opt/omlx/libexec/lib/python3.11/site-packages"
    if omlx_sp not in sys.path:
        sys.path.insert(0, omlx_sp)
    from mlx_vlm.trainer.trainer import vision_language_loss_fn, iterate_batches

    state = [model.state, optimizer.state, mx.random.state]
    loss_value_and_grad = nn.value_and_grad(model, vision_language_loss_fn)

    losses_acc = 0.0
    n_tokens_acc = 0
    best_loss = float("inf")
    t_start = time.time()

    for it, batch in zip(
        range(1, iters + 1),
        iterate_batches(dataset=dataset, batch_size=batch_size, max_seq_length=2048, train=True),
    ):
        # Update learning rate
        cur_lr = lr_schedule(it)
        optimizer.learning_rate = cur_lr

        # Forward + backward
        lvalue, grad = loss_value_and_grad(model, batch)

        # Clip gradients
        from mlx.utils import tree_map
        grad = tree_map(lambda g: mx.clip(g, -1.0, 1.0), grad)

        optimizer.update(model, grad)
        mx.eval(state, lvalue)
        mx.clear_cache()

        losses_acc += lvalue.item()

        # Report
        if it % 10 == 0 or it == iters:
            avg_loss = losses_acc / 10
            elapsed = time.time() - t_start
            it_sec = 10 / elapsed if elapsed > 0 else 0
            peak_mem = mx.get_peak_memory() / 1e9
            print(
                f"Iter {it:4d}/{iters}: loss={avg_loss:.4f}  lr={cur_lr:.2e}  "
                f"it/s={it_sec:.2f}  peak_mem={peak_mem:.1f}GB"
            )
            if avg_loss < best_loss:
                best_loss = avg_loss
            losses_acc = 0.0
            t_start = time.time()

        # Save checkpoint
        if it % 50 == 0 or it == iters:
            ckpt_path = output_dir / f"projector_{it:04d}.safetensors"
            proj_weights = dict(tree_flatten(model.multi_modal_projector.parameters()))
            mx.save_safetensors(str(ckpt_path), proj_weights)
            print(f"  Saved checkpoint: {ckpt_path.name}")

    # Save final
    final_path = output_dir / "projector_final.safetensors"
    proj_weights = dict(tree_flatten(model.multi_modal_projector.parameters()))
    mx.save_safetensors(str(final_path), proj_weights)
    print(f"\nTraining complete! Best loss: {best_loss:.4f}")
    print(f"Final projector saved to: {final_path}")


# ═══════════════════════════════════════════════════════════════════════════
# Phase 3: Apply trained projector back to model
# ═══════════════════════════════════════════════════════════════════════════

def apply_projector():
    """Replace the projector weights in TAIDE-Vision model with trained ones."""
    import mlx.core as mx

    ckpt_path = TRAIN_DATA_DIR / "checkpoints" / "projector_final.safetensors"
    if not ckpt_path.exists():
        print(f"ERROR: Trained projector not found at {ckpt_path}")
        print("Run with --train first")
        sys.exit(1)

    # Load trained projector weights
    trained = mx.load(str(ckpt_path))
    print(f"Loaded trained projector: {len(trained)} tensors")
    for k, v in trained.items():
        print(f"  {k}: shape={v.shape} dtype={v.dtype}")

    # Load current model vision shard
    vision_shard = MODEL_DIR / "model-vision.safetensors"
    if not vision_shard.exists():
        print(f"ERROR: Vision shard not found at {vision_shard}")
        sys.exit(1)

    current = mx.load(str(vision_shard))
    print(f"\nCurrent vision shard: {len(current)} tensors")

    # Replace projector weights
    replaced = 0
    for key, value in trained.items():
        if key in current:
            old_shape = current[key].shape
            if old_shape == value.shape:
                current[key] = value
                replaced += 1
                print(f"  Replaced: {key}")
            else:
                print(f"  SHAPE MISMATCH: {key} ({old_shape} vs {value.shape})")
        else:
            print(f"  NOT FOUND in shard: {key}")

    if replaced == 0:
        print("\nERROR: No weights replaced!")
        sys.exit(1)

    # Save updated vision shard
    backup = MODEL_DIR / "model-vision.safetensors.bak"
    if not backup.exists():
        import shutil
        shutil.copy2(vision_shard, backup)
        print(f"\nBackup saved to: {backup}")

    mx.save_safetensors(str(vision_shard), current)
    print(f"Updated vision shard with {replaced} trained projector weights")
    print(f"\nDone! Restart oMLX to use the updated model.")


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Train TAIDE Vision Projector")
    parser.add_argument("--generate-data", action="store_true", help="Generate training data using Gemma-3")
    parser.add_argument("--train", action="store_true", help="Train the projector")
    parser.add_argument("--apply", action="store_true", help="Apply trained projector to model")
    parser.add_argument("--num-samples", type=int, default=200, help="Number of training samples")
    parser.add_argument("--iters", type=int, default=200, help="Training iterations")
    parser.add_argument("--lr", type=float, default=2e-4, help="Learning rate")
    parser.add_argument("--cooldown", type=float, default=5.0, help="Seconds between Gemma-3 calls")
    args = parser.parse_args()

    if not any([args.generate_data, args.train, args.apply]):
        parser.print_help()
        print("\nExample workflow:")
        print("  1. python3 scripts/train_taide_vision_projector.py --generate-data --num-samples 200")
        print("  2. python3 scripts/train_taide_vision_projector.py --train --iters 200")
        print("  3. python3 scripts/train_taide_vision_projector.py --apply")
        sys.exit(0)

    if args.generate_data:
        generate_training_data(num_samples=args.num_samples, cooldown=args.cooldown)

    if args.train:
        train_projector(iters=args.iters, lr=args.lr)

    if args.apply:
        apply_projector()


if __name__ == "__main__":
    main()
