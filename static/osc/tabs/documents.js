/* tabs/documents.js – Document index + templates + keywords + forms + wizards */
async function loadLaf() {
    const q = encodeURIComponent((document.getElementById("lafQ").value || "").trim());
    const caseNumber = encodeURIComponent((document.getElementById("lafCaseNumber").value || "").trim());
    const data = await api(`/api/osc/laf?limit=500&q=${q}&case_number=${caseNumber}`);
    const casesData = await api(`/api/osc/laf/cases?limit=500&q=${q}`);
    const items = data.items || {};
    state.laf = {
        checklist: items.checklist || [],
        lifecycle: items.lifecycle || [],
        emails: items.emails || [],
        cases: casesData.items || [],
        selectedCaseId: state.laf?.selectedCaseId || "",
        selectedWorkbench: state.laf?.selectedWorkbench || null,
    };

    const setText = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.textContent = String(value);
    };
    setText("lafCaseCount", state.laf.cases.length);
    setText("lafChecklistCount", (data.counts || {}).checklist || 0);
    setText("lafLifecycleCount", (data.counts || {}).lifecycle || 0);
    setText("lafEmailCount", (data.counts || {}).emails || 0);
    renderLafCaseList(state.laf.cases);

    const selectedStillVisible = state.laf.selectedCaseId && state.laf.cases.some(x => String(x.id) === String(state.laf.selectedCaseId));
    const nextCaseId = selectedStillVisible ? state.laf.selectedCaseId : (state.laf.cases[0]?.id || "");
    if (nextCaseId) {
        await openLafCaseDetail(nextCaseId, { silent: true });
    } else {
        renderLafEmptyDetail("目前沒有符合條件的法扶案件。");
    }

    renderLafCaseSummary(state.laf.checklist, state.laf.lifecycle, state.laf.emails);

    const checklistBody = document.getElementById("lafChecklistBody");
    if (checklistBody) {
        if (!state.laf.checklist.length) {
            checklistBody.innerHTML = `<tr><td colspan="6" class="muted">沒有法扶補件資料</td></tr>`;
        } else {
            checklistBody.innerHTML = state.laf.checklist.map(r => `
        <tr>
            <td>${esc(r.last_updated)}</td>
            <td>${esc(r.case_number)}</td>
            <td>${esc(r.item_label)}</td>
            <td>${esc(r.status)}</td>
            <td>${esc(r.notes || "")}</td>
            <td><button class="btn" data-act="laf-open-checklist" data-case="${esc(r.case_number || "")}">管理</button></td>
        </tr>
    `).join("");
        }
    }

    const lifecycleBody = document.getElementById("lafLifecycleBody");
    if (!lifecycleBody) return;
    if (!state.laf.lifecycle.length) {
        lifecycleBody.innerHTML = `<tr><td colspan="6" class="muted">沒有法扶流程紀錄</td></tr>`;
    } else {
        lifecycleBody.innerHTML = state.laf.lifecycle.map(r => `
        <tr>
            <td>${esc(r.created_at)}</td>
            <td>${esc(r.case_number)}</td>
            <td>${esc(r.event_type)}</td>
            <td>${esc(r.status)}</td>
            <td>${esc(r.completed_at || "")}</td>
            <td title="${esc(r.event_data || "")}">${esc(shortText(r.event_data || "", 120))}</td>
        </tr>
    `).join("");
    }

    const emailBody = document.getElementById("lafEmailBody");
    if (!emailBody) return;
    if (!state.laf.emails.length) {
        emailBody.innerHTML = `<tr><td colspan="6" class="muted">沒有法扶信件紀錄</td></tr>`;
    } else {
        emailBody.innerHTML = state.laf.emails.map(r => `
        <tr>
            <td>${esc(r.received_at)}</td>
            <td>${esc(r.case_number || "")}</td>
            <td>${esc(r.status || "")}</td>
            <td>${esc(r.sender || "")}</td>
            <td title="${esc(r.subject || "")}">${esc(r.subject || "")}</td>
            <td>${esc(r.error_message || "")}</td>
        </tr>
    `).join("");
    }
}

function lafStatusClass(status) {
    const s = String(status || "").trim();
    if (s.includes("未開辦")) return "pending";
    if (s.includes("進行")) return "active";
    if (s.includes("待報結") || s.includes("結案中")) return "closing";
    if (s.includes("已結案")) return "closed";
    return "";
}

function renderLafCaseList(cases = []) {
    const body = document.getElementById("lafCaseBody");
    if (!body) return;
    if (!cases.length) {
        body.innerHTML = `<tr><td colspan="4" class="muted">沒有法扶案件。請調整搜尋或按「掃描派案」。</td></tr>`;
        return;
    }
    const sort = state.lafSort || { col: "case_number", dir: 1, type: "string" };
    const rows = applySort(cases.map(c => ({
        ...c,
        legal_aid_status: c.legal_aid_status || c.status || "未開辦",
        case_type: c.case_type || c.case_reason || c.case_category || "",
    })), sort.col, sort.dir, sort.type);
    body.innerHTML = rows.map(c => {
        const status = c.legal_aid_status || c.status || "未開辦";
        const type = c.case_type || c.case_reason || c.case_category || "";
        const pending = Number(c.pending_laf_items || 0);
        const pendingText = pending ? ` · 待補 ${pending}` : "";
        return `
            <tr data-act="laf-select-case" data-id="${esc(c.id)}" class="${String(c.id) === String(state.laf?.selectedCaseId || "") ? "active" : ""}">
                <td>${esc(c.case_number || "")}</td>
                <td>${esc(c.client_name || "")}</td>
                <td>${esc(shortText(type, 24))}</td>
                <td><span class="laf-status-pill ${lafStatusClass(status)}">${esc(status)}${esc(pendingText)}</span></td>
            </tr>
        `;
    }).join("");

    document.querySelectorAll("#laf th[data-sort]").forEach(th => {
        th.innerHTML = th.textContent.replace(/ [▲▼]/g, "") + (sort.col === th.dataset.sort ? (sort.dir === 1 ? " ▲" : " ▼") : "");
    });
}

function renderLafEmptyDetail(message) {
    const panel = document.getElementById("lafDetailPanel");
    if (!panel) return;
    panel.innerHTML = `
        <div class="empty-state">
            <div>
                <h3>${esc(message || "請從左側選擇法扶案件")}</h3>
                <p>選取案件後會顯示原 OSC 單機版的開辦資料、報結資料彙總、閱卷統計與書狀列表。</p>
            </div>
        </div>
    `;
}

function isConsumerDebtCase(c = {}) {
    const text = `${c.case_type || ""} ${c.case_reason || ""} ${c.case_category || ""}`;
    return text.includes("消費者債務清理") || text.includes("消債") || text.includes("債務清理");
}

