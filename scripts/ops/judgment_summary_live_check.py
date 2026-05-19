#!/usr/bin/env python3
"""Live check for judgment/practice-insight summarization.

Uses a real normalized Judicial Yuan TXT cache item, runs MAGI's judgment
summary path, and verifies that the output is source-bound and free of prompt
or retired reasoning scaffolding leaks.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DEFAULT_CACHE = Path.home() / ".cache" / "judgment_collector" / "judicial_api" / "normalized"
OUTPUT_PATH = ROOT / ".runtime" / "judgment_summary_live_latest.json"

BAD_MARKERS = (
    "Thought:",
    "Action:",
    "Observation:",
    "EXECUTE WFGY",
    "WFGY",
    "THE 7-STEP",
    "你是一位精確的法律助理",
    "【嚴格規則】",
    "判決內文：",
    "系統降級回覆",
    "本機模型逾時",
    "請稍後重試",
)


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


def _load_judgment_action():
    path = ROOT / "skills" / "judgment-collector" / "action.py"
    spec = importlib.util.spec_from_file_location("judgment_collector_action_live", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _select_source(cache_root: Path, *, max_chars: int) -> tuple[Path, str]:
    candidates: list[tuple[int, Path, str]] = []
    for p in cache_root.rglob("*.txt"):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        body = text.strip()
        if len(body) < 1800:
            continue
        if not any(marker in body for marker in ("主文", "理由", "事實及理由", "本院")):
            continue
        candidates.append((len(body), p, body))
    if not candidates:
        raise FileNotFoundError(f"no suitable normalized judgment TXT under {cache_root}")
    candidates.sort(key=lambda item: abs(item[0] - min(max_chars, 9000)))
    _size, path, text = candidates[0]
    return path, text[:max_chars]


def _quality(summary: str, action_mod, case_reason: str) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    text = str(summary or "").strip()
    if len(text) < 80:
        reasons.append("too_short")
    if "## 實務見解" not in text and "本判決無可擷取之實務見解" not in text:
        reasons.append("missing_practice_insight_section")
    for marker in BAD_MARKERS:
        if marker in text:
            reasons.append(f"bad_marker:{marker}")
    if getattr(action_mod, "_is_degraded_summary")(text, case_reason):
        reasons.append("degraded_summary_detector")
    if re.search(r"[\u4e00-\u9fff]", text) is None:
        reasons.append("missing_cjk")
    return not reasons, reasons


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", default=str(DEFAULT_CACHE))
    parser.add_argument("--max-chars", type=int, default=9000)
    parser.add_argument("--timeout", type=int, default=160)
    parser.add_argument("--json-out", default=str(OUTPUT_PATH))
    args = parser.parse_args()

    _load_env()
    action = _load_judgment_action()
    source_path, source_text = _select_source(Path(args.cache_root).expanduser(), max_chars=args.max_chars)
    case_reason = source_path.stem

    started = time.monotonic()
    summary = action._summarize_judgment(source_text, case_reason, timeout_sec=args.timeout)
    elapsed = round(time.monotonic() - started, 2)
    ok, reasons = _quality(summary, action, case_reason)
    meta = dict(getattr(action, "_LAST_SUMMARY_META", {}) or {})

    result = {
        "success": ok,
        "source_path": str(source_path),
        "source_chars": len(source_text),
        "summary_chars": len(summary or ""),
        "elapsed_sec": elapsed,
        "summary_meta": meta,
        "quality_reasons": reasons,
        "summary_preview": str(summary or "")[:500],
    }
    out = Path(args.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    status = "✅" if ok else "❌"
    route = meta.get("route", "")
    print(f"{status} judgment_summary_live route={route} source={source_path.name} chars={len(summary or '')} elapsed={elapsed}s")
    if reasons:
        print("reasons:", ", ".join(reasons))
    print(f"JSON: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
