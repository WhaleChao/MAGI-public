#!/usr/bin/env python3
"""Live-safe check for Gemma distillation readiness and deployment guardrails.

This does not train or deploy a model. It validates the current distillation
corpus, quality filters, and pending-deploy safety state.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OUTPUT_PATH = ROOT / ".runtime" / "distill_live_latest.json"


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for raw in env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _latest_metric(metrics_path: Path) -> dict:
    if not metrics_path.exists():
        return {}
    lines = [ln for ln in metrics_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        return {}
    try:
        return json.loads(lines[-1])
    except Exception:
        return {"parse_error": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-usable", type=int, default=50)
    parser.add_argument("--json-out", default=str(OUTPUT_PATH))
    args = parser.parse_args()

    _load_env()
    from scripts import nightly_distill_gemma as nightly
    from scripts import train_gemma_e4b_lora as train_mod
    from skills.bridge import distill_collector

    distill_dir = Path(os.environ.get("GEMMA_DISTILL_DIR", str(Path.home() / ".omlx/training/gemma-distill")))
    raw_path = distill_dir / "raw_pairs.jsonl"
    train_path = distill_dir / "train.jsonl"
    eval_path = distill_dir / "eval.jsonl"
    pending_path = distill_dir / "pending_deploy.json"
    metrics_path = distill_dir / "metrics.jsonl"

    checks: list[dict] = []
    usable_stats = distill_collector.count_usable_pairs()
    checks.append({
        "name": "usable_distill_pairs",
        "ok": int(usable_stats.get("usable") or 0) >= args.min_usable,
        "detail": usable_stats,
    })

    bad_output = "Analysis: let's think step by step. 這是混入思考痕跡的回答。"
    gate_ok, gate_reasons, gate_stats = train_mod._validate_output_gate(bad_output)
    checks.append({
        "name": "output_gate_rejects_reasoning_trace",
        "ok": gate_ok is False and "english_thinking_trace" in gate_reasons,
        "detail": {"reasons": gate_reasons, "stats": gate_stats},
    })

    good_output = (
        "損害賠償係指因侵權或債務不履行所生之財產與非財產損害，"
        "被害人得依民法請求回復原狀或金錢賠償，並應具體說明因果關係、可歸責事由與損害範圍。"
    )
    gate_ok, gate_reasons, gate_stats = train_mod._validate_output_gate(good_output)
    checks.append({
        "name": "output_gate_accepts_clean_traditional_chinese",
        "ok": gate_ok is True and not gate_reasons,
        "detail": {"reasons": gate_reasons, "stats": gate_stats},
    })

    reject_reasons = distill_collector._reject_reasons(
        "## 裁判要旨\n法院認為契約解除後仍應依民法規定回復原狀。\n## 法院見解\n法院依卷證資料確認事實。",
        prompt="### EXECUTE WFGY PROTOCOL\nOutput your thought process before final answer.",
        source="nim_resummary",
    )
    checks.append({
        "name": "collector_rejects_prompt_trace_requests",
        "ok": "prompt_requests_reasoning_trace" in reject_reasons,
        "detail": {"reasons": reject_reasons},
    })

    pending = {}
    if pending_path.exists():
        try:
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
        except Exception as exc:
            pending = {"parse_error": str(exc)}

    latest_metric = _latest_metric(metrics_path)
    pending_status_ok = True
    if pending:
        deploy_allowed = bool(pending.get("deploy_allowed", True))
        validation = pending.get("validate_result") or {}
        validation_pass = bool(validation.get("validation_pass") or validation.get("success"))
        if validation_pass is False and deploy_allowed is True:
            pending_status_ok = False
    checks.append({
        "name": "pending_deploy_matches_validation_gate",
        "ok": pending_status_ok,
        "detail": {
            "version": pending.get("version"),
            "status": pending.get("status"),
            "deploy_allowed": pending.get("deploy_allowed"),
            "validation": pending.get("validate_result") or {},
        },
    })

    blocked_deploy_ok = True
    blocked_deploy_rc = None
    version = str(pending.get("version") or "")
    if pending.get("status") == "rejected" and pending.get("deploy_allowed") is False and version:
        blocked_deploy_rc = nightly.deploy_model(version)
        blocked_deploy_ok = blocked_deploy_rc != 0
    checks.append({
        "name": "rejected_pending_cannot_deploy",
        "ok": blocked_deploy_ok,
        "detail": {"version": version, "deploy_rc": blocked_deploy_rc},
    })

    result = {
        "success": all(item["ok"] for item in checks),
        "distill_dir": str(distill_dir),
        "files": {
            "raw_pairs": {"exists": raw_path.exists(), "size": raw_path.stat().st_size if raw_path.exists() else 0},
            "train": {"exists": train_path.exists(), "size": train_path.stat().st_size if train_path.exists() else 0},
            "eval": {"exists": eval_path.exists(), "size": eval_path.stat().st_size if eval_path.exists() else 0},
            "pending_deploy": {"exists": pending_path.exists(), "size": pending_path.stat().st_size if pending_path.exists() else 0},
            "metrics": {"exists": metrics_path.exists(), "size": metrics_path.stat().st_size if metrics_path.exists() else 0},
        },
        "latest_metric": latest_metric,
        "checks": checks,
    }
    out = Path(args.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    for item in checks:
        status = "✅" if item["ok"] else "❌"
        print(f"{status} {item['name']} {item['detail']}")
    print(f"JSON: {out}")
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
