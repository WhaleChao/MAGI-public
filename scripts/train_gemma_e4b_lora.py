#!/usr/bin/env python3
"""
train_gemma_e4b_lora.py — Gemma E4B LoRA 微調 / 合併 / 驗證

仿 train_taide_lora.py，但針對 Gemma 4 E4B 模型。

用法：
  python train_gemma_e4b_lora.py --train     # LoRA 訓練
  python train_gemma_e4b_lora.py --merge     # 合併 adapter 到 base model
  python train_gemma_e4b_lora.py --validate  # 跑 eval 驗證
  python train_gemma_e4b_lora.py --all       # 依序 train → merge → validate

模型資訊：
  - base: ~/.omlx/models/gemma-4-e4b-it-4bit
  - model_type: gemma4（mlx-lm 已支援）
  - 訓練視窗：E4B 日間 07:00-21:50

禁用中國大陸模型：Qwen / DeepSeek / GLM / Yi / Baichuan 等有內容審查，
法律工作不可接受。本腳本只使用 Gemma（Google，無審查）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("train_gemma_e4b_lora")

# ── 路徑 ──────────────────────────────────────────────────────────────
DISTILL_DIR = Path(os.environ.get(
    "GEMMA_DISTILL_DIR",
    str(Path.home() / ".omlx/training/gemma-distill"),
))
BASE_MODEL = Path(os.environ.get(
    "GEMMA_E4B_BASE_MODEL",
    str(Path.home() / ".omlx/models/gemma-4-e4b-it-4bit"),
))
TRAIN_PATH = DISTILL_DIR / "train.jsonl"
EVAL_PATH = DISTILL_DIR / "eval.jsonl"
ADAPTERS_DIR = DISTILL_DIR / "adapters"
MERGED_DIR = DISTILL_DIR / "merged"
METRICS_PATH = DISTILL_DIR / "metrics.jsonl"
ACTIVE_MODEL_PATH = DISTILL_DIR / "active_model.json"

# ── LoRA 參數（Gemma E4B）─────────────────────────────────────────────
LORA_CONFIG = {
    "rank": 8,
    "alpha": 16,
    "dropout": 0.0,
    "target_modules": ["q_proj", "v_proj"],  # mlx-lm 對 Gemma 預設支援
}

TRAIN_CONFIG = {
    "batch_size": 1,             # E4B 比 12B 小，但保守起步
    "gradient_accumulation_steps": 4,
    "learning_rate": 1e-5,
    "max_steps": 200,
    "max_seq_length": 1024,      # 省記憶體
    "save_every": 50,
    "eval_every": 50,
    "warmup_steps": 10,
}

# ── 結構檢查關鍵字 ────────────────────────────────────────────────────
STRUCTURE_HEADERS = ["實務見解", "法院見解", "適用法條", "法院認為", "應解為"]
MIN_ROUGE1_F1 = 0.3
MIN_OUTPUT_CHARS = 48
MIN_CJK_CHARS = 24
MIN_CJK_RATIO = 0.45
MAX_ASCII_ALPHA_RATIO = 0.35

# 規則式繁簡檢查：只放「繁體不共碼位」的常見簡體字，避免誤判。
_SC_LEGAL_CHARS = frozenset(
    "损权责证诉处规进对认时问说来过义务类协签举书审长会还为从发开关应现给让边单实续区动结请们违约赔据议订讨论题"
)
_CHANNEL_MARKER_PATTERNS = (
    re.compile(r"<\|channel\>\s*thought", re.IGNORECASE),
    re.compile(r"<\|channel\>", re.IGNORECASE),
)
_EN_THINKING_TRACE = re.compile(
    r"(?i)\b("
    r"let'?s think|"
    r"chain\s+of\s+thought|"
    r"thought\s+process|"
    r"internal\s+monologue|"
    r"my\s+reasoning|"
    r"i\s+will\s+reason|"
    r"step\s+by\s+step|"
    r"reasoning\s*:|"
    r"analysis\s*:"
    r")\b"
)
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_ASCII_ALPHA_RE = re.compile(r"[A-Za-z]")

# ── mlx-lm Python（使用 oMLX 內建 Python 3.11）──────────────────────
OMLX_PYTHON = "/opt/homebrew/opt/omlx/libexec/bin/python3.11"


def _version_tag() -> str:
    """自動產生版本號 gemma-distill-vNNN。"""
    existing = sorted(ADAPTERS_DIR.glob("adapter_gemma-*")) if ADAPTERS_DIR.exists() else []
    return f"gemma-distill-v{len(existing) + 1:03d}"


def _adapter_dir(version: str) -> Path:
    return ADAPTERS_DIR / f"adapter_{version}"


def _merged_dir(version: str) -> Path:
    return MERGED_DIR / f"Gemma-{version}"


def _check_simplified_chinese(text: str) -> list[str]:
    found = [c for c in text if c in _SC_LEGAL_CHARS]
    seen: set[str] = set()
    unique_found = []
    for c in found:
        if c not in seen:
            seen.add(c)
            unique_found.append(c)
    return unique_found


def _build_validation_messages(prompt: str) -> list[dict[str, str]]:
    """Validation 專用提示，加入 final-channel suppression（僅訓練/驗證流程使用）。"""
    return [
        {
            "role": "system",
            "content": (
                "你是台灣法律助理。只輸出最終答案，不要輸出思考過程。"
                "禁止輸出任何 channel marker（例如 <|channel>thought、<|channel>）。"
                "請使用繁體中文，內容需具體完整。/no_think"
            ),
        },
        {"role": "user", "content": prompt},
    ]


def _validate_output_gate(output: str) -> tuple[bool, list[str], dict]:
    text = (output or "").strip()
    reasons: list[str] = []
    stats = {
        "length": len(text),
        "cjk_chars": 0,
        "cjk_ratio": 0.0,
        "ascii_alpha_ratio": 0.0,
        "simplified_chars": [],
    }

    if not text:
        reasons.append("empty_output")
        return False, reasons, stats

    for pattern in _CHANNEL_MARKER_PATTERNS:
        if pattern.search(text):
            reasons.append("channel_marker_leak")
            break

    if _EN_THINKING_TRACE.search(text):
        reasons.append("english_thinking_trace")

    if len(text) < MIN_OUTPUT_CHARS:
        reasons.append("too_short")

    cjk_chars = len(_CJK_RE.findall(text))
    ascii_alpha = len(_ASCII_ALPHA_RE.findall(text))
    total_len = max(len(text), 1)
    cjk_ratio = cjk_chars / total_len
    ascii_alpha_ratio = ascii_alpha / total_len

    stats["cjk_chars"] = cjk_chars
    stats["cjk_ratio"] = round(cjk_ratio, 3)
    stats["ascii_alpha_ratio"] = round(ascii_alpha_ratio, 3)

    if cjk_chars < MIN_CJK_CHARS or cjk_ratio < MIN_CJK_RATIO:
        reasons.append("insufficient_traditional_chinese")
    if ascii_alpha_ratio > MAX_ASCII_ALPHA_RATIO:
        reasons.append("too_much_english")

    simplified_chars = _check_simplified_chinese(text)
    stats["simplified_chars"] = simplified_chars[:8]
    if simplified_chars:
        reasons.append("simplified_chinese_detected")

    return len(reasons) == 0, reasons, stats


def train(version: str) -> dict:
    """執行 LoRA 訓練。"""
    adapter_dir = _adapter_dir(version)
    adapter_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Starting LoRA training: version=%s", version)
    logger.info("  base_model: %s", BASE_MODEL)
    logger.info("  train_path: %s", TRAIN_PATH)
    logger.info("  eval_path: %s", EVAL_PATH)
    logger.info("  adapter_dir: %s", adapter_dir)

    if not TRAIN_PATH.exists():
        raise FileNotFoundError(f"Training data not found: {TRAIN_PATH}")
    if not BASE_MODEL.exists():
        raise FileNotFoundError(f"Base model not found: {BASE_MODEL}")

    # 新版 mlx-lm 把 LoRA 超參數搬到 YAML config（用 -c 載入），CLI 不再支援
    # --lora-rank/--lora-alpha/--lora-dropout/--target-modules/--warmup
    import yaml
    lora_yaml_path = adapter_dir / "lora_params.yaml"
    lora_scale = 2.0 * LORA_CONFIG["alpha"] / LORA_CONFIG["rank"]  # 沿用 train_taide_lora 慣例
    lora_yaml_path.write_text(yaml.dump({
        "lora_parameters": {
            "rank": LORA_CONFIG["rank"],
            "dropout": LORA_CONFIG["dropout"],
            "scale": lora_scale,
        },
    }), "utf-8")

    cmd = [
        OMLX_PYTHON, "-m", "mlx_lm.lora",
        "--model", str(BASE_MODEL),
        "--train",
        "--fine-tune-type", "lora",
        "--data", str(DISTILL_DIR),
        "--adapter-path", str(adapter_dir),
        "--num-layers", "16",  # 只微調最後 16 層省記憶體（沿用 TAIDE 設定）
        "--batch-size", str(TRAIN_CONFIG["batch_size"]),
        "--grad-checkpoint",
        "--grad-accumulation-steps", str(TRAIN_CONFIG["gradient_accumulation_steps"]),
        "--learning-rate", str(TRAIN_CONFIG["learning_rate"]),
        "--iters", str(TRAIN_CONFIG["max_steps"]),
        "--max-seq-length", str(TRAIN_CONFIG["max_seq_length"]),
        "--save-every", str(TRAIN_CONFIG["save_every"]),
        "--steps-per-eval", str(TRAIN_CONFIG["eval_every"]),
        "-c", str(lora_yaml_path),
    ]

    logger.info("Running: %s", " ".join(cmd))
    t0 = time.time()

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=3600,
        cwd=str(Path(__file__).parent.parent),
    )

    elapsed = time.time() - t0
    logger.info("LoRA training stdout:\n%s", result.stdout[-3000:])
    if result.stderr:
        logger.info("LoRA training stderr:\n%s", result.stderr[-1000:])

    success = result.returncode == 0
    return {
        "version": version,
        "success": success,
        "returncode": result.returncode,
        "elapsed_sec": int(elapsed),
        "adapter_dir": str(adapter_dir),
    }


def merge(version: str) -> dict:
    """合併 LoRA adapter 到 base model。"""
    adapter_dir = _adapter_dir(version)
    merged_path = _merged_dir(version)
    merged_path.mkdir(parents=True, exist_ok=True)

    if not adapter_dir.exists():
        raise FileNotFoundError(f"Adapter dir not found: {adapter_dir}")

    logger.info("Merging adapter: version=%s", version)
    logger.info("  adapter_dir: %s", adapter_dir)
    logger.info("  merged_path: %s", merged_path)

    cmd = [
        OMLX_PYTHON, "-m", "mlx_lm.fuse",
        "--model", str(BASE_MODEL),
        "--adapter-path", str(adapter_dir),
        "--save-path", str(merged_path),
        "--dequantize",  # 新版 mlx_lm.fuse 改用 --dequantize（無連字號）
    ]

    logger.info("Running: %s", " ".join(cmd))
    t0 = time.time()

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=1800,
        cwd=str(Path(__file__).parent.parent),
    )

    elapsed = time.time() - t0
    logger.info("Merge stdout:\n%s", result.stdout[-1000:])
    if result.stderr:
        logger.info("Merge stderr:\n%s", result.stderr[-500:])

    success = result.returncode == 0 and (merged_path / "config.json").exists()
    return {
        "version": version,
        "success": success,
        "returncode": result.returncode,
        "elapsed_sec": int(elapsed),
        "merged_path": str(merged_path),
    }


def validate(version: str) -> dict:
    """跑 eval 驗證，確認合併模型能正確推理。"""
    merged_path = _merged_dir(version)
    if not merged_path.exists():
        raise FileNotFoundError(f"Merged model not found: {merged_path}")

    test_prompts = [
        "請用一句話說明何謂損害賠償。",
        "刑法第339條的構成要件是什麼？",
        "何謂善意第三人？",
    ]

    passed = 0
    details = []
    for i, prompt in enumerate(test_prompts):
        try:
            # 新版 mlx-lm: generate(model, tok, prompt, verbose, **kwargs) — temp 不再是位置/直接參數
            # Gemma 4 instruction-tuned 必須套 chat template，否則只會複讀
            cmd = [
                OMLX_PYTHON, "-c",
                f"""
