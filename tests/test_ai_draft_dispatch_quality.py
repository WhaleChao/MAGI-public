from __future__ import annotations

from api.osc import drafts
from api.osc import saas_workbench
from api.pipelines.skill_dispatch import dispatch_ai_draft


def _context(**overrides):
    value = {
        "case": {"id": 49},
        "doc_type": "答辯狀",
        "case_number": "115年度訴字第123號",
        "court_name": "臺灣臺北地方法院",
        "reason": "返還借款",
        "plaintiff": "王小明",
        "defendant": "陳小華",
        "case_facts": "原告主張於民國115年1月2日交付新臺幣10萬元，被告否認收受。",
        "prompt": "案件事實與完整書狀生成提示",
        "selected_documents": [],
        "selected_insights": [],
        "citation_lock": {"allowed": [], "rejected": []},
    }
    value.update(overrides)
    return value


def test_ai_draft_requires_case_identifier_before_any_generation(monkeypatch):
    called = {"value": False}
    monkeypatch.setattr(
        drafts,
        "_osc_generate_draft_with_nvidia",
        lambda _prompt: called.update(value=True),
    )

    reply = dispatch_ai_draft("請幫我草擬答辯狀")

    assert "請先提供完整法院案號或事務所案件編號" in reply
    assert called["value"] is False


def test_ai_draft_asks_for_missing_grounding_instead_of_guessing(monkeypatch):
    monkeypatch.setattr(
        drafts,
        "_osc_build_draft_context",
        lambda _payload: _context(case_facts="", defendant=""),
    )
    called = {"value": False}
    monkeypatch.setattr(
        drafts,
        "_osc_generate_draft_with_nvidia",
        lambda _prompt: called.update(value=True),
    )

    reply = dispatch_ai_draft("請幫我草擬答辯狀 2026-0049")

    assert "為避免模型猜寫" in reply
    assert "案件事實" in reply
    assert "對造當事人" in reply
    assert called["value"] is False


def test_ai_draft_blocks_nonempty_output_that_fails_quality(monkeypatch):
    monkeypatch.setattr(drafts, "_osc_build_draft_context", lambda _payload: _context())
    monkeypatch.setattr(drafts, "_osc_generate_draft_with_nvidia", lambda _prompt: ("有文字但不可用", "nim"))
    monkeypatch.setattr(drafts, "_osc_clean_draft_output", lambda text: text)
    monkeypatch.setattr(
        saas_workbench,
        "quality_check",
        lambda _payload: {
            "pass": False,
            "issues": [{"severity": "high", "message": "缺少事實及理由段落"}],
        },
    )

    reply = dispatch_ai_draft("請幫我草擬答辯狀 2026-0049")

    assert "品質閘門攔截" in reply
    assert "缺少事實及理由段落" in reply
    assert "有文字但不可用" not in reply


def test_ai_draft_only_previews_quality_passed_nvidia_output(monkeypatch):
    monkeypatch.setattr(drafts, "_osc_build_draft_context", lambda _payload: _context())
    monkeypatch.setattr(
        drafts,
        "_osc_generate_draft_with_nvidia",
        lambda _prompt: ("答辯狀\n臺灣臺北地方法院\n王小明\n陳小華\n事實及理由\n此致\n具狀人", "nim-120b"),
    )
    monkeypatch.setattr(drafts, "_osc_clean_draft_output", lambda text: text)
    captured = {}

    def _quality(payload):
        captured.update(payload)
        return {"pass": True, "issues": []}

    monkeypatch.setattr(saas_workbench, "quality_check", _quality)

    reply = dispatch_ai_draft("請幫我草擬答辯狀 2026-0049")

    assert "已通過事實錨定與結構檢查" in reply
    assert "nim-120b" in reply
    assert captured["strict_export"] is True
    assert captured["citation_validation"]["ok"] is True
    assert captured["case_facts"].startswith("原告主張")


def test_ai_draft_blocks_invented_judgment_citation(monkeypatch):
    monkeypatch.setattr(drafts, "_osc_build_draft_context", lambda _payload: _context())
    invented = (
        "答辯狀\n臺灣臺北地方法院\n115年度訴字第123號\n王小明\n陳小華\n"
        "事實及理由\n另最高法院114年度台上字第9999號判決同此見解。\n"
        "此致\n臺灣臺北地方法院\n具狀人：王小明"
    )
    monkeypatch.setattr(
        drafts,
        "_osc_generate_draft_with_nvidia",
        lambda _prompt: (invented, "nvidia/nemotron"),
    )

    reply = dispatch_ai_draft("請幫我草擬答辯狀 2026-0049")

    assert "白名單以外" in reply
    assert "114年度台上字第9999號" not in reply
