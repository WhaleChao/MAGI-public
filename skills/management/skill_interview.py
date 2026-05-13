#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

from skills.evolution.skill_genesis import (
    _register_skill_tool_definition,
    _safe_write_skill_file,
    _snapshot_skill_version,
    list_skill_versions,
    run_skill_ci,
    validate_skill_safety,
)

_MAGI_ROOT = os.environ.get("MAGI_ROOT_DIR", str(Path(__file__).resolve().parents[2]))
SKILLS_DIR = os.path.join(_MAGI_ROOT, "skills")
INTERVIEW_HISTORY_FILE = os.path.join(_MAGI_ROOT, "logs", "skill_interview_history.jsonl")

_DEFAULT_INPUTS = [
    "使用者的文字需求",
    "相關附件、截圖、音訊或文件（如果有）",
    "必要的案件資訊、時間、對象或識別碼",
]

_DEFAULT_OUTPUTS = [
    "可直接交付的結果或草稿",
    "精簡的重點整理",
    "若資訊不足時，回覆下一個需要補充的欄位",
]

_DEFAULT_GUARDRAILS = [
    "遵守 Iron Dome 與 MAGI 邊界，不執行破壞性操作。",
    "涉及外部送出、刪除、覆蓋、付款、發信或不可逆動作時，先明確確認。",
    "資訊不足時先追問一個最關鍵的缺漏，不要自行猜測敏感內容。",
    "預設優先輸出草稿、摘要、建議步驟或可審核內容。",
]


def _clean_request(text: str) -> str:
    cleaned = str(text or "").strip()
    patterns = [
        r"^@magi\s+",
        r"^(幫我|請|麻煩|可以幫我|我想要|我要)\s*",
        r"^(學會|學習|建立技能|建立skill|製作技能|create skill|build skill|learn to|寫工具)\s*",
    ]
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned.strip(" ：:，,。.\n\t") or "自訂流程"


def _split_items(text: str, limit: int = 8) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    parts = re.split(r"[\n,，、;；/|]+", raw)
    out: list[str] = []
    seen = set()
    for part in parts:
        item = str(part or "").strip(" -•\t")
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


def _derive_slug(display_name: str, initial_request: str) -> str:
    base = str(display_name or "").strip()
    slug = re.sub(r"[^a-z0-9_-]+", "-", base.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        words = re.findall(r"[a-z0-9]{2,}", str(initial_request or "").lower())
        if words:
            slug = "-".join(words[:4]).strip("-")
        else:
            digest = hashlib.sha1(str(initial_request or base).encode("utf-8")).hexdigest()[:10]
            slug = f"skill-{digest}"
    slug = f"custom-{slug[:40]}".strip("-")
    return re.sub(r"-{2,}", "-", slug)


def _ensure_unique_slug(slug: str) -> str:
    candidate = str(slug or "").strip() or "custom-skill"
    n = 1
    while os.path.exists(os.path.join(SKILLS_DIR, candidate)):
        n += 1
        candidate = f"{slug}-{n}"
    return candidate


def infer_skill_defaults(initial_request: str) -> dict:
    cleaned = _clean_request(initial_request)
    display_name = cleaned[:48] or "自訂流程技能"
    trigger_seed = _split_items(cleaned, limit=3)
    if not trigger_seed:
        trigger_seed = [cleaned]
    return {
        "display_name": display_name,
        "purpose": f"處理與「{cleaned}」相關的請求，產出可直接使用的結果或下一步指引。",
        "trigger_examples": [
            cleaned,
            f"幫我處理{cleaned}",
            f"{cleaned} 要怎麼做",
        ][:3],
        "inputs": list(_DEFAULT_INPUTS),
        "outputs": list(_DEFAULT_OUTPUTS),
        "guardrails": list(_DEFAULT_GUARDRAILS),
    }


def _normalise_answers(initial_request: str, answers: dict) -> dict:
    defaults = infer_skill_defaults(initial_request)
    merged = dict(defaults)
    merged.update(answers or {})

    display_name = str(merged.get("display_name") or defaults["display_name"]).strip()[:60] or defaults["display_name"]
    purpose = str(merged.get("purpose") or defaults["purpose"]).strip()[:500] or defaults["purpose"]
    trigger_examples = _split_items("\n".join(merged.get("trigger_examples") or [])) or defaults["trigger_examples"]
    inputs = _split_items("\n".join(merged.get("inputs") or [])) or defaults["inputs"]
    outputs = _split_items("\n".join(merged.get("outputs") or [])) or defaults["outputs"]
    guardrails = _split_items("\n".join(merged.get("guardrails") or [])) or defaults["guardrails"]

    workflow = [
        f"辨識這是否屬於「{display_name}」相關需求；若不是，回到一般路由。",
        "先確認任務目標與必要輸入；若關鍵資訊不足，先追問一個最重要的缺口。",
        "依照使用者需求產出結果，並保持內容可審核、可複查。",
        f"預期輸出重點包含：{'、'.join(outputs[:3])}。",
        "若涉及外部系統寫入、正式送出或不可逆動作，先停下來請求確認。",
    ]

    slug = _ensure_unique_slug(_derive_slug(display_name, initial_request))
    return {
        "display_name": display_name,
        "slug": slug,
        "purpose": purpose,
        "trigger_examples": trigger_examples[:6],
        "inputs": inputs[:8],
        "outputs": outputs[:8],
        "guardrails": guardrails[:8],
        "workflow": workflow,
        "initial_request": str(initial_request or "").strip(),
        "created_at": datetime.now().isoformat(),
    }


def _build_description(profile: dict) -> str:
    """Build a 'pushy' description following Anthropic skill-creator pattern.

    The description is the primary triggering mechanism — it should explicitly
    state when to use the skill to combat undertriggering. Include:
    1. What the skill does (purpose)
    2. Explicit 'when to use' triggers
    3. Key trigger phrases the user might say
    """
    purpose = str(profile.get("purpose") or "").strip()
    triggers = [str(x).strip() for x in profile.get("trigger_examples") or [] if str(x).strip()]
    desc = purpose
    if triggers:
        # "Pushy" pattern: explicitly tell the model to use this skill
        trigger_str = "；".join(triggers[:4])
        desc += f" 使用時機：當使用者說「{trigger_str}」等相關指令時，務必使用本技能。"
    return desc[:400]  # Allow longer descriptions for triggering accuracy


def _to_bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items if str(item).strip())