function lafFilterDocs(docs = [], keywords = []) {
    return (docs || []).filter(d => {
        const haystack = `${d.file_name || ""} ${d.subfolder_name || ""} ${d.reason || ""}`;
        return keywords.some(k => haystack.includes(k));
    });
}

function renderLafDocList(docs = [], keywords = [], empty = "尚未索引到相關文件") {
    const hits = lafFilterDocs(docs, keywords).slice(0, 12);
    if (!hits.length) return `<div class="muted">${esc(empty)}</div>`;
    return `<div class="laf-compact-list">${hits.map(d => `
        <div class="laf-compact-item">
            <div class="name" title="${esc(d.file_name || "")}">${esc(d.file_name || "")}</div>
            <div class="sub" title="${esc(d.subfolder_name || d.file_path || "")}">${esc(d.subfolder_name || d.file_path || "")}</div>
            <div class="actions">
                <a class="btn slim" href="${fileContentUrl(d.file_path || "", true)}" target="_blank" rel="noopener noreferrer">預覽</a>
                <button class="btn slim" data-act="doc-open" data-path="${esc(d.file_path || "")}">開啟</button>
            </div>
        </div>
    `).join("")}</div>`;
}

function lafCollectEvents(data = {}, keyword) {
    const rows = [];
    (data.meetings || []).forEach(m => {
        const text = `${m.type || ""} ${m.notes || ""} ${m.location || ""}`;
        if (text.includes(keyword)) rows.push({ date: m.datetime || "", summary: `${m.type || keyword} ${m.location || ""}`.trim() });
    });
    (data.todos || []).forEach(t => {
        const text = `${t.todo_type || ""} ${t.description || ""}`;
        if (text.includes(keyword)) rows.push({ date: `${t.todo_date || ""} ${t.todo_time || ""}`.trim(), summary: t.description || t.todo_type || keyword });
    });
    return rows;
}

function renderLafEventStats(data = {}) {
    const configs = ["開庭", "會議", "律見", "電話聯繫"];
    return `<div class="laf-event-grid">${configs.map(label => {
        const rows = lafCollectEvents(data, label);
        const latest = rows[0]?.date || "未記錄";
        return `<div class="laf-event-card"><span>${esc(label)}</span><strong>${rows.length}</strong><small class="muted">${esc(shortText(latest, 18))}</small></div>`;
    }).join("")}</div>`;
}

function renderLafReviewStats(data = {}) {
    const docs = lafFilterDocs(data.documents || [], ["閱卷", "OCR", "卷證", "電子卷"]);
    const dates = Array.from(new Set(docs.map(d => String(d.modified_date || "").slice(0, 10)).filter(Boolean))).sort().reverse();
    return `
        <div class="laf-status-row">
            <strong>閱卷次數：${docs.length}</strong>
            <button class="btn slim" data-act="case-open" data-id="${esc(data.case?.id || "")}">開啟案件資料夾</button>
            <button class="btn slim" data-act="laf-open-doc-keyword" data-id="${esc(data.case?.id || "")}" data-keyword="閱卷">開啟閱卷資料</button>
        </div>
        <div class="muted">${dates.length ? `日期：${esc(dates.slice(0, 8).join("、"))}` : "尚未索引到閱卷日期。"}</div>
        ${renderLafDocList(data.documents || [], ["閱卷", "OCR", "卷證", "電子卷"], "尚未索引到閱卷資料")}
    `;
}

function renderLafProgressRows(rows = []) {
    if (!rows.length) return `<div class="muted">目前無法扶流程紀錄。</div>`;
    return `<div class="table-wrap"><table>
        <thead><tr><th>時間</th><th>事件</th><th>狀態</th><th>內容</th></tr></thead>
        <tbody>${rows.slice(0, 10).map(r => `<tr>
            <td>${esc(r.created_at || "")}</td>
            <td>${esc(r.event_type || "")}</td>
            <td>${esc(r.status || "")}</td>
            <td title="${esc(r.event_data || "")}">${esc(shortText(r.event_data || "", 100))}</td>
        </tr>`).join("")}</tbody>
    </table></div>`;
}

function renderLafEmailRows(caseNumber) {
    const emails = (state.laf?.emails || []).filter(e => String(e.case_number || "") === String(caseNumber || "")).slice(0, 8);
    if (!emails.length) return `<div class="muted">目前無法扶信件紀錄。</div>`;
    return `<div class="laf-compact-list">${emails.map(e => `
        <div class="laf-compact-item">
            <div class="name">${esc(e.received_at || "")}</div>
            <div class="sub" title="${esc(e.subject || "")}">${esc(e.subject || "")}</div>
            <div><span class="laf-status-pill">${esc(e.status || "")}</span></div>
        </div>
    `).join("")}</div>`;
}

function renderLafOpenDocButtons(c, docs) {
    const names = ["委任狀", "接案通知書", "預付酬金領款單"];
    if (isConsumerDebtCase(c)) names.push("應備資料", "附條件第二階段預付酬金領款單");
    return names.map(name => {
        const found = lafFilterDocs(docs, [name]).length;
        return `<button class="btn" data-act="laf-open-doc-keyword" data-id="${esc(c.id)}" data-keyword="${esc(name)}">${esc(name)}${found ? ` (${found})` : ""}</button>`;
    }).join("");
}

function renderLafDebtTools(c) {
    if (!isConsumerDebtCase(c)) return "";
    const tools = [
        ["聲請狀", "application"],
        ["財產及收入狀況說明書", "asset_statement"],
        ["債權人清冊", "creditor_list"],
        ["合併 PDF", "pdf_merge"],
        ["陳報狀", "report"],
        ["補件陳報狀", "supplement"],
    ];
    return `
        <div class="laf-debt-tools">
            <h4>消債羅伯特</h4>
            <div class="laf-button-grid">
                <button class="btn" data-act="laf-open-checklist" data-case="${esc(c.case_number || "")}">開啟/編輯應備事項表</button>
                ${tools.map(([label, key]) => `<button class="btn" data-act="laf-debt-tool" data-id="${esc(c.id)}" data-module="${esc(key)}">${esc(label)}</button>`).join("")}
            </div>
        </div>
    `;
}

async function openLafCaseDetail(caseId, options = {}) {
    const id = String(caseId || "").trim();
    if (!id) return;
    state.laf.selectedCaseId = id;
    renderLafCaseList(state.laf.cases || []);
    const panel = document.getElementById("lafDetailPanel");
    if (panel && !options.silent) panel.innerHTML = `<div class="empty-state"><h3>載入法扶案件中...</h3></div>`;
    const data = await api(`/api/osc/cases/${encodeURIComponent(id)}/workbench`);
    state.laf.selectedWorkbench = data;
    renderLafCaseDetail(data);
}

