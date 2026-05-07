from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_laf_event_cards_render_detailed_time_lines():
    js = (ROOT / "static" / "osc" / "tabs" / "documents.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "osc" / "osc-components.css").read_text(encoding="utf-8")
    html = (ROOT / "templates" / "osc.html").read_text(encoding="utf-8")
    events_js = (ROOT / "static" / "osc" / "osc-events.js").read_text(encoding="utf-8")

    assert "function renderLafEventLines" in js
    assert "function openLafEventDetailDialog" in js
    assert "data.laf_activity_stats?.[keyword]?.rows" in js
    assert "data.laf_activity_stats?.[\"閱卷\"]?.count" in js
    assert "skipped_payment_only" in js
    assert 'data-act="laf-event-detail"' in js
    assert "<time>${esc(row.date || \"未標示時間\")}</time>" in js
    assert "gcal_import:" in js
    assert ".laf-event-lines" in css
    assert ".laf-event-line time" in css
    assert ".laf-event-full-row" in css
    assert 'act === "laf-event-detail"' in events_js
    assert "laf-activity-stats-v1" in html
