from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_desktop_home_prioritises_workflows_over_engineering_controls():
    page = _read("templates/golem_console.html")

    assert 'aria-label="MAGI 主要導覽"' in page
    assert 'href="/golem" aria-current="page">首頁</a>' in page
    assert 'href="/osc">案件與文件</a>' in page
    assert 'href="/sentencing-trends">判決趨勢</a>' in page
    assert 'href="/status">系統檢測</a>' in page
    assert 'href="/manual" target="_blank" rel="noopener">維修百科</a>' in page
    assert 'class="nav-more"' in page
    assert 'href="/osc?tab=todos"' in page
    assert 'href="https://calendar.google.com/calendar/u/0/r" target="_blank" rel="noopener noreferrer"' in page
    assert "工具與外部資源" in page
    assert '<details class="legal-links-panel' in page
    assert 'data-tool-category="public-tools"' in page
    assert 'data-tool-category="legal-work"' in page
    assert 'data-tool-category="research-admin"' in page
    assert 'href="/tools">全部公開工具</a>' in page
    assert 'href="/video-studio">影片工作室</a>' in page
    assert page.index('href="/video-studio"') < page.index('href="https://portal.ezlawyer.com.tw/"')
    public = page.split('data-tool-category="public-tools"', 1)[1].split("</section>", 1)[0]
    for href in ("/tools", "/video-studio", "/cookie-cutter", "/lottery", "/exam-tutor"):
        assert f'href="{href}"' in public
    assert "portal.ezlawyer.com.tw" not in public


def test_research_and_sentencing_share_the_same_primary_navigation():
    research = _read("templates/research.html")
    sentencing = _read("templates/sentencing_trends.html")

    for page in (research, sentencing):
        assert 'href="/golem">首頁</a>' in page
        assert 'href="/osc">案件與文件</a>' in page
        assert 'href="/sentencing-trends"' in page
        assert 'href="/research"' in page
        assert 'href="/status">系統檢測</a>' in page
        assert 'href="/manual" target="_blank" rel="noopener">維修百科</a>' in page
        assert 'href="/mobile">手機</a>' in page
        assert 'class="skip-link"' in page
    assert 'href="/research" aria-current="page"' in research
    assert 'href="/sentencing-trends" aria-current="page"' in sentencing


def test_maintenance_manual_is_self_contained_and_uses_shared_theme_contract():
    # Verify the immutable asset that is actually bundled and served by
    # /manual.  The authoring copy under docs/ is useful to repository readers
    # but is intentionally outside the production release allowlist.
    page = _read("magi_v3/manual_assets/MAGI_V3_維修百科全書_rc641.html")

    assert '<html lang="zh-Hant" data-magi-theme="cyber">' in page
    assert 'localStorage.getItem("magi.ui.theme.v1")' in page
    assert 'localStorage.setItem("magi.ui.theme.v1", theme)' in page
    assert 'id="themeToggleBtn"' in page
    assert 'id="manual-search"' in page
    assert 'id="manual-toc"' in page
    assert 'href="/manual/pdf"' in page
    assert 'href="/manual/source-index.json"' in page
    assert "__MANUAL_BODY__" not in page


def test_maintenance_manual_tables_wrap_without_hiding_cells_on_narrow_screens():
    page = _read("magi_v3/manual_assets/MAGI_V3_維修百科全書_rc641.html")

    assert "table-layout: fixed" in page
    assert "overflow-wrap: anywhere" in page
    assert "word-break: break-word" in page
    assert "white-space: normal" in page
    assert "th, td { min-width: 120px" not in page
    assert "table { display: block" not in page


def test_mobile_home_exposes_direct_work_routes_without_desktop_detour():
    page = _read("templates/mobile_home.html")
    script = _read("static/mobile/mobile.js")

    for href in (
        "/osc",
        "/osc?tab=todos",
        "https://calendar.google.com/calendar/u/0/r",
        "/osc?tab=fileManager",
        "/sentencing-trends",
        "/research",
        "/status",
    ):
        assert f'href="{href}"' in page
    assert "待辦期限" in page
    assert "量刑趨勢" in page
    assert "依工作目的分類" in page
    assert "friendlyFailureMessage" in script
    assert "data.error ||" not in script


def test_paperclip_deep_links_are_allowlisted_and_shareable():
    page = _read("templates/osc.html")
    script = _read("static/osc/osc-events.js")

    assert "案件與文件 | MAGI" in page
    assert 'document.querySelectorAll(".tab-btn[data-tab]")' in script
    assert 'new URLSearchParams(window.location.search).get("tab")' in script
    assert "button.dataset.tab === candidate" in script
    assert 'url.searchParams.set("tab", tabId)' in script
    assert "jumpToPaperclipTab(requestedTab)" in script
    assert "CSS.escape(candidate)" not in script


def test_mobile_and_desktop_navigation_remain_adaptive_and_keyboard_accessible():
    site_css = _read("static/magi-site.css")
    console_css = _read("static/golem/golem-console.css")
    mobile_css = _read("static/mobile/mobile.css")

    assert ".skip-link:focus" in console_css
    assert '.topnav a[aria-current="page"]' in console_css
    assert "overflow-x: auto" in console_css
    assert ".site-console .topnav::-webkit-scrollbar" in site_css
    assert "@media (max-width: 410px)" in mobile_css
    assert "@media (min-width: 760px)" in mobile_css
    assert "repeat(3, minmax(0, 1fr))" in mobile_css


def test_sentencing_form_is_single_column_and_width_bounded_on_phones():
    page = _read("templates/sentencing_trends.html")

    assert ".trend-form input,.trend-form select{width:100%;min-width:0" in page
    assert ".trend-form label{display:grid;min-width:0" in page
    assert "@media(max-width:600px){.trend-shell{padding:12px}.trend-form{grid-template-columns:1fr}" in page


def test_sentencing_reuses_court_directory_and_explains_judge_order():
    page = _read("templates/sentencing_trends.html")

    assert "fetch('/api/osc/courts?limit=2000'" in page
    assert 'list="trend-court-options"' in page
    assert "末位列名法官（建議）" in page
    assert "參與判決法官" in page
    assert "依裁判簽署區順序" in page
