/* tabs/accounting.js – Transactions + expense defaults + recurring + quotations */
async function loadTransactions() {
    const q = encodeURIComponent((document.getElementById("accountingQ").value || "").trim());
    const caseNumber = encodeURIComponent((document.getElementById("accountingCaseNumber").value || "").trim());
    const startDate = encodeURIComponent((document.getElementById("accountingStartDate").value || "").trim());
    const endDate = encodeURIComponent((document.getElementById("accountingEndDate").value || "").trim());
    const data = await api(`/api/osc/accounting/transactions?limit=500&q=${q}&case_number=${caseNumber}&start_date=${startDate}&end_date=${endDate}`);
    state.transactions = data.items || [];
    renderTransactions();
}

function renderTransactions() {
    const body = document.getElementById("txBody");
    if (!state.transactions.length) {
        body.innerHTML = `<tr><td colspan="7" class="muted">沒有帳務資料</td></tr>`;
    } else {
        const sorted = applySort([...state.transactions], state.sort.col, state.sort.dir, state.sort.type);
        body.innerHTML = sorted.map(r => `
        <tr>
            <td>${esc(r.date)}</td>
            <td>${esc(r.case_number || r.case_id)}</td>
            <td>${esc(r.type)} / ${esc(r.sub_type || "")}</td>
            <td>${esc(r.category)}</td>
            <td>${esc(r.description)}</td>
            <td>${fmtAmount(r.amount)}</td>
            <td class="actions">
                <button class="btn" data-act="tx-edit" data-id="${Number(r.id)}">編輯</button>
                <button class="btn danger" data-act="tx-del" data-id="${Number(r.id)}">刪除</button>
            </td>
        </tr>
    `).join("");
    }
    const ts = document.querySelectorAll("#accounting th[data-sort]");
    ts.forEach(th => {
        th.innerHTML = th.innerHTML.replace(/ [▲▼]/g, "") + renderSortArrow(th.dataset.sort);
    });
    loadAccountingSummary();
}

async function loadAccountingSummary() {
    const caseNumber = encodeURIComponent((document.getElementById("accountingCaseNumber").value || "").trim());
    const startDate = encodeURIComponent((document.getElementById("accountingStartDate").value || "").trim());
    const endDate = encodeURIComponent((document.getElementById("accountingEndDate").value || "").trim());
    const data = await api(`/api/osc/accounting/summary?case_number=${caseNumber}&start_date=${startDate}&end_date=${endDate}`);
    const t = data.totals || {};
    document.getElementById("txCount").textContent = fmtAmount(t.tx_count || 0);
    document.getElementById("txIncome").textContent = fmtAmount(t.income_total || 0);
    document.getElementById("txExpense").textContent = fmtAmount(t.expense_total || 0);
    document.getElementById("txNet").textContent = fmtAmount(t.net_total || 0);
    const top = (data.by_category || [])[0];
    document.getElementById("txTopCategory").textContent = top ? `${top.category} (${fmtAmount(top.total)})` : "-";
}

async function applyAccountingPeriod() {
    const now = new Date();
    const y = now.getFullYear();
    const m = now.getMonth(); // 0-based
    let start, end;
    if (now.getDate() >= 26) {
        start = new Date(y, m, 26);
        end = new Date(y, m + 1, 25);
    } else {
        start = new Date(y, m - 1, 26);
        end = new Date(y, m, 25);
    }
    const toISO = d => d.toISOString().slice(0, 10);
    document.getElementById("accountingStartDate").value = toISO(start);
    document.getElementById("accountingEndDate").value = toISO(end);
    await loadTransactions();
}

async function editTransaction(id) {
    const data = await api(`/api/osc/accounting/transactions/${id}`);
    const x = data.item || {};
    writeFields("tx_", x, ["id", "case_id", "date", "type", "sub_type", "category", "amount", "description"]);
}

async function delTransaction(id) {
    if (!confirm(`確定刪除帳務紀錄 ${id}？`)) return;
    await api(`/api/osc/accounting/transactions/${id}`, "DELETE");
    await loadTransactions();
}

