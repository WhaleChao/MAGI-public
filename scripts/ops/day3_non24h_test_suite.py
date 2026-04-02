#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DAY3 non-24h comprehensive verification suite.
Covers immediate checks that do not require a full 24h observation window.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib import request as _urlreq


_MAGI_ROOT_DEFAULT = Path(__file__).resolve().parent.parent.parent
MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", str(_MAGI_ROOT_DEFAULT)))
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from api.runtime_paths import get_metrics_dir, get_skill_python

METRICS_DIR = get_metrics_dir()
VENV_PY = get_skill_python()
TOOLS_URL = os.environ.get("MAGI_TOOLS_URL", "http://127.0.0.1:5003").rstrip("/")
AUTOPILOT = MAGI_ROOT / "skills" / "magi-autopilot" / "action.py"
DAY3_REPORT = MAGI_ROOT / "scripts" / "ops" / "day3_stability_report.py"
EXPORT_MOD = MAGI_ROOT / "skills" / "ops" / "export_text.py"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    latency_sec: float = 0.0


def _http_json(method: str, url: str, body: Dict[str, Any] | None = None, timeout_sec: int = 20) -> Tuple[bool, int, Dict[str, Any], str, float]:
    t0 = time.monotonic()
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = _urlreq.Request(url=url, data=data, headers=headers, method=method)
    try:
        with _urlreq.urlopen(req, timeout=max(2, int(timeout_sec))) as resp:  # nosec B310
            raw = (resp.read() or b"").decode("utf-8", errors="ignore")
            code = int(getattr(resp, "status", 200))
            try:
                obj = json.loads(raw) if raw.strip() else {}
            except Exception:
                obj = {}
            return (200 <= code < 300), code, obj, raw[:400], time.monotonic() - t0
    except Exception as e:
        return False, 0, {}, f"{type(e).__name__}: {e}", time.monotonic() - t0


def _run_cmd(cmd: List[str], timeout_sec: int = 120, env: Dict[str, str] | None = None) -> Tuple[bool, str, str, int, float]:
    t0 = time.monotonic()
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=max(5, int(timeout_sec)), env=env)
        ok = cp.returncode == 0
        return ok, cp.stdout or "", cp.stderr or "", cp.returncode, time.monotonic() - t0
    except Exception as e:
        return False, "", f"{type(e).__name__}: {e}", 1, time.monotonic() - t0


def _load_export_txt():
    spec = importlib.util.spec_from_file_location("export_text", str(EXPORT_MOD))
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod.export_txt


def _ensure_tools_api_up() -> Check:
    ok, code, obj, body, dt = _http_json("GET", f"{TOOLS_URL}/health", timeout_sec=8)
    if ok and obj.get("status") == "ok":
        return Check("tools_api_health", True, f"HTTP {code}", dt)
    # try restart once
    tools_log = MAGI_ROOT / "logs" / "tools_api_standalone.log"
    _run_cmd(["/bin/zsh", "-lc", f"pkill -f 'api/tools_api.py' || true; nohup {VENV_PY} {MAGI_ROOT}/api/tools_api.py > {tools_log} 2>&1 & disown; sleep 2"], timeout_sec=30)
    ok2, code2, obj2, body2, dt2 = _http_json("GET", f"{TOOLS_URL}/health", timeout_sec=8)
    if ok2 and obj2.get("status") == "ok":
        return Check("tools_api_health", True, f"restarted, HTTP {code2}", dt + dt2)
    return Check("tools_api_health", False, body2 or body or f"HTTP {code2}", dt + dt2)


