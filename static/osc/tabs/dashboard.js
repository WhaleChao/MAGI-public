/* tabs/dashboard.js – Dashboard loading/rendering */
async function loadDashboard() {
    try {
        state.dashboard = await api("/api/osc/dashboard");
    } catch (e) {
        console.warn("loadDashboard failed:", e);
        renderDashboardLoadError(e);
        if (typeof loadSaasWorkbench === "function") {
            await loadSaasWorkbench({ embedded: true });
        }
        return;
    }
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
        (data.recent_cases || []).map(r => {
            const status = typeof caseDisplayStatus === "function" ? caseDisplayStatus(r) : (r.status || "");
            return `<tr><td>${esc(r.case_number)}</td><td>${esc(r.client_name)}</td><td>${esc(r.case_reason)}</td><td>${esc(status)}</td></tr>`;
        }),
        4,
        "沒有案件資料"
    );
    const splitFallback = splitDashboardTodosBySource(data.pending_todos || []);
    const oscTodos = Array.isArray(data.pending_osc_todos) ? data.pending_osc_todos : splitFallback.osc;
    const calendarTodos = Array.isArray(data.pending_calendar_todos) ? data.pending_calendar_todos : splitFallback.calendar;
    updateDashboardTodoSummary("dashboardOscTodosSummary", "OSC 建立待辦", oscTodos.length, "來源：OSC 手動或 PDF 建立待辦（排除 Google 日曆匯入）");
    updateDashboardTodoSummary("dashboardCalendarTodosSummary", "案件行程同步紀錄", calendarTodos.length, "只顯示已配對案件的 MAGI／Google 同步資料，不是完整 Google 日曆");
    renderDashboardTodos("dashboardOscTodosBody", oscTodos, "目前沒有 OSC 建立待辦");
    renderDashboardTodos("dashboardCalendarTodosBody", calendarTodos, "目前沒有案件行程同步紀錄");
    renderSimpleRows(
        "dashboardCalendarBody",
        (data.upcoming_calendar || []).map(r => `<tr><td>${esc(r.start_date)}</td><td>${esc(r.title)}</td><td>${esc(r.case_number || "")}</td><td>${esc(r.location || "")}</td></tr>`),
        4,
        "目前沒有近期案件行程同步紀錄"
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
        "目前沒有 PDF 產生紀錄"
    );
    if (typeof loadSaasWorkbench === "function") {
        await loadSaasWorkbench({ embedded: true });
    }
}

function renderDashboardLoadError(error) {
    const message = `載入失敗：${esc(error?.message || "請稍後重試")}`;
    const ids = [
        "dashActiveCases",
        "dashLegalAidCases",
        "dashRevenue",
        "dashExpense",
        "dashClosedRegular",
        "dashClosedLaf",
    ];
    ids.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.textContent = "-";
    });
    const windowEl = document.getElementById("dashboardWindow");
    if (windowEl) windowEl.textContent = message;
    [
        ["dashboardCasesBody", 4],
        ["dashboardOscTodosBody", 5],
        ["dashboardCalendarTodosBody", 5],
        ["dashboardCalendarBody", 4],
        ["dashboardActivityBody", 4],
        ["dashboardPdfLogBody", 5],
    ].forEach(([id, cols]) => renderSimpleRows(id, [], cols, message));
    updateDashboardTodoSummary("dashboardOscTodosSummary", "OSC 建立待辦", "-", message);
    updateDashboardTodoSummary("dashboardCalendarTodosSummary", "行事曆事件", "-", message);
}

function updateDashboardTodoSummary(id, label, count, detail) {
    const el = document.getElementById(id);
    const numeric = Number(count);
    const countText = Number.isFinite(numeric) ? String(numeric) : String(count || "-");
    if (el) el.textContent = `${label} ${countText} 筆｜${detail}`;
}

function renderDashboardTodos(bodyId, rows, emptyText) {
    renderSimpleRows(
        bodyId,
        (rows || []).map(r => `<tr>
            <td style="white-space:nowrap">${esc(r.todo_date || "")} ${esc(r.todo_time || "")}</td>
            <td style="white-space:nowrap">${esc(r.case_number)}</td>
            <td style="white-space:nowrap">${esc(r.todo_type)}</td>
            <td>${esc(shortText(r.description, 60))}</td>
            <td><button class="btn" data-act="todo-complete" data-id="${Number(r.id)}">已完成</button></td>
        </tr>`),
        5,
        emptyText
    );
}

function splitDashboardTodosBySource(rows) {
    const split = { osc: [], calendar: [] };
    (rows || []).forEach(row => {
        const source = String(row.source_file || "").trim();
        const fallbackCalendar = source.startsWith("gcal_import") || String(row.todo_type || "").trim() === "行事曆事件";
        const isCalendar = typeof oscTodoIsCalendarSource === "function" ? oscTodoIsCalendarSource(row) : fallbackCalendar;
        if (isCalendar) split.calendar.push(row);
        else split.osc.push(row);
    });
    return split;
}
