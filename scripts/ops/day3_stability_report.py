#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DAY3 stability report
- Schedule success (tick/nightly)
- Summarize p95 + timeout rate
- Transcript captcha defer queue stats
- Transcribe dual-speaker pass rate
- External connectivity checks
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import socket
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib import request as _urlreq

MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", str(Path(__file__).resolve().parents[2])))
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from api.runtime_paths import get_autopilot_runs_dir, get_metrics_dir

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    pass

METRICS_DIR = get_metrics_dir()
RUNS_DIR = Path(os.environ.get("MAGI_AUTOPILOT_RUNS_DIR", str(get_autopilot_runs_dir())))
SUMMARY_METRICS_PATH = Path(os.environ.get("MAGI_SUMMARY_METRICS_PATH", str(METRICS_DIR / "summarize_requests.jsonl")))
TRANSCRIBE_METRICS_PATH = Path(os.environ.get("MAGI_TRANSCRIBE_METRICS_PATH", str(METRICS_DIR / "transcribe_requests.jsonl")))
TRANSCRIPT_CAPTCHA_QUEUE_PATH = Path(os.environ.get("MAGI_TRANSCRIPT_CAPTCHA_DEFER_PATH", str(RUNS_DIR / "_pending_transcript_captcha.jsonl")))
TRANSCRIPT_MANUAL_QUEUE_PATH = Path(
    os.environ.get(
        "MAGI_TRANSCRIPT_MANUAL_QUEUE_PATH",
        str(MAGI_ROOT / "static" / "transcript_manual_queue.jsonl"),
    )
)
RED_PHONE_OUTBOX_PATH = Path(
    os.environ.get(
        "MAGI_RED_PHONE_OUTBOX_FILE",
        str(MAGI_ROOT / ".agent" / "red_phone_outbox.json"),
    )
)
RED_PHONE_DELIVERY_LOG_PATH = Path(
    os.environ.get(
        "MAGI_RED_PHONE_DELIVERY_LOG",
        str(MAGI_ROOT / ".agent" / "red_phone_delivery.jsonl"),
    )
)
EXPORT_MOD = MAGI_ROOT / "skills" / "ops" / "export_text.py"


@dataclass
class RunRow:
    ts: datetime
    task: str
    ok: bool
    blockers: List[str]


@dataclass
class HttpProbe:
    name: str
    ok: bool
    detail: str


def _parse_ts(v: str) -> datetime | None:
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_jsonl(path: Path, start: datetime) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = raw.strip()
        if not s:
            continue
        try:
            row = json.loads(s)
        except Exception:
            continue
        ts = _parse_ts(
            str(
                row.get("ts")
                or row.get("created_at")
                or row.get("timestamp")
                or row.get("updated_at")
                or ""
            )
        )
        if not ts:
            continue
        if ts >= start:
            out.append(row)
    return out


def _percentile(values: List[float], p: float) -> float:
    arr = sorted(float(x) for x in values if x is not None)
    if not arr:
        return 0.0
    if p <= 0:
        return arr[0]
    if p >= 100:
        return arr[-1]
    k = (len(arr) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(arr) - 1)
    if f == c:
        return arr[f]
    return arr[f] + (arr[c] - arr[f]) * (k - f)


def collect_autopilot(start: datetime) -> Dict[str, Any]:
    rows: List[RunRow] = []
    if RUNS_DIR.exists():
        for p in RUNS_DIR.glob("*/report.json"):
            obj = _load_json(p)
            ts = _parse_ts(str(obj.get("ts") or ""))
            if not ts or ts < start:
                continue
            task = str(obj.get("task") or "").strip()
            if task not in {"tick", "nightly", "self_test"}:
                continue
            details = obj.get("details") if isinstance(obj.get("details"), dict) else {}
            blockers = details.get("blockers") if isinstance(details, dict) else []
            rows.append(RunRow(ts=ts, task=task, ok=bool(obj.get("ok")), blockers=[str(x) for x in (blockers or [])]))

    rows.sort(key=lambda x: x.ts)
    task_rows = [r for r in rows if r.task in {"tick", "nightly"}]
    total = len(task_rows)
    ok_count = sum(1 for r in task_rows if r.ok)
    success_rate = (ok_count / total * 100.0) if total else 0.0

    blocker_counter = Counter()
    for r in rows:
        for b in r.blockers:
            blocker_counter[b] += 1

    return {
        "total": total,
        "ok": ok_count,
        "fail": total - ok_count,
        "success_rate": round(success_rate, 2),
        "top_blockers": blocker_counter.most_common(10),
        "recent_runs": [
            {
                "ts": r.ts.isoformat(timespec="seconds"),
                "task": r.task,
                "ok": r.ok,
                "blockers": r.blockers,
            }
            for r in rows[-20:]
        ],
    }