def _check_summarize_circuit() -> List[Check]:
    out: List[Check] = []
    # reset metrics for this test slice
    metrics = METRICS_DIR / "summarize_requests.jsonl"
    metrics.parent.mkdir(parents=True, exist_ok=True)
    metrics.write_text("", encoding="utf-8")

    bodies = [
        {"text": "第一輪摘要壓測：預期上游可能逾時。"},
        {"text": "第二輪摘要壓測：若再逾時應開啟 circuit。"},
        {"text": "第三輪摘要壓測：應快速降級，不等待長逾時。"},
    ]
    dts: List[float] = []
    routes: List[str] = []
    for i, b in enumerate(bodies, start=1):
        ok, code, obj, body, dt = _http_json("POST", f"{TOOLS_URL}/summarize", body=b, timeout_sec=50)
        dts.append(dt)
        route = ""
        try:
            route = str(((obj.get("result") or {}).get("provider") or ""))
        except Exception:
            route = ""
        routes.append(route)
        out.append(Check(f"summarize_round_{i}", ok and code == 200, f"route={route or '-'} code={code}", dt))

    okh, codeh, objh, bodyh, dth = _http_json("GET", f"{TOOLS_URL}/summarize/health", timeout_sec=8)
    cb_open = bool((((objh.get("circuit_breaker") or {}).get("open")) if isinstance(objh, dict) else False))
    out.append(Check("summarize_health_endpoint", okh and codeh == 200, f"cb_open={cb_open}", dth))

    fast_ok = (len(dts) >= 3 and dts[2] < 2.0) or (len(routes) >= 3 and routes[2] == "circuit_open_degraded")
    out.append(Check("summarize_circuit_fast_path", fast_ok, f"dts={[round(x,3) for x in dts]} routes={routes}"))
    return out


def _check_transcribe_dual() -> Check:
    wav = str(MAGI_ROOT / "tmp_qa" / "qa_two_speakers.wav")
    ok, code, obj, body, dt = _http_json("POST", f"{TOOLS_URL}/collab/transcribe", body={"audio_path": wav}, timeout_sec=120)
    if not ok or code != 200:
        return Check("transcribe_dual", False, f"HTTP={code} {body[:200]}", dt)
    spk = int(obj.get("speaker_count_estimate", 0) or 0)
    segs = obj.get("segments") if isinstance(obj.get("segments"), list) else []
    return Check("transcribe_dual", spk >= 2 and len(segs) >= 2, f"speaker_count_estimate={spk}, segments={len(segs)}", dt)


def _check_translate() -> Check:
    payload = {
        "text": "請翻譯為繁體中文: This contract shall be governed by the laws of Taiwan.",
        "target_lang": "繁體中文",
        "source_lang": "English",
        "mode": "full",
    }
    ok, code, obj, body, dt = _http_json("POST", f"{TOOLS_URL}/collab/translate", body=payload, timeout_sec=60)
    if not ok or code not in (200, 400):
        return Check("translate_smoke", False, f"HTTP={code} {body[:220]}", dt)
    # allow degraded success style but require success true
    succ = bool(obj.get("success"))
    txt = str(obj.get("text") or "")
    return Check("translate_smoke", succ and len(txt.strip()) > 0, f"success={succ}, len={len(txt)}", dt)


def _check_autopilot_selftest() -> Check:
    ok, out, err, rc, dt = _run_cmd([str(VENV_PY), str(AUTOPILOT), "--task", "self_test"], timeout_sec=120)
    if not ok:
        return Check("autopilot_self_test", False, f"rc={rc} err={err[:220]}", dt)
    try:
        obj = json.loads((out or "{}").strip())
    except Exception:
        obj = {}
    return Check("autopilot_self_test", bool(obj.get("ok")), f"blocked={obj.get('blocked')} report={obj.get('report_json','')}", dt)


