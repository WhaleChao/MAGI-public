from __future__ import annotations

import json

import pytest

from skills.engine.pii_scrubber import PIIScrubber, PRIVACY_POLICY_VERSION


PRIVATE_SAMPLE = (
    "原告王大明，身分證 A123456789，手機 0912-345-678，"
    "電話 (02)2345-6789，電子郵件 test@example.com，"
    "出生日期民國80年1月2日，住址：臺北市中正區忠孝東路1號，"
    "銀行帳號 1234-5678-9012，法扶案號1150409-I-004，"
    "法院114年度原訴字第24號，本所案號2026-0062。"
)


def _verified_scrubber() -> PIIScrubber:
    return PIIScrubber(
        known_names=["王大明", "陳小美"],
        known_names_verified=True,
        source="test",
    )


def test_taiwan_law_office_identifiers_are_removed_and_restorable():
    result = _verified_scrubber().scrub(PRIVATE_SAMPLE)
    assert result.safe_to_send is True
    assert result.residual_categories == ()
    assert result.restore(result.scrubbed_text) == PRIVATE_SAMPLE
    for private_value in (
        "王大明",
        "A123456789",
        "0912-345-678",
        "test@example.com",
        "臺北市中正區忠孝東路1號",
        "1150409-I-004",
        "2026-0062",
    ):
        assert private_value not in result.scrubbed_text
    assert result.policy_version == PRIVACY_POLICY_VERSION


def test_legal_prose_is_not_misclassified_as_a_person():
    text = "律師的工作是分析證據，被告主張無罪，本院認為上訴無理由。"
    result = _verified_scrubber().scrub(text)
    assert result.safe_to_send is True
    assert result.scrubbed_text == text


def test_labelled_name_without_space_is_removed():
    result = PIIScrubber(known_names=[], known_names_verified=True).scrub("被告陳小美主張無罪。")
    assert result.safe_to_send is True
    assert "陳小美" not in result.scrubbed_text
    assert result.counts["labelled_name"] == 1


def test_office_profile_fails_closed_without_verified_name_inventory():
    result = PIIScrubber(known_names=None, known_names_verified=False).scrub("請摘要這份案件資料")
    assert result.safe_to_send is False
    assert "known_name_inventory_unavailable" in result.warnings


def test_public_judgment_profile_does_not_need_office_name_inventory():
    result = PIIScrubber(known_names=None, known_names_verified=False).scrub(
        "本院認為契約解除應以意思表示到達為準。",
        profile="public_judgment",
        require_known_names=False,
    )
    assert result.safe_to_send is True


def test_certificate_never_contains_original_values_or_mapping():
    result = _verified_scrubber().scrub(PRIVATE_SAMPLE)
    encoded = json.dumps(result.certificate(), ensure_ascii=False)
    assert "mapping" not in result.certificate()
    assert "王大明" not in encoded
    assert "A123456789" not in encoded
    assert result.original_sha256 and result.scrubbed_sha256


def test_second_pass_detects_unmasked_identifier():
    scrubber = _verified_scrubber()
    residuals = scrubber.detect_residuals("聯絡 test@example.com", profile="office_confidential")
    assert residuals == ["email"]


class _FakeResponse:
    def __init__(self, *, status_code=200, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload


def _prepare_nim(monkeypatch):
    from skills.bridge import nim_heavy

    monkeypatch.setenv("NVIDIA_NIM_ENABLE", "1")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "test-key")
    monkeypatch.setattr(nim_heavy, "_get_daily_count", lambda: 0)
    monkeypatch.setattr(nim_heavy, "_incr_daily_count", lambda: 1)
    monkeypatch.setattr(nim_heavy, "_cb_can_call", lambda: (True, ""))
    monkeypatch.setattr(nim_heavy, "_cb_record_success", lambda: None)
    monkeypatch.setattr(nim_heavy, "record_nim_outcome", lambda *args, **kwargs: None)
    monkeypatch.setattr(nim_heavy, "_log_usage", lambda payload: None)
    monkeypatch.setattr(nim_heavy, "build_scrubber_from_magi_db", _verified_scrubber)
    return nim_heavy


