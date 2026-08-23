from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    return module


def _complete_draft() -> str:
    return """民事起訴狀
案號：115年度訴字第123號
原告：王○○
被告：林○○
案由：損害賠償
聲明事項
一、被告應負損害賠償責任。
事實及理由
一、依民法第184條規定，被告侵害原告權利，應負損害賠償責任。
此致
臺灣臺北地方法院
具狀人：王○○
中華民國115年8月9日
"""


def test_strict_draft_quality_accepts_complete_source_grounded_pleading() -> None:
    from api.osc.saas_workbench import quality_check

    draft = _complete_draft()
    result = quality_check(
        {
            "mode": "draft",
            "strict_export": True,
            "draft_text": draft,
            "doc_type": "民事起訴狀",
            "case_number": "115年度訴字第123號",
            "court_name": "臺灣臺北地方法院",
            "reason": "損害賠償",
            "grounding_text": "本案資料及參考文件載有民法第184條。",
            "selected_documents": [{"file_path": "/safe/reference.docx"}],
            "source_paths": ["/safe/reference.docx"],
            "citation_validation": {"ok": True},
        }
    )

    assert result["pass"] is True, result["issues"]


def test_strict_draft_quality_blocks_ungrounded_statute_and_placeholder() -> None:
    from api.osc.saas_workbench import quality_check

    draft = _complete_draft().replace("王○○\n中華", "（待確認）\n中華")
    result = quality_check(
        {
            "mode": "draft",
            "strict_export": True,
            "draft_text": draft,
            "doc_type": "民事起訴狀",
            "case_number": "115年度訴字第123號",
            "court_name": "臺灣臺北地方法院",
            "reason": "損害賠償",
            "grounding_text": "只提供事故事實，沒有任何法條。",
            "citation_validation": {"ok": True},
        }
    )

    codes = {item["code"] for item in result["issues"]}
    assert result["pass"] is False
    assert "placeholder" in codes
    assert "ungrounded_statute_reference" in codes


def test_strict_draft_quality_blocks_citation_lock_violation() -> None:
    from api.osc.saas_workbench import quality_check

    result = quality_check(
        {
            "mode": "draft",
            "strict_export": True,
            "draft_text": _complete_draft(),
            "doc_type": "民事起訴狀",
            "case_number": "115年度訴字第123號",
            "court_name": "臺灣臺北地方法院",
            "reason": "損害賠償",
            "grounding_text": "民法第184條",
            "citation_validation": {"ok": False, "violations": [{"case_key": "x"}]},
        }
    )

    assert result["pass"] is False
    assert "citation_lock_violation" in {item["code"] for item in result["issues"]}


def test_strict_draft_quality_requires_both_parties() -> None:
    from api.osc.saas_workbench import quality_check

    result = quality_check(
        {
            "mode": "draft",
            "strict_export": True,
            "draft_text": _complete_draft().replace("林○○", "陳○○"),
            "doc_type": "民事起訴狀",
            "case_number": "115年度訴字第123號",
            "court_name": "臺灣臺北地方法院",
            "reason": "損害賠償",
            "plaintiff": "王○○",
            "defendant": "林○○",
            "grounding_text": "王○○對林○○提起損害賠償；民法第184條。",
            "citation_validation": {"ok": True},
        }
    )

    assert result["pass"] is False
    assert "defendant_missing" in {item["code"] for item in result["issues"]}


def test_strict_draft_quality_blocks_invented_and_missing_factual_anchors() -> None:
    from api.osc.saas_workbench import quality_check

    draft = _complete_draft().replace(
        "一、被告應負損害賠償責任。",
        "一、被告應給付新臺幣99,999元（甲證2號）。",
    )
    result = quality_check(
        {
            "mode": "draft",
            "strict_export": True,
            "draft_text": draft,
            "doc_type": "民事起訴狀",
            "case_number": "115年度訴字第123號",
            "court_name": "臺灣臺北地方法院",
            "reason": "損害賠償",
            "plaintiff": "王○○",
            "defendant": "林○○",
            "case_facts": "115年7月1日發生事故，損害新臺幣50,000元，有甲證1號。",
            "grounding_text": "115年7月1日發生事故，損害新臺幣50,000元，有甲證1號；民法第184條。",
            "citation_validation": {"ok": True},
        }
    )

    codes = {item["code"] for item in result["issues"]}
    assert result["pass"] is False
    assert {"ungrounded_amounts", "ungrounded_evidence"} <= codes
    assert {"missing_required_amounts", "missing_required_dates", "missing_required_evidence"} <= codes


