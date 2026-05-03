"""Tests for PDF bridge large-document summary recovery."""

import re
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch


def _make_gateway_response(text="摘要結果", success=True, model="gemma-4-e4b", route="local_ollama"):
    return {
        "success": success,
        "response": text,
        "model": model,
        "route": route,
        "degraded": not success,
        "error": "" if success else "mock_error",
        "text": text,
        "summary": text,
        "analysis": text,
    }


@patch("skills.bridge.inference_gateway.InferenceGateway")
@patch("skills.bridge.melchior_client._omlx_available", return_value=False)
def test_pdf_chunk_summary_retries_synthetic_timeout(mock_omlx_avail, mock_gw_cls):
    """Chunk summarization should retry timeout placeholders instead of returning them."""
    from skills.documents.pdf_bridge import _mr_summarize_one_chunk

    attempts = {}

    def _mock_chat(prompt, **kwargs):
        m = re.search(r"這是第\s+([0-9.]+)/", prompt)
        label = m.group(1) if m else "merge"
        attempts[label] = attempts.get(label, 0) + 1
        if label == "1" and attempts[label] == 1:
            return {
                "success": True,
                "response": "（系統降級回覆）本機模型逾時，請稍後重試。",
                "text": "（系統降級回覆）本機模型逾時，請稍後重試。",
                "summary": "（系統降級回覆）本機模型逾時，請稍後重試。",
                "analysis": "（系統降級回覆）本機模型逾時，請稍後重試。",
                "model": "gemma-4-e4b",
                "route": "local_ollama",
                "degraded": True,
                "synthetic_fallback": True,
                "error": "mock_timeout",
            }
        return _make_gateway_response(f"第{label}段摘要：法院整理出重要事實、法條與理由。")

    mock_gw = MagicMock()
    mock_gw.chat.side_effect = _mock_chat
    mock_gw_cls.return_value = mock_gw

    chunk_text = ("本案涉及刑事政策、刑罰目的與保安處分制度。" * 220).strip()
    with patch.dict("os.environ", {"MAGI_PDF_MR_CHUNK_RETRIES": "2"}):
        idx, out = _mr_summarize_one_chunk(1, 4, chunk_text, chunk_timeout=30)

    assert idx == 1
    assert "本機模型逾時" not in out
    assert attempts.get("1", 0) >= 2


def test_pdf_map_reduce_total_timeout_does_not_hang():
    """A stuck chunk should honor total_timeout instead of blocking forever."""
    from skills.documents.pdf_bridge import map_reduce_summarize

    def _slow_chunk(*args, **kwargs):
        time.sleep(2.0)
        return args[0], "慢速摘要"

    long_text = ("刑事政策與刑罰理論。" * 800).strip()
    started = time.monotonic()
    with patch("skills.documents.pdf_bridge._mr_summarize_one_chunk", side_effect=_slow_chunk), \
         patch.dict("os.environ", {
             "MAGI_PDF_MR_TOTAL_TIMEOUT_SEC": "1",
             "MAGI_PDF_MR_CHUNK_TIMEOUT_SEC": "1",
             "MAGI_PDF_MR_CHUNK_CHARS": "2000",
         }):
        out = map_reduce_summarize(long_text)
    elapsed = time.monotonic() - started

    assert elapsed < 1.8
    assert out == long_text[:3000]


def test_segment_pages_splits_on_top_level_heading_boundary():
    from skills.documents.pdf_bridge import _segment_pages

    text = "\n\n".join(
        [
            f"--- 第 {idx} 頁 ---\n壹、刑事政策總論\n本頁說明刑事政策定義與沿革。"
            for idx in range(1, 6)
        ]
        + [
            f"--- 第 {idx} 頁 ---\n貳、保安處分\n本頁說明保安處分與監護制度。"
            for idx in range(6, 11)
        ]
    )

    segments = _segment_pages(text, pages_per_segment=12, segment_chars=20000)

    assert len(segments) == 2
    assert segments[0]["label"] == "頁 1-5"
    assert segments[1]["label"] == "頁 6-10"