def _check_autopilot_tick_light() -> Check:
    env = os.environ.copy()
    env.update(
        {
            "MAGI_TICK_LIGHT_MODE": "1",
            "MAGI_BIG_BRAIN_HEALTH_ENABLE": "0",
            "MAGI_ENABLE_FILE_REVIEW_SITE_CHECK_TICK": "0",
            "MAGI_ENABLE_JUDICIAL_API_DAY_PROCESS": "0",
            "MAGI_LAF_CONDITION_ENABLE": "0",
            "MAGI_LAF_DEEP_EXTRACT_ENABLE": "0",
        }
    )
    ok, out, err, rc, dt = _run_cmd([str(VENV_PY), str(AUTOPILOT), "--task", "tick"], timeout_sec=180, env=env)
    if rc not in (0, 2):
        return Check("autopilot_tick_light", False, f"rc={rc} err={err[:220]}", dt)
    try:
        obj = json.loads((out or "{}").strip())
        report = str(obj.get("report_json") or "")
        blocked = bool(obj.get("blocked"))
        detail = f"ok={obj.get('ok')} blocked={blocked} report={report}"
        completed = bool(report and os.path.exists(report))
        within_budget = dt < 180.0
        # verify cooldown skip is present when applicable
        if report and os.path.exists(report):
            rp = json.loads(Path(report).read_text(encoding="utf-8"))
            tr = (((rp.get("details") or {}).get("steps") or {}).get("transcript_sync") or {})
            if tr.get("skipped") and "captcha cooldown" in str(tr.get("reason", "")):
                detail += " cooldown_skip=yes"
        # light mode is a runtime smoke: blocked business queues should not fail this check.
        return Check("autopilot_tick_light", completed and within_budget, detail, dt)
    except Exception as e:
        return Check("autopilot_tick_light", False, f"parse_failed: {e}", dt)


def _check_day3_report_from_ts() -> Check:
    cmd = [
        "python3",
        str(DAY3_REPORT),
        "--hours",
        "24",
        "--from-ts",
        "2026-03-05T12:17:00",
    ]
    ok, out, err, rc, dt = _run_cmd(cmd, timeout_sec=60)
    if not ok:
        return Check("day3_report_from_ts", False, f"rc={rc} err={err[:220]}", dt)
    try:
        obj = json.loads((out or "{}").strip())
        rep = obj.get("report") if isinstance(obj.get("report"), dict) else {}
        same = str(rep.get("window_start") or "").startswith("2026-03-05T12:17")
        txt = ((obj.get("txt_export") or {}).get("path") if isinstance(obj.get("txt_export"), dict) else "")
        return Check("day3_report_from_ts", same and bool(txt), f"window_start={rep.get('window_start')} txt={txt}", dt)
    except Exception as e:
        return Check("day3_report_from_ts", False, f"parse_failed: {e}", dt)


def render_report(checks: List[Check]) -> str:
    total = len(checks)
    ok_count = sum(1 for c in checks if c.ok)
    fail_count = total - ok_count
    lines: List[str] = []
    lines.append("MAGI DAY3 非24H全量測試報告")
    lines.append(f"生成時間: {datetime.now().isoformat(timespec='seconds')}")
    lines.append("")
    lines.append("一、總覽")
    lines.append(f"- total={total}, pass={ok_count}, fail={fail_count}, pass_rate={round((ok_count/total*100.0) if total else 0.0,2)}%")
    lines.append("")
    lines.append("二、測試明細")
    for c in checks:
        icon = "PASS" if c.ok else "FAIL"
        lines.append(f"- [{icon}] {c.name}: {c.detail} (latency={round(c.latency_sec,3)}s)")
    return "\n".join(lines).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="DAY3 non-24h comprehensive test suite")
    parser.add_argument("--strict", action="store_true", help="exit non-zero if any check fails")
    args = parser.parse_args()

    checks: List[Check] = []
    checks.append(_ensure_tools_api_up())
    checks.extend(_check_summarize_circuit())
    checks.append(_check_translate())
    checks.append(_check_transcribe_dual())
    checks.append(_check_autopilot_selftest())
    checks.append(_check_autopilot_tick_light())
    checks.append(_check_day3_report_from_ts())

    txt = render_report(checks)
    export_txt = _load_export_txt()
    out = export_txt(txt, prefix="qa_day3_non24h_test_suite")

    payload = {
        "success": True,
        "checks": [
            {
                "name": c.name,
                "ok": c.ok,
                "detail": c.detail,
                "latency_sec": round(c.latency_sec, 3),
            }
            for c in checks
        ],
        "summary": {
            "total": len(checks),
            "pass": sum(1 for c in checks if c.ok),
            "fail": sum(1 for c in checks if not c.ok),
        },
        "txt_export": out,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if args.strict and any(not c.ok for c in checks):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