import sys; sys.path.insert(0, '/opt/homebrew/opt/omlx/libexec/lib/python3.11/site-packages')
from mlx_lm import load, generate
model, tok = load({str(merged_path)!r})
msgs = { _build_validation_messages(prompt)!r }
p = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
out = generate(model, tok, p, max_tokens=128, verbose=False)
print(out[:400])
""",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            output = (result.stdout or "").strip()
            sample_ok, reasons, stats = _validate_output_gate(output)
            details.append({
                "sample": i + 1,
                "prompt": prompt,
                "pass": sample_ok,
                "reasons": reasons,
                "stats": stats,
                "output_preview": output[:160],
            })
            if sample_ok:
                passed += 1
                logger.info("  Validate %d/3: OK (%d chars)", i + 1, len(output))
            else:
                logger.warning(
                    "  Validate %d/3: FAIL reasons=%s stats=%s",
                    i + 1, ",".join(reasons), stats,
                )
        except Exception as e:
            logger.warning("  Validate %d/3: %s", i + 1, e)
            details.append({
                "sample": i + 1,
                "prompt": prompt,
                "pass": False,
                "reasons": ["exception"],
                "stats": {"error": str(e)},
                "output_preview": "",
            })

    validation_pass = passed >= 2
    result_dict = {
        "version": version,
        "success": validation_pass,
        "validation_pass": validation_pass,
        "passed": passed,
        "total": len(test_prompts),
        "pass_rate": round(passed / len(test_prompts), 3),
        "merged_path": str(merged_path),
        "details": details,
    }

    # 寫 metrics
    try:
        with open(METRICS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                **result_dict,
                "validated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("metrics write failed: %s", e)

    return result_dict


def main() -> int:
    parser = argparse.ArgumentParser(description="Gemma E4B LoRA 微調 / 合併 / 驗證")
    parser.add_argument("--train", action="store_true", help="LoRA 訓練")
    parser.add_argument("--merge", action="store_true", help="合併 adapter 到 base model")
    parser.add_argument("--validate", action="store_true", help="跑 eval 驗證")
    parser.add_argument("--all", action="store_true", help="依序 train → merge → validate")
    parser.add_argument("--version", default="", help="指定版本號（不指定則自動生成）")
    args = parser.parse_args()

    if not any([args.train, args.merge, args.validate, args.all]):
        parser.print_help()
        return 1

    DISTILL_DIR.mkdir(parents=True, exist_ok=True)
    ADAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    MERGED_DIR.mkdir(parents=True, exist_ok=True)

    version = args.version or _version_tag()
    logger.info("Gemma E4B 蒸餾訓練: version=%s", version)

    results = {}

    if args.train or args.all:
        r = train(version)
        results["train"] = r
        if not r["success"]:
            logger.error("Training failed: %s", r)
            print(json.dumps(results, ensure_ascii=False))
            return 1

    if args.merge or args.all:
        r = merge(version)
        results["merge"] = r
        if not r["success"]:
            logger.error("Merge failed: %s", r)
            print(json.dumps(results, ensure_ascii=False))
            return 1

    if args.validate or args.all:
        r = validate(version)
        results["validate"] = r
        if not r["success"]:
            logger.warning("Validation did not fully pass: %s", r)

    print(json.dumps(results, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