def test_nim_scrubs_user_and_system_messages_before_network(monkeypatch):
    nim_heavy = _prepare_nim(monkeypatch)
    observed = {}

    def fake_post(url, *, headers, json, timeout):
        observed["messages"] = json["messages"]
        return _FakeResponse(
            text='{"ok":true}',
            payload={"choices": [{"message": {"content": "[PERSON-001]案件已完成"}}]},
        )

    monkeypatch.setattr(nim_heavy.requests, "post", fake_post)
    result = nim_heavy.run_nim_chat(
        prompt="原告王大明的身分證 A123456789",
        system_prompt="承辦律師：陳小美",
        task_type="legal_analysis",
        require_pii_scrub=True,
    )
    outbound = json.dumps(observed["messages"], ensure_ascii=False)
    assert "王大明" not in outbound
    assert "陳小美" not in outbound
    assert "A123456789" not in outbound
    assert result["success"] is True
    assert result["response"] == "王大明案件已完成"
    assert result["privacy_certificate"]["safe_to_send"] is True


def test_nim_never_allows_scrub_bypass_for_office_data(monkeypatch):
    nim_heavy = _prepare_nim(monkeypatch)
    observed = {}

    def fake_post(url, *, headers, json, timeout):
        observed["messages"] = json["messages"]
        return _FakeResponse(
            text='{"ok":true}',
            payload={"choices": [{"message": {"content": "[PERSON-001]案件已完成"}}]},
        )

    monkeypatch.setattr(nim_heavy.requests, "post", fake_post)
    result = nim_heavy.run_nim_chat(
        prompt="原告王大明",
        require_pii_scrub=False,
        data_classification="office_confidential",
    )
    assert result["success"] is True
    assert "王大明" not in json.dumps(observed["messages"], ensure_ascii=False)
    assert result["response"] == "王大明案件已完成"
    assert result["privacy_certificate"]["safe_to_send"] is True


def test_nim_scrubs_explicit_synthetic_probe_even_when_legacy_bypass_is_requested(monkeypatch):
    nim_heavy = _prepare_nim(monkeypatch)
    monkeypatch.setattr(
        nim_heavy.requests,
        "post",
        lambda *args, **kwargs: _FakeResponse(
            text='{"ok":true}',
            payload={"choices": [{"message": {"content": "2"}}]},
        ),
    )
    result = nim_heavy.run_nim_chat(
        prompt="1+1=?",
        require_pii_scrub=False,
        data_classification="synthetic",
    )
    assert result["success"] is True
    # ``pii_scrubbed`` means that at least one value was replaced, not that the
    # mandatory privacy gate was skipped.  This probe contains no PII.
    assert result["pii_scrubbed"] is False
    assert result["privacy_certificate"]["safe_to_send"] is True
    assert result["privacy_certificate"]["original_sha256"]


def test_nim_allows_verbatim_content_only_for_exam_tutor_grading(monkeypatch):
    nim_heavy = _prepare_nim(monkeypatch)
    observed = {}

    def fake_post(url, *, headers, json, timeout):
        observed["messages"] = json["messages"]
        return _FakeResponse(
            text='{"ok":true}',
            payload={"choices": [{"message": {"content": "甲與乙的法律關係"}}]},
        )

    monkeypatch.setattr(nim_heavy.requests, "post", fake_post)
    prompt = "題目：甲對乙提起114年度訴字第24號；作答：王大明認為契約有效。"
    result = nim_heavy.run_nim_chat(
        prompt=prompt,
        task_type="exam_tutor_grading",
        require_pii_scrub=False,
        data_classification="exam_practice_content",
        privacy_profile="exam_practice_content",
        restore_pii=False,
    )
    outbound = json.dumps(observed["messages"], ensure_ascii=False)
    assert prompt in outbound
    assert result["success"] is True
    assert result["pii_scrubbed"] is False
    assert result["content_handling"] == "verbatim_exam_content"
    assert result["privacy_certificate"]["safe_to_send"] is True


def test_nim_rejects_exam_profile_as_bypass_for_other_tasks(monkeypatch):
    nim_heavy = _prepare_nim(monkeypatch)
    result = nim_heavy.run_nim_chat(
        prompt="原告王大明",
        task_type="legal_analysis",
        require_pii_scrub=False,
        data_classification="exam_practice_content",
        privacy_profile="exam_practice_content",
    )
    assert result["success"] is False
    assert result["error"] == "privacy_exam_profile_scope_invalid"


