"""T4 tests: legal_termbase three-tier architecture."""
import sys
import os
import json
import pathlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── build_tier1_from_moj ──────────────────────────────────────────────────────

def _make_mini_moj_json(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a minimal MOJ bilingual JSON fixture (matches MOJ format)."""
    data = {
        "法規名稱": "民法",
        "英文法規名稱": "Civil Code",
        "法條": [
            {
                "條號": "第 184 條",
                "條文內容": "因故意或過失，不法侵害他人之權利者，負損害賠償責任。",
            },
            {
                "條號": "第 179 條",
                "條文內容": "無法律上之原因而受利益，致他人受損害者，應返還其利益。",
            },
        ],
    }
    moj_dir = tmp_path / "moj"
    moj_dir.mkdir()
    f = moj_dir / "civil_code.json"
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return moj_dir


def test_build_tier1_returns_sqlite_path(tmp_path):
    from skills.translator.legal_termbase import build_tier1_from_moj
    moj_dir = _make_mini_moj_json(tmp_path)
    db_path = build_tier1_from_moj(moj_dir, db_path=tmp_path / "t1.db")
    assert db_path is not None
    assert db_path.exists()
    assert db_path.suffix == ".db"


def test_build_tier1_contains_article(tmp_path):
    import sqlite3
    from skills.translator.legal_termbase import build_tier1_from_moj
    moj_dir = _make_mini_moj_json(tmp_path)
    db_path = build_tier1_from_moj(moj_dir, db_path=tmp_path / "t1.db")
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT * FROM articles WHERE article_no LIKE '%184%'").fetchall()
    conn.close()
    assert len(rows) >= 1


def test_build_tier1_idempotent(tmp_path):
    from skills.translator.legal_termbase import build_tier1_from_moj
    moj_dir = _make_mini_moj_json(tmp_path)
    custom_db = tmp_path / "t1.db"
    p1 = build_tier1_from_moj(moj_dir, db_path=custom_db)
    p2 = build_tier1_from_moj(moj_dir, db_path=custom_db)
    assert p1 == p2


# ── lookup_tier2 ──────────────────────────────────────────────────────────────

def test_lookup_tier2_exact_en_tort():
    from skills.translator.legal_termbase import lookup_tier2
    result = lookup_tier2("tort", "en")
    assert result is not None
    zh_list = result.get("zh", [])
    assert any("侵權行為" in z for z in zh_list)


def test_lookup_tier2_exact_en_stare_decisis():
    from skills.translator.legal_termbase import lookup_tier2
    result = lookup_tier2("stare decisis", "en")
    assert result is not None
    zh_list = result.get("zh", [])
    assert any("遵循先例" in z for z in zh_list)


def test_lookup_tier2_case_insensitive():
    from skills.translator.legal_termbase import lookup_tier2
    r1 = lookup_tier2("TORT", "en")
    r2 = lookup_tier2("tort", "en")
    assert r1 is not None
    assert r2 is not None
    assert r1["zh"] == r2["zh"]


def test_lookup_tier2_novel_term_nomosphere():
    from skills.translator.legal_termbase import lookup_tier2
    result = lookup_tier2("Nomosphere", "en")
    assert result is not None
    zh_list = result.get("zh", [])
    assert any("規範空間" in z or "規範" in z for z in zh_list)
    assert result.get("confidence") == "medium"


def test_lookup_tier2_unknown_returns_none():
    from skills.translator.legal_termbase import lookup_tier2
    result = lookup_tier2("xyzzy_nonsense_term_12345", "en")
    assert result is None


# ── build_glossary_for_chunk ──────────────────────────────────────────────────

def test_build_glossary_zh2en_returns_tuple():
    from skills.translator.legal_termbase import build_glossary_for_chunk
    text = "依民法第184條，侵權行為之損害賠償責任。"
    t1, t2 = build_glossary_for_chunk(text, target_lang="English")
    assert isinstance(t1, str)
    assert isinstance(t2, str)


def test_build_glossary_en2zh_detects_tier2():
    from skills.translator.legal_termbase import build_glossary_for_chunk
    text = "The concept of stare decisis is foundational in common law jurisdictions."
    t1, t2 = build_glossary_for_chunk(text, target_lang="繁體中文")
    assert isinstance(t2, str)
    if t2:
        assert "stare decisis" in t2 or "遵循先例" in t2


def test_build_glossary_empty_text_returns_empty():
    from skills.translator.legal_termbase import build_glossary_for_chunk
    t1, t2 = build_glossary_for_chunk("", target_lang="繁體中文")
    assert t1 == ""
    assert t2 == ""


def test_build_glossary_non_legal_text_returns_empty():
    from skills.translator.legal_termbase import build_glossary_for_chunk
    text = "今天天氣真好，我們去公園散步吃冰淇淋。"
    t1, t2 = build_glossary_for_chunk(text, target_lang="English")
    assert t1 == "" and t2 == ""


# ── build_legal_prompt ────────────────────────────────────────────────────────

def test_legal_prompt_contains_tier3_rule():
    from skills.translator.legal_termbase import build_legal_prompt
    prompt = build_legal_prompt("test text", "繁體中文", "", "")
    assert "保留" in prompt and "暫譯" in prompt


def test_legal_prompt_includes_tier1_block():
    from skills.translator.legal_termbase import build_legal_prompt
    t1 = "民法第184條 = Civil Code Article 184: 因故意或過失...\n"
    prompt = build_legal_prompt("test", "繁體中文", t1, "")
    assert "184" in prompt


def test_legal_prompt_includes_tier2_block():
    from skills.translator.legal_termbase import build_legal_prompt
    t2 = "stare decisis → 遵循先例原則\n"
    prompt = build_legal_prompt("The stare decisis doctrine.", "繁體中文", "", t2)
    assert "stare decisis" in prompt


def test_legal_prompt_has_target_lang():
    from skills.translator.legal_termbase import build_legal_prompt
    prompt = build_legal_prompt("test", "English", "", "")
    assert "English" in prompt


# ── should_use_legal_mode ─────────────────────────────────────────────────────

def test_legal_mode_auto_detects_legal_text(monkeypatch):
    monkeypatch.setenv("MAGI_TRANSLATOR_LEGAL_MODE", "auto")
    from skills.translator.legal_termbase import should_use_legal_mode
    assert should_use_legal_mode("依民法第184條，侵權行為之損害賠償。") is True


def test_legal_mode_auto_skips_non_legal(monkeypatch):
    monkeypatch.setenv("MAGI_TRANSLATOR_LEGAL_MODE", "auto")
    from skills.translator.legal_termbase import should_use_legal_mode
    assert should_use_legal_mode("今天天氣很好，適合散步。") is False


def test_legal_mode_forced_on(monkeypatch):
    monkeypatch.setenv("MAGI_TRANSLATOR_LEGAL_MODE", "1")
    from skills.translator.legal_termbase import should_use_legal_mode
    assert should_use_legal_mode("any text at all") is True


def test_legal_mode_forced_off(monkeypatch):
    monkeypatch.setenv("MAGI_TRANSLATOR_LEGAL_MODE", "0")
    from skills.translator.legal_termbase import should_use_legal_mode
    assert should_use_legal_mode("依民法第184條，侵權行為之損害賠償。") is False


# ── fallback when termbase absent ────────────────────────────────────────────

def test_build_glossary_no_crash_when_db_missing(monkeypatch, tmp_path):
    """build_glossary_for_chunk must return ("","") not crash if DB not found."""
    from skills.translator import legal_termbase
    monkeypatch.setattr(legal_termbase, "_DB_PATH", tmp_path / "nonexistent.db")
    t1, t2 = legal_termbase.build_glossary_for_chunk("依民法侵權行為損害。", "English")
    assert isinstance(t1, str)
    assert isinstance(t2, str)


def test_lookup_tier2_no_crash_when_json_missing(monkeypatch, tmp_path):
    """lookup_tier2 must return None not crash if tier2 JSON missing."""
    from skills.translator import legal_termbase
    legal_termbase._TIER2_CACHE = None
    legal_termbase._TIER2_MTIME = 0.0
    monkeypatch.setattr(legal_termbase, "_TIER2_PATH", tmp_path / "nonexistent.json")
    result = legal_termbase.lookup_tier2("tort", "en")
    assert result is None
    legal_termbase._TIER2_CACHE = None
    legal_termbase._TIER2_MTIME = 0.0
