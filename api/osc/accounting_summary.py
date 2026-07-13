from __future__ import annotations

from typing import Any, Callable


ExecFn = Callable[[str, tuple[Any, ...], str], tuple[Any, dict[str, Any]]]


def accounting_summary_filters(
    *,
    case_id: str = "",
    start_date: str = "",
    end_date: str = "",
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if case_id:
        clauses.append("(case_id=%s OR case_id IN (SELECT id FROM cases WHERE case_number=%s))")
        params.extend([case_id, case_id])
    if start_date:
        clauses.append("date >= %s")
        params.append(start_date)
    if end_date:
        clauses.append("date <= %s")
        params.append(end_date)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def accounting_totals_sql(where: str = "") -> str:
    return (
        "SELECT COUNT(*) AS tx_count, "
        "COALESCE(SUM(CASE WHEN type LIKE '收入%%' THEN ABS(amount) WHEN amount>=0 AND type NOT LIKE '支出%%' THEN amount ELSE 0 END),0) AS income_total, "
        "COALESCE(SUM(CASE WHEN type LIKE '支出%%' THEN ABS(amount) WHEN amount<0 THEN ABS(amount) ELSE 0 END),0) AS expense_total, "
        "COALESCE(SUM(CASE WHEN type LIKE '支出%%' THEN -ABS(amount) WHEN type LIKE '收入%%' THEN ABS(amount) ELSE amount END),0) AS net_total "
        "FROM case_transactions"
        + where
    )


def accounting_by_category_sql(where: str = "") -> str:
    signed_amount = "CASE WHEN type LIKE '支出%%' THEN -ABS(amount) WHEN type LIKE '收入%%' THEN ABS(amount) ELSE amount END"
    return (
        "SELECT COALESCE(category,'未分類') AS category, COUNT(*) AS tx_count, "
        f"COALESCE(SUM({signed_amount}),0) AS total "
        "FROM case_transactions"
        + where
        + f" GROUP BY COALESCE(category,'未分類') ORDER BY ABS(COALESCE(SUM({signed_amount}),0)) DESC LIMIT 20"
    )


def load_accounting_summary(
    exec_fn: ExecFn,
    *,
    case_id: str = "",
    start_date: str = "",
    end_date: str = "",
) -> dict[str, Any]:
    where, params = accounting_summary_filters(
        case_id=str(case_id or "").strip(),
        start_date=str(start_date or "").strip(),
        end_date=str(end_date or "").strip(),
    )
    totals, _ = exec_fn(accounting_totals_sql(where), tuple(params), fetch="one")
    by_category, _ = exec_fn(accounting_by_category_sql(where), tuple(params), fetch="all")
    return {"totals": totals or {}, "by_category": by_category or []}
