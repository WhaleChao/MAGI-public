from pathlib import Path

from scripts.install_magi import build_install_plan, venv_python
from scripts.magi_doctor import collect_report


def test_doctor_collect_report_without_live_probe_has_expected_shape():
    report = collect_report(live=False)

    assert {"ok", "status", "system", "summary", "checks"} <= set(report)
    assert any(item["name"] == "python" for item in report["checks"])


def test_install_plan_uses_requested_venv_dir():
    venv_dir = Path("/tmp/magi-test-venv")
    plan = build_install_plan(include_optional=False, venv_dir=venv_dir)

    assert [step.name for step in plan] == ["create_venv", "upgrade_pip", "install_core", "doctor"]
    assert str(venv_python(venv_dir)) in plan[1].command
