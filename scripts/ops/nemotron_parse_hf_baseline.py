#!/usr/bin/env python3
"""
nemotron_parse_hf_baseline.py — Phase 1a single-sample HF baseline pipeline.

Usage:
    python3 nemotron_parse_hf_baseline.py \
        --pdf /path/to/doc.pdf \
        --page 0 \
        --output-json /tmp/nemotron_phase1a/sample01.json \
        --output-text /tmp/nemotron_phase1a/sample01.txt \
        --max-tokens 2048

Exit code 0 = success; non-0 = failure.
Partial JSON with `error` field is always written on failure.
"""

import argparse
import gc
import json
import os
import re
import resource
import sys
import time
from pathlib import Path

MODEL_PATH = "/Users/ai/.omlx/models-vision/nemotron-parse-v1.2-hf"
PROMPT_TOKEN_IDS = [2, 0, 50004, 50008, 50001, 50010]


def parse_args():
    p = argparse.ArgumentParser(description="NemotronParse HF baseline — single page")
    p.add_argument("--pdf", required=True, help="Path to input PDF")
    p.add_argument("--page", type=int, default=0, help="0-based page index (default: 0)")
    p.add_argument("--output-json", required=True, help="Output JSON path")
    p.add_argument("--output-text", required=True, help="Output raw text path")
    p.add_argument("--max-tokens", type=int, default=2048, help="max_new_tokens for generation")
    return p.parse_args()


def peak_rss_mb() -> float:
    """Return peak RSS in MB. macOS getrusage returns bytes."""
    rss_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS returns bytes; Linux returns KB
    if sys.platform == "darwin":
        return rss_bytes / 1024 / 1024
    else:
        return rss_bytes / 1024