def test_ultra_large_summary_uses_checkpoint_and_finishes(tmp_path):
    """Ultra-large summaries should checkpoint segment briefs and reuse them on rerun."""
    from skills.documents.pdf_bridge import summarize_ultra_large_text

    pages = []
    for page_no in range(1, 521):
        pages.append(
            f"--- 第 {page_no} 頁 ---\n"
            "壹、刑事政策總論\n"
            f"本頁第 {page_no} 頁重點說明刑罰目的、保安處分與矯治政策的關聯。\n\n"
            "二、制度比較\n"
            "法院並比較寬嚴並進政策、累犯處遇與毒品法庭的制度效果。"
        )
    huge_text = "\n\n".join(pages)

    def _fake_reduce(parts, **kwargs):
        return "\n".join(parts[:3]).strip()

    env = {
        "MAGI_DOC_RUN_ROOT": str(tmp_path),
        "MAGI_PDF_ULTRA_SEGMENT_PAGES": "20",
        "MAGI_PDF_ULTRA_SEGMENT_CHARS": "12000",
        "MAGI_PDF_ULTRA_NOTE_ITEMS": "5",
        "MAGI_PDF_ULTRA_NOTE_MAX_CHARS": "700",
    }

    with patch("skills.documents.pdf_bridge._ultra_segment_note_with_model", return_value="【頁 1-20】主題：刑事政策總論\n- 說明刑罰目的與保安處分"), \
         patch("skills.documents.pdf_bridge._ultra_final_summary_with_model", return_value="【主題總覽】\n- 已完成整合"), \
         patch("skills.documents.pdf_bridge._mr_reduce_summaries", side_effect=_fake_reduce), \
         patch.dict("os.environ", env, clear=False):
        out1 = summarize_ultra_large_text(huge_text, source_hint="huge.pdf", page_count=520)

    assert "可辨識頁數：約 520 頁" in out1
    assert "分析分段：" in out1
    checkpoint_dirs = list((Path(tmp_path) / "pdf_summary").glob("*"))
    assert checkpoint_dirs, "checkpoint dir should be created"
    segment_files = list(checkpoint_dirs[0].glob("segment_*.json"))
    assert len(segment_files) >= 20
    state_path = checkpoint_dirs[0] / "state.json"
    assert state_path.exists()
    assert '"status": "done"' in state_path.read_text(encoding="utf-8")

    with patch("skills.documents.pdf_bridge._ultra_segment_note_with_model", side_effect=RuntimeError("should use cached notes")), \
         patch("skills.documents.pdf_bridge._build_ultra_segment_seed", side_effect=RuntimeError("should use cached notes")), \
         patch("skills.documents.pdf_bridge._ultra_final_summary_with_model", return_value="【主題總覽】\n- 已完成整合"), \
         patch("skills.documents.pdf_bridge._mr_reduce_summaries", side_effect=_fake_reduce), \
         patch.dict("os.environ", env, clear=False):
        out2 = summarize_ultra_large_text(huge_text, source_hint="huge.pdf", page_count=520)

    assert out2 == out1


def test_ultra_large_summary_refreshes_old_note_cache(tmp_path):
    """Old checkpoint note schema should be regenerated instead of reusing stale low-quality notes."""
    from skills.documents.pdf_bridge import summarize_ultra_large_text, _summary_checkpoint_dir

    huge_text = "\n\n".join(
        f"--- 第 {page_no} 頁 ---\n壹、刑事政策\n本頁討論刑罰目的與保安處分。"
        for page_no in range(1, 41)
    )
    with patch.dict("os.environ", {
        "MAGI_DOC_RUN_ROOT": str(tmp_path),
        "MAGI_PDF_ULTRA_SEGMENT_PAGES": "20",
        "MAGI_PDF_ULTRA_SEGMENT_CHARS": "12000",
    }, clear=False):
        checkpoint_dir = _summary_checkpoint_dir("legacy.pdf", huge_text, kind="pdf_summary")
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        (checkpoint_dir / "segment_0001.json").write_text(
            '{"index": 1, "label": "頁 1-20", "note": "舊版抽句摘要"}',
            encoding="utf-8",
        )

    with patch.dict("os.environ", {
        "MAGI_DOC_RUN_ROOT": str(tmp_path),
        "MAGI_PDF_ULTRA_SEGMENT_PAGES": "20",
        "MAGI_PDF_ULTRA_SEGMENT_CHARS": "12000",
    }, clear=False), \
        patch("skills.documents.pdf_bridge._ultra_segment_note_with_model", return_value="【頁 1-20】主題：更新後段摘要\n- 已重新整理重點"), \
        patch("skills.documents.pdf_bridge._ultra_final_summary_with_model", return_value="【主題總覽】\n- 已更新"):
        out = summarize_ultra_large_text(huge_text, source_hint="legacy.pdf", page_count=40)

    assert "已更新" in out


