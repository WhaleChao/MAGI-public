from __future__ import annotations

from casper_ecosystem.law_firm_orchestrators import file_review_automation as mod


def test_lawyer_portal_sso_records_driver_bootstrap_failure(monkeypatch, tmp_path):
    sso = mod.LawyerPortalSSO(
        username="user",
        password="pass",
        download_folder=str(tmp_path),
        headless=True,
    )

    def _boom():
        raise RuntimeError("session not created: DevToolsActivePort file doesn't exist")

    monkeypatch.setattr(sso, "_setup_driver", _boom)

    assert sso.login(max_retries=1) is False
    assert sso.last_error_code == "driver_init_failed"
    assert "DevToolsActivePort" in sso.last_error_detail


def test_file_review_manager_exposes_last_login_error(monkeypatch, tmp_path):
    class FakeSSO:
        def __init__(self, **kwargs):
            self.driver = None
            self.last_error_code = "driver_init_failed"
            self.last_error_detail = "session not created: DevToolsActivePort file doesn't exist"

        def login(self):
            return False

        def close(self):
            return None

    monkeypatch.setattr(mod, "LawyerPortalSSO", FakeSSO)

    mgr = mod.FileReviewManager(
        username="user",
        password="pass",
        download_folder=str(tmp_path),
        headless=True,
    )

    assert mgr.login() is False
    assert mgr.last_login_error_code == "driver_init_failed"
    assert "DevToolsActivePort" in mgr.last_login_error_detail
