from __future__ import annotations

from skills.research import web_research


def test_fetch_url_content_uses_scrapling_adapter(monkeypatch):
    monkeypatch.setattr(web_research, "_internet_guard", lambda *args, **kwargs: (True, ""))

    def _fake_fetch(url, timeout=20):
        return {
            "use_fallback": False,
            "success": True,
            "url": url,
            "title": "Threads title",
            "content": "這是 Scrapling 抓回來的實測內容",
        }

    monkeypatch.setattr("skills.engine.scraping_adapter.fetch_page", _fake_fetch)

    result = web_research.fetch_url_content("https://www.threads.com/@lawchat.tw/post/example")

    assert result["success"] is True
    assert result["title"] == "Threads title"
    assert result["engine"] == "scrapling"
    assert "實測內容" in result["content"]


def test_fetch_url_sections_uses_scrapling_html(monkeypatch):
    monkeypatch.setattr(web_research, "_internet_guard", lambda *args, **kwargs: (True, ""))

    def _fake_fetch(url, timeout=25):
        return {
            "use_fallback": False,
            "success": True,
            "url": url,
            "title": "Example",
            "html": """
            <html><head><title>Example</title></head>
            <body>
              <div id="tabberpost">
                <ul class="tabs">
                  <li><a href="#sec1">第一段</a></li>
                </ul>
              </div>
              <div id="sec1">條文內容 A</div>
            </body></html>
            """,
        }

    monkeypatch.setattr("skills.engine.scraping_adapter.fetch_page", _fake_fetch)

    result = web_research.fetch_url_sections("https://example.com")

    assert result["success"] is True
    assert result["engine"] == "scrapling"
    assert result["sections"][0]["title"] == "第一段"
    assert "條文內容 A" in result["sections"][0]["content"]
