from datetime import date


def test_period_for_settlement_month_uses_26_to_25_window():
    from api.osc.accounting_bonus import default_settlement_month, period_for_settlement_month

    start, end, key = period_for_settlement_month("2026-05")
    assert key == "2026-05"
    assert start.isoformat() == "2026-04-26"
    assert end.isoformat() == "2026-05-25"
    assert default_settlement_month(date(2026, 5, 24), catch_up=True) == "2026-05"
    assert default_settlement_month(date(2026, 6, 3), catch_up=True) == "2026-05"
    assert default_settlement_month(date(2026, 5, 12), catch_up=True) is None


def test_query_laf_debt_fee_rows_filters_to_debt_laf_income(monkeypatch):
    from api.osc import accounting_bonus as mod

    candidate_rows = [
        {
            "id": 1,
            "date": "2026-05-20",
            "type": "收入",
            "category": "法扶案件",
            "description": "1150317-E-009 邱衣萱｜預付酬金",
            "amount": 4000,
            "case_type": "消費者債務清理",
            "case_category": "法律扶助案件",
            "case_reason": "更生",
            "case_number": "2026-0024",
            "client_name": "邱衣萱",
        },
        {
            "id": 2,
            "date": "2026-05-20",
            "type": "收入",
            "category": "法扶案件",
            "description": "1150320-E-014 劉信義｜預付酬金",
            "amount": 15000,
            "case_type": "刑事",
            "case_category": "法律扶助案件",
            "case_reason": "殺人",
        },
    ]

    def fake_exec(sql, params=(), fetch="none"):
        if "FROM case_transactions t" in sql:
            return candidate_rows, {}
        if "FROM cases" in sql:
            return None, {}
        raise AssertionError(sql)

    monkeypatch.setattr(mod, "_get_osc_helpers", lambda: fake_exec)
    rows = mod.query_laf_debt_fee_rows(date(2026, 5, 1), date(2026, 5, 31))
    assert len(rows) == 1
    assert rows[0]["amount"] == 4000
    assert rows[0]["client_name"] == "邱衣萱"


def test_query_laf_debt_fee_rows_matches_single_debt_case_by_client_name(monkeypatch):
    from api.osc import accounting_bonus as mod

    candidate_rows = [
        {
            "id": 8,
            "date": "2026-05-20",
            "type": "收入",
            "category": "法扶案件",
            "description": "1150304-E-003 黃鎮洲｜預付酬金",
            "amount": 4000,
            "case_type": None,
            "case_category": None,
            "case_reason": None,
            "case_number": None,
            "client_name": None,
            "legal_aid_number": None,
            "laf_case_no": None,
        }
    ]
    matching_case = {
        "id": "2026-0031",
        "case_number": "2026-0031",
        "client_name": "黃鎮洲",
        "case_type": "消費者債務清理",
        "case_category": "法律扶助案件",
        "case_reason": "更生",
        "folder_path": "",
        "folder_name": "",
        "legal_aid_number": "",
        "laf_case_no": "",
    }

    def fake_exec(sql, params=(), fetch="none"):
        if "FROM case_transactions t" in sql:
            return candidate_rows, {}
        if "WHERE legal_aid_number=%s" in sql:
            return None, {}
        if "WHERE client_name=%s" in sql:
            assert params == ("黃鎮洲",)
            return [matching_case], {}
        raise AssertionError(sql)

    monkeypatch.setattr(mod, "_get_osc_helpers", lambda: fake_exec)
    rows = mod.query_laf_debt_fee_rows(date(2026, 5, 1), date(2026, 5, 31))
    assert len(rows) == 1
    assert rows[0]["case_number"] == "2026-0031"
    assert rows[0]["client_name"] == "黃鎮洲"
    assert rows[0]["amount"] == 4000


