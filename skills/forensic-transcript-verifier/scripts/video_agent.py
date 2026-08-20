#!/usr/bin/env python3
"""Autonomous MAGI video-review workflow for court transcript correction."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from audit_engine import (
    TIME_RE,
    UNCERTAINTY_RE,
    Turn,
    audit_question_like_speaker_segments,
    audit_transcript,
    clock_to_seconds,
    extract_text,
    load_asr_segments,
    parse_turns,
    seconds_to_clock,
    sha256_file,
    split_monotonic_blocks,
    validate_docx,
)


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
MAGI_ROOT = SKILL_DIR.parents[1]
DOCX_WRITER = SCRIPT_DIR / "write_transcript_docx.py"
FORBIDDEN_PROVIDER_MARKERS = ("openai", "codex")
REVIEW_REASONS_REQUIRING_TEXT = frozenset(
    {
        "full_turn_review",
        "question_inside_answer",
        "uncertainty_marker",
        "uncovered_asr",
        "short_interjection",
    }
)


def _json_write(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _run(command: list[str], timeout: int = 300, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(env) if env is not None else None,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"returncode": 127, "stdout": "", "stderr": str(exc), "command": command}
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "command": command,
    }


def _json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    if start < 0:
        return {}
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(raw)):
        char = raw[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(raw[start : index + 1])
                    return value if isinstance(value, dict) else {}
                except json.JSONDecodeError:
                    return {}
    return {}


def _model_text(result: Mapping[str, Any]) -> str:
    return str(result.get("analysis") or result.get("response") or result.get("text") or "").strip()


def _is_independent_provider(result: Mapping[str, Any]) -> bool:
    label = " ".join(
        str(result.get(key) or "") for key in ("provider", "route", "model")
    ).lower()
    route = str(result.get("route") or "").strip().lower()
    return (
        not any(marker in label for marker in FORBIDDEN_PROVIDER_MARKERS)
        and route in {"omlx", "local_ollama"}
    )


def _baseline_context(payload: Mapping[str, Any], output_dir: Path) -> dict[str, Any]:
    baseline_text, _ = extract_text(str(payload["baseline"]), output_dir)
    baseline_turns = parse_turns(baseline_text)
    starts = [turn.start for turn in baseline_turns]
    ends = [turn.end for turn in baseline_turns]
    return {
        "text": baseline_text,
        "turns": baseline_turns,
        "start": min(starts) if starts else None,
        "end": max(ends) if ends else None,
        "video_start": (
            clock_to_seconds(str(payload["baseline_video_start"]))
            if payload.get("baseline_video_start")
            else None
        ),
        "time_tokens": frozenset(TIME_RE.findall(baseline_text)),
    }


def _locked_turn_indices(turns: list[Turn], baseline_tokens: Iterable[str]) -> set[int]:
    tokens = tuple(baseline_tokens)
    blocks = split_monotonic_blocks(turns)
    if not blocks or not tokens:
        return set()
    scored = [
        (
            sum(1 for turn in block for token in tokens if token in turn.display),
            block_index,
            block,
        )
        for block_index, block in enumerate(blocks)
    ]
    score, _, selected = max(scored, key=lambda row: (row[0], row[1]))
    if score <= 0:
        return set()
    selected_orders = {turn.order for turn in selected}
    return {
        index
        for index, turn in enumerate(turns)
        if turn.order in selected_orders and any(token in turn.display for token in tokens)
    }


def _closest_turn_index(
    turns: list[Turn],
    second: float,
    *,
    excluded: set[int] | None = None,
) -> int | None:
    best: tuple[float, int] | None = None
    for index, turn in enumerate(turns):
        if excluded and index in excluded:
            continue
        midpoint = turn.start + max(0.0, turn.end - turn.start) / 2.0
        distance = abs(midpoint - second)
        if best is None or distance < best[0]:
            best = (distance, index)
    return best[1] if best is not None else None


def _point(
    point_id: str,
    turn_index: int,
    turn: Turn,
    video_second: float,
    reason: str,
    priority: int,
    nearby_speakers: Iterable[str],
) -> dict[str, Any]:
    return {
        "id": point_id,
        "turn_index": turn_index,
        "display": turn.display,
        "speaker": turn.speaker,
        "text": turn.text,
        "video_second": round(max(0.0, video_second), 3),
        "video_time": seconds_to_clock(video_second),
        "video_start": round(max(0.0, turn.start), 3),
        "video_end": round(max(turn.start, turn.end), 3),
        "reason": reason,
        "reasons": [reason],
        "priority": priority,
        "candidate_speakers": [speaker for speaker in dict.fromkeys(nearby_speakers) if speaker],
    }


def prepare_autonomous_video_review(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Create a bounded, evidence-prioritized visual/audio review plan."""

    for field in ("video", "transcript", "baseline", "asr_json", "output_dir"):
        if not str(payload.get(field) or "").strip():
            raise ValueError(f"autonomous-plan requires {field}")
    output_dir = Path(str(payload["output_dir"])).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    transcript_text, extraction = extract_text(str(payload["transcript"]), output_dir)
    turns = parse_turns(transcript_text)
    if not turns:
        raise ValueError("candidate transcript contains no timestamped turns")
    baseline = _baseline_context(payload, output_dir)
    segments = load_asr_segments(str(payload["asr_json"]))
    locked = _locked_turn_indices(turns, baseline["time_tokens"])
    points: list[dict[str, Any]] = []

    def add(
        index: int,
        second: float,
        reason: str,
        priority: int,
        *,
        evidence_text: str = "",
    ) -> None:
        if not 0 <= index < len(turns):
            return
        nearby = [
            turns[position].speaker
            for position in range(max(0, index - 1), min(len(turns), index + 2))
        ]
        item = _point(
                f"p{len(points) + 1:04d}",
                index,
                turns[index],
                second,
                reason,
                priority,
                nearby,
            )
        item["evidence_text"] = evidence_text or turn.text[:500]
        points.append(item)

    speaker_findings = audit_question_like_speaker_segments(turns, segments)
    for finding in speaker_findings:
        second = (float(finding["start"]) + float(finding["end"])) / 2.0
        index = _closest_turn_index(turns, second, excluded=locked)
        if index is not None:
            add(
                index,
                second,
                "question_inside_answer",
                100,
                evidence_text=str(finding.get("text") or ""),
            )

    for index, turn in enumerate(turns):
        if index in locked:
            continue
        mapped = turn.start
        middle = mapped + max(0.0, turn.end - turn.start) / 2.0
        add(index, middle, "full_turn_review", 70)
        duration = max(0.0, turn.end - turn.start)
        if duration > 8.0:
            sweep_second = turn.start + 4.0
            while sweep_second < turn.end:
                nearby_asr = " ".join(
                    str(segment.get("text") or "").strip()
                    for segment in segments
                    if float(segment.get("end", segment.get("start", 0)) or 0) >= sweep_second - 4.0
                    and float(segment.get("start", 0) or 0) <= sweep_second + 4.0
                ).strip()
                add(
                    index,
                    sweep_second,
                    "speaker_sweep",
                    65,
                    evidence_text=nearby_asr[:500],
                )
                sweep_second += 8.0
        if UNCERTAINTY_RE.search(turn.text):
            add(index, middle, "uncertainty_marker", 95)
        if turn.end - turn.start <= 3.0:
            add(index, middle, "short_interjection", 75)
        if index and turn.speaker != turns[index - 1].speaker:
            add(index, mapped + min(0.8, max(0.15, (turn.end - turn.start) / 2.0)), "speaker_boundary", 60)

    deduped: list[dict[str, Any]] = []
    by_location: dict[tuple[int, int], dict[str, Any]] = {}
    for item in sorted(points, key=lambda row: (-row["priority"], row["video_second"], row["id"])):
        key = (int(item["turn_index"]), round(float(item["video_second"]) * 10))
        existing = by_location.get(key)
        if existing is not None:
            reasons = existing.setdefault("reasons", [existing["reason"]])
            if item["reason"] not in reasons:
                reasons.append(item["reason"])
            if item.get("evidence_text") and not existing.get("evidence_text"):
                existing["evidence_text"] = item["evidence_text"]
            continue
        by_location[key] = item
        deduped.append(item)
    maximum = max(1, min(5000, int(payload.get("max_visual_reviews", 2000))))
    selected = sorted(deduped[:maximum], key=lambda row: (row["video_second"], -row["priority"]))
    for number, item in enumerate(selected, start=1):
        item["id"] = f"p{number:04d}"
        item["baseline_locked"] = int(item["turn_index"]) in locked

    result = {
        "success": True,
        "operation": "autonomous-plan",
        "video": str(Path(str(payload["video"])).expanduser().resolve()),
        "transcript": str(Path(str(payload["transcript"])).expanduser().resolve()),
        "turns": len(turns),
        "asr_segments": len(segments),
        "baseline_locked_turns": len(locked),
        "speaker_review_findings": len(speaker_findings),
        "review_points_total": len(deduped),
        "review_points_selected": len(selected),
        "timeline_complete": len(selected) == len(deduped),
        "review_points": selected,
        "extraction": extraction,
    }
    result["report"] = _json_write(output_dir / "autonomous-plan.json", result)
    return result


