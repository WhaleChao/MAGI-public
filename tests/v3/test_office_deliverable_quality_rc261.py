from __future__ import annotations

from api.handlers.output_quality_handler import (
    format_quality_gate_failure,
    run_output_quality_gate,
)


LEGAL_SOURCE = (
    "臺灣臺北地方法院115年度訴字第123號判決。"
    "原告請求被告給付新臺幣10,000元。"
    "本院認為民法第184條的故意、過失、不法侵害、損害與因果關係均應由原告舉證。"
    "因原告未證明相當因果關係，主文駁回原告之訴。"
)


def test_legal_summary_requires_identifiers_money_law_and_reasoning() -> None:
    good = run_output_quality_gate(
        "summary",
        "115年度訴字第123號的爭點是民法第184條。法院認為原告未證明因果關係，駁回新臺幣10,000元的請求。",
        source_text=LEGAL_SOURCE,
    )
    missing = run_output_quality_gate(
        "summary",
        "法院認為原告舉證不足，因此駁回請求。",
        source_text=LEGAL_SOURCE,
    )
    invented = run_output_quality_gate(
        "summary",
        "115年度訴字第999號的爭點是民法第184條，法院認為應駁回新臺幣10,000元的請求。",
        source_text=LEGAL_SOURCE,
    )

    assert good["ok"] is True
    assert good["quality_version"] == "office-deliverable-v2"
    assert missing["issue"] == "summary_critical_anchor_missing"
    assert invented["issue"] == "invented_case_identifier"


def test_translation_must_preserve_critical_anchors() -> None:
    source = "Case 2026-0062 requires payment of NT$10,000 on 2026-08-03."
    good = run_output_quality_gate(
        "translation",
        "2026-0062 案應於 2026-08-03 支付 NT$10,000。",
        source_text=source,
    )
    bad = run_output_quality_gate(
        "translation",
        "本案應於指定日期支付款項。",
        source_text=source,
    )
    assert good["ok"] is True
    assert bad["issue"] == "translation_critical_anchor_missing"


def test_translation_compares_english_and_roc_date_money_semantically() -> None:
    source = (
        "Under Taiwan's Citizen Judges Act, the court interpreter must accurately "
        "interpret the defendant. The court ordered payment of NT$50,000 by September 8, 2026."
    )
    equivalent = run_output_quality_gate(
        "translation",
        "\u4f9d\u81fa\u7063\u570b\u6c11\u6cd5\u5b98\u6cd5\uff0c\u53f8\u6cd5\u901a\u8b6f\u61c9\u6e96\u78ba\u7ffb\u8b6f\u88ab\u544a\u9673\u8ff0\u3002"
        "\u6cd5\u9662\u547d\u65bc\u6c11\u570b115\u5e749\u67088\u65e5\u524d\u7d66\u4ed8\u65b0\u81fa\u5e6350,000\u5143\u3002",
        source_text=source,
    )
    invented = run_output_quality_gate(
        "translation",
        "\u6cd5\u9662\u547d\u65bc\u6c11\u570b115\u5e749\u67089\u65e5\u524d\u7d66\u4ed8\u65b0\u81fa\u5e6399,999\u5143\u3002",
        source_text=source,
    )

    assert equivalent["ok"] is True, equivalent
    assert invented["ok"] is False
    assert invented["issue"] in {"invented_money_anchor", "invented_date_anchor"}
    message = format_quality_gate_failure("translation", invented["issue"])
    assert "invented_" not in message
    assert "原文沒有" in message


def test_transcript_compares_spoken_chinese_date_and_article_semantically() -> None:
    source = "民國一百一十五年九月八日前，依民法第一百八十四條提出答辯。"
    output = "115年9月8日前，依民法第184條提出答辯。"

    result = run_output_quality_gate("transcript", output, source_text=source)

    assert result["ok"] is True, result


def test_repetition_is_not_shipped_as_human_quality_output() -> None:
    repeated = "\n".join(["這是一樣的重複輸出內容。"] * 8)
    for kind in ("summary", "translation", "transcript"):
        gate = run_output_quality_gate(kind, repeated, source_text="原文內容。" * 40)
        assert gate["ok"] is False
        assert gate["issue"].endswith("excessive_repetition")


def test_transcript_requested_timestamp_and_speaker_are_verified() -> None:
    missing = run_output_quality_gate(
        "transcript",
        "今天請求法院延後開庭。",
        instruction="請保留時間戳與說話人",
        metadata={"speaker_count_estimate": 0, "timestamp_text": ""},
    )
    good = run_output_quality_gate(
        "transcript",
        "[00:03] SPEAKER_1：今天請求法院延後開庭。",
        instruction="請保留時間戳與說話人",
        metadata={"speaker_count_estimate": 1, "timestamp_text": "[00:03]"},
    )
    assert missing["issue"] == "transcript_missing_timestamps"
    assert good["ok"] is True


def test_transcript_polish_cannot_change_case_identifier() -> None:
    gate = run_output_quality_gate(
        "transcript",
        "今天討論115年度訴字第999號。",
        source_text="今天討論115年度訴字第123號。",
    )
    assert gate["ok"] is False
    assert gate["issue"] == "invented_case_identifier"


def test_speaker_roles_are_not_invented_from_mentions(monkeypatch) -> None:
    from skills.bridge import balthasar_bridge

    monkeypatch.delenv("MAGI_TRANSCRIBE_AUTO_SPEAKER", raising=False)
    rows = [{"start": 0.0, "end": 2.0, "text": "法官詢問被告是否認罪。"}]
    annotated = balthasar_bridge._annotate_speakers(rows)
    assert "speaker" not in annotated[0]

    explicit = [{"start": 0.0, "end": 2.0, "text": "法官：請被告回答。"}]
    assert balthasar_bridge._annotate_speakers(explicit)[0]["speaker"] == "SPEAKER_法官"


def test_transcript_preserves_and_flags_recognizer_uncertainty() -> None:
    from skills.bridge import balthasar_bridge

    rows = balthasar_bridge._normalize_segments([
        {
            "start": 3.0,
            "end": 7.0,
            "text": "案號聽不清楚。",
            "avg_logprob": -1.3,
            "no_speech_prob": 0.1,
            "compression_ratio": 1.1,
        },
        {"start": 8.0, "end": 10.0, "text": "這一段清楚。", "avg_logprob": -0.2},
    ])
    quality = balthasar_bridge._segment_quality_summary(rows)
    assert rows[0]["avg_logprob"] == -1.3
    assert quality["quality_measured_segments"] == 2
    assert quality["low_confidence_count"] == 1
    assert quality["low_confidence_segments"][0]["start"] == 3.0
