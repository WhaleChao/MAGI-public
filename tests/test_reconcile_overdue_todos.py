from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "ops" / "reconcile_overdue_todos.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("reconcile_overdue_todos_test", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("event", ["開庭", "言詞辯論", "準備程序", "宣判"])
def test_calendar_past_proceeding_is_archived_not_escalated(event):
    module = _load_module()

    action, reason = module.classify_todo(
        {"todo_type": "確認", "description": f"{event} 行程", "source_file": "gcal_import:opaque"}
    )

    assert (action, reason) == ("archive", "past_calendar_occurrence")


@pytest.mark.parametrize("todo_type", ["辯論", "宣判", "庭期"])
def test_past_proceeding_type_is_completed_even_without_calendar_source(todo_type):
    module = _load_module()

    assert module.classify_todo({"todo_type": todo_type, "description": "程序紀錄"}) == (
        "complete",
        "past_occurrence",
    )


@pytest.mark.parametrize("duty", ["補正", "提出", "繳費", "回報"])
def test_actionable_duty_is_not_hidden_by_calendar_hearing_wording(duty):
    module = _load_module()

    action, reason = module.classify_todo(
        {"todo_type": duty, "description": f"開庭前須{duty}", "source_file": "gcal_import:opaque"}
    )

    assert (action, reason) == ("escalate", "no_verifiable_completion_evidence")


def test_existing_overdue_confirmation_for_past_proceeding_is_reconciled():
    module = _load_module()

    action, reason = module.classify_todo(
        {
            "todo_type": "逾期確認",
            "description": "原類型：辯論\n尚無可驗證的完成證據",
            "source_file": "gcal_import:opaque",
        }
    )

    assert (action, reason) == ("archive", "overdue_confirmation_for_past_calendar_occurrence")


def test_existing_document_overdue_confirmation_recovers_original_proceeding_type():
    module = _load_module()

    action, reason = module.classify_todo(
        {
            "todo_type": "逾期確認",
            "description": "原期限：2026-02-12／原類型：辯論\n⚖️ 下午2時50分辯論",
            "source_file": "20260115 法院通知書.pdf",
        }
    )

    assert (action, reason) == ("complete", "overdue_confirmation_for_past_occurrence")


def test_real_pure_occurrence_with_google_event_keeps_calendar_history():
    module = _load_module()

    assert module._terminal_status(
        {
            "todo_type": "開庭",
            "description": "下午二時開庭",
            "source_file": "gcal_import:opaque",
            "google_calendar_id": "opaque-event-id",
        },
        action="complete",
        reason="overdue_confirmation_for_past_occurrence",
    ) == "completed"


def test_archived_calendar_occurrence_keeps_google_event_as_history():
    module = _load_module()

    assert module._terminal_status(
        {
            "todo_type": "準備程序",
            "description": "準備程序",
            "source_file": "gcal_import:opaque",
            "google_calendar_id": "opaque-event-id",
        },
        action="archive",
        reason="past_calendar_occurrence",
    ) == "calendar_deduped"


def test_synthetic_overdue_confirmation_still_uses_delete_handoff_status():
    module = _load_module()

    assert module._terminal_status(
        {
            "todo_type": "逾期確認",
            "description": "【MAGI逾期治理：原待辦#42】\n原類型：開庭",
            "google_calendar_id": "opaque-event-id",
        },
        action="complete",
        reason="overdue_confirmation_for_past_occurrence",
    ) == "deleted"


def test_pure_occurrence_without_google_event_completes_locally():
    module = _load_module()

    assert module._terminal_status(
        {"google_calendar_id": ""},
        action="complete",
        reason="overdue_confirmation_for_past_occurrence",
    ) == "completed"


def test_existing_overdue_confirmation_with_payment_obligation_stays_pending():
    module = _load_module()

    assert module.classify_todo(
        {
            "todo_type": "逾期確認",
            "description": "原類型：繳費\n開庭前須繳費",
            "source_file": "gcal_import:opaque",
        }
    ) == ("normalize", "legacy_overdue_confirmation_label")


def test_legacy_overdue_confirmation_recovers_actionable_label_and_plain_language():
    module = _load_module()
    row = {
        "todo_type": "逾期確認",
        "description": (
            "【MAGI逾期治理：原待辦#42】\n"
            "原期限：2026-06-15／原類型：提出資料\n"
            "尚無可驗證的完成證據，請確認是否已辦理；確認後即可結束本待辦。"
        ),
    }

    assert module._overdue_action_label(row) == "提出資料"
    description = module._actionable_overdue_description(row)
    assert "請立即處理" in description
    assert "請確認是否已辦理" not in description


def test_new_actionable_escalation_is_not_renormalized_or_duplicated():
    module = _load_module()

    assert module.classify_todo(
        {
            "todo_type": "抗告",
            "description": "【MAGI逾期治理：原待辦#42】\n原類型：抗告",
        }
    ) == ("skip", "already_escalated_actionable")
