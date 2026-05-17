from scripts import nightly_distill_gemma as nightly
from scripts import train_gemma_e4b_lora as gemma_train
from skills.bridge import distill_collector


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


def test_nightly_last_accepted_pair_count_prefers_accepted_pairs(tmp_path):
    metrics = tmp_path / "metrics.jsonl"
    metrics.write_text(
        "\n".join(
            [
                '{"train_pairs": 100, "eval_pairs": 10}',
                '{"train_pairs": 80, "eval_pairs": 8, "accepted_pairs": 120}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert nightly._last_accepted_pair_count(metrics) == 120


def test_deploy_model_refuses_rejected_pending(monkeypatch, tmp_path):
    pending = tmp_path / "pending_deploy.json"
    pending.write_text(
        '{"version":"gemma-distill-v001","status":"rejected","deploy_allowed":false}',
        encoding="utf-8",
    )
    monkeypatch.setattr(nightly, "PENDING_DEPLOY_PATH", pending)
    monkeypatch.setattr(nightly, "DISTILL_DIR", tmp_path)

    assert nightly.deploy_model("gemma-distill-v001") == 1


def test_write_rejected_deploy_record_blocks_deploy(monkeypatch, tmp_path):
    pending = tmp_path / "pending_deploy.json"
    merged = tmp_path / "merged" / "Gemma-gemma-distill-v003"
    merged.mkdir(parents=True)
    (merged / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(nightly, "PENDING_DEPLOY_PATH", pending)
    monkeypatch.setattr(nightly, "DISTILL_DIR", tmp_path)

    nightly._write_rejected_deploy_record(
        version="gemma-distill-v003",
        merged_path=str(merged),
        train_result={"success": True},
        validate_result={"validation_pass": False},
        reason="validation failed",
    )

    assert nightly.deploy_model("gemma-distill-v003") == 1


def test_distill_collector_rejects_reasoning_trace_prompt():
    response = (
        "## 裁判要旨\n"
        "法院認為契約解除後仍應依民法規定回復原狀。\n"
        "## 法院見解\n"
        "本件應依判決原文所載事實與法條整理，不得外加推論。"
    )
    reasons = distill_collector._reject_reasons(
        response,
        prompt="### EXECUTE WFGY PROTOCOL\nOutput your thought process before final answer.",
        source="nim_resummary",
    )
    assert "prompt_requests_reasoning_trace" in reasons


def test_distill_collector_rejects_retired_openclaw_source():
    response = (
        "## 裁判要旨\n"
        "法院認為聲請人主張有據，應依相關法律規定處理。\n"
        "## 法院見解\n"
        "法院依卷證資料確認事實，並就各項爭點逐一說明。"
    )
    reasons = distill_collector._reject_reasons(
        response,
        prompt="請摘要判決。",
        source="openclaw_codex",
    )
    assert "retired_source_openclaw" in reasons


def test_distill_training_set_filters_trace_prompt(monkeypatch, tmp_path):
    raw = tmp_path / "raw_pairs.jsonl"
    train = tmp_path / "train.jsonl"
    eval_path = tmp_path / "eval.jsonl"
    good_response = (
        "## 裁判要旨\n"
        "法院認為債務不履行損害賠償應以可歸責事由、損害及因果關係為核心。\n"
        "## 法院見解\n"
        "法院依契約約定與民法規定審酌雙方主張，確認請求是否有理由。"
        "判決並說明請求人仍須就損害範圍、相當因果關係及可歸責事由負舉證責任。"
    )
    bad_prompt = "### EXECUTE WFGY PROTOCOL\nOutput your thought process before final answer."
    rows = [
        {
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "請摘要判決。"},
                {"role": "assistant", "content": good_response},
            ],
            "metadata": {"source": "nim_resummary", "content_hash": "sha256:good"},
        },
        {
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": bad_prompt},
                {"role": "assistant", "content": good_response},
            ],
            "metadata": {"source": "nim_resummary", "content_hash": "sha256:bad"},
        },
    ]
    raw.write_text("\n".join(__import__("json").dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(distill_collector, "RAW_PATH", raw)
    monkeypatch.setattr(distill_collector, "TRAIN_PATH", train)
    monkeypatch.setattr(distill_collector, "EVAL_PATH", eval_path)

    split = distill_collector.build_training_set(eval_ratio=0.5, seed=1)

    assert split == {"train": 1, "eval": 0, "skipped": 1}
    assert "sha256:good" in train.read_text(encoding="utf-8")
    assert not eval_path.exists() or "sha256:bad" not in eval_path.read_text(encoding="utf-8")


def test_distill_count_usable_pairs_ignores_trace_prompt(monkeypatch, tmp_path):
    raw = tmp_path / "raw_pairs.jsonl"
    good_response = (
        "## 裁判要旨\n"
        "法院認為請求人必須提出足以證明其法律主張之證據，始得請求損害賠償。\n"
        "## 法院見解\n"
        "法院依卷內證據判斷事實，並就法條適用與舉證責任分配詳加說明。"
        "若請求人未能證明損害發生、數額及相當因果關係，法院即不得逕行推認其請求有理由。"
    )
    rows = [
        {
            "messages": [
                {"role": "user", "content": "請摘要判決。"},
                {"role": "assistant", "content": good_response},
            ],
            "metadata": {"source": "nim_resummary", "content_hash": "sha256:good"},
        },
        {
            "messages": [
                {"role": "user", "content": "THE 7-STEP REASONING CHAIN"},
                {"role": "assistant", "content": good_response},
            ],
            "metadata": {"source": "nim_resummary", "content_hash": "sha256:bad"},
        },
    ]
    raw.write_text("\n".join(__import__("json").dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(distill_collector, "RAW_PATH", raw)

    assert distill_collector.count_usable_pairs() == {"raw": 2, "usable": 1, "skipped": 1}
