from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.v3_validation.change_scope import (
    FULL_SCOPE,
    SCOPED_SCOPE,
    build_receipt,
    classify_paths,
)


def test_docs_css_and_tests_are_scoped_only_for_development() -> None:
    decision = classify_paths(["docs/guide.md", "templates/theme.css", "tests/test_theme.py"])
    assert decision.development_scope == SCOPED_SCOPE
    assert decision.promotion_scope == FULL_SCOPE


def test_operational_boundaries_force_full_even_when_mixed_with_docs() -> None:
    decision = classify_paths(["docs/guide.md", "api/blueprints/cookie_cutter.py"])
    assert decision.development_scope == FULL_SCOPE
    assert "operational-boundary" in decision.reasons


def test_operational_prefix_cannot_be_downgraded_by_css_suffix() -> None:
    for path in ("api/render.css", "skills/formatting.css"):
        decision = classify_paths([path])
        assert decision.development_scope == FULL_SCOPE
        assert "operational-boundary" in decision.reasons


def test_keyword_boundaries_force_full() -> None:
    decision = classify_paths(["assets/calendar-migration.svg", "assets/runtime-diagram.svg"])
    assert decision.development_scope == FULL_SCOPE
    assert "operational-keyword" in decision.reasons


def test_only_explicit_pure_source_is_scoped(tmp_path: Path) -> None:
    pure = tmp_path / "lib" / "formatting.py"
    pure.parent.mkdir()
    pure.write_text("# magi-validation-scope: pure-function\n", encoding="utf-8")
    decision = classify_paths(["lib/formatting.py"], root=tmp_path)
    assert decision.development_scope == SCOPED_SCOPE
    assert decision.promotion_scope == FULL_SCOPE


def test_marker_in_allowed_magi_pure_directory_is_scoped(tmp_path: Path) -> None:
    pure = tmp_path / "magi_v3" / "pure" / "formatting.py"
    pure.parent.mkdir(parents=True)
    pure.write_text("# magi-validation-scope: pure-function\n", encoding="utf-8")
    assert classify_paths(["magi_v3/pure/formatting.py"], root=tmp_path).development_scope == SCOPED_SCOPE


def test_marker_cannot_downgrade_operational_file(tmp_path: Path) -> None:
    operational = tmp_path / "api" / "route_formatting.py"
    operational.parent.mkdir()
    operational.write_text("# magi-validation-scope: pure-function\n", encoding="utf-8")
    decision = classify_paths(["api/route_formatting.py"], root=tmp_path)
    assert decision.development_scope == FULL_SCOPE
    assert "operational-keyword" in decision.reasons


def test_unknown_source_and_empty_diff_fail_closed() -> None:
    assert classify_paths(["lib/new_algorithm.py"]).development_scope == FULL_SCOPE
    assert classify_paths([]).development_scope == FULL_SCOPE


def test_standalone_normaliser_rejects_absolute_and_traversal_paths() -> None:
    posix_absolute = "/" + "tmp/outside.py"
    windows_absolute = "C:" + "/outside.py"
    for unsafe in ("../outside.py", "./docs/readme.md", posix_absolute, windows_absolute):
        with pytest.raises(ValueError):
            classify_paths([unsafe])


def test_receipt_cannot_downgrade_promotion() -> None:
    receipt = build_receipt(["docs/guide.md"])
    assert receipt["schema"] == "magi.v3.validation-scope/v1"
    assert receipt["development_scope"] == SCOPED_SCOPE
    assert receipt["promotion_scope"] == FULL_SCOPE
    assert receipt["promotion_requires_full_release_quality"] is True
    assert json.loads(json.dumps(receipt))["promotion_scope"] == FULL_SCOPE
