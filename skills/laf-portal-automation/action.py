#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LAF portal training router.

Usage:
  python action.py --query "幫我做開案回報，當事人是蕭仁俊（只填寫不送出）"
  python action.py --list
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple


SKILL_DIR = Path(__file__).resolve().parent
TRAINING_PATH = SKILL_DIR / "references" / "snapshot_training.json"


def _load() -> Dict:
    if not TRAINING_PATH.exists():
        return {"entries": [], "sample_count": 0}
    try:
        return json.loads(TRAINING_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"entries": [], "sample_count": 0}


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", "", s)
    return s


def _score(query: str, entry: Dict) -> Tuple[int, List[str]]:
    q = _norm(query)
    score = 0
    hits: List[str] = []

    if not q:
        return score, hits

    for key in [entry.get("person", ""), entry.get("label", ""), entry.get("workflow", "")]:
        k = _norm(str(key))
        if k and k in q:
            score += 20
            hits.append(str(key))

    for cmd in entry.get("command_examples") or []:
        c = _norm(str(cmd))
        if c and c in q:
            score += 50
            hits.append(str(cmd))

    tokens = [entry.get("person", ""), entry.get("label", ""), entry.get("workflow", "")]
    for t in tokens:
        tv = _norm(str(t))
        if not tv:
            continue
        overlap = sum(1 for ch in set(tv) if ch in q)
        score += overlap

    return score, hits


def resolve(query: str, top_k: int = 3) -> List[Dict]:
    data = _load()
    rows = []
    for e in data.get("entries") or []:
        score, hits = _score(query, e)
        if score <= 0:
            continue
        rows.append(
            {
                "score": score,
                "hits": hits[:8],
                "workflow": e.get("workflow"),
                "label": e.get("label"),
                "person": e.get("person"),
                "run": e.get("run"),
                "sample_id": e.get("sample_id"),
                "expected_step_notes": e.get("expected_step_notes") or [],
                "core_step_notes": e.get("core_step_notes") or [],
                "policy": e.get("policy") or {},
                "command_examples": (e.get("command_examples") or [])[:5],
            }
        )
    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows[: max(1, top_k)]


def list_entries() -> Dict:
    data = _load()
    entries = data.get("entries") or []
    brief = []
    for e in entries:
        brief.append(
            {
                "workflow": e.get("workflow"),
                "label": e.get("label"),
                "person": e.get("person"),
                "sample_id": e.get("sample_id"),
            }
        )
    return {
        "sample_count": len(brief),
        "workflows": sorted({str(x.get("workflow")) for x in brief if x.get("workflow")}),
        "entries": brief,
    }


def extract_open_case_date(case_folder: str) -> Dict:
    """
    掃描案件資料夾下「02_開辦資料」的 PDF，
    用視覺模組抽取法院收文章日期，供開辦表單使用。
    """
    try:
        import sys
        skill_dir = Path(__file__).resolve().parent
        if str(skill_dir) not in sys.path:
            sys.path.insert(0, str(skill_dir))
        from open_case_vision import extract_open_case_date as _extract
        result = _extract(case_folder)
        return {"ok": True, **result}
    except Exception as e:
        return {"ok": False, "error": str(e), "date": None, "date_str": None}


def execute_workflow(workflow: str, person: str, dry_run: bool = True) -> Dict:
    """
    Bridge: 將已匹配的 workflow 委派給 laf-orchestrator 實際執行。
    dry_run=True 僅填表不送出（預設安全策略）。
    """
    import subprocess, sys
    from pathlib import Path as _P
    orch_path = _P(__file__).resolve().parents[2] / "skills" / "laf-orchestrator" / "action.py"
    if not orch_path.exists():
        return {"ok": False, "error": f"laf-orchestrator not found: {orch_path}"}
    cmd = [sys.executable, str(orch_path), "--mode", workflow]
    if person:
        cmd += ["--client", person]
    if dry_run:
        cmd += ["--dry-run"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return {"ok": r.returncode == 0, "stdout": r.stdout[-2000:], "stderr": r.stderr[-500:], "rc": r.returncode}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main() -> int:
    parser = argparse.ArgumentParser(description="LAF portal training router")
    parser.add_argument("--task", default="", help="help=顯示說明")
    parser.add_argument("--query", default="", help="Natural language query")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--list", action="store_true", help="List loaded entries")
    parser.add_argument("--extract-date", default="", metavar="CASE_FOLDER",
                        help="從指定案件資料夾抽取開辦收文章日期")
    parser.add_argument("--execute", action="store_true",
                        help="匹配成功後直接呼叫 laf-orchestrator 執行（預設 dry-run）")
    parser.add_argument("--submit", action="store_true",
                        help="搭配 --execute 使用：解除 dry-run，允許實際送出（需人工確認）")
    args = parser.parse_args()

    if (args.task or "").strip().lower() == "help":
        print(json.dumps({"skill": "laf-portal-automation",
                          "tasks": ["--query", "--list", "--extract-date", "--execute", "--execute --submit"],
                          "description": "法扶 Portal 操作訓練路由、日期抽取、及委派 laf-orchestrator 執行"}, ensure_ascii=False, indent=2))
        return 0

    if args.extract_date:
        result = extract_open_case_date(args.extract_date)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.list:
        print(json.dumps(list_entries(), ensure_ascii=False, indent=2))
        return 0

    if not args.query.strip():
        print(json.dumps({"ok": False, "error": "query_required"}, ensure_ascii=False))
        return 2

    matches = resolve(args.query, top_k=args.top_k)
    result: Dict = {"ok": True, "query": args.query, "matches": matches}

    if args.execute and matches:
        top = matches[0]
        workflow = top.get("workflow") or ""
        person = top.get("person") or ""
        dry_run = not args.submit
        if not workflow:
            result["execute"] = {"ok": False, "error": "no workflow matched"}
        else:
            result["execute"] = execute_workflow(workflow, person, dry_run=dry_run)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