def test_ultra_large_summary_reuses_final_summary_cache(tmp_path):
    from skills.documents.pdf_bridge import summarize_ultra_large_text, _summary_checkpoint_dir

    huge_text = "\n\n".join(
        f"--- 第 {page_no} 頁 ---\n壹、刑事政策\n本頁討論刑罰目的與保安處分。"
        for page_no in range(1, 41)
    )
    env = {"MAGI_DOC_RUN_ROOT": str(tmp_path)}
    with patch.dict("os.environ", env, clear=False):
        checkpoint_dir = _summary_checkpoint_dir("final.pdf", huge_text, kind="pdf_summary")
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        (checkpoint_dir / "final_summary.txt").write_text("【文件概況】\n- 已使用快取", encoding="utf-8")

    with patch.dict("os.environ", env, clear=False), \
         patch("skills.documents.pdf_bridge._ultra_segment_note_with_model", side_effect=AssertionError("should not rebuild")):
        out = summarize_ultra_large_text(huge_text, source_hint="final.pdf", page_count=40)

    assert "已使用快取" in out


def test_ultra_final_summary_falls_back_to_deterministic_merge():
    from skills.documents.pdf_bridge import _ultra_final_summary_with_model

    notes = [
        "【頁 1-24】主題：刑事政策定義與沿革\n\n- 刑事政策有廣義與狹義兩種定義。\n- 二分政策於 2005 年引進。",
        "【頁 25-48】主題：兒童最佳利益與量刑\n\n- 兒童表意權與最佳利益相輔相成。\n- 量刑時應考量兒童最佳利益。",
    ]

    with patch("skills.documents.pdf_bridge._mr_reduce_summaries", return_value="【文件概況】\n- 可能案名/主題："), \
         patch("api.handlers.summary_handler.summarize_text_resilient", return_value={"success": True, "text": "【文件概況】\n- 可能案名/主題："}), \
         patch("skills.bridge.inference_gateway.InferenceGateway.chat", return_value={"success": False, "response": ""}), \
         patch("skills.bridge.balthasar_bridge.summarize_text", return_value={"success": False, "text": ""}):
        out = _ultra_final_summary_with_model(notes, reduce_batch=4, reduce_timeout=30, final_timeout=30)

    assert "【主題總覽】" in out
    assert "刑事政策之定義與沿革" in out
    assert "兒童最佳利益與量刑" in out


def test_ultra_final_summary_cleans_noisy_segment_notes():
    from skills.documents.pdf_bridge import _ultra_final_summary_with_model

    notes = [
        "【頁 1-24】主題：壹、刑事政策之定義與沿革\n\n【文件概況】\n- 可能案名/主題：\n  - 【可能章節】\n  - - 壹、刑事政策之定義與沿革\n\n【重點摘要】\n1. 【可能章節】 - 壹、刑事政策之定義與沿革 - 一、刑事政策之定義： - 廣義說：係指國家以預防及鎮壓犯罪為目的所為一切手段及方法。",
        "【頁 121-139】主題：(3) 過於強調責任的傾向：在犯罪控制文化下，強調個人的管理與\n\n【文件概況】\n- 可能案名/主題：\n  - 【可能章節】\n  - - （四）少年最佳利益及少年修復式司法：\n\n【重點摘要】\n1. 【可能章節】 - （四）少年最佳利益及少年修復式司法： - 認識少年的特性：由於少年的某些特性，使得現實上少年向被害人 - 道歉或進行賠償的情況不如成人頻繁。",
    ]

    out = _ultra_final_summary_with_model(notes, reduce_batch=4, reduce_timeout=30, final_timeout=30)

    assert "【主題總覽】" in out
    assert "刑事政策之定義與沿革" in out
    assert "少年最佳利益與修復式司法" in out
    assert "可能案名/主題" not in out
    assert "【可能章節】" not in out


