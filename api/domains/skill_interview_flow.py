"""
Skill interview system extracted from Orchestrator.

All functions accept an `orch` parameter (the Orchestrator instance)
instead of `self`.
"""
from __future__ import annotations

import logging
import os
import re
import time

logger = logging.getLogger("Orchestrator")


# ---------------------------------------------------------------------------
# Static helpers (no orch needed)
# ---------------------------------------------------------------------------

def skill_interview_default_reply(message: str) -> bool:
    low = str(message or "").strip().lower()
    return low in {
        "\u5c31\u9019\u6a23", "\u7167\u9810\u8a2d", "\u7167\u9810\u8a2d\u5373\u53ef", "\u9810\u8a2d", "ok", "okay", "yes", "y",
        "auto", "\u597d", "\u53ef\u4ee5", "\u90fd\u53ef\u4ee5", "\u4f60\u6c7a\u5b9a", "\u7167\u9435\u7a79\u9810\u8a2d", "\u7565\u904e",
    }


def skill_interview_cancel_reply(message: str) -> bool:
    low = str(message or "").strip().lower()
    return low in {"\u53d6\u6d88", "\u5148\u4e0d\u8981", "\u505c\u6b62", "stop", "cancel", "\u7b97\u4e86"}


def skill_interview_status_reply(message: str) -> bool:
    low = str(message or "").strip().lower()
    return low in {"\u76ee\u524d\u9032\u5ea6", "\u9032\u5ea6", "skill \u72c0\u614b", "\u6280\u80fd\u72c0\u614b", "\u8a2a\u8ac7\u72c0\u614b", "status"}


def skill_interview_split_items(text: str, limit: int = 8) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    parts = re.split(r"[\n,\uff0c\u3001;\uff1b/|]+", raw)
    out = []
    seen = set()
    for part in parts:
        item = str(part or "").strip(" -\u2022\t")
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item[:120])
        if len(out) >= limit:
            break
    return out


def parse_skill_interview_io(message: str) -> tuple[list[str], list[str]]:
    text = str(message or "").strip()
    if not text:
        return [], []
    inputs: list[str] = []
    outputs: list[str] = []
    match_in = re.search(r"\u8f38\u5165\s*[:\uff1a]\s*(.*?)(?=\s*\u8f38\u51fa\s*[:\uff1a]|$)", text, re.IGNORECASE | re.DOTALL)
    match_out = re.search(r"\u8f38\u51fa\s*[:\uff1a]\s*(.*)$", text, re.IGNORECASE | re.DOTALL)
    if match_in:
        inputs = skill_interview_split_items(match_in.group(1))
    if match_out:
        outputs = skill_interview_split_items(match_out.group(1))
    if not inputs and not outputs and "->" in text:
        left, right = text.split("->", 1)
        inputs = skill_interview_split_items(left)
        outputs = skill_interview_split_items(right)
    if not inputs and not outputs:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) >= 2:
            inputs = skill_interview_split_items(lines[0])
            outputs = skill_interview_split_items(" ".join(lines[1:]))
    return inputs, outputs


def format_skill_interview_progress(entry: dict) -> str:
    draft = entry.get("draft") if isinstance(entry.get("draft"), dict) else {}
    step = int(entry.get("step") or 0)
    total = 5
    lines = [f"\U0001f9e9 SKILL \u8a2a\u8ac7\u9032\u884c\u4e2d\uff08{min(step + 1, total)}/{total}\uff09"]
    if draft.get("purpose"):
        lines.append(f"\u76ee\u6a19\uff1a{str(draft.get('purpose'))[:120]}")
    if draft.get("trigger_examples"):
        lines.append("\u89f8\u767c\u8a5e\uff1a" + "\u3001".join([str(x) for x in (draft.get("trigger_examples") or [])[:4]]))
    if draft.get("inputs"):
        lines.append("\u8f38\u5165\uff1a" + "\u3001".join([str(x) for x in (draft.get("inputs") or [])[:3]]))
    if draft.get("outputs"):
        lines.append("\u8f38\u51fa\uff1a" + "\u3001".join([str(x) for x in (draft.get("outputs") or [])[:3]]))
    return "\n".join(lines)


