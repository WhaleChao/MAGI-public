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
                        results[step_key] = {
                            "name": step_name,
                            "ok": bool(step.get("ok")),
                            "skipped": bool((step.get("parsed") or {}).get("skipped")),
                            "detail": _extract_step_detail(step),
                        }
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


def _extract_step_detail(step: dict) -> str:
    """從 step dict 提取摘要資訊。"""
    parsed = step.get("parsed") or {}
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
        lines.append("可能原因：nightly 任務未啟動或 _autopilot_runs 路徑不正確")

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
