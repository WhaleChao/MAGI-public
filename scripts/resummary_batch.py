#!/usr/bin/env python3
"""批次重新摘要所有判決。用 nohup 跑時輸出不受 buffering 影響。
含自動重試機制：oMLX 斷線時等待恢復後繼續。"""
import sys, os, json, time, urllib.request

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

_MAGI_ROOT = os.environ.get("MAGI_ROOT_DIR") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _MAGI_ROOT)
sys.path.insert(0, os.path.join(_MAGI_ROOT, "skills", "judgment-collector"))
import importlib
import action as jc
importlib.reload(jc)

try:
    from api.routing.service_registry import get_service_url as _get_svc_url
    OMLX_URL = _get_svc_url("omlx_inference") + "/v1/models"
except Exception:
    OMLX_URL = "http://127.0.0.1:8080/v1/models"
MAX_WAIT = 600  # 最多等 10 分鐘


def wait_for_omlx():
    """等待 oMLX 恢復，回傳 True 表示恢復，False 表示超時。"""
    waited = 0
    while waited < MAX_WAIT:
        try:
            r = urllib.request.urlopen(OMLX_URL, timeout=5)
            if r.status == 200:
                print(f"  oMLX recovered after {waited}s", flush=True)
                return True
        except Exception:
            pass
        time.sleep(10)
        waited += 10
        if waited % 60 == 0:
            print(f"  Waiting for oMLX... {waited}s", flush=True)
    return False


total_improved = 0
total_failed = 0
total_skipped = 0
batch_num = 0
consecutive_failures = 0

while True:
    batch_num += 1
    print(f"\n=== Batch {batch_num} (50 judgments) ===", flush=True)

    try:
        result = jc.resummary_all(batch_size=50, timeout_sec=300, notify=False)
    except Exception as e:
        print(f"Batch exception: {e}", flush=True)
        result = {"success": False, "error": str(e)}

    print(json.dumps(result, ensure_ascii=False), flush=True)

    if not result.get("success"):
        consecutive_failures += 1
        print(f"Batch failed ({consecutive_failures}/5): {result.get('error')}", flush=True)

        if consecutive_failures >= 5:
            print("Too many consecutive failures, stopping.", flush=True)
            break

        print("Checking oMLX...", flush=True)
        if wait_for_omlx():
            importlib.reload(jc)
            continue
        else:
            print("oMLX did not recover in time, stopping.", flush=True)
            break

    consecutive_failures = 0
    total_improved += result.get("improved", 0)
    total_failed += result.get("failed", 0)
    total_skipped += result.get("skipped", 0)

    summary_msg = (
        f"[Batch {batch_num}] improved={result.get('improved',0)} "
        f"failed={result.get('failed',0)} skipped={result.get('skipped',0)} "
        f"| TOTAL: improved={total_improved} failed={total_failed} skipped={total_skipped}"
    )
    print(summary_msg, flush=True)

    if result.get("improved", 0) == 0:
        print("No improvements in this batch, stopping.", flush=True)
        break

print(f"\n=== FINAL TOTALS ===", flush=True)
print(f"Improved: {total_improved}", flush=True)
print(f"Failed: {total_failed}", flush=True)
print(f"Skipped: {total_skipped}", flush=True)
