from api.osc.accounting_sheet_import import (
    AccountingSheetRow,
    month_window,
    parse_date,
    parse_sheet_values,
)


def test_month_window_current_and_previous():
    _, _, current = month_window(None)
    assert len(current) == 7
    start, end, key = month_window("2026-05")
    assert key == "2026-05"
    assert start.isoformat() == "2026-05-01"
    assert end.isoformat() == "2026-05-31"


def test_parse_date_accepts_roc_year():
    parsed = parse_date("115/5/12")
    assert parsed is not None
    assert parsed.isoformat() == "2026-05-12"


def test_parse_sheet_values_skips_junru_and_filters_month():
    values = [
        ["日期", "標識", "分類", "支出", "收入", "備註", "OSC案號"],
        ["115/05/01", "", "影印", "120", "", "卷證影印", "2026-0001"],
        ["115/05/02", "俊儒", "郵資", "80", "", "不是我的帳", "2026-0002"],
        ["115/04/30", "", "交通", "300", "", "上月", "2026-0003"],
        ["115/05/03", "", "委任費", "", "5000", "收款", "2026-0004"],
    ]
    rows, stats = parse_sheet_values(values, month="2026-05")
    assert stats["parsed"] == 2
    assert stats["skipped_owner"] == 1
    assert stats["skipped_outside_month"] == 1
    assert rows[0] == AccountingSheetRow(
        source_row=2,
        date="2026-05-01",
        type="支出",
        amount=120.0,
        category="影印",
        sub_type=None,
        description="卷證影印",
        case_ref="2026-0001",
        owner=None,
        fingerprint=rows[0].fingerprint,
    )
    assert rows[1].type == "收入"
    assert rows[1].amount == 5000.0
    assert rows[0].fingerprint and rows[1].fingerprint


def test_accounting_import_api_preview(monkeypatch):
    from flask import Flask
    from flask_login import LoginManager, UserMixin

    from api.blueprints.osc_accounting import osc_accounting_bp

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["LOGIN_DISABLED"] = True
    app.secret_key = "test"
    login = LoginManager(app)

    class User(UserMixin):
        id = "test"

    @login.user_loader
    def _load(_user_id):
        return User()

    app.register_blueprint(osc_accounting_bp)

    def fake_run_import(**kwargs):
        assert kwargs["month"] == "2026-05"
        assert kwargs["dry_run"] is True
        assert kwargs["account_hint"] == "zl.hualien"
        return {"ok": True, "month": "2026-05", "importable_count": 1}

    monkeypatch.setattr("api.osc.accounting_sheet_import.run_import", fake_run_import)
    resp = app.test_client().get("/api/osc/accounting/import/google-sheet?month=2026-05")
    assert resp.status_code == 200
    assert resp.get_json()["importable_count"] == 1


def test_parse_colleague_month_sheet_multiple_sections():
    values = [
        ["每月收支清單 2026年", "四月", "", "", ""],
        ["類別", "時間", "姓名", "備註", "收入"],
        ["一般案件", "2026-04-24 00:00:00", "社團法人花蓮縣牛犁社區交流協會", "法律顧問契約費用", "12000.0"],
        ["總額", "", "", "", "12000"],
        ["類別", "時間", "說明", "備註", "支出"],
        ["郵資", "2026-04-25 00:00:00", "掛號", "郵局", "36"],
    ]
    rows, stats = parse_sheet_values(values, month="2026-04")
    assert stats["header_rows"] == [2, 5]
    assert len(rows) == 2
    assert rows[0].type == "收入"
    assert rows[0].description == "社團法人花蓮縣牛犁社區交流協會｜法律顧問契約費用"
    assert rows[1].type == "支出"
    assert rows[1].category == "郵資"
    assert rows[1].description == "掛號｜郵局"
