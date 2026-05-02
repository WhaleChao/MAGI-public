#!/usr/bin/env python3
"""
nemotron_phase1b_compare.py — 5 份繁中 PDF Nemotron Parse vs macOS Vision 對照。

只在 oMLX 4 個 server 已 bootout 的狀態下執行（避免記憶體爭用）。

執行：
    /Users/ai/Desktop/MAGI_v2/venv/bin/python3 \
        /Users/ai/Desktop/MAGI_v2/scripts/ops/nemotron_phase1b_compare.py

輸出：/tmp/nemotron_phase1b/
"""
from __future__ import annotations

import gc
import json
import os
import resource
import sys
import time
import traceback
from pathlib import Path

# 5 份樣本（label, pdf path, document_type）
SAMPLES = [
    ("sample01_judgment",
     "/Users/ai/Desktop/AGENT TEST DATA/判決/1499.pdf",
     "裁判書"),
    ("sample02_court_spec",
     "/Users/ai/Desktop/AGENT TEST DATA/判決/裁判書開放API規格說明(1140822版).pdf",
     "司法院規格說明"),
    ("sample03_passbook",
     "/Users/ai/Desktop/存摺.pdf",
     "低品質掃描"),
    ("sample04_report",
     "/Users/ai/Desktop/AGENT TEST DATA/判決/平等近用司法專案報告.pdf",
     "司法報告"),
    ("sample05_form",
     "/Users/ai/Desktop/0000-0000-範本-消費者債務清理/02_各種書狀/04_債務人清冊.pdf",
     "表單"),
]

MODEL_PATH = "/Users/ai/.omlx/models-vision/nemotron-parse-v1.2-hf"
OUT_DIR = Path("/tmp/nemotron_phase1b")
PROMPT = "</s><s><predict_bbox><predict_classes><output_markdown><predict_no_text_in_pic>"
PROMPT_IDS = [2, 0, 50004, 50008, 50001, 50010]
MAX_NEW_TOKENS = 1024


def rss_mb() -> float:
    b = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return b / 1024 / 1024 if sys.platform == "darwin" else b / 1024


