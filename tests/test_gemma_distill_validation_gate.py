from scripts import nightly_distill_gemma as nightly
from scripts import train_gemma_e4b_lora as gemma_train


def test_validate_output_gate_rejects_channel_markers():
    ok, reasons, _ = gemma_train._validate_output_gate(
        "這是回答。<|channel>thought 我先思考，再回答。"
    )
    assert not ok
    assert "channel_marker_leak" in reasons


def test_validate_output_gate_rejects_english_thinking_trace():
    ok, reasons, _ = gemma_train._validate_output_gate(
        "Analysis: let's think step by step before final answer. "
        "這段內容即使混入中文也應被擋下，避免英文思考軌跡外洩。"
    )
    assert not ok
    assert "english_thinking_trace" in reasons


def test_validate_output_gate_rejects_too_short_answer():
    ok, reasons, _ = gemma_train._validate_output_gate("可請求賠償。")
    assert not ok
    assert "too_short" in reasons


def test_validate_output_gate_rejects_insufficient_traditional_chinese():
    ok, reasons, stats = gemma_train._validate_output_gate(
        "This answer is mostly English legal text with only 少量中文, so it should fail gate."
    )
    assert not ok
    assert "insufficient_traditional_chinese" in reasons
    assert stats["cjk_chars"] < gemma_train.MIN_CJK_CHARS


def test_validate_output_gate_rejects_simplified_chinese():
    ok, reasons, stats = gemma_train._validate_output_gate(
        "法院认定被告应负赔偿责任，原告可请求给付损害赔偿，并得申请强制执行。"
    )
    assert not ok
    assert "simplified_chinese_detected" in reasons
    assert stats["simplified_chars"]


def test_validate_output_gate_accepts_traditional_chinese_answer():
    text = (
        "損害賠償係指因侵權或債務不履行所生之財產與非財產損害，"
        "被害人得依民法請求回復原狀或金錢賠償，並應具體說明因果關係與損害範圍。"
    )
    ok, reasons, stats = gemma_train._validate_output_gate(text)
    assert ok
    assert reasons == []
    assert stats["cjk_chars"] >= gemma_train.MIN_CJK_CHARS


def test_build_validation_messages_contains_suppression_instructions():
    messages = gemma_train._build_validation_messages("測試問題")
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert "/no_think" in messages[0]["content"]
    assert "<|channel>" in messages[0]["content"]


def test_nightly_validation_gate_prefers_validation_pass():
    assert nightly._validation_gate_passed({"validation_pass": True, "success": False}) is True
    assert nightly._validation_gate_passed({"validation_pass": False, "success": True}) is False


def test_nightly_validation_gate_fallback_to_success():
    assert nightly._validation_gate_passed({"success": True}) is True
    assert nightly._validation_gate_passed({"success": False}) is False
    assert nightly._validation_gate_passed({}) is False
    assert nightly._validation_gate_passed("bad") is False


def test_deploy_model_refuses_rejected_pending(monkeypatch, tmp_path):
    pending = tmp_path / "pending_deploy.json"
    pending.write_text(
        '{"version":"gemma-distill-v001","status":"rejected","deploy_allowed":false}',
        encoding="utf-8",
    )
    monkeypatch.setattr(nightly, "PENDING_DEPLOY_PATH", pending)
    monkeypatch.setattr(nightly, "DISTILL_DIR", tmp_path)

    assert nightly.deploy_model("gemma-distill-v001") == 1
