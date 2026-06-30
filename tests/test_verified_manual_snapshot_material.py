from __future__ import annotations

import json
from pathlib import Path

from scripts import generate_verified_user_manual_docx as manual


def _snapshot() -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-06-29T12:00:00+00:00",
        "summary": {"status_counts": {"verified_live": 1}},
        "core_functions": [
            {
                "id": "calendar_todos",
                "name": "Google Calendar 與 OSC 待辦",
                "status": "verified_live",
                "user_summary": "查今日或本週行程、看 OSC 待辦，並保留外部日曆事件標題。",
                "entry_points": ["Calendar", "OSC 待辦頁", "自然語言"],
                "manual_commands": ["今天有什麼行程？", "列出本週 OSC 建立待辦。"],
                "manual_section_hint": "Calendar 與 OSC 待辦",
                "last_unit_test": {"source": "tests/test_osc_events_refresh.py"},
                "last_live_check": {
                    "source": ".runtime/business_module_live_check_latest.json",
                    "check_id": "calendar_todo_status_live",
                },
                "token_status_hint": {"hint": "相關 token health 最近回報可用。"},
            }
        ],
    }


def test_build_manual_material_turns_snapshot_into_reader_friendly_sections():
    material = manual.build_manual_material(_snapshot())

    assert material["schema_version"] == 1
    assert material["source_snapshot_schema_version"] == 1
    section = material["sections"][0]
    assert section["title"] == "Google Calendar 與 OSC 待辦"
    assert section["plain_intro"].startswith("查今日或本週行程")
    assert "入口：Calendar、OSC 待辦頁、自然語言。" == section["how_to_use"]
    assert "可用功能" in section["status_summary"]
    assert "PASS" not in json.dumps(section, ensure_ascii=False)


def test_feature_rows_and_material_json_are_usable(tmp_path: Path):
    material = manual.build_manual_material(_snapshot())
    rows = manual.feature_rows_from_manual_material(material)
    out = manual.write_manual_material(material, tmp_path / "manual_material.json")

    assert out.exists()
    assert rows == [
        [
            "Google Calendar 與 OSC 待辦",
            "查今日或本週行程、看 OSC 待辦，並保留外部日曆事件標題。",
            "入口：Calendar、OSC 待辦頁、自然語言。\n今天有什麼行程？\n列出本週 OSC 建立待辦。",
            "最近有 LIVE 或 runtime 健康證據，可列為可用功能。",
            "Calendar 與 OSC 待辦",
        ]
    ]
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["sections"][0]["feature_id"] == "calendar_todos"
