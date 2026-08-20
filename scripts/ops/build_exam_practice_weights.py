#!/usr/bin/env python3
"""Build locked practice weights for official essay rubrics without point details.

The grading model never runs this code and never chooses points.  This is an
offline maintenance compiler: it reads the already archived official question
totals and official rubric excerpts, creates a deterministic allocation, then
writes the reviewed allocation file shipped with the release.  Hand-reviewed
entries are preserved verbatim and take precedence over compiled entries.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BANK = REPO_ROOT / "static" / "exam_tutor" / "essay_bank.json"
DEFAULT_OUTPUT = REPO_ROOT / "static" / "exam_tutor" / "curated_practice_weights.json"
STATUS_CREDIT = {"covered": 1.0, "partial": 0.5, "missing": 0.0, "incorrect": 0.0}
APPLICATION_CUES = ("本案", "題示", "準此", "因此", "故", "結論", "應認", "有無理由", "是否有理由")
VIEW_CUES = ("實務", "學說", "見解", "爭議", "肯定說", "否定說", "另有見解", "不同見解")
CORE_CUES = ("核心", "關鍵", "主要", "應考人應", "必須", "不得", "應先")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).replace("|", "").strip()


def _is_structure(issue: dict[str, Any]) -> bool:
    text = _clean(issue.get("issue") or issue.get("rule"))
    if not text:
        return True
    if re.search(r"第\s*[一二三四五六七八九十數\d]+\s*題\s*評分要點", text):
        return True
    if len(text) <= 48 and text.startswith(("─", "—", "【")):
        return True
    if len(text) <= 32 and text.endswith(("如下:", "如下：", "之理由:", "之理由：")):
        return True
    if len(text) <= 22 and text in {
        "相關大法官解釋之見解:", "相關大法官解釋之見解：", "本案之認定", "公司之抗辯",
    }:
        return True
    return False


def _importance_weight(issue: dict[str, Any]) -> float:
    text = _clean(issue.get("rule") or issue.get("official_excerpt") or issue.get("issue"))
    length = max(1, len(text))
    weight = 1.0 + min(2.75, math.log2(length + 1) / 3.0)
    if any(cue in text for cue in APPLICATION_CUES):
        weight += 1.1
    if any(cue in text for cue in VIEW_CUES):
        weight += 0.9
    if any(cue in text for cue in CORE_CUES):
        weight += 0.9
    return weight


def _partition(issues: list[dict[str, Any]], parts: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Contiguously align ordered official excerpts to ordered official parts."""
    if len(parts) == 1:
        return [issues]
    if len(issues) < len(parts):
        return [issues]
    masses = [min(1600.0, max(30.0, float(len(_clean(item.get("rule") or item.get("issue")))))) for item in issues]
    total_mass = sum(masses)
    total_points = sum(_number(part.get("max_score")) for part in parts)
    targets = [total_mass * _number(part.get("max_score")) / total_points for part in parts]
    prefix = [0.0]
    for mass in masses:
        prefix.append(prefix[-1] + mass)
    count = len(issues)
    part_count = len(parts)
    infinity = float("inf")
    dp = [[infinity] * (count + 1) for _ in range(part_count + 1)]
    back = [[-1] * (count + 1) for _ in range(part_count + 1)]
    dp[0][0] = 0.0
    for part_index in range(1, part_count + 1):
        target = max(1.0, targets[part_index - 1])
        for end in range(part_index, count - (part_count - part_index) + 1):
            for start in range(part_index - 1, end):
                if dp[part_index - 1][start] == infinity:
                    continue
                mass = prefix[end] - prefix[start]
                cost = ((mass - target) / target) ** 2
                candidate = dp[part_index - 1][start] + cost
                if candidate < dp[part_index][end]:
                    dp[part_index][end] = candidate
                    back[part_index][end] = start
    groups: list[list[dict[str, Any]]] = []
    end = count
    for part_index in range(part_count, 0, -1):
        start = back[part_index][end]
        if start < 0:
            raise RuntimeError("無法將官方爭點對應至官方小題")
        groups.append(issues[start:end])
        end = start
    return list(reversed(groups))


def _allocate(group: list[dict[str, Any]], max_score: float) -> dict[str, dict[str, Any]]:
    resolution = 0.25
    total_units = int(round(max_score / resolution))
    structure_ids = {str(item.get("id")) for item in group if _is_structure(item)}
    substantive = [item for item in group if str(item.get("id")) not in structure_ids]
    if not substantive:
        substantive = list(group)
        structure_ids.clear()
    if len(substantive) > total_units:
        raise RuntimeError("爭點數量超過可用配分單位")
    weights = [_importance_weight(item) for item in substantive]
    remaining = total_units - len(substantive)
    weight_total = sum(weights)
    raw_extra = [remaining * weight / weight_total for weight in weights]
    extra_units = [int(math.floor(value)) for value in raw_extra]
    leftovers = remaining - sum(extra_units)
    order = sorted(
        range(len(substantive)),
        key=lambda index: (raw_extra[index] - extra_units[index], weights[index], -index),
        reverse=True,
    )
    for index in order[:leftovers]:
        extra_units[index] += 1
    points = [(1 + extra_units[index]) * resolution for index in range(len(substantive))]
    median = statistics.median(points)
    average = sum(points) / len(points)
    classifications = []
    for point in points:
        if point >= max(median * 1.3, average * 1.18):
            classifications.append("major")
        elif point <= min(median * 0.78, average * 0.82):
            classifications.append("minor")
        else:
            classifications.append("medium")
    classifications[max(range(len(points)), key=points.__getitem__)] = "major"
    if len(points) >= 3:
        classifications[min(range(len(points)), key=points.__getitem__)] = "minor"

    allocated: dict[str, dict[str, Any]] = {}
    substantive_index = 0
    for item in group:
        issue_id = str(item.get("id") or "")
        if issue_id in structure_ids:
            allocated[issue_id] = {
                "points": 0,
                "importance": "structure",
                "rationale": "官方評分要點的純章節標題，不與展開的實質爭點重複計分。",
            }
            continue
        importance = classifications[substantive_index]
        rationale = {
            "major": "官方原文包含核心規範、分歧見解或本案涵攝，為本小題主要得分點。",
            "medium": "官方原文所列的實質規範或涵攝項目。",
            "minor": "官方原文所列的定義、問題定位或輔助論證。",
        }[importance]
        allocated[issue_id] = {
            "points": points[substantive_index],
            "importance": importance,
            "rationale": rationale,
        }
        substantive_index += 1
    return allocated