def render_skill_interview_question(entry: dict) -> str:
    draft = entry.get("draft") if isinstance(entry.get("draft"), dict) else {}
    step = int(entry.get("step") or 0)
    total = 5
    reason = str(entry.get("trigger_reason") or "manual")
    intro = (
        "\U0001f9e9 \u6211\u5224\u65b7\u9019\u500b\u9700\u6c42\u76ee\u524d\u6c92\u6709\u73fe\u6210 SKILL \u53ef\u7a69\u5b9a\u63a5\u624b\uff0c\u5148\u7528 5 \u984c\u554f\u7b54\u5e6b\u4f60\u88dc\u4e00\u500b\u65b0 SKILL\u3002"
        if reason == "gap"
        else "\U0001f9e9 \u6211\u5011\u5148\u7528 5 \u984c\u554f\u7b54\u628a\u9019\u500b\u65b0 SKILL \u5b9a\u7fa9\u6e05\u695a\uff0c\u5b8c\u6210\u5f8c\u6211\u6703\u76f4\u63a5\u5beb\u9032 MAGI\u3002"
    )
    footer = "\n\u56de\u8986 `\u53d6\u6d88` \u53ef\u7d42\u6b62\uff0c\u56de\u8986 `\u76ee\u524d\u9032\u5ea6` \u53ef\u67e5\u770b\u8349\u7a3f\u3002"
    if step == 0:
        return (
            f"{intro}\n\n"
            f"Q1/{total} \u76ee\u6a19\u78ba\u8a8d\n"
            f"\u6211\u5148\u6293\u6210\uff1a\n{draft.get('purpose')}\n\n"
            "\u5982\u679c\u6b63\u78ba\u56de `\u5c31\u9019\u6a23`\uff0c\u8981\u4fee\u6539\u5c31\u76f4\u63a5\u6539\u5beb\u6210\u4f60\u8981\u7684\u76ee\u6a19\u3002"
            f"{footer}"
        )
    if step == 1:
        triggers = "\u3001".join([str(x) for x in (draft.get("trigger_examples") or [])[:3]])
        return (
            f"Q2/{total} \u89f8\u767c\u65b9\u5f0f\n"
            f"\u4f60\u901a\u5e38\u6703\u600e\u9ebc\u53eb\u9019\u985e\u4efb\u52d9\uff1f\u8acb\u7d66\u6211 2-5 \u500b\u89f8\u767c\u8a5e\u6216\u4f8b\u53e5\u3002\n"
            f"\u9810\u8a2d\u6211\u6703\u5148\u7528\uff1a{triggers}\n\n"
            "\u4e0d\u77e5\u9053\u53ef\u56de `\u7167\u9810\u8a2d`\u3002"
            f"{footer}"
        )
    if step == 2:
        return (
            f"Q3/{total} \u8f38\u5165\u8207\u8f38\u51fa\n"
            "\u9019\u500b SKILL \u901a\u5e38\u6703\u6536\u5230\u54ea\u4e9b\u8f38\u5165\u3001\u8981\u56de\u54ea\u4e9b\u8f38\u51fa\uff1f\n"
            "\u53ef\u76f4\u63a5\u7528\uff1a`\u8f38\u5165\uff1a... / \u8f38\u51fa\uff1a...`\u3002\n\n"
            "\u4e0d\u77e5\u9053\u53ef\u56de `\u7167\u9810\u8a2d`\u3002"
            f"{footer}"
        )
    if step == 3:
        guards = "\u3001".join([str(x) for x in (draft.get("guardrails") or [])[:3]])
        return (
            f"Q4/{total} \u908a\u754c\u8207\u7981\u5340\n"
            "\u6709\u6c92\u6709\u7279\u6b8a\u908a\u754c\uff1f\u4f8b\u5982\u53ea\u80fd\u5148\u8349\u7a3f\u3001\u4e0d\u80fd\u81ea\u52d5\u9001\u51fa\u3001\u4e0d\u80fd\u78b0\u5916\u7db2\u3001\u8981\u5148\u554f\u4f60\u78ba\u8a8d\u3002\n"
            f"\u76ee\u524d\u9810\u8a2d\uff1a{guards}\n\n"
            "\u6c92\u6709\u5c31\u56de `\u7167\u9435\u7a79\u9810\u8a2d`\u3002"
            f"{footer}"
        )
    return (
        f"Q5/{total} \u6280\u80fd\u540d\u7a31\n"
        f"\u6700\u5f8c\uff0c\u9019\u500b SKILL \u60f3\u53eb\u4ec0\u9ebc\u540d\u5b57\uff1f\u76ee\u524d\u66ab\u540d\uff1a{draft.get('display_name')}\n\n"
        "\u4f60\u53ef\u4ee5\u76f4\u63a5\u7d66\u4e2d\u6587\u6216\u82f1\u6587\u540d\u7a31\uff1b\u4e0d\u77e5\u9053\u5c31\u56de `\u7167\u9810\u8a2d`\u3002"
        f"{footer}"
    )


# ---------------------------------------------------------------------------
# Main flow functions (need orch)
# ---------------------------------------------------------------------------

