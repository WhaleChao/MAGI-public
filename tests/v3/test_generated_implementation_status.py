from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.docs.generate_implementation_status import build_status, render_markdown


ROOT = Path(__file__).resolve().parents[2]


def test_generated_status_uses_active_v3_marker_and_manifest(tmp_path) -> None:
    release = tmp_path / "releases" / "v3-test"
    release.mkdir(parents=True)
    manifest = release / "release-manifest.json"
    manifest.write_text(
        json.dumps({"release_id": "v3-test", "commit": "a" * 40}),
        encoding="utf-8",
    )
    marker = tmp_path / "active-release.json"
    marker.write_text(
        json.dumps(
            {
                "schema": "magi.v3.active-release/v1",
                "release": "v3",
                "release_id": "v3-test",
                "release_root": str(release),
                "release_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "committed_at": "2026-08-29T04:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    status = build_status(
        ROOT,
        active_marker=marker,
        generated_at=datetime(2026, 8, 29, 4, 0, tzinfo=timezone.utc),
    )
    assert status["active_release"]["release_id"] == "v3-test"
    assert status["contracts"]["v2_active_matrix"] is False
    assert status["contracts"]["rollback_floor_release_id"] == "v3-20260829-rc643-r59"
    assert status["contracts"]["pre_seal_production_mutation"] is False
    assert status["inventory"]["api_routes"] > 0
    assert status["inventory"]["skills"] > 0


def test_rendered_status_cannot_claim_v2_is_production() -> None:
    status = {
        "generated_at": "2026-08-29T04:00:00+00:00",
        "source_commit": "a" * 40,
        "source_matches_active_release": True,
        "active_release": {"status": "active", "release_id": "v3-current"},
        "contracts": {"rollback_floor_release_id": "v3-r59"},
        "inventory": {
            "api_routes": 1,
            "api_route_domains": 1,
            "skills": 1,
            "skill_issues": 0,
            "schedule_body_adapters": 1,
            "quality": {"unique_test_file_count": 1, "declared_test_reference_count": 1},
        },
    }
    rendered = render_markdown(status)
    assert "active production release：v3-current（V3）" in rendered
    assert "- source commit：" not in rendered
    assert "V2 是唯一 active" not in rendered
    assert "不得手動維護版本與數量" in rendered
    assert "rollback floor：`v3-r59`" in rendered
    assert "production marker 全程唯讀" in rendered
