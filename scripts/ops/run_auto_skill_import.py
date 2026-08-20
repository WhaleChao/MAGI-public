#!/usr/bin/env python3
"""Daily, measurable and fail-closed MAGI self-evolution entrypoint.

The upstream Auto-Skill repository is only one candidate knowledge source.  A
successful fetch is never reported as an improvement by itself.  The daily
receipt separates newly accepted knowledge, measurable capability delta and
candidate-only repair proposals.  Source proposals can never deploy from this
job.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TARGET_GAIN_PERCENT = 1.0


def _runtime_dir() -> Path:
    configured = str(os.environ.get("MAGI_RUNTIME_DIR") or "").strip()
    return Path(configured).expanduser().resolve() if configured else (ROOT / ".runtime").resolve()


def _receipt_path(runtime_dir: Path) -> Path:
    return runtime_dir / "daily_self_evolution_latest.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _toolsai_count(entries: list[dict[str, Any]]) -> int:
    return sum(
        1
        for entry in entries
        if str(entry.get("context") or "").startswith("toolsai-auto-skill")
    )


def _fresh_payload(path: Path, *, max_age_hours: float = 24.0) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    raw = str(payload.get("generated_at") or "").strip()
    try:
        generated = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=datetime.now().astimezone().tzinfo)
        if datetime.now(timezone.utc) - generated.astimezone(timezone.utc) > timedelta(hours=max_age_hours):
            return {}
    except Exception:
        return {}
    return payload


def _open_guardian_signals(runtime_dir: Path) -> list[dict[str, Any]]:
    path = runtime_dir / "magi_self_repair_guardian_latest.json"
    payload = _fresh_payload(path)
    signals: list[dict[str, Any]] = []
    for issue in payload.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        severity = str(issue.get("severity") or "").lower()
        status = str(issue.get("status") or "").lower()
        if severity not in {"warning", "error", "critical"}:
            continue
        if status in {"resolved", "closed", "observed", "archived"}:
            continue
        signals.append(issue)
    return signals


def _open_business_signals(runtime_dir: Path) -> list[dict[str, Any]]:
    """Turn fresh failed business checks into aggregate-only evolution signals."""

    payload = _fresh_payload(runtime_dir / "business_module_live_check_latest.json")
    results = payload.get("results") if isinstance(payload.get("results"), dict) else {}
    signals: list[dict[str, Any]] = []
    for check_id, check in results.items():
        if not isinstance(check, dict) or check.get("ok") is True:
            continue
        parsed = check.get("parsed") if isinstance(check.get("parsed"), dict) else {}
        # Only fixed identifiers and reason codes enter controlled evolution;
        # paths, samples, exception text and case data are never forwarded.
        reason_code = str(parsed.get("reason") or check.get("error") or "business_check_failed")
        reason_code = reason_code.split(",", 1)[0].strip()[:80]
        signals.append(
            {
                "id": f"business:{check_id}",
                "source": "business_module_live_check",
                "category": str(check_id)[:80],
                "severity": "error",
                "status": "open",
                "reason_code": reason_code,
                "summary": str(check_id),
            }
        )
    return signals


def _plan_controlled_candidates(runtime_dir: Path) -> dict[str, Any]:
    """Create only de-identified candidate plans; never edit or deploy source."""

    signals = _open_guardian_signals(runtime_dir) + _open_business_signals(runtime_dir)
    if not signals:
        return {
            "ok": True,
            "new_proposal_count": 0,
            "open_proposal_count": 0,
            "proposal_ids": [],
            "auto_deploy": False,
        }
    try:
        from magi_v3.controlled_evolution import EvolutionStore, ingest_signals

        store = EvolutionStore(runtime_dir / "controlled-evolution" / "evolution.sqlite3")
        release_id = str(os.environ.get("MAGI_V3_RELEASE_ID") or ROOT.name)
        existing_ids = {
            str(item.get("proposal_id") or "")
            for item in store.list(limit=500)
        }
        proposals = ingest_signals(signals, root=ROOT, release_id=release_id, store=store)
        proposal_ids = [str(item.get("proposal_id") or "") for item in proposals]
        return {
            "ok": True,
            "new_proposal_count": sum(
                1 for proposal_id in proposal_ids if proposal_id not in existing_ids
            ),
            "open_proposal_count": len(proposals),
            "proposal_ids": proposal_ids,
            "auto_deploy": False,
        }
    except Exception as exc:
        return {
            "ok": False,
            "new_proposal_count": 0,
            "open_proposal_count": 0,
            "proposal_ids": [],
            "auto_deploy": False,
            "reason_code": f"proposal_store_{type(exc).__name__.lower()}",
        }


def _summary_text(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    proposals = report["controlled_candidates"]
    status_text = {
        "improved": "已完成可量測改善",
        "candidate_planned": "已建立受控改善候選，尚未部署",
        "no_measurable_change": "今日沒有新的可採用改善",
        "failed": "本輪未通過品質閘門",
    }.get(str(report.get("status") or ""), "狀態待確認")
    target = "達成" if metrics["target_met"] else "未達；不冒充成功"
    return (
        "MAGI 每日受控自我進化\n"
        f"結果：{status_text}\n"
        f"能力知識：{metrics['knowledge_before']} → {metrics['knowledge_after']}（新增 {metrics['knowledge_new']}）\n"
        f"量測增幅：{metrics['capability_gain_percent']:.2f}%／目標 {TARGET_GAIN_PERCENT:.2f}%（{target}）\n"
        f"受控修復候選：新增 {proposals['new_proposal_count']}／待處理 {proposals['open_proposal_count']}"
        "（只建立候選，絕不自動部署）\n"
        f"來源檔案：{metrics['source_files_checked']}；略過：{metrics['source_files_skipped']}\n"
        f"下一步：{report['next_action']}\n"
        f"時間：{report['generated_at']}"
    )


def _notify(report: dict[str, Any]) -> dict[str, Any]:
    try:
        from skills.ops.red_phone import alert_admin

        return alert_admin(_summary_text(report), severity="info", topic_key="nightly")
    except Exception as exc:
        return {"delivered": False, "reason_code": f"notify_{type(exc).__name__.lower()}"}


def run_daily_evolution(*, local_path: str = "", notify: bool = True) -> dict[str, Any]:
    from skills.management.auto_skill import AutoSkill

    runtime_dir = _runtime_dir()
    engine = AutoSkill()
    before_entries = list(engine.knowledge)
    before_count = len(before_entries)
    toolsai_before = _toolsai_count(before_entries)

    try:
        imported = engine.import_toolsai_auto_skill(
            local_path=local_path,
            cleanup=not bool(local_path),
            notify_dc=False,
        )
    except Exception as exc:
        imported = {"success": False, "reason_code": f"import_{type(exc).__name__.lower()}"}

    after_entries = list(engine.knowledge)
    after_count = len(after_entries)
    learned = max(0, int(imported.get("learned") or 0)) if imported.get("success") else 0
    knowledge_new = max(0, after_count - before_count)
    # The persisted count is authoritative.  Never report a larger gain merely
    # because an importer returned an optimistic number.
    knowledge_new = min(knowledge_new, learned)
    gain_percent = round((knowledge_new / max(1, before_count)) * 100.0, 4)
    imported_files = imported.get("imported_files") if isinstance(imported.get("imported_files"), list) else []
    skipped_files = imported.get("skipped") if isinstance(imported.get("skipped"), list) else []
    vector = imported.get("vector_mirror") if isinstance(imported.get("vector_mirror"), dict) else {}
    import_ok = imported.get("success") is True
    vector_ok = vector.get("success") is not False
    candidates = _plan_controlled_candidates(runtime_dir)

    if not import_ok or not vector_ok or not candidates.get("ok"):
        status = "failed"
        next_action = "保留既有能力；下一輪重新驗證失敗的來源或候選收據"
    elif knowledge_new > 0:
        status = "improved"
        next_action = "持續以實際測試與使用結果驗證新知識，不自動改動正式程式"
    elif int(candidates.get("new_proposal_count") or 0) > 0:
        status = "candidate_planned"
        next_action = "在隔離工作區產生並測試候選，通過後仍須走正式發行流程"
    else:
        status = "no_measurable_change"
        next_action = "上游內容已吸收；改查內部品質訊號，不重複貼來源連結"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report: dict[str, Any] = {
        "schema_version": 1,
        "ok": status != "failed",
        "success": status != "failed",
        "status": status,
        "generated_at": now,
        "release_id": str(os.environ.get("MAGI_V3_RELEASE_ID") or ROOT.name),
        "target": {
            "name": "daily_measurable_capability_gain",
            "percent": TARGET_GAIN_PERCENT,
            "guaranteed": False,
            "honest_zero_required": True,
        },
        "metrics": {
            "knowledge_before": before_count,
            "knowledge_after": after_count,
            "knowledge_new": knowledge_new,
            "toolsai_knowledge_before": toolsai_before,
            "toolsai_knowledge_after": _toolsai_count(after_entries),
            "capability_gain_percent": gain_percent,
            "target_met": gain_percent >= TARGET_GAIN_PERCENT,
            "source_files_checked": len(imported_files),
            "source_files_skipped": len(skipped_files),
            "vector_mirror_ok": vector_ok,
            "vector_mirror_mode": (
                "disabled"
                if str(vector.get("message") or "").strip().lower() == "vector mirroring disabled"
                else "completed"
            ),
        },
        "controlled_candidates": candidates,
        "source_policy": {
            "external_repository_is_reference_only": True,
            "repository_url_in_notification": False,
            "new_content_requires_safety_validation": True,
        },
        "deployment_policy": {
            "auto_deploy": False,
            "immutable_release_required": True,
            "live_validation_required": True,
        },
        "next_action": next_action,
    }
    _atomic_write_json(_receipt_path(runtime_dir), report)
    if notify:
        report["notification"] = _notify(report)
    return report


def main() -> int:
    local_path = str(os.environ.get("MAGI_AUTOSKILL_IMPORT_LOCAL_PATH") or "").strip()
    notifications_disabled = str(os.environ.get("MAGI_DISABLE_NOTIFICATIONS") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    try:
        result = run_daily_evolution(local_path=local_path, notify=not notifications_disabled)
    except Exception as exc:
        result = {
            "schema_version": 1,
            "ok": False,
            "success": False,
            "status": "failed",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "reason_code": f"daily_evolution_{type(exc).__name__.lower()}",
        }
        _atomic_write_json(_receipt_path(_runtime_dir()), result)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("success") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