async function saveTransaction() {
    const p = readFields(["tx_id", "tx_case_id", "tx_date", "tx_type", "tx_sub_type", "tx_category", "tx_amount", "tx_description"]);
    const body = {
        case_id: p.tx_case_id,
        date: p.tx_date,
        type: p.tx_type,
        sub_type: p.tx_sub_type,
        category: p.tx_category,
        amount: p.tx_amount,
        description: p.tx_description,
    };
    if (!body.case_id) return alert("請輸入案件編號");
    if ((p.tx_id || "").trim()) await api(`/api/osc/accounting/transactions/${Number(p.tx_id)}`, "PUT", body);
    else await api(`/api/osc/accounting/transactions`, "POST", body);
    clearFields(["tx_id", "tx_case_id", "tx_date", "tx_type", "tx_sub_type", "tx_category", "tx_amount", "tx_description"]);
    await loadTransactions();
    await loadMeta();
}

async function loadExpenseDefaults() {
    const q = encodeURIComponent((document.getElementById("txDefQ").value || "").trim());
    const data = await api(`/api/osc/accounting/defaults?limit=400&q=${q}`);
    state.txDefaults = data.items || [];
    const body = document.getElementById("txDefBody");
    if (!state.txDefaults.length) {
        body.innerHTML = `<tr><td colspan="5" class="muted">沒有預設帳務項目</td></tr>`;
        return;
    }
    body.innerHTML = state.txDefaults.map(r => `
    <tr>
        <td>${esc(r.id)}</td>
        <td>${esc(r.category)}</td>
        <td>${esc(r.default_description || "")}</td>
        <td>${fmtAmount(r.default_amount)}</td>
        <td class="actions">
            <button class="btn" data-act="tx-def-edit" data-id="${Number(r.id)}">編輯</button>
            <button class="btn danger" data-act="tx-def-del" data-id="${Number(r.id)}">刪除</button>
        </td>
    </tr>
`).join("");
}

async function editExpenseDefault(id) {
    const data = await api(`/api/osc/accounting/defaults/${Number(id)}`);
    const x = data.item || {};
    document.getElementById("txDefId").value = x.id || "";
    document.getElementById("txDefCategory").value = x.category || "";
    document.getElementById("txDefAmount").value = x.default_amount || 0;
    document.getElementById("txDefDescription").value = x.default_description || "";
}

async function saveExpenseDefault() {
    const body = {
        category: (document.getElementById("txDefCategory").value || "").trim(),
        default_amount: (document.getElementById("txDefAmount").value || "0").trim(),
        default_description: (document.getElementById("txDefDescription").value || "").trim(),
    };
    const id = (document.getElementById("txDefId").value || "").trim();
    if (!body.category) return alert("請輸入分類");
    if (id) await api(`/api/osc/accounting/defaults/${Number(id)}`, "PUT", body);
    else await api(`/api/osc/accounting/defaults`, "POST", body);
    ["txDefId", "txDefCategory", "txDefAmount", "txDefDescription"].forEach(x => {
        const el = document.getElementById(x); if (el) el.value = "";
    });
    await loadExpenseDefaults();
    await loadMeta();
}

async function delExpenseDefault(id) {
    if (!confirm(`確定刪除預設帳務項目 ${id}？`)) return;
    await api(`/api/osc/accounting/defaults/${Number(id)}`, "DELETE");
    await loadExpenseDefaults();
    await loadMeta();
}

async function loadRecurringExpenses() {
    const q = encodeURIComponent((document.getElementById("txRecurringQ").value || "").trim());
    const only = document.getElementById("txRecurringOnlyActive").checked ? "1" : "0";
    const data = await api(`/api/osc/accounting/recurring?limit=400&q=${q}&only_active=${only}`);
    state.txRecurring = data.items || [];
    const body = document.getElementById("txRecurringBody");
    if (!state.txRecurring.length) {
        body.innerHTML = `<tr><td colspan="10" class="muted">沒有固定支出資料</td></tr>`;
        return;
    }
    body.innerHTML = state.txRecurring.map(r => `
    <tr>
        <td>${esc(r.id)}</td>
        <td>${esc(r.category)}</td>
        <td>${esc(r.sub_type || "")}</td>
        <td>${esc(r.description || "")}</td>
        <td>${fmtAmount(r.amount)}</td>
        <td>${esc(r.day_of_month || "")}</td>
        <td>${esc(r.start_date || "")} ~ ${esc(r.end_date || "")}</td>
        <td>${esc(r.is_active)}</td>
        <td>${esc(r.last_generated_month || "")}</td>
        <td class="actions">
            <button class="btn" data-act="tx-rec-edit" data-id="${Number(r.id)}">編輯</button>
            <button class="btn danger" data-act="tx-rec-del" data-id="${Number(r.id)}">刪除</button>
        </td>
    </tr>
`).join("");
}