def start_skill_interview(orch, user_id: str, platform: str, role: str, initial_request: str, trigger_reason: str = "manual") -> str:
    if role != "admin":
        return "\u26d4 \u9019\u500b\u9700\u6c42\u770b\u8d77\u4f86\u9700\u8981\u65b0\u589e SKILL\uff0c\u4f46\u76ee\u524d\u53ea\u6709\u7ba1\u7406\u54e1\u53ef\u4ee5\u6b63\u5f0f\u5beb\u5165 MAGI\u3002"
    from skills.management.skill_interview import infer_skill_defaults

    pending = orch._load_skill_interview_pending()
    key = orch._pending_key(user_id, platform)
    draft = infer_skill_defaults(initial_request)
    pending[key] = {
        "kind": "skill_interview",
        "user_id": str(user_id or "").strip(),
        "platform": str(platform or "").strip(),
        "role": str(role or "user").strip(),
        "trigger_reason": str(trigger_reason or "manual").strip(),
        "initial_request": str(initial_request or "").strip()[:2000],
        "draft": draft,
        "step": 0,
        "created_at": time.time(),
        "updated_at": time.time(),
        "expires_at": time.time() + float(os.environ.get("MAGI_SKILL_INTERVIEW_TTL_SEC", "5400")),
    }
    orch._save_skill_interview_pending(pending)
    orch._append_route_trace(
        str(user_id or ""),
        str(platform or ""),
        "skill_interview",
        "started",
        {"trigger_reason": str(trigger_reason or "manual"), "preview": str(initial_request or "")[:80]},
    )
    return render_skill_interview_question(pending[key])


def finalize_skill_interview(orch, user_id: str, platform: str, entry: dict) -> str:
    from skills.management.skill_interview import create_skill_from_interview

    result = create_skill_from_interview(
        str(entry.get("initial_request") or ""),
        entry.get("draft") if isinstance(entry.get("draft"), dict) else {},
        requested_by=f"{platform}:{user_id}",
    )
    if not result.get("success"):
        violations = result.get("violations") or []
        if violations:
            return "\U0001f6e1\ufe0f \u9019\u6b21 SKILL \u751f\u6210\u88ab Iron Dome \u64cb\u4e0b\uff0c\u539f\u56e0\uff1a\n- " + "\n- ".join([str(v) for v in violations[:4]])
        return f"\u274c \u65b0 SKILL \u751f\u6210\u5931\u6557\uff1a{result.get('error', 'unknown')}"

    ci = result.get("ci") if isinstance(result.get("ci"), dict) else {}
    ci_ok = bool(ci.get("success"))
    profile = result.get("profile") if isinstance(result.get("profile"), dict) else {}
    snapshot = result.get("snapshot") if isinstance(result.get("snapshot"), dict) else {}
    definition = result.get("definition") if isinstance(result.get("definition"), dict) else {}
    triggers = "\u3001".join([str(x) for x in (profile.get("trigger_examples") or [])[:3]])
    lines = [
        "\U0001f9ec \u65b0 SKILL \u5df2\u5efa\u7acb\u4e26\u555f\u7528",
        f"\u540d\u7a31\uff1a{result.get('display_name')}",
        f"\u8cc7\u6599\u593e\uff1a`{result.get('skill_name')}`",
        f"\u8def\u5f91\uff1a`{result.get('skill_path')}`",
        f"\u89f8\u767c\u8a5e\uff1a{triggers}" if triggers else "\u89f8\u767c\u8a5e\uff1a\u5df2\u4f9d\u63cf\u8ff0\u5efa\u7acb",
        f"\u7248\u672c\u5feb\u7167\uff1a`{snapshot.get('version_id') or 'n/a'}`",
        "\u8a3b\u518a\uff1a{} definitions.json".format("\u2705" if definition.get("success") else "\u26a0\ufe0f"),
        "\u9a57\u8b49\uff1a{} smoke / CI".format("\u2705" if ci_ok else "\u26a0\ufe0f"),
        "\u6a94\u6848\uff1aSKILL.md\u3001action.py\u3001skill_profile.json",
        "",
        "\u4e4b\u5f8c\u4f60\u53ef\u4ee5\u76f4\u63a5\u7528\u9019\u985e\u53e5\u5b50\u6e2c\u5b83\uff1a",
        "- %s" % str((profile.get("trigger_examples") or ["\u76f4\u63a5\u63cf\u8ff0\u4f60\u7684\u4efb\u52d9"])[0]),
    ]
    if not ci_ok:
        lines.append(f"CI \u88dc\u5145\uff1a{str(ci.get('checks') or '')[:240]}")
    orch._append_route_trace(
        str(user_id or ""),
        str(platform or ""),
        "skill_interview",
        "finalized",
        {"skill_name": str(result.get("skill_name") or ""), "ci_ok": ci_ok},
    )
    return "\n".join(lines).strip()


