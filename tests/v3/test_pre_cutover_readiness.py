from __future__ import annotations

import json
from pathlib import Path

from scripts.v3_validation.source_anchor_refresh import refresh_readiness_evidence


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "config" / "v3_pre_cutover_readiness.json"
EXPECTED_REQUIRED = {
    "production_http_5002",
    "production_http_5003",
    "production_http_8088",
    "v2_route_handlers_347",
    "login_and_session",
    "callbacks_and_webhooks",
    "sse_streaming",
    "multipart_upload",
}


def _load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _surfaces(manifest: dict) -> dict[str, dict]:
    return {surface["id"]: surface for surface in manifest["surfaces"]}


def test_readiness_manifest_has_complete_required_surface_gate() -> None:
    manifest = _load_manifest()
    surfaces = _surfaces(manifest)

    assert manifest["schema_version"] == 1
    assert "documentation claims are not evidence" in manifest["audit_basis"]
    assert set(manifest["required_surface_ids"]) == EXPECTED_REQUIRED
    assert set(surfaces) == EXPECTED_REQUIRED
    assert len(surfaces) == len(manifest["surfaces"])

    required = [surfaces[surface_id] for surface_id in manifest["required_surface_ids"]]
    assert all(surface["required"] is True for surface in required)
    assert manifest["summary"] == {
        "required": len(required),
        "implemented": sum(surface["implemented"] is True for surface in required),
        "tested": sum(surface["tested"] is True for surface in required),
        "blocked": sum(surface["blocked"] is True for surface in required),
    }


def test_every_status_is_machine_readable_and_backed_by_precise_evidence() -> None:
    manifest = _load_manifest()

    assert refresh_readiness_evidence(manifest, ROOT) == manifest

    for surface in manifest["surfaces"]:
        for field in ("required", "implemented", "tested", "blocked"):
            assert type(surface[field]) is bool, (surface["id"], field)
        expected_status = (
            "blocked"
            if surface["blocked"]
            else "ready"
            if surface["implemented"] and surface["tested"]
            else "implemented"
            if surface["implemented"]
            else "missing"
        )
        assert surface["status"] == expected_status
        assert surface["blockers"] if surface["blocked"] else True
        assert surface["evidence"], surface["id"]

        for evidence in surface["evidence"]:
            assert set(evidence) == {"file", "line", "anchor", "kind", "finding"}
            assert evidence["kind"] in {"source", "test"}
            path = ROOT / evidence["file"]
            assert path.is_file(), path
            lines = path.read_text(encoding="utf-8").splitlines()
            assert 1 <= evidence["line"] <= len(lines), evidence
            actual_line = lines[evidence["line"] - 1]
            assert evidence["anchor"] in actual_line, (evidence, actual_line)


def test_every_required_surface_is_implemented_tested_and_ready() -> None:
    manifest = _load_manifest()
    surfaces = _surfaces(manifest)
    required = [surfaces[surface_id] for surface_id in manifest["required_surface_ids"]]
    derived_ready = all(
        surface["implemented"] is True
        and surface["tested"] is True
        and surface["blocked"] is False
        for surface in required
    )

    assert not any(surface["blocked"] for surface in required)
    assert manifest["replacement_ready"] is derived_ready is True
    assert all(surface["status"] == "ready" for surface in required)


def test_surface_readiness_remains_explicitly_separate_from_live_validation() -> None:
    surfaces = _surfaces(_load_manifest())

    for surface_id in EXPECTED_REQUIRED:
        surface = surfaces[surface_id]
        assert surface["live_validation_required"] is True
        assert surface["implemented"] is True
        assert surface["tested"] is True
        assert surface["blocked"] is False
        assert {item["kind"] for item in surface["evidence"]} <= {"source", "test"}
