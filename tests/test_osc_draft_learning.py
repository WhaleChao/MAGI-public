def test_record_draft_feedback_and_prompt_guidance(tmp_path, monkeypatch):
    from api.osc import draft_learning

    monkeypatch.setattr(draft_learning, "EVENTS_PATH", tmp_path / "events.jsonl")

    result = draft_learning.record_draft_feedback(
        {
            "case_number": "114年度建字第16號",
            "doc_type": "民事聲請調查證據狀",
            "original_text": "案號：OSC-001\n一、請求調查證據。",
            "corrected_text": "案號：114年度建字第16號\n一、懇請 鈞院調查施工日誌。",
            "note": "不要把內部案號當法院案號；聲請調查要寫懇請 鈞院。",
        },
        actor="tester",
    )

    assert result["ok"] is True
    assert result["event"]["stats"]["char_delta"] != 0
    assert result["event"]["lessons"]

    guidance = draft_learning.learning_guidance_for_prompt(
        doc_type="民事聲請調查證據狀",
        case_number="114年度建字第16號",
        reason="損害賠償",
    )

    assert "不要把內部案號當法院案號" in guidance
    assert "懇請" in guidance


def test_record_draft_feedback_rejects_no_change(tmp_path, monkeypatch):
    from api.osc import draft_learning

    monkeypatch.setattr(draft_learning, "EVENTS_PATH", tmp_path / "events.jsonl")

    result = draft_learning.record_draft_feedback(
        {
            "original_text": "民事起訴狀\n一、內容。",
            "corrected_text": "民事起訴狀\n一、內容。",
        }
    )

    assert result["ok"] is False
    assert result["error"] == "no_change"


def test_custom_draft_template_still_receives_learning_guidance(tmp_path, monkeypatch):
    from api.osc import draft_learning, drafts

    monkeypatch.setattr(draft_learning, "EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(drafts, "_osc_exec", lambda *args, **kwargs: ({}, None))
    monkeypatch.setattr(drafts, "_osc_collect_insights", lambda: [])
    monkeypatch.setattr(drafts, "_osc_get_setting_value", lambda key, default="": "自訂模板：{doc_type}\n{case_facts}")

    draft_learning.record_draft_feedback(
        {
            "case_number": "115年度訴字第99號",
            "doc_type": "民事準備書狀",
            "reason": "損害賠償",
            "original_text": "一、請求事項\n請求調查。",
            "corrected_text": "一、聲明事項\n懇請 鈞院命對造提出契約原本。",
            "note": "自訂模板也必須帶入人工修正紀錄。",
        }
    )

    ctx = drafts._osc_build_draft_context(
        {
            "doc_type": "民事準備書狀",
            "case_number": "115年度訴字第99號",
            "reason": "損害賠償",
            "case_facts": "測試事實",
        }
    )

    assert "自訂模板" in ctx["prompt"]
    assert "使用者修正學習紀錄" in ctx["prompt"]
    assert "人工修正紀錄" in ctx["prompt"]
    assert "聲明事項" in ctx["prompt"]


def test_learning_guidance_does_not_mix_different_case_reasons(tmp_path, monkeypatch):
    from api.osc import draft_learning

    monkeypatch.setattr(draft_learning, "EVENTS_PATH", tmp_path / "events.jsonl")

    draft_learning.record_draft_feedback(
        {
            "case_number": "115年度消債更字第1號",
            "doc_type": "民事準備書狀",
            "reason": "消債更生",
            "original_text": "請求事項：准予更生。",
            "corrected_text": "聲明事項：請裁定開始更生程序。",
            "note": "消債案件要寫開始更生程序。",
        }
    )
    draft_learning.record_draft_feedback(
        {
            "case_number": "115年度訴字第99號",
            "doc_type": "民事準備書狀",
            "reason": "損害賠償",
            "original_text": "請求事項：請求調查。",
            "corrected_text": "聲明事項：請命對造提出契約原本。",
            "note": "損害賠償案件要具體列契約證據。",
        }
    )

    guidance = draft_learning.learning_guidance_for_prompt(
        doc_type="民事準備書狀",
        case_number="115年度訴字第100號",
        reason="損害賠償",
    )

    assert "損害賠償案件" in guidance
    assert "消債案件" not in guidance
    assert "開始更生程序" not in guidance
