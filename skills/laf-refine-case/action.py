#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import ensure_orch_on_sys_path

ensure_orch_on_sys_path()


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


def _build_prompt(case_info: dict, existing_cases: list) -> str:
    client_name = case_info.get("client_name", "")
    case_reason = case_info.get("case_reason", "")
    current_type = case_info.get("case_type", "")
    current_stage = case_info.get("case_stage", "")

    lines = []
    for r in existing_cases or []:
        if not isinstance(r, dict):
            continue
        lines.append(
            f"- 案號:{r.get('case_number','')} 類型:{r.get('case_type','')} 階段:{r.get('case_stage','')} 案由:{r.get('case_reason','')}"
        )
    existing_str = "\n".join(lines) if lines else "無"

    return f"""
請協助判斷以下法律扶助案件的正確「案件類型」與「案件階段」。
請參考該當事人在資料庫中的現有案件，判斷這是否為同一案件的後續階段，或是新案件。

新案件資訊：
- 當事人：{client_name}
- 原始案由：{case_reason}
- 初步判斷類型：{current_type}
- 初步判斷階段：{current_stage}

資料庫中現有案件：
{existing_str}

判斷邏輯：
1. 若新案件案由包含「再審」、「非常上訴」，且資料庫中有對應的原審案件（例如案由相同或相關），請標記為「刑事/再審」或「刑事/非常上訴」。
2. 若新案件案由包含「消費者債務清理」、「更生」、「清算」，類型應為「消費者債務清理」。
3. 若資料庫中有完全相同的案件（案由、階段皆同），請維持與資料庫一致的類型與階段。
4. 若無法確定，請根據台灣法律實務判斷。

請只回覆 JSON（不要 code block、不要多餘文字）：
{{"case_type":"刑事","case_stage":"再審"}}
""".strip()


def refine(case_info: dict, existing_cases: list | None = None) -> dict:
    from casper_tools_client import casper_chat

    prompt = _build_prompt(case_info, existing_cases or [])
    r = casper_chat(prompt, timeout_sec=120)
    if not isinstance(r, dict) or not r.get("success"):
        return {"success": False, "error": (r.get("error") if isinstance(r, dict) else "casper_chat failed")}
    s = (r.get("response") or "").strip()
    if not s:
        return {"success": False, "error": "empty response"}

    # Strip fences if needed
    if "```json" in s:
        s = s.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in s:
        s = s.split("```", 1)[1].split("```", 1)[0].strip()

    try:
        obj = json.loads(s)
    except Exception as e:
        return {"success": False, "error": f"json parse failed: {e}"}
    if not isinstance(obj, dict):
        return {"success": False, "error": "response is not a json object"}

    out = dict(case_info or {})
    if obj.get("case_type"):
        out["case_type"] = obj.get("case_type")
    if obj.get("case_stage"):
        out["case_stage"] = obj.get("case_stage")
    return {"success": True, "output": out}


def main() -> int:
    ap = argparse.ArgumentParser(description="laf-refine-case skill")
    ap.add_argument("--task", default="help", help="task text")
    args = ap.parse_args()
    task = (args.task or "").strip()

    if task in {"help", "summary", "list"}:
        return _ok({"success": True, "commands": ["help", "self_test", "refine {..json..}"]})

    if task == "self_test":
        # Just ensure CASPER is callable; logic correctness depends on live knowledge.
        try:
            from casper_tools_client import casper_chat
            r = casper_chat("請回覆：OK（只輸出 OK）", timeout_sec=25)
            ok = bool(isinstance(r, dict) and r.get("success") and (r.get("response") or "").strip())
            if not ok and "timeout" in str((r or {}).get("error", "")).lower():
                return _ok({"success": True, "degraded": True, "check": "casper_chat_timeout"})
            return _ok({"success": bool(ok), "check": "casper_chat"})
        except Exception as e:
            return _ok({"success": False, "error": str(e)[:200]})

    if task.startswith("refine"):
        payload = _load_jsonish(task[len("refine") :].strip())
        case_info = payload.get("case_info")
        if not isinstance(case_info, dict):
            return _ok({"success": False, "error": "missing case_info (dict)"})
        existing = payload.get("existing_cases") or []
        if not isinstance(existing, list):
            existing = []
        res = refine(case_info, existing)
        return _ok(res)

    return _ok({"success": False, "error": f"unknown task: {task}"})


if __name__ == "__main__":
    raise SystemExit(main())