function renderLafCaseDetail(data = {}) {
    const panel = document.getElementById("lafDetailPanel");
    if (!panel) return;
    const c = data.case || {};
    const s = data.stats || {};
    const status = c.legal_aid_status || c.status || "未開辦";
    const pending = (data.legal_aid_checklist || []).filter(isLafPending);
    panel.innerHTML = `
        <div class="laf-detail-head">
            <div>
                <h3>${esc(c.case_number || "")} - ${esc(c.client_name || "")}</h3>
                <div class="muted">案件分類：${esc(c.case_type || c.case_reason || c.case_category || "未標示")}｜法扶案號：${esc(c.laf_case_no || "")}</div>
            </div>
            <span class="laf-status-pill ${lafStatusClass(status)}">${esc(status)}</span>
        </div>

        <div class="laf-detail-section">
            <h4>案件狀態</h4>
            <div class="laf-status-row">
                <select id="lafStatusSelect">
                    ${["未開辦", "進行中", "已結案，待報結", "已結案"].map(x => `<option value="${esc(x)}" ${x === status ? "selected" : ""}>${esc(x)}</option>`).join("")}
                </select>
                <button class="btn primary" data-act="laf-status-update" data-id="${esc(c.id)}">更新狀態</button>
                <button class="btn" data-act="case-open" data-id="${esc(c.id)}">開啟案件資料夾</button>
                <button class="btn" data-act="case-workbench" data-id="${esc(c.id)}">完整案件工作台</button>
            </div>
        </div>

        <div class="laf-detail-section">
            <h4>開辦資料</h4>
            <div class="laf-button-grid">${renderLafOpenDocButtons(c, data.documents || [])}</div>
            ${renderLafDebtTools(c)}
        </div>

        <div class="laf-detail-section">
            <h4>報結資料彙總</h4>
            <div class="laf-button-grid">
                <button class="btn" data-act="laf-open-doc-keyword" data-id="${esc(c.id)}" data-keyword="結案酬金領款單">開啟結案酬金領款單</button>
                <button class="btn" data-act="laf-export-activity" data-id="${esc(c.id)}">匯出活動記錄</button>
                <a class="btn" href="/api/osc/cases/${encodeURIComponent(c.id || "")}/address-label?recipient=laf&mode=preview" target="_blank" rel="noopener noreferrer">列印法扶地址</a>
                <button class="btn warn" data-act="laf-case-action" data-id="${esc(c.id)}" data-action="laf_closing_status">結案狀況盤點</button>
            </div>
            <div class="stat-grid" style="margin-top:10px;">
                <div class="stat-card"><div class="k">待補項目</div><div class="v">${pending.length}</div></div>
                <div class="stat-card"><div class="k">流程紀錄</div><div class="v">${(data.laf_progress || []).length}</div></div>
                <div class="stat-card"><div class="k">索引文件</div><div class="v">${esc(s.docs_indexed || 0)}</div></div>
                <div class="stat-card"><div class="k">待辦事項</div><div class="v">${esc(s.todo_pending || 0)}</div></div>
            </div>
        </div>

        <div class="laf-detail-section">
            <h4>Google 行事曆 / 待辦統計</h4>
            ${renderLafEventStats(data)}
        </div>

        <div class="laf-detail-section">
            <h4>閱卷資料統計</h4>
            ${renderLafReviewStats(data)}
        </div>

        <div class="laf-detail-grid">
            <div class="laf-detail-section">
                <h4>我方歷次書狀</h4>
                ${renderLafDocList(data.documents || [], ["我方歷次書狀", "書狀", "聲請狀", "陳報狀"])}
            </div>
            <div class="laf-detail-section">
                <h4>判決書</h4>
                ${renderLafDocList(data.documents || [], ["判決", "裁定", "判決書"])}
            </div>
        </div>

        <div class="laf-detail-grid">
            <div class="laf-detail-section">
                <h4>法扶流程紀錄</h4>
                ${renderLafProgressRows(data.laf_progress || [])}
            </div>
            <div class="laf-detail-section">
                <h4>法扶信件紀錄</h4>
                ${renderLafEmailRows(c.case_number)}
            </div>
        </div>
    `;
}

async function updateLafCaseStatus(caseId) {
    const id = String(caseId || "").trim();
    const status = (document.getElementById("lafStatusSelect")?.value || "").trim();
    if (!id || !status) return;
    const caseStatus = status === "未開辦" || status === "進行中" ? "進行中" : "已結案";
    await api(`/api/osc/cases/${encodeURIComponent(id)}`, "PUT", {
        legal_aid_status: status,
        status: caseStatus,
    });
    showToast(`法扶狀態已更新為「${status}」。`, "ok", 2600);
    await loadLaf();
}

async function runLafScan() {
    const result = await api("/api/osc/laf-backfill", "POST", {});
    if (!result || result.ok === false) throw new Error(result?.error || "掃描派案失敗");
    showToast("法扶派案掃描完成。", "ok", 3200);
    await loadLaf();
}

async function batchLafStatusToInProgress() {
    if (!confirm("確定要將所有未開辦的法扶案件改為「進行中」嗎？")) return;
    const result = await api("/api/osc/laf/batch-status", "POST", { legal_aid_status: "進行中" });
    if (!result || result.ok === false) throw new Error(result?.error || "批次更新失敗");
    showToast("已批次更新未開辦法扶案件。", "ok", 3200);
    await loadLaf();
}

async function openLafKeywordDoc(caseId, keyword) {
    const data = state.laf?.selectedWorkbench?.case?.id === caseId
        ? state.laf.selectedWorkbench
        : await api(`/api/osc/cases/${encodeURIComponent(caseId)}/workbench`);
    const hit = lafFilterDocs(data.documents || [], [keyword])[0];
    if (!hit?.file_path) {
        showToast(`索引未命中「${keyword}」，改搜案件資料夾。`, "warn", 2600);
        const found = await api(`/api/osc/cases/${encodeURIComponent(caseId)}/file-search?q=${encodeURIComponent(keyword)}&limit=30`);
        showLafFileSearchResults(caseId, keyword, found);
        return;
    }
    await openDocumentPath(hit.file_path);
}