def log(msg: str):
    print(f"[phase1b {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def render_page(pdf_path: str, page: int = 0):
    from pdf2image import convert_from_path
    images = convert_from_path(pdf_path, dpi=200, first_page=page + 1, last_page=page + 1)
    return images[0] if images else None


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 載入模型一次
    log("loading torch + transformers ...")
    import torch
    from transformers import AutoModel, AutoProcessor

    log(f"loading model from {MODEL_PATH} ...")
    t0 = time.monotonic()
    model = AutoModel.from_pretrained(
        MODEL_PATH,
        torch_dtype=torch.bfloat16,
        device_map="cpu",
        trust_remote_code=True,
        low_cpu_mem_usage=True,
    )
    model.eval()
    processor = AutoProcessor.from_pretrained(MODEL_PATH, trust_remote_code=True)
    load_dur = time.monotonic() - t0
    log(f"model loaded in {load_dur:.1f}s, RSS={rss_mb():.0f} MB")

    # Apple Vision
    sys.path.insert(0, "/Users/ai/Desktop/MAGI_v2")
    log("loading apple_vision_provider ...")
    from skills.engine.ocr import apple_vision_provider
    av_ok, av_reason = apple_vision_provider.check_available()
    log(f"apple_vision available={av_ok} reason={av_reason}")

    summary = []

    for label, pdf_path, doc_type in SAMPLES:
        rec = {
            "label": label,
            "pdf": pdf_path,
            "doc_type": doc_type,
            "exists": os.path.isfile(pdf_path),
            "nemotron": {"success": False, "raw_text": "", "duration_sec": 0.0, "error": ""},
            "apple_vision": {"success": False, "raw_text": "", "duration_sec": 0.0, "quality": 0.0, "error": ""},
        }
        log(f"=== {label}: {Path(pdf_path).name} ===")

        if not rec["exists"]:
            rec["nemotron"]["error"] = "pdf not found"
            rec["apple_vision"]["error"] = "pdf not found"
            summary.append(rec)
            continue

        # render page 0
        try:
            t1 = time.monotonic()
            pil_img = render_page(pdf_path, page=0)
            log(f"  rendered page 0 in {time.monotonic()-t1:.1f}s, size={pil_img.size}")
        except Exception as e:
            rec["nemotron"]["error"] = f"render_page: {type(e).__name__}: {e}"
            rec["apple_vision"]["error"] = rec["nemotron"]["error"]
            summary.append(rec)
            continue

        # save PNG for Vision
        png_path = OUT_DIR / f"{label}.png"
        pil_img.save(png_path, "PNG")

        # ---- Nemotron ----
        try:
            log("  Nemotron: preprocessing ...")
            inputs = processor(
                images=[pil_img],
                text=PROMPT,
                return_tensors="pt",
                add_special_tokens=False,
            )
            inputs["pixel_values"] = inputs["pixel_values"].to(torch.bfloat16)
            log(f"  Nemotron: generating max_new_tokens={MAX_NEW_TOKENS} ...")
            t2 = time.monotonic()
            with torch.no_grad():
                outputs = model.generate(
                    **inputs,
                    do_sample=False,
                    num_beams=1,
                    max_new_tokens=MAX_NEW_TOKENS,
                )
            inf_dur = time.monotonic() - t2
            raw = processor.batch_decode(outputs, skip_special_tokens=False)[0]
            rec["nemotron"]["success"] = True
            rec["nemotron"]["raw_text"] = raw
            rec["nemotron"]["duration_sec"] = round(inf_dur, 2)
            rec["nemotron"]["text_length"] = len(raw)
            rec["nemotron"]["has_chinese"] = bool(__import__("re").search(r"[一-鿿]", raw))
            log(f"  Nemotron: done in {inf_dur:.1f}s, len={len(raw)}, chinese={rec['nemotron']['has_chinese']}")
        except Exception as e:
            rec["nemotron"]["error"] = f"{type(e).__name__}: {e}"
            log(f"  Nemotron FAIL: {rec['nemotron']['error']}")
            log(traceback.format_exc())

        # cleanup tensors between samples
        try:
            del inputs, outputs
        except Exception:
            pass
        gc.collect()

        # ---- Apple Vision ----
        try:
            log("  Vision: running ...")
            r = apple_vision_provider.run(str(png_path), task_type="legal", timeout_sec=30.0)
            rec["apple_vision"]["success"] = bool(r.success)
            rec["apple_vision"]["raw_text"] = r.raw_text or ""
            rec["apple_vision"]["duration_sec"] = round(r.duration_sec, 2) if r.duration_sec else 0.0
            rec["apple_vision"]["quality"] = r.quality_score or 0.0
            rec["apple_vision"]["text_length"] = len(rec["apple_vision"]["raw_text"])
            rec["apple_vision"]["has_chinese"] = bool(__import__("re").search(r"[一-鿿]", rec["apple_vision"]["raw_text"]))
            if not r.success:
                rec["apple_vision"]["error"] = getattr(r, "error", "") or ""
            log(f"  Vision: success={r.success}, len={rec['apple_vision']['text_length']}, q={rec['apple_vision']['quality']:.2f}")
        except Exception as e:
            rec["apple_vision"]["error"] = f"{type(e).__name__}: {e}"
            log(f"  Vision FAIL: {rec['apple_vision']['error']}")

        # 寫單樣本 JSON
        sample_json = OUT_DIR / f"{label}.json"
        with open(sample_json, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        # 寫 raw text 給人看
        (OUT_DIR / f"{label}_nemotron.txt").write_text(rec["nemotron"]["raw_text"], encoding="utf-8")
        (OUT_DIR / f"{label}_vision.txt").write_text(rec["apple_vision"]["raw_text"], encoding="utf-8")

        summary.append(rec)
        log(f"  RSS now: {rss_mb():.0f} MB")
        gc.collect()

    # 總表
    summary_path = OUT_DIR / "summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump({"samples": summary, "model_load_sec": round(load_dur, 2)}, f, ensure_ascii=False, indent=2)
    log(f"summary written: {summary_path}")

    # console table
    print()
    print(f"{'label':<24} {'doc_type':<14} {'NemoOK':<7} {'NemoLen':<8} {'NemoSec':<8} {'VisOK':<6} {'VisLen':<8} {'VisQ':<5}")
    print("-" * 90)
    for r in summary:
        n = r["nemotron"]
        v = r["apple_vision"]
        print(f"{r['label']:<24} {r['doc_type']:<14} {str(n['success']):<7} {n.get('text_length', 0):<8} {n.get('duration_sec', 0):<8} {str(v['success']):<6} {v.get('text_length', 0):<8} {v.get('quality', 0):.2f}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"FATAL: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        sys.exit(1)
