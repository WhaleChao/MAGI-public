#!/usr/bin/env python3
"""
Stress smoke for large-file translation across Discord/Telegram/LINE routing.

Goals:
- Use three English judgments under ~/Desktop/判決
- Verify translate and translate+summary both complete
- Verify FILE_PATH handoff exists for all three channel-style user ids
- Verify degraded fallback still completes when distributed inference crashes
"""

from __future__ import annotations

import argparse
import json
import os
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import re
import time
from pathlib import Path
from typing import Callable

ROOT = Path(_MAGI_ROOT)
JUDGMENT_DIR_DEFAULT = Path("/Users/ai/Desktop/判決")


def _now_ts() -> int:
    return int(time.time())


def _pick_three_pdfs(folder: Path) -> list[Path]:
    files = sorted([p for p in folder.glob("*.pdf") if p.is_file()])
    return files[:3]


def _extract_file_path(reply: str) -> str:
    s = str(reply or "")
    if "|||FILE_PATH|||" not in s:
        return ""
    _, p = s.split("|||FILE_PATH|||", 1)
    return p.strip()


def _read_head(path: Path, n: int = 1200) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            return f.read(n)
    except Exception:
        return ""


def _make_fake_route_functions(mode: str):
    """
    mode=normal   : distributed works
    mode=degraded : distributed always fails, quick-local works
    """
    counters = {"distributed_calls": 0, "quick_calls": 0}

    def _fake_distributed_chat(prompt: str, timeout: int = 120, **kwargs):
        counters["distributed_calls"] += 1
        m = re.search(r"目前段落：(\d+)/(\d+)", prompt or "")
        marker = f"{m.group(1)}/{m.group(2)}" if m else "?"
        if mode == "degraded":
            return {"success": False, "error": "simulated_distributed_crash"}
        return {"success": True, "response": f"【譯文段落 {marker}】", "model": "fake-distributed"}

    def _fake_quick_local_chat(prompt: str, timeout: int = 30, model_hint: str = ""):
        counters["quick_calls"] += 1
        m = re.search(r"目前段落：(\d+)/(\d+)", prompt or "")
        marker = f"{m.group(1)}/{m.group(2)}" if m else "?"
        return {"success": True, "response": f"【本地降級譯文段落 {marker}】", "model": "fake-local"}

    return counters, _fake_distributed_chat, _fake_quick_local_chat


