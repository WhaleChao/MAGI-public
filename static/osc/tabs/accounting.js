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

function accountingImportMonthValue() {
    const el = document.getElementById("accountingImportMonth");
    if (!el) return "";
    if (!el.value) el.value = new Date().toISOString().slice(0, 7);
    return el.value;
}

function renderAccountingImportResult(data) {
    const box = document.getElementById("accountingImportResult");
    if (!box) return;
    const conflicts = data.fixed_expense_conflicts || [];
    const skips = data.fixed_expense_skips || [];
    const existing = data.existing_matches || [];
    const duplicates = data.duplicates || [];
    const rows = [
        ["可匯入", data.importable_count || 0],
        ["已匯入過", data.duplicate_count || 0],
        ["DB 已有相同紀錄", data.existing_count || 0],
        ["固定支出已跳過", data.fixed_expense_skip_count || 0],
        ["固定支出金額不一致", data.fixed_expense_conflict_count || 0],
    ];
    const conflictHtml = conflicts.length ? `
        <div class="soft-block" style="margin-top:10px;">
            <strong>需要確認的固定支出</strong>
            <ul>
                ${conflicts.map(x => {
                    const m = x.fixed_expense_match || {};
                    const recurring = (m.recurring || []).map(r => `${esc(r.description || r.sub_type || r.category)}：${fmtAmount(r.amount)}`).join("、");
                    return `<li>${esc(x.date)}｜${esc(x.category || "")}｜${esc(x.description || "")}｜同事表 ${fmtAmount(x.amount)}；固定支出 ${esc(recurring || fmtAmount(m.recurring_amount_total || 0))}</li>`;
                }).join("")}
            </ul>
        </div>` : "";
    const sample = [...skips.slice(0, 6), ...existing.slice(0, 4), ...duplicates.slice(0, 4)];
    const sampleHtml = sample.length ? `
        <div class="muted" style="margin-top:10px;">
            對帳樣本：${sample.map(x => `${esc(x.date || "")} ${esc(x.category || "")} ${esc(x.description || "")}`).join("；")}
        </div>` : "";
    box.innerHTML = `
        <div class="stat-grid">
            ${rows.map(([k, v]) => `<div class="stat-card"><div class="k">${esc(k)}</div><div class="v">${fmtAmount(v)}</div></div>`).join("")}
        </div>
        <div class="muted" style="margin-top:10px;">月份：${esc(data.month || accountingImportMonthValue())}；${data.dry_run ? "目前是預覽，尚未寫入。" : "已完成匯入與對帳。"}</div>
        ${conflictHtml}
        ${sampleHtml}
    `;
}

async function previewAccountingImport() {
    const month = encodeURIComponent(accountingImportMonthValue());
    const data = await api(`/api/osc/accounting/import/google-sheet?month=${month}`);
    renderAccountingImportResult(data);
}

