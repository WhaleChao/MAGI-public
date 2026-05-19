#!/usr/bin/env python3
"""Controlled live stress gate for OSC web routes.

The goal is to exercise the OSC web surface with real routing and the real
configured DB, while keeping load bounded enough for the desktop host.
It does not submit portal forms or mutate production case data.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse


MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))


def _load_env() -> None:
    env = MAGI_ROOT / ".env"
    if not env.exists():
        return
    for raw in env.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass
class Sample:
    name: str
    ok: bool
    status: int | str
    elapsed_ms: float
    detail: str = ""


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(round((len(ordered) - 1) * pct))
    return round(ordered[max(0, min(idx, len(ordered) - 1))], 2)


def _summarize(samples: list[Sample]) -> dict:
    latencies = [s.elapsed_ms for s in samples if s.ok]
    failures = [asdict(s) for s in samples if not s.ok]
    return {
        "total": len(samples),
        "ok": len(samples) - len(failures),
        "fail": len(failures),
        "avg_ms": round(statistics.mean(latencies), 2) if latencies else 0.0,
        "p95_ms": _percentile(latencies, 0.95),
        "max_ms": round(max(latencies), 2) if latencies else 0.0,
        "failures": failures[:20],
    }


def _run_json(cmd: list[str], timeout: int = 45) -> dict:
    proc = subprocess.run(
        cmd,
        cwd=MAGI_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )
    raw = (proc.stdout or "").strip()
    try:
        return json.loads(raw)
    except Exception:
        return {"ok": False, "raw": raw[-1000:], "returncode": proc.returncode}


def _resource_snapshot(stage: str) -> dict:
    payload = _run_json([sys.executable, "scripts/ops/resource_governor.py", "--json", "status"], timeout=45)
    return {"stage": stage, **payload}


def _build_app():
    from flask import Flask
    from flask_login import LoginManager, UserMixin

    from api.blueprints.osc_accounting import osc_accounting_bp
    from api.blueprints.osc_cases import osc_bp
    from api.blueprints.osc_debt import osc_debt_bp
    from api.blueprints.osc_files import osc_files_bp
    from api.blueprints.osc_gcal import osc_gcal_bp
    from api.blueprints.osc_pdf import osc_pdf_bp
    from api.blueprints.osc_settings import osc_settings_bp

    app = Flask(__name__, template_folder=str(MAGI_ROOT / "templates"))
    app.config["TESTING"] = True
    app.config["LOGIN_DISABLED"] = True
    app.config["PROPAGATE_EXCEPTIONS"] = False
    app.secret_key = "osc-web-stress-live"
    login = LoginManager()
    login.init_app(app)

    class _StressUser(UserMixin):
        id = "osc-stress"

    @login.user_loader
    def _load_user(_user_id):
        return _StressUser()

    app.register_blueprint(osc_bp)
    app.register_blueprint(osc_settings_bp)
    app.register_blueprint(osc_accounting_bp)
    app.register_blueprint(osc_files_bp)
    app.register_blueprint(osc_pdf_bp)
    app.register_blueprint(osc_debt_bp)
    app.register_blueprint(osc_gcal_bp)
    return app


def _route_sample(app, name: str, path: str) -> Sample:
    started = time.perf_counter()
    try:
        with app.test_client() as client:
            resp = client.get(path)
            body = resp.get_data()[:500]
        elapsed = (time.perf_counter() - started) * 1000
        ok = 200 <= int(resp.status_code) < 400
        detail = "" if ok else body.decode("utf-8", errors="replace")
        return Sample(name, ok, int(resp.status_code), round(elapsed, 2), detail[:300])
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return Sample(name, False, type(exc).__name__, round(elapsed, 2), str(exc)[:300])


def _health_sample(base_url: str) -> Sample:
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/health", timeout=8) as resp:
            data = resp.read(4096)
        elapsed = (time.perf_counter() - started) * 1000
        ok = int(resp.status) == 200 and b'"db":{"ok":true' in data
        return Sample("daemon_health", ok, int(resp.status), round(elapsed, 2), "" if ok else data.decode("utf-8", "replace")[:300])
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return Sample("daemon_health", False, type(exc).__name__, round(elapsed, 2), str(exc)[:300])


def _file_ops_smoke(app) -> list[Sample]:
    import api.blueprints.osc_files as files_mod

    out_dir = MAGI_ROOT / "exports" / "_osc_stress"
    out_dir.mkdir(parents=True, exist_ok=True)
    sample_file = out_dir / "osc_stress_file_ops.txt"
    sample_file.write_text("MAGI OSC stress file smoke ok\n", encoding="utf-8")

    share_runtime = MAGI_ROOT / ".runtime" / "osc_web_stress_share"
    share_runtime.mkdir(parents=True, exist_ok=True)
    files_mod._SHARE_STORE_PATH = share_runtime / "shares.json"
    files_mod._SHARE_PUBLIC_BASE_FILE = MAGI_ROOT / ".runtime" / "osc_share_public_base_url.txt"
    files_mod._share_cache_dir = lambda: share_runtime / "cache"

    samples: list[Sample] = []
    with app.test_client() as client:
        for name, path in [
            ("file_preview", f"/api/osc/files/preview?path={sample_file}"),
            ("file_content", f"/api/osc/files/content?path={sample_file}&inline=1"),
        ]:
            started = time.perf_counter()
            try:
                resp = client.get(path)
                elapsed = (time.perf_counter() - started) * 1000
                ok = resp.status_code == 200
                samples.append(Sample(name, ok, resp.status_code, round(elapsed, 2), "" if ok else resp.get_data(as_text=True)[:300]))
            except Exception as exc:
                samples.append(Sample(name, False, type(exc).__name__, round((time.perf_counter() - started) * 1000, 2), str(exc)[:300]))

        started = time.perf_counter()
        try:
            resp = client.post("/api/osc/files/share", json={"path": str(sample_file), "ttl_sec": 300})
            elapsed = (time.perf_counter() - started) * 1000
            payload = resp.get_json(silent=True) or {}
            ok = resp.status_code == 200 and bool(payload.get("url"))
            samples.append(Sample("file_share_create", ok, resp.status_code, round(elapsed, 2), "" if ok else str(payload)[:300]))
            if ok:
                token = urlparse(payload["url"]).path.rsplit("/", 1)[-1]
                started = time.perf_counter()
                resp2 = client.get("/s/" + token)
                elapsed = (time.perf_counter() - started) * 1000
                public_ok = resp2.status_code == 200 and b"stress file smoke" in resp2.get_data()
                samples.append(Sample("file_share_public", public_ok, resp2.status_code, round(elapsed, 2), "" if public_ok else resp2.get_data(as_text=True)[:300]))
        except Exception as exc:
            samples.append(Sample("file_share_create", False, type(exc).__name__, round((time.perf_counter() - started) * 1000, 2), str(exc)[:300]))

    try:
        sample_file.unlink(missing_ok=True)
    except Exception:
        pass
    return samples


def run_stress(*, rounds: int, workers: int, base_url: str) -> dict:
    _load_env()
    before = _resource_snapshot("before")
    if before.get("level") == "critical":
        return {"ok": False, "error": "resource_governor_critical_before", "resource": [before]}

    app = _build_app()
    routes = [
        ("dashboard", "/api/osc/dashboard"),
        ("cases", "/api/osc/cases?limit=25"),
        ("todos", "/api/osc/todos?limit=25"),
        ("calendar", "/api/osc/calendar/events?limit=25"),
        ("laf_cases", "/api/osc/laf/cases?limit=25"),
        ("documents", "/api/osc/documents?limit=25"),
        ("accounting_summary", "/api/osc/accounting/summary"),
        ("saas_overview", "/api/osc/saas/overview"),
        ("template_folder", "/api/osc/template-folder"),
        ("folder_roots", "/api/osc/folders/roots"),
    ]

    http_samples: list[Sample] = []
    api_samples: list[Sample] = []
    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as pool:
        futures = []
        for _ in range(max(1, rounds)):
            futures.append(pool.submit(_health_sample, base_url))
            for name, path in routes:
                futures.append(pool.submit(_route_sample, app, name, path))
        for fut in concurrent.futures.as_completed(futures):
            sample = fut.result()
            if sample.name == "daemon_health":
                http_samples.append(sample)
            else:
                api_samples.append(sample)

    file_samples = _file_ops_smoke(app)
    after = _resource_snapshot("after")
    elapsed = round(time.time() - started, 3)

    http_summary = _summarize(http_samples)
    api_summary = _summarize(api_samples)
    file_summary = _summarize(file_samples)
    resource_ok = after.get("level") != "critical"
    ok = (
        http_summary["fail"] == 0
        and api_summary["fail"] == 0
        and file_summary["fail"] == 0
        and resource_ok
        and http_summary["p95_ms"] <= 1500
        and api_summary["p95_ms"] <= 8000
    )
    return {
        "ok": ok,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "rounds": rounds,
        "workers": workers,
        "elapsed_sec": elapsed,
        "thresholds": {"health_p95_ms": 1500, "api_p95_ms": 8000, "resource_not_critical": True},
        "http_health": http_summary,
        "osc_api": api_summary,
        "file_ops": file_summary,
        "resource": [before, after],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=8)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--base-url", default="http://127.0.0.1:5002")
    parser.add_argument("--json-out", default=str(MAGI_ROOT / ".runtime" / "osc_web_stress_live_latest.json"))
    args = parser.parse_args()

    payload = run_stress(rounds=args.rounds, workers=args.workers, base_url=args.base_url)
    out = Path(args.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["json_out"] = str(out)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