def test_calculate_monthly_bonus_waits_when_no_laf_fee(monkeypatch):
    from api.osc import accounting_bonus as mod

    calls = []

    def fake_exec(sql, params=(), fetch="none"):
        calls.append((sql, params, fetch))
        if sql.strip().startswith("CREATE TABLE"):
            return {}, {}
        if sql.strip().startswith("SHOW COLUMNS"):
            return [{"Field": "xlsx_path"}], {}
        if "FROM case_transactions t" in sql:
            return [], {}
        if "income_total" in sql and "expense_total" in sql:
            return {"income_total": 30000, "expense_total": 10000}, {}
        if "SELECT id FROM case_transactions" in sql:
            return None, {}
        if "INSERT INTO accounting_monthly_bonus_runs" in sql:
            return {"rowcount": 1}, {}
        return {"rowcount": 0}, {}

    monkeypatch.setattr(mod, "_get_osc_helpers", lambda: fake_exec)
    monkeypatch.setattr(mod, "refresh_accounting_import_for_period", lambda *a, **k: [])
    result = mod.calculate_monthly_bonus(month="2026-05", commit=True, refresh_import=False)

    assert result["status"] == "waiting_laf_fee"
    assert result["legal_aid_bonus_amount"] == 0
    assert result["case_bonus_employee_amount"] == 0
    assert any("accounting_monthly_bonus_runs" in sql for sql, _, _ in calls)


def test_ensure_bonus_schema_adds_xlsx_path_for_existing_table(monkeypatch):
    from api.osc import accounting_bonus as mod

    calls = []

    def fake_exec(sql, params=(), fetch="none"):
        calls.append((sql, params, fetch))
        if sql.strip().startswith("SHOW COLUMNS"):
            return [{"Field": "month_key"}], {}
        return {"rowcount": 0}, {}

    monkeypatch.setattr(mod, "_get_osc_helpers", lambda: fake_exec)
    mod.ensure_bonus_schema()

    assert any("ADD COLUMN xlsx_path" in sql for sql, _, _ in calls)


def test_calculate_monthly_bonus_posts_two_expenses(monkeypatch):
    from api.osc import accounting_bonus as mod

    inserts = []

    def fake_exec(sql, params=(), fetch="none"):
        if sql.strip().startswith("CREATE TABLE"):
            return {}, {}
        if sql.strip().startswith("SHOW COLUMNS"):
            return [{"Field": "xlsx_path"}], {}
        if "FROM case_transactions t" in sql:
            return [
                {
                    "id": 9,
                    "date": "2026-05-20",
                    "type": "收入",
                    "category": "法扶案件",
                    "description": "1150317-E-009 邱衣萱｜預付酬金",
                    "amount": 10000,
                    "case_type": "消費者債務清理",
                    "case_category": "法律扶助案件",
                    "case_reason": "更生",
                    "case_number": "2026-0024",
                    "client_name": "邱衣萱",
                    "laf_case_no": "1150317-E-009",
                }
            ], {}
        if "income_total" in sql and "expense_total" in sql:
            return {"income_total": 30000, "expense_total": 10000}, {}
        if "SELECT id FROM case_transactions" in sql:
            return None, {}
        if "INSERT INTO case_transactions" in sql:
            inserts.append(params)
            return {"lastrowid": 100 + len(inserts)}, {}
        if "INSERT INTO accounting_monthly_bonus_runs" in sql:
            return {"rowcount": 1}, {}
        return {"rowcount": 0}, {}

    monkeypatch.setattr(mod, "_get_osc_helpers", lambda: fake_exec)
    monkeypatch.setattr(mod, "refresh_accounting_import_for_period", lambda *a, **k: [{"ok": True, "month": "2026-05"}])
    result = mod.calculate_monthly_bonus(month="2026-05", commit=True, refresh_import=True)

    assert result["status"] == "posted"
    assert result["legal_aid_debt_fee_total"] == 10000
    assert result["legal_aid_bonus_amount"] == 5000
    assert result["balance_after_laf_bonus"] == 15000
    assert result["case_bonus_pool"] == 7500
    assert result["case_bonus_employee_amount"] == 3750
    assert result["final_expense_total"] == 18750
    assert len(inserts) == 2
    assert "法扶消債酬金獎金" in inserts[0][5]
    assert "案件獎金" in inserts[1][5]


def test_monthly_bonus_route_preview(monkeypatch):
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

    def fake_calc(**kwargs):
        assert kwargs["month"] == "2026-05"
        assert kwargs["commit"] is False
        return {"ok": True, "month": "2026-05", "status": "waiting_laf_fee"}

    monkeypatch.setattr("api.osc.accounting_bonus.calculate_monthly_bonus", fake_calc)
    resp = app.test_client().get("/api/osc/accounting/monthly-bonus?month=2026-05")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "waiting_laf_fee"
