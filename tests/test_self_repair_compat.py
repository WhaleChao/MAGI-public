from __future__ import annotations

import importlib.util
from pathlib import Path


MAGI_ROOT = Path(__file__).resolve().parent.parent
SELF_REPAIR_PATH = MAGI_ROOT / "skills" / "magi-self-repair" / "action.py"


def _load_self_repair_module():
    spec = importlib.util.spec_from_file_location("magi_self_repair_test", SELF_REPAIR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_self_repair_wrapper_delegates_targets(monkeypatch):
    module = _load_self_repair_module()

    class DoctorStub:
        @staticmethod
        def heal(targets):
            return [
                {
                    "id": target,
                    "repaired": True,
                    "action": f"repair:{target}",
                    "detail": "ok",
                }
                for target in targets
            ]

    monkeypatch.setattr(module, "_load_doctor_module", lambda: DoctorStub())

    report = module.repair_targets(["omlx_local", {"id": "network"}, ""])

    assert report["total_targets"] == 2
    assert report["repaired"] == 2
    assert report["failed"] == 0
    assert [item["id"] for item in report["repairs"]] == ["omlx_local", "network"]


def test_self_repair_wrapper_falls_back_to_doctor_report(monkeypatch):
    module = _load_self_repair_module()

    class DoctorStub:
        @staticmethod
        def diagnose():
            return {"sections": [{"category": "infrastructure", "items": []}]}

        @staticmethod
        def heal_from_report(report):
            return {
                "message": "基礎設施全部正常，無需修復",
                "repairs": [],
            }

    monkeypatch.setattr(module, "_load_doctor_module", lambda: DoctorStub())

    report = module.repair_targets(None)

    assert report["message"] == "基礎設施全部正常，無需修復"
    assert report["repairs"] == []
