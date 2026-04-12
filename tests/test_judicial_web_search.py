from __future__ import annotations

import importlib.util
from pathlib import Path


_MODULE_PATH = Path("/Users/ai/Desktop/MAGI_v2/skills/judicial-web-search/action.py")
_SPEC = importlib.util.spec_from_file_location("test_judicial_web_search_action", _MODULE_PATH)
judicial_web_search = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(judicial_web_search)


def test_parse_result_items_extracts_titles_and_urls():
    html = """
    <html><body>
      <a href="data.aspx?ty=JD&id=AAA">臺灣臺北地方法院 115 年度 訴 字第 1 號民事判決</a>
      <a href="data.aspx?ty=JD&id=AAA">臺灣臺北地方法院 115 年度 訴 字第 1 號民事判決</a>
      <a href="data.aspx?ty=JD&id=BBB">最高法院 114 年度 台上 字第 1 號民事判決</a>
    </body></html>
    """
    items = judicial_web_search._parse_result_items(html, "https://judgment.judicial.gov.tw/FJUD/qryresultlst.aspx")
    assert len(items) == 2
    assert items[0]["url"].startswith("https://judgment.judicial.gov.tw/FJUD/data.aspx")


def test_next_page_href_returns_absolute_url():
    html = """
    <html><body>
      <a href="/FJUD/qryresultlst.aspx?q=token&page=2">下一頁</a>
    </body></html>
    """
    href = judicial_web_search._next_page_href(html, "https://judgment.judicial.gov.tw/FJUD/qryresultlst.aspx?q=token&page=1")
    assert href == "https://judgment.judicial.gov.tw/FJUD/qryresultlst.aspx?q=token&page=2"