def _compile_entry(entry: dict[str, Any], *, curated_at: str) -> dict[str, Any]:
    uid = str(entry.get("uid") or "")
    max_score = _number(entry.get("max_score"), -1)
    parts = [dict(item) for item in entry.get("score_parts") or [] if isinstance(item, dict)]
    issues = [dict(item) for item in (entry.get("stored_rubric") or {}).get("issues") or [] if isinstance(item, dict)]
    if not uid or max_score <= 0 or not parts or not issues:
        raise RuntimeError(f"題庫缺少官方小題配分或官方爭點：{uid}")
    if abs(sum(_number(item.get("max_score")) for item in parts) - max_score) > 0.01:
        raise RuntimeError(f"官方小題配分加總不一致：{uid}")
    issue_ids = [str(item.get("id") or "") for item in issues]
    if not all(issue_ids) or len(set(issue_ids)) != len(issue_ids):
        raise RuntimeError(f"官方爭點 ID 不完整：{uid}")

    mapping_mode = "official_parts_contiguous"
    scoring_parts = parts
    if len(issues) < len(parts):
        mapping_mode = "whole_question_due_to_source_segmentation"
        scoring_parts = [{
            "id": "TOTAL",
            "label": "全題固定練習配分（官方小題總分另列）",
            "max_score": max_score,
            "official_source": "官方題卷本題總分",
        }]
    groups = _partition(issues, scoring_parts)
    allocations: dict[str, dict[str, Any]] = {}
    for part, group in zip(scoring_parts, groups):
        part_id = str(part.get("id") or "")
        part_allocations = _allocate(group, _number(part.get("max_score")))
        for issue_id, allocation in part_allocations.items():
            allocations[issue_id] = {"part_id": part_id, **allocation}
    if set(allocations) != set(issue_ids):
        raise RuntimeError(f"固定練習配分尺未覆蓋全部官方爭點：{uid}")
    for part in scoring_parts:
        part_id = str(part.get("id") or "")
        total = sum(_number(item.get("points")) for item in allocations.values() if item.get("part_id") == part_id)
        if abs(total - _number(part.get("max_score"))) > 0.001:
            raise RuntimeError(f"固定練習配分尺小題加總錯誤：{uid}/{part_id}")
    compiled_parts = []
    for part in scoring_parts:
        normalized = dict(part)
        normalized.setdefault("official_source", "官方題卷")
        compiled_parts.append(normalized)
    return {
        "version": "2026-08-12.2",
        "curated_at": curated_at,
        "curator": "exam_rubric_maintenance",
        "curation_method": "deterministic_offline_official_rubric_compilation",
        "source_basis": "考選部官方題目小題配分與封存官方評分要點原文。",
        "official_issue_points_published": False,
        "part_allocation_mode": mapping_mode,
        "official_score_parts": parts,
        "total_points": max_score,
        "parts": compiled_parts,
        "issues": allocations,
    }


def build(bank: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    previous_entries = previous.get("entries") if isinstance(previous.get("entries"), dict) else {}
    bank_generated_at = str(bank.get("generated_at") or "未記載")
    entries: dict[str, dict[str, Any]] = {}
    for entry in bank.get("entries") or []:
        if not isinstance(entry, dict) or entry.get("rubric_basis") != "official":
            continue
        official = (entry.get("stored_rubric") or {}).get("official_rubric") or {}
        if official.get("numeric_scoring_available"):
            continue
        uid = str(entry.get("uid") or "")
        prior = previous_entries.get(uid)
        if isinstance(prior, dict) and prior.get("curation_method") == "manual_issue_review":
            entries[uid] = prior
        else:
            entries[uid] = _compile_entry(entry, curated_at=bank_generated_at)
    return {
        "schema_version": 2,
        "generated_at": bank_generated_at,
        "policy": "考選部明列小題總分、但未明列逐爭點數字時，依封存官方評分要點事先整理題內練習權重並鎖定存檔；MAGI 批改模型不得新增、刪除或調整權重。",
        "status_credit": STATUS_CREDIT,
        "coverage": {
            "official_without_issue_points": len(entries),
            "manual_issue_review": sum(item.get("curation_method") == "manual_issue_review" for item in entries.values()),
            "offline_compiled": sum(item.get("curation_method") != "manual_issue_review" for item in entries.values()),
        },
        "entries": dict(sorted(entries.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bank", type=Path, default=DEFAULT_BANK)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--previous", type=Path, help="existing reviewed overlay to preserve")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    bank = json.loads(args.bank.read_text(encoding="utf-8"))
    previous_path = args.previous or args.output
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.is_file() else {}
    payload = build(bank, previous)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
            raise SystemExit("固定練習配分尺需要重新產生")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".partial")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps(payload["coverage"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