def test_draft_frontend_round_trips_context_and_server_quality_state() -> None:
    state = Path("static/osc/osc-state.js").read_text(encoding="utf-8")
    frontend = Path("static/osc/tabs/drafts.js").read_text(encoding="utf-8")
    route = Path("api/blueprints/osc_cases.py").read_text(encoding="utf-8")

    assert "citationLock: {}" in state
    assert "qualityCheck: null" in state
    assert "exportAllowed: false" in state
    assert "...collectDraftPayload()" in frontend
    assert "data.quality_check || null" in frontend
    assert '"strict_export": True' in route
    assert '"draft_quality_gate_failed"' in route


def test_summary_audit_uses_canonical_database_text_when_cache_moved(tmp_path) -> None:
    from scripts.ops.audit_judicial_api_summary_quality import _source_text_for_row

    text, origin = _source_text_for_row(
        {
            "full_text_path": str(tmp_path / "moved.txt"),
            "court_full_text": "理由\n按民法第184條規定，侵權行為人應負損害賠償責任。",
        }
    )
    assert "民法第184條" in text
    assert origin == "court_judgments"


def test_summary_issue_inference_rejects_sentence_fragments_and_normalizes_debt() -> None:
    from api.domains.judgment_summary_quality import infer_case_issue

    assert infer_case_issue(
        "本件聲請之本案係智審法修正施行前繫屬於法院事件，經核於法尚無不合。",
        "115年度民秘聲字第3號",
        "一般",
    ) == "未分類"
    assert infer_case_issue(
        "上列聲請人聲請更生事件，本院裁定如下。",
        "115年度消債更字第9號",
        "一般",
    ) == "更生"


def test_draft_live_compare_nvidia_path_uses_strict_quality(monkeypatch) -> None:
    from scripts.ops import osc_draft_live_compare as compare

    complete = """民事聲請狀
臺灣花蓮地方法院 公鑒
案號：115年度訴字第123號
聲請事項：請准予調查證據。
事實及理由：依輸入資料，為釐清爭點有調查必要。
此致
臺灣花蓮地方法院
具狀人：測試原告
中華民國115年8月9日
""" + "理由補充。" * 80
    monkeypatch.setattr(
        "api.osc.drafts._osc_generate_draft_with_nvidia",
        lambda _prompt: (complete, "nvidia/test"),
    )

    result = compare._run_ai(
        complete,
        "unused",
        "http://unused",
        ["民事聲請狀", "調查證據"],
        sample={
            "title": "民事聲請狀",
            "ai_facts": "臺灣花蓮地方法院 115年度訴字第123號；請調查證據。",
        },
        provider="nvidia",
    )

    assert result["provider"] == "nvidia"
    assert result["model"] == "nvidia/test"
    assert result["quality_check"]["pass"] is True
    assert result["ok"] is True


def test_translation_missing_known_terms_append_bilingual_taiwan_glossary() -> None:
    from api.handlers.document_handler import ensure_translation_terms_visible

    output = ensure_translation_terms_visible(
        "Court interpreters use a powerless style under the Citizen Judges Act.",
        "法院中的語言工作者採用較弱勢的語氣。",
        target_lang="繁體中文",
    )

    assert "司法通譯（court interpreters）" in output
    assert "無力風格（powerless style）" in output
    assert "國民法官法（Citizen Judges Act）" in output
    assert "法庭通譯" not in output


def test_reportlab_pleading_pdf_has_searchable_unicode_text(tmp_path) -> None:
    from pypdf import PdfReader
    from api import startup

    output = tmp_path / "pleading.pdf"
    result = startup._export_form_pdf_reportlab(
        "民事聲請狀",
        "案號：115年度訴字第123號\n聲請事項：請准調查證據。\n此致\n臺灣花蓮地方法院",
        str(output),
    )

    extracted = "\n".join((page.extract_text() or "") for page in PdfReader(str(output)).pages)
    assert result["success"] is True
    assert result["font_embedded"] is True
    assert result["text_layer"] == "embedded_unicode"
    assert "115年度訴字第123號" in extracted
    assert "調查證據" in extracted


