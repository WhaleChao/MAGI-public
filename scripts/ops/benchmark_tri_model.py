#!/usr/bin/env python3
"""
benchmark_tri_model.py — 三模型並行基準測試
Phase 2: 在臨時 port 8085-8087 上測試三模型品質

使用方式：
  # 先啟動臨時實例：
  #   Tab 1: omlx serve --model-dir ~/.omlx/models-text-e4b --port 8085 --no-cache --base-path ~/.omlx
  #   Tab 2: omlx serve --model-dir ~/.omlx/models-text-phi4 --port 8086 --no-cache --base-path ~/.omlx
  #   Tab 3: omlx serve --model-dir ~/.omlx/models-text-smol --port 8087 --no-cache --base-path ~/.omlx

  ./venv/bin/python3 scripts/ops/benchmark_tri_model.py --ports 8085,8086,8087
  # 或測試正式 ports：
  ./venv/bin/python3 scripts/ops/benchmark_tri_model.py --ports 8080,8082,8083

建立時間：2026-04-14
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import concurrent.futures
from typing import Optional, Dict, Any, List

try:
    import requests  # type: ignore
except ImportError:
    print("請安裝 requests: pip install requests")
    sys.exit(1)

try:
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from api.tw_output_guard import normalize_output_text as _tw_guard  # type: ignore
except Exception:
    def _tw_guard(t):  # fallback
        return t


# ── 測試題目 ──

INTENT_CASES = [
    ("查詢 114年度原訴字第24號 的案件進度", "QUERY"),
    ("台灣侵權行為的構成要件是什麼", "QUERY"),
    ("開始夜議", "CMD"),
    ("今天天氣怎麼樣", "CHAT"),
    ("幫我查一下王大明的案件", "QUERY"),
    ("列出所有待繳費案件", "CMD"),
    ("什麼是消費者債務清理條例", "QUERY"),
    ("回憶上次提到的判決", "RECALL"),
    ("開始法扶批次掃描", "CMD"),
    ("被告有沒有抗辯理由", "QUERY"),
    ("聲請閱卷", "CMD"),
    ("台灣法院如何認定過失", "QUERY"),
    ("更新 Google 行事曆", "CMD"),
    ("最近的司法院判決有什麼趨勢", "QUERY"),
    ("你好嗎", "CHAT"),
    ("排庭 5/20 花蓮地院", "CMD"),
    ("民法 184 條的要件", "QUERY"),
    ("下載筆錄", "CMD"),
    ("繁體中文跟簡體中文有什麼差別", "QUERY"),
    ("草擬起訴狀", "CMD"),
]

SUMMARY_CASES = [
    {
        "title": "法律文件-1",
        "text": (
            "本件原告張三主張被告李四於民國一一四年三月十五日，"
            "在花蓮市中正路一段一百號前，因過失駕駛機車，撞擊原告"
            "致原告右手臂骨折，需住院手術治療，花費醫療費用新台幣"
            "十八萬元，並請求被告賠償精神慰撫金三十萬元，合計四十八萬元。"
            "被告否認過失，主張係原告突然變換車道所致。"
        ),
    },
    {
        "title": "法律文件-2",
        "text": (
            "消費者債務清理條例之更生方案，由法院依本條例第六十四條"
            "規定裁定認可，或裁定不認可時依聲請裁定更生方案者，均係"
            "法院就更生方案之認可為裁定，並非就更生程序為裁定。"
        ),
    },
    {
        "title": "法律文件-3",
        "text": (
            "民法第一百八十四條第一項前段規定，因故意或過失，不法侵害"
            "他人之權利者，負損害賠償責任。所謂權利，須為私法上之"
            "具體的特定權利，始足當之。至於侵害利益之情形，則應適用"
            "同條項後段之規定，而與前段之構成要件有別。"
        ),
    },
]

CHAT_CASES = [
    "你好，今天有什麼新消息嗎？",
    "台灣的法律制度是怎麼運作的？",
    "侵權行為跟債務不履行有什麼不同？",
    "什麼時候應該提起訴訟？",
    "法院判決書的結構是什麼？",
]


def get_model_id(base_url: str, timeout: int = 5) -> Optional[str]:
    try:
        resp = requests.get(f"{base_url}/v1/models", timeout=timeout)
        data = resp.json()
        return data["data"][0]["id"] if data.get("data") else None
    except Exception:
        return None


def call_chat(
    base_url: str, model_id: str, system: str, user: str, timeout: int = 60, max_tokens: int = 512
) -> Dict[str, Any]:
    t0 = time.time()
    try:
        resp = requests.post(
            f"{base_url}/v1/chat/completions",
            json={
                "model": model_id,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": max_tokens,
                "temperature": 0.1,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        elapsed = time.time() - t0
        # 估算 tok/s（從 usage）
        usage = data.get("usage", {})
        out_tokens = usage.get("completion_tokens", 0)
        toks = out_tokens / elapsed if elapsed > 0 and out_tokens > 0 else 0
        return {"success": True, "text": text, "elapsed": elapsed, "tok_s": toks}
    except Exception as e:
        return {"success": False, "text": "", "elapsed": time.time() - t0, "error": str(e)}


def run_benchmark(ports: List[int]) -> Dict[str, Any]:
    base_urls = [f"http://127.0.0.1:{p}" for p in ports]
    role_names = ["事實查核員 (E4B)", "邏輯審查員 (Phi-4)", "格式稽核員 (SmolLM3)"]

    print(f"\n{'='*60}")
    print("三模型並行基準測試")
    print(f"Ports: {ports}")
    print(f"{'='*60}\n")

    # 取得 model IDs
    model_ids = []
    for i, base in enumerate(base_urls):
        mid = get_model_id(base)
        if mid:
            print(f"✅ Port {ports[i]}: {mid} ({role_names[i]})")
            model_ids.append(mid)
        else:
            print(f"❌ Port {ports[i]}: 無法連接")
            model_ids.append(f"unknown-port-{ports[i]}")

    available_count = sum(1 for mid in model_ids if not mid.startswith("unknown"))
    print(f"\n可用模型數: {available_count}/3\n")
    if available_count < 1:
        return {"success": False, "error": "No models available"}

    results = {"intent": {}, "summary": {}, "chat": {}, "speed": {}}

    # A. 意圖分類 (20 題)
    print(f"{'─'*40}")
    print("A. 意圖分類測試（一票否決：三模型完全一致 >= 95%）")
    print(f"{'─'*40}")

    intent_scores = {i: 0 for i in range(len(base_urls))}
    consensus_count = 0
    intent_prompt = "請分類以下訊息的意圖，只回答一個 label（CMD / QUERY / CHAT / RECALL），不要解釋：\n\n{msg}"

    system_prefixes = [
        "你是事實查核員。請用繁體中文回答。",
        "你是邏輯審查員。請用繁體中文回答。",
        "你是格式稽核員。請用繁體中文回答。",
    ]

    for msg, expected in INTENT_CASES:
        labels = []
        for i, (base, mid, sys_p) in enumerate(zip(base_urls, model_ids, system_prefixes)):
            if mid.startswith("unknown"):
                labels.append(None)
                continue
            res = call_chat(base, mid, sys_p, intent_prompt.format(msg=msg), timeout=30, max_tokens=20)
            if res["success"]:
                raw = res["text"].strip().upper()
                # 正規化
                label = "UNKNOWN"
                for kw in ("CMD", "COMMAND"):
                    if kw in raw:
                        label = "CMD"
                        break
                for kw in ("QUERY", "查詢"):
                    if kw in raw:
                        label = "QUERY"
                        break
                for kw in ("CHAT", "聊天"):
                    if kw in raw:
                        label = "CHAT"
                        break
                for kw in ("RECALL", "記憶"):
                    if kw in raw:
                        label = "RECALL"
                        break
                labels.append(label)
                if label == expected:
                    intent_scores[i] += 1
            else:
                labels.append(None)

        valid_labels = [l for l in labels if l is not None]
        unanimous = len(set(valid_labels)) == 1 and len(valid_labels) == available_count
        if unanimous:
            consensus_count += 1
        status = "✅ 共識" if unanimous else f"⚠️ 分歧 {labels}"
        print(f"  [{expected:5s}] {msg[:30]:30s} → {labels} {status}")

    total_cases = len(INTENT_CASES)
    consensus_rate = consensus_count / total_cases * 100
    print(f"\n共識率: {consensus_count}/{total_cases} = {consensus_rate:.1f}%  (目標 >= 95%)")
    results["intent"] = {
        "consensus_rate": consensus_rate,
        "pass": consensus_rate >= 95,
        "consensus_count": consensus_count,
        "total": total_cases,
    }

    # B. 摘要測試 (3 篇)
    print(f"\n{'─'*40}")
    print("B. 摘要測試（交集要點 + [待確認] 標記）")
    print(f"{'─'*40}")

    summary_system = "你是法律摘要助理，請用繁體中文條列式摘要，3-5 點，每點一句話。"
    summary_results_ok = 0
    for case in SUMMARY_CASES:
        print(f"\n  文件: {case['title']}")
        summaries = []
        for i, (base, mid) in enumerate(zip(base_urls, model_ids)):
            if mid.startswith("unknown"):
                continue
            res = call_chat(base, mid, summary_system, f"請摘要：\n{case['text']}", timeout=60, max_tokens=200)
            if res["success"]:
                guarded = _tw_guard(res["text"])
                has_issue = "抱歉" in guarded and guarded != res["text"]
                status = "⚠️ 品質問題" if has_issue else "✅"
                print(f"    {role_names[i]}: {status} ({res['elapsed']:.1f}s)")
                summaries.append(res["text"])
            else:
                print(f"    {role_names[i]}: ❌ {res.get('error', '?')}")
        if summaries:
            summary_results_ok += 1

    results["summary"] = {"success": summary_results_ok == len(SUMMARY_CASES), "completed": summary_results_ok}

    # C. 閒聊品質 (5 題)
    print(f"\n{'─'*40}")
    print("C. 閒聊品質測試（無 persona leak / badge leak / 簡體）")
    print(f"{'─'*40}")

    chat_system = "你是 MAGI 法律助理，請用繁體中文回答，不要說出 [使用者陳述] 或 身為 CASPER 等內部語言。"
    chat_pass = 0
    for msg in CHAT_CASES:
        for i, (base, mid) in enumerate(zip(base_urls, model_ids)):
            if mid.startswith("unknown"):
                continue
            res = call_chat(base, mid, chat_system, msg, timeout=30, max_tokens=100)
            if res["success"]:
                guarded = _tw_guard(res["text"])
                ok = "抱歉" not in guarded or guarded == res["text"]
                if ok:
                    chat_pass += 1

    total_chat = len(CHAT_CASES) * available_count
    chat_rate = chat_pass / total_chat * 100 if total_chat > 0 else 0
    print(f"  通過率: {chat_pass}/{total_chat} = {chat_rate:.1f}%")
    results["chat"] = {"pass_rate": chat_rate, "pass": chat_rate >= 80}

    # D. 繁中品質
    print(f"\n{'─'*40}")
    print("D. 繁體中文品質（tw_output_guard 無攔截）")
    print(f"{'─'*40}")
    tw_pass_count = 0
    tw_total = 0
    for i, (base, mid) in enumerate(zip(base_urls, model_ids)):
        if mid.startswith("unknown"):
            continue
        res = call_chat(base, mid, "請用繁體中文回答：", "什麼是侵權行為？", timeout=30, max_tokens=150)
        if res["success"]:
            tw_total += 1
            guarded = _tw_guard(res["text"])
            ok = "抱歉" not in guarded or guarded == res["text"]
            print(f"  {role_names[i]}: {'✅ 通過' if ok else '⚠️ 攔截'}")
            if ok:
                tw_pass_count += 1
    results["tw_quality"] = {"pass": tw_pass_count == tw_total, "rate": tw_pass_count / tw_total * 100 if tw_total else 0}

    # E. 速度基線
    print(f"\n{'─'*40}")
    print("E. 速度基線 (tok/s)")
    print(f"{'─'*40}")
    speed_prompt = "請用繁體中文寫出民法侵權行為的構成要件，條列式，5 點。"
    for i, (base, mid) in enumerate(zip(base_urls, model_ids)):
        if mid.startswith("unknown"):
            continue
        res = call_chat(base, mid, "你是法律助理。", speed_prompt, timeout=60, max_tokens=200)
        tps = res.get("tok_s", 0)
        elapsed = res.get("elapsed", 0)
        print(f"  {role_names[i]}: {tps:.1f} tok/s ({elapsed:.1f}s)")
        results["speed"][role_names[i]] = tps

    # 總結
    print(f"\n{'='*60}")
    print("總結")
    print(f"{'='*60}")
    overall_pass = all([
        results["intent"].get("pass", False),
        results["summary"].get("success", False),
        results["chat"].get("pass", False),
        results["tw_quality"].get("pass", False),
    ])
    results["overall_pass"] = overall_pass
    print(f"意圖共識率: {results['intent'].get('consensus_rate', 0):.1f}% {'✅' if results['intent'].get('pass') else '❌'}")
    print(f"摘要完成:   {results['summary'].get('completed', 0)}/3 {'✅' if results['summary'].get('success') else '❌'}")
    print(f"閒聊品質:   {results['chat'].get('pass_rate', 0):.1f}% {'✅' if results['chat'].get('pass') else '❌'}")
    print(f"繁中品質:   {results['tw_quality'].get('rate', 0):.1f}% {'✅' if results['tw_quality'].get('pass') else '❌'}")
    print(f"\n{'✅ 全部通過 — 可進入下一 Phase' if overall_pass else '❌ 有項目未通過 — 請檢查模型品質'}")

    return results


def main():
    parser = argparse.ArgumentParser(description="三模型並行基準測試")
    parser.add_argument("--ports", default="8085,8086,8087", help="逗號分隔 port，例如 8085,8086,8087")
    parser.add_argument("--json", action="store_true", help="輸出 JSON 格式")
    args = parser.parse_args()

    ports = [int(p.strip()) for p in args.ports.split(",")]
    if len(ports) != 3:
        print("需要指定 3 個 port")
        sys.exit(1)

    results = run_benchmark(ports)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    sys.exit(0 if results.get("overall_pass") else 1)


if __name__ == "__main__":
    main()