async function runAccountingImport() {
    const month = accountingImportMonthValue();
    if (!confirm(`確定匯入 ${month} 的同事帳務表？固定支出會跳過，不會重複入帳。`)) return;
    const data = await api(`/api/osc/accounting/import/google-sheet`, "POST", { month, commit: true });
    renderAccountingImportResult(data);
    await loadTransactions();
    await loadMeta();
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

async function syncRecurringGenerated() {
    const id = (document.getElementById("txRecurringId").value || "").trim();
    if (!id) return alert("請先選擇一筆固定支出");
    const amount = (document.getElementById("txRecurringAmount").value || "0").trim();
    const description = (document.getElementById("txRecurringDescription").value || "").trim();
    if (!confirm(`確定同步今年已產生的「${description || id}」固定支出金額？`)) return;
    const data = await api(`/api/osc/accounting/recurring/${Number(id)}/sync-generated`, "POST", { amount });
    alert(`已同步 ${data.updated_count || 0} 筆固定支出紀錄`);
    await loadTransactions();
    await loadAccountingSummary();
}

async function delRecurringExpense(id) {
    if (!confirm(`確定刪除固定支出 ${id}？`)) return;
    await api(`/api/osc/accounting/recurring/${Number(id)}`, "DELETE");
    await loadRecurringExpenses();
    await loadMeta();
}

const QT_PRESETS = {
    consult: { item: "法律諮詢", description: "面談、線上諮詢與初步法律分析", unit: "小時", qty: 1, unit_price: 5000 },
    draft: { item: "書狀代擬", description: "起訴狀、答辯狀、聲請狀或存證信函等文書代擬", unit: "件", qty: 1, unit_price: 30000 },
    litigation: { item: "訴訟代理", description: "第一審程序代理；未含裁判費、規費、郵資與差旅費", unit: "審級", qty: 1, unit_price: 80000 },
    contract: { item: "契約審閱", description: "契約風險分析、條款修訂建議與一次討論", unit: "件", qty: 1, unit_price: 20000 },
};

function qtToday() {
    return new Date().toISOString().slice(0, 10);
}

function qtDateAfter(days) {
    const d = new Date();
    d.setDate(d.getDate() + days);
    return d.toISOString().slice(0, 10);
}

function qtStatusLabel(status) {
    const s = String(status || "").trim();
    return ({ draft: "待確認", sent: "待確認", accepted: "已確認", paid: "已收款", rejected: "未成交" }[s] || s || "待確認");
}

function qtNumber(value) {
    const n = Number(String(value ?? "").replace(/,/g, ""));
    return Number.isFinite(n) ? n : 0;
}

function parseQuotationItems(raw) {
    if (Array.isArray(raw)) return raw.map(normalizeQuotationItem);
    const text = String(raw || "").trim();
    if (!text) return [];
    try {
        const parsed = JSON.parse(text);
        if (Array.isArray(parsed)) return parsed.map(normalizeQuotationItem);
        if (Array.isArray(parsed.items)) return parsed.items.map(normalizeQuotationItem);
        if (typeof parsed.items === "string") return parseQuotationItems(parsed.items);
        if (typeof parsed.content === "string") return parseQuotationItems(parsed.content);
    } catch { }
    return text.split(/\n+/).map(line => line.trim()).filter(Boolean).map(line => ({
        item: line, description: "", unit: "式", qty: 1, unit_price: 0, amount: 0
    }));
}

function normalizeQuotationItem(item) {
    const it = item && typeof item === "object" ? item : {};
    const qty = qtNumber(it.qty ?? it.quantity ?? 1) || 1;
    const unitPrice = qtNumber(it.unit_price ?? it.price ?? it.cost ?? 0);
    const rawAmount = it.amount ?? it.subtotal;
    const amount = String(rawAmount ?? "").trim() === "" ? qty * unitPrice : qtNumber(rawAmount);
    return {
        item: String(it.item || it.name || it.service || "").trim(),
        description: String(it.description || it.desc || "").trim(),
        unit: String(it.unit || "式").trim() || "式",
        qty,
        unit_price: unitPrice,
        amount,
    };
}

function quotationRowHtml(item = {}, scope = "qt") {
    const it = normalizeQuotationItem(item);
    const deleteButton = scope === "qtTpl"
        ? '<button class="btn danger" type="button" data-act="qtTpl-item-del">刪除</button>'
        : '<button class="btn danger" type="button" data-act="qt-item-del">刪除</button>';
    return `
        <tr>
            <td><input class="${scope}-item-field" data-key="item" value="${esc(it.item)}" placeholder="例如：書狀代擬"></td>
            <td><input class="${scope}-item-field" data-key="description" value="${esc(it.description)}" placeholder="服務範圍、排除事項"></td>
            <td><input class="${scope}-item-field" data-key="unit" value="${esc(it.unit || "式")}"></td>
            <td><input class="${scope}-item-field" data-key="qty" type="number" step="0.01" value="${esc(it.qty || 1)}"></td>
            <td><input class="${scope}-item-field" data-key="unit_price" type="number" step="1" value="${esc(it.unit_price || 0)}"></td>
            <td><input class="${scope}-item-field" data-key="amount" type="number" step="1" value="${esc(it.amount || 0)}"></td>
            <td>${deleteButton}</td>
        </tr>
    `;
}

function renderQuotationItems(items, scope = "qt") {
    const body = document.getElementById(scope === "qt" ? "qtItemsBody" : "qtTplItemsBody");
    if (!body) return;
    const rows = (items && items.length ? items : [QT_PRESETS.consult]).map(item => quotationRowHtml(item, scope));
    body.innerHTML = rows.join("");
    if (scope === "qt") recalcQuotationTotals();
    else syncTemplateItemsField();
}

function collectQuotationItems(scope = "qt") {
    const body = document.getElementById(scope === "qt" ? "qtItemsBody" : "qtTplItemsBody");
    if (!body) return [];
    return Array.from(body.querySelectorAll("tr")).map(row => {
        const item = {};
        row.querySelectorAll(`[data-key]`).forEach(input => {
            item[input.dataset.key] = input.value;
        });
        const qty = qtNumber(item.qty) || 1;
        const unitPrice = qtNumber(item.unit_price);
        const amount = qtNumber(item.amount) || qty * unitPrice;
        return {
            item: String(item.item || "").trim(),
            description: String(item.description || "").trim(),
            unit: String(item.unit || "式").trim() || "式",
            qty,
            unit_price: unitPrice,
            amount,
        };
    }).filter(item => item.item || item.description || item.amount);
}

function updateQuotationRowAmount(input) {
    if (!input || !["qty", "unit_price"].includes(input.dataset.key || "")) return;
    const row = input.closest("tr");
    if (!row) return;
    const qty = qtNumber(row.querySelector('[data-key="qty"]')?.value) || 1;
    const unitPrice = qtNumber(row.querySelector('[data-key="unit_price"]')?.value);
    const amountEl = row.querySelector('[data-key="amount"]');
    if (amountEl) amountEl.value = String(Math.round(qty * unitPrice));
}

function recalcQuotationTotals() {
    const items = collectQuotationItems("qt");
    const subtotal = items.reduce((sum, item) => sum + qtNumber(item.amount), 0);
    const discount = qtNumber(document.getElementById("qt_discount")?.value);
    const tax = qtNumber(document.getElementById("qt_tax")?.value);
    const total = Math.max(0, subtotal - discount + tax);
    const subtotalEl = document.getElementById("qt_subtotal");
    const totalEl = document.getElementById("qt_total");
    const itemsEl = document.getElementById("qt_items");
    if (subtotalEl) subtotalEl.value = String(Math.round(subtotal));
    if (totalEl) totalEl.value = String(Math.round(total));
    if (itemsEl) itemsEl.value = JSON.stringify(items);
}

function syncTemplateItemsField() {
    const el = document.getElementById("qtTplItems");
    if (el) el.value = JSON.stringify(collectQuotationItems("qtTpl"));
}

function addQuotationItem(scope = "qt", item = {}) {
    const body = document.getElementById(scope === "qt" ? "qtItemsBody" : "qtTplItemsBody");
    if (!body) return;
    body.insertAdjacentHTML("beforeend", quotationRowHtml(item, scope));
    if (scope === "qt") recalcQuotationTotals();
    else syncTemplateItemsField();
}

function applyQuotationPreset(key, scope = "qt") {
    const preset = QT_PRESETS[key] || QT_PRESETS.consult;
    addQuotationItem(scope, preset);
}

function resetQuotationForm() {
    clearFields(["qt_id", "qt_client_name", "qt_project_name", "qt_contact", "qt_phone", "qt_email", "qt_address", "qt_tax_id", "qt_subtotal", "qt_discount", "qt_tax", "qt_total", "qt_notes", "qt_items", "qt_extended_data"]);
    const dateEl = document.getElementById("qt_date");
    const expiryEl = document.getElementById("qt_expiry");
    const statusEl = document.getElementById("qt_status");
    if (dateEl) dateEl.value = qtToday();
    if (expiryEl) expiryEl.value = qtDateAfter(30);
    if (statusEl) statusEl.value = "待確認";
    renderQuotationItems([QT_PRESETS.consult], "qt");
}

function resetQuotationTemplateForm() {
    clearFields(["qtTplId", "qtTplName", "qtTplDescription", "qtTplItems", "qtTplNotes"]);
    const def = document.getElementById("qtTplDefault");
    if (def) def.value = "0";
    renderQuotationItems([QT_PRESETS.consult], "qtTpl");
}

function applySelectedQuotationTemplate() {
    const select = document.getElementById("qtTemplateSelect");
    const id = select ? select.value : "";
    if (!id) return alert("請先選擇模板");
    const tpl = (state.quotationTemplates || []).find(x => String(x.id) === String(id));
    if (!tpl) return alert("找不到模板，請重新整理模板");
    const items = parseQuotationItems(tpl.items);
    renderQuotationItems(items.length ? items : [QT_PRESETS.consult], "qt");
    const projectEl = document.getElementById("qt_project_name");
    const notesEl = document.getElementById("qt_notes");
    if (projectEl && !projectEl.value) projectEl.value = tpl.name || "";
    if (notesEl && !notesEl.value) notesEl.value = tpl.notes || "";
}

function downloadCurrentQuotationPdf() {
    const id = (document.getElementById("qt_id")?.value || "").trim();
    if (!id) return alert("請先儲存報價單，再下載 PDF");
    window.open(`/api/osc/quotations/${encodeURIComponent(id)}/export-pdf`, "_blank", "noopener");
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
        <td>${esc(qtStatusLabel(r.status))}</td>
        <td>${fmtAmount(r.total)}</td>
        <td class="actions">
            <button class="btn" data-act="qt-edit" data-id="${esc(r.id)}">編輯</button>
            <a class="btn" href="/api/osc/quotations/${esc(r.id)}/export-pdf" target="_blank">📄 下載 PDF</a>
            <button class="btn danger" data-act="qt-del" data-id="${esc(r.id)}">刪除</button>
        </td>
    </tr>
`).join("");
}

async function editQuotation(id) {
    const data = await api(`/api/osc/quotations/${encodeURIComponent(id)}`);
    const x = data.item || {};
    writeFields("qt_", x, ["id", "client_name", "project_name", "contact", "phone", "email", "address", "tax_id", "date", "expiry", "subtotal", "discount", "tax", "total", "status", "notes", "items", "extended_data"]);
    const statusEl = document.getElementById("qt_status");
    if (statusEl) statusEl.value = qtStatusLabel(x.status);
    renderQuotationItems(parseQuotationItems(x.items), "qt");
    recalcQuotationTotals();
}

async function saveQuotation() {
    recalcQuotationTotals();
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
        items: JSON.stringify(collectQuotationItems("qt")),
        extended_data: p.qt_extended_data || "{}",
    };
    if (!body.client_name || !body.project_name) return alert("請輸入當事人與專案名稱");
    if (!collectQuotationItems("qt").length) return alert("請至少新增一個服務項目");
    if ((body.id || "").trim()) await api(`/api/osc/quotations/${encodeURIComponent(body.id)}`, "PUT", body);
    else await api(`/api/osc/quotations`, "POST", body);
    resetQuotationForm();
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
    const select = document.getElementById("qtTemplateSelect");
    if (select) {
        const current = select.value;
        select.innerHTML = `<option value="">選擇模板</option>` + state.quotationTemplates.map(r => `<option value="${Number(r.id)}">${esc(r.name || "")}</option>`).join("");
        select.value = current;
    }
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
    renderQuotationItems(parseQuotationItems(x.items), "qtTpl");
}

async function saveQuotationTemplate() {
    syncTemplateItemsField();
    const id = (document.getElementById("qtTplId").value || "").trim();
    const body = {
        name: (document.getElementById("qtTplName").value || "").trim(),
        is_default: (document.getElementById("qtTplDefault").value || "0").trim(),
        description: (document.getElementById("qtTplDescription").value || "").trim(),
        items: JSON.stringify(collectQuotationItems("qtTpl")),
        notes: (document.getElementById("qtTplNotes").value || "").trim(),
    };
    if (!body.name) return alert("請輸入模板名稱");
    if (!collectQuotationItems("qtTpl").length) return alert("請至少新增一個模板項目");
    if (id) await api(`/api/osc/quotation-templates/${Number(id)}`, "PUT", body);
    else await api(`/api/osc/quotation-templates`, "POST", body);
    resetQuotationTemplateForm();
    await loadQuotationTemplates();
    await loadMeta();
}

async function delQuotationTemplate(id) {
    if (!confirm(`確定刪除報價模板 ${id}？`)) return;
    await api(`/api/osc/quotation-templates/${Number(id)}`, "DELETE");
    await loadQuotationTemplates();
    await loadMeta();
}