def collect_summary_metrics(start: datetime) -> Dict[str, Any]:
    rows = _load_jsonl(SUMMARY_METRICS_PATH, start)
    latencies = [float(r.get("latency_ms") or 0) / 1000.0 for r in rows if float(r.get("latency_ms") or 0) >= 0]
    hard_timeouts = sum(1 for r in rows if bool(r.get("timeout")))
    upstream_timeouts = sum(1 for r in rows if bool(r.get("upstream_timeout")))
    success = sum(1 for r in rows if bool(r.get("success")))
    total = len(rows)
    timeout_rate = (hard_timeouts / total * 100.0) if total else 0.0
    upstream_timeout_rate = (upstream_timeouts / total * 100.0) if total else 0.0
    success_rate = (success / total * 100.0) if total else 0.0
    return {
        "total": total,
        "success": success,
        "success_rate": round(success_rate, 2),
        "timeouts": hard_timeouts,
        "upstream_timeouts": upstream_timeouts,
        "timeout_rate": round(timeout_rate, 2),
        "upstream_timeout_rate": round(upstream_timeout_rate, 2),
        "p50_sec": round(_percentile(latencies, 50), 2),
        "p95_sec": round(_percentile(latencies, 95), 2),
    }


def collect_transcribe_metrics(start: datetime) -> Dict[str, Any]:
    rows = _load_jsonl(TRANSCRIBE_METRICS_PATH, start)
    dual_candidates: List[Dict[str, Any]] = []
    for r in rows:
        p = str(r.get("audio_path") or "")
        low = p.lower()
        if any(k in low for k in ["two", "dual", "2spk", "twospeaker", "雙人"]):
            dual_candidates.append(r)

    dual_pass = sum(1 for r in dual_candidates if int(r.get("speaker_count_estimate") or 0) >= 2)
    dual_total = len(dual_candidates)
    dual_pass_rate = (dual_pass / dual_total * 100.0) if dual_total else 0.0

    return {
        "total": len(rows),
        "dual_total": dual_total,
        "dual_pass": dual_pass,
        "dual_pass_rate": round(dual_pass_rate, 2),
    }


def collect_captcha_queue(start: datetime) -> Dict[str, Any]:
    rows = _load_jsonl(TRANSCRIPT_CAPTCHA_QUEUE_PATH, start)
    retry_attempted = sum(1 for r in rows if bool(r.get("retry_attempted")))
    retry_ok = sum(1 for r in rows if bool(r.get("retry_ok")))
    return {
        "total": len(rows),
        "retry_attempted": retry_attempted,
        "retry_ok": retry_ok,
        "path": str(TRANSCRIPT_CAPTCHA_QUEUE_PATH),
    }


def collect_transcript_manual_queue(start: datetime) -> Dict[str, Any]:
    rows = _load_jsonl(TRANSCRIPT_MANUAL_QUEUE_PATH, start)
    pending = sum(1 for r in rows if str(r.get("status") or "").strip().lower() in {"pending", "pending_manual", "manual_required"})
    by_action = Counter(str(r.get("action") or "unknown") for r in rows)
    return {
        "total": len(rows),
        "pending": pending,
        "by_action": by_action.most_common(10),
        "path": str(TRANSCRIPT_MANUAL_QUEUE_PATH),
    }


def collect_notify_outbox(start: datetime) -> Dict[str, Any]:
    pending = 0
    try:
        if RED_PHONE_OUTBOX_PATH.exists():
            arr = json.loads(RED_PHONE_OUTBOX_PATH.read_text(encoding="utf-8")) or []
            if isinstance(arr, list):
                pending = len([x for x in arr if isinstance(x, dict)])
    except Exception:
        pending = 0
    rows = _load_jsonl(RED_PHONE_DELIVERY_LOG_PATH, start)
    by_event = Counter(str(r.get("event") or "unknown") for r in rows)
    return {
        "pending": pending,
        "events_total": len(rows),
        "sent": int(by_event.get("sent", 0)),
        "failed": int(by_event.get("failed", 0)),
        "recovered": int(by_event.get("outbox_recovered", 0)),
        "dropped": int(by_event.get("outbox_drop", 0)),
        "path_outbox": str(RED_PHONE_OUTBOX_PATH),
        "path_delivery_log": str(RED_PHONE_DELIVERY_LOG_PATH),
    }


