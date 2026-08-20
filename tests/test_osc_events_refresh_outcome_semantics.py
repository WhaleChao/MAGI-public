from scripts.ops.osc_events_refresh import _apply_outcome_semantics


def test_quality_warning_is_partial_but_successful() -> None:
    result = {
        "ok": True,
        "warnings": ["calendar_import_only_without_pdf_source"],
    }

    assert _apply_outcome_semantics(result) == {
        "ok": True,
        "warnings": ["calendar_import_only_without_pdf_source"],
        "status": "partial",
        "success": True,
    }


def test_component_failure_remains_terminal_failure() -> None:
    result = {
        "ok": False,
        "warnings": ["calendar_push_failed"],
    }

    assert _apply_outcome_semantics(result) == {
        "ok": False,
        "warnings": ["calendar_push_failed"],
        "status": "failed",
        "success": False,
    }


def test_warning_free_refresh_is_completed() -> None:
    result = {"ok": True, "warnings": []}

    assert _apply_outcome_semantics(result) == {
        "ok": True,
        "warnings": [],
        "status": "completed",
        "success": True,
    }
