/* tabs/dashboard.js – Dashboard loading/rendering */
async function loadDashboard() {
    try {
    state.dashboard = await api("/api/osc/dashboard");
    } catch (e) { console.warn("loadDashboard failed:", e); state.dashboard = state.dashboard || {}; }
    const data = state.dashboard || {};
    const s = data.stats || {};
    document.getElementById("dashboardWindow").textContent = `帳務區間：${data.window?.start_date || "-"} ~ ${data.window?.end_date || "-"}`;
    document.getElementById("dashActiveCases").textContent = `${s.active_cases ?? 0}`;
    document.getElementById("dashLegalAidCases").textContent = `${s.legal_aid_cases ?? 0}`;
    document.getElementById("dashRevenue").textContent = fmtAmount(s.monthly_revenue || 0);
    document.getElementById("dashExpense").textContent = fmtAmount(s.monthly_expense || 0);
    document.getElementById("dashClosedRegular").textContent = `${s.closed_regular ?? 0}`;
    document.getElementById("dashClosedLaf").textContent = `${s.closed_legal_aid ?? 0}`;

    renderSimpleRows(
        "dashboardCasesBody",
        (data.recent_cases || []).map(r => `<tr><td>${esc(r.case_number)}</td><td>${esc(r.client_name)}</td><td>${esc(r.case_reason)}</td><td>${esc(r.status)}</td></tr>`),
        4,
        "沒有案件資料"
    );
    renderSimpleRows(
        "dashboardTodosBody",
        (data.pending_todos || []).map(r => `<tr><td>${esc(r.todo_date || "")} ${esc(r.todo_time || "")}</td><td>${esc(r.case_number)}</td><td>${esc(r.todo_type)}</td><td>${esc(shortText(r.description, 60))}</td></tr>`),
        4,
        "目前沒有待辦"
    );
    renderSimpleRows(
        "dashboardCalendarBody",
        (data.upcoming_calendar || []).map(r => `<tr><td>${esc(r.start_date)}</td><td>${esc(r.title)}</td><td>${esc(r.case_number || "")}</td><td>${esc(r.location || "")}</td></tr>`),
        4,
        "目前沒有近期行事曆"
    );
    renderSimpleRows(
        "dashboardActivityBody",
        (data.recent_activity || []).map(r => `<tr><td>${esc(r.timestamp)}</td><td>${esc(r.action)}</td><td>${esc(r.entity_type || "")}</td><td>${esc(r.user || "")}</td></tr>`),
        4,
        "目前沒有活動紀錄"
    );
    renderSimpleRows(
        "dashboardPdfLogBody",
        (data.recent_pdf_logs || []).map(r => `<tr><td>${esc(r.log_timestamp)}</td><td>${esc(r.case_number)}</td><td>${esc(r.file_name || "")}</td><td>${esc(r.status || "")}</td><td>${esc(shortText(r.error_message, 80))}</td></tr>`),
        5,
        "目前沒有 PDF 生成紀錄"
    );
}
