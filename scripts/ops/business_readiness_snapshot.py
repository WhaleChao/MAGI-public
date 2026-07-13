#!/usr/bin/env python3
"""Build a public-safe snapshot of work that can still block MAGI operations."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable


ROOT = Path(os.environ.get("MAGI_ROOT_DIR") or Path(__file__).resolve().parents[2]).expanduser()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _recent_report_failures(root: Path, *, now: datetime, days: int = 7) -> dict:
    path = root / ".runtime" / "laf_report_jobs.jsonl"
    if not path.exists():
        return {"count": 0, "reasons": {}}
    latest_by_case: dict[str, dict] = {}
    cutoff = now - timedelta(days=max(1, days))
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines()[-3000:]:
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if not isinstance(item, dict) or item.get("status") not in {"ok", "failed"}:
            continue
        ts = _parse_time(item.get("ts"))
        if ts is None or ts < cutoff:
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        identity = result.get("identity") if isinstance(result.get("identity"), dict) else {}
        case_key = str(
            identity.get("case_number")
            or identity.get("laf_case_number")
            or item.get("job_id")
            or ""
        ).strip()
        if case_key:
            latest_by_case[case_key] = item
    failures = [
        item
        for item in latest_by_case.values()
        if item.get("status") == "failed" and not _report_failure_is_now_resolved(item)
    ]
    reasons = Counter()
    for item in failures:
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        reason = str(result.get("error") or "unknown").strip()
        reasons["missing_required_docs" if reason == "missing_required_docs" else "other"] += 1
    return {"count": len(failures), "reasons": dict(reasons)}


def _report_failure_is_now_resolved(item: dict) -> bool:
    """Re-check document failures so an old log cannot keep the UI red."""
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    if str(result.get("error") or "") != "missing_required_docs":
        return False
    identity = result.get("identity") if isinstance(result.get("identity"), dict) else {}
    case_folder = str(identity.get("case_folder") or "").strip()
    if not case_folder or not os.path.isdir(case_folder):
        return False
    try:
        from casper_ecosystem.law_firm_orchestrators.laf_orchestrator_docmixins import (
            LAFOrchestratorDocumentMixin,
        )

        docs = LAFOrchestratorDocumentMixin()._scan_case_folder_docs(case_folder, action="closing")
        return bool(docs.get("closing_basis_files") or docs.get("mediation_success_files"))
    except Exception:
        return False


def _latest_file_review_job(root: Path) -> dict:
    jobs_dir = root / "skills" / "file-review-orchestrator" / "_bg_jobs"
    try:
        latest = max(jobs_dir.glob("download_*.json"), key=lambda path: path.stat().st_mtime)
    except (OSError, ValueError):
        return {}
    return _load_json(latest)


def _operations(exec_fn: Callable | None) -> dict:
    if exec_fn is None:
        try:
            from api.blueprints.osc_cases import _osc_exec
            from api.osc.saas_workbench import build_operations_report

            return build_operations_report(_osc_exec)
        except Exception:
            return {}
    try:
        from api.osc.saas_workbench import build_operations_report

        return build_operations_report(exec_fn)
    except Exception:
        return {}


def _business_item_text(value: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _closing_pending_items(operations: dict) -> list[dict]:
    items = []
    for row in operations.get("closing_pending_items") or []:
        if not isinstance(row, dict):
            continue
        items.append(
            {
                "case_number": _business_item_text(row.get("case_number"), 40),
                "client_name": _business_item_text(row.get("client_name"), 80),
                "status": _business_item_text(row.get("legal_aid_status") or row.get("status"), 80),
            }
        )
    return items


def _review_pending_items(operations: dict) -> list[dict]:
    items = []
    for row in operations.get("pending_review_items") or []:
        if not isinstance(row, dict):
            continue
        description = str(row.get("description") or "")
        match = re.search(r"原期限：([^／\n]+)／原類型：([^\n]+)", description)
        original_due = _business_item_text(match.group(1), 20) if match else ""
        original_type = _business_item_text(match.group(2), 40) if match else ""
        summary = ""
        for raw_line in description.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("【MAGI", "原期限：", "尚無可驗證", "MAGI分享連結：", "連結有效至：")):
                continue
            summary = _business_item_text(line, 160)
            break
        items.append(
            {
                "case_number": _business_item_text(row.get("case_number"), 40),
                "client_name": _business_item_text(row.get("client_name"), 80),
                "review_date": _business_item_text(row.get("todo_date"), 20),
                "original_due_date": original_due,
                "original_type": original_type,
                "summary": summary,
            }
        )
    return items


_LAF_RETRY_REASON_LABELS = {
    "portal_not_listed": "法扶網站目前尚未列出可下載附件",
    "portal_check_failed": "法扶網站檢查失敗，等待下一輪重試",
    "login_failed": "法扶網站登入失敗，等待下一輪重試",
    "missing_local_case_folder": "找不到本機案件資料夾",
    "identity_ambiguous": "案件資料無法唯一比對",
}


def _laf_retry_details(items: list[dict]) -> list[dict]:
    details = []
    for row in items:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "pending_retry").strip().lower()
        if status not in {"", "pending_retry", "manual_review", "exhausted"}:
            continue
        reason_key = str(row.get("last_error") or row.get("reason") or "").strip()
        reason = _LAF_RETRY_REASON_LABELS.get(reason_key) or _business_item_text(reason_key.replace("_", " "), 120)
        if status == "exhausted":
            reason = "已達自動重試上限，需要人工確認" + (f"；{reason}" if reason else "")
        elif status == "manual_review" and reason:
            reason = "需要人工確認；" + reason
        details.append(
            {
                "case_number": _business_item_text(row.get("case_number"), 40),
                "laf_case_number": _business_item_text(row.get("laf_case_number"), 40),
                "client_name": _business_item_text(row.get("client_name"), 80),
                "case_type": _business_item_text(row.get("case_type"), 60),
                "case_reason": _business_item_text(row.get("case_reason"), 100),
                "status": "需人工確認" if status in {"manual_review", "exhausted"} else "自動重試中",
                "reason": reason or "附件尚未取得，等待下一輪重試",
                "tries": int(row.get("tries") or 0),
                "last_try_at": _business_item_text(str(row.get("last_try_at") or "").replace("T", " "), 30),
            }
        )
    return details


def _laf_missing_details(portal: dict) -> list[dict]:
    details = []
    for row in portal.get("portal_new_files") or []:
        if not isinstance(row, dict) or int(row.get("new_count") or 0) <= 0:
            continue
        details.append(
            {
                "laf_case_number": _business_item_text(row.get("laf_no") or row.get("case_number"), 40),
                "client_name": _business_item_text(row.get("client_name"), 80),
                "missing_files": [
                    _business_item_text(name, 120)
                    for name in (row.get("missing_files") or [])
                    if str(name or "").strip()
                ],
            }
        )
    return details


def _review_ready_items(review_result: dict) -> list[dict]:
    parsed = (review_result.get("check") or {}).get("parsed") or {}
    rows = parsed.get("ready_to_download_items") if isinstance(parsed, dict) else []
    details = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        details.append(
            {
                "case_number": _business_item_text(row.get("court_case_no") or row.get("case_number"), 80),
                "laf_case_number": _business_item_text(row.get("laf_case_no"), 40),
                "application_no": _business_item_text(row.get("application_no"), 60),
                "client_name": _business_item_text(row.get("client_name"), 80),
                "court": _business_item_text(row.get("court"), 80),
            }
        )
    return details


def build_snapshot(
    *,
    root: Path = ROOT,
    env: dict[str, str] | None = None,
    exec_fn: Callable | None = None,
    now: datetime | None = None,
    mlx_available: bool | None = None,
    whisper_cli: str | None = None,
) -> dict:
    root = Path(root)
    env = dict(os.environ if env is None else env)
    now = now or datetime.now()
    operations = _operations(exec_fn)
    report_failures = _recent_report_failures(root, now=now)
    closing_pending = int(operations.get("closing_pending_cases") or 0) if operations else 0
    review_pending = int(operations.get("pending_review_todos") or 0) if operations else 0
    closing_items = _closing_pending_items(operations) if operations else []
    review_items = _review_pending_items(operations) if operations else []

    if report_failures["count"]:
        missing_docs = int(report_failures["reasons"].get("missing_required_docs") or 0)
        label = f"{report_failures['count']}案受阻"
        if missing_docs == report_failures["count"]:
            label = f"{missing_docs}案欠件"
        if closing_pending:
            label += f"／{closing_pending}案待辦"
        if review_pending:
            label += f"／{review_pending}項確認"
        report_item = {
            "state": "attention",
            "label": label,
            "count": report_failures["count"],
            "pending": closing_pending,
            "review_pending": review_pending,
            "pending_items": closing_items,
            "review_items": review_items,
        }
    elif operations:
        labels = []
        if closing_pending:
            labels.append(f"{closing_pending}案回報")
        if review_pending:
            labels.append(f"{review_pending}項確認")
        report_item = {
            "state": "waiting" if labels else "ok",
            "label": "／".join(labels) if labels else "無待處理",
            "count": closing_pending + review_pending,
            "pending": closing_pending,
            "review_pending": review_pending,
            "pending_items": closing_items,
            "review_items": review_items,
        }
    else:
        report_item = {"state": "waiting", "label": "資料庫待確認", "count": 0}

    portal = _load_json(root / "static" / "laf_portal_new_files_latest.json")
    missing_files = int(portal.get("portal_still_missing") or 0)
    retry = _load_json(root / ".agent" / "laf_pending_portal_downloads.json")
    retry_items = retry.get("items") if isinstance(retry.get("items"), list) else []
    pending_retry = sum(1 for item in retry_items if str(item.get("status") or "pending_retry") in {"", "pending_retry"})
    manual_retry = sum(1 for item in retry_items if str(item.get("status") or "") in {"manual_review", "exhausted"})
    laf_retry_details = _laf_retry_details(retry_items)
    laf_missing_details = _laf_missing_details(portal)
    if missing_files or manual_retry:
        laf_item = {
            "state": "attention",
            "label": f"{missing_files}份欠檔" if missing_files else f"{manual_retry}案人工確認",
            "missing": missing_files,
            "pending_retry": pending_retry,
            "manual_review": manual_retry,
            "retry_items": laf_retry_details,
            "missing_items": laf_missing_details,
        }
    elif pending_retry:
        laf_item = {
            "state": "waiting",
            "label": f"{pending_retry}案重試中",
            "missing": 0,
            "pending_retry": pending_retry,
            "manual_review": 0,
            "retry_items": laf_retry_details,
            "missing_items": [],
        }
    else:
        laf_item = {"state": "ok", "label": "附件齊全", "missing": 0, "pending_retry": 0, "manual_review": 0, "retry_items": [], "missing_items": []}

    review = _load_json(root / "static" / "file_review_auto_state.json")
    review_result = review.get("result") if isinstance(review.get("result"), dict) else {}
    review_job = _latest_file_review_job(root)
    review_ready_items = _review_ready_items(review_result)
    auto_download = _truthy(env.get("MAGI_FILE_REVIEW_AUTO_DOWNLOAD"))
    if review_job and (
        str(review_job.get("status") or "").lower() in {"failed", "error"}
        or review_job.get("success") is False
    ):
        review_item = {"state": "attention", "label": "下載工作失敗", "auto_download": auto_download, "ready_items": review_ready_items}
    elif review_result and not bool(review_result.get("ok", True)):
        review_item = {"state": "attention", "label": "上輪失敗", "auto_download": auto_download, "ready_items": review_ready_items}
    elif not auto_download:
        review_item = {"state": "attention", "label": "僅掃描未下載", "auto_download": False, "ready_items": review_ready_items}
    elif str(review_result.get("reason") or "") == "auto_download_disabled":
        review_item = {"state": "waiting", "label": "已啟用待首輪", "auto_download": True, "ready_items": review_ready_items}
    else:
        ready = int(((review_result.get("check") or {}).get("parsed") or {}).get("ready_to_download_count") or 0)
        review_item = {
            "state": "waiting" if ready else "ok",
            "label": f"{ready}件待下載" if ready else "自動下載正常",
            "auto_download": True,
            "ready_to_download": ready,
            "ready_items": review_ready_items,
        }

    if mlx_available is None:
        mlx_available = importlib.util.find_spec("mlx_whisper") is not None
    whisper_cli = shutil.which("whisper") if whisper_cli is None else whisper_cli
    if mlx_available:
        transcript_item = {"state": "ok", "label": "MLX高品質", "provider": "mlx_whisper"}
    elif whisper_cli:
        transcript_item = {"state": "waiting", "label": "CPU備援", "provider": "whisper_cli"}
    else:
        transcript_item = {"state": "attention", "label": "無可用引擎", "provider": ""}

    heavy_live = _load_json(root / ".runtime" / "heavy_fallback_live_latest.json")
    heavy_enabled = _truthy(env.get("NVIDIA_NIM_ENABLE"))
    heavy_model = str(env.get("NVIDIA_NIM_MODEL") or "").strip()
    heavy_checked = datetime.fromtimestamp((root / ".runtime" / "heavy_fallback_live_latest.json").stat().st_mtime) if heavy_live else None
    heavy_recent = bool(heavy_checked and now - heavy_checked <= timedelta(days=7))
    if not heavy_enabled or not heavy_model:
        heavy_item = {"state": "attention", "label": "未啟用", "model": heavy_model}
    elif heavy_live.get("success") is True and heavy_recent:
        heavy_item = {"state": "ok", "label": "NVIDIA 120B", "model": heavy_model}
    else:
        heavy_item = {"state": "waiting", "label": "等待LIVE驗證", "model": heavy_model}

    items = {
        "案件回報": report_item,
        "法扶附件": laf_item,
        "閱卷下載": review_item,
        "錄音轉文字": transcript_item,
        "NVIDIA重型": heavy_item,
    }
    attention = sum(1 for item in items.values() if item.get("state") == "attention")
    waiting = sum(1 for item in items.values() if item.get("state") == "waiting")
    state = "attention" if attention else ("waiting" if waiting else "ok")
    return {
        "ok": attention == 0,
        "state": state,
        "generated_at": now.isoformat(timespec="seconds"),
        "summary": {"attention": attention, "waiting": waiting, "ok": len(items) - attention - waiting},
        "items": items,
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="建立 MAGI 業務就緒快照")
    parser.add_argument("--json-out", default=str(ROOT / "static" / "business_readiness_latest.json"))
    args = parser.parse_args(argv)
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", override=False)
    except Exception:
        pass
    payload = build_snapshot(root=ROOT)
    _write_json(Path(args.json_out), payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    # Business blockers are carried in the payload; generating the snapshot is
    # still a successful cron run and must not masquerade as scheduler failure.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
