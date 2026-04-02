#!/usr/bin/env python3
import argparse
import json
import sys


CODE_DIR = "/Users/ai/Desktop/code"
if CODE_DIR not in sys.path:
    sys.path.insert(0, CODE_DIR)


DEFAULT_PROMPT = """你是台灣法律實務見解整理助理，請將下列「原始文字」整理成可直接存入資料庫的實務見解。

規則：
1. 請用繁體中文。
2. 不要編造未出現的事實或數字；不確定就不要寫。
3. 內容要可讀、可搜尋：保留關鍵法律爭點、結論、理由脈絡。
4. 若原文含大量雜訊（OCR/頁眉頁腳），請自行去除。
5. 請不要摘要成太短，目標是「可用的精煉全文」。

[案由脈絡]
{case_reason_context}

[來源 URL]
{url}

[高品質範例]
{examples_text}

[原始文字]
{raw_text}
"""


def _load_jsonish(text: str) -> dict:
    t = (text or "").strip()
    if not t:
        return {}
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else {"value": v}
    except Exception:
        return {"value": t}


def _ok(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("success") else 1


def _build_prompt(p: dict) -> str:
    tpl = (p.get("prompt_template") or "").strip() or DEFAULT_PROMPT
    return tpl.format(
        case_reason_context=(p.get("case_reason_context") or "").strip() or "(無)",
        url=(p.get("url") or "").strip() or "(無)",
        examples_text=(p.get("examples_text") or "").strip() or "(無)",
        raw_text=(p.get("raw_text") or "").strip(),
    )


def main() -> int:
    from casper_tools_client import casper_chat

    ap = argparse.ArgumentParser(description="insight-refine skill")
    ap.add_argument("--task", default="help", help="task text")
    args = ap.parse_args()
    task = (args.task or "").strip()

    if task in {"help", "summary", "list"}:
        return _ok({"success": True, "commands": ["help", "self_test", "refine {..json..}"]})

    if task == "self_test":
        prompt = "請用繁體中文回覆：OK（只要輸出 OK）"
        r = casper_chat(prompt, timeout_sec=60)
        ok = bool(r.get("success") and (r.get("response") or "").strip())
        return _ok({"success": bool(ok), "check": "casper_chat", "route": r.get("route", ""), "model": r.get("model", "")})

    if task.startswith("refine"):
        payload = _load_jsonish(task[len("refine") :].strip())
        raw = (payload.get("raw_text") or payload.get("text") or "").strip()
        if not raw:
            return _ok({"success": False, "error": "missing raw_text"})
        prompt = _build_prompt(payload)
        timeout = int(payload.get("timeout_sec", 240))
        r = casper_chat(prompt, timeout_sec=timeout)
        if not r.get("success"):
            return _ok({"success": False, "error": r.get("error", "casper_chat failed"), "route": r.get("route", "")})
        out = (r.get("response") or "").strip()
        if not out or len(out) < 20:
            # Behave like the original refiner: fall back to raw if too short.
            return _ok({"success": True, "note": "refined too short; returned raw_text", "output": raw})
        return _ok({"success": True, "route": r.get("route", ""), "model": r.get("model", ""), "output": out})

    return _ok({"success": False, "error": f"unknown task: {task}"})


if __name__ == "__main__":
    raise SystemExit(main())