function showLafFileSearchResults(caseId, keyword, data = {}) {
    const items = data.items || [];
    const caseLabel = [data.case?.case_number, data.case?.client_name].filter(Boolean).join(" - ");
    const title = `文件查找｜${keyword}`;
    const rows = items.map(file => {
        const path = file.file_path || "";
        const isPdf = file.is_pdf || String(path).toLowerCase().endsWith(".pdf");
        return `
            <div class="laf-file-result">
                <div class="laf-file-main">
                    <strong title="${esc(file.relative_path || file.file_name || "")}">${esc(file.file_name || "")}</strong>
                    <span>${esc(file.relative_path || "")}</span>
                    <small>${esc(file.size_label || "")}${file.modified_date ? `｜${esc(file.modified_date)}` : ""}</small>
                </div>
                <div class="laf-file-actions">
                    <a class="btn slim" href="${fileContentUrl(path, true)}" target="_blank" rel="noopener noreferrer">預覽</a>
                    <a class="btn slim" href="${fileContentUrl(path)}" target="_blank" rel="noopener noreferrer">下載</a>
                    ${isPdf ? `<button class="btn slim" data-act="doc-pdf-tool" data-path="${esc(path)}">PDF 工具</button>` : ""}
                    <button class="btn slim" data-act="doc-open" data-path="${esc(path)}">本機開啟</button>
                    <button class="btn slim" data-act="doc-copy" data-path="${esc(path)}">複製路徑</button>
                </div>
            </div>
        `;
    }).join("");
    const body = items.length ? rows : `
        <div class="empty-state">
            <h3>尚未找到「${esc(keyword)}」</h3>
            <p>索引與案件資料夾都沒有命中。可以先開啟案件資料夾確認檔名，或進 PDF 工具上傳處理。</p>
        </div>
    `;
    wbShow(title, `
        <div class="soft-block">
            <div class="case-list-head">
                <div>
                    <h3>${esc(caseLabel || "案件資料夾")}</h3>
                    <div class="section-note">索引未命中時，MAGI 會直接搜尋本機同步的案件資料夾。找到 PDF/文件後可預覽、下載或帶入 PDF 工具。</div>
                </div>
                <div class="inline-actions">
                    <button class="btn" data-act="case-open" data-id="${esc(caseId)}">開啟案件資料夾</button>
                    <button class="btn" data-act="case-magi-tab" data-tab="pdfTools">PDF 工具</button>
                </div>
            </div>
            <div class="laf-file-result-list">${body}</div>
        </div>
    `);
}

function openLafDebtTool(caseId, moduleKey) {
    const wb = state.laf?.selectedWorkbench;
    const c = wb?.case || {};
    const params = new URLSearchParams();
    if (c.case_number) params.set("case_number", c.case_number);
    if (moduleKey) params.set("module", moduleKey);
    window.location.href = `/osc/debt?${params.toString()}`;
}

async function runLafCaseAction(caseId, action) {
    const result = await api(`/api/osc/cases/${encodeURIComponent(caseId)}/quick-action`, "POST", { action });
    if (!result || result.ok === false) throw new Error(result?.error || "法扶案件盤點失敗");
    showToast(result.message || "已完成法扶案件盤點。", "ok", 3200);
    if (result.reply) {
        alert(result.reply);
    }
}

