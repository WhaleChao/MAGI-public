from __future__ import annotations

from api import tw_output_guard
from skills.law_review import tw_legal_review


def test_tw_legal_review_module_import_has_default_model() -> None:
    assert isinstance(tw_legal_review.MODEL_NAME, str)


def test_tw_review_cannot_drop_legal_fact_anchors(monkeypatch) -> None:
    source = "臺灣臺北地方法院認為被告應於民國115年8月9日前提出答辯狀，案號115年度訴字第123號。"
    monkeypatch.setenv("MAGI_TW_REVIEW_ENABLED", "1")
    monkeypatch.setattr(
        tw_legal_review,
        "review_legal_text",
        lambda *_args, **_kwargs: "臺灣臺北地方法院認為被告應提出答辯狀。",
    )

    result = tw_output_guard.normalize_output_text(source, force_tw_review=True)

    assert "民國115年8月9日" in result
    assert "115年度訴字第123號" in result
