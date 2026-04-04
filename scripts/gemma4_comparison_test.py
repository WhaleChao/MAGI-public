#!/usr/bin/env python3
"""
Gemma 4 26B vs TAIDE-12b 對比測試
==================================
透過 oMLX API 同時測試兩個模型在 MAGI 關鍵場景的表現。
用法: python3 scripts/gemma4_comparison_test.py
"""

import json
import time
import requests
import sys

OMLX_BASE = "http://127.0.0.1:8080"
CHAT_URL = f"{OMLX_BASE}/v1/chat/completions"

MODEL_A = "TAIDE-12b-Chat-mlx-4bit"
MODEL_B = "gemma-4-26b-a4b-it-4bit"

# ── 測試用例 ──────────────────────────────────────────────────
TEST_CASES = [
    {
        "name": "繁中日常對話",
        "messages": [
            {"role": "user", "content": "請用繁體中文幫我寫一封正式的電子郵件，向客戶說明下週的會議時間改到週三下午兩點。"}
        ],
        "criteria": "使用正確繁體中文、語氣正式得體、格式完整"
    },
    {
        "name": "台灣法律術語",
        "messages": [
            {"role": "user", "content": "請解釋台灣民事訴訟法中「假扣押」與「假處分」的差異，並說明聲請要件。"}
        ],
        "criteria": "法律術語正確、引用條文正確、使用台灣法律體系而非中國大陸"
    },
    {
        "name": "文件摘要",
        "messages": [
            {"role": "system", "content": "你是 MAGI 系統的文件分析引擎。請將使用者提供的文字做重點摘要。"},
            {"role": "user", "content": """請摘要以下內容：
人工智慧在法律領域的應用日益廣泛。首先，在法律文件的審查方面，AI 可以快速分析合約條款，找出潛在風險。其次，在案件預測方面，透過機器學習模型分析歷史判決，可以預測案件結果的可能走向。第三，在法律研究方面，自然語言處理技術能夠幫助律師更快速地從大量判例和法規中找到相關資料。然而，AI 在法律領域的應用也面臨挑戰，包括數據隱私問題、算法偏見、以及法律責任歸屬等議題。此外，法律專業人士對 AI 工具的信任度也需要時間建立。"""}
        ],
        "criteria": "摘要精準、不遺漏重點、不產生幻覺"
    },
    {
        "name": "意圖分類",
        "messages": [
            {"role": "system", "content": "你是一個意圖分類器。請將使用者輸入分類為以下類別之一：CHAT, QUERY, COMMAND, SKILL, ADMIN。只輸出類別名稱。"},
            {"role": "user", "content": "幫我翻譯這份合約成英文"}
        ],
        "criteria": "正確分類為 SKILL 或 COMMAND"
    },
    {
        "name": "結構化輸出 (JSON)",
        "messages": [
            {"role": "system", "content": "你是一個資料提取引擎。請從使用者的描述中提取出結構化的案件資訊，以 JSON 格式輸出。"},
            {"role": "user", "content": "委託人王小明，案號 112年度訴字第1234號，對造為張三，案由是給付貨款，請求金額新台幣五十萬元。"}
        ],
        "criteria": "輸出有效 JSON、提取正確、欄位完整"
    },
    {
        "name": "複雜推理",
        "messages": [
            {"role": "user", "content": "一個袋子裡有3顆紅球和5顆藍球。不放回地依次抽取2顆球，請問兩顆都是紅球的機率是多少？請用繁體中文詳細解釋推理過程。"}
        ],
        "criteria": "數學推理正確（答案應為 3/28）、步驟清晰、使用繁體中文"
    },
]