def write_output(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def extract_blocks(raw_text: str) -> list:
    """
    Very simple block extractor: split by double-newline runs.
    Returns list of dicts with 'text' key.
    Phase 1b will do proper structured parsing.
    """
    chunks = re.split(r"\n{2,}", raw_text.strip())
    blocks = [{"text": c.strip()} for c in chunks if c.strip()]
    return blocks


def run(args) -> dict:
    result = {
        "engine": "nemotron-parse-v1.2-hf",
        "pdf": args.pdf,
        "page": args.page,
        "prompt_token_ids": PROMPT_TOKEN_IDS,
        "raw_text": "",
        "blocks": [],
        "duration_load_sec": 0.0,
        "duration_inference_sec": 0.0,
        "duration_total_sec": 0.0,
        "peak_rss_mb": 0,
        "device": "cpu",
        "torch_dtype": "bfloat16",
        "error": "",
    }

    t_start = time.monotonic()

    # --- Step 1: pdf2image ---
    try:
        from pdf2image import convert_from_path
    except ImportError as e:
        result["error"] = f"ImportError pdf2image: {e}. Install: pip install pdf2image; brew install poppler"
        return result

    print(f"[nemotron-baseline] Converting PDF page {args.page} ...", flush=True)
    try:
        images = convert_from_path(
            args.pdf,
            dpi=200,
            first_page=args.page + 1,
            last_page=args.page + 1,
        )
    except Exception as e:
        result["error"] = f"pdf2image failed: {type(e).__name__}: {e}"
        return result

    if not images:
        result["error"] = f"pdf2image returned 0 images for page {args.page}"
        return result

    pil_image = images[0]
    print(f"[nemotron-baseline] Image size: {pil_image.size}", flush=True)

    # --- Step 2: Load model ---
    print(f"[nemotron-baseline] Loading model from {MODEL_PATH} ...", flush=True)
    t_load_start = time.monotonic()

    try:
        import torch
        from transformers import AutoModel, AutoProcessor
    except ImportError as e:
        result["error"] = f"ImportError transformers/torch: {e}"
        return result

    try:
        model = AutoModel.from_pretrained(
            MODEL_PATH,
            torch_dtype=torch.bfloat16,
            device_map="cpu",
            trust_remote_code=True,
            low_cpu_mem_usage=True,
        )
        model.eval()
        processor = AutoProcessor.from_pretrained(
            MODEL_PATH,
            trust_remote_code=True,
        )
    except Exception as e:
        result["error"] = f"Model load failed: {type(e).__name__}: {e}"
        return result

    t_load_end = time.monotonic()
    result["duration_load_sec"] = round(t_load_end - t_load_start, 2)
    print(f"[nemotron-baseline] Model loaded in {result['duration_load_sec']:.1f}s", flush=True)
    print(f"[nemotron-baseline] RSS after load: {peak_rss_mb():.0f} MB", flush=True)

    # --- Step 3: Preprocess ---
    # Official usage: pass text prompt to processor, which tokenizes it into input_ids
    # These become decoder_input_ids via prepare_inputs_for_generation
    TASK_PROMPT = "</s><s><predict_bbox><predict_classes><output_markdown><predict_no_text_in_pic>"
    print(f"[nemotron-baseline] Preprocessing image with task prompt ...", flush=True)
    try:
        inputs = processor(
            images=[pil_image],
            text=TASK_PROMPT,
            return_tensors="pt",
            add_special_tokens=False,
        )
        # Verify pixel_values present
        if "pixel_values" not in inputs:
            raise ValueError(f"processor returned keys: {list(inputs.keys())} — no pixel_values")
        # Cast pixel_values to bfloat16
        inputs["pixel_values"] = inputs["pixel_values"].to(torch.bfloat16)
    except Exception as e:
        del model
        gc.collect()
        result["error"] = f"Preprocessing failed: {type(e).__name__}: {e}"
        return result

    print(f"[nemotron-baseline] Input keys: {list(inputs.keys())}", flush=True)
    if "input_ids" in inputs:
        print(f"[nemotron-baseline] input_ids: {inputs['input_ids'].tolist()}", flush=True)

    # --- Step 4: Inference ---
    print(f"[nemotron-baseline] Running inference (max_new_tokens={args.max_tokens}) ...", flush=True)
    t_inf_start = time.monotonic()

    try:
        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                do_sample=False,
                num_beams=1,
                max_new_tokens=args.max_tokens,
            )
    except Exception as e:
        del model
        gc.collect()
        result["error"] = f"Inference failed: {type(e).__name__}: {e}"
        return result

    t_inf_end = time.monotonic()
    result["duration_inference_sec"] = round(t_inf_end - t_inf_start, 2)
    print(f"[nemotron-baseline] Inference done in {result['duration_inference_sec']:.1f}s", flush=True)

    # --- Step 5: Decode ---
    try:
        # outputs shape: (1, seq_len)
        # Skip the prompt tokens (input_ids length) from the output
        prompt_len = inputs["input_ids"].shape[1] if "input_ids" in inputs else 0
        generated_ids = outputs[0][prompt_len:]
        raw_text = processor.tokenizer.decode(generated_ids, skip_special_tokens=True)
    except Exception as e:
        del model
        gc.collect()
        result["error"] = f"Decode failed: {type(e).__name__}: {e}"
        return result

    # --- Step 7: Cleanup ---
    del model
    gc.collect()

    t_end = time.monotonic()
    result["duration_total_sec"] = round(t_end - t_start, 2)
    result["peak_rss_mb"] = round(peak_rss_mb(), 1)
    result["raw_text"] = raw_text
    result["blocks"] = extract_blocks(raw_text)

    print(f"[nemotron-baseline] Peak RSS: {result['peak_rss_mb']:.0f} MB", flush=True)
    print(f"[nemotron-baseline] raw_text length: {len(raw_text)} chars", flush=True)
    print(f"[nemotron-baseline] blocks: {len(result['blocks'])}", flush=True)

    return result


def main():
    args = parse_args()

    # Ensure output dirs exist
    for p in [args.output_json, args.output_text]:
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)

    result = {}
    exit_code = 0

    try:
        result = run(args)
        if result.get("error"):
            print(f"[nemotron-baseline] ERROR: {result['error']}", file=sys.stderr)
            exit_code = 1
    except Exception as e:
        import traceback
        result["error"] = f"Unhandled exception: {type(e).__name__}: {e}\n{traceback.format_exc()}"
        print(f"[nemotron-baseline] FATAL: {result['error']}", file=sys.stderr)
        exit_code = 1

    # Always write JSON (partial on failure)
    write_output(args.output_json, result)
    print(f"[nemotron-baseline] JSON written to {args.output_json}", flush=True)

    # Write raw text
    raw_text = result.get("raw_text", "")
    with open(args.output_text, "w", encoding="utf-8") as f:
        f.write(raw_text)
    print(f"[nemotron-baseline] Text written to {args.output_text}", flush=True)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
