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


def test_http_court_values_accepts_label_and_code():
    soup = judicial_web_search.BeautifulSoup(
        """
        <select id="jud_court">
          <option value="">所有法院</option>
          <option value="TPS">最高法院</option>
        </select>
        """,
        "html.parser",
    )
    assert judicial_web_search._http_court_values(soup, ["最高法院"]) == ["TPS"]
    assert judicial_web_search._http_court_values(soup, ["TPS"]) == ["TPS"]


def test_normalize_judicial_text_removes_page_chrome_and_joins_lines():
    raw = """
    去格式引用
    分享網址
    裁判字號：
    最高法院 115 年度台抗字第 540 號刑事裁定
    裁判日期：
    民國 115 年 04 月 23 日
    裁判案由：
    偽造有價證券聲請再審及停止刑罰執行
    最高法院刑事
    裁定
    115年度台抗字第540號
    主  文
    抗告駁回。
    理  由
    一、
    按
    原判決所憑之
    證言
    、
    鑑定
    或
    通譯
    已證明其為虛偽者，得聲請再審。
    歷審裁判
    列印歷審清單
    相關法條
    """
    text = judicial_web_search._normalize_judicial_text(raw)
    assert "去格式引用" not in text
    assert "分享網址" not in text
    assert "歷審裁判" not in text
    assert "裁判字號：最高法院 115 年度台抗字第 540 號刑事裁定" in text
    assert "主文" in text
    assert "理由" in text
    assert "一、按原判決所憑之證言、鑑定或通譯已證明其為虛偽者，得聲請再審。" in text
