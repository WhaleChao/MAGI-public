from __future__ import annotations

import json
from datetime import datetime, timezone

from scripts.ops import osc_events_refresh


def test_write_latest_serializes_datetime_nested_payload(tmp_path):
    out = tmp_path / "osc_events_refresh_latest.json"
    payload = {
        "ok": True,
        "scan": {
            "results": [
                {
                    "items": [
                        {
                            "todos": [
                                {
                                    "datetime": datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
                                }
                            ]
                        }
                    ]
                }
            ]
        },
    }

    osc_events_refresh._write_latest(payload, out)

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["scan"]["results"][0]["items"][0]["todos"][0]["datetime"] == "2026-05-13T12:00:00+00:00"
