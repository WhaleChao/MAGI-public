from __future__ import annotations

from scripts.ops.selfhost_release_smoke import run_smoke


def test_native_release_smoke_proves_package_upgrade_rollback_and_tamper_detection() -> None:
    report = run_smoke()

    assert report["ok"] is True
    assert report["native_execution"] is True
    assert report["summary"]["fail"] == 0
    assert {item["key"] for item in report["findings"]} == {
        "secret_free_archive",
        "clean_extract",
        "stage_and_certify",
        "upgrade_and_rollback",
        "tamper_detection",
        "native_service_plan",
    }