def _build_skill_md(profile: dict) -> str:
    created = datetime.now().strftime("%Y-%m-%d")
    display_name = str(profile.get("display_name") or profile.get("slug") or "Custom Skill")
    slug = str(profile.get("slug") or "").strip()
    description = _build_description(profile)
    purpose = str(profile.get("purpose") or "").strip()
    triggers = _to_bullets(profile.get("trigger_examples") or [])
    inputs = _to_bullets(profile.get("inputs") or [])
    outputs = _to_bullets(profile.get("outputs") or [])
    guardrails = _to_bullets(profile.get("guardrails") or [])
    workflow = "\n".join(
        f"{idx}. {step}"
        for idx, step in enumerate(profile.get("workflow") or [], 1)
        if str(step).strip()
    )
    examples = []
    for phrase in profile.get("trigger_examples") or []:
        phrase = str(phrase or "").strip()
        if phrase:
            examples.append(f"- User: {phrase}")
    examples.append("- User: 請直接處理這個需求並告訴我還缺什麼")
    # Build resource hints (references/ and scripts/ are standard Anthropic pattern)
    resource_note = (
        "If this skill grows large, put detailed docs in `references/` and executable helpers in `scripts/`.\n"
        "Keep SKILL.md under 500 lines — add a table of contents if approaching that limit."
    )

    return f"""---
name: {slug}
description: {description}
metadata:
  author: MAGI Skill Interview
  version: "1.0"
  source: chat_interview
  created: {created}
---

# {display_name}

## Purpose

{purpose}

## Trigger When

{triggers}

## Inputs

{inputs}

## Outputs

{outputs}

## Workflow

{workflow}

## Guardrails

{guardrails}

## Runtime Contract

- Execute with `python3 action.py --task "<user request>"`.
- If critical information is missing, ask for the smallest next clarification before acting.
- Prefer draft, checklist, guidance, or structured output unless the user explicitly requests final form.

## Examples

{os.linesep.join(examples)}

---
> {resource_note}
"""


def _build_action_py(profile: dict) -> str:
    display_name = json.dumps(str(profile.get("display_name") or "Custom Skill"), ensure_ascii=False)
    return f"""#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import json
import os


def _load_profile():
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "skill_profile.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _emit_lines(items, prefix="- "):
    out = []
    for item in items or []:
        text = str(item or "").strip()
        if text:
            out.append(f"{{prefix}}{{text}}")
    return out


def _render(profile, task):
    task = str(task or "").strip() or "help"
    lines = [
        f"✅ 技能「{{profile.get('display_name') or {display_name}}}」已接手。",
        f"目標：{{profile.get('purpose') or ''}}",
        f"目前任務：{{task}}",
        "",
        "建議處理流程：",
    ]
    for idx, step in enumerate(profile.get("workflow") or [], 1):
        step = str(step or "").strip()
        if step:
            lines.append(f"{{idx}}. {{step}}")
    lines.append("")
    lines.append("預期輸入：")
    lines.extend(_emit_lines(profile.get("inputs") or []))
    lines.append("")
    lines.append("預期輸出：")
    lines.extend(_emit_lines(profile.get("outputs") or []))
    lines.append("")
    lines.append("邊界：")
    lines.extend(_emit_lines(profile.get("guardrails") or []))
    return "\n".join(lines).strip()


def main():
    parser = argparse.ArgumentParser(description="Interview-generated MAGI skill")
    parser.add_argument("--task", default="", help="Task text to execute")
    parser.add_argument("task_text", nargs="*", help="Fallback task text")
    args = parser.parse_args()
    task = args.task or " ".join(args.task_text).strip() or "help"
    profile = _load_profile()
    print(_render(profile, task))


if __name__ == "__main__":
    main()
"""