def _http_probe(name: str, url: str, timeout_sec: int = 5) -> HttpProbe:
    try:
        req = _urlreq.Request(url=url, method="GET")
        with _urlreq.urlopen(req, timeout=max(2, int(timeout_sec))) as resp:  # nosec B310
            code = int(getattr(resp, "status", 200))
            if 200 <= code < 300:
                return HttpProbe(name=name, ok=True, detail=f"{url} -> HTTP {code}")
            return HttpProbe(name=name, ok=False, detail=f"{url} -> HTTP {code}")
    except Exception as e:
        return HttpProbe(name=name, ok=False, detail=f"{url} -> {type(e).__name__}: {e}")


def _tcp_probe(name: str, host: str, port: int, timeout_sec: float = 1.8) -> HttpProbe:
    try:
        with socket.create_connection((host, int(port)), timeout=max(0.8, float(timeout_sec))):
            return HttpProbe(name=name, ok=True, detail=f"{host}:{port} reachable")
    except Exception as e:
        return HttpProbe(name=name, ok=False, detail=f"{host}:{port} {type(e).__name__}: {e}")


def collect_connectivity() -> Dict[str, Any]:
    probes: List[HttpProbe] = []
    try:
        from api.routing.service_registry import get_service_url as _gsurl
        _tools_default = _gsurl("tools_api")
    except Exception:
        _tools_default = "http://127.0.0.1:5003"
    tools_url = str(os.environ.get("MAGI_TOOLS_URL", _tools_default)).rstrip("/")
    probes.append(_http_probe("tools_api_health", f"{tools_url}/health", timeout_sec=5))

    b_remote_enabled = str(os.environ.get("BALTHASAR_REMOTE_ENABLED", "0")).strip().lower() in {"1", "true", "yes", "on"}
    if b_remote_enabled:
        try:
            from api.routing.node_registry import get_node_ip as _get_node_ip
            _b_default = _get_node_ip("balthasar") or ""
        except Exception:
            _b_default = ""
        b_host = str(os.environ.get("BALTHASAR_HOST", _b_default)).strip()
        b_port = int(os.environ.get("BALTHASAR_PORT", "5002") or "5002")
        fallbacks = [x.strip() for x in str(os.environ.get("BALTHASAR_FALLBACK_HOSTS", "") or "").split(",") if x.strip()]
        checked = []
        for h in [b_host] + fallbacks:
            key = h.lower()
            if not h or key in checked:
                continue
            checked.append(key)
            probes.append(_http_probe(f"balthasar_health[{h}]", f"http://{h}:{b_port}/health", timeout_sec=4))
    else:
        probes.append(
            HttpProbe(
                name="balthasar_health",
                ok=True,
                detail="skip (BALTHASAR_REMOTE_ENABLED=0)",
            )
        )

    db_host = str(os.environ.get("MAGI_REMOTE_DB_HOST", "127.0.0.1") or "127.0.0.1").strip()
    db_port = int(os.environ.get("MAGI_REMOTE_DB_PORT", "3306") or "3306")
    probes.append(_tcp_probe("remote_db_tcp", db_host, db_port, timeout_sec=1.8))

    ok_count = sum(1 for p in probes if p.ok)
    return {
        "total": len(probes),
        "ok": ok_count,
        "fail": len(probes) - ok_count,
        "probes": [{"name": p.name, "ok": p.ok, "detail": p.detail} for p in probes],
    }


def _load_export_txt():
    spec = importlib.util.spec_from_file_location("export_text", str(EXPORT_MOD))
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod.export_txt