def extract_contact_sheet(
    video: str | Path,
    second: float,
    output: str | Path,
    *,
    pass_number: int,
) -> dict[str, Any]:
    """Extract five chronological frames around one utterance into a contact sheet."""

    source = Path(video).expanduser().resolve()
    target = Path(output).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not source.is_file():
        return {"success": False, "error": f"video missing: {source}"}
    if not shutil.which("ffmpeg"):
        return {"success": False, "error": "ffmpeg not found"}
    radius = 0.9 if pass_number == 1 else 1.3
    duration = radius * 2.0
    start = max(0.0, float(second) - radius)
    result = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-i",
            str(source),
            "-t",
            f"{duration:.3f}",
            "-vf",
            "fps=5/1.8,scale=420:-2,tile=5x1:padding=4:margin=4:color=white",
            "-frames:v",
            "1",
            str(target),
        ],
        timeout=90,
    )
    return {
        "success": result["returncode"] == 0 and target.is_file(),
        "path": str(target) if target.is_file() else "",
        "start": round(start, 3),
        "end": round(start + duration, 3),
        "pass": pass_number,
        "error": result["stderr"] if result["returncode"] else "",
    }


def extract_contact_sheet_batch(
    video: str | Path,
    points: list[Mapping[str, Any]],
    output: str | Path,
    *,
    pass_number: int,
) -> dict[str, Any]:
    """Render several same-turn five-frame rows in one ffmpeg subprocess."""

    source = Path(video).expanduser().resolve()
    target = Path(output).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not source.is_file():
        return {"success": False, "error": f"video missing: {source}"}
    if not points or len(points) > 4:
        return {"success": False, "error": "visual batch size must be in [1, 4]"}
    if not shutil.which("ffmpeg"):
        return {"success": False, "error": "ffmpeg not found"}
    radius = 0.9 if pass_number == 1 else 1.3
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    rows: list[str] = []
    windows: list[dict[str, Any]] = []
    for index, point in enumerate(points):
        second = float(point.get("video_second", 0) or 0)
        start = max(0.0, second - radius)
        duration = radius * 2.0
        command.extend(["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source)])
        rows.append(
            f"[{index}:v]fps=5/1.8,scale=420:-2,"
            f"tile=5x1:padding=4:margin=4:color=white[row{index}]"
        )
        windows.append(
            {
                "point_id": str(point.get("id") or ""),
                "start": round(start, 3),
                "end": round(start + duration, 3),
            }
        )
    if len(points) == 1:
        rows.append("[row0]null[out]")
    else:
        labels = "".join(f"[row{index}]" for index in range(len(points)))
        rows.append(f"{labels}vstack=inputs={len(points)}[out]")
    command.extend(
        ["-filter_complex", ";".join(rows), "-map", "[out]", "-frames:v", "1", str(target)]
    )
    result = _run(command, timeout=max(90, 45 * len(points)))
    return {
        "success": result["returncode"] == 0 and target.is_file(),
        "path": str(target) if target.is_file() else "",
        "pass": pass_number,
        "point_ids": [str(point.get("id") or "") for point in points],
        "windows": windows,
        "subprocesses": 1,
        "error": result["stderr"] if result["returncode"] else "",
    }


def _vision_prompt(point: Mapping[str, Any], pass_number: int) -> str:
    candidates = "、".join(point.get("candidate_speakers") or []) or "未知"
    ordering = "先只看人物動作，再參考對話角色" if pass_number == 1 else "先檢查反證與其他可能發話者，再下結論"
    return f"""你是臺灣法院影音勘驗員。這是同一時間點由左至右排列的五張連續畫面。
{ordering}。判斷畫面中哪個位置的人最可能正在發話；嘴部、頭部、身體與麥克風方向必須優先於語意猜測。
候選發話者：{candidates}
現有草稿標籤：{point.get('speaker') or '空白'}
該時間點附近的 ASR／草稿內容：{point.get('evidence_text') or point.get('text') or ''}
如果畫面無法支持身分，uncertain 必須為 true，speaker 必須填「未定」。不得假裝看到嘴部動作。
只輸出 JSON，不要說明：
{{"speaker":"候選名稱或未定","active_position":"左/中/右/畫外/未定","confidence":0.0,"uncertain":true,"visible_evidence":"可觀察的動作","contrary_evidence":"反證或空字串"}}"""


def _vision_batch_prompt(points: list[Mapping[str, Any]], pass_number: int) -> str:
    ordering = "先逐列看人物動作，再參考角色" if pass_number == 1 else "逐列先找反證與其他可能發話者，再下結論"
    rows = [
        {
            "id": point.get("id"),
            "time": point.get("video_time"),
            "draft_speaker": point.get("speaker"),
            "candidate_speakers": point.get("candidate_speakers") or [],
            "nearby_text": point.get("evidence_text") or point.get("text") or "",
        }
        for point in points
    ]
    return f"""你是臺灣法院影音勘驗員。影像由上到下每一列對應一個觀察點，每列由左到右五張連續畫面。
{ordering}。每列必須各自判斷，不能以同一結論覆蓋整批；看不清就標未定，不得猜測。
觀察點 JSON：
{json.dumps(rows, ensure_ascii=False)}
只輸出 JSON：
{{"decisions":[{{"id":"觀察點id","speaker":"候選名稱或未定","active_position":"左/中/右/畫外/未定","confidence":0.0,"uncertain":true,"visible_evidence":"可觀察動作","contrary_evidence":"反證或空字串"}}]}}"""


def _call_vision(gateway: Any, image_path: str, prompt: str) -> tuple[dict[str, Any], dict[str, Any]]:
    direct_omlx = getattr(gateway, "_omlx_vision", None)
    direct_local = getattr(gateway, "_local_vision", None)
    if callable(direct_omlx):
        raw = direct_omlx(image_path, prompt, timeout=90, task_type="vision")
        if not raw.get("success") and callable(direct_local):
            raw = direct_local(
                image_path,
                prompt,
                timeout=90,
                task_type="vision",
            )
    elif callable(direct_local):
        raw = direct_local(
            image_path,
            prompt,
            timeout=90,
            task_type="vision",
        )
    else:
        return {}, {
            "success": False,
            "error": "court review requires an explicit local-only vision adapter",
        }
    if not isinstance(raw, Mapping):
        return {}, {"success": False, "error": "vision returned non-object"}
    independent = _is_independent_provider(raw)
    parsed = _json_object(_model_text(raw)) if raw.get("success") and independent else {}
    return parsed, {
        "success": bool(raw.get("success")) and independent and bool(parsed),
        "route": raw.get("route", ""),
        "provider": raw.get("provider", ""),
        "model": raw.get("model", ""),
        "error": (
            "forbidden external assistant provider"
            if raw.get("success") and not independent
            else str(raw.get("error") or ("invalid JSON" if raw.get("success") and not parsed else ""))
        ),
    }


def _call_vision_batch(
    gateway: Any,
    image_path: str,
    points: list[Mapping[str, Any]],
    pass_number: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    parsed, model = _call_vision(
        gateway,
        image_path,
        _vision_batch_prompt(points, pass_number),
    )
    rows = parsed.get("decisions") if isinstance(parsed, Mapping) else None
    expected = {str(point.get("id") or "") for point in points}
    decisions = {
        str(row.get("id") or ""): dict(row)
        for row in (rows or [])
        if isinstance(row, Mapping) and str(row.get("id") or "") in expected
    }
    if set(decisions) != expected:
        model = {**model, "success": False, "error": "visual batch response omitted point ids"}
        return {}, model
    return decisions, model


def _default_gateway() -> Any:
    from skills.bridge.inference_gateway import InferenceGateway

    return InferenceGateway()


def _observation_pass(
    payload: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    pass_number: int,
    gateway: Any,
    frame_extractor: Callable[..., dict[str, Any]],
    batch_frame_extractor: Callable[..., dict[str, Any]] | None,
    output_dir: Path,
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    frames_dir = output_dir / f"visual-pass-{pass_number}"
    points = list(plan.get("review_points") or [])
    batches: list[list[Mapping[str, Any]]] = []
    by_turn: dict[int, list[Mapping[str, Any]]] = {}
    for point in points:
        by_turn.setdefault(int(point.get("turn_index", -1)), []).append(point)
    for turn_points in by_turn.values():
        for start in range(0, len(turn_points), 4):
            batches.append(turn_points[start : start + 4])
    # Custom single-frame extractors are retained for deterministic fixture
    # tests. Production always receives the batched extractor.
    if batch_frame_extractor is None:
        batches = [[point] for point in points]
    for batch_number, batch in enumerate(batches, start=1):
        if len(batch) == 1 and batch_frame_extractor is None:
            point = batch[0]
            sheet_path = frames_dir / f"{point['id']}.jpg"
            frame = frame_extractor(
                str(payload["video"]),
                float(point["video_second"]),
                sheet_path,
                pass_number=pass_number,
            )
            decision: dict[str, Any] = {}
            model: dict[str, Any] = {"success": False, "error": "contact sheet unavailable"}
            if frame.get("success"):
                decision, model = _call_vision(
                    gateway,
                    str(frame["path"]),
                    _vision_prompt(point, pass_number),
                )
            observations.append({"point": point, "frame": frame, "decision": decision, "model": model})
            continue
        sheet_path = frames_dir / f"batch-{batch_number:04d}.jpg"
        frame = batch_frame_extractor(
            str(payload["video"]),
            batch,
            sheet_path,
            pass_number=pass_number,
        )
        decisions: dict[str, dict[str, Any]] = {}
        model = {"success": False, "error": "batched contact sheet unavailable"}
        if frame.get("success"):
            decisions, model = _call_vision_batch(
                gateway, str(frame["path"]), batch, pass_number
            )
        for point in batch:
            observations.append(
                {
                    "point": point,
                    "frame": frame,
                    "decision": decisions.get(str(point.get("id") or ""), {}),
                    "model": {**model, "batch_size": len(batch), "batch_number": batch_number},
                }
            )
    _json_write(output_dir / f"visual-pass-{pass_number}.json", observations)
    return observations


def reconcile_visual_observations(
    first: list[Mapping[str, Any]],
    second: list[Mapping[str, Any]],
    *,
    minimum_confidence: float = 0.72,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Accept a speaker only when both independently sampled visual passes agree."""

    second_by_id = {
        str((row.get("point") or {}).get("id") or ""): row for row in second
    }
    accepted: dict[str, str] = {}
    unresolved: list[dict[str, Any]] = []
    for row in first:
        point = row.get("point") or {}
        point_id = str(point.get("id") or "")
        other = second_by_id.get(point_id) or {}
        a = row.get("decision") or {}
        b = other.get("decision") or {}
        speaker_a = str(a.get("speaker") or "").strip()
        speaker_b = str(b.get("speaker") or "").strip()
        candidates = set(point.get("candidate_speakers") or [])
        confidence_a = float(a.get("confidence", 0) or 0)
        confidence_b = float(b.get("confidence", 0) or 0)
        agrees = (
            speaker_a
            and speaker_a == speaker_b
            and speaker_a != "未定"
            and speaker_a in candidates
            and not bool(a.get("uncertain"))
            and not bool(b.get("uncertain"))
            and confidence_a >= minimum_confidence
            and confidence_b >= minimum_confidence
        )
        if agrees:
            accepted[point_id] = speaker_a
            continue
        unresolved.append(
            {
                "time": point.get("video_time", ""),
                "speaker": point.get("speaker", ""),
                "content": point.get("text", ""),
                "reason": "兩次畫面觀察未形成高信心一致結論",
                "point_id": point_id,
                "turn_index": point.get("turn_index"),
                "pass_1": a,
                "pass_2": b,
            }
        )
    return accepted, unresolved


def _compact_visual_unresolved(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, str], dict[str, Any]] = {}
    for row in rows:
        key = (row.get("turn_index"), str(row.get("reason") or ""))
        item = grouped.setdefault(
            key,
            {
                "time": row.get("time", ""),
                "speaker": row.get("speaker", ""),
                "content": row.get("content", ""),
                "reason": row.get("reason", ""),
                "turn_index": row.get("turn_index"),
                "point_ids": [],
            },
        )
        if row.get("point_id"):
            item["point_ids"].append(row["point_id"])
    for item in grouped.values():
        count = len(item["point_ids"])
        if count > 1:
            item["reason"] = f"{item['reason']}（本段 {count} 個觀察點）"
    return list(grouped.values())


def _segments_for_point(
    segments: list[Mapping[str, Any]],
    point: Mapping[str, Any],
    radius: float = 4.0,
) -> str:
    reasons = set(point.get("reasons") or [point.get("reason")])
    if "full_turn_review" in reasons:
        left = float(point.get("video_start", point.get("video_second", 0)) or 0) - 1.0
        right = float(point.get("video_end", point.get("video_second", 0)) or 0) + 1.0
    else:
        second = float(point.get("video_second", 0) or 0)
        left, right = second - radius, second + radius
    rows = []
    for segment in segments:
        start = float(segment.get("start", 0) or 0)
        end = float(segment.get("end", start) or start)
        if end >= left and start <= right:
            rows.append(
                f"[{seconds_to_clock(start)}–{seconds_to_clock(end)}] {str(segment.get('text') or '').strip()}"
            )
    return "\n".join(rows)


def _text_prompt(
    point: Mapping[str, Any],
    primary_asr: str,
    secondary_asr: str,
    visual_speaker: str,
    *,
    pass_number: int,
) -> str:
    method = "先逐字比較兩路 ASR，再看草稿" if pass_number == 1 else "先找兩路 ASR 的衝突與草稿反證，再決定是否修改"
    return f"""你是臺灣檢察署訊問影音逐字稿複核員。{method}。
只有兩路證據一致支持時才能修改。不得用語意補寫未聽到的字；不得刪除否認、同意、數字、姓名、停頓或程序性發話。
畫面兩輪一致發話者：{visual_speaker or '未形成一致'}
草稿時間：{point.get('display')}
草稿發話者：{point.get('speaker')}
草稿文字：{point.get('text')}
ASR 路一：
{primary_asr or '無'}
ASR 路二：
{secondary_asr or '無'}
若證據不足，change_required=false，text 必須原樣回傳。只輸出 JSON：
{{"change_required":false,"speaker":"原標籤或新標籤","text":"完整文字","confidence":0.0,"reason":"證據理由"}}"""


def _call_chat(gateway: Any, prompt: str) -> tuple[dict[str, Any], dict[str, Any]]:
    direct_omlx = getattr(gateway, "_omlx_chat", None)
    direct_local = getattr(gateway, "_local_chat", None)
    if callable(direct_omlx):
        raw = direct_omlx(prompt, timeout=120, task_type="legal_analysis")
        if not raw.get("success") and callable(direct_local):
            raw = direct_local(prompt, timeout=120)
    elif callable(direct_local):
        raw = direct_local(prompt, timeout=120)
    else:
        return {}, {
            "success": False,
            "error": "court review requires an explicit local-only text adapter",
        }
    if not isinstance(raw, Mapping):
        return {}, {"success": False, "error": "chat returned non-object"}
    independent = _is_independent_provider(raw)
    parsed = _json_object(_model_text(raw)) if raw.get("success") and independent else {}
    return parsed, {
        "success": bool(raw.get("success")) and independent and bool(parsed),
        "route": raw.get("route", ""),
        "provider": raw.get("provider", ""),
        "model": raw.get("model", ""),
        "error": (
            "forbidden external assistant provider"
            if raw.get("success") and not independent
            else str(raw.get("error") or ("invalid JSON" if raw.get("success") and not parsed else ""))
        ),
    }


def _review_text_points(
    plan: Mapping[str, Any],
    primary_segments: list[dict[str, Any]],
    secondary_segments: list[dict[str, Any]],
    visual_speakers: Mapping[str, str],
    *,
    gateway: Any,
    output_dir: Path,
) -> tuple[dict[int, dict[str, str]], list[dict[str, Any]], list[dict[str, Any]]]:
    proposals: list[dict[str, Any]] = []
    accepted: dict[int, dict[str, str]] = {}
    unresolved: list[dict[str, Any]] = []
    grouped: dict[int, list[Mapping[str, Any]]] = {}
    relevant_turns: set[int] = set()
    for point in plan.get("review_points") or []:
        if point.get("baseline_locked"):
            continue
        turn_index = int(point["turn_index"])
        grouped.setdefault(turn_index, []).append(point)
        reasons = set(point.get("reasons") or [point.get("reason")])
        if reasons.intersection(REVIEW_REASONS_REQUIRING_TEXT):
            relevant_turns.add(turn_index)
    for turn_index in sorted(relevant_turns):
        points = grouped[turn_index]
        point = dict(points[0])
        point["reasons"] = sorted(
            {
                str(reason)
                for row in points
                for reason in (row.get("reasons") or [row.get("reason")])
                if reason
            }
        )
        speakers = {
            visual_speakers.get(str(row.get("id") or ""), "") for row in points
        } - {""}
        visual_speaker = next(iter(speakers)) if len(speakers) == 1 else ""
        primary = _segments_for_point(primary_segments, point)
        secondary = _segments_for_point(secondary_segments, point)
        a, model_a = _call_chat(
            gateway,
            _text_prompt(point, primary, secondary, visual_speaker, pass_number=1),
        )
        b, model_b = _call_chat(
            gateway,
            _text_prompt(point, primary, secondary, visual_speaker, pass_number=2),
        )
        record = {
            "point": point,
            "point_ids": [str(row.get("id") or "") for row in points],
            "covered_review_points": len(points),
            "pass_1": a,
            "pass_2": b,
            "model_1": model_a,
            "model_2": model_b,
        }
        proposals.append(record)
        draft = str(point.get("text") or "")
        text_a = str(a.get("text") or "").strip()
        text_b = str(b.get("text") or "").strip()
        speaker_a = str(a.get("speaker") or point.get("speaker") or "").strip()
        speaker_b = str(b.get("speaker") or point.get("speaker") or "").strip()
        change_a = bool(a.get("change_required"))
        change_b = bool(b.get("change_required"))
        confidence_a = float(a.get("confidence", 0) or 0)
        confidence_b = float(b.get("confidence", 0) or 0)
        evidence_complete = bool(primary.strip()) and bool(secondary.strip())
        safe_length = len(text_a) >= max(1, int(len(draft) * 0.65))
        agrees = (
            evidence_complete
            and change_a == change_b
            and text_a == text_b
            and speaker_a == speaker_b
            and confidence_a >= 0.82
            and confidence_b >= 0.82
            and safe_length
        )
        if agrees and change_a:
            accepted[turn_index] = {"speaker": speaker_a, "text": text_a}
        elif change_a or change_b or not evidence_complete:
            unresolved.append(
                {
                    "time": point.get("video_time", ""),
                    "speaker": point.get("speaker", ""),
                    "content": draft,
                    "reason": "兩次文字／ASR 複核未形成可安全套用的一致修正",
                    "point_ids": record["point_ids"],
                }
            )
    _json_write(output_dir / "text-review-proposals.json", proposals)
    return accepted, unresolved, proposals


def merge_consecutive_same_speaker(
    turns: list[Turn],
    *,
    locked_indices: set[int],
    maximum_gap: float = 1.5,
) -> list[Turn]:
    """Semantically merge only adjacent same-speaker turns without dropping words."""

    if not turns:
        return []
    output: list[tuple[Turn, set[int]]] = []
    for index, turn in enumerate(turns):
        if not output:
            output.append((turn, {index}))
            continue
        previous, source_indices = output[-1]
        can_merge = (
            previous.speaker
            and previous.speaker == turn.speaker
            and index not in locked_indices
            and not (source_indices & locked_indices)
            and -0.25 <= turn.start - previous.end <= maximum_gap
            and turn.start >= previous.start
        )
        if not can_merge:
            output.append((turn, {index}))
            continue
        separator = "" if previous.text.endswith(("。", "？", "！", "…", "；")) else "，"
        merged = Turn(
            display=f"{seconds_to_clock(previous.start)}–{seconds_to_clock(turn.end)}",
            speaker=previous.speaker,
            text=f"{previous.text}{separator}{turn.text}",
            start=previous.start,
            end=max(previous.end, turn.end),
            order=previous.order,
        )
        output[-1] = (merged, source_indices | {index})
    return [turn for turn, _ in output]


def _apply_corrections(
    turns: list[Turn],
    plan: Mapping[str, Any],
    visual: Mapping[str, str],
    textual: Mapping[int, Mapping[str, str]],
    *,
    locked: set[int],
) -> tuple[list[Turn], list[dict[str, Any]], list[dict[str, Any]]]:
    by_point = {str(row["id"]): row for row in plan.get("review_points") or []}
    planned_by_index: dict[int, set[str]] = {}
    accepted_by_index: dict[int, dict[str, str]] = {}
    for point in plan.get("review_points") or []:
        if point.get("baseline_locked"):
            continue
        planned_by_index.setdefault(int(point["turn_index"]), set()).add(str(point["id"]))
    for point_id, speaker in visual.items():
        point = by_point.get(point_id)
        if point is None or point.get("baseline_locked"):
            continue
        accepted_by_index.setdefault(int(point["turn_index"]), {})[point_id] = speaker
    speaker_by_index: dict[int, str] = {}
    conflicts: list[dict[str, Any]] = []
    for index, decisions in accepted_by_index.items():
        speakers = set(decisions.values())
        planned = planned_by_index.get(index, set())
        if len(speakers) == 1 and set(decisions) == planned:
            speaker_by_index[index] = next(iter(speakers))
            continue
        if any(speaker != turns[index].speaker for speaker in speakers):
            conflicts.append(
                {
                    "time": turns[index].display,
                    "speaker": turns[index].speaker,
                    "content": turns[index].text,
                    "reason": "同一發話段的完整畫面掃描未一致支持單一發話者，未自動改標",
                    "turn_index": index,
                    "accepted_visual_points": decisions,
                    "planned_visual_points": len(planned),
                }
            )
    changes: list[dict[str, Any]] = []
    output: list[Turn] = []
    for index, turn in enumerate(turns):
        if index in locked:
            output.append(turn)
            continue
        proposed = textual.get(index) or {}
        speaker = str(speaker_by_index.get(index) or turn.speaker)
        proposed_speaker = str(proposed.get("speaker") or turn.speaker)
        if proposed and proposed_speaker != speaker:
            text = turn.text
            conflicts.append(
                {
                    "time": turn.display,
                    "speaker": turn.speaker,
                    "content": turn.text,
                    "reason": "文字修正所含發話者與完整畫面共識不一致，未自動套用",
                    "turn_index": index,
                }
            )
        else:
            text = str(proposed.get("text") or turn.text)
        updated = Turn(turn.display, speaker, text, turn.start, turn.end, turn.order)
        if updated.speaker != turn.speaker or updated.text != turn.text:
            changes.append(
                {
                    "turn_index": index,
                    "time": turn.display,
                    "before_speaker": turn.speaker,
                    "after_speaker": updated.speaker,
                    "before_text": turn.text,
                    "after_text": updated.text,
                }
            )
        output.append(updated)
    return output, changes, conflicts


def _extract_audio(video: Path, output: Path, *, filtered: bool) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
    ]
    if filtered:
        command.extend(["-af", "highpass=f=75,lowpass=f=7800,afftdn=nf=-25"])
    command.append(str(output))
    result = _run(command, timeout=1800)
    return {
        "success": result["returncode"] == 0 and output.is_file(),
        "path": str(output) if output.is_file() else "",
        "error": result["stderr"] if result["returncode"] else "",
    }


def _runtime_expected_sha256(name: str) -> str:
    value = str(os.environ.get(name) or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RuntimeError(f"{name} missing or invalid")
    return value


def _runtime_content_evidence(path: Path, expected_env: str) -> dict[str, Any]:
    if not path.is_absolute() or not path.is_file():
        raise RuntimeError(f"offline ASR artifact missing: {path}")
    expected = _runtime_expected_sha256(expected_env)
    actual = sha256_file(path)
    if actual != expected:
        raise RuntimeError(f"offline ASR artifact SHA mismatch: {path}")
    return {"path": str(path), "size": path.stat().st_size, "sha256": actual}


def _runtime_asr_evidence(role: str, backend_id: str, model: str) -> dict[str, Any]:
    """Revalidate deployment-bound ASR artifacts immediately before inference."""

    role = role.upper()
    configured_backend = str(
        os.environ.get(f"MAGI_FORENSIC_{role}_ASR_BACKEND") or ""
    ).strip().lower()
    if configured_backend != backend_id:
        raise RuntimeError(f"{role} ASR backend does not match the bound deployment")
    configured_model = Path(
        str(os.environ.get(f"MAGI_FORENSIC_{role}_ASR_MODEL") or "")
    ).expanduser()
    if not configured_model.is_absolute() or not configured_model.exists():
        raise RuntimeError(f"{role} ASR model must be an existing absolute path")
    license_id = str(os.environ.get(f"MAGI_FORENSIC_{role}_MODEL_LICENSE") or "").strip()
    if license_id != "MIT":
        raise RuntimeError(f"{role} ASR model license is missing or not approved")
    requested = Path(str(model or "")).expanduser()
    if requested.is_absolute() and requested.resolve() != configured_model.resolve():
        raise RuntimeError(f"{role} ASR requested model differs from bound deployment")

    if backend_id == "mlx_whisper":
        if not configured_model.is_dir():
            raise RuntimeError("primary MLX model must be a local snapshot directory")
        config = (configured_model / "config.json").resolve()
        weights = [
            path.resolve()
            for path in (
                configured_model / "weights.safetensors",
                configured_model / "weights.npz",
            )
            if path.is_file()
        ]
        if len(weights) != 1 or not config.is_file():
            raise RuntimeError("primary MLX snapshot is incomplete")
        binary = Path(
            str(os.environ.get("MAGI_FORENSIC_PRIMARY_BACKEND_BINARY") or "")
        ).expanduser().resolve()
        evidence = {
            "backend_id": backend_id,
            "model_id": str(configured_model.resolve()),
            "license_id": license_id,
            "weights": _runtime_content_evidence(
                weights[0], "MAGI_FORENSIC_PRIMARY_MODEL_WEIGHTS_SHA256"
            ),
            "config": _runtime_content_evidence(
                config, "MAGI_FORENSIC_PRIMARY_MODEL_CONFIG_SHA256"
            ),
            "backend_binary": _runtime_content_evidence(
                binary, "MAGI_FORENSIC_PRIMARY_BACKEND_BINARY_SHA256"
            ),
        }
    elif backend_id == "whisper_cli":
        if not configured_model.is_file():
            raise RuntimeError("secondary Whisper model must be one local checkpoint")
        model_dir = Path(str(os.environ.get("MAGI_WHISPER_MODEL_DIR") or "")).expanduser()
        if not model_dir.is_absolute() or not model_dir.is_dir() or model_dir.resolve() not in (
            configured_model.resolve().parent,
            *configured_model.resolve().parents,
        ):
            raise RuntimeError("secondary Whisper model escaped its bound model directory")
        binary = Path(str(os.environ.get("MAGI_WHISPER_BIN") or "")).expanduser().resolve()
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise RuntimeError("secondary Whisper binary is missing or not executable")
        evidence = {
            "backend_id": backend_id,
            "model_id": str(configured_model.resolve()),
            "model_dir": str(model_dir.resolve()),
            "license_id": license_id,
            "weights": _runtime_content_evidence(
                configured_model.resolve(),
                "MAGI_FORENSIC_SECONDARY_MODEL_WEIGHTS_SHA256",
            ),
            "config": {"mode": "embedded_in_checkpoint"},
            "backend_binary": _runtime_content_evidence(
                binary, "MAGI_FORENSIC_SECONDARY_BACKEND_BINARY_SHA256"
            ),
        }
    else:
        raise RuntimeError(f"unsupported bound local ASR backend: {backend_id}")
    evidence["content_binding_sha256"] = hashlib.sha256(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return evidence


def _preinstalled_mlx_model(requested: str = "") -> Path:
    raw = str(
        requested
        or os.environ.get("MAGI_FORENSIC_MLX_MODEL_PATH")
        or ""
    ).strip()
    if not raw:
        raise RuntimeError("preinstalled MLX ASR model path is not configured")
    path = Path(raw).expanduser()
    if not path.is_absolute() or not path.exists():
        raise RuntimeError(f"preinstalled MLX ASR model is unavailable: {path}")
    return path.resolve()


def _asr_document(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _segment_content_sha256(segments: Iterable[Mapping[str, Any]]) -> str:
    normalized = [
        {
            "start": round(float(row.get("start", 0) or 0), 3),
            "end": round(float(row.get("end", row.get("start", 0)) or 0), 3),
            "text": re.sub(r"\s+", "", str(row.get("text") or "")),
        }
        for row in segments
    ]
    return hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _asr_provenance(path: Path, segments: list[dict[str, Any]]) -> dict[str, Any]:
    document = _asr_document(path)
    raw = document.get("forensic_provenance")
    provenance = dict(raw) if isinstance(raw, Mapping) else {}
    provenance["artifact_sha256"] = sha256_file(path) if path.is_file() else ""
    provenance["segment_content_sha256"] = _segment_content_sha256(segments)
    return provenance


def _secondary_asr_independence(
    primary: Mapping[str, Any], secondary: Mapping[str, Any]
) -> tuple[bool, list[str]]:
    required = (
        "backend_id",
        "model_id",
        "run_id",
        "audio_sha256",
        "install_identity_sha256",
        "model_artifact_sha256",
        "backend_binary_sha256",
        "license_id",
        "execution_ordinal",
    )
    reasons = [
        f"primary_missing_{field}" for field in required if not str(primary.get(field) or "").strip()
    ] + [
        f"secondary_missing_{field}"
        for field in required
        if not str(secondary.get(field) or "").strip()
    ]
    if primary.get("offline") is not True:
        reasons.append("primary_not_offline")
    if secondary.get("offline") is not True:
        reasons.append("secondary_not_offline")
    comparisons = (
        ("artifact_sha256", "same_artifact_content"),
        ("segment_content_sha256", "same_transcript_content"),
        ("backend_id", "same_backend"),
        ("model_id", "same_model"),
        ("install_identity_sha256", "same_model_install"),
        ("model_artifact_sha256", "same_model_artifact"),
        ("backend_binary_sha256", "same_backend_binary"),
        ("run_id", "same_run"),
        ("audio_sha256", "same_audio_variant"),
    )
    for field, reason in comparisons:
        if primary.get(field) and primary.get(field) == secondary.get(field):
            reasons.append(reason)
    return not reasons, reasons


def _transcribe_to_json(
    video: Path,
    output_dir: Path,
    *,
    filtered: bool,
    backend: str = "",
    model: str = "",
) -> tuple[str, list[dict[str, Any]]]:
    role = "SECONDARY" if filtered else "PRIMARY"
    backend_id = str(
        backend
        or os.environ.get(f"MAGI_FORENSIC_{role}_ASR_BACKEND")
        or ("whisper_cli" if filtered else "mlx_whisper")
    ).strip().lower()
    requested_model = str(
        model or os.environ.get(f"MAGI_FORENSIC_{role}_ASR_MODEL") or ""
    ).strip()
    runtime_evidence = _runtime_asr_evidence(role, backend_id, requested_model)
    audio = output_dir / ("audio-filtered.wav" if filtered else "audio-primary.wav")
    extracted = _extract_audio(video, audio, filtered=filtered)
    if not extracted["success"]:
        raise RuntimeError(f"audio extraction failed: {extracted['error']}")
    result: Mapping[str, Any]
    model_path: Path
    if backend_id == "mlx_whisper":
        from skills.hearing import balthasar_local

        model_path = _preinstalled_mlx_model(
            requested_model
        )
        try:
            result = balthasar_local.transcribe_audio(
                str(audio),
                model_path=str(model_path),
                language="zh",
                taigi_hint=True,
            )
        except Exception as exc:
            result = {"success": False, "error": f"mlx_whisper_unavailable:{exc}"}
    elif backend_id == "whisper_cli":
        from skills.bridge.balthasar_bridge import _transcribe_with_whisper_cli

        requested = requested_model
        result = _transcribe_with_whisper_cli(str(audio), language="zh", model=requested)
        model_path = Path(str((result or {}).get("model") or requested)).expanduser().resolve()
    else:
        raise RuntimeError(f"unsupported local ASR backend: {backend_id}")
    if not isinstance(result, Mapping) or not result.get("success"):
        raise RuntimeError(f"local ASR failed: {str((result or {}).get('error') or 'unknown')}")
    target = output_dir / ("asr-secondary.json" if filtered else "asr-primary.json")
    payload = dict(result)
    payload["audio_source"] = str(audio)
    payload["forensic_provenance"] = {
        "backend_id": backend_id,
        "model_id": str(model_path),
        "install_identity_sha256": runtime_evidence["content_binding_sha256"],
        "model_artifact_sha256": runtime_evidence["weights"]["sha256"],
        "backend_binary_sha256": runtime_evidence["backend_binary"]["sha256"],
        "license_id": runtime_evidence["license_id"],
        "model_evidence": runtime_evidence,
        "run_id": uuid.uuid4().hex,
        "execution_ordinal": 2 if filtered else 1,
        "execution_mode": "serialized",
        "audio_sha256": sha256_file(audio),
        "source_video_sha256": sha256_file(video),
        "filter_chain": "highpass75_lowpass7800_afftdn-25" if filtered else "pcm16k_mono",
        "offline": True,
        "generated_at_ns": time.time_ns(),
    }
    _json_write(target, payload)
    if not target.is_file():
        raise RuntimeError(f"local ASR evidence JSON was not persisted: {target}")
    segments = load_asr_segments(target)
    if not segments:
        raise RuntimeError(f"local ASR evidence JSON contains no timestamped segments: {target}")
    return str(target), segments


def write_court_docx(
    turns: list[Turn],
    unresolved: list[Mapping[str, Any]],
    *,
    output_path: Path,
    title: str,
    case_info: str,
) -> dict[str, Any]:
    payload = {
        "output_path": str(output_path),
        "title": title,
        "header": title,
        "case_info": case_info,
        "turns": [asdict(turn) for turn in turns],
        "unresolved": list(unresolved),
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as stream:
        json.dump(payload, stream, ensure_ascii=False)
        task_path = Path(stream.name)
    try:
        result = _run(
            [sys.executable, str(DOCX_WRITER), str(task_path)],
            timeout=300,
            env=dict(os.environ),
        )
    finally:
        task_path.unlink(missing_ok=True)
    return {
        "success": result["returncode"] == 0 and output_path.is_file(),
        "output": str(output_path) if output_path.is_file() else "",
        "turns": len(turns),
        "error": result["stderr"] if result["returncode"] else "",
    }


def _audit_projection(result: Mapping[str, Any]) -> dict[str, Any]:
    baseline = result.get("baseline") or {}
    coverage = result.get("coverage") or {}
    return {
        "turns": result.get("turns"),
        "monotonic_blocks": result.get("monotonic_blocks"),
        "overlaps": result.get("overlaps"),
        "uncertainty_markers": result.get("uncertainty_markers"),
        "baseline": {
            "baseline_entries": baseline.get("baseline_entries"),
            "matched_entries": baseline.get("matched_entries"),
            "exact": baseline.get("exact"),
            "mismatches": baseline.get("mismatches"),
        },
        "coverage": {
            "selected_segments": coverage.get("selected_segments"),
            "uncovered_segments": coverage.get("uncovered_segments"),
            "uncovered_groups": coverage.get("uncovered_groups"),
        },
        "speaker_review": result.get("speaker_review"),
        "passed": result.get("passed_deterministic_gates"),
    }


def run_autonomous_video_review(
    payload: Mapping[str, Any],
    *,
    gateway: Any | None = None,
    frame_extractor: Callable[..., dict[str, Any]] = extract_contact_sheet,
    batch_frame_extractor: Callable[..., dict[str, Any]] | None = extract_contact_sheet_batch,
) -> dict[str, Any]:
    """Run MAGI's full local video/audio/speaker/transcript/DOCX agent workflow."""

    for field in ("video", "transcript", "baseline", "output_dir"):
        if not str(payload.get(field) or "").strip():
            raise ValueError(f"autonomous requires {field}")
    output_dir = Path(str(payload["output_dir"])).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    video = Path(str(payload["video"])).expanduser().resolve()
    transcript = Path(str(payload["transcript"])).expanduser().resolve()
    if not video.is_file() or not transcript.is_file():
        raise FileNotFoundError("autonomous video or transcript is missing")

    primary_path = str(payload.get("asr_json") or "").strip()
    secondary_supplied = str(payload.get("secondary_asr_json") or "").strip()
    if payload.get("require_generated_dual_asr") and (primary_path or secondary_supplied):
        raise RuntimeError("V3 court workflow requires freshly generated bound dual ASR")
    if primary_path:
        primary_segments = load_asr_segments(primary_path)
    else:
        primary_path, primary_segments = _transcribe_to_json(
            video, output_dir, filtered=False
        )
    secondary_path = secondary_supplied
    secondary_error = ""
    secondary_generated_from_filtered_audio = False
    require_secondary = bool(payload.get("require_secondary_asr", True))
    if secondary_path:
        secondary_segments = load_asr_segments(secondary_path)
    elif require_secondary:
        try:
            secondary_path, secondary_segments = _transcribe_to_json(video, output_dir, filtered=True)
            secondary_generated_from_filtered_audio = True
        except Exception as exc:
            secondary_segments = []
            secondary_error = str(exc)
    else:
        secondary_segments = list(primary_segments)
        secondary_path = primary_path

    primary_artifact = Path(primary_path).expanduser().resolve()
    secondary_artifact = Path(secondary_path).expanduser().resolve() if secondary_path else None
    if not primary_artifact.is_file() or not primary_segments:
        raise RuntimeError("第一路 ASR 證據檔不存在或沒有時間戳 segments")
    if require_secondary and (
        secondary_artifact is None
        or not secondary_artifact.is_file()
        or not secondary_segments
    ):
        detail = f"：{secondary_error}" if secondary_error else ""
        raise RuntimeError(f"第二路 ASR 證據檔不存在或沒有時間戳 segments{detail}")
    primary_provenance = _asr_provenance(primary_artifact, primary_segments)
    secondary_provenance = (
        _asr_provenance(secondary_artifact, secondary_segments)
        if secondary_artifact is not None and secondary_artifact.is_file()
        else {}
    )
    secondary_independent, secondary_independence_failures = _secondary_asr_independence(
        primary_provenance, secondary_provenance
    )
    if require_secondary and not secondary_independent:
        raise RuntimeError(
            "第二路 ASR 未形成法院級獨立證據："
            + ", ".join(secondary_independence_failures)
        )

    effective_payload = dict(payload)
    effective_payload["asr_json"] = primary_path
    plan = prepare_autonomous_video_review(effective_payload)
    gateway = gateway or _default_gateway()
    first = _observation_pass(
        effective_payload,
        plan,
        pass_number=1,
        gateway=gateway,
        frame_extractor=frame_extractor,
        batch_frame_extractor=(
            batch_frame_extractor if frame_extractor is extract_contact_sheet else None
        ),
        output_dir=output_dir,
    )
    second = _observation_pass(
        effective_payload,
        plan,
        pass_number=2,
        gateway=gateway,
        frame_extractor=frame_extractor,
        batch_frame_extractor=(
            batch_frame_extractor if frame_extractor is extract_contact_sheet else None
        ),
        output_dir=output_dir,
    )
    visual_speakers, visual_unresolved = reconcile_visual_observations(first, second)
    _json_write(output_dir / "visual-unresolved-full.json", visual_unresolved)

    candidate_text, _ = extract_text(transcript, output_dir)
    turns = parse_turns(candidate_text)
    baseline = _baseline_context(effective_payload, output_dir)
    locked = _locked_turn_indices(turns, baseline["time_tokens"])
    textual, text_unresolved, proposals = _review_text_points(
        plan,
        primary_segments,
        secondary_segments,
        visual_speakers,
        gateway=gateway,
        output_dir=output_dir,
    )
    corrected, changes, correction_conflicts = _apply_corrections(
        turns,
        plan,
        visual_speakers,
        textual,
        locked=locked,
    )
    merged = merge_consecutive_same_speaker(
        corrected,
        locked_indices=locked,
        maximum_gap=float(payload.get("semantic_merge_gap", 1.5)),
    )

    unresolved = _compact_visual_unresolved(visual_unresolved) + text_unresolved + correction_conflicts
    for turn in merged:
        for marker in UNCERTAINTY_RE.findall(turn.text):
            unresolved.append(
                {
                    "time": turn.display,
                    "speaker": turn.speaker,
                    "content": marker,
                    "reason": "原譯文仍含明示不確定標記，法院送件前須確認",
                }
            )
    if secondary_error:
        unresolved.append(
            {
                "time": "全片",
                "speaker": "",
                "content": "第二路 ASR 未完成",
                "reason": secondary_error,
            }
        )

    output_name = str(payload.get("output_docx") or f"{transcript.stem}_MAGI自主雙重勘驗版.docx")
    output_docx = Path(output_name)
    if not output_docx.is_absolute():
        output_docx = output_dir / output_docx.name
    output_docx = output_docx.expanduser().resolve()
    if output_docx == transcript:
        raise ValueError("autonomous output must not overwrite the source transcript")
    writer = write_court_docx(
        merged,
        unresolved,
        output_path=output_docx,
        title=str(payload.get("title") or "訊問影音完整譯文（MAGI 自主雙重勘驗版）"),
        case_info=(
            f"來源影音：{video.name}；兩輪畫面複核點：{plan['review_points_selected']}；"
            f"套用修正：{len(changes)}；未決項目：{len(unresolved)}。"
        ),
    )
    if not writer["success"]:
        raise RuntimeError(f"DOCX generation failed: {writer['error']}")

    audit_payload = {
        **effective_payload,
        "transcript": str(output_docx),
        "output_dir": str(output_dir / "readback-pass-1"),
    }
    audit_1 = audit_transcript(audit_payload)
    audit_payload["output_dir"] = str(output_dir)
    audit_2 = audit_transcript(audit_payload)
    validated = validate_docx(
        {**effective_payload, "transcript": str(output_docx), "output_dir": str(output_dir)},
        magi_root=MAGI_ROOT,
    )
    audit_consistent = _audit_projection(audit_1) == _audit_projection(audit_2)
    model_calls = [
        row.get("model") or {}
        for row in first + second
    ] + [
        record.get(key) or {}
        for record in proposals
        for key in ("model_1", "model_2")
    ]
    independent = bool(model_calls) and all(_is_independent_provider(item) for item in model_calls)
    visual_execution_complete = bool(first) and len(first) == len(second) and all(
        bool((row.get("model") or {}).get("success")) for row in first + second
    )
    text_execution_complete = all(
        bool((record.get(key) or {}).get("success"))
        for record in proposals
        for key in ("model_1", "model_2")
    )
    model_execution_complete = visual_execution_complete and text_execution_complete
    secondary_artifact_present = bool(
        secondary_artifact is not None
        and secondary_artifact.is_file()
        and secondary_segments
    )
    secondary_provenance_verified = bool(
        secondary_independent and not secondary_independence_failures
    )
    technical_passed = bool(
        independent
        and model_execution_complete
        and not secondary_error
        and (secondary_independent or not require_secondary)
        and plan.get("timeline_complete")
        and audit_1.get("passed_deterministic_gates")
        and audit_2.get("passed_deterministic_gates")
        and audit_consistent
        and validated.get("passed")
    )
    result = {
        "success": True,
        "operation": "autonomous",
        "independent_of_codex": independent,
        "local_model_execution_complete": model_execution_complete,
        "video_observation_passes": 2,
        "dual_asr_execution": "serialized",
        "asr_sources": [primary_path, secondary_path],
        "asr_evidence": {
            "primary": {
                "path": str(primary_artifact),
                "sha256": sha256_file(primary_artifact),
                "segments": len(primary_segments),
                "provenance": primary_provenance,
            },
            "secondary": {
                "path": str(secondary_artifact) if secondary_artifact else "",
                "sha256": sha256_file(secondary_artifact) if secondary_artifact_present else "",
                "segments": len(secondary_segments),
                "provenance": secondary_provenance,
            },
        },
        "secondary_asr_error": secondary_error,
        "secondary_asr_independent": secondary_independent,
        "secondary_asr_artifact_present": secondary_artifact_present,
        "secondary_asr_generated_from_filtered_audio": secondary_generated_from_filtered_audio,
        "secondary_asr_provenance_verified": secondary_provenance_verified,
        "secondary_asr_independence_failures": secondary_independence_failures,
        "timeline_complete": bool(plan.get("timeline_complete")),
        "review_points": plan["review_points_selected"],
        "visual_batch_count_pass_1": len(
            {(row.get("model") or {}).get("batch_number") for row in first}
        ),
        "visual_batch_count_pass_2": len(
            {(row.get("model") or {}).get("batch_number") for row in second}
        ),
        "text_review_turn_batches": len(proposals),
        "visual_consensus": len(visual_speakers),
        "speaker_or_text_changes": changes,
        "turns_before": len(turns),
        "turns_after_semantic_merge": len(merged),
        "baseline_locked_turns": len(locked),
        "unresolved": unresolved,
        "output_docx": str(output_docx),
        "audit_pass_1": audit_1,
        "audit_pass_2": audit_2,
        "audit_consistent": audit_consistent,
        "docx_validation": validated,
        "passed": technical_passed,
        "court_grade_contract_satisfied": bool(
            technical_passed
            and require_secondary
            and secondary_independent
            and secondary_provenance_verified
        ),
        "unresolved_count": len(unresolved),
        "legal_filing_authorized": False,
        "human_final_confirmation_required": True,
        "artifacts": [
            str(output_docx),
            str(output_dir / "autonomous.json"),
            str(output_dir / "visual-pass-1.json"),
            str(output_dir / "visual-pass-2.json"),
            (audit_1.get("reports") or {}).get("json", ""),
            (audit_2.get("reports") or {}).get("json", ""),
            validated.get("pdf", ""),
        ],
    }
    _json_write(output_dir / "corrections.json", changes)
    _json_write(output_dir / "unresolved.json", unresolved)
    _json_write(output_dir / "autonomous.json", result)
    return result