def _build_skill_profile(profile: dict, description: str) -> str:
    payload = dict(profile)
    payload["description"] = description
    payload["engine"] = "magi-skill-interview"
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _generate_evals_template(profile: dict) -> str:
    """Generate evals/evals.json with test prompt templates (Anthropic skill-creator eval loop pattern).

    Each entry has: prompt (user utterance), expected_contains (substrings that must appear),
    expected_not_contains (strings that must NOT appear), and a description.
    """
    display_name = str(profile.get("display_name") or "技能").strip()
    triggers = [str(x).strip() for x in profile.get("trigger_examples") or [] if str(x).strip()]
    outputs = [str(x).strip() for x in profile.get("outputs") or [] if str(x).strip()]

    # Build 3 eval cases: happy path, missing-info path, help/list path
    evals = []

    # Case 1: happy path — use first trigger example as prompt
    happy_prompt = triggers[0] if triggers else f"請執行{display_name}"
    expected_out = outputs[0][:40] if outputs else display_name[:30]
    evals.append({
        "id": "happy_path",
        "description": f"正常觸發「{display_name}」並產出結果",
        "prompt": happy_prompt,
        "expected_contains": [expected_out],
        "expected_not_contains": ["Traceback", "Error", "error"],
        "tags": ["smoke", "happy"],
    })

    # Case 2: second trigger or variant
    if len(triggers) >= 2:
        variant_prompt = triggers[1]
    else:
        variant_prompt = f"幫我{happy_prompt}"
    evals.append({
        "id": "variant_trigger",
        "description": "以不同表達方式觸發相同技能",
        "prompt": variant_prompt,
        "expected_contains": [],
        "expected_not_contains": ["Traceback"],
        "tags": ["smoke"],
    })

    # Case 3: help / info request
    evals.append({
        "id": "help_request",
        "description": "使用者詢問技能功能說明",
        "prompt": f"{display_name} 可以做什麼？",
        "expected_contains": [],
        "expected_not_contains": ["Traceback", "Error"],
        "tags": ["info"],
    })

    payload = {
        "_meta": {
            "skill": profile.get("slug", ""),
            "display_name": display_name,
            "generated_by": "magi-skill-interview",
            "generated_at": datetime.now().isoformat(),
            "eval_format": "v1",
            "instructions": (
                "Run: python action.py --task \"<prompt>\" and verify output "
                "contains expected_contains and does NOT contain expected_not_contains. "
                "Extend this file with domain-specific cases before shipping."
            ),
        },
        "evals": evals,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _refresh_skill_routers() -> dict:
    info = {"embedding_router": "skipped", "semantic_router": "skipped"}
    try:
        from skills.bridge.embedding_router import get_router

        router = get_router()
        rebuilt = router.rebuild_cache() if router.is_ready else router.initialize()
        info["embedding_router"] = "rebuilt" if rebuilt else "init_failed"
    except Exception as e:
        info["embedding_router"] = f"error:{e}"

    try:
        import skills.bridge.semantic_router as semantic_router

        semantic_router._SKILLS_CACHE = None
        semantic_router._SKILLS_CACHE_TS = 0.0
        info["semantic_router"] = "cleared"
    except Exception as e:
        info["semantic_router"] = f"error:{e}"
    return info


def _append_interview_history(event_type: str, payload: dict) -> None:
    try:
        os.makedirs(os.path.dirname(INTERVIEW_HISTORY_FILE), exist_ok=True)
        row = {
            "ts": datetime.now().isoformat(),
            "event": str(event_type or "unknown"),
            "payload": payload or {},
        }
        with open(INTERVIEW_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 428, exc_info=True)


def list_interview_history(limit: int = 20) -> list[dict]:
    if limit <= 0:
        return []
    if not os.path.exists(INTERVIEW_HISTORY_FILE):
        return []
    rows: list[dict] = []
    try:
        with open(INTERVIEW_HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                text = str(line or "").strip()
                if not text:
                    continue
                try:
                    item = json.loads(text)
                except Exception:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except Exception:
        return []
    return rows[-limit:][::-1]


def create_skill_from_interview(initial_request: str, answers: dict, requested_by: str = "") -> dict:
    profile = _normalise_answers(initial_request, answers)
    description = _build_description(profile)
    skill_md = _build_skill_md(profile)
    action_py = _build_action_py(profile)
    profile_json = _build_skill_profile(profile, description)
    combined = "\n\n".join([skill_md, action_py, profile_json])
    safe, violations = validate_skill_safety(combined)
    if not safe:
        _append_interview_history(
            "blocked",
            {
                "requested_by": str(requested_by or "").strip(),
                "initial_request": str(initial_request or "").strip(),
                "display_name": str(profile.get("display_name") or ""),
                "violations": violations[:8],
            },
        )
        return {
            "success": False,
            "error": "IRON_DOME_BLOCKED",
            "violations": violations,
        }

    slug = profile["slug"]
    write_md = _safe_write_skill_file(slug, "SKILL.md", skill_md, reason="generate_skill")
    if not write_md.get("success"):
        _append_interview_history(
            "error",
            {
                "requested_by": str(requested_by or "").strip(),
                "initial_request": str(initial_request or "").strip(),
                "skill_name": slug,
                "stage": "SKILL.md",
                "error": write_md.get("error") or "write_skill_md_failed",
            },
        )
        return {"success": False, "error": write_md.get("error") or "write_skill_md_failed"}

    write_action = _safe_write_skill_file(slug, "action.py", action_py, reason="generate_skill_stub")
    if not write_action.get("success"):
        _append_interview_history(
            "error",
            {
                "requested_by": str(requested_by or "").strip(),
                "initial_request": str(initial_request or "").strip(),
                "skill_name": slug,
                "stage": "action.py",
                "error": write_action.get("error") or "write_action_failed",
            },
        )
        return {"success": False, "error": write_action.get("error") or "write_action_failed"}

    write_profile = _safe_write_skill_file(slug, "skill_profile.json", profile_json, reason="generate_skill_stub")
    if not write_profile.get("success"):
        _append_interview_history(
            "error",
            {
                "requested_by": str(requested_by or "").strip(),
                "initial_request": str(initial_request or "").strip(),
                "skill_name": slug,
                "stage": "skill_profile.json",
                "error": write_profile.get("error") or "write_profile_failed",
            },
        )
        return {"success": False, "error": write_profile.get("error") or "write_profile_failed"}

    # Write evals template (Anthropic eval-loop pattern)
    evals_json = _generate_evals_template(profile)
    evals_dir = os.path.join(SKILLS_DIR, slug, "evals")
    try:
        os.makedirs(evals_dir, exist_ok=True)
        evals_path = os.path.join(evals_dir, "evals.json")
        with open(evals_path, "w", encoding="utf-8") as _ef:
            _ef.write(evals_json)
    except Exception as _ee:
        import logging as _logging
        _logging.getLogger(__name__).warning("skill_interview: evals write failed for %s: %s", slug, _ee)

    definition = _register_skill_tool_definition(slug, description)
    snapshot = _snapshot_skill_version(os.path.join(SKILLS_DIR, slug), reason="interview_generated_initial")
    router_refresh = _refresh_skill_routers()
    ci = run_skill_ci(slug, task="help", attempt_repair=False)
    versions = list_skill_versions(slug)
    _append_interview_history(
        "created",
        {
            "requested_by": str(requested_by or "").strip(),
            "initial_request": str(initial_request or "").strip(),
            "skill_name": slug,
            "display_name": profile["display_name"],
            "description": description,
            "skill_path": os.path.join(SKILLS_DIR, slug, "SKILL.md"),
            "evals_path": os.path.join(SKILLS_DIR, slug, "evals", "evals.json"),
            "trigger_examples": profile.get("trigger_examples") or [],
            "snapshot_version": snapshot.get("version_id") if isinstance(snapshot, dict) else "",
            "ci_ok": bool(ci.get("success")) if isinstance(ci, dict) else False,
            "definition_ok": bool(definition.get("success")) if isinstance(definition, dict) else False,
        },
    )

    return {
        "success": True,
        "skill_name": slug,
        "display_name": profile["display_name"],
        "description": description,
        "skill_dir": os.path.join(SKILLS_DIR, slug),
        "skill_path": os.path.join(SKILLS_DIR, slug, "SKILL.md"),
        "action_path": os.path.join(SKILLS_DIR, slug, "action.py"),
        "profile_path": os.path.join(SKILLS_DIR, slug, "skill_profile.json"),
        "evals_path": os.path.join(SKILLS_DIR, slug, "evals", "evals.json"),
        "definition": definition,
        "snapshot": snapshot,
        "router_refresh": router_refresh,
        "ci": ci,
        "versions": versions,
        "requested_by": str(requested_by or "").strip(),
        "profile": profile,
    }
