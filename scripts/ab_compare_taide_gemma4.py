#!/usr/bin/env python3
from __future__ import annotations

import gc
import json
import re
import sys
import time
from pathlib import Path

from mlx_vlm import generate, load

OUT_JSON = Path("/Users/ai/Desktop/MAGI_gemma4_vs_taide_ab_2026-04-03.json")

CASES = [
    {
        "id": "exact_ok",
        "messages": [{"role": "user", "content": "Respond with exactly: OK"}],
        "max_tokens": 8,
        "judge": "exact",
        "expected": "OK",
    },
    {
        "id": "capital_tw",
        "messages": [{"role": "user", "content": "請只回答一個城市名：台灣的首都是哪裡？"}],
        "max_tokens": 8,
        "judge": "contains_any",
        "expected_any": ["台北", "臺北"],
    },
    {
        "id": "tw_law_age",
        "messages": [{"role": "user", "content": "依目前台灣法律，成年人幾歲？請只回答阿拉伯數字。"}],
        "max_tokens": 8,
        "judge": "exact",
        "expected": "18",
    },
    {
        "id": "math_prob",
        "messages": [{"role": "user", "content": "袋中有3顆紅球和5顆藍球，不放回抽2顆，兩顆都紅球的機率是多少？請只回答分數。"}],
        "max_tokens": 16,
        "judge": "contains",
        "expected": "3/28",
    },
    {
        "id": "json_extract",
        "messages": [
            {"role": "system", "content": "你是資料抽取器。請只輸出 JSON，不要有其他文字。"},
            {
                "role": "user",
                "content": "委託人王小明，案號112年度訴字第1234號，對造張三，案由給付貨款，金額新台幣五十萬元。請輸出欄位 client, case_no, opponent, cause, amount_twd，且 amount_twd 盡量用數字。",
            },
        ],
        "max_tokens": 80,
        "judge": "json_fields",
        "fields": {
            "client": "王小明",
            "case_no": "112年度訴字第1234號",
            "opponent": "張三",
            "cause": "給付貨款",
        },
        "amount_keywords": ["500000", "五十萬", "五十萬元"],
    },
    {
        "id": "translate_tw",
        "messages": [
            {
                "role": "user",
                "content": '把 "The meeting is moved to Wednesday at 2 PM." 翻成自然繁體中文，只輸出譯文。',
            }
        ],
        "max_tokens": 32,
        "judge": "all_keywords",
        "keywords": ["會議"],
        "keyword_groups": [["週三", "星期三"], ["下午兩點", "2點", "兩點"]],
    },
    {
        "id": "intent_skill",
        "messages": [
            {
                "role": "system",
                "content": "你是意圖分類器。請只輸出以下其中一個標籤：CHAT, QUERY, COMMAND, SKILL, ADMIN。",
            },
            {"role": "user", "content": "幫我翻譯這份合約成英文"},
        ],
        "max_tokens": 4,
        "judge": "exact",
        "expected": "SKILL",
    },
    {
        "id": "legal_distinction",
        "messages": [{"role": "user", "content": "請用繁體中文、三點列出台灣法上假扣押與假處分的主要差異，總長不超過120字。"}],
        "max_tokens": 64,
        "judge": "all_keywords",
        "keywords": ["假扣押", "假處分"],
        "keyword_groups": [["金錢", "財產"], ["非金錢", "特定", "行為"]],
    },
]

MODELS = {
    "TAIDE-12b-Chat-mlx-4bit": "/Users/ai/.omlx/models/TAIDE-12b-Chat-mlx-4bit",
    "gemma-4-e2b-it-local-bf16": "/Users/ai/.omlx/models/gemma-4-e2b-it-local-bf16",
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _judge(case: dict, text: str) -> tuple[bool, str]:
    raw = text or ""
    normalized = _norm(raw)

    if case["judge"] == "exact":
        ok = normalized == case["expected"]
        return ok, f"expected={case['expected']} got={normalized}"

    if case["judge"] == "contains":
        ok = case["expected"] in normalized
        return ok, f"contains {case['expected']} -> {ok}"

    if case["judge"] == "contains_any":
        ok = any(item in normalized for item in case["expected_any"])
        return ok, f"contains_any {case['expected_any']} -> {ok}"

    if case["judge"] == "all_keywords":
        missing = [keyword for keyword in case.get("keywords", []) if keyword not in normalized]
        for group in case.get("keyword_groups", []):
            if not any(keyword in normalized for keyword in group):
                missing.append("/".join(group))
        ok = not missing
        return ok, "all keywords present" if ok else f"missing={','.join(missing)}"

    if case["judge"] == "json_fields":
        try:
            data = json.loads(raw)
        except Exception as exc:
            return False, f"json parse failed: {exc}"

        for key, expected in case["fields"].items():
            actual = str(data.get(key, "")).strip()
            if actual != expected:
                return False, f"field {key} expected {expected} got {actual}"

        amount = str(data.get("amount_twd", "")).strip()
        if not any(keyword in amount for keyword in case["amount_keywords"]):
            return False, f"amount_twd unexpected: {amount}"

        return True, "json fields ok"

    return False, "unknown judge"


def _build_prompt(processor, messages: list[dict]) -> str:
    tokenizer = getattr(processor, "tokenizer", processor)
    return tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    results = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "models": {},
    }

    for model_name, model_path in MODELS.items():
        print(f"=== loading {model_name}")
        load_t0 = time.time()
        model, processor = load(model_path)
        load_sec = round(time.time() - load_t0, 2)

        rows = []
        passed = 0
        total_gen_sec = 0.0
        peak_mem_max = 0.0

        for case in CASES:
            prompt = _build_prompt(processor, case["messages"])
            gen_t0 = time.time()
            out = generate(
                model,
                processor,
                prompt=prompt,
                max_tokens=case["max_tokens"],
                temperature=0.0,
                verbose=False,
            )
            gen_sec = round(time.time() - gen_t0, 2)
            total_gen_sec += gen_sec

            text = getattr(out, "text", "")
            peak_mem = float(getattr(out, "peak_memory", 0.0) or 0.0)
            peak_mem_max = max(peak_mem_max, peak_mem)
            ok, note = _judge(case, text)
            if ok:
                passed += 1

            row = {
                "case_id": case["id"],
                "ok": ok,
                "note": note,
                "text": text,
                "gen_sec": gen_sec,
                "prompt_tokens": int(getattr(out, "prompt_tokens", 0) or 0),
                "generation_tokens": int(getattr(out, "generation_tokens", 0) or 0),
                "generation_tps": round(float(getattr(out, "generation_tps", 0.0) or 0.0), 2),
                "peak_memory_gb": round(peak_mem, 3),
            }
            rows.append(row)
            print(model_name, case["id"], "PASS" if ok else "FAIL", note, "sec=", gen_sec)

        results["models"][model_name] = {
            "load_sec": load_sec,
            "pass_count": passed,
            "case_count": len(CASES),
            "avg_gen_sec": round(total_gen_sec / len(CASES), 2),
            "max_peak_memory_gb": round(peak_mem_max, 3),
            "rows": rows,
        }

        del model, processor
        gc.collect()

    OUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"WROTE {OUT_JSON}")
    compact = {
        model_name: {
            "load_sec": data["load_sec"],
            "pass_count": data["pass_count"],
            "case_count": data["case_count"],
            "avg_gen_sec": data["avg_gen_sec"],
            "max_peak_memory_gb": data["max_peak_memory_gb"],
        }
        for model_name, data in results["models"].items()
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
