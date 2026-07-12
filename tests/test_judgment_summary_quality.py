from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_judgment_action():
    path = ROOT / "skills" / "judgment-collector" / "action.py"
    spec = importlib.util.spec_from_file_location("judgment_collector_action_quality_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_insight_refine_action():
    path = ROOT / "skills" / "insight-refine" / "action.py"
    spec = importlib.util.spec_from_file_location("insight_refine_action_quality_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_legal_insight_summary_must_be_source_supported():
    action = _load_judgment_action()
    source = (
        "臺灣臺南地方法院刑事判決\n"
        "理由\n"
        "按刑法上之故意，可分為確定故意與不確定故意，所謂不確定故意，"
        "係指行為人對於構成犯罪之事實，預見其發生而其發生並不違背其本意者。"
    )
    summary = (
        "## 實務見解\n"
        "按刑法上之故意，可分為確定故意與不確定故意，所謂不確定故意，"
        "係指行為人對於構成犯罪之事實，預見其發生而其發生並不違背其本意者。\n"
        "## 適用法條\n刑法第13條"
    )
    assert action._summary_source_support_failure(summary, source) == ""


def test_legal_insight_summary_rejects_unsupported_opinion():
    action = _load_judgment_action()
    source = "理由\n本院認為，被告所為應依卷內證據認定。"
    summary = "## 實務見解\n最高法院向來認為所有帳戶交付均成立詐欺幫助犯，且不得例外。"
    assert action._summary_source_support_failure(summary, source) == "unsupported_opinion"


def test_legal_insight_summary_accepts_short_source_supported_opinion():
    action = _load_judgment_action()
    source = "理由\n本院認為應依法宣告沒收。另審酌被告犯後態度，量處如主文所示之刑。"
    summary = "## 實務見解\n本院認為應依法宣告沒收。\n## 適用法條\n刑法第38條"

    assert action._summary_source_support_failure(summary, source) == ""


def test_legal_insight_summary_rejects_unsupported_citation():
    action = _load_judgment_action()
    source = "理由\n本院認為，被告所為應依卷內證據認定。"
    summary = "## 實務見解\n本院認為，被告所為應依卷內證據認定。\n## 引用裁判\n最高法院113年度台上字第5678號判決"
    assert action._summary_source_support_failure(summary, source).startswith("unsupported_citation:")


def test_extract_fallback_has_source_supported_practice_insight():
    action = _load_judgment_action()
    source = (
        "臺灣測試地方法院刑事判決\n"
        "主文\n被告犯詐欺取財罪。\n"
        "事實及理由\n"
        "按刑法第339條規定，意圖為自己不法之所有，以詐術使人將本人之物交付者，成立詐欺取財罪。\n"
        "經查，被告以前開方式取得款項，足認其主觀上具有不法所有意圖。\n"
        "中華民國一一五年五月十日\n"
    )

    summary = action._extractive_judgment_summary(source, "詐欺")

    assert "## 實務見解" in summary
    assert "## 理由摘錄" in summary
    assert "摘要失敗，前 20 行預覽" not in summary
    assert action._summary_source_support_failure(summary, source) == ""


def test_summarize_judgment_uses_extract_fallback_when_models_fail(monkeypatch):
    action = _load_judgment_action()
    source = (
        "臺灣測試地方法院刑事判決\n"
        "主文\n被告犯詐欺取財罪。\n"
        "事實及理由\n"
        "按刑法第339條規定，意圖為自己不法之所有，以詐術使人將本人之物交付者，成立詐欺取財罪。\n"
        "經查，被告以前開方式取得款項，足認其主觀上具有不法所有意圖。\n"
        "中華民國一一五年五月十日\n"
    )
    monkeypatch.setattr(action, "_run_skill", lambda *_args, **_kwargs: {"success": False, "error": "offline"})
    monkeypatch.setattr(action, "_get_inference_gateway", lambda: None)

    summary = action._summarize_judgment(source, "詐欺", timeout_sec=1)

    assert "## 實務見解" in summary
    assert "摘要失敗，前 20 行預覽" not in summary
    assert action._get_last_summary_meta()["route"] == "extractive_fallback"
    assert action._summary_source_support_failure(summary, source) == ""


def test_extract_fallback_validates_against_full_text_after_preextract(monkeypatch):
    action = _load_judgment_action()
    source = (
        "臺灣測試地方法院刑事判決\n"
        "主文\n"
        "被告犯三人以上共同詐欺取財罪，處有期徒刑壹年。\n"
        "犯罪事實\n"
        "一、被告加入詐欺集團擔任取款車手。\n"
        "理由\n"
        "一、認定犯罪事實之理由及證據：\n"
        "經查，被告對上揭犯罪事實坦承不諱，核與卷內證據相符，"
        "足認被告任意性之自白確與事實相符，堪以採信；又卷附通聯紀錄、"
        "監視器畫面及告訴人陳述互核一致，足以補強其自白之真實性。\n"
        "二、新舊法比較：\n"
        "按修正前詐欺犯罪危害防制條例第47條前段規定，犯詐欺犯罪，"
        "在偵查及歷次審判中均自白，如有犯罪所得，自動繳交其犯罪所得者，減輕其刑；"
        "修正後則改採支付與被害人達成調解或和解之全部金額者，得減輕其刑，"
        "並將必減輕其刑修正為得減輕其刑，規範效果已有不同。\n"
        "查被告行為後法規已有修正，經比較結果，舊法較有利於被告，"
        "依刑法第2條第1項前段規定，自應適用修正前規定。\n"
        "中華民國一一五年五月十日\n"
    )
    assert action._extract_court_opinion_sections(source) != source
    monkeypatch.setattr(action, "_run_skill", lambda *_args, **_kwargs: {"success": False, "error": "offline"})
    monkeypatch.setattr(action, "_get_inference_gateway", lambda: None)

    summary = action._summarize_judgment(source, "詐欺", timeout_sec=1)

    assert "## 實務見解" in summary
    assert "摘要失敗，前 20 行預覽" not in summary
    assert action._get_last_summary_meta()["route"] == "extractive_fallback"
    assert action._summary_source_support_failure(summary, source) == ""


def test_insight_refine_short_output_does_not_return_raw_text(monkeypatch, capsys):
    action = _load_insight_refine_action()
    fake_client = types.SimpleNamespace(
        casper_chat=lambda prompt, timeout_sec=240: {"success": True, "response": "短", "route": "fake"}
    )
    monkeypatch.setitem(sys.modules, "casper_tools_client", fake_client)
    payload = {"raw_text": "這是一段足夠長的原始判決文字，不應在模型輸出太短時被當成摘要回傳。"}
    monkeypatch.setattr(sys, "argv", ["action.py", "--task", "refine " + json.dumps(payload, ensure_ascii=False)])

    rc = action.main()
    out = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert out["success"] is False
    assert "raw_text" not in out.get("output", "")
