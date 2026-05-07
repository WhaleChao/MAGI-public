# -*- coding: utf-8 -*-
"""Consumer debt supplement items must follow OSC case checklist logic."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch
import sys

from flask import Flask

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_osc_debt_module():
    spec = importlib.util.spec_from_file_location(
        "osc_debt_for_test",
        ROOT / "api" / "blueprints" / "osc_debt.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _build_app() -> tuple[Flask, object]:
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test"
    mod = _load_osc_debt_module()

    app.register_blueprint(mod.osc_debt_bp)
    return app, mod


def test_debt_supplement_checklist_writes_case_checklists_not_todos():
    app, mod = _build_app()
    calls = []

    def fake_exec(sql, params=(), fetch="none"):
        calls.append((sql, params, fetch))
        return {"rowcount": 1}, {"host": "test"}

    payload = {
        "case_no": "113消債更字第1號",
        "applicant": "王小明",
        "items": [
            {"category": "勞保資料", "period": "112年度", "attachment": "附件一"},
            {"name": "收入證明", "description": "近三個月", "notes": "請當事人補正"},
        ],
    }
    with patch.object(mod, "_debt_osc_exec", side_effect=fake_exec):
        r = app.test_client().post("/api/osc/debt/supplement-checklist", json=payload)

    body = r.get_json()
    assert r.status_code == 200
    assert body["ok"] is True
    assert body["case_number"] == "113消債更字第1號"
    assert body["synced"] == 2
    assert body["skipped"] == 0
    assert len(calls) == 2
    assert all("INSERT INTO case_checklists" in sql for sql, _params, _fetch in calls)
    assert not any("case_todos" in sql for sql, _params, _fetch in calls)
    assert calls[0][1] == ("113消債更字第1號", "勞保資料（112年度）", "待補", "附件: 附件一")


def test_debt_supplement_checklist_requires_case_number():
    app, _mod = _build_app()
    r = app.test_client().post(
        "/api/osc/debt/supplement-checklist",
        json={"items": [{"category": "戶籍謄本"}]},
    )

    body = r.get_json()
    assert r.status_code == 400
    assert body["ok"] is False
    assert "case_number" in body["error"]
