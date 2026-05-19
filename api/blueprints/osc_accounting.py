"""
OSC Accounting Blueprint
========================
Handles /api/osc/accounting/* routes: transactions, summary, defaults, recurring.
Migrated from server.py to reduce monolith size.
"""

from datetime import date

from flask import Blueprint, request, jsonify
from flask_login import login_required

osc_accounting_bp = Blueprint("osc_accounting", __name__)


def _get_osc_helpers():
    """Lazy import OSC helpers from server.py to avoid circular imports."""
    from api.osc.utils import _osc_exec, _osc_text, _osc_log_activity, _osc_resolve_case_id, _osc_safe_int
    return _osc_exec, _osc_text, _osc_log_activity, _osc_resolve_case_id, _osc_safe_int


# ── Transactions ─────────────────────────────────────────────────────

@osc_accounting_bp.route("/api/osc/accounting/transactions", methods=["GET", "POST"])
@login_required
def osc_accounting_transactions_api():
    _osc_exec, _osc_text, _osc_log_activity, _osc_resolve_case_id, _osc_safe_int = _get_osc_helpers()
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        case_id = (request.args.get("case_number") or request.args.get("case_id") or "").strip()
        limit = max(1, min(1000, int(request.args.get("limit") or "300")))
        start_date = (request.args.get("start_date") or "").strip()
        end_date = (request.args.get("end_date") or "").strip()
        where = []
        params = []
        if case_id:
            where.append("(t.case_id=%s OR t.case_id IN (SELECT id FROM cases WHERE case_number=%s))")
            params.extend([case_id, case_id])
        if start_date:
            where.append("t.date >= %s")
            params.append(start_date)
        if end_date:
            where.append("t.date <= %s")
            params.append(end_date)
        if q:
            like = f"%{q}%"
            where.append("(t.case_id LIKE %s OR t.type LIKE %s OR t.sub_type LIKE %s OR t.category LIKE %s OR t.description LIKE %s)")
            params.extend([like, like, like, like, like])
        sql = """
            SELECT t.id, t.case_id, c.case_number, t.date, t.type, t.sub_type, t.category, t.description, t.amount
            FROM case_transactions t
            LEFT JOIN cases c ON c.id = t.case_id
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY date DESC, id DESC LIMIT %s"
        params.append(limit)
        rows, _ = _osc_exec(sql, tuple(params), fetch="all")
        return jsonify({"ok": True, "items": rows})

    payload = request.get_json() or {}
    case_id = _osc_resolve_case_id(payload.get("case_id") or payload.get("case_number") or "")
    tx_date = str(payload.get("date") or "").strip() or str(date.today())
    if not case_id:
        return jsonify({"ok": False, "error": "case_id required"}), 400
    try:
        amount = float(payload.get("amount") or 0)
    except Exception:
        return jsonify({"ok": False, "error": "amount invalid"}), 400
    cols = ["case_id", "date", "type", "sub_type", "category", "description", "amount"]
    vals = [
        case_id,
        tx_date,
        str(payload.get("type") or "").strip() or None,
        str(payload.get("sub_type") or "").strip() or None,
        str(payload.get("category") or "").strip() or None,
        str(payload.get("description") or "").strip() or None,
        amount,
    ]
    result, _ = _osc_exec(
        f"INSERT INTO case_transactions ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})",
        tuple(vals),
        fetch="none",
    )
    return jsonify({"ok": True, "result": result})


@osc_accounting_bp.route("/api/osc/accounting/transactions/<int:row_id>", methods=["GET", "PUT", "DELETE"])
@login_required
def osc_accounting_transaction_detail_api(row_id):
    _osc_exec, _osc_text, _osc_log_activity, _osc_resolve_case_id, _osc_safe_int = _get_osc_helpers()
    if request.method == "GET":
        row, _ = _osc_exec("SELECT * FROM case_transactions WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    if request.method == "DELETE":
        result, _ = _osc_exec("DELETE FROM case_transactions WHERE id=%s", (row_id,), fetch="none")
        return jsonify({"ok": True, "result": result})
    payload = request.get_json() or {}
    allowed = ["case_id", "date", "type", "sub_type", "category", "description", "amount"]
    sets = []
    vals = []
    for k in allowed:
        if k not in payload:
            continue
        sets.append(f"{k}=%s")
        if k == "amount":
            try:
                vals.append(float(payload.get(k) or 0))
            except Exception:
                return jsonify({"ok": False, "error": "amount invalid"}), 400
        else:
            v = (payload.get(k) or "").strip() or None
            if k == "case_id" and v:
                v = _osc_resolve_case_id(v)
            vals.append(v)
    if not sets:
        return jsonify({"ok": False, "error": "no fields"}), 400
    vals.append(row_id)
    result, _ = _osc_exec(f"UPDATE case_transactions SET {','.join(sets)} WHERE id=%s", tuple(vals), fetch="none")
    return jsonify({"ok": True, "result": result})


# ── Summary ──────────────────────────────────────────────────────────

@osc_accounting_bp.route("/api/osc/accounting/summary", methods=["GET"])
@login_required
def osc_accounting_summary_api():
    _osc_exec, _osc_text, _osc_log_activity, _osc_resolve_case_id, _osc_safe_int = _get_osc_helpers()
    case_id = (request.args.get("case_number") or request.args.get("case_id") or "").strip()
    start_date = (request.args.get("start_date") or "").strip()
    end_date = (request.args.get("end_date") or "").strip()
    where = ""
    params = []
    clauses = []
    if case_id:
        clauses.append("(case_id=%s OR case_id IN (SELECT id FROM cases WHERE case_number=%s))")
        params.extend([case_id, case_id])
    if start_date:
        clauses.append("date >= %s")
        params.append(start_date)
    if end_date:
        clauses.append("date <= %s")
        params.append(end_date)
    if clauses:
        where = " WHERE " + " AND ".join(clauses)
    total_sql = (
        "SELECT COUNT(*) AS tx_count, "
        "COALESCE(SUM(CASE WHEN type LIKE '收入%%' THEN ABS(amount) WHEN amount>=0 AND type NOT LIKE '支出%%' THEN amount ELSE 0 END),0) AS income_total, "
        "COALESCE(SUM(CASE WHEN type LIKE '支出%%' THEN ABS(amount) WHEN amount<0 THEN ABS(amount) ELSE 0 END),0) AS expense_total, "
        "COALESCE(SUM(CASE WHEN type LIKE '支出%%' THEN -ABS(amount) WHEN type LIKE '收入%%' THEN ABS(amount) ELSE amount END),0) AS net_total "
        "FROM case_transactions"
        + where
    )
    totals, _ = _osc_exec(total_sql, tuple(params), fetch="one")
    by_category_sql = (
        "SELECT COALESCE(category,'未分類') AS category, COUNT(*) AS tx_count, "
        "COALESCE(SUM(CASE WHEN type LIKE '支出%%' THEN -ABS(amount) WHEN type LIKE '收入%%' THEN ABS(amount) ELSE amount END),0) AS total "
        "FROM case_transactions"
        + where
        + " GROUP BY COALESCE(category,'未分類') ORDER BY ABS(COALESCE(SUM(CASE WHEN type LIKE '支出%%' THEN -ABS(amount) WHEN type LIKE '收入%%' THEN ABS(amount) ELSE amount END),0)) DESC LIMIT 20"
    )
    by_category, _ = _osc_exec(by_category_sql, tuple(params), fetch="all")
    return jsonify({"ok": True, "totals": totals or {}, "by_category": by_category or []})


@osc_accounting_bp.route("/api/osc/accounting/import/google-sheet", methods=["GET", "POST"])
@login_required
def osc_accounting_google_sheet_import_api():
    from api.osc.accounting_sheet_import import (
        DEFAULT_ACCOUNT_HINT,
        DEFAULT_GID,
        DEFAULT_SPREADSHEET_ID,
        SheetsAuthorizationRequired,
        run_import,
    )

    payload = request.get_json(silent=True) or {}
    month = (payload.get("month") or request.args.get("month") or "").strip() or None
    commit = bool(payload.get("commit")) if request.method == "POST" else False
    auth = bool(payload.get("auth")) if request.method == "POST" else False
    try:
        result = run_import(
            month=month,
            dry_run=not commit,
            spreadsheet_id=(payload.get("spreadsheet_id") or request.args.get("spreadsheet_id") or DEFAULT_SPREADSHEET_ID),
            gid=int(payload.get("gid") or request.args.get("gid") or DEFAULT_GID),
            interactive=auth,
            account_hint=(payload.get("account_hint") or request.args.get("account_hint") or DEFAULT_ACCOUNT_HINT),
        )
    except SheetsAuthorizationRequired as exc:
        return jsonify({"ok": False, "error": "auth_required", "message": str(exc)}), 428
    except Exception as exc:
        return jsonify({"ok": False, "error": type(exc).__name__, "message": str(exc)}), 500
    return jsonify(result)


# ── Expense Defaults ─────────────────────────────────────────────────

@osc_accounting_bp.route("/api/osc/accounting/defaults", methods=["GET", "POST"])
@login_required
def osc_accounting_defaults_api():
    _osc_exec, _osc_text, _osc_log_activity, _osc_resolve_case_id, _osc_safe_int = _get_osc_helpers()
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        limit = max(1, min(1000, int(request.args.get("limit") or "300")))
        sql = "SELECT id, category, default_description, default_amount FROM expense_defaults WHERE 1=1 "
        params = []
        if q:
            like = f"%{q}%"
            sql += "AND (category LIKE %s OR default_description LIKE %s) "
            params.extend([like, like])
        sql += "ORDER BY category ASC, id DESC LIMIT %s"
        params.append(limit)
        rows, _ = _osc_exec(sql, tuple(params), fetch="all")
        return jsonify({"ok": True, "items": rows})
    payload = request.get_json() or {}
    category = (payload.get("category") or "").strip()
    if not category:
        return jsonify({"ok": False, "error": "category required"}), 400
    try:
        amt = float(payload.get("default_amount") or 0)
    except Exception:
        return jsonify({"ok": False, "error": "default_amount invalid"}), 400
    result, _ = _osc_exec(
        "INSERT INTO expense_defaults (category, default_description, default_amount) VALUES (%s,%s,%s)",
        (category, (payload.get("default_description") or "").strip() or None, amt),
        fetch="none",
    )
    return jsonify({"ok": True, "result": result})


@osc_accounting_bp.route("/api/osc/accounting/defaults/<int:row_id>", methods=["GET", "PUT", "DELETE"])
@login_required
def osc_accounting_default_detail_api(row_id):
    _osc_exec, _osc_text, _osc_log_activity, _osc_resolve_case_id, _osc_safe_int = _get_osc_helpers()
    if request.method == "GET":
        row, _ = _osc_exec("SELECT * FROM expense_defaults WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    if request.method == "DELETE":
        result, _ = _osc_exec("DELETE FROM expense_defaults WHERE id=%s", (row_id,), fetch="none")
        return jsonify({"ok": True, "result": result})
    payload = request.get_json() or {}
    sets, vals = [], []
    for k in ["category", "default_description", "default_amount"]:
        if k not in payload:
            continue
        sets.append(f"{k}=%s")
        if k == "default_amount":
            try:
                vals.append(float(payload.get(k) or 0))
            except Exception:
                return jsonify({"ok": False, "error": "default_amount invalid"}), 400
        else:
            vals.append((payload.get(k) or "").strip() or None)
    if not sets:
        return jsonify({"ok": False, "error": "no fields"}), 400
    vals.append(row_id)
    result, _ = _osc_exec(f"UPDATE expense_defaults SET {','.join(sets)} WHERE id=%s", tuple(vals), fetch="none")
    return jsonify({"ok": True, "result": result})


# ── Recurring Expenses ───────────────────────────────────────────────

@osc_accounting_bp.route("/api/osc/accounting/recurring", methods=["GET", "POST"])
@login_required
def osc_accounting_recurring_api():
    _osc_exec, _osc_text, _osc_log_activity, _osc_resolve_case_id, _osc_safe_int = _get_osc_helpers()
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        only_active = str(request.args.get("only_active") or "0").strip().lower() in {"1", "true", "yes", "on"}
        limit = max(1, min(1000, int(request.args.get("limit") or "300")))
        sql = (
            "SELECT id, category, sub_type, description, amount, day_of_month, start_date, end_date, is_active, last_generated_month, created_date "
            "FROM recurring_expenses WHERE 1=1 "
        )
        params = []
        if only_active:
            sql += "AND is_active=1 "
        if q:
            like = f"%{q}%"
            sql += "AND (category LIKE %s OR sub_type LIKE %s OR description LIKE %s) "
            params.extend([like, like, like])
        sql += "ORDER BY is_active DESC, category ASC, id DESC LIMIT %s"
        params.append(limit)
        rows, _ = _osc_exec(sql, tuple(params), fetch="all")
        return jsonify({"ok": True, "items": rows})

    payload = request.get_json() or {}
    category = (payload.get("category") or "").strip()
    if not category:
        return jsonify({"ok": False, "error": "category required"}), 400
    try:
        amount = float(payload.get("amount") or 0)
    except Exception:
        return jsonify({"ok": False, "error": "amount invalid"}), 400
    day_of_month = _osc_safe_int(payload.get("day_of_month"), 1)
    if day_of_month < 1 or day_of_month > 31:
        return jsonify({"ok": False, "error": "day_of_month invalid"}), 400
    is_active = 1 if str(payload.get("is_active") or "").strip().lower() in {"1", "true", "yes", "on"} else 0
    result, _ = _osc_exec(
        "INSERT INTO recurring_expenses (category, sub_type, description, amount, day_of_month, start_date, end_date, is_active, last_generated_month) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            category,
            (payload.get("sub_type") or "").strip() or None,
            (payload.get("description") or "").strip() or None,
            amount,
            day_of_month,
            (payload.get("start_date") or "").strip() or None,
            (payload.get("end_date") or "").strip() or None,
            is_active,
            (payload.get("last_generated_month") or "").strip() or None,
        ),
        fetch="none",
    )
    return jsonify({"ok": True, "result": result})


@osc_accounting_bp.route("/api/osc/accounting/recurring/<int:row_id>", methods=["GET", "PUT", "DELETE"])
@login_required
def osc_accounting_recurring_detail_api(row_id):
    _osc_exec, _osc_text, _osc_log_activity, _osc_resolve_case_id, _osc_safe_int = _get_osc_helpers()
    if request.method == "GET":
        row, _ = _osc_exec("SELECT * FROM recurring_expenses WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    if request.method == "DELETE":
        result, _ = _osc_exec("DELETE FROM recurring_expenses WHERE id=%s", (row_id,), fetch="none")
        return jsonify({"ok": True, "result": result})
    payload = request.get_json() or {}
    allowed = ["category", "sub_type", "description", "amount", "day_of_month", "start_date", "end_date", "is_active", "last_generated_month"]
    sets, vals = [], []
    for k in allowed:
        if k not in payload:
            continue
        sets.append(f"{k}=%s")
        if k in {"amount"}:
            try:
                vals.append(float(payload.get(k) or 0))
            except Exception:
                return jsonify({"ok": False, "error": f"{k} invalid"}), 400
        elif k in {"day_of_month", "is_active"}:
            vals.append(_osc_safe_int(payload.get(k), 0))
        else:
            vals.append((payload.get(k) or "").strip() or None)
    if not sets:
        return jsonify({"ok": False, "error": "no fields"}), 400
    vals.append(row_id)
    result, _ = _osc_exec(f"UPDATE recurring_expenses SET {','.join(sets)} WHERE id=%s", tuple(vals), fetch="none")
    return jsonify({"ok": True, "result": result})


@osc_accounting_bp.route("/api/osc/accounting/recurring/<int:row_id>/sync-generated", methods=["POST"])
@login_required
def osc_accounting_recurring_sync_generated_api(row_id):
    _osc_exec, _osc_text, _osc_log_activity, _osc_resolve_case_id, _osc_safe_int = _get_osc_helpers()
    row, _ = _osc_exec("SELECT * FROM recurring_expenses WHERE id=%s", (row_id,), fetch="one")
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    today = date.today()
    start_date = (payload.get("start_date") or f"{today.year}-01-01").strip()
    end_date = (payload.get("end_date") or str(today)).strip()
    try:
        amount = float(payload.get("amount") if "amount" in payload else row.get("amount") or 0)
    except Exception:
        return jsonify({"ok": False, "error": "amount invalid"}), 400
    category = (payload.get("category") or row.get("category") or "").strip()
    sub_type = (payload.get("sub_type") or row.get("sub_type") or "").strip()
    label = (payload.get("description") or row.get("description") or row.get("sub_type") or row.get("category") or "").strip()
    fixed_description = f"[固定] {label}".strip()
    if not category or not fixed_description:
        return jsonify({"ok": False, "error": "recurring expense is incomplete"}), 400
    result, _ = _osc_exec(
        """
        UPDATE case_transactions
           SET amount=%s, category=%s, sub_type=%s
         WHERE type='支出'
           AND date >= %s
           AND date <= %s
           AND COALESCE(category,'')=%s
           AND COALESCE(sub_type,'')=%s
           AND COALESCE(description,'')=%s
        """,
        (amount, category, sub_type or None, start_date, end_date, row.get("category") or "", row.get("sub_type") or "", fixed_description),
        fetch="none",
    )
    updated, _ = _osc_exec(
        """
        SELECT id, case_id, date, type, sub_type, category, description, amount
          FROM case_transactions
         WHERE type='支出'
           AND date >= %s
           AND date <= %s
           AND COALESCE(category,'')=%s
           AND COALESCE(sub_type,'')=%s
           AND COALESCE(description,'')=%s
         ORDER BY date DESC, id DESC
         LIMIT 80
        """,
        (start_date, end_date, category, sub_type, fixed_description),
        fetch="all",
    )
    return jsonify(
        {
            "ok": True,
            "row_id": row_id,
            "start_date": start_date,
            "end_date": end_date,
            "updated_count": (result or {}).get("rowcount", 0),
            "items": updated or [],
        }
    )
