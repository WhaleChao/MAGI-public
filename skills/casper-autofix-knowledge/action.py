#!/usr/bin/env python3
import argparse
import json
import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEED_FILE = os.path.join(BASE_DIR, "knowledge_seed.json")

def _extract_terms(text):
    return set(re.findall(r"[a-zA-Z0-9_\-\u4e00-\u9fff]{2,}", (text or "").lower()))

def main():
    parser = argparse.ArgumentParser(description="CASPER internalized skill responder")
    parser.add_argument("--task", default="", help="Task or question")
    parser.add_argument("task_fallback", nargs="*", help="Fallback task words")
    args = parser.parse_args()
    task = (args.task or " ".join(args.task_fallback)).strip()
    if not task:
        print("請提供問題：python3 action.py --task \"<text>\"")
        return 0

    if not os.path.exists(SEED_FILE):
        print("knowledge_seed.json not found")
        return 1

    with open(SEED_FILE, "r", encoding="utf-8") as f:
        seed = json.load(f)
    terms = _extract_terms(task)
    ranked = []
    for item in seed:
        kws = set([str(k).lower() for k in item.get("keywords", []) if str(k).strip()])
        score = len(terms.intersection(kws))
        if score > 0:
            ranked.append((score, item))
    ranked.sort(key=lambda x: x[0], reverse=True)

    if not ranked:
        print("目前沒有精準匹配的內化經驗，請再提供更多上下文。")
        return 0

    print("CASPER Internalized Tips:")
    for _, item in ranked[:5]:
        tip = (item.get("tip") or "").strip()
        if tip:
            print(f"- {tip}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