def _run_case(
    orchestrator,
    *,
    file_path: Path,
    user_id: str,
    platform: str,
    prompt: str,
    enable_ingest: bool,
) -> dict:
    os.environ["MAGI_DOC_AUTO_INGEST"] = "1" if enable_ingest else "0"
    t0 = time.time()
    reply = orchestrator.process_message(
        user_id,
        prompt,
        platform=platform,
        role="admin",
        attachment={
            "type": "file",
            "path": str(file_path),
            "filename": file_path.name,
        },
    )
    elapsed_ms = int((time.time() - t0) * 1000)

    path_str = _extract_file_path(reply)
    txt_ok = False
    txt_head = ""
    if path_str:
        p = Path(path_str)
        if p.exists() and p.is_file():
            txt_ok = True
            txt_head = _read_head(p, n=1600)

    has_summary = "摘要" in txt_head
    has_header = "MAGI Translation Output" in txt_head and "[Translated Text]" in txt_head
    has_ingest_note = "doc_key=" in str(reply or "")

    expected_summary = any(k in prompt for k in ["摘要", "summary"])
    ok = bool(path_str) and txt_ok and has_header and ((not expected_summary) or has_summary)

    return {
        "ok": ok,
        "platform": platform,
        "user_id": user_id,
        "file": str(file_path),
        "prompt": prompt,
        "elapsed_ms": elapsed_ms,
        "reply_preview": str(reply or "")[:360],
        "exported_path": path_str,
        "txt_ok": txt_ok,
        "has_header": has_header,
        "has_summary": has_summary,
        "has_ingest_note": has_ingest_note,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Judgment translation stress smoke (DC/TG/LINE)")
    ap.add_argument("--judgment-dir", default=str(JUDGMENT_DIR_DEFAULT))
    ap.add_argument("--json-out", default=f"/tmp/magi_judgment_smoke_{_now_ts()}.json")
    args = ap.parse_args()

    folder = Path(args.judgment_dir).expanduser()
    files = _pick_three_pdfs(folder)
    if len(files) < 3:
        print(json.dumps({"success": False, "error": f"need >=3 pdf files in {folder}", "found": [str(p) for p in files]}, ensure_ascii=False))
        return 2

    os.chdir(str(ROOT))
    from api.orchestrator import Orchestrator
    from skills.bridge import melchior_client

    original_distributed = melchior_client.distributed_chat
    original_quick = melchior_client.quick_local_chat
    original_summary = Orchestrator._summarize_text_resilient

    channels = [
        {"platform": "Discord", "user_id": "discord_13370001", "file": files[0]},
        {"platform": "Telegram", "user_id": "telegram_13370001", "file": files[1]},
        {"platform": "LINE", "user_id": "U" + "1" * 24, "file": files[2]},
    ]
    prompts = [
        "請翻譯這份檔案並給我TXT",
        "請翻譯這份檔案並摘要，給我TXT",
    ]

    report = {
        "generated_at": _now_ts(),
        "folder": str(folder),
        "files": [str(p) for p in files],
        "scenarios": [],
    }

    try:
        for scenario in ["normal", "degraded"]:
            counters, fake_dist, fake_quick = _make_fake_route_functions(scenario)
            melchior_client.distributed_chat = fake_dist  # type: ignore[assignment]
            melchior_client.quick_local_chat = fake_quick  # type: ignore[assignment]

            def _fake_summary(self, text: str, *args, **kwargs) -> dict:
                return {"success": True, "text": "- 重點一\n- 重點二\n- 重點三", "provider": f"fake-summary-{scenario}"}

            Orchestrator._summarize_text_resilient = _fake_summary  # type: ignore[assignment]

            orc = Orchestrator()
            scenario_rows = []
            ingested = set()
            for ch in channels:
                for prompt in prompts:
                    f = ch["file"]
                    enable_ingest = f not in ingested
                    row = _run_case(
                        orc,
                        file_path=f,
                        user_id=ch["user_id"],
                        platform=ch["platform"],
                        prompt=prompt,
                        enable_ingest=enable_ingest,
                    )
                    scenario_rows.append(row)
                    if row.get("has_ingest_note"):
                        ingested.add(f)

            pass_count = sum(1 for r in scenario_rows if r.get("ok"))
            fail_count = len(scenario_rows) - pass_count
            report["scenarios"].append(
                {
                    "name": scenario,
                    "counts": {"pass": pass_count, "fail": fail_count},
                    "route_counters": counters,
                    "rows": scenario_rows,
                }
            )
    finally:
        melchior_client.distributed_chat = original_distributed  # type: ignore[assignment]
        melchior_client.quick_local_chat = original_quick  # type: ignore[assignment]
        Orchestrator._summarize_text_resilient = original_summary  # type: ignore[assignment]

    overall_fail = 0
    for sc in report["scenarios"]:
        overall_fail += int(sc.get("counts", {}).get("fail", 0))

    out_path = Path(args.json_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== Judgment Translation 3-Channel Smoke ===")
    for sc in report["scenarios"]:
        c = sc["counts"]
        rt = sc["route_counters"]
        print(
            f"- {sc['name']}: PASS={c['pass']} FAIL={c['fail']} "
            f"(distributed_calls={rt.get('distributed_calls',0)} quick_calls={rt.get('quick_calls',0)})"
        )
    print(f"JSON report: {out_path}")

    return 0 if overall_fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