def test_ultra_final_summary_trusts_explicit_segment_titles():
    from skills.documents.pdf_bridge import _ultra_final_summary_with_model

    notes = [
        "【頁 1-24】主題：刑事政策之定義與沿革\n- 刑事政策之定義：國家以預防及鎮壓犯罪為目的。\n- 被害人程序參與議題仍需兼顧正當程序。",
        "【頁 97-120】主題：毒品法庭與處遇爭議\n- 毒品法庭試圖從懲罰轉向治療。\n- 修復式司法後段才出現。",
    ]

    with patch.dict("os.environ", {"MAGI_PDF_ULTRA_FINAL_MODE": "deterministic"}, clear=False):
        out = _ultra_final_summary_with_model(notes, reduce_batch=4, reduce_timeout=30, final_timeout=30)

    assert "【刑事政策之定義與沿革】" in out
    assert "【毒品法庭與處遇爭議】" in out
    assert "【被害人政策與程序參與】" not in out


def test_build_structured_segment_note_cleans_seed_noise():
    from skills.documents.pdf_bridge import _build_structured_segment_note

    segment_text = (
        "--- 第 1 頁 ---\n"
        "壹、刑事政策之定義與沿革\n"
        "一、刑事政策之定義：\n"
        "廣義說：係指國家以預防及鎮壓犯罪為目的所為一切手段及方法。\n\n"
        "二、刑事政策之變革：\n"
        "2005 年法務部提出刑法修正草案，引進二分政策。"
    )

    out = _build_structured_segment_note(segment_text, label="頁 1-24", max_items=4, max_chars=800)

    assert out.startswith("【頁 1-24】主題：")
    assert "刑事政策之定義與沿革" in out
    assert "可能案名/主題" not in out
    assert "【可能章節】" not in out
    assert "2005 年法務部提出刑法修正草案" in out


def test_build_structured_segment_note_removes_trial_noise():
    from skills.documents.pdf_bridge import _build_structured_segment_note

    segment_text = (
        "--- 第 1 頁 ---\n"
        "壹、刑事政策之定義與沿革\n"
        "一、刑事政策之變革：\n"
        "2005 年法務部提出刑法修正草案，引進二分政策。\n"
        "【試題演練】\n"
        "請論述寬嚴並進政策之得失。 自擬 【擬答】"
    )

    out = _build_structured_segment_note(segment_text, label="頁 1-24", max_items=4, max_chars=800)

    assert "【試題演練】" not in out
    assert "自擬" not in out


def test_structured_segment_note_infers_thematic_title_from_source():
    from skills.documents.pdf_bridge import _build_structured_segment_note

    segment_text = (
        "--- 第 97 頁 ---\n"
        "二、毒品法庭的疑慮：\n"
        "「成癮是種疾病」影響著毒品法制，但施用毒品行為未能除罪化。\n\n"
        "三、我國毒品施用者處遇現狀：\n"
        "毒品施用者普遍具有成癮問題，於醫學上被歸類為物質使用疾患。"
    )

    out = _build_structured_segment_note(segment_text, label="頁 97-120", max_items=4, max_chars=800)

    assert "毒品法庭與處遇爭議" in out


def test_structured_segment_note_keeps_strong_heading_title():
    from skills.documents.pdf_bridge import _build_structured_segment_note

    segment_text = (
        "--- 第 1 頁 ---\n"
        "壹、刑事政策之定義與沿革\n"
        "一、刑事政策之變革：\n"
        "2005 年法務部提出刑法修正草案，引進二分政策。\n\n"
        "四、特別議題：被告子女與量刑\n"
        "量刑時應考量兒童最佳利益。"
    )

    out = _build_structured_segment_note(segment_text, label="頁 1-24", max_items=4, max_chars=800)

    assert "【頁 1-24】主題：刑事政策之定義與沿革" in out
    assert "兒童最佳利益與量刑" not in out.splitlines()[0]


def test_structured_segment_note_prefers_legal_toc_heading_over_fragment():
    from skills.documents.pdf_bridge import _build_structured_segment_note

    segment_text = (
        "--- 第 4 頁 ---\n"
        "目錄\n"
        "一、引言 26-40\n"
        "二.爭議主題 41-70\n"
        "三．第一個初步異議：屬事管轄權 71-114\n"
        "A. 「國籍」一詞是否包含目前國籍 74-105\n"
        "1. 「國籍」一詞依其規定 任何含義，請根據上下文並根據 CERD 的目標和宗旨來閱讀 78-88\n"
    )

    out = _build_structured_segment_note(segment_text, label="頁 1-26", max_items=4, max_chars=800)

    assert "【頁 1-26】主題：「national origin」是否涵蓋現行國籍" in out
    assert "官方引用格式" not in out