def render_txt(rep: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("MAGI DAY3 穩定度檢核報告")
    lines.append(f"生成時間: {rep.get('generated_at')}")
    lines.append(f"觀測區間: {rep.get('window_start')} ~ {rep.get('window_end')} (近 {rep.get('window_hours')}h)")
    if str(rep.get("from_ts") or "").strip():
        lines.append(f"下限起算(from-ts): {rep.get('from_ts')}")
    lines.append("")

    ap = rep.get("autopilot") or {}
    lines.append("一、排程穩定度（tick/nightly）")
    lines.append(f"- success_rate: {ap.get('success_rate', 0)}% ({ap.get('ok', 0)}/{ap.get('total', 0)})")
    lines.append(f"- DAY3門檻: >= 80% -> {'PASS' if float(ap.get('success_rate', 0)) >= 80.0 else 'FAIL'}")

    sm = rep.get("summary") or {}
    lines.append("")
    lines.append("二、/summarize 指標")
    lines.append(
        f"- total={sm.get('total', 0)}, success_rate={sm.get('success_rate', 0)}%, "
        f"timeouts={sm.get('timeouts', 0)}, timeout_rate={sm.get('timeout_rate', 0)}%, "
        f"upstream_timeouts={sm.get('upstream_timeouts', 0)}, upstream_timeout_rate={sm.get('upstream_timeout_rate', 0)}%, "
        f"p50={sm.get('p50_sec', 0)}s, p95={sm.get('p95_sec', 0)}s"
    )
    lines.append(f"- DAY3門檻: p95 < 45s -> {'PASS' if float(sm.get('p95_sec', 0)) < 45.0 else 'FAIL'}")
    lines.append(f"- DAY3門檻: timeout_rate < 10% -> {'PASS' if float(sm.get('timeout_rate', 0)) < 10.0 else 'FAIL'}")

    tq = rep.get("transcript_queue") or {}
    lines.append("")
    lines.append("三、transcript captcha defer queue")
    lines.append(
        f"- queue_count={tq.get('total', 0)}, retry_attempted={tq.get('retry_attempted', 0)}, "
        f"retry_ok={tq.get('retry_ok', 0)}"
    )
    lines.append(f"- queue_path={tq.get('path', '-')}")

    tm = rep.get("transcript_manual_queue") or {}
    lines.append("")
    lines.append("四、transcript 人工佇列")
    lines.append(
        f"- total={tm.get('total', 0)}, pending={tm.get('pending', 0)}"
    )
    if tm.get("by_action"):
        lines.append("- by_action:")
        for item in tm.get("by_action")[:6]:
            lines.append(f"  - {item[0]}: {item[1]}")
    lines.append(f"- queue_path={tm.get('path', '-')}")

    no = rep.get("notify_outbox") or {}
    lines.append("")
    lines.append("五、通知送達 / outbox")
    lines.append(
        f"- outbox_pending={no.get('pending', 0)}, events={no.get('events_total', 0)}, "
        f"sent={no.get('sent', 0)}, failed={no.get('failed', 0)}, recovered={no.get('recovered', 0)}, dropped={no.get('dropped', 0)}"
    )
    lines.append(f"- outbox_path={no.get('path_outbox', '-')}")
    lines.append(f"- delivery_log_path={no.get('path_delivery_log', '-')}")

    tr = rep.get("transcribe") or {}
    lines.append("")
    lines.append("六、逐字稿雙人辨識")
    lines.append(
        f"- transcribe_total={tr.get('total', 0)}, dual_test_total={tr.get('dual_total', 0)}, "
        f"dual_pass={tr.get('dual_pass', 0)}, dual_pass_rate={tr.get('dual_pass_rate', 0)}%"
    )
    lines.append(f"- DAY3門檻: dual_pass_rate=100%（雙人測試） -> {'PASS' if int(tr.get('dual_total', 0)) > 0 and float(tr.get('dual_pass_rate', 0)) >= 100.0 else 'FAIL'}")

    conn = rep.get("connectivity") or {}
    lines.append("")
    lines.append("七、外網/服務連線")
    lines.append(f"- probes ok/fail: {conn.get('ok', 0)}/{conn.get('fail', 0)}")
    for p in conn.get("probes") or []:
        lines.append(f"- [{'OK' if p.get('ok') else 'FAIL'}] {p.get('name')}: {p.get('detail')}")

    top = ap.get("top_blockers") or []
    lines.append("")
    lines.append("八、主要阻塞（Top）")
    if not top:
        lines.append("- 無")
    else:
        for b, c in top[:8]:
            lines.append(f"- {b}: {c}")

    return "\n".join(lines).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="DAY3 stability report")
    ap.add_argument("--hours", type=int, default=24, help="rolling window hours")
    ap.add_argument("--from-ts", type=str, default="", help="optional ISO timestamp lower bound")
    args = ap.parse_args()

    now = datetime.now()
    start = now - timedelta(hours=max(1, int(args.hours)))
    from_ts = str(args.from_ts or "").strip()
    if from_ts:
        try:
            ts_from = datetime.fromisoformat(from_ts)
            if ts_from > start:
                start = ts_from
        except Exception:
            pass

    report = {
        "generated_at": now.isoformat(timespec="seconds"),
        "window_hours": int(args.hours),
        "from_ts": from_ts,
        "window_start": start.isoformat(timespec="seconds"),
        "window_end": now.isoformat(timespec="seconds"),
        "autopilot": collect_autopilot(start),
        "summary": collect_summary_metrics(start),
        "transcript_queue": collect_captcha_queue(start),
        "transcript_manual_queue": collect_transcript_manual_queue(start),
        "notify_outbox": collect_notify_outbox(start),
        "transcribe": collect_transcribe_metrics(start),
        "connectivity": collect_connectivity(),
    }

    txt = render_txt(report)
    export_txt = _load_export_txt()
    out = export_txt(txt, prefix="qa_day3_stability_report")

    payload = {
        "success": True,
        "report": report,
        "txt_export": out,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
