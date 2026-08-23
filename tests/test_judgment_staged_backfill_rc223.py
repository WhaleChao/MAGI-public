from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api.domains.judgment_summary_quality import (
    _normalize_caption_issue,
    build_extractive_practice_summary,
    evaluate_practice_summary,
    rank_practice_candidates,
)
from scripts.ops.judgment_summary_staged_backfill import _due_queue_ids
from scripts.ops import judgment_summary_staged_backfill as staged


def test_caption_issue_canonicalization_does_not_store_party_names() -> None:
    assert _normalize_caption_issue("周○○死亡宣告") == "死亡宣告"
    assert (
        _normalize_caption_issue(
            "變更財團法人某股份有限公司職工福利委員會捐助章程"
        )
        == "變更捐助章程"
    )
    assert _normalize_caption_issue("為相對人某有限公司選任臨時管理人") == "選任臨時管理人"


def test_procedural_issue_requires_the_procedural_rule_not_underlying_offence() -> None:
    source = """理由
    二、核被告所為，係犯毒品危害防制條例第10條第2項之施用第二級毒品罪。
    本院衡酌被告犯後態度，依刑法第47條第1項加重其刑，並認未違反罪刑相當原則。
    """
    summary = build_extractive_practice_summary(source, "定應執行刑（毒品危害防制條例）")
    assert summary == ""


def test_sentence_aggregation_synonyms_restore_source_bound_rule() -> None:
    source = """理由
    按數罪併罰，分別宣告多數有期徒刑者，應依刑法第51條第5款定其應執行之刑，並審酌各罪關係與整體非難性，避免過度評價。
    本院審酌各罪侵害法益、行為時間及罪刑相當原則，定其應執行之刑如主文。
    """
    summary = build_extractive_practice_summary(source, "定應執行刑（詐欺）")
    quality = evaluate_practice_summary(summary, source, "定應執行刑（詐欺）")
    assert quality.ok is True
    assert quality.score >= 70
    assert "刑法第51條" in summary
    assert "定其應執行之刑" in summary


def test_relevant_statute_without_ocr_paragraph_lead_is_still_candidate() -> None:
    source = """理由
    行為人吐氣所含酒精濃度達每公升零點二五毫克以上而駕駛動力交通工具，依刑法第185條之3規定，不問是否肇事，均屬不能安全駕駛。
    """
    candidates = rank_practice_candidates(source, "公共危險")
    assert any(value.kind == "rule" for value in candidates)
    assert any("刑法第185條之3" in value.text for value in candidates)


def test_nvidia_queue_backoff_is_durable_and_bounded() -> None:
    now = datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc)
    rows = {
        "1": {"id": 1, "queued_at": "2026-08-01T00:00:00+00:00"},
        "2": {
            "id": 2,
            "queued_at": "2026-08-01T00:01:00+00:00",
            "next_retry_at": (now + timedelta(hours=1)).isoformat(),
        },
        "3": {"id": 3, "queued_at": "2026-08-01T00:02:00+00:00"},
    }
    assert _due_queue_ids(rows, limit=2, now=now) == [1, 3]


def test_provider_capacity_reports_durable_daily_budget_not_scheduler_theory(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
    budget_path = tmp_path / "budget.json"
    budget_path.write_text('{"day":"2026-08-22","used":9}', encoding="utf-8")
    monkeypatch.setattr(staged, "NVIDIA_BUDGET_PATH", budget_path)
    monkeypatch.setenv("MAGI_NVIDIA_RESUMMARY_DAILY_BUDGET", "24")
    assert staged._nvidia_budget_snapshot(now=now) == {
        "ceiling": 24,
        "used": 9,
        "remaining": 15,
    }
    assert staged._nvidia_budget_remaining(requested=32, now=now) == 15
