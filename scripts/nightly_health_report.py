#!/usr/bin/env python3
"""
MAGI 夜間健康報告（06:30 晨間通知）

檢查 00:00-06:00 期間的夜間任務是否正確執行，並發送摘要到 judicial_api topic。
包含：司法院 API 拉取、LAF 巡檢、PDF 訓練、DB 同步 等夜間任務狀態。
"""
import json
import os
import sys
import glob
from datetime import datetime, timedelta

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_MAGI_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, ".."))
sys.path.insert(0, _MAGI_ROOT)

AGENT_DIR = os.path.join(_MAGI_ROOT, ".agent")
DELIVERY_LOG = os.path.join(AGENT_DIR, "red_phone_delivery.jsonl")
AUTOPILOT_RUNS_DIR = os.environ.get(
    "MAGI_AUTOPILOT_RUNS_DIR",
    os.path.join(_MAGI_ROOT, "_autopilot_runs"),
)
RUNTIME_DIR = os.environ.get("MAGI_RUNTIME_DIR", os.path.join(_MAGI_ROOT, ".runtime"))
RESOURCE_GUARD_LOG = os.environ.get(
    "MAGI_RESOURCE_GUARD_LOG",
    os.path.join(RUNTIME_DIR, "resource_guarded_run.jsonl"),
)
CRON_STATE_PATH = os.environ.get(
    "MAGI_CRON_STATE_PATH",
    os.path.join(RUNTIME_DIR, "cron_state.json"),
)

# 夜間關鍵步驟清單
NIGHTLY_KEY_STEPS = [
    ("pdf_nightly_train", "PDF 視覺訓練"),
    ("judicial_api_night_pull", "司法院 API 夜間拉取"),
    ("judicial_api_nightly_process", "拉取後摘要整理"),
    ("laf_deep_extract", "法扶深度擷取"),
    ("db_bidirectional_sync", "DB 雙向同步"),
    ("db_daily_backup", "DB 每日備份"),
    ("night_talk", "三哲人夜間會議"),
]


def _find_latest_nightly_run() -> str | None:
    """找到最近一次 nightly run 的目錄。"""
    today_str = datetime.now().strftime("%Y%m%d")
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

    candidates = []
    for prefix in [today_str, yesterday_str]:
        pattern = os.path.join(AUTOPILOT_RUNS_DIR, f"{prefix}_*")
        candidates.extend(glob.glob(pattern))
    candidates.sort(reverse=True)

    nightly_candidates = [d for d in candidates if os.path.basename(d).endswith("_nightly")]
    ordered_candidates = nightly_candidates + [d for d in candidates if d not in nightly_candidates]

    for d in ordered_candidates:
        if os.path.isdir(d):
            # 檢查是否有 nightly 相關的輸出
            report_path = os.path.join(d, "report.json")
            if os.path.exists(report_path):
                return d
            # 也檢查有無 judicial_api 相關檔案
            if any(
                os.path.exists(os.path.join(d, f"{step}.stdout.txt"))
                for step, _ in NIGHTLY_KEY_STEPS
            ):
                return d
    return candidates[0] if candidates else None