def test_ultra_final_summary_infers_legal_judgment_titles():
    from skills.documents.pdf_bridge import _ultra_final_summary_with_model

    notes = [
        "【頁 1-26】主題：任何含義，請根據上下文並根據 CERD 的目標和宗旨來閱讀\n- 卡達主張「national origin」包含目前國籍。\n- 第一個初步異議涉及屬事管轄權。",
        "【頁 27-43】主題：阿聯酋對卡達某些媒體公司採取的措施是否屬於《公約》範圍的問題\n- 爭點包括媒體公司措施與間接歧視是否落入《公約》範圍。",
    ]

    out = _ultra_final_summary_with_model(notes, reduce_batch=4, reduce_timeout=30, final_timeout=30)

    assert "「national origin」是否涵蓋現行國籍" in out
    assert "媒體公司措施是否屬《公約》範圍" in out or "「間接歧視」是否屬《公約》範圍" in out


def test_ultra_final_summary_long_mode_keeps_more_detail():
    from skills.documents.pdf_bridge import _ultra_final_summary_with_model

    notes = [
        "【頁 1-26】主題：第一個初步異議：屬事管轄權\n"
        "- 核心爭點之一是：CERD 所稱「national origin」依通常文義、上下文與公約目的，是否涵蓋現行國籍。\n"
        "- 阿聯酋主張法院對卡達的申請欠缺管轄權，且該申請不具可受理性。\n"
        "- 法院並於 2020 年 8 月至 9 月，就阿聯酋提出的初步異議舉行公開聽證。\n"
        "- 法院最終認為，CERD 第一條第一款所稱「national origin」不包括現行國籍。",
        "【頁 27-43】主題：「間接歧視」是否屬《公約》範圍\n"
        "- 法院審查卡達主張的「間接歧視」是否屬於 CERD 規範範圍。\n"
        "- 法院回到 CERD 第一條第一款對「種族歧視」的定義，作為判斷公約適用範圍的基礎。\n"
        "- 法院指出，媒體公司措施與間接歧視是否落入《公約》範圍，是另一個主要爭點。\n"
        "- 法院最終維持第一個初步異議，無須審查第二個初步異議。",
    ]

    out = _ultra_final_summary_with_model(
        notes,
        reduce_batch=4,
        reduce_timeout=30,
        final_timeout=30,
        summary_length="long",
    )

    assert "【主要爭點】" in out
    assert out.count("\n- ") >= 8
    assert "公開聽證" in out


def test_ultra_final_summary_crosslingual_notes_use_model_polish():
    from skills.documents.pdf_bridge import _ultra_final_summary_with_model

    notes = [
        "【頁 1-26】主題：Subject-Matter of the Dispute\n- The Court considered whether national origin includes current nationality.\n- The Court held that CERD does not cover current nationality.",
        "【頁 27-43】主題：For these reasons\n- By eleven votes to six, the Court upheld the first preliminary objection.",
    ]
    out = _ultra_final_summary_with_model(notes, reduce_batch=4, reduce_timeout=30, final_timeout=30)

    assert "法院認為《消除種族歧視公約》不涵蓋現行國籍" in out
    assert "Subject-Matter of the Dispute" not in out


def test_crosslingual_segment_note_prefers_translation_fallback():
    from skills.documents.pdf_bridge import _ultra_segment_note_with_model

    segment_text = (
        "--- 第 1 頁 ---\n"
        "Subject-Matter of the Dispute\n"
        "The Court considered whether national origin includes current nationality.\n"
        "The Court held that CERD does not cover current nationality."
    )

    with patch("skills.documents.pdf_bridge._translate_note_to_traditional_chinese", return_value="【頁 1-26】主題：第一個初步異議：屬事管轄權\n- 法院認為 CERD 不涵蓋現行國籍。"):
        out = _ultra_segment_note_with_model(
            segment_text,
            label="頁 1-26",
            total_segments=3,
            max_items=5,
            max_chars=900,
            timeout_sec=25,
            summary_length="long",
        )

    assert "第一個初步異議：屬事管轄權" in out
    assert "Subject-Matter of the Dispute" not in out


