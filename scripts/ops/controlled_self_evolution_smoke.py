#!/usr/bin/env python3
"""Disposable host smoke for the controlled self-evolution isolation boundary."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from magi_v3.controlled_evolution import (  # noqa: E402
    EvolutionStore,
    build_proposal,
    stage_candidate,
    verify_candidate,
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="magi_rc557_evolution_smoke_"))
    try:
        source = base / "source"
        (source / "api" / "routing").mkdir(parents=True)
        (source / "tests").mkdir()
        for package in (source / "api" / "__init__.py", source / "api" / "routing" / "__init__.py"):
            package.write_text("", encoding="utf-8")
        target = source / "api" / "routing" / "sample.py"
        target.write_text("VALUE = 1\n", encoding="utf-8")
        (source / "tests" / "test_candidate.py").write_text(
            "from api.routing.sample import VALUE\n\ndef test_value():\n    assert VALUE == 2\n",
            encoding="utf-8",
        )
        commands = (
            ("init",),
            ("config", "user.email", "magi@example.invalid"),
            ("config", "user.name", "MAGI Smoke"),
            ("add", "."),
            ("commit", "-m", "fixture"),
        )
        if any(_git(source, *command).returncode != 0 for command in commands):
            print(json.dumps({"ok": False, "reason": "fixture_git_failed", "debug_root": str(base)}))
            return 1
        target.write_text("VALUE = 2\n", encoding="utf-8")
        patch = _git(source, "diff", "--binary", "--no-ext-diff").stdout
        _git(source, "checkout", "--", "api/routing/sample.py")
        store = EvolutionStore(base / "runtime" / "evolution.sqlite3")
        proposal = build_proposal(
            {
                "id": "smoke:routing",
                "source": "controlled_evolution_smoke",
                "category": "routing_quality",
                "severity": "error",
                "status": "needs_repair",
                "summary": "intent routing regression",
            },
            release_id="smoke",
            root=source,
        )
        proposal["structure_scope"] = {
            "source_prefixes": ["api/routing"],
            "acceptance_tests": ["tests/test_candidate.py"],
        }
        proposal = store.upsert(proposal)
        staged = stage_candidate(
            proposal=proposal,
            store=store,
            source_root=source,
            workspace_root=base / "candidates",
            patch_text=patch,
        )
        if not staged.get("ok"):
            staged["debug_root"] = str(base)
            print(json.dumps(staged, ensure_ascii=False))
            return 1
        verified = verify_candidate(
            proposal=store.get(proposal["proposal_id"]),
            store=store,
            candidate=Path(str(staged["candidate"])),
            timeout=120,
        )
        if not verified.get("ok"):
            verified["debug_root"] = str(base)
        print(json.dumps(verified, ensure_ascii=False, sort_keys=True))
        if verified.get("ok"):
            shutil.rmtree(base, ignore_errors=True)
            return 0
        return 1
    except Exception:
        print(json.dumps({"ok": False, "reason": "smoke_exception", "debug_root": str(base)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
