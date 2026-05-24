from api.osc.accounting_sheet_import import (
    AccountingSheetRow,
    DEFAULT_ACCOUNT_HINT,
    _default_credentials_path,
    _default_token_path,
    fixed_expense_overlap_details,
    is_revoked_google_token_error,
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


def test_revoked_google_token_error_detection():
    assert is_revoked_google_token_error(Exception("invalid_grant: Token has been expired or revoked."))
    assert not is_revoked_google_token_error(Exception("quota exceeded"))


def test_accounting_google_env_paths_are_isolated(monkeypatch):
    monkeypatch.delenv("MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_PATH", raising=False)
    monkeypatch.delenv("MAGI_ACCOUNTING_GOOGLE_SHEETS_TOKEN", raising=False)
    monkeypatch.setenv("MAGI_GOOGLE_CREDENTIALS_PATH", "/shared/google_credentials.json")
    monkeypatch.setenv("MAGI_GOOGLE_SHEETS_TOKEN", "/shared/sheets_token.json")
    assert str(_default_credentials_path()) == "/shared/google_credentials.json"
    assert str(_default_token_path()) == "/shared/sheets_token.json"

    monkeypatch.setenv("MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_PATH", "/accounting/credentials.json")
    monkeypatch.setenv("MAGI_ACCOUNTING_GOOGLE_SHEETS_TOKEN", "/accounting/token.json")
    assert str(_default_credentials_path()) == "/accounting/credentials.json"
    assert str(_default_token_path()) == "/accounting/token.json"


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
        assert kwargs["account_hint"] == DEFAULT_ACCOUNT_HINT
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


def test_parse_colleague_month_sheet_loose_fixed_layout():
    values = [
        ["每月收支清單"],
        ["2026年", "五月", "", "", ""],
        ["類別", "", "", "", ""],
        ["一般案件", "", "", "", ""],
        ["總額", "", "", "", ""],
        ["法扶案件", "2026-05-20 00:00:00", "1141216-E-014 林里", "預付酬金", 6000],
        ["", "2026-05-20 00:00:00", "1150303-I-004 林亮宏", "預付酬金", 4000],
        ["雜支", "2026-05-05 00:00:00", "法扶分會、地檢署、地方法院等木章", "", 1200],
    ]
    rows, stats = parse_sheet_values(values, month="2026-05")
    assert stats["header_rows"] == [3]
    assert len(rows) == 3
    assert rows[0].type == "收入"
    assert rows[0].category == "法扶案件"
    assert rows[0].case_ref == "1141216-E-014"
    assert rows[1].type == "收入"
    assert rows[1].category == "法扶案件"
    assert rows[2].type == "支出"
    assert rows[2].category == "雜支"


def test_fixed_expense_overlap_skips_payroll(monkeypatch):
    from api.osc.accounting_sheet_import import AccountingSheetRow, is_fixed_expense_overlap

    def fake_helpers():
        def fake_exec(sql, params=(), fetch="none"):
            return [
                {"id": 1, "category": "人事費", "sub_type": "薪資", "description": "政翔薪水", "amount": 45800.0}
            ], {}

        return fake_exec, lambda ref: ref

    monkeypatch.setattr("api.osc.accounting_sheet_import._get_osc_helpers", fake_helpers)
    row = AccountingSheetRow(
        source_row=23,
        date="2026-05-25",
        type="支出",
        amount=46800.0,
        category="薪資",
        description="主持律師薪資",
    )
    assert is_fixed_expense_overlap(row) is True


def test_resolve_accounting_case_ref_returns_none_for_unknown_laf_no(monkeypatch):
    from api.osc.accounting_sheet_import import resolve_accounting_case_ref

    def fake_helpers():
        def fake_exec(sql, params=(), fetch="none"):
            return None, {}

        return fake_exec, lambda ref: ref

    monkeypatch.setattr("api.osc.accounting_sheet_import._get_osc_helpers", fake_helpers)
    assert resolve_accounting_case_ref("1150519-E-014") is None


def test_fixed_expense_overlap_reports_amount_conflict(monkeypatch):
    def fake_helpers():
        def fake_exec(sql, params=(), fetch="none"):
            return [
                {"id": 1, "category": "人事費", "sub_type": "薪資", "description": "政翔薪水", "amount": 45800.0}
            ], {}

        return fake_exec, lambda ref: ref

    monkeypatch.setattr("api.osc.accounting_sheet_import._get_osc_helpers", fake_helpers)
    row = AccountingSheetRow(
        source_row=23,
        date="2026-05-25",
        type="支出",
        amount=46800.0,
        category="薪資",
        description="主持律師薪資",
    )
    details = fixed_expense_overlap_details(row)
    assert details is not None
    assert details["family"] == "薪資"
    assert details["amount_conflict"] is True


def test_recurring_sync_generated_route(monkeypatch):
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
    calls = []

    def fake_helpers():
        def fake_exec(sql, params=(), fetch="none"):
            calls.append((sql, params, fetch))
            if "FROM recurring_expenses" in sql:
                return {
                    "id": 9,
                    "category": "人事費",
                    "sub_type": "薪資",
                    "description": "政翔薪水",
                    "amount": 46800.0,
                }, {}
            if sql.strip().startswith("UPDATE case_transactions"):
                return {"rowcount": 5}, {}
            if "FROM case_transactions" in sql:
                return [], {}
            return {}, {}

        return fake_exec, lambda x: x, lambda *a, **k: None, lambda x: x, lambda v, d=0: int(v or d)

    monkeypatch.setattr("api.blueprints.osc_accounting._get_osc_helpers", fake_helpers)
    resp = app.test_client().post("/api/osc/accounting/recurring/9/sync-generated", json={"amount": 46800})
    assert resp.status_code == 200
    assert resp.get_json()["updated_count"] == 5
    assert any("[固定] 政翔薪水" in str(params) for _, params, _ in calls)