async function editRecurringExpense(id) {
    const data = await api(`/api/osc/accounting/recurring/${Number(id)}`);
    const x = data.item || {};
    document.getElementById("txRecurringId").value = x.id || "";
    document.getElementById("txRecurringCategory").value = x.category || "";
    document.getElementById("txRecurringSubType").value = x.sub_type || "";
    document.getElementById("txRecurringDescription").value = x.description || "";
    document.getElementById("txRecurringAmount").value = x.amount || 0;
    document.getElementById("txRecurringDay").value = x.day_of_month || 1;
    document.getElementById("txRecurringStartDate").value = x.start_date || "";
    document.getElementById("txRecurringEndDate").value = x.end_date || "";
    document.getElementById("txRecurringActive").value = x.is_active ?? 1;
    document.getElementById("txRecurringLastMonth").value = x.last_generated_month || "";
}

async function saveRecurringExpense() {
    const body = {
        category: (document.getElementById("txRecurringCategory").value || "").trim(),
        sub_type: (document.getElementById("txRecurringSubType").value || "").trim(),
        description: (document.getElementById("txRecurringDescription").value || "").trim(),
        amount: (document.getElementById("txRecurringAmount").value || "0").trim(),
        day_of_month: (document.getElementById("txRecurringDay").value || "1").trim(),
        start_date: (document.getElementById("txRecurringStartDate").value || "").trim(),
        end_date: (document.getElementById("txRecurringEndDate").value || "").trim(),
        is_active: (document.getElementById("txRecurringActive").value || "1").trim(),
        last_generated_month: (document.getElementById("txRecurringLastMonth").value || "").trim(),
    };
    const id = (document.getElementById("txRecurringId").value || "").trim();
    if (!body.category) return alert("請輸入分類");
    if (id) await api(`/api/osc/accounting/recurring/${Number(id)}`, "PUT", body);
    else await api(`/api/osc/accounting/recurring`, "POST", body);
    ["txRecurringId", "txRecurringCategory", "txRecurringSubType", "txRecurringDescription", "txRecurringAmount", "txRecurringDay", "txRecurringStartDate", "txRecurringEndDate", "txRecurringActive", "txRecurringLastMonth"].forEach(x => {
        const el = document.getElementById(x); if (el) el.value = "";
    });
    await loadRecurringExpenses();
    await loadMeta();
}

async function delRecurringExpense(id) {
    if (!confirm(`確定刪除固定支出 ${id}？`)) return;
    await api(`/api/osc/accounting/recurring/${Number(id)}`, "DELETE");
    await loadRecurringExpenses();
    await loadMeta();
}

async function loadQuotations() {
    const q = encodeURIComponent((document.getElementById("qtQ").value || "").trim());
    const status = encodeURIComponent((document.getElementById("qtStatusFilter").value || "").trim());
    const data = await api(`/api/osc/quotations?limit=500&q=${q}&status=${status}`);
    state.quotations = data.items || [];
    const body = document.getElementById("qtBody");
    if (!state.quotations.length) {
        body.innerHTML = `<tr><td colspan="8" class="muted">沒有報價單資料</td></tr>`;
        return;
    }
    body.innerHTML = state.quotations.map(r => `
    <tr>
        <td>${esc(r.id)}</td>
        <td>${esc(r.client_name)}</td>
        <td>${esc(r.project_name)}</td>
        <td>${esc(r.date || "")}</td>
        <td>${esc(r.expiry || "")}</td>
        <td>${esc(r.status || "")}</td>
        <td>${fmtAmount(r.total)}</td>
        <td class="actions">
            <button class="btn" data-act="qt-edit" data-id="${esc(r.id)}">編輯</button>
            <button class="btn danger" data-act="qt-del" data-id="${esc(r.id)}">刪除</button>
        </td>
    </tr>
`).join("");
}

async function editQuotation(id) {
    const data = await api(`/api/osc/quotations/${encodeURIComponent(id)}`);
    const x = data.item || {};
    writeFields("qt_", x, ["id", "client_name", "project_name", "contact", "phone", "email", "address", "tax_id", "date", "expiry", "subtotal", "discount", "tax", "total", "status", "notes", "items", "extended_data"]);
}

