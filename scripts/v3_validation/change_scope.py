"""Fail-closed validation scope selection for development changes.

This module is deliberately conservative.  It can make a development feedback
loop smaller, but it can never make a promotion smaller: promotion evidence is
always the complete release-quality suite.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


POLICY_VERSION = "magi.v3.validation-scope/v1"
FULL_SCOPE = "full"
SCOPED_SCOPE = "scoped"

# These are operational boundaries, not a list of currently-known files.  An
# unknown source path therefore fails closed rather than silently acquiring a
# scoped-only test plan.
FULL_PREFIXES = (
    "api/",
    "magi_v3/",
    "config/",
    "scripts/",
    "skills/",
    "deploy/",
    "ops/",
    "requirements",
    "pyproject.toml",
    "package",
    "Caddyfile",
    "Dockerfile",
)
FULL_TERMS = (
    "route", "auth", "csrf", "cron", "database", "db_", "mysql", "sqlite",
    "nas", "drive", "google", "calendar", "notification", "runtime", "deploy",
    "rollback", "dependency", "resource", "policy", "cross_module", "cutover",
)
SCOPED_PREFIXES = ("docs/", "tests/")
SCOPED_SUFFIXES = (".md", ".txt", ".rst", ".css")
PURE_MARKER = "magi-validation-scope: pure-function"
# A marker is a narrow opt-in for implementation-only code.  In particular it
# does not provide an escape hatch for API handlers, jobs, or generic skills.
PURE_ALLOWED_PREFIXES = ("lib/", "utils/", "magi_v3/pure/")


@dataclass(frozen=True)
class ScopeDecision:
    development_scope: str
    promotion_scope: str
    reasons: tuple[str, ...]
    changed_files: tuple[str, ...]


def _normalise(path: str) -> str:
    """Validate a repository-relative path; never repair unsafe input."""

    raw = str(path)
    value = raw.replace("\\", "/")
    if (
        not value
        or "\x00" in value
        or value.startswith("/")
        or re.fullmatch(r"[A-Za-z]:/.*", value)
    ):
        raise ValueError(f"unsafe validation path: {raw!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe validation path traversal: {raw!r}")
    return "/".join(parts)


def _bound_source(root: Path, relative: str) -> Path:
    candidate = (root.resolve() / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"validation source escapes workspace: {relative}") from exc
    return candidate


def _is_explicit_pure(path: Path) -> bool:
    try:
        return PURE_MARKER in path.read_text(encoding="utf-8", errors="ignore")[:2048]
    except OSError:
        return False


def _development_reason(path: str, root: Path | None) -> str | None:
    lowered = path.lower()
    if not path or path.startswith("../") or "/../" in path:
        return "unsafe-or-empty-path"
    # An operational boundary wins over a filename suffix.  In particular an
    # API or skill implementation named ``*.css`` is still implementation
    # code, not a safe style-only edit.
    if any(term in lowered for term in FULL_TERMS):
        return "operational-keyword"
    if (
        root is not None
        and any(path.startswith(prefix) for prefix in PURE_ALLOWED_PREFIXES)
        and _is_explicit_pure(_bound_source(root, path))
    ):
        return None
    if any(path.startswith(prefix) for prefix in FULL_PREFIXES):
        return "operational-boundary"
    # Documentation, style sheets and test-only changes are explicitly safe
    # development scopes even when their prose mentions an operational word.
    if any(path.startswith(prefix) for prefix in SCOPED_PREFIXES):
        return None
    if path.endswith(SCOPED_SUFFIXES):
        return None
    return "unknown-or-non-pure-source"


def classify_paths(paths: Iterable[str], *, root: Path | None = None) -> ScopeDecision:
    changed = tuple(sorted({_normalise(str(path)) for path in paths}))
    if not changed:
        reasons = ("no-changed-files-fails-closed",)
        return ScopeDecision(FULL_SCOPE, FULL_SCOPE, reasons, changed)
    blocked = sorted({reason for path in changed if (reason := _development_reason(path, root))})
    development = FULL_SCOPE if blocked else SCOPED_SCOPE
    reasons = tuple(blocked) if blocked else ("only-explicit-safe-content",)
    # This invariant is intentional and must be represented in every receipt.
    return ScopeDecision(development, FULL_SCOPE, reasons, changed)


def build_receipt(paths: Iterable[str], *, root: Path | None = None, base: str | None = None,
                  head: str | None = None) -> dict[str, object]:
    decision = classify_paths(paths, root=root)
    return {
        "schema": POLICY_VERSION,
        "base": base,
        "head": head,
        "changed_files": list(decision.changed_files),
        "development_scope": decision.development_scope,
        "promotion_scope": decision.promotion_scope,
        "promotion_requires_full_release_quality": True,
        "reasons": list(decision.reasons),
    }


def changed_paths(base: str, head: str, *, root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", base, head],
        cwd=root, text=True, capture_output=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser(description="Create fail-closed validation scope receipt")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    receipt = build_receipt(changed_paths(args.base, args.head, root=root), root=root,
                            base=args.base, head=args.head)
    args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
