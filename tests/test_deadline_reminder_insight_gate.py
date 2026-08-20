from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    name = "court_hearing_reminder_insight_gate_test"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "skills" / "court-hearing-reminder" / "action.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _usable_debt_relief_summary() -> str:
    return (
        "## 裁判要旨\n"
        "債務人聲請更生時，法院應依消費者債務清理條例第3條審查清償可能性。\n"
        "## 實務見解\n"
        "- 按消費者債務清理條例第3條規定，債務人不能清償債務或有不能清償之虞者，"
        "得聲請更生；法院應依收入、必要生活費及無擔保債務具體判斷，不能只以形式文件不足即否准。\n"
        "- 本院認為，債務人已補正收入與債務明細，足供法院審酌更生方案，"
        "故應就實質清償可能性判斷。"
    )


def _usable_debt_relief_source() -> str:
    return (
        "理由\n"
        "按消費者債務清理條例第3條規定，債務人不能清償債務或有不能清償之虞者，"
        "得聲請更生；法院應依收入、必要生活費及無擔保債務具體判斷，不能只以形式文件不足即否准。\n"
        "本院認為，債務人已補正收入與債務明細，足供法院審酌更生方案，"
        "故應就實質清償可能性判斷。"
    )


def test_deadline_notice_rejects_extractive_and_cross_domain_rows(monkeypatch, tmp_path):
    module = _load_module()
    judgments = [
        {
            "title": "最高法院刑事判決",
            "summary_type": "llm",
            # Simulate a polluted historical reason field: title/summary still
            # prove this is criminal and must never reach a civil deadline.
            "case_reason": "更生",
            "summary": "## 裁判要旨\n刑事程序。\n## 實務見解\n- 刑事訴訟法關於非常上訴之法律適用。",
        },
        {
            "title": "民事裁定",
            "summary_type": "llm",
            "case_reason": "更生",
            "summary": "## 摘要型別\n抽取式快篩（主文與理由均取自裁判原文；未經 LLM 改寫）\n"
            "## 主文摘錄\n駁回。\n## 實務見解\n- 更生程序事項。",
        },
        {
            "title": "民事更生裁定",
            "summary_type": "",  # legacy untyped records are not citeable
            "case_reason": "更生",
            "summary": _usable_debt_relief_summary(),
        },
        {
            "title": "民事更生裁定",
            "summary_type": "llm",
            "case_reason": "更生",
            "case_type": "民事",
            "summary": _usable_debt_relief_summary(),
            "full_text": _usable_debt_relief_source(),
        },
    ]
    path = tmp_path / "judgments.json"
    path.write_text(__import__("json").dumps(judgments, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(module, "get_judgments_json_path", lambda: path)

    items = module._fetch_related_judgments("更生", "臺灣某地方法院", case_type="民事")

    assert len(items) == 1
    assert "民事更生裁定" in items[0]
    assert "抽取式快篩" not in items[0]
    assert "刑事" not in items[0]


def test_deadline_notice_has_no_generic_empty_reason_fallback(monkeypatch, tmp_path):
    module = _load_module()
    path = tmp_path / "judgments.json"
    path.write_text(
        __import__("json").dumps(
            [{"title": "未標記事項", "summary_type": "llm", "case_reason": "", "summary": _usable_debt_relief_summary()}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "get_judgments_json_path", lambda: path)

    assert module._fetch_related_judgments("更生", "", case_type="民事") == []