async function saveQuotation() {
    const p = readFields(["qt_id", "qt_client_name", "qt_project_name", "qt_contact", "qt_phone", "qt_email", "qt_address", "qt_tax_id", "qt_date", "qt_expiry", "qt_subtotal", "qt_discount", "qt_tax", "qt_total", "qt_status", "qt_notes", "qt_items", "qt_extended_data"]);
    const body = {
        id: p.qt_id,
        client_name: p.qt_client_name,
        project_name: p.qt_project_name,
        contact: p.qt_contact,
        phone: p.qt_phone,
        email: p.qt_email,
        address: p.qt_address,
        tax_id: p.qt_tax_id,
        date: p.qt_date,
        expiry: p.qt_expiry,
        subtotal: p.qt_subtotal,
        discount: p.qt_discount,
        tax: p.qt_tax,
        total: p.qt_total,
        status: p.qt_status || "draft",
        notes: p.qt_notes,
        items: p.qt_items,
        extended_data: p.qt_extended_data,
    };
    if (!body.client_name || !body.project_name) return alert("請輸入當事人與專案名稱");
    if ((body.id || "").trim()) await api(`/api/osc/quotations/${encodeURIComponent(body.id)}`, "PUT", body);
    else await api(`/api/osc/quotations`, "POST", body);
    clearFields(["qt_id", "qt_client_name", "qt_project_name", "qt_contact", "qt_phone", "qt_email", "qt_address", "qt_tax_id", "qt_date", "qt_expiry", "qt_subtotal", "qt_discount", "qt_tax", "qt_total", "qt_status", "qt_notes", "qt_items", "qt_extended_data"]);
    await loadQuotations();
    await loadMeta();
}

async function delQuotation(id) {
    if (!confirm(`確定刪除報價單 ${id}？`)) return;
    await api(`/api/osc/quotations/${encodeURIComponent(id)}`, "DELETE");
    await loadQuotations();
    await loadMeta();
}

async function loadQuotationTemplates() {
    const q = encodeURIComponent((document.getElementById("qtTplQ").value || "").trim());
    const data = await api(`/api/osc/quotation-templates?limit=400&q=${q}`);
    state.quotationTemplates = data.items || [];
    const body = document.getElementById("qtTplBody");
    if (!state.quotationTemplates.length) {
        body.innerHTML = `<tr><td colspan="6" class="muted">沒有報價模板</td></tr>`;
        return;
    }
    body.innerHTML = state.quotationTemplates.map(r => `
    <tr>
        <td>${esc(r.id)}</td>
        <td>${esc(r.name)}</td>
        <td>${esc(r.is_default)}</td>
        <td>${esc(r.description || "")}</td>
        <td>${esc(r.updated_date || r.created_date || "")}</td>
        <td class="actions">
            <button class="btn" data-act="qt-tpl-edit" data-id="${Number(r.id)}">編輯</button>
            <button class="btn danger" data-act="qt-tpl-del" data-id="${Number(r.id)}">刪除</button>
        </td>
    </tr>
`).join("");
}

async function editQuotationTemplate(id) {
    const data = await api(`/api/osc/quotation-templates/${Number(id)}`);
    const x = data.item || {};
    document.getElementById("qtTplId").value = x.id || "";
    document.getElementById("qtTplName").value = x.name || "";
    document.getElementById("qtTplDefault").value = x.is_default ?? 0;
    document.getElementById("qtTplDescription").value = x.description || "";
    document.getElementById("qtTplItems").value = x.items || "";
    document.getElementById("qtTplNotes").value = x.notes || "";
}

async function saveQuotationTemplate() {
    const id = (document.getElementById("qtTplId").value || "").trim();
    const body = {
        name: (document.getElementById("qtTplName").value || "").trim(),
        is_default: (document.getElementById("qtTplDefault").value || "0").trim(),
        description: (document.getElementById("qtTplDescription").value || "").trim(),
        items: (document.getElementById("qtTplItems").value || "").trim(),
        notes: (document.getElementById("qtTplNotes").value || "").trim(),
    };
    if (!body.name) return alert("請輸入模板名稱");
    if (id) await api(`/api/osc/quotation-templates/${Number(id)}`, "PUT", body);
    else await api(`/api/osc/quotation-templates`, "POST", body);
    ["qtTplId", "qtTplName", "qtTplDefault", "qtTplDescription", "qtTplItems", "qtTplNotes"].forEach(x => {
        const el = document.getElementById(x); if (el) el.value = "";
    });
    await loadQuotationTemplates();
    await loadMeta();
}

async function delQuotationTemplate(id) {
    if (!confirm(`確定刪除報價模板 ${id}？`)) return;
    await api(`/api/osc/quotation-templates/${Number(id)}`, "DELETE");
    await loadQuotationTemplates();
    await loadMeta();
}