function downloadLafActivityCsv() {
    const data = state.laf?.selectedWorkbench;
    if (!data?.case) return;
    const rows = [["類型", "日期", "內容", "狀態"]];
    (data.meetings || []).forEach(x => rows.push(["會議", x.datetime || "", `${x.type || ""} ${x.location || ""} ${x.notes || ""}`.trim(), x.status || ""]));
    (data.todos || []).forEach(x => rows.push(["待辦", `${x.todo_date || ""} ${x.todo_time || ""}`.trim(), x.description || x.todo_type || "", x.status || ""]));
    (data.laf_progress || []).forEach(x => rows.push(["法扶流程", x.created_at || "", `${x.event_type || ""} ${x.event_data || ""}`.trim(), x.status || ""]));
    const csv = rows.map(row => row.map(cell => `"${String(cell || "").replaceAll('"', '""')}"`).join(",")).join("\n");
    const blob = new Blob(["\ufeff" + csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `法扶活動記錄_${data.case.case_number || data.case.id}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
}

function isLafPending(row) {
    const status = String(row?.status || "").trim();
    if (!status) return true;
    return !["已備齊", "不適用", "完成", "已完成"].includes(status);
}

function renderLafCaseSummary(checklist = [], lifecycle = [], emails = []) {
    const target = document.getElementById("lafCaseSummary");
    if (!target) return;
    if (!checklist.length && !lifecycle.length && !emails.length) {
        target.innerHTML = `<div class="muted">沒有可整理的法扶資料。</div>`;
        return;
    }
    const grouped = new Map();
    const ensure = (caseNumber) => {
        const key = String(caseNumber || "未標示案號").trim() || "未標示案號";
        if (!grouped.has(key)) grouped.set(key, { caseNumber: key, checklist: [], lifecycle: [], emails: [] });
        return grouped.get(key);
    };
    checklist.forEach(row => ensure(row.case_number).checklist.push(row));
    lifecycle.forEach(row => ensure(row.case_number).lifecycle.push(row));
    emails.forEach(row => ensure(row.case_number).emails.push(row));

    const cases = Array.from(grouped.values()).sort((a, b) => {
        const ap = a.checklist.filter(isLafPending).length;
        const bp = b.checklist.filter(isLafPending).length;
        if (ap !== bp) return bp - ap;
        return String(a.caseNumber).localeCompare(String(b.caseNumber), "zh-Hant");
    });

    target.innerHTML = cases.slice(0, 24).map(group => {
        const pending = group.checklist.filter(isLafPending);
        const done = group.checklist.length - pending.length;
        const latest = [
            ...group.checklist.map(x => x.last_updated),
            ...group.lifecycle.map(x => x.created_at),
            ...group.emails.map(x => x.received_at),
        ].filter(Boolean).sort().pop() || "";
        const needed = pending.length
            ? pending.slice(0, 5).map(item => {
                const note = item.notes ? `：${item.notes}` : "";
                return `<div class="needed-item">${esc(item.item_label || item.item_key || "未命名項目")}${esc(note)}</div>`;
            }).join("")
            : `<div class="needed-item muted">目前沒有待補項目。</div>`;
        const emailHint = group.emails[0]?.subject ? `<div class="case-meta">最近信件：${esc(group.emails[0].subject)}</div>` : "";
        return `
            <div class="laf-case-card ${pending.length ? "" : "done"}">
                <div class="case-line">
                    <div class="case-title">${esc(group.caseNumber)}</div>
                    <div class="case-badge">${pending.length ? `待補 ${pending.length}` : "已整理"}</div>
                </div>
                <div class="needed-list">${needed}</div>
                <div class="case-meta">已備齊/不適用：${done}｜最後更新：${esc(latest || "未記錄")}</div>
                ${emailHint}
                <div class="toolbar" style="margin:10px 0 0;">
                    <button class="btn primary" data-act="laf-open-checklist" data-case="${esc(group.caseNumber)}">打開補件管理</button>
                </div>
            </div>
        `;
    }).join("");
}

async function openLafChecklistCase(caseNumber) {
    const value = String(caseNumber || "").trim();
    if (!value || value === "未標示案號") {
        showToast("這筆資料沒有案號，無法直接帶入補件管理。", "warn");
        return;
    }
    const html = `
        <div class="card">
            <h3>應備事項表 / 法扶補件清單</h3>
            <div class="toolbar">
                <input id="lafChecklistCaseNumber" value="${esc(value)}" readonly>
                <button class="btn" data-act="laf-checklist-reload">重新載入</button>
                <button class="btn primary" data-act="laf-checklist-seed">填入預設項目</button>
            </div>
            <div class="table-wrap">
                <table class="compact-table">
                    <thead><tr><th>項目</th><th>狀態</th><th>備註</th><th>更新時間</th><th>操作</th></tr></thead>
                    <tbody id="lafChecklistMgmtBody"><tr><td colspan="5" class="muted">載入中...</td></tr></tbody>
                </table>
            </div>
            <div class="soft-block" style="margin-top:10px">
                <div class="field-grid cols-3">
                    <div class="field"><label>新增項目</label><input id="lafChecklistNewLabel" placeholder="自訂補件項目"></div>
                    <div class="field"><label>狀態</label><select id="lafChecklistNewStatus"><option>待補</option><option>已備齊</option><option>不適用</option></select></div>
                    <div class="field"><label>備註</label><input id="lafChecklistNewNotes" placeholder="可選"></div>
                </div>
                <div class="toolbar"><button class="btn primary" data-act="laf-checklist-add">新增項目</button></div>
            </div>
        </div>
    `;
    wbShow(`應備事項表｜${value}`, html);
    await loadLafChecklistInWorkbench(value);
}

async function loadLafChecklistInWorkbench(caseNumber) {
    const value = String(caseNumber || document.getElementById("lafChecklistCaseNumber")?.value || "").trim();
    const tbody = document.getElementById("lafChecklistMgmtBody");
    if (!value || !tbody) return;
    tbody.innerHTML = `<tr><td colspan="5" class="muted">載入中...</td></tr>`;
    const data = await api(`/api/osc/checklists/legal-aid?case_number=${encodeURIComponent(value)}`);
    if (!data || data.ok === false) throw new Error(data?.error || "載入應備事項表失敗");
    renderLafChecklistRows(value, data.items || []);
}

async function reloadLafChecklistFromModal() {
    await loadLafChecklistInWorkbench();
}

async function seedLafChecklistFromModal() {
    if (typeof seedLafChecklist === "function") {
        seedLafChecklist();
        window.setTimeout(() => loadLafChecklistInWorkbench().catch(err => showToast(err.message, "warn")), 700);
    }
}

async function addLafChecklistFromModal() {
    if (typeof addLafChecklistItem === "function") {
        addLafChecklistItem();
        window.setTimeout(() => loadLafChecklistInWorkbench().catch(err => showToast(err.message, "warn")), 700);
    }
}

async function loadDocuments() {
    const q = encodeURIComponent((document.getElementById("docsQ").value || "").trim());
    const caseNumber = encodeURIComponent((document.getElementById("docsCaseNumber").value || "").trim());
    const kind = encodeURIComponent((document.getElementById("docsKind").value || "all").trim());
    const data = await api(`/api/osc/documents?limit=400&q=${q}&case_number=${caseNumber}&kind=${kind}`);
    state.documents = data.items || [];
    const body = document.getElementById("docsBody");
    if (!state.documents.length) {
        body.innerHTML = `<tr><td colspan="7" class="muted">沒有文件資料</td></tr>`;
        return;
    }
    body.innerHTML = state.documents.map(r => {
        const fp = r.file_path || "";
        const ext = (fp.split(".").pop() || "").toLowerCase();
        const stampable = ["pdf", "docx", "doc"].includes(ext);
        const stampBtn = stampable
            ? `<button class="btn" data-act="doc-stamp" data-path="${esc(fp)}" title="蓋章製作正本/副本/繕本">📋 蓋章</button>`
            : "";
        const pdfBtn = ext === "pdf"
            ? `<button class="btn" data-act="doc-pdf-tool" data-path="${esc(fp)}" title="帶入 PDF 工具">PDF 工具</button>`
            : "";
        return `
    <tr>
        <td>${esc(r.timestamp)}</td>
        <td>${esc(r.source)}</td>
        <td>${esc(r.case_number)}</td>
        <td>${esc(r.kind_label || "")}</td>
        <td>${esc(r.file_name)}</td>
        <td title="${esc(fp)}">${esc(fp)}</td>
        <td class="actions">
            <button class="btn" data-act="doc-open" data-path="${esc(fp)}">開啟</button>
            <button class="btn" data-act="doc-copy" data-path="${esc(fp)}">複製路徑</button>
            ${stampBtn}
            ${pdfBtn}
        </td>
    </tr>`;
    }).join("");
}

function setPdfToolPath(path) {
    const input = document.getElementById("pdfToolPath");
    if (input) input.value = path || "";
    const pdfTab = document.querySelector('.tab-btn[data-tab="pdfTools"]');
    if (pdfTab && state.activeTab !== "pdfTools") pdfTab.click();
    const card = document.getElementById("pdfToolCard");
    if (card) window.setTimeout(() => card.scrollIntoView({ behavior: "smooth", block: "start" }), 60);
    const status = document.getElementById("pdfToolStatus");
    if (status) {
        status.hidden = false;
        status.className = "status-banner";
        status.textContent = path ? `已帶入 PDF：${path.split(/[\\/]/).pop()}` : "請指定 PDF 路徑。";
    }
}

function pdfToolPayload(action) {
    return {
        action,
        file_path: (document.getElementById("pdfToolPath")?.value || "").trim(),
        pages: (document.getElementById("pdfToolPages")?.value || "").trim(),
        ranges: (document.getElementById("pdfToolPages")?.value || "").trim(),
        angle: Number(document.getElementById("pdfToolAngle")?.value || 90),
        other_paths: (document.getElementById("pdfToolOtherPaths")?.value || "").trim(),
        text: (document.getElementById("pdfToolWatermark")?.value || "").trim(),
        password: (document.getElementById("pdfToolPassword")?.value || "").trim(),
    };
}

function renderPdfToolResult(result) {
    const status = document.getElementById("pdfToolStatus");
    const outputs = document.getElementById("pdfToolOutputs");
    if (status) {
        status.hidden = false;
        status.className = "status-banner";
        status.textContent = result.message || "PDF 操作完成。";
    }
    if (!outputs) return;
    const files = result.outputs || [];
    const info = result.item;
    if (info) {
        outputs.hidden = false;
        outputs.innerHTML = `
            <div><b>${esc(info.file_name || "")}</b></div>
            <div class="muted">頁數：${esc(info.page_count)}｜大小：${formatBytes(info.size)}｜加密：${info.encrypted ? "是" : "否"}</div>
            <div class="muted">標題：${esc((info.metadata || {}).title || "未設定")}</div>
        `;
        return;
    }
    outputs.hidden = !files.length;
    outputs.innerHTML = files.map(path => `
        <div style="display:flex; gap:8px; align-items:center; margin-top:6px; flex-wrap:wrap;">
            <span style="flex:1; min-width:220px;">${esc(path)}</span>
            <button class="btn" data-act="doc-open" data-path="${esc(path)}">開啟</button>
            <button class="btn" data-act="doc-copy" data-path="${esc(path)}">複製路徑</button>
        </div>
    `).join("");
}

async function runPdfTool(action) {
    const result = await api("/api/osc/pdf/action", "POST", pdfToolPayload(action));
    if (!result || !result.ok) throw new Error(result?.error || "PDF 操作失敗");
    renderPdfToolResult(result);
    showToast(result.message || "PDF 操作完成。", "ok", 3200);
}

async function uploadPdfToolFile() {
    const input = document.getElementById("pdfToolUpload");
    const file = input?.files?.[0];
    if (!file) {
        showToast("請先選擇 PDF 檔案。", "warn");
        return;
    }
    const form = new FormData();
    form.append("file", file);
    const result = await apiForm("/api/osc/pdf/upload", form);
    if (!result || !result.ok) throw new Error(result?.error || "PDF 上傳失敗");
    setPdfToolPath(result.path || "");
    renderPdfToolResult(result);
    showToast(result.message || "PDF 已上傳。", "ok", 3200);
}

// ── 蓋章製作（呼叫後端 doc-producer skill）──
async function stampDocument(path) {
    if (!path) return;
    const ext = (path.split(".").pop() || "").toLowerCase();
    if (!["pdf", "docx", "doc"].includes(ext)) {
        showToast("僅支援 PDF / DOCX 蓋章", "warn");
        return;
    }

    const copyType = (prompt(
        "請選擇蓋章類型（直接按確定預設「正本」）：\n\n  正本 / 副本 / 繕本",
        "正本"
    ) || "").trim();
    if (!copyType) return;
    if (!["正本", "副本", "繕本"].includes(copyType)) {
        showToast("無效的蓋章類型，僅可填正本/副本/繕本", "warn");
        return;
    }

    let addPoa = false;
    let addSent = false;
    if (copyType === "正本") {
        addPoa = confirm("正本是否加註「附委任狀」？");
        addSent = confirm("正本是否加註「繕本已送對造」？");
    }

    const fileLabel = path.split(/[\\/]/).pop() || path;
    showToast(`蓋章中：${fileLabel} → ${copyType}${addPoa ? "（附委任狀）" : ""}${addSent ? "（繕本已送對造）" : ""}`, "info", 2400);

    try {
        const result = await api("/api/osc/documents/stamp", "POST", {
            file_path: path,
            copy_type: copyType,
            add_poa: addPoa,
            add_sent_to_opponent: addSent,
        });
        if (result && result.ok) {
            const out = result.output_path || "（無輸出路徑）";
            showToast(`✅ ${copyType}已產出：${out.split(/[\\/]/).pop()}`, "ok", 4000);
            // 自動把產出路徑放剪貼簿
            try { await copyText(out, "已複製產出檔路徑到剪貼簿。"); } catch (_) {}
        } else {
            showToast(`蓋章失敗：${result?.error || "未知錯誤"}`, "warn", 4000);
        }
    } catch (err) {
        showToast(`蓋章失敗：${err.message || err}`, "warn", 4000);
    }
}

async function loadDocumentTemplates() {
    const q = encodeURIComponent((document.getElementById("docTplQ").value || "").trim());
    const caseNumber = encodeURIComponent((document.getElementById("docTplCaseNumber").value || "").trim());
    const docType = encodeURIComponent((document.getElementById("docTplTypeFilter").value || "").trim());
    const data = await api(`/api/osc/document-templates?limit=400&q=${q}&case_number=${caseNumber}&doc_type=${docType}`);
    state.docTemplates = data.items || [];
    const body = document.getElementById("docTplBody");
    if (!state.docTemplates.length) {
        body.innerHTML = `<tr><td colspan="8" class="muted">沒有模板資料</td></tr>`;
        return;
    }
    body.innerHTML = state.docTemplates.map(r => `
    <tr>
        <td>${esc(r.id)}</td>
        <td>${esc(r.doc_type)}</td>
        <td>${esc(r.party_name)}</td>
        <td>${esc(r.case_number)}</td>
        <td>${esc(r.division)}</td>
        <td>${esc(r.use_count)}</td>
        <td>${esc(r.last_used || r.created_date || "")}</td>
        <td class="actions">
            <button class="btn" data-act="doc-tpl-edit" data-id="${Number(r.id)}">編輯</button>
            <button class="btn danger" data-act="doc-tpl-del" data-id="${Number(r.id)}">刪除</button>
        </td>
    </tr>
`).join("");
}

async function editDocumentTemplate(id) {
    const data = await api(`/api/osc/document-templates/${Number(id)}`);
    const x = data.item || {};
    document.getElementById("docTplId").value = x.id || "";
    document.getElementById("docTplType").value = x.doc_type || "";
    document.getElementById("docTplParty").value = x.party_name || "";
    document.getElementById("docTplCase").value = x.case_number || "";
    document.getElementById("docTplDivision").value = x.division || "";
    document.getElementById("docTplUseCount").value = x.use_count || 0;
    document.getElementById("docTplData").value = x.template_data || "";
}

async function saveDocumentTemplate() {
    const body = {
        id: (document.getElementById("docTplId").value || "").trim(),
        doc_type: (document.getElementById("docTplType").value || "").trim(),
        party_name: (document.getElementById("docTplParty").value || "").trim(),
        case_number: (document.getElementById("docTplCase").value || "").trim(),
        division: (document.getElementById("docTplDivision").value || "").trim(),
        use_count: (document.getElementById("docTplUseCount").value || "0").trim(),
        template_data: (document.getElementById("docTplData").value || "").trim(),
    };
    if (body.id) await api(`/api/osc/document-templates/${Number(body.id)}`, "PUT", body);
    else await api(`/api/osc/document-templates`, "POST", body);
    ["docTplId", "docTplType", "docTplParty", "docTplCase", "docTplDivision", "docTplUseCount", "docTplData"].forEach(id => {
        const el = document.getElementById(id); if (el) el.value = "";
    });
    await loadDocumentTemplates();
    await loadMeta();
}

async function delDocumentTemplate(id) {
    if (!confirm(`確定刪除書狀模板 ${id}？`)) return;
    await api(`/api/osc/document-templates/${Number(id)}`, "DELETE");
    await loadDocumentTemplates();
    await loadMeta();
}

async function loadDocumentKeywords() {
    const q = encodeURIComponent((document.getElementById("docKwQ").value || "").trim());
    const caseNumber = encodeURIComponent((document.getElementById("docKwCaseNumber").value || "").trim());
    const category = encodeURIComponent((document.getElementById("docKwCategoryFilter").value || "").trim());
    const data = await api(`/api/osc/document-keywords?limit=600&q=${q}&case_number=${caseNumber}&category=${category}`);
    state.docKeywords = data.items || [];
    const body = document.getElementById("docKwBody");
    if (!state.docKeywords.length) {
        body.innerHTML = `<tr><td colspan="9" class="muted">沒有關鍵字資料</td></tr>`;
        return;
    }
    body.innerHTML = state.docKeywords.map(r => `
    <tr>
        <td>${esc(r.id)}</td>
        <td>${esc(r.case_number)}</td>
        <td>${esc(r.keyword_name)}</td>
        <td>${esc(r.category)}</td>
        <td>${esc(r.hotkey)}</td>
        <td>${esc(r.is_case_specific)}</td>
        <td>${esc(r.usage_count)}</td>
        <td title="${esc(r.keyword_content || "")}">${esc(r.keyword_content || "")}</td>
        <td class="actions">
            <button class="btn" data-act="doc-kw-edit" data-id="${Number(r.id)}">編輯</button>
            <button class="btn danger" data-act="doc-kw-del" data-id="${Number(r.id)}">刪除</button>
        </td>
    </tr>
`).join("");
}

async function editDocumentKeyword(id) {
    const data = await api(`/api/osc/document-keywords/${Number(id)}`);
    const x = data.item || {};
    document.getElementById("docKwId").value = x.id || "";
    document.getElementById("docKwCase").value = x.case_number || "";
    document.getElementById("docKwName").value = x.keyword_name || "";
    document.getElementById("docKwCategory").value = x.category || "";
    document.getElementById("docKwHotkey").value = x.hotkey || "";
    document.getElementById("docKwCaseSpecific").value = x.is_case_specific ?? 0;
    document.getElementById("docKwUsageCount").value = x.usage_count ?? 0;
    document.getElementById("docKwContent").value = x.keyword_content || "";
}

async function saveDocumentKeyword() {
    const body = {
        id: (document.getElementById("docKwId").value || "").trim(),
        case_number: (document.getElementById("docKwCase").value || "").trim(),
        keyword_name: (document.getElementById("docKwName").value || "").trim(),
        category: (document.getElementById("docKwCategory").value || "").trim(),
        hotkey: (document.getElementById("docKwHotkey").value || "").trim(),
        is_case_specific: (document.getElementById("docKwCaseSpecific").value || "0").trim(),
        usage_count: (document.getElementById("docKwUsageCount").value || "0").trim(),
        keyword_content: (document.getElementById("docKwContent").value || "").trim(),
    };
    if (!body.keyword_name) return alert("請先輸入 keyword_name");
    if (body.id) await api(`/api/osc/document-keywords/${Number(body.id)}`, "PUT", body);
    else await api(`/api/osc/document-keywords`, "POST", body);
    ["docKwId", "docKwCase", "docKwName", "docKwCategory", "docKwHotkey", "docKwCaseSpecific", "docKwUsageCount", "docKwContent"].forEach(id => {
        const el = document.getElementById(id); if (el) el.value = "";
    });
    await loadDocumentKeywords();
    await loadMeta();
}

async function delDocumentKeyword(id) {
    if (!confirm(`確定刪除關鍵字 ${id}？`)) return;
    await api(`/api/osc/document-keywords/${Number(id)}`, "DELETE");
    await loadDocumentKeywords();
    await loadMeta();
}

async function loadDocumentReplacements() {
    const q = encodeURIComponent((document.getElementById("docRpQ").value || "").trim());
    const caseNumber = encodeURIComponent((document.getElementById("docRpCaseNumber").value || "").trim());
    const data = await api(`/api/osc/document-replacements?limit=400&q=${q}&case_number=${caseNumber}`);
    state.docReplacements = data.items || [];
    const body = document.getElementById("docRpBody");
    if (!state.docReplacements.length) {
        body.innerHTML = `<tr><td colspan="8" class="muted">沒有替換紀錄</td></tr>`;
        return;
    }
    body.innerHTML = state.docReplacements.map(r => `
    <tr>
        <td>${esc(r.replaced_date || "")}</td>
        <td>${esc(r.template_file || "")}</td>
        <td>${esc(r.new_case_number || "")}</td>
        <td>${esc(r.old_client_name || "")}</td>
        <td>${esc(r.new_client_name || "")}</td>
        <td>${esc(r.old_data || "")}</td>
        <td>${esc(r.new_data || "")}</td>
        <td class="actions">
            <button class="btn danger" data-act="doc-rp-del" data-id="${Number(r.id)}">刪除</button>
        </td>
    </tr>
`).join("");
}

async function delDocumentReplacement(id) {
    if (!confirm(`確定刪除替換紀錄 ${id}？`)) return;
    await api(`/api/osc/document-replacements/${Number(id)}`, "DELETE");
    await loadDocumentReplacements();
    await loadMeta();
}

async function openDocumentPath(path) {
    if (!isLocalConsole()) {
        window.open(fileContentUrl(path, true), "_blank", "noopener,noreferrer");
        return;
    }
    const data = await api("/api/osc/documents/open", "POST", { path });
    const result = data.open_result || {};
    if (result.ok) return;
    const smb = (data.smb_candidates || [])[0] || "";
    if (smb) window.open(smb, "_blank");
    alert(`無法直接開啟，請手動使用路徑：\n${path}`);
}

async function copyDocumentPath(path) {
    const text = String(path || "").trim();
    if (!text) return;
    try {
        await navigator.clipboard.writeText(text);
        showToast("檔案路徑已複製。", "ok");
    } catch {
        alert("複製失敗，請手動複製");
    }
}

async function runDocCaseAction(action) {
    const caseId = (document.getElementById("docActionCaseId").value || "").trim();
    if (!caseId) {
        alert("請先輸入案件 ID");
        return;
    }
    const data = await api(`/api/osc/cases/${encodeURIComponent(caseId)}/quick-action`, "POST", { action });
    alert(data.reply || "已完成");
}

function collectFormPayload() {
    return {
        form_type: (document.getElementById("formType").value || "").trim(),
        case_id: (document.getElementById("formCaseId").value || "").trim(),
        case_number: (document.getElementById("formCaseNumber").value || "").trim(),
        client_name: (document.getElementById("formClientName").value || "").trim(),
        fields: {
            date: (document.getElementById("formDate").value || "").trim(),
            receipt_no: (document.getElementById("formReceiptNo").value || "").trim(),
            amount: (document.getElementById("formAmount").value || "").trim(),
            item: (document.getElementById("formItem").value || "").trim(),
            payment_method: (document.getElementById("formPaymentMethod").value || "").trim(),
            lawyer_name: (document.getElementById("formLawyerName").value || "").trim(),
            court_case_no: (document.getElementById("formCourtCaseNo").value || "").trim(),
            laf_case_no: (document.getElementById("formLafCaseNo").value || "").trim(),
            sender_name: (document.getElementById("formSenderName")?.value || "").trim(),
            sender_addr: (document.getElementById("formSenderAddr")?.value || "").trim(),
            receiver_name: (document.getElementById("formReceiverName")?.value || "").trim(),
            receiver_addr: (document.getElementById("formReceiverAddr")?.value || "").trim(),
            notes: (document.getElementById("formNotes").value || "").trim(),
        }
    };
}

function renderFormPreview(data) {
    const meta = document.getElementById("formPreviewMeta");
    const box = document.getElementById("formPreviewText");
    const c = data.case || {};
    const exPdf = data.export_pdf || {};
    const exDocx = data.export_docx || {};
    const links = [];
    if (exPdf.url) links.push(`PDF：${exPdf.url}`);
    if (exDocx.url) links.push(`WORD：${exDocx.url}`);
    meta.textContent = `類型：${data.title || data.form_type || "-"} ｜ 案號：${c.case_number || "-"} ｜ 當事人：${c.client_name || "-"}${links.length ? ` ｜ 下載：${links.join(" ｜ ")}` : ""}`;
    box.textContent = data.preview_text || "(空白)";
}

async function previewForm() {
    const payload = collectFormPayload();
    const data = await api("/api/osc/forms/preview", "POST", payload);
    state.formPreview = data;
    renderFormPreview(data);
}

async function exportForm() {
    const payload = collectFormPayload();
    const data = await api("/api/osc/forms/export", "POST", payload);
    state.formPreview = data;
    renderFormPreview(data);
    const urls = [];
    if (data.export_pdf?.url) urls.push(data.export_pdf.url);
    if (data.export_docx?.url) urls.push(data.export_docx.url);
    if (urls.length) {
        urls.forEach((u) => window.open(u, "_blank"));
        return;
    }
    const errs = Array.isArray(data.export_errors) ? data.export_errors : [];
    const errText = errs.map((e) => `${e.type || "file"}: ${e.error || "unknown_error"}`).join("\n");
    if (errText) {
        alert(`匯出失敗：\n${errText}`);
    } else {
        alert("已產出檔案，但目前沒有公開下載網址。");
    }
}

async function runLafWizard(mode) {
    const payload = {
        mode,
        action: (document.getElementById("lafWizardAction").value || "").trim(),
        case_id: (document.getElementById("lafWizardCaseId").value || "").trim(),
        case_number: (document.getElementById("lafWizardCaseNumber").value || "").trim(),
        laf_case_no: (document.getElementById("lafWizardLafCaseNo").value || "").trim(),
        client_name: (document.getElementById("lafWizardClientName").value || "").trim(),
        reason: (document.getElementById("lafWizardReason").value || "").trim(),
        fields: parseMaybeJson(document.getElementById("lafWizardFields").value || ""),
    };
    if (mode === "submit") {
        if (payload.action !== "go_live") {
            alert("送出模式目前僅允許開辦（go_live）。");
            return;
        }
        if (!confirm("確定要送出？此動作可能影響正式法扶資料。")) return;
    }
    const data = await api("/api/osc/laf-wizard/run", "POST", payload);
    state.lafWizardResult = data;
    const sum = document.getElementById("lafWizardSummary");
    const rs = data.result || {};
    sum.textContent = `模式：${data.mode || "-"} ｜ 動作：${data.action || "-"} ｜ 結果：${data.ok ? "成功" : "失敗"}${rs.error ? ` ｜ 錯誤：${rs.error}` : ""}`;
    document.getElementById("lafWizardResult").textContent = JSON.stringify(data, null, 2);
    const links = document.getElementById("lafWizardLinks");
    links.innerHTML = "";
    const art = data.artifact || {};
    const png = art.png_export?.url || "";
    const html = art.html_export?.url || "";
    if (png) {
        const b = document.createElement("button");
        b.className = "btn";
        b.textContent = "開啟預覽截圖";
        b.onclick = () => window.open(png, "_blank");
        links.appendChild(b);
    }
    if (html) {
        const b = document.createElement("button");
        b.className = "btn";
        b.textContent = "開啟頁面 HTML";
        b.onclick = () => window.open(html, "_blank");
        links.appendChild(b);
    }
}

async function loadArchivePreview() {
    const data = await api("/api/osc/archive-wizard/preview?limit=500");
    state.archivePreview = data.items || [];
    const sum = data.summary || {};
    document.getElementById("archiveSummary").textContent = `封存根目錄：${data.archive_local || data.archive_base || "-"} ｜ 總數 ${sum.total || 0} ｜ 可搬移 ${sum.ready || 0} ｜ 缺來源 ${sum.missing_source || 0} ｜ 目標已存在 ${sum.target_exists || 0}`;
    const body = document.getElementById("archiveBody");
    if (!state.archivePreview.length) {
        body.innerHTML = `<tr><td colspan="7" class="muted">沒有可檢視資料</td></tr>`;
        return;
    }
    body.innerHTML = state.archivePreview.map(r => `
    <tr>
        <td><input type="checkbox" class="archive-pick" data-id="${esc(r.id)}" ${r.ready ? "checked" : ""}></td>
        <td>${esc(r.case_number || "")}</td>
        <td>${esc(r.client_name || "")}</td>
        <td>${esc(r.status || "")}</td>
        <td title="${esc(r.source_local || r.source_path || "")}">${esc(r.source_local || r.source_path || "")}</td>
        <td title="${esc(r.target_local || "")}">${esc(r.target_local || "")}</td>
        <td>${esc(r.reason || "")}</td>
    </tr>
`).join("");
}

async function executeArchiveMove() {
    const force = !!document.getElementById("archiveForce").checked;
    const picks = Array.from(document.querySelectorAll(".archive-pick:checked")).map(el => String(el.dataset.id || "").trim()).filter(Boolean);
    if (!picks.length) {
        alert("請先勾選要搬移的案件。");
        return;
    }
    if (!confirm(`確定搬移 ${picks.length} 筆已結案案件？`)) return;
    const data = await api("/api/osc/archive-wizard/execute", "POST", { confirm: true, case_ids: picks, force });
    const s = data.summary || {};
    const skipped = (data.skipped || []).slice(0, 3).map(x => `${x.case_number || x.id || "-"}：${x.reason || x.error || "略過"}`);
    const errors = (data.errors || []).slice(0, 3).map(x => `${x.case_number || x.id || "-"}：${x.error || "錯誤"}`);
    const detail = skipped.concat(errors).join("\n");
    alert(`搬移完成：已搬移 ${s.moved || 0}，略過 ${s.skipped || 0}，錯誤 ${s.errors || 0}${detail ? "\n\n" + detail : ""}`);
    await loadArchivePreview();
    await loadCases();
    await loadMeta();
}
