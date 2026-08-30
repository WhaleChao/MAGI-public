#!/usr/bin/env python3
"""Refresh source-derived line anchors without guessing line offsets."""

from __future__ import annotations

import argparse
import ast
import copy
import json
from pathlib import Path
from typing import Any


class SourceAnchorError(ValueError):
    """A manifest anchor cannot be resolved to one exact source location."""


def _source_file(root: Path, relative_path: str) -> Path:
    root = root.resolve()
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise SourceAnchorError(f"source anchor is outside the repository or missing: {relative_path}")
    return path


def _function_lines(path: Path) -> dict[str, list[int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result.setdefault(node.name, []).append(node.lineno)
    return result


def refresh_route_review_sources(payload: dict[str, Any], root: Path) -> dict[str, Any]:
    """Bind every review row to the exact AST definition for its endpoint."""

    refreshed = copy.deepcopy(payload)
    cache: dict[str, dict[str, list[int]]] = {}
    rows = [*refreshed.get("reviews", []), *refreshed.get("unreviewed", [])]
    for row in rows:
        try:
            source_path, _old_line = str(row["v2_handler_source"]).rsplit(":", 1)
            endpoint_name = str(row["endpoint"]).rsplit(".", 1)[-1]
        except (KeyError, ValueError) as exc:
            raise SourceAnchorError("route review lacks endpoint/source identity") from exc
        if source_path not in cache:
            cache[source_path] = _function_lines(_source_file(root, source_path))
        matches = cache[source_path].get(endpoint_name, [])
        if len(matches) != 1:
            raise SourceAnchorError(
                f"expected one definition for {endpoint_name!r} in {source_path}, got {matches}"
            )
        row["v2_handler_source"] = f"{source_path}:{matches[0]}"
    return refreshed


def refresh_readiness_evidence(payload: dict[str, Any], root: Path) -> dict[str, Any]:
    """Relocate stale readiness anchors by exact content, failing on ambiguity."""

    refreshed = copy.deepcopy(payload)
    for surface in refreshed.get("surfaces", []):
        for evidence in surface.get("evidence", []):
            try:
                source_path = str(evidence["file"])
                anchor = str(evidence["anchor"])
                old_line = int(evidence["line"])
            except (KeyError, TypeError, ValueError) as exc:
                raise SourceAnchorError("readiness evidence lacks a valid file/line/anchor") from exc
            lines = _source_file(root, source_path).read_text(encoding="utf-8").splitlines()
            if 1 <= old_line <= len(lines) and anchor in lines[old_line - 1]:
                continue
            matches = [line for line, text in enumerate(lines, 1) if anchor in text]
            if len(matches) != 1:
                raise SourceAnchorError(
                    f"stale anchor {anchor!r} in {source_path} is not unique: {matches}"
                )
            evidence["line"] = matches[0]
    return refreshed


def _render(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _refresh_file(path: Path, refresh: Any, root: Path, *, write: bool) -> bool:
    original = path.read_text(encoding="utf-8")
    refreshed = _render(refresh(json.loads(original), root))
    current = original == refreshed
    if write and not current:
        path.write_text(refreshed, encoding="utf-8")
    return current


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument(
        "--route-review",
        type=Path,
        default=Path("scripts/v3_validation/route-method-review.json"),
    )
    parser.add_argument(
        "--readiness",
        type=Path,
        default=Path("config/v3_pre_cutover_readiness.json"),
    )
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    route_review = args.route_review if args.route_review.is_absolute() else root / args.route_review
    readiness = args.readiness if args.readiness.is_absolute() else root / args.readiness
    route_current = _refresh_file(route_review, refresh_route_review_sources, root, write=args.write)
    readiness_current = _refresh_file(readiness, refresh_readiness_evidence, root, write=args.write)
    if args.write:
        print("source anchors refreshed")
        return 0
    if route_current and readiness_current:
        print("source anchors are current")
        return 0
    print("source anchors are stale; rerun with --write")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