def _recent_date_prefixes() -> tuple[str, str]:
    return (
        datetime.now().strftime("%Y-%m-%d"),
        (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"),
    )


def _read_json_file(path: str) -> dict:
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_recent_resource_guard_event(job_id: str = "job_nightly_autopilot") -> dict:
    """Return the latest resource guard event for today/yesterday."""
    if not os.path.exists(RESOURCE_GUARD_LOG):
        return {}
    date_prefixes = _recent_date_prefixes()
    latest: dict = {}
    try:
        with open(RESOURCE_GUARD_LOG, "r", encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
                if entry.get("job_id") != job_id:
                    continue
                try:
                    event_dt = datetime.fromtimestamp(float(entry.get("ts") or 0))
                except Exception:
                    continue
                if not any(event_dt.strftime("%Y-%m-%d").startswith(p) for p in date_prefixes):
                    continue
                if not latest or float(entry.get("ts") or 0) > float(latest.get("ts") or 0):
                    latest = entry
    except Exception:
        return {}
    return latest


def _format_guard_snapshot(snapshot: dict) -> str:
    disk = snapshot.get("disk_free_gb")
    total = snapshot.get("disk_total_gb")
    swap = snapshot.get("swap_used_gb")
    free_inactive = snapshot.get("free_plus_inactive_gb")
    parts = []
    if isinstance(disk, (int, float)):
        if isinstance(total, (int, float)):
            parts.append(f"磁碟可用 {disk:.2f}/{total:.2f}GB")
        else:
            parts.append(f"磁碟可用 {disk:.2f}GB")
    if isinstance(swap, (int, float)):
        parts.append(f"swap {swap:.2f}GB")
    if isinstance(free_inactive, (int, float)):
        parts.append(f"記憶體 free+inactive {free_inactive:.2f}GB")
    return "、".join(parts)


def _diagnose_missing_nightly_run() -> list[str]:
    """Explain why no nightly run directory exists, using scheduler telemetry."""
    lines: list[str] = []
    event = _load_recent_resource_guard_event("job_nightly_autopilot")
    if event:
        try:
            event_time = datetime.fromtimestamp(float(event.get("ts") or 0)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            event_time = "未知時間"
        decision = event.get("decision") if isinstance(event.get("decision"), dict) else {}
        snapshot = decision.get("snapshot") if isinstance(decision.get("snapshot"), dict) else {}
        level = decision.get("level") or "unknown"
        reasons = event.get("block_reasons") or decision.get("reasons") or []
        if isinstance(reasons, list):
            reasons_text = "、".join(str(r) for r in reasons if r)
        else:
            reasons_text = str(reasons)
        snapshot_text = _format_guard_snapshot(snapshot)
        if event.get("blocked"):
            lines.append(f"🛡️ 資源守門：{event_time} 已觸發 nightly，但被跳過（level={level}）。")
            if reasons_text:
                lines.append(f"  原因：{reasons_text}")
            if snapshot_text:
                lines.append(f"  當時資源：{snapshot_text}")
            lines.append("  處理方向：釋放磁碟/降低負載後，下一輪會自動恢復；若只是 throttle，應改跑降載版 nightly。")
            return lines
        returncode = event.get("returncode")
        lines.append(f"🛡️ 資源守門：{event_time} 已放行 nightly（level={level}, returncode={returncode}），但未找到 run 目錄。")
        if snapshot_text:
            lines.append(f"  當時資源：{snapshot_text}")

    state = _read_json_file(CRON_STATE_PATH)
    nightly_state = state.get("job_nightly_autopilot") if isinstance(state.get("job_nightly_autopilot"), dict) else {}
    last_run = str(nightly_state.get("last_run") or "").strip()
    if last_run:
        lines.append(f"⏱️ cron_state 顯示 job_nightly_autopilot 最近觸發：{last_run}")
        lines.append("  但 _autopilot_runs 沒有對應目錄，需檢查 command 是否在建立 run_dir 前被中止。")
    else:
        lines.append("⏱️ cron_state 沒有 job_nightly_autopilot 今日/昨日觸發紀錄。")

    if not lines:
        lines.append("可能原因：nightly 任務未啟動或 _autopilot_runs 路徑不正確")
    return lines


def _parse_step_results(run_dir: str) -> dict:
    """從 run_dir 解析各步驟的執行結果。"""
    results = {}

    # 優先讀 report.json
    report_path = os.path.join(run_dir, "report.json")
    if os.path.exists(report_path):
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f) or {}
            details = report.get("details") or report
            steps = (details.get("steps") or {}) if isinstance(details, dict) else {}
            for step_key, step_name in NIGHTLY_KEY_STEPS:
                if step_key in steps:
                    step = steps[step_key]
                    if isinstance(step, dict):
                        result = {
                            "name": step_name,
                            "ok": bool(step.get("ok")),
                            "skipped": bool(step.get("skipped") or (step.get("parsed") or {}).get("skipped")),
                            "detail": _extract_step_detail(step),
                        }
                        results[step_key] = _normalize_step_result(step_key, result, step)
            if not results and report.get("ok") is False:
                detail = ""
                if isinstance(details, dict):
                    detail = str(details.get("error") or details.get("summary") or "")
                detail = detail or str(report.get("summary") or "nightly report marked ok=false")
                results["_nightly_run"] = {
                    "name": "夜間主流程",
                    "ok": False,
                    "skipped": False,
                    "detail": detail[:240],
                }
            if not results and report.get("task") == "self_test":
                results["_nightly_run"] = {
                    "name": "夜間主流程",
                    "ok": bool(report.get("ok")),
                    "skipped": True,
                    "detail": "最近一筆是 self_test，未代表夜間排程",
                }
            return results
        except Exception:
            pass

    # Fallback: 從個別 stdout 檔案推斷
    for step_key, step_name in NIGHTLY_KEY_STEPS:
        stdout_file = os.path.join(run_dir, f"{step_key}.stdout.txt")
        stderr_file = os.path.join(run_dir, f"{step_key}.stderr.txt")
        if os.path.exists(stdout_file):
            try:
                with open(stdout_file, "r", encoding="utf-8") as f:
                    stdout = f.read()
                parsed = {}
                try:
                    parsed = json.loads(stdout)
                except Exception:
                    pass
                ok = bool(parsed.get("success", parsed.get("ok", True)))
                results[step_key] = {
                    "name": step_name,
                    "ok": ok,
                    "skipped": bool(parsed.get("skipped")),
                    "detail": parsed.get("message", "")[:200],
                }
            except Exception:
                results[step_key] = {
                    "name": step_name,
                    "ok": False,
                    "skipped": False,
                    "detail": "無法讀取輸出",
                }
        elif os.path.exists(stderr_file):
            results[step_key] = {
                "name": step_name,
                "ok": False,
                "skipped": False,
                "detail": "僅有 stderr 輸出",
            }

    return results


def _dotenv_value(name: str) -> str | None:
    path = os.path.join(_MAGI_ROOT, ".env")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if not raw or raw.startswith("#") or "=" not in raw:
                    continue
                key, value = raw.split("=", 1)
                if key.strip() == name:
                    return value.strip().strip('"').strip("'")
    except Exception:
        return None
    return None


def _env_truthy(name: str, default: str = "0") -> bool:
    value = os.environ.get(name)
    if value is None:
        value = _dotenv_value(name)
    return str(value if value is not None else default).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_step_result(step_key: str, result: dict, raw_step: dict) -> dict:
    """Apply current operating policy to historical nightly step records."""
    parsed = raw_step.get("parsed") if isinstance(raw_step, dict) else {}
    parsed = parsed if isinstance(parsed, dict) else {}
    if step_key == "db_bidirectional_sync" and not _env_truthy("MAGI_ENABLE_DB_BIDIR_SYNC", "0"):
        if not result.get("ok"):
            result = dict(result)
            result["ok"] = True
            result["skipped"] = True
            result["detail"] = "目前採本機備份模式，原 DB 雙向同步已停用"
    elif step_key == "db_daily_backup" and not result.get("ok"):
        items = parsed.get("items") if isinstance(parsed.get("items"), list) else []
        if any(isinstance(item, dict) and item.get("ok") and item.get("path") for item in items):
            result = dict(result)
            result["ok"] = True
            result["detail"] = "已有 DB 備份檔落地；舊 both 目標中的不可用 profile 已忽略"
    return result


def _extract_step_detail(step: dict) -> str:
    """從 step dict 提取摘要資訊。"""
    parsed = step.get("parsed") or {}
    reason = step.get("reason")
    if reason:
        return str(reason)[:200]
    if isinstance(parsed, dict):
        msg = parsed.get("message", "")
        if msg:
            return str(msg)[:200]
        # 判決拉取特殊欄位
        fetched = parsed.get("fetched")
        if fetched is not None:
            return f"新抓 {fetched}"
    stderr_tail = str(step.get("stderr_tail") or "")
    if stderr_tail and not step.get("ok"):
        return stderr_tail[-150:]
    return ""


def _count_delivery_log_window(start_hour: int = 0, end_hour: int = 6) -> dict:
    """統計 delivery log 中指定時段的發送記錄。"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    sent = 0
    failed = 0
    topics_used: set[str] = set()

    if not os.path.exists(DELIVERY_LOG):
        return {"sent": 0, "failed": 0, "topics": [], "log_exists": False}

    try:
        with open(DELIVERY_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                ts = str(entry.get("ts", ""))
                if not ts.startswith(today_str):
                    continue
                try:
                    hour = int(ts[11:13])
                except Exception:
                    continue
                if start_hour <= hour < end_hour:
                    event = entry.get("event", "")
                    if event == "sent":
                        sent += 1
                    elif event == "failed":
                        failed += 1
                    topic = entry.get("topic_key", "")
                    if topic:
                        topics_used.add(topic)
    except Exception:
        pass

    return {
        "sent": sent,
        "failed": failed,
        "topics": sorted(topics_used),
        "log_exists": True,
    }


def generate_report() -> str:
    """產生晨間健康報告文字。"""
    now = datetime.now()
    lines = [f"🌅 MAGI 夜間健康報告 — {now.strftime('%Y-%m-%d %H:%M')}"]
    lines.append(f"檢查區間：{now.strftime('%Y-%m-%d')} 00:00 ~ 06:00")
    lines.append("")
    missing_diagnosis: list[str] = []

    # 1. 尋找最近的 nightly run
    run_dir = _find_latest_nightly_run()
    if run_dir:
        run_name = os.path.basename(run_dir)
        lines.append(f"📁 最近執行：{run_name}")

        step_results = _parse_step_results(run_dir)
        if step_results:
            lines.append("")
            ok_count = 0
            fail_count = 0
            skip_count = 0

            display_steps = [("_nightly_run", "夜間主流程")] + NIGHTLY_KEY_STEPS
            for step_key, step_name in display_steps:
                if step_key in step_results:
                    r = step_results[step_key]
                    if r["skipped"]:
                        icon = "⏭️"
                        skip_count += 1
                    elif r["ok"]:
                        icon = "✅"
                        ok_count += 1
                    else:
                        icon = "❌"
                        fail_count += 1
                    line = f"  {icon} {r['name']}"
                    if r.get("detail"):
                        line += f"：{r['detail']}"
                    lines.append(line)

            lines.append("")
            lines.append(f"統計：✅ {ok_count} 成功 / ❌ {fail_count} 失敗 / ⏭️ {skip_count} 略過")
        else:
            lines.append("⚠️ 無法解析步驟結果")
    else:
        lines.append("⚠️ 未找到今日/昨日的 nightly run 目錄")
        missing_diagnosis = _diagnose_missing_nightly_run()
        lines.extend(missing_diagnosis)

    # 2. 通知投遞統計
    delivery = _count_delivery_log_window(0, 6)
    lines.append("")
    lines.append("📨 00:00~06:00 通知投遞：")
    if delivery["log_exists"]:
        lines.append(f"  發送成功 {delivery['sent']} 則 / 失敗 {delivery['failed']} 則")
        if delivery["topics"]:
            lines.append(f"  使用 Topic：{', '.join(delivery['topics'])}")
    else:
        lines.append("  ⚠️ 無投遞紀錄檔")

    # 3. 整體判定
    lines.append("")
    has_failures = False
    if run_dir:
        step_results = _parse_step_results(run_dir)
        fail_steps = [
            r["name"]
            for r in step_results.values()
            if not r["ok"] and not r["skipped"]
        ]
        if fail_steps:
            has_failures = True
            lines.append(f"⚠️ 有 {len(fail_steps)} 個步驟失敗，請檢查")
        elif not step_results:
            lines.append("⚠️ 無步驟資料可供判定")
        else:
            lines.append("✅ 夜間任務全部正常完成")
    else:
        has_failures = True
        if any("資源守門" in line and "被跳過" in line for line in missing_diagnosis):
            lines.append("⚠️ 夜間主流程已由資源守門略過；非 run 目錄路徑錯誤。")
        else:
            lines.append("❌ 夜間任務可能未執行")

    if delivery["failed"] > 0:
        lines.append(f"⚠️ 有 {delivery['failed']} 則通知投遞失敗")

    return "\n".join(lines)


def send_report():
    """發送晨間健康報告到 judicial_api topic。"""
    report_text = generate_report()

    try:
        from skills.ops.red_phone import send_telegram_push_with_status
        result = send_telegram_push_with_status(
            report_text,
            severity="info",
            source="nightly_health_report",
            topic_key="judicial_api",
            queue_on_fail=True,
        )
        if result.get("telegram"):
            print(f"✅ 報告已發送至 judicial_api topic")
        elif result.get("queued"):
            print(f"📤 報告已排入待發佇列")
        else:
            print(f"❌ 發送失敗：{result.get('error', 'unknown')}")
    except Exception as e:
        print(f"❌ 發送失敗：{e}")

    # 也印出報告方便 debug
    print("\n" + report_text)
    return report_text


if __name__ == "__main__":
    send_report()