def test_nim_allows_verbatim_public_sources_only_for_exam_trend_job(monkeypatch):
    nim_heavy = _prepare_nim(monkeypatch)
    observed = {}

    def fake_post(url, *, headers, json, timeout):
        observed["messages"] = json["messages"]
        return _FakeResponse(
            text='{"ok":true}',
            payload={"choices": [{"message": {"content": "公開法律爭點"}}]},
        )

    monkeypatch.setattr(nim_heavy.requests, "post", fake_post)
    prompt = "司法院公開資料：臺北市重慶南路一段124號；115年度憲判字第1號。"
    result = nim_heavy.run_nim_chat(
        prompt=prompt,
        task_type="exam_tutor_trend_analysis",
        require_pii_scrub=False,
        data_classification="public_source",
        privacy_profile="public_source",
        restore_pii=False,
        background_heavy_authorized={
            "job_id": "exam-trend-analysis",
            "task_type": "exam_tutor_trend_analysis",
            "source_class": "public_source",
            "provider": "nvidia_nim",
            "model": nim_heavy._pick_model("exam_tutor_trend_analysis", heavy=False),
            "daily_budget": 3,
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
    )

    assert prompt in json.dumps(observed["messages"], ensure_ascii=False)
    assert result["success"] is True
    assert result["content_handling"] == "verbatim_public_source"
    assert result["privacy_certificate"]["safe_to_send"] is True


def test_nim_keeps_scrubbing_for_other_public_source_tasks(monkeypatch):
    nim_heavy = _prepare_nim(monkeypatch)
    monkeypatch.setattr(
        nim_heavy.requests,
        "post",
        lambda *args, **kwargs: _FakeResponse(
            text='{"ok":true}',
            payload={"choices": [{"message": {"content": "公開摘要"}}]},
        ),
    )
    result = nim_heavy.run_nim_chat(
        prompt="公開來源",
        task_type="legal_analysis",
        require_pii_scrub=False,
        data_classification="public_source",
        privacy_profile="public_source",
    )

    assert result["success"] is True
    assert result["content_handling"] == "pii_scrubbed"
    assert result["privacy_certificate"]["original_sha256"]


def test_nim_error_log_uses_hash_not_provider_body(monkeypatch):
    nim_heavy = _prepare_nim(monkeypatch)
    events = []
    monkeypatch.setattr(nim_heavy, "_log_usage", events.append)
    monkeypatch.setattr(
        nim_heavy.requests,
        "post",
        lambda *args, **kwargs: _FakeResponse(
            status_code=400,
            text="provider echoed 王大明 A123456789",
        ),
    )
    result = nim_heavy.run_nim_chat(prompt="原告王大明 A123456789")
    assert result["success"] is False
    encoded = json.dumps(events, ensure_ascii=False)
    assert "王大明" not in encoded
    assert "A123456789" not in encoded
    assert "error_body_sha256" in encoded


def test_llm_direct_scrubs_anthropic_messages_and_restores_locally(monkeypatch):
    from skills.bridge import llm_direct
    import skills.engine.pii_scrubber as privacy_module

    monkeypatch.setenv("MAGI_ALLOW_CLOUD_MODELS", "1")
    monkeypatch.setitem(llm_direct.PROVIDERS["claude"], "api_key", "test-key")
    monkeypatch.setattr(privacy_module, "build_scrubber_from_magi_db", _verified_scrubber)
    observed = {}

    def fake_dispatcher(cfg, messages, **kwargs):
        observed["messages"] = messages
        return {"text": "[PERSON-001]的結果", "usage": {"total": 1}}

    monkeypatch.setitem(llm_direct._API_DISPATCHERS, "anthropic", fake_dispatcher)
    result = llm_direct.chat(
        prompt="原告王大明的案件",
        feature="react",
        provider="claude",
    )
    outbound = json.dumps(observed["messages"], ensure_ascii=False)
    assert "王大明" not in outbound
    assert result["success"] is True
    assert result["text"] == "王大明的結果"


def test_remote_response_with_new_identifier_is_blocked(monkeypatch):
    nim_heavy = _prepare_nim(monkeypatch)
    monkeypatch.setattr(
        nim_heavy.requests,
        "post",
        lambda *args, **kwargs: _FakeResponse(
            text='{"ok":true}',
            payload={"choices": [{"message": {"content": "請聯絡 test@example.com"}}]},
        ),
    )
    result = nim_heavy.run_nim_chat(prompt="請分析契約")
    assert result["success"] is False
    assert result["error"] == "privacy_response_blocked:email"