def test_ultra_large_summary_does_not_reuse_seed_cache(tmp_path):
    """Seed/fallback notes should be regenerated on rerun so quality can recover."""
    from skills.documents.pdf_bridge import summarize_ultra_large_text, _summary_checkpoint_dir

    huge_text = "\n\n".join(
        f"--- 第 {page_no} 頁 ---\n壹、刑事政策\n本頁討論刑罰目的與保安處分。"
        for page_no in range(1, 41)
    )
    env = {
        "MAGI_DOC_RUN_ROOT": str(tmp_path),
        "MAGI_PDF_ULTRA_SEGMENT_PAGES": "20",
        "MAGI_PDF_ULTRA_SEGMENT_CHARS": "12000",
    }
    with patch.dict("os.environ", env, clear=False):
        checkpoint_dir = _summary_checkpoint_dir("seed.pdf", huge_text, kind="pdf_summary")
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        (checkpoint_dir / "segment_0001.json").write_text(
            '{"index": 1, "label": "頁 1-20", "note_version": 2, "note_source": "seed", "note": "舊 seed 筆記"}',
            encoding="utf-8",
        )

    with patch.dict("os.environ", env, clear=False), \
         patch("skills.documents.pdf_bridge._ultra_segment_note_with_model", return_value="【頁 1-20】主題：重新模型整理\n- 已升級"), \
         patch("skills.documents.pdf_bridge._ultra_final_summary_with_model", return_value="【主題總覽】\n- 已更新"):
        out = summarize_ultra_large_text(huge_text, source_hint="seed.pdf", page_count=40)

    assert "已更新" in out


def test_summarize_pdf_routes_midlarge_doc_to_ultra_by_chunk_count():
    """Documents below old page/char thresholds should still use ultra path when MR chunk count is high."""
    from skills.documents.pdf_bridge import summarize_pdf

    pages = []
    for page_no in range(1, 140):
        pages.append(
            f"--- 第 {page_no} 頁 ---\n"
            "壹、刑事政策總論\n"
            "本頁重點說明刑罰目的、保安處分與矯治政策的差異。" * 8
        )
    extracted = "\n\n".join(pages)

    with patch("skills.documents.pdf_bridge.extract_text", return_value=extracted), \
         patch("skills.documents.pdf_bridge.summarize_ultra_large_text", return_value="【文件概況】\n- 可辨識頁數：約 139 頁\n\n【重點摘要】\n1. 已走分層摘要。") as mock_ultra, \
         patch("skills.documents.pdf_bridge.map_reduce_summarize", side_effect=RuntimeError("should not use MR")), \
         patch.dict("os.environ", {"MAGI_PDF_VECTOR_INGEST_ENABLE": "0"}, clear=False):
        out = summarize_pdf("/tmp/mock.pdf")

    assert "已走分層摘要" in out
    mock_ultra.assert_called_once()


def test_summarize_pdf_passes_summary_length_to_ultra_path():
    from skills.documents.pdf_bridge import summarize_pdf

    extracted = "\n\n".join(
        f"--- 第 {page_no} 頁 ---\n壹、刑事政策總論\n本頁重點說明刑罰目的、保安處分與矯治政策的差異。"
        for page_no in range(1, 140)
    )
    fake_grounded = types.ModuleType("skills.bridge.grounded_ai")
    fake_grounded.chat_casper = MagicMock(return_value={"success": False, "text": ""})

    with patch("skills.documents.pdf_bridge.extract_text", return_value=extracted), \
         patch.dict(sys.modules, {"skills.bridge.grounded_ai": fake_grounded}), \
         patch("skills.documents.pdf_bridge.summarize_ultra_large_text", return_value="【文件概況】\n- 可辨識頁數：約 139 頁") as mock_ultra, \
         patch.dict("os.environ", {"MAGI_PDF_VECTOR_INGEST_ENABLE": "0"}, clear=False):
        summarize_pdf("/tmp/mock.pdf", summary_length="long")

    assert mock_ultra.call_args.kwargs["summary_length"] == "long"