def test_pdf_bookmark_generation_has_zero_known_invalid_label_tolerance(tmp_path) -> None:
    from scripts import weekend_bookmark_batch
    from scripts.ops import benchmark_pdf_bookmarker

    source = tmp_path / "准予扶助證明書_1150605-W-002_1150611.pdf"

    assert weekend_bookmark_batch._single_doc_bookmark_title(source) == "准予扶助證明書"
    assert benchmark_pdf_bookmarker.LABEL_MATCH_THRESHOLD == 1.0

    validator, bookmarker = benchmark_pdf_bookmarker._load_bookmarker_modules()
    assert callable(bookmarker.normalize_bookmark)
    assert bookmarker._single_doc_bookmark_title(source) == "准予扶助證明書"
    assert validator.validate_bookmark(bookmarker._single_doc_bookmark_title(source)) == (True, [])


def test_pdf_namer_live_gate_requires_99_percent_and_retains_late_failures() -> None:
    from scripts.ops import benchmark_pdf_namer

    rows = [{"path": f"ok-{index}.pdf", "valid": True} for index in range(25)]
    rows.append(
        {
            "path": "bad-after-display-cap.pdf",
            "valid": False,
            "quality_ok": False,
            "quality_issues": ["當事人欄不可辨識"],
        }
    )

    assert benchmark_pdf_namer.FORMAT_VALID_THRESHOLD == 0.99
    assert benchmark_pdf_namer.QUALITY_PASS_THRESHOLD == 0.99
    assert benchmark_pdf_namer.OVERALL_PASS_THRESHOLD == 0.99
    assert benchmark_pdf_namer._failure_results(rows) == [rows[-1]]


def test_pdf_namer_live_gate_source_fails_closed_on_degraded_rules() -> None:
    source = (
        __import__("pathlib").Path("scripts/ops/benchmark_pdf_namer.py")
        .read_text(encoding="utf-8")
    )

    assert 'fixture_proposals is None and summary["rules_degraded"]' in source
    assert 'failed.append(' in source


def test_pdf_namer_rejects_field_labels_as_parties() -> None:
    namer = _load_module(
        "pdf_namer_action_party_placeholders",
        ROOT / "skills" / "pdf-namer" / "action.py",
    )
    validator = _load_module(
        "pdf_namer_validator_party_placeholders",
        ROOT / "skills" / "pdf-namer" / "naming_validator.py",
    )

    for token in ("案由", "統一", "當事人", "通知"):
        assert namer._normalize_party_candidate(token) == ""
        ok, issues, details = validator.validate_filename_quality(
            f"20260809 臺北地方法院115年度訴字第1號通知（{token}）.pdf"
        )
        assert ok is False
        assert "當事人欄不是可辨識的人名或機構名稱" in issues
        assert details["implausible_party"] == [token]


def test_pdf_namer_recovers_party_from_case_folder_when_extractor_returns_placeholder() -> None:
    namer = _load_module(
        "pdf_namer_action_case_folder_party",
        ROOT / "skills" / "pdf-namer" / "action.py",
    )
    path = (
        "fixtures/01_案件/法扶案件/刑事/"
        "2026-0066-凡江-二審-毒品危害防制條例/01_法扶資料/通知.pdf"
    )

    assert namer._normalize_party_candidate("案由") == ""
    assert namer._infer_party_from_case_folder_path(path) == "凡江"
    recovered = namer._normalize_party_candidate(
        namer._infer_party_from_case_folder_path(path)
    )
    result = namer._build_name_result(
        found_date="20260612",
        found_type="法院通知",
        found_party=recovered,
        doc_subtype="扶助律師接案通知書",
    )
    assert result["filename"].endswith("通知（凡江）.pdf")


def test_pdf_namer_nested_article_citation_does_not_replace_outer_party() -> None:
    validator = _load_module(
        "pdf_namer_validator_nested_summary",
        ROOT / "skills" / "pdf-namer" / "naming_validator.py",
    )
    filename = (
        "20260612 臺北地方法院115年度訴字第1號通知"
        "（王小明；應依契約（第4條）於期限內提出資料）.pdf"
    )

    party, span = validator._extract_party_segment_with_span(
        validator._strip_ext(filename)
    )
    assert party == "王小明"
    assert filename[span[0] : span[1]] == "王小明"

    ok, issues, details = validator.validate_filename_quality(
        filename,
        source_hint="fixtures/2026-0001-王小明/通知.pdf",
    )
    assert ok is True
    assert "當事人欄不是可辨識的人名或機構名稱" not in issues
    assert "implausible_party" not in details
