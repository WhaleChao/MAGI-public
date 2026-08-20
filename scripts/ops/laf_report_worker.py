#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Durable LAF report worker.

Discord/LINE handlers are short-lived service processes.  Long LAF portal
automation must not run inside those handlers, otherwise a coordinated daemon
restart kills the background thread and the user never receives a completion
message.  This worker is launched as an independent process and owns:

- running laf_orchestrator.py --mode portal-draft
- writing a durable job record
- notifying the proper LAF business channel on success/failure
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", Path(__file__).resolve().parents[2])).resolve()
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(MAGI_ROOT / ".env")
except Exception:
    pass

from api.runtime_paths import get_laf_script, get_runtime_dir, get_skill_python

_RESULT_START = "===MAGI_RESULT_JSON_START==="
_RESULT_END = "===MAGI_RESULT_JSON_END==="


def _runtime_dir() -> Path:
    p = get_runtime_dir()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _append_job_event(job_id: str, status: str, payload: dict[str, Any]) -> None:
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "job_id": job_id,
        "status": status,
        **payload,
    }
    try:
        with (_runtime_dir() / "laf_report_jobs.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _parse_result(stdout: str) -> dict[str, Any] | None:
    text = stdout or ""
    s_idx = text.rfind(_RESULT_START)
    if s_idx >= 0:
        body_start = s_idx + len(_RESULT_START)
        e_idx = text.find(_RESULT_END, body_start)
        if e_idx > body_start:
            try:
                return json.loads(text[body_start:e_idx].strip())
            except Exception:
                return None
    try:
        parsed = json.loads(text.strip())
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = json.loads(line)
                return parsed if isinstance(parsed, dict) else None
            except Exception:
                continue
    return None


def _topic_for_action(action: str) -> str:
    return {
        "go_live": "laf_go_live",
        "closing": "laf_closing",
        "condition": "laf_condition",
        "fee": "laf_fee",
        "inquiry": "laf_inquiry",
        "progress": "laf_progress",
    }.get((action or "").strip().lower(), "laf_general")


def _action_label(payload: dict[str, Any], action: str) -> str:
    label = str(payload.get("action_label") or "").strip()
    if label:
        return label
    return {
        "go_live": "開辦",
        "closing": "結案回報",
        "condition": "二階段",
        "fee": "費用支付",
        "inquiry": "疑義回報",
        "withdrawal": "撤回",
        "progress": "進度回報",
    }.get(action, "回報")


def _target_text(data: dict[str, Any], payload: dict[str, Any]) -> str:
    ident = data.get("identity") if isinstance(data.get("identity"), dict) else {}
    parts = [
        str(ident.get("client_name") or payload.get("client_name") or "").strip(),
        str(ident.get("laf_case_number") or payload.get("laf_case_no") or "").strip(),
        str(ident.get("case_number") or payload.get("case_number") or "").strip(),
    ]
    return "｜".join([p for p in parts if p]) or str(
        payload.get("laf_case_no") or payload.get("case_number") or payload.get("client_name") or "-"
    )


def _preview_path(data: dict[str, Any]) -> str:
    preview = data.get("preview") if isinstance(data.get("preview"), dict) else {}
    return str(preview.get("png") or data.get("screenshot_path") or "").strip()


def _preview_url(data: dict[str, Any]) -> str:
    preview = data.get("preview") if isinstance(data.get("preview"), dict) else {}
    png_export = preview.get("png_export") if isinstance(preview.get("png_export"), dict) else {}
    html_export = preview.get("html_export") if isinstance(preview.get("html_export"), dict) else {}
    return str(png_export.get("url") or html_export.get("url") or "").strip()


def _format_success(data: dict[str, Any], payload: dict[str, Any], action: str) -> str:
    label = _action_label(payload, action)
    if action == "go_live":
        lines = [f"✅ 法扶{label}已完成填寫（尚未送出）"]
    else:
        lines = [f"✅ 法扶{label}已完成存檔（未送出）"]
    lines.append(f"目標：{_target_text(data, payload)}")

    if action == "closing":
        counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
        if counts:
            stats = []
            for key, label2 in [
                ("meeting_count", "開會"),
                ("contact_count", "聯繫"),
                ("inq_count", "律見"),
                ("court_count", "開庭"),
                ("review_count", "閱卷"),
                ("document_count", "書狀"),
            ]:
                if key in counts:
                    try:
                        stats.append(f"{label2} {int(counts.get(key) or 0)}")
                    except Exception:
                        stats.append(f"{label2} {counts.get(key)}")
            if stats:
                lines.append(f"統計：{'／'.join(stats)}")
            court_name = str(counts.get("court_name") or "").strip()
            case_year = str(counts.get("court_case_year") or "").strip()
            case_code = str(counts.get("court_case_code") or "").strip()
            case_no = str(counts.get("court_case_no") or "").strip()
            if court_name and case_year:
                lines.append(f"案號：{court_name}{case_year}年度{case_code}字第{case_no}號")
            result = str(counts.get("closing_result") or "").strip()
            if result:
                lines.append(f"結果：{result[:80]}")
            doc_type = str(counts.get("closing_doc_type") or "").strip()
            judg_eff = str(counts.get("judg_eff") or "").strip()
            if doc_type or judg_eff:
                lines.append(f"裁判：{doc_type}{'，' + judg_eff if judg_eff else ''}")
            zeros = []
            for key, label2 in [
                ("meeting_count", "開會"),
                ("contact_count", "聯繫"),
                ("court_count", "開庭"),
                ("review_count", "閱卷"),
                ("document_count", "書狀"),
            ]:
                try:
                    if int(counts.get(key, 0) or 0) == 0:
                        zeros.append(label2)
                except Exception:
                    pass
            if zeros:
                lines.append(f"⚠️ 以下為 0：{'、'.join(zeros)}，請確認「扶助律師特別說明」")

        upload_bundle = data.get("upload_bundle") if isinstance(data.get("upload_bundle"), dict) else {}
        upload_files = upload_bundle.get("pdf_files") or []
        if upload_files:
            lines.append(f"上傳：{len(upload_files)} 份（書狀／判決書）")
        zero_reasons = data.get("zero_reasons") if isinstance(data.get("zero_reasons"), dict) else {}
        if zero_reasons:
            label_map = {"disc_times": "討論次數", "review_count": "閱卷", "court_count": "開庭", "document_count": "書狀"}
            lines.append("理由：")
            for key, value in zero_reasons.items():
                lines.append(f"- {label_map.get(key, key)}：{value}")
        lines.append("🔒 安全政策：目前僅暫存，不會代為送出。")

    url = _preview_url(data)
    if url:
        lines.append(f"畫面預覽：{url}")
    return "\n".join(lines)


def _format_failure(data: dict[str, Any] | None, payload: dict[str, Any], action: str, *, stderr: str = "", stdout: str = "", rc: int | None = None) -> str:
    label = _action_label(payload, action)
    if rc is not None and rc != 0:
        detail = (stderr or stdout or "").strip()[-1200:]
        return f"❌ 法扶{label}流程失敗（code={rc}）\n{detail}"
    data = data or {}
    err = str(data.get("error") or "unknown").strip()
    detail = str(data.get("detail") or data.get("portal_error") or "").strip()
    if err == "missing_target":
        return f"❌ 法扶{label}失敗：缺少目標。\n請補上姓名、法扶案號或案件系統編號。"
    if err in {"missing_case_folder", "missing_case_folder_for_closing"}:
        return f"❌ 法扶{label}失敗：找不到案件資料夾。\n請先確認該案已建立資料夾並可由 DB 對應。"
    if err == "missing_reason":
        return f"❌ 法扶{label}失敗：缺少原因。\n請重送並補上原因。"
    if err == "missing_required_docs":
        missing = data.get("missing") if isinstance(data.get("missing"), list) else []
        miss_txt = "、".join(str(x) for x in missing) if missing else "必要文件"
        return f"❌ 法扶{label}失敗：缺少文件：{miss_txt}\n請先把文件放入對應案件資料夾後再重試。"
    if err == "missing_required_dates":
        missing = data.get("missing") if isinstance(data.get("missing"), list) else []
        miss_txt = "、".join(str(x) for x in missing) if missing else "必要日期"
        return f"❌ 法扶{label}失敗：視覺判讀日期不足（{miss_txt}）。\n請確認文件內容清晰。"
    if err == "identity_needs_manual_confirmation":
        ident = data.get("identity") if isinstance(data.get("identity"), dict) else {}
        reason = str(ident.get("manual_reason") or "").strip()
        hint = str(ident.get("manual_hint") or "").strip()
        lines = [f"⚠️ 法扶{label}需要補充資訊："]
        if hint:
            lines.append(hint)
        elif reason:
            lines.append(f"原因：{reason}")
        lines.append("請補上更精確的法扶案號、案件系統編號或案由關鍵字後重試。")
        return "\n".join(lines)
    if err == "portal_draft_failed":
        msg = (
            f"❌ 法扶{label}表單填寫失敗。\n"
            "可能原因：法扶網站登入逾時、頁面載入異常、附件上傳未完成或按鈕找不到。\n"
            "MAGI 已留下背景 job 記錄；請稍後重試，若連續失敗我會列入巡檢。"
        )
        if detail:
            msg += f"\n細節：{detail[:300]}"
        return msg
    return f"❌ 法扶{label}存檔失敗：{err}" + (f"\n細節：{detail[:300]}" if detail else "")


def _notify(message: str, *, topic_key: str, file_path: str = "") -> dict[str, Any]:
    status: dict[str, Any] = {"discord_file": False, "telegram_file": False, "discord_text": False, "telegram_text": False}
    if file_path and os.path.isfile(file_path):
        try:
            from skills.ops.red_phone import send_discord_bot_file
            status["discord_file"] = bool(send_discord_bot_file(file_path=file_path, caption=message[:1200], topic_key=topic_key, source=topic_key))
        except Exception as exc:
            status["discord_file_error"] = str(exc)[:200]
        try:
            from skills.ops.red_phone import send_file_admin
            tg = send_file_admin(file_path=file_path, caption=message[:900], topic_key=topic_key)
            status["telegram_file"] = bool(isinstance(tg, dict) and tg.get("ok"))
        except Exception as exc:
            status["telegram_file_error"] = str(exc)[:200]
        if status["discord_file"] or status["telegram_file"]:
            return status
    try:
        from skills.ops.red_phone import _send_discord_bot_message
        status["discord_text"] = bool(_send_discord_bot_message(message, "info", topic_key=topic_key, source=topic_key))
    except Exception as exc:
        status["discord_text_error"] = str(exc)[:200]
    try:
        from skills.ops.red_phone import send_telegram_push_with_status
        tg = send_telegram_push_with_status(
            message,
            severity="info",
            source=topic_key,
            topic_key=topic_key,
            queue_on_fail=True,
            mirror_to_discord=False,
        )
        status["telegram_text"] = bool(isinstance(tg, dict) and (tg.get("telegram") or tg.get("queued")))
    except Exception as exc:
        status["telegram_text_error"] = str(exc)[:200]
    return status


def run_worker(payload: dict[str, Any]) -> dict[str, Any]:
    job_id = str(payload.get("job_id") or f"laf-{int(time.time())}-{uuid.uuid4().hex[:6]}")
    action = str(payload.get("action") or "").strip().lower()
    topic_key = _topic_for_action(action)
    timeout_sec = int(os.environ.get("MAGI_LAF_REPORT_TIMEOUT_SEC", "2400") or "2400")
    _append_job_event(job_id, "started", {"action": action, "payload": payload})

    laf_script = str(get_laf_script())
    py = str(get_skill_python())
    cmd = [py, laf_script, "--mode", "portal-draft", "--action", action, "--no-notify"]
    if payload.get("laf_case_no"):
        cmd += ["--laf-case-no", str(payload.get("laf_case_no"))]
    if payload.get("case_number"):
        cmd += ["--case", str(payload.get("case_number"))]
    if payload.get("client_name"):
        cmd += ["--client", str(payload.get("client_name"))]
    if payload.get("reason"):
        cmd += ["--reason", str(payload.get("reason"))]
    fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
    if fields:
        cmd += ["--fields-json", json.dumps(fields, ensure_ascii=False)]

    log_path = _runtime_dir() / f"laf_report_worker_{job_id}.log"
    run_env = os.environ.copy()
    run_env.setdefault("MAGI_LAF_PORTAL_LOCK_WAIT_SEC", str(timeout_sec))
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(MAGI_ROOT),
            env=run_env,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        try:
            log_path.write_text(
                "COMMAND: " + json.dumps(cmd, ensure_ascii=False) + "\n\nSTDOUT:\n" + stdout + "\n\nSTDERR:\n" + stderr,
                encoding="utf-8",
            )
        except Exception:
            pass
        data = _parse_result(stdout)
        if proc.returncode == 0 and isinstance(data, dict) and data.get("ok"):
            msg = _format_success(data, payload, action)
            shot = _preview_path(data)
            delivery = _notify(msg, topic_key=topic_key, file_path=shot)
            status = "ok"
        else:
            msg = _format_failure(data, payload, action, stderr=stderr, stdout=stdout, rc=proc.returncode)
            delivery = _notify(msg, topic_key=topic_key)
            status = "failed"
        result = {
            "ok": status == "ok",
            "status": status,
            "job_id": job_id,
            "action": action,
            "returncode": proc.returncode,
            "duration_sec": round(time.time() - started, 3),
            "result": data or {},
            "message": msg,
            "delivery": delivery,
            "log_path": str(log_path),
        }
        _append_job_event(job_id, status, result)
        return result
    except subprocess.TimeoutExpired as exc:
        msg = f"⏳ 法扶{_action_label(payload, action)}流程逾時（>{timeout_sec} 秒）。\n目標：{payload.get('laf_case_no') or payload.get('client_name') or payload.get('case_number') or '-'}\nMAGI 已記錄，請稍後重試或由巡檢補跑。"
        delivery = _notify(msg, topic_key=topic_key)
        result = {"ok": False, "status": "timeout", "job_id": job_id, "action": action, "error": "timeout", "duration_sec": round(time.time() - started, 3), "delivery": delivery, "log_path": str(log_path)}
        try:
            log_path.write_text(str(exc), encoding="utf-8")
        except Exception:
            pass
        _append_job_event(job_id, "timeout", result)
        return result
    except Exception as exc:
        msg = f"❌ 法扶{_action_label(payload, action)}背景流程異常：{str(exc)[:500]}"
        delivery = _notify(msg, topic_key=topic_key)
        result = {"ok": False, "status": "error", "job_id": job_id, "action": action, "error": str(exc)[:500], "delivery": delivery, "log_path": str(log_path)}
        _append_job_event(job_id, "error", result)
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Durable LAF report worker")
    parser.add_argument("--payload-json", required=True)
    parser.add_argument("--print-result", action="store_true")
    args = parser.parse_args()
    try:
        payload = json.loads(args.payload_json)
        if not isinstance(payload, dict):
            raise ValueError("payload-json must be object")
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"invalid_payload_json:{exc}"}, ensure_ascii=False))
        return 2
    result = run_worker(payload)
    if args.print_result:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
