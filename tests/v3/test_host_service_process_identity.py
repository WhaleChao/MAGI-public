from pathlib import Path

from scripts.v3_cutover import probe


def test_stable_launcher_identity_handles_unquoted_application_support_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    application_root = tmp_path / "Application Support" / "MAGI"
    monkeypatch.setattr(probe, "MAGI_APPLICATION_ROOT", application_root)
    launcher = application_root / "bin" / "magi-active-release-service.py"

    identity = probe._host_singleton_process_identity(
        f"/usr/bin/python3 {launcher} paperclip-share-gateway"
    )

    assert identity == ("share_gateway", f"{launcher}:paperclip-share-gateway")


def test_active_release_child_identity_handles_unquoted_release_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    application_root = tmp_path / "Application Support" / "MAGI"
    monkeypatch.setattr(probe, "MAGI_APPLICATION_ROOT", application_root)
    child = application_root / "releases" / "v3-test" / "scripts" / "share_tunnel_supervisor.py"

    identity = probe._host_singleton_process_identity(f"/usr/bin/python3 -B {child}")

    assert identity == ("share_tunnel", str(child))


def test_similar_non_allowlisted_process_is_not_suppressed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    application_root = tmp_path / "Application Support" / "MAGI"
    monkeypatch.setattr(probe, "MAGI_APPLICATION_ROOT", application_root)
    launcher = application_root / "bin" / "magi-active-release-service.py"

    assert (
        probe._host_singleton_process_identity(
            f"/usr/bin/python3 {launcher}.bak paperclip-share-gateway"
        )
        is None
    )
