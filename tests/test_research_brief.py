# -*- coding: utf-8 -*-
"""Unit tests for research-brief skill.

Uses importlib.util to load skill modules by explicit file path — avoids
collisions with other `action.py` / `fetchers.py` / `digest.py` siblings
from other skills that may appear on sys.path during full-suite runs.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_MAGI_ROOT = Path(__file__).resolve().parents[1]
_SKILL_DIR = _MAGI_ROOT / "skills" / "research-brief"
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))


def _load_skill_module(module_name: str, file_name: str):
    """Load a skill-local module under a unique qualified name."""
    unique_name = f"_rb_test_{module_name}"
    if unique_name in sys.modules:
        return sys.modules[unique_name]
    path = _SKILL_DIR / file_name
    spec = importlib.util.spec_from_file_location(unique_name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = mod
    # Expose local siblings too for the case where action.py does
    # `from fetchers import X` — register them under simple names
    # just for the duration of this test file.
    if str(_SKILL_DIR) not in sys.path:
        sys.path.insert(0, str(_SKILL_DIR))
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def rb_action(tmp_path):
    """Fresh isolated action module with patched runtime dir."""
    mod = _load_skill_module("action", "action.py")
    rt = tmp_path / "research_brief"
    rt.mkdir()
    mod._RUNTIME_DIR = rt
    mod._NS_DIR = rt / "namespaces"
    mod._SEEN_PATH = rt / "seen.json"
    mod._LAST_DIGEST_PATH = rt / "last_digest.jsonl"
    yield mod


@pytest.fixture
def rb_fetchers():
    return _load_skill_module("fetchers", "fetchers.py")


@pytest.fixture
def rb_translator():
    return _load_skill_module("translator_bridge", "translator_bridge.py")


@pytest.fixture
def rb_digest():
    return _load_skill_module("digest", "digest.py")


# ───────── self_test + bootstrap ─────────

def test_self_test_succeeds(rb_action):
    assert rb_action.task_self_test() == 0


def test_seeds_bootstrap_creates_5_namespaces(rb_action):
    rb_action._ensure_seeds_bootstrapped()
    names = rb_action._list_namespaces()
    assert len(names) >= 5
    for required in ("通譯", "人權公約", "族群人類學", "語言政策", "東亞法學與語言"):
        assert required in names


# ───────── CRUD ─────────

def test_add_remove_namespace_cycle(rb_action):
    assert rb_action.task_add_namespace("測試空間") == 0
    ns = rb_action._load_namespace("測試空間")
    assert ns is not None
    assert ns["namespace"] == "測試空間"
    assert rb_action.task_remove_namespace("測試空間") == 0
    assert rb_action._load_namespace("測試空間") is None


def test_add_source_then_remove(rb_action):
    rb_action.task_add_namespace("T1")
    assert rb_action.task_add_source("T1", "https://example.com/feed", stype="rss",
                                     lang="en", note="test") == 0
    ns = rb_action._load_namespace("T1")
    assert len(ns["sources"]) == 1
    # Duplicate → fail
    assert rb_action.task_add_source("T1", "https://example.com/feed") == 1
    # Remove
    assert rb_action.task_remove_source("T1", "https://example.com/feed") == 0
    assert rb_action._load_namespace("T1")["sources"] == []


def test_remove_non_empty_namespace_refused(rb_action):
    rb_action.task_add_namespace("T2")
    rb_action.task_add_source("T2", "https://example.com/x", stype="rss")
    assert rb_action.task_remove_namespace("T2") == 1  # refused


def test_keyword_add_remove(rb_action):
    rb_action.task_add_namespace("T3")
    assert rb_action.task_add_keyword("T3", "human rights") == 0
    assert "human rights" in rb_action._load_namespace("T3")["keywords"]
    assert rb_action.task_remove_keyword("T3", "human rights") == 0
    # Removing non-existent → error
    assert rb_action.task_remove_keyword("T3", "nonexistent") == 1


# ───────── keyword filter ─────────

def test_keyword_filter_passes_empty_pool(rb_action):
    assert rb_action._match_keywords("anything", [])


def test_keyword_filter_matches_zh_and_en(rb_action):
    text = "人權公約 concluding observations on ICCPR"
    assert rb_action._match_keywords(text, ["ICCPR"])
    assert rb_action._match_keywords(text, ["人權公約"])
    assert not rb_action._match_keywords(text, ["勞基法", "contract"])


# ───────── hashing + dedupe ─────────

def test_hash_url_deterministic(rb_action):
    assert rb_action._hash_url("https://ex.com/x") == rb_action._hash_url("https://ex.com/x")
    assert rb_action._hash_url("https://ex.com/x") != rb_action._hash_url("https://ex.com/y")


def test_hash_content_distinguishes_updates(rb_action):
    h1 = rb_action._hash_content("Title A", "Snippet body")
    h2 = rb_action._hash_content("Title A", "Snippet body")
    h3 = rb_action._hash_content("Title A", "Snippet UPDATED body")
    assert h1 == h2
    assert h1 != h3


def test_seen_helpers_extract_ts_and_content(rb_action):
    new_entry = {"ts": 123.4, "content": "abc", "url": "https://x/"}
    legacy_entry = 100.0
    assert rb_action._seen_timestamp(new_entry) == 123.4
    assert rb_action._seen_content_hash(new_entry) == "abc"
    assert rb_action._seen_timestamp(legacy_entry) == 100.0
    assert rb_action._seen_content_hash(legacy_entry) == ""


def test_prune_seen_drops_old_entries(rb_action):
    import time
    seen = {
        "old_hash": {"ts": time.time() - (100 * 86400), "content": "abc", "url": "x"},
        "new_hash": {"ts": time.time() - (1 * 86400), "content": "def", "url": "y"},
    }
    pruned = rb_action._prune_seen(seen)
    assert "new_hash" in pruned
    assert "old_hash" not in pruned


def test_prune_seen_legacy_format_still_works(rb_action):
    import time
    seen = {
        "old_hash": time.time() - (100 * 86400),
        "new_hash": time.time() - (1 * 86400),
    }
    pruned = rb_action._prune_seen(seen)
    assert "new_hash" in pruned
    assert "old_hash" not in pruned


def test_fetch_namespace_skips_unchanged_content(rb_action, monkeypatch):
    """URL seen + content unchanged → should skip."""
    def fake_fetch_source(src):
        return [{"title": "A", "url": "https://a/1", "snippet": "same body",
                 "raw": "same body", "lang_hint": "en"}]
    # action.py does `from fetchers import fetch_source` locally, so we
    # need to patch the fetchers module exported under that name
    fetchers_mod = _load_skill_module("fetchers", "fetchers.py")
    monkeypatch.setattr(fetchers_mod, "fetch_source", fake_fetch_source)
    # Also inject into sys.modules under plain "fetchers" so action.py's
    # local import resolves to the same module
    sys.modules["fetchers"] = fetchers_mod
    ns = {"sources": [{"url": "https://a/1", "type": "rss"}], "keywords": []}
    collected = rb_action._fetch_namespace(ns, seen={})
    assert len(collected) == 1
    seen = {collected[0]["_hash"]: {"ts": 1000.0,
                                     "content": collected[0]["_content_hash"],
                                     "url": "https://a/1"}}
    assert rb_action._fetch_namespace(ns, seen=seen) == []


def test_fetch_namespace_re_notifies_on_content_change(rb_action, monkeypatch):
    def fake_fetch_source(src):
        return [{"title": "A", "url": "https://a/1", "snippet": "UPDATED body",
                 "raw": "UPDATED body", "lang_hint": "en"}]
    fetchers_mod = _load_skill_module("fetchers", "fetchers.py")
    monkeypatch.setattr(fetchers_mod, "fetch_source", fake_fetch_source)
    sys.modules["fetchers"] = fetchers_mod
    ns = {"sources": [{"url": "https://a/1", "type": "rss"}], "keywords": []}
    url_hash = rb_action._hash_url("https://a/1")
    old_content_hash = rb_action._hash_content("A", "old body")
    seen = {url_hash: {"ts": 1000.0, "content": old_content_hash, "url": "https://a/1"}}
    collected = rb_action._fetch_namespace(ns, seen=seen)
    assert len(collected) == 1
    assert collected[0].get("_content_changed") is True


# ───────── memory ingestion ─────────

def test_memory_ingestion_silent_when_bridge_unavailable(rb_action, monkeypatch):
    # Replace with a stub that raises on attribute access for remember_batch
    import types
    stub = types.ModuleType("skills.memory.mem_bridge")
    # Intentionally do NOT set remember_batch → AttributeError when imported
    monkeypatch.setitem(sys.modules, "skills.memory.mem_bridge", stub)
    entries = [{"title": "T", "raw": "body", "url": "https://u/", "source_name": "src"}]
    # Should not raise
    count = rb_action._ingest_entries_to_memory("TestNS", entries)
    assert count == 0


def test_memory_ingestion_calls_remember_batch(rb_action, monkeypatch):
    import types
    captured: list = []

    def fake_remember_batch(payloads):
        captured.extend(payloads)
        return len(payloads)

    stub = types.ModuleType("skills.memory.mem_bridge")
    stub.remember_batch = fake_remember_batch
    monkeypatch.setitem(sys.modules, "skills.memory.mem_bridge", stub)

    entries = [
        {"title": "T1", "raw": "body1", "url": "https://a/", "source_name": "src1",
         "lang_hint": "en", "published": "2026-04-21"},
        {"title": "T2", "raw": "body2", "url": "https://b/", "source_name": "src2",
         "lang_hint": "ja"},
    ]
    count = rb_action._ingest_entries_to_memory("人權公約", entries)
    assert count == 2
    assert len(captured) == 2
    assert captured[0]["metadata"]["namespace"] == "人權公約"
    assert captured[0]["source"] == "research-brief:人權公約"
    assert captured[0]["metadata"]["url"] == "https://a/"
    assert captured[0]["metadata"]["lang_hint"] == "en"
    assert "T1" in captured[0]["content"]
    assert "body1" in captured[0]["content"]


# ───────── translator ─────────

def test_translator_passthrough_for_zh(rb_translator):
    r = rb_translator.translate_to_zh_hant("這是繁體中文", source_lang="zh-Hant")
    assert r["provider"] == "passthrough"
    assert r["text"] == "這是繁體中文"
    assert r["degraded"] is False


def test_translator_detect_japanese(rb_translator):
    assert rb_translator._detect_lang("これは日本語のテストです") == "ja"


def test_translator_detect_korean(rb_translator):
    assert rb_translator._detect_lang("이것은 한국어 테스트입니다") == "ko"


def test_translator_detect_german(rb_translator):
    assert rb_translator._detect_lang("Über die Ähnlichkeit von Sprache und Recht") == "de"


def test_translator_normalize_lang(rb_translator):
    assert rb_translator._normalize_lang("zh-TW") == "zh-Hant"
    assert rb_translator._normalize_lang("zh_tw") == "zh-Hant"
    assert rb_translator._normalize_lang("en") == "en"
    assert rb_translator._normalize_lang("") == ""


# ───────── digest ─────────

def test_digest_format_empty(rb_digest):
    out = rb_digest.format_digest("測試", [], keyword_pool=["x"])
    assert "今日無新文獻" in out


def test_digest_extract_tags(rb_digest):
    tags = rb_digest._extract_tags(
        "This article on human rights and ICCPR obligations",
        ["ICCPR", "human rights", "irrelevant"],
        limit=2,
    )
    assert "ICCPR" in tags
    assert "human rights" in tags
    assert len(tags) == 2


def test_digest_truncate_zh(rb_digest):
    long_text = "這是一段很長的中文文字" * 20
    out = rb_digest._truncate_zh(long_text, 30)
    assert len(out) <= 31
    assert out.endswith("…")


def test_digest_hostname_extract(rb_digest):
    assert rb_digest._hostname("https://www.example.com/path") == "example.com"
    assert rb_digest._hostname("https://sub.judicial.gov.tw/x") == "sub.judicial.gov.tw"


# ───────── fetchers ─────────

def test_fetch_rss_parses_valid_xml(rb_fetchers, monkeypatch):
    sample = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Test</title>
  <item>
    <title>Article 1</title>
    <link>https://ex.com/1</link>
    <description>Summary of article 1</description>
    <pubDate>Mon, 20 Apr 2026 10:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Article 2</title>
    <link>https://ex.com/2</link>
    <description>Another summary</description>
  </item>
</channel></rss>"""
    monkeypatch.setattr(rb_fetchers, "_http_get", lambda u, timeout=15: sample)
    items = rb_fetchers.fetch_rss("https://ex.com/feed", lang_hint="en")
    assert len(items) == 2
    assert items[0]["title"] == "Article 1"
    assert items[0]["url"] == "https://ex.com/1"
    assert items[0]["lang_hint"] == "en"


