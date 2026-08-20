from __future__ import annotations

import json
from pathlib import Path
import sys

from api.domains import judgment_nvidia_summary as selector
from api.domains.judgment_summary_quality import evaluate_practice_summary
from scripts.seed_cron_jobs import operational_jobs


SOURCE = """臺灣某法院民事判決
案由：侵權行為損害賠償
主文
原告之訴駁回。
理由

按民法第184條第1項前段規定，因故意或過失不法侵害他人之權利者，負損害賠償責任；侵權行為損害賠償請求權之成立，應由請求權人就故意或過失、權利受侵害及相當因果關係等構成要件負舉證責任。

本院認為，系爭行為與所主張損害間欠缺相當因果關係，且提出之資料尚不足證明權利確受侵害，故不能認定損害賠償責任成立。

中華民國115年7月31日
"""


def _mock_provider(response: str, *, success: bool = True, error: str = ""):
    def _run_nim_chat(**kwargs):
        assert kwargs["heavy"] is True
        assert kwargs["task_type"] == "judgment_summary"
        assert kwargs["require_pii_scrub"] is True
        return {
            "success": success,
            "response": response,
            "model": "nvidia/nemotron-3-super-120b-a12b",
            "error": error,
            "pii_scrubbed": True,
            "pii_counts": {"person": 2},
            "duration_ms": 17,
        }

    return _run_nim_chat


def test_nvidia_selects_ids_but_stored_text_is_exact_source(monkeypatch) -> None:
    from skills.bridge import nim_heavy

    monkeypatch.setattr(
        nim_heavy,
        "run_nim_chat",
        _mock_provider(
            '{"usable":true,"rule_ids":["R01"],'
            '"application_ids":["A01"],"confidence":0.93,'
            '"reason_code":"selected","invented_quote":"模型竄改的文字"}'
        ),
    )
    result = selector.summarize_with_nvidia(SOURCE, "侵權行為損害賠償")
    assert result.success is True
    assert "模型竄改的文字" not in result.summary
    assert "侵權行為損害賠償請求權之成立" in result.summary
    assert "欠缺相當因果關係" in result.summary
    quality = evaluate_practice_summary(
        result.summary,
        SOURCE,
        "侵權行為損害賠償",
    )
    assert quality.ok is True
    assert quality.source_supported_spans >= 1
    assert result.pii_scrubbed is True
    assert result.pii_counts == {"person": 2}


def test_selector_contract_requires_application_for_bare_statute_rule() -> None:
    from api.domains.judgment_nvidia_summary import _selection_prompt

    prompt = _selection_prompt(
        "損害賠償",
        [
            {"id": "R01", "kind": "rule", "score": 36, "text": "民法第184條規定。"},
            {"id": "A01", "kind": "application", "score": 24, "text": "本院認為原告未舉證。"},
        ],
    )
    assert "必須選 1 個與議題最相符的 A 編號" in prompt
    assert "只有所選 R 本身已有完整涵攝" in prompt
    assert "中文合計未滿 40 字" in prompt


def test_bare_statute_selection_recovers_source_bound_application(monkeypatch) -> None:
    from skills.bridge import nim_heavy

    source = (
        "理由\n"
        "按票據法第123條規定，執票人向本票發票人行使追索權時，得聲請法院裁定後強制執行，"
        "法院並應依聲請內容審查票據之形式。\n"
        "本院認為，系爭本票具備應記載事項，執票人得依票據法規定行使追索權並聲請強制執行。\n"
    )
    monkeypatch.setattr(
        nim_heavy,
        "run_nim_chat",
        _mock_provider(
            '{"usable":true,"rule_ids":["R01"],'
            '"application_ids":[],"confidence":0.93,'
            '"reason_code":"selected"}'
        ),
    )
    result = selector.summarize_with_nvidia(source, "本票")
    assert result.success is True
    assert result.selected_application_ids == ("A01",)
    assert "具備應記載事項" in result.summary
    assert evaluate_practice_summary(
        result.summary,
        source,
        "本票",
    ).ok is True


def test_bare_statute_selection_without_application_is_terminal_no_insight(monkeypatch) -> None:
    from skills.bridge import nim_heavy

    source = (
        "理由\n"
        "按票據法第123條規定，執票人向本票發票人行使追索權時，得聲請法院裁定後強制執行，"
        "法院並應依聲請內容審查票據之形式。\n"
    )
    def forbidden_provider(**_kwargs):
        raise AssertionError("doomed statute-only candidate must not leave the host")

    monkeypatch.setattr(nim_heavy, "run_nim_chat", forbidden_provider)
    result = selector.summarize_with_nvidia(source, "本票")
    assert result.success is False
    assert result.summary == ""
    assert result.selected_application_ids == ()
    assert result.reviewed_no_insight is True
    assert result.error == "reviewed:no_source_application"
    assert result.response_sha256 == ""
    assert result.duration_ms == 0


