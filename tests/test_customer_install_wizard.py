from types import SimpleNamespace

from scripts import customer_install_wizard as wizard


def _args(tmp_path, **overrides):
    data = {
        "yes": False,
        "public": True,
        "require_config": False,
        "no_write_env": False,
        "no_optional": True,
        "with_db": False,
        "skip_readiness": False,
        "check_live": False,
        "install_service": False,
        "no_live": True,
        "json": True,
        "output": tmp_path / "customer_install_wizard.json",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_customer_install_wizard_dry_run_writes_report(tmp_path, monkeypatch):
    monkeypatch.setattr(
        wizard,
        "_preflight_step",
        lambda *, live: wizard.WizardStep("preflight", "Detect this computer", "pass", "ok"),
    )
    monkeypatch.setattr(
        wizard,
        "_env_step",
        lambda *, public, require_config, write_env: wizard.WizardStep("env", "Create and check local .env", "pass", "ok"),
    )
    monkeypatch.setattr(
        wizard,
        "_public_audit_step",
        lambda *, public: wizard.WizardStep("public_audit", "Public isolation audit", "pass", "ok"),
    )
    monkeypatch.setattr(
        wizard,
        "_readiness_step",
        lambda *, skip, skip_db, live: wizard.WizardStep("readiness", "Commercial readiness gate", "pass", "ok"),
    )
    monkeypatch.setattr(
        wizard,
        "_service_step",
        lambda *, install_service, execute: wizard.WizardStep("service", "Install background service", "skipped", "not requested"),
    )

    payload = wizard.run_wizard(_args(tmp_path))

    assert payload["ok"] is True
    assert payload["mode"] == "dry-run"
    assert payload["summary"]["fail"] == 0
    assert payload["summary"]["skipped"] >= 1
    assert "customer_install_wizard.py --public --yes" in "\n".join(payload["next_steps"])
    assert (tmp_path / "customer_install_wizard.json").exists()


def test_customer_install_wizard_reports_required_config_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        wizard,
        "_preflight_step",
        lambda *, live: wizard.WizardStep("preflight", "Detect this computer", "pass", "ok"),
    )
    monkeypatch.setattr(
        wizard,
        "_env_step",
        lambda *, public, require_config, write_env: wizard.WizardStep(
            "env",
            "Create and check local .env",
            "fail",
            "missing DB_PASSWORD",
            required=True,
        ),
    )
    monkeypatch.setattr(
        wizard,
        "_public_audit_step",
        lambda *, public: wizard.WizardStep("public_audit", "Public isolation audit", "pass", "ok"),
    )
    monkeypatch.setattr(
        wizard,
        "_readiness_step",
        lambda *, skip, skip_db, live: wizard.WizardStep("readiness", "Commercial readiness gate", "skipped", "skipped"),
    )
    monkeypatch.setattr(
        wizard,
        "_service_step",
        lambda *, install_service, execute: wizard.WizardStep("service", "Install background service", "skipped", "not requested"),
    )

    payload = wizard.run_wizard(_args(tmp_path, require_config=True, skip_readiness=True))

    assert payload["ok"] is False
    assert payload["status"] == "fail"
    assert payload["summary"]["fail"] == 1