def test_fetch_rss_handles_atom(rb_fetchers, monkeypatch):
    sample = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test Atom</title>
  <entry>
    <title>Atom Entry 1</title>
    <link href="https://atom.example/1"/>
    <summary>Atom summary</summary>
    <updated>2026-04-20T10:00:00Z</updated>
  </entry>
</feed>"""
    monkeypatch.setattr(rb_fetchers, "_http_get", lambda u, timeout=15: sample)
    items = rb_fetchers.fetch_rss("https://atom.example/feed", lang_hint="en")
    assert len(items) == 1
    assert items[0]["title"] == "Atom Entry 1"


def test_fetch_rss_returns_empty_on_bad_xml(rb_fetchers, monkeypatch):
    monkeypatch.setattr(rb_fetchers, "_http_get", lambda u, timeout=15: "not xml")
    assert rb_fetchers.fetch_rss("https://bad.example/feed") == []


def test_strip_html_removes_tags(rb_fetchers):
    s = rb_fetchers._strip_html("<p>Hello <b>world</b></p><script>alert('x')</script>")
    assert "Hello" in s
    assert "world" in s
    assert "<" not in s
    assert "alert" not in s


def test_fetch_source_dispatches_by_type(rb_fetchers, monkeypatch):
    called: list = []

    def fake_rss(url, **kw):
        called.append(("rss", url))
        return [{"title": "x", "url": url}]

    def fake_html(url, **kw):
        called.append(("html", url))
        return [{"title": "y", "url": url}]

    monkeypatch.setattr(rb_fetchers, "fetch_rss", fake_rss)
    monkeypatch.setattr(rb_fetchers, "fetch_html", fake_html)
    rb_fetchers.fetch_source({"url": "https://a/", "type": "rss"})
    rb_fetchers.fetch_source({"url": "https://b/", "type": "html"})
    assert ("rss", "https://a/") in called
    assert ("html", "https://b/") in called


# ───────── fetch_html article extraction ─────────

def test_fetch_html_autodiscovers_rss(rb_fetchers, monkeypatch):
    """If page contains <link rel=alternate type=rss+xml>, delegate to fetch_rss."""
    html_with_rss = """<html><head>
      <link rel="alternate" type="application/rss+xml" href="/feed.xml">
    </head><body><a href="/news/1">Some Article Title Here</a></body></html>"""
    rss_items = [{"title": "RSS Article", "url": "https://ex.com/news/rss-1",
                  "snippet": "snippet", "raw": "raw", "lang_hint": "en", "published": ""}]
    monkeypatch.setattr(rb_fetchers, "_http_get", lambda u, timeout=15: html_with_rss)
    monkeypatch.setattr(rb_fetchers, "fetch_rss", lambda u, **kw: rss_items)
    items = rb_fetchers.fetch_html("https://ex.com/news/", lang_hint="en")
    assert len(items) == 1
    assert items[0]["title"] == "RSS Article"


def test_fetch_html_extracts_article_links(rb_fetchers, monkeypatch):
    """fetch_html should extract individual article links, not return the homepage."""
    html = """<html><head><title>News Site</title></head>
    <body>
      <nav><a href="/">首頁</a><a href="/about">About</a></nav>
      <main>
        <a href="/news/2026/indigenous-rights-report">Indigenous Rights Annual Report 2026</a>
        <a href="/news/2026/iccpr-concluding-observations">ICCPR Concluding Observations Released</a>
        <a href="/news/2026/un-treaty-body-session">UN Treaty Body Session Outcomes</a>
      </main>
      <footer><a href="/contact">Contact Us</a></footer>
    </body></html>"""
    monkeypatch.setattr(rb_fetchers, "_http_get", lambda u, timeout=15: html)
    items = rb_fetchers.fetch_html("https://nhrc.cy.gov.tw/news/", lang_hint="zh")
    assert len(items) >= 3
    titles = [i["title"] for i in items]
    assert any("Indigenous Rights" in t for t in titles)
    assert any("ICCPR" in t for t in titles)
    # Navigation links should be excluded
    assert not any(t in ("首頁", "About", "Contact Us") for t in titles)


def test_fetch_html_filters_short_titles(rb_fetchers, monkeypatch):
    """Links with very short text (nav items) should be skipped."""
    html = """<html><head><title>Site</title></head><body>
      <a href="/page/very-important-human-rights-article-title">Important Article on Human Rights</a>
      <a href="/short">OK</a>
      <a href="/x">X</a>
    </body></html>"""
    monkeypatch.setattr(rb_fetchers, "_http_get", lambda u, timeout=15: html)
    items = rb_fetchers.fetch_html("https://ex.com/listing/", lang_hint="en")
    assert len(items) == 1
    assert "Important Article" in items[0]["title"]


def test_fetch_html_excludes_cross_domain_links(rb_fetchers, monkeypatch):
    """Links pointing to external domains should not be included."""
    html = """<html><head><title>Site</title></head><body>
      <a href="/local/article-title-long-enough-to-pass">Local Article That Is Long Enough</a>
      <a href="https://external.org/some/different/article/path">External Article Link Here</a>
    </body></html>"""
    monkeypatch.setattr(rb_fetchers, "_http_get", lambda u, timeout=15: html)
    items = rb_fetchers.fetch_html("https://ex.com/", lang_hint="en")
    assert all("ex.com" in i["url"] or i["url"].startswith("/") for i in items)
    assert not any("external.org" in i["url"] for i in items)


def test_fetch_html_falls_back_to_page_when_no_links(rb_fetchers, monkeypatch):
    """If no article links found, fall back to returning the page itself."""
    html = """<html><head><title>Single Page</title></head>
    <body><p>This page has no article links, just plain text content here.</p></body></html>"""
    monkeypatch.setattr(rb_fetchers, "_http_get", lambda u, timeout=15: html)
    items = rb_fetchers.fetch_html("https://ex.com/", lang_hint="zh")
    assert len(items) == 1
    assert items[0]["url"] == "https://ex.com/"