def chat(model: str, messages: list, timeout: int = 60) -> dict:
    """Send a chat request to oMLX."""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 1024,
    }
    t0 = time.time()
    try:
        r = requests.post(CHAT_URL, json=payload, timeout=timeout)
        elapsed = time.time() - t0
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        tokens = data.get("usage", {})
        return {
            "ok": True,
            "content": content,
            "elapsed": round(elapsed, 2),
            "prompt_tokens": tokens.get("prompt_tokens", 0),
            "completion_tokens": tokens.get("completion_tokens", 0),
            "tokens_per_sec": round(tokens.get("completion_tokens", 0) / elapsed, 1) if elapsed > 0 else 0,
        }
    except Exception as e:
        return {"ok": False, "content": str(e), "elapsed": round(time.time() - t0, 2)}


def check_models():
    """Check which models are available on oMLX."""
    try:
        r = requests.get(f"{OMLX_BASE}/v1/models", timeout=5)
        r.raise_for_status()
        models = [m["id"] for m in r.json().get("data", [])]
        return models
    except Exception as e:
        print(f"❌ oMLX 連線失敗: {e}")
        sys.exit(1)


def run_test():
    """Run comparison tests."""
    print("=" * 70)
    print("  Gemma 4 26B vs TAIDE-12b 對比測試")
    print("=" * 70)

    # Check available models
    models = check_models()
    print(f"\n📦 oMLX 可用模型: {models}")

    has_a = MODEL_A in models
    has_b = MODEL_B in models

    if not has_a:
        print(f"⚠️  {MODEL_A} 不在 oMLX 中")
    if not has_b:
        print(f"⚠️  {MODEL_B} 不在 oMLX 中")
    if not has_a and not has_b:
        print("❌ 兩個模型都不可用，請確認 oMLX 設定")
        sys.exit(1)

    test_models = []
    if has_a:
        test_models.append(("TAIDE-12b", MODEL_A))
    if has_b:
        test_models.append(("Gemma4-26B", MODEL_B))

    results = []

    for i, tc in enumerate(TEST_CASES, 1):
        print(f"\n{'─' * 70}")
        print(f"📝 測試 {i}/{len(TEST_CASES)}: {tc['name']}")
        print(f"   評估標準: {tc['criteria']}")
        print(f"{'─' * 70}")

        row = {"test": tc["name"]}

        for label, model_id in test_models:
            print(f"\n  🔄 {label} ({model_id})...")
            result = chat(model_id, tc["messages"])

            if result["ok"]:
                print(f"  ✅ {label} — {result['elapsed']}s, "
                      f"{result['completion_tokens']} tokens, "
                      f"{result['tokens_per_sec']} tok/s")
                print(f"  📄 回答前 200 字:")
                print(f"     {result['content'][:200]}...")
                row[f"{label}_time"] = result["elapsed"]
                row[f"{label}_tps"] = result["tokens_per_sec"]
                row[f"{label}_response"] = result["content"]
            else:
                print(f"  ❌ {label} 失敗: {result['content']}")
                row[f"{label}_time"] = None
                row[f"{label}_response"] = f"ERROR: {result['content']}"

        results.append(row)

    # Summary
    print(f"\n{'=' * 70}")
    print("  📊 測試結果總覽")
    print(f"{'=' * 70}")
    print(f"\n{'測試名稱':<20}", end="")
    for label, _ in test_models:
        print(f"  {label:>15}s  {label:>10} tok/s", end="")
    print()
    print("-" * (20 + len(test_models) * 30))

    for row in results:
        print(f"{row['test']:<20}", end="")
        for label, _ in test_models:
            t = row.get(f"{label}_time")
            tps = row.get(f"{label}_tps")
            t_str = f"{t:>15.2f}s" if t else f"{'FAIL':>16}"
            tps_str = f"{tps:>10.1f} tok/s" if tps else f"{'N/A':>14}"
            print(f"  {t_str}  {tps_str}", end="")
        print()

    # Save full results
    out_path = "/Users/ai/Desktop/MAGI_v2/scripts/gemma4_test_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n💾 完整結果已儲存: {out_path}")


if __name__ == "__main__":
    run_test()
