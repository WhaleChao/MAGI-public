#!/usr/bin/env python3
"""
train_taide_lora.py — TAIDE-12b LoRA 微調 / 合併 / 驗證

用法：
  python train_taide_lora.py --train     # LoRA 訓練
  python train_taide_lora.py --merge     # 合併 adapter 到 base model
  python train_taide_lora.py --validate  # 跑 eval 驗證
  python train_taide_lora.py --all       # 依序 train → merge → validate
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("train_taide_lora")

# ── 路徑 ──────────────────────────────────────────────────────────────
DISTILL_DIR = Path(os.environ.get(
    "TAIDE_DISTILL_DIR",
    str(Path.home() / ".omlx/training/taide-distill"),
))
BASE_MODEL = Path(os.environ.get(
    "TAIDE_BASE_MODEL",
    str(Path.home() / ".omlx/models/TAIDE-12b-Chat-mlx-4bit-textonly-backup"),
))
TRAIN_PATH = DISTILL_DIR / "train.jsonl"
EVAL_PATH = DISTILL_DIR / "eval.jsonl"
ADAPTERS_DIR = DISTILL_DIR / "adapters"
MERGED_DIR = DISTILL_DIR / "merged"
METRICS_PATH = DISTILL_DIR / "metrics.jsonl"
ACTIVE_MODEL_PATH = DISTILL_DIR / "active_model.json"

# ── LoRA 參數 ─────────────────────────────────────────────────────────
LORA_CONFIG = {
    "rank": 8,
    "alpha": 16,
    "dropout": 0.0,
    "target_modules": ["q_proj", "v_proj"],
}

TRAIN_CONFIG = {
    "batch_size": 1,
    "gradient_accumulation_steps": 4,
    "learning_rate": 1e-5,
    "max_steps": 200,
    "max_seq_length": 1024,   # 從 2048 降到 1024 省記憶體（12B 在 24GB 上）
    "save_every": 50,
    "eval_every": 50,
    "warmup_steps": 10,
}

# ── 結構檢查關鍵字 ────────────────────────────────────────────────────
STRUCTURE_HEADERS = ["實務見解", "法院見解", "適用法條", "法院認為", "應解為"]
MIN_ROUGE1_F1 = 0.3


def _version_tag() -> str:
    """自動產生版本號 distill-vNNN。"""
    existing = sorted(ADAPTERS_DIR.glob("adapter_*")) if ADAPTERS_DIR.exists() else []
    return f"distill-v{len(existing) + 1:03d}"


def _adapter_dir(version: str) -> Path:
    return ADAPTERS_DIR / f"adapter_{version.replace('distill-', '')}"


def _merged_dir(version: str) -> Path:
    return MERGED_DIR / f"TAIDE-{version}"


# ── 訓練 ──────────────────────────────────────────────────────────────
def do_train(version: str) -> dict:
    """執行 LoRA 訓練，回傳 metrics dict。"""
    try:
        from mlx_lm import lora as mlx_lora
    except ImportError:
        logger.error("mlx-lm not installed. Run: pip install mlx-lm")
        return {"error": "mlx-lm not installed"}

    if not TRAIN_PATH.exists():
        logger.error("train.jsonl not found at %s", TRAIN_PATH)
        return {"error": "train.jsonl not found"}

    train_count = sum(1 for l in open(TRAIN_PATH) if l.strip())
    if train_count < 10:
        logger.error("Only %d training samples, need at least 10", train_count)
        return {"error": f"insufficient data ({train_count})"}

    adapter_path = _adapter_dir(version)
    adapter_path.mkdir(parents=True, exist_ok=True)

    # 寫 LoRA config
    lora_cfg_path = adapter_path / "lora_config.json"
    lora_cfg_path.write_text(json.dumps(LORA_CONFIG, indent=2), "utf-8")

    logger.info("Starting LoRA training: %s (%d samples)", version, train_count)
    t0 = time.time()

    try:
        # 寫 LoRA YAML config（mlx-lm 用 -c 設定 lora_parameters）
        lora_yaml_path = adapter_path / "lora_params.yaml"
        import yaml
        lora_scale = 2.0 * LORA_CONFIG["alpha"] / LORA_CONFIG["rank"]  # alpha/rank * 2
        lora_yaml_path.write_text(yaml.dump({
            "lora_parameters": {
                "rank": LORA_CONFIG["rank"],
                "dropout": LORA_CONFIG["dropout"],
                "scale": lora_scale,
            },
        }), "utf-8")

        lora_args = [
            "--model", str(BASE_MODEL),
            "--train",
            "--fine-tune-type", "lora",
            "--data", str(DISTILL_DIR),
            "--adapter-path", str(adapter_path),
            "--num-layers", "16",  # 只微調最後 16 層，省記憶體
            "--batch-size", str(TRAIN_CONFIG["batch_size"]),
            "--iters", str(TRAIN_CONFIG["max_steps"]),
            "--steps-per-eval", str(TRAIN_CONFIG["eval_every"]),
            "--save-every", str(TRAIN_CONFIG["save_every"]),
            "--learning-rate", str(TRAIN_CONFIG["learning_rate"]),
            "--max-seq-length", str(TRAIN_CONFIG["max_seq_length"]),
            "--grad-checkpoint",
            "--grad-accumulation-steps", str(TRAIN_CONFIG["gradient_accumulation_steps"]),
            "-c", str(lora_yaml_path),
        ]

        # mlx_lm.lora 的 main 函式
        old_argv = sys.argv
        sys.argv = ["mlx_lm.lora"] + lora_args
        try:
            mlx_lora.main()
        finally:
            sys.argv = old_argv

    except Exception as e:
        logger.error("Training failed: %s", e)
        return {"error": str(e)}

    elapsed = time.time() - t0
    logger.info("Training complete in %.0fs", elapsed)

    # 讀取 adapter 訓練 loss（如有）
    train_loss = None
    eval_loss = None
    loss_log = adapter_path / "training_log.jsonl"
    if loss_log.exists():
        try:
            lines = [json.loads(l) for l in open(loss_log) if l.strip()]
            if lines:
                train_loss = lines[-1].get("train_loss")
                eval_loss = lines[-1].get("eval_loss")
        except Exception:
            pass

    return {
        "version": version,
        "adapter_path": str(adapter_path),
        "train_samples": train_count,
        "training_time_sec": round(elapsed),
        "train_loss": train_loss,
        "eval_loss": eval_loss,
    }


# ── 合併 ──────────────────────────────────────────────────────────────
def do_merge(version: str) -> dict:
    """合併 LoRA adapter 到 base model。"""
    try:
        from mlx_lm import fuse as mlx_fuse
    except ImportError:
        logger.error("mlx-lm not installed")
        return {"error": "mlx-lm not installed"}

    adapter_path = _adapter_dir(version)
    if not adapter_path.exists():
        return {"error": f"adapter not found: {adapter_path}"}

    output_path = _merged_dir(version)
    output_path.mkdir(parents=True, exist_ok=True)

    logger.info("Merging adapter %s → %s", adapter_path, output_path)
    t0 = time.time()

    try:
        old_argv = sys.argv
        sys.argv = [
            "mlx_lm.fuse",
            "--model", str(BASE_MODEL),
            "--adapter-path", str(adapter_path),
            "--save-path", str(output_path),
        ]
        try:
            mlx_fuse.main()
        finally:
            sys.argv = old_argv
    except Exception as e:
        logger.error("Merge failed: %s", e)
        return {"error": str(e)}

    elapsed = time.time() - t0
    logger.info("Merge complete in %.0fs", elapsed)

    # 驗證 output 有 config.json + weights
    if not (output_path / "config.json").exists():
        return {"error": "merged model missing config.json"}

    return {
        "version": version,
        "merged_path": str(output_path),
        "merge_time_sec": round(elapsed),
    }


# ── 驗證 ──────────────────────────────────────────────────────────────
def do_validate(version: str, num_samples: int = 5) -> dict:
    """跑 eval 樣本驗證合併模型品質。"""
    try:
        from mlx_lm import load, generate
    except ImportError:
        return {"error": "mlx-lm not installed"}

    merged_path = _merged_dir(version)
    if not merged_path.exists():
        return {"error": f"merged model not found: {merged_path}"}

    if not EVAL_PATH.exists():
        return {"error": "eval.jsonl not found"}

    # 載入 eval 樣本
    eval_samples = []
    with open(EVAL_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                messages = rec.get("messages", [])
                user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
                ref_msg = next((m["content"] for m in messages if m["role"] == "assistant"), "")
                if user_msg and ref_msg:
                    eval_samples.append({"prompt": user_msg, "reference": ref_msg})
            except Exception:
                continue
    if not eval_samples:
        return {"error": "no valid eval samples"}

    eval_samples = eval_samples[:num_samples]

    logger.info("Validating %s with %d samples", version, len(eval_samples))

    # 載入模型
    try:
        model, tokenizer = load(str(merged_path))
    except Exception as e:
        return {"error": f"model load failed: {e}"}

    results = []
    for i, sample in enumerate(eval_samples):
        try:
            prompt = sample["prompt"]
            # 使用 chat template 如果可用
            if hasattr(tokenizer, "apply_chat_template"):
                formatted = tokenizer.apply_chat_template(
                    [
                        {"role": "system", "content": "你是資深法律研究助理，專精司法見解分析。"},
                        {"role": "user", "content": prompt},
                    ],
                    tokenize=False,
                    add_generation_prompt=True,
                )
            else:
                formatted = prompt

            output = generate(
                model, tokenizer, prompt=formatted,
                max_tokens=1024, temp=0.1,
            )

            # 基本品質檢查
            is_gibberish = len(output.strip()) < 20
            has_structure = sum(1 for h in STRUCTURE_HEADERS if h in output) >= 2
            reasonable_length = 50 < len(output) < 5000

            results.append({
                "sample": i + 1,
                "output_len": len(output),
                "is_gibberish": is_gibberish,
                "has_structure": has_structure,
                "reasonable_length": reasonable_length,
                "pass": not is_gibberish and has_structure and reasonable_length,
            })
            logger.info(
                "  Sample %d: len=%d structure=%s pass=%s",
                i + 1, len(output), has_structure, results[-1]["pass"],
            )
        except Exception as e:
            results.append({"sample": i + 1, "error": str(e), "pass": False})
            logger.warning("  Sample %d failed: %s", i + 1, e)

    pass_count = sum(1 for r in results if r.get("pass"))
    pass_rate = pass_count / len(results) if results else 0

    # ROUGE-1 計算（簡易版：unigram overlap）
    rouge_scores = []
    for i, sample in enumerate(eval_samples):
        if i < len(results) and results[i].get("pass"):
            ref_chars = set(sample["reference"])
            try:
                formatted = tokenizer.apply_chat_template(
                    [
                        {"role": "system", "content": "你是資深法律研究助理，專精司法見解分析。"},
                        {"role": "user", "content": sample["prompt"]},
                    ],
                    tokenize=False,
                    add_generation_prompt=True,
                ) if hasattr(tokenizer, "apply_chat_template") else sample["prompt"]
                out = generate(model, tokenizer, prompt=formatted, max_tokens=1024, temp=0.1)
                out_chars = set(out)
                if ref_chars and out_chars:
                    overlap = len(ref_chars & out_chars)
                    p = overlap / len(out_chars)
                    r = overlap / len(ref_chars)
                    f1 = 2 * p * r / (p + r) if (p + r) > 0 else 0
                    rouge_scores.append(f1)
            except Exception:
                pass

    avg_rouge1 = sum(rouge_scores) / len(rouge_scores) if rouge_scores else 0

    # 釋放模型記憶體
    del model, tokenizer

    validation_pass = pass_rate >= 0.6 and avg_rouge1 >= MIN_ROUGE1_F1
    logger.info(
        "Validation: %d/%d passed, ROUGE-1=%.3f → %s",
        pass_count, len(results), avg_rouge1,
        "PASS" if validation_pass else "FAIL",
    )

    return {
        "version": version,
        "samples": len(results),
        "passed": pass_count,
        "pass_rate": round(pass_rate, 3),
        "rouge1_f1": round(avg_rouge1, 3),
        "validation_pass": validation_pass,
        "details": results,
    }


# ── 指標紀錄 ──────────────────────────────────────────────────────────
def record_metrics(train_result: dict, validate_result: dict, deployed: bool) -> None:
    """寫入 metrics.jsonl。"""
    entry = {
        "date": time.strftime("%Y-%m-%d"),
        "version": train_result.get("version", "?"),
        "train_loss": train_result.get("train_loss"),
        "eval_loss": train_result.get("eval_loss"),
        "rouge1_f1": validate_result.get("rouge1_f1"),
        "train_pairs": train_result.get("train_samples"),
        "training_time_sec": train_result.get("training_time_sec"),
        "pass_rate": validate_result.get("pass_rate"),
        "deployed": deployed,
    }
    with open(METRICS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    logger.info("Metrics recorded: %s", json.dumps(entry, ensure_ascii=False))


# ── CLI ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="TAIDE-12b LoRA 訓練管線")
    parser.add_argument("--train", action="store_true", help="執行 LoRA 訓練")
    parser.add_argument("--merge", action="store_true", help="合併 adapter 到 base model")
    parser.add_argument("--validate", action="store_true", help="驗證合併模型")
    parser.add_argument("--all", action="store_true", help="依序 train → merge → validate")
    parser.add_argument("--version", type=str, default="", help="版本號（預設自動產生）")
    parser.add_argument("--validate-samples", type=int, default=5, help="驗證樣本數")
    args = parser.parse_args()

    if not any([args.train, args.merge, args.validate, args.all]):
        parser.print_help()
        sys.exit(1)

    version = args.version or _version_tag()
    logger.info("Version: %s", version)

    if args.all:
        args.train = args.merge = args.validate = True

    results = {}

    if args.train:
        results["train"] = do_train(version)
        if results["train"].get("error"):
            logger.error("Training failed, aborting: %s", results["train"]["error"])
            print(json.dumps(results, ensure_ascii=False, indent=2))
            sys.exit(1)

    if args.merge:
        results["merge"] = do_merge(version)
        if results["merge"].get("error"):
            logger.error("Merge failed, aborting: %s", results["merge"]["error"])
            print(json.dumps(results, ensure_ascii=False, indent=2))
            sys.exit(1)

    if args.validate:
        results["validate"] = do_validate(version, args.validate_samples)
        if args.train:
            record_metrics(
                results.get("train", {}),
                results.get("validate", {}),
                deployed=False,
            )

    print(json.dumps(results, ensure_ascii=False, indent=2))

    if results.get("validate", {}).get("validation_pass"):
        sys.exit(0)
    elif args.validate and not results.get("validate", {}).get("validation_pass"):
        logger.warning("Validation did not pass — model will NOT be deployed")
        sys.exit(2)


if __name__ == "__main__":
    main()
