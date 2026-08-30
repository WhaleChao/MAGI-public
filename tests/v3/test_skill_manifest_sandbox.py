from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from magi_v3.skill_manifest import (
    CATALOG_SCHEMA,
    SkillManifestError,
    load_manifest,
    manifest_digest,
    verify_catalog_approval,
    write_candidate_manifest,
)
from magi_v3.skill_sandbox import run_manifested_skill


def _skill(tmp_path: Path) -> tuple[Path, dict]:
    skill = tmp_path / "generated-one"
    skill.mkdir()
    (skill / "action.py").write_text("print('ok')\n", encoding="utf-8")
    write_candidate_manifest(skill_dir=skill, skill_id="generated-one")
    return skill, load_manifest(skill)


def test_candidate_manifest_binds_action_and_defaults_to_no_authority(tmp_path: Path) -> None:
    skill, manifest = _skill(tmp_path)
    assert manifest["permissions"]["network"] == {"mode": "none", "hosts": []}
    assert manifest["permissions"]["secrets"] == []
    assert manifest["permissions"]["subprocess"] is False

    (skill / "action.py").write_text("print('tampered')\n", encoding="utf-8")
    with pytest.raises(SkillManifestError, match="digest mismatch"):
        load_manifest(skill)


def test_live_skill_requires_exact_release_catalog_digest(tmp_path: Path) -> None:
    skill, manifest = _skill(tmp_path)
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "schema": CATALOG_SCHEMA,
                "skills": {
                    "generated-one": {
                        "enabled": True,
                        "manifest_sha256": manifest_digest(manifest),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    verify_catalog_approval(manifest, catalog_path=catalog)

    value = json.loads(catalog.read_text(encoding="utf-8"))
    value["skills"]["generated-one"]["manifest_sha256"] = "0" * 64
    catalog.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(SkillManifestError, match="digest mismatch"):
        verify_catalog_approval(manifest, catalog_path=catalog)


def test_manifest_rejects_undeclared_dependency_hash(tmp_path: Path) -> None:
    skill, manifest = _skill(tmp_path)
    manifest["dependencies"]["packages"] = [
        {"name": "requests", "version": "2.32.0", "sha256": "f" * 64}
    ]
    manifest["signature"]["value"] = manifest_digest(manifest)
    (skill / "skill-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SkillManifestError, match="lock SHA-256"):
        load_manifest(skill)


@pytest.mark.skipif(sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file(), reason="macOS Seatbelt required")
def test_whole_process_seatbelt_denies_network_and_external_write(tmp_path: Path) -> None:
    skill, _ = _skill(tmp_path)
    escape = tmp_path.parent / "magi-skill-escape-probe"
    code = (
        "import pathlib,socket,sys; escape=pathlib.Path(sys.argv[1]);"
        "\ntry: escape.write_text('bad')\nexcept OSError: pass\nelse: raise SystemExit(91);"
        "\ns=socket.socket();s.settimeout(.2);"
        "\ntry: s.connect(('127.0.0.1',9))\nexcept OSError: pass\nelse: raise SystemExit(92);"
        "\nprint('confined')"
    )
    result = run_manifested_skill(
        [sys.executable, "-c", code, str(escape)],
        skill_dir=skill,
        env={"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8"},
        timeout_seconds=10,
    )
    if result["rc"] == 71 and "sandbox_apply: Operation not permitted" in result["stderr"]:
        assert result["sandbox"] == {
            "ok": False,
            "kind": "macos_seatbelt",
            "fail_closed": True,
            "reason": "sandbox_apply_denied_by_host",
            "manifest_skill_id": "generated-one",
        }
        if os.environ.get("MAGI_V3_RELEASE_QUALITY_SEATBELT_CHILD") != "1":
            # A managed host can deny nested sandbox creation without placing
            # this pytest process inside MAGI's formal profile. The runtime must
            # fail closed; only the formal campaign marker permits the inherited
            # confinement proof below.
            assert not escape.exists()
            return
        # The formal campaign runs the complete pytest process inside Seatbelt.
        # Prove inherited whole-process confinement instead of trusting the
        # nested sandbox error as evidence by itself.
        outer_escape = Path("/private/tmp") / f"magi-skill-outer-escape-probe-{os.getpid()}"
        outer_result = subprocess.run(
            [sys.executable, "-c", code, str(outer_escape)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        try:
            assert outer_result.returncode == 0, outer_result
            assert outer_result.stdout.strip() == "confined"
            assert not outer_escape.exists()
        finally:
            if outer_escape.exists():
                outer_escape.unlink()
        return
    assert result["rc"] == 0, result
    assert result["stdout"] == "confined"
    assert result["sandbox"]["ok"] is True
    assert not escape.exists()