def test_doctrinal_rule_without_application_still_reaches_selector(monkeypatch) -> None:
    from skills.bridge import nim_heavy

    source = (
        "理由\n"
        "最高法院統一見解指出，所謂權利濫用，係指權利人行使權利雖無違反形式規定，"
        "但其目的、方法及所造成之利益失衡已逾越誠信原則容許範圍者，仍應禁止。\n"
    )
    called = {"value": False}

    def provider(**_kwargs):
        called["value"] = True
        return _mock_provider(
            '{"usable":true,"rule_ids":["R01"],'
            '"application_ids":[],"confidence":0.93,'
            '"reason_code":"selected"}'
        )(**_kwargs)

    monkeypatch.setattr(nim_heavy, "run_nim_chat", provider)
    result = selector.summarize_with_nvidia(source, "權利濫用")
    assert called["value"] is True
    assert result.success is True


def test_nvidia_candidates_exclude_rules_unrelated_to_case_issue() -> None:
    source = """\
理由
按民事訴訟法第249條規定，起訴不合程式者，法院應以裁定駁回之。
按洗錢防制法規定，掩飾或隱匿特定犯罪所得之來源、去向，始構成洗錢行為。
本院認為，被告收受款項後層轉他人帳戶，足認有掩飾金流去向之行為。
"""
    records, _lookup = selector._candidate_records(
        source,
        "洗錢防制法",
        max_candidates=14,
    )
    rule_texts = [
        str(record.get("text") or "")
        for record in records
        if record.get("kind") == "rule"
    ]
    assert any("洗錢防制法" in text for text in rule_texts)
    assert all("民事訴訟法第249條" not in text for text in rule_texts)


def test_unknown_provider_id_is_rejected_without_summary(monkeypatch) -> None:
    from skills.bridge import nim_heavy

    monkeypatch.setattr(
        nim_heavy,
        "run_nim_chat",
        _mock_provider(
            '{"usable":true,"rule_ids":["R99"],'
            '"application_ids":[],"confidence":1.0,"reason_code":"selected"}'
        ),
    )
    result = selector.summarize_with_nvidia(SOURCE, "侵權行為損害賠償")
    assert result.success is False
    assert result.summary == ""
    assert result.error == "selector_unknown_id"
    assert result.reviewed_no_insight is False


def test_provider_failure_is_fail_closed(monkeypatch) -> None:
    from skills.bridge import nim_heavy

    monkeypatch.setattr(
        nim_heavy,
        "run_nim_chat",
        _mock_provider("", success=False, error="nim_http_timeout"),
    )
    result = selector.summarize_with_nvidia(SOURCE, "侵權行為損害賠償")
    assert result.success is False
    assert result.summary == ""
    assert result.error == "provider:nim_http_timeout"
    assert result.reviewed_no_insight is False


def test_explicit_no_insight_is_terminal_review_not_garbage(monkeypatch) -> None:
    from skills.bridge import nim_heavy

    monkeypatch.setattr(
        nim_heavy,
        "run_nim_chat",
        _mock_provider(
            '{"usable":false,"rule_ids":[],"application_ids":[],'
            '"confidence":0.88,"reason_code":"fact_only"}'
        ),
    )
    result = selector.summarize_with_nvidia(SOURCE, "侵權行為損害賠償")
    assert result.success is False
    assert result.summary == ""
    assert result.reviewed_no_insight is True
    assert result.error == "reviewed:fact_only"


def test_selector_module_does_not_import_mlx() -> None:
    imported = set(sys.modules)
    assert not any(
        name == "mlx" or name.startswith("mlx.")
        for name in imported
        if name in selector.__dict__
    )
    source = selector.__file__
    assert source
    assert "import mlx" not in open(source, encoding="utf-8").read()


def test_continuous_cleaner_is_bounded_two_stage_source_selector() -> None:
    job = next(
        row
        for row in operational_jobs()
        if row["id"] == "job_legacy_judgment_resummary_quality"
    )
    command = job["command"]
    assert "judgment_summary_staged_backfill.py" in command
    assert "--scan-limit 480" in command
    assert "--nvidia-limit 32" in command
    assert "--nvidia-timeout 180" in command
    assert "--local-min-score 80" in command
    assert job["cron"] == "*/15 * * * *"
    assert "兩階段" in job["desc"]
    assert "NVIDIA 120B" in job["desc"]


def test_bulk_and_staged_resummary_are_both_release_bound() -> None:
    root = Path(__file__).resolve().parents[1]
    jobs = json.loads((root / "cron_jobs.json").read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in jobs}
    assert by_id["job_weekend_resummary"]["enabled"] is True
    assert by_id["job_legacy_judgment_resummary_quality"]["enabled"] is True
