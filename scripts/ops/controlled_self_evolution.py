#!/usr/bin/env python3
"""CLI for MAGI's candidate-only controlled self-evolution loop."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from magi_v3.controlled_evolution import (  # noqa: E402
    EvolutionStore,
    build_structure_inventory,
    ingest_signals,
    stage_candidate,
    verify_candidate,
)


def _runtime_dir() -> Path:
    configured = os.environ.get("MAGI_RUNTIME_DIR", "").strip()
    return Path(configured).expanduser().resolve() if configured else (ROOT / ".runtime").resolve()


def _store(runtime_dir: Path) -> EvolutionStore:
    return EvolutionStore(runtime_dir / "controlled-evolution" / "evolution.sqlite3")


def _load_json(path: Path) -> Any:
    return json.loads(path.expanduser().read_text(encoding="utf-8"))


def _write_json(path: Path | None, payload: Any) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path is None:
        print(text, end="")
        return
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + f".tmp-{os.getpid()}")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, target)


def _release_id(root: Path) -> str:
    return os.environ.get("MAGI_RELEASE_ID", "").strip() or root.name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MAGI controlled self-evolution (candidate-only; no deploy operation).")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--runtime-dir", default="")
    sub = parser.add_subparsers(dest="command", required=True)

    inventory = sub.add_parser("inventory")
    inventory.add_argument("--json-out", default="")

    analyze = sub.add_parser("analyze")
    analyze.add_argument("--report", required=True, help="Guardian/quality JSON report")
    analyze.add_argument("--json-out", default="")

    listing = sub.add_parser("list")
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--json-out", default="")

    stage = sub.add_parser("stage")
    stage.add_argument("--proposal-id", required=True)
    stage.add_argument("--patch-file", required=True)
    stage.add_argument("--source-root", required=True)
    stage.add_argument("--workspace-root", required=True)
    stage.add_argument("--commit", default="HEAD")
    stage.add_argument("--json-out", default="")

    verify = sub.add_parser("verify")
    verify.add_argument("--proposal-id", required=True)
    verify.add_argument("--candidate", required=True)
    verify.add_argument("--python", default=sys.executable)
    verify.add_argument("--timeout", type=int, default=900)
    verify.add_argument("--json-out", default="")

    args = parser.parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    runtime_dir = Path(args.runtime_dir).expanduser().resolve() if args.runtime_dir else _runtime_dir()
    store = _store(runtime_dir)

    if args.command == "inventory":
        result = build_structure_inventory(root)
    elif args.command == "analyze":
        report = _load_json(Path(args.report))
        signals = report.get("issues") if isinstance(report, dict) else None
        if not isinstance(signals, list):
            signals = report.get("signals") if isinstance(report, dict) else None
        if not isinstance(signals, list):
            raise SystemExit("report must contain issues[] or signals[]")
        proposals = ingest_signals(signals, root=root, release_id=_release_id(root), store=store)
        result = {
            "ok": True,
            "status": "planned" if proposals else "no_open_evolution_signal",
            "proposal_count": len(proposals),
            "proposals": proposals,
            "auto_deploy": False,
        }
    elif args.command == "list":
        proposals = store.list(limit=args.limit)
        result = {"ok": True, "proposal_count": len(proposals), "proposals": proposals}
    elif args.command == "stage":
        proposal = store.get(args.proposal_id)
        result = stage_candidate(
            proposal=proposal,
            store=store,
            source_root=Path(args.source_root),
            workspace_root=Path(args.workspace_root),
            patch_text=Path(args.patch_file).expanduser().read_text(encoding="utf-8"),
            commit=args.commit,
        )
    else:
        proposal = store.get(args.proposal_id)
        result = verify_candidate(
            proposal=proposal,
            store=store,
            candidate=Path(args.candidate),
            python=args.python,
            timeout=args.timeout,
        )

    out = Path(args.json_out) if args.json_out else None
    _write_json(out, result)
    return 0 if result.get("ok") or result.get("status") in {"deferred", "no_open_evolution_signal"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