def handle_skill_interview_if_any(orch, user_id: str, platform: str, role: str, message: str) -> tuple[bool, str]:
    msg = str(message or "").strip()
    if not msg:
        return False, ""
    pending = orch._load_skill_interview_pending()
    key = orch._pending_key(user_id, platform)
    entry = pending.get(key) if isinstance(pending, dict) else None
    if not isinstance(entry, dict):
        return False, ""

    now = time.time()
    exp = float(entry.get("expires_at", 0.0) or 0.0)
    if exp and now > exp:
        pending.pop(key, None)
        orch._save_skill_interview_pending(pending)
        return True, "\u23f1\ufe0f \u525b\u525b\u90a3\u7b46 SKILL \u8a2a\u8ac7\u5df2\u904e\u671f\u3002\u4f60\u53ef\u4ee5\u518d\u63cf\u8ff0\u4e00\u6b21\u9700\u6c42\uff0c\u6211\u6703\u91cd\u65b0\u958b\u59cb\u3002"

    if skill_interview_cancel_reply(msg):
        pending.pop(key, None)
        orch._save_skill_interview_pending(pending)
        return True, "\U0001f6d1 \u5df2\u53d6\u6d88\u9019\u6b21 SKILL \u8a2a\u8ac7\uff0c\u4e0d\u6703\u5beb\u5165\u65b0\u6280\u80fd\u3002"

    if skill_interview_status_reply(msg):
        return True, format_skill_interview_progress(entry) + "\n\n" + render_skill_interview_question(entry)

    if role != "admin":
        pending.pop(key, None)
        orch._save_skill_interview_pending(pending)
        return True, "\u26d4 \u9019\u7b46 SKILL \u8a2a\u8ac7\u9700\u8981\u7ba1\u7406\u54e1\u6b0a\u9650\u624d\u80fd\u5b8c\u6210\u3002"

    draft = entry.get("draft") if isinstance(entry.get("draft"), dict) else {}
    step = int(entry.get("step") or 0)
    use_default = skill_interview_default_reply(msg)

    if step == 0 and not use_default:
        draft["purpose"] = msg[:500]
    elif step == 1 and not use_default:
        triggers = skill_interview_split_items(msg)
        if triggers:
            draft["trigger_examples"] = triggers
    elif step == 2 and not use_default:
        inputs, outputs = parse_skill_interview_io(msg)
        if inputs:
            draft["inputs"] = inputs
        if outputs:
            draft["outputs"] = outputs
        if not inputs and not outputs:
            draft["outputs"] = skill_interview_split_items(msg)
    elif step == 3 and not use_default:
        guardrails = skill_interview_split_items(msg)
        if guardrails:
            draft["guardrails"] = guardrails
    elif step == 4 and not use_default:
        draft["display_name"] = msg[:60]

    entry["draft"] = draft
    entry["step"] = step + 1
    entry["updated_at"] = now
    entry["expires_at"] = now + float(os.environ.get("MAGI_SKILL_INTERVIEW_TTL_SEC", "5400"))

    if entry["step"] >= 5:
        pending.pop(key, None)
        orch._save_skill_interview_pending(pending)
        return True, finalize_skill_interview(orch, user_id, platform, entry)

    pending[key] = entry
    orch._save_skill_interview_pending(pending)
    return True, render_skill_interview_question(entry)


def get_skill_interview_state(orch, user_id: str, platform: str) -> dict:
    pending = orch._load_skill_interview_pending()
    key = orch._pending_key(user_id, platform)
    entry = pending.get(key) if isinstance(pending, dict) else None
    if not isinstance(entry, dict):
        return {"active": False, "step": 0, "total_steps": 5, "prompt": "", "draft": {}}

    now = time.time()
    exp = float(entry.get("expires_at", 0.0) or 0.0)
    if exp and now > exp:
        pending.pop(key, None)
        orch._save_skill_interview_pending(pending)
        return {"active": False, "step": 0, "total_steps": 5, "prompt": "", "draft": {}}

    step = int(entry.get("step") or 0)
    return {
        "active": True,
        "step": min(step + 1, 5),
        "total_steps": 5,
        "trigger_reason": str(entry.get("trigger_reason") or "manual"),
        "initial_request": str(entry.get("initial_request") or ""),
        "draft": entry.get("draft") if isinstance(entry.get("draft"), dict) else {},
        "prompt": render_skill_interview_question(entry),
        "updated_at": float(entry.get("updated_at") or 0.0),
    }
