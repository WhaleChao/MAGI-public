// checklists.js — OSC P1 法扶補件清單 + 案件補正清單
// Handles: legal_aid_checklists (5 endpoints) + case_checklists (4 endpoints)

// ── Helpers ──────────────────────────────────────────────────────────────────

function _checklistToast(msg, isError) {
    if (typeof showToast === "function") {
        showToast(msg, isError ? "error" : "success");
    } else if (typeof showAlert === "function") {
        showAlert("MAGI說", msg);
    } else {
        console.warn("[MAGI checklist]", msg);
    }
}

function _checklistCaseNumber(inputId) {
    return (document.getElementById(inputId)?.value || "").trim();
}

async function _checklistApi(path, method = "GET", body = null) {
    if (typeof api !== "function") throw new Error("MAGI API helper unavailable");
    return await api(path, method, body);
}

// ── LAF Checklist ─────────────────────────────────────────────────────────────

async function loadLafChecklist() {
    const caseNumber = _checklistCaseNumber("lafChecklistCaseNumber");
    if (!caseNumber) { _checklistToast("請輸入案件編號", true); return; }
    try {
        const data = await _checklistApi(`/api/osc/checklists/legal-aid?case_number=${encodeURIComponent(caseNumber)}`);
        if (!data.ok) { _checklistToast("載入失敗：" + data.error, true); return; }
        renderLafChecklistRows(data.items);
    } catch (e) {
        _checklistToast("載入錯誤：" + e.message, true);
    }
}

function renderLafChecklistRows(items) {
    const tbody = document.getElementById("lafChecklistMgmtBody");
    if (!tbody) return;
    if (!items || items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="muted">尚無補件項目。可使用「填入預設清單」快速建立。</td></tr>';
        return;
    }
    tbody.innerHTML = items.map(item => {
        const id = _escAttr(item.id);
        const lu = item.last_updated ? _escHtml(item.last_updated.replace("T", " ").slice(0, 16)) : "-";
        return `<tr>
            <td>${_escHtml(item.item_label || item.item_key)}</td>
            <td>
                <select class="laf-cl-status" data-id="${id}" style="width:80px">
                    ${["待補","已備齊","不適用"].map(s =>
                        `<option${item.status===s?" selected":""}>${s}</option>`
                    ).join("")}
                </select>
            </td>
            <td><input class="laf-cl-notes" data-id="${id}" value="${_escAttr(item.notes||"")}" style="width:140px" placeholder="備註"></td>
            <td class="muted" style="font-size:11px">${lu}</td>
            <td>
                <button class="btn btn-sm" type="button" data-checklist-action="laf-save" data-id="${id}">儲存</button>
                <button class="btn btn-sm warn" type="button" data-checklist-action="laf-delete" data-id="${id}">刪除</button>
            </td>
        </tr>`;
    }).join("");
}

async function updateLafChecklistRow(id) {
    const status = document.querySelector(`.laf-cl-status[data-id="${id}"]`)?.value;
    const notes = document.querySelector(`.laf-cl-notes[data-id="${id}"]`)?.value || "";
    try {
        const d = await _checklistApi(`/api/osc/checklists/legal-aid/${encodeURIComponent(id)}`, "PUT", { status, notes });
        if (d.ok) _checklistToast("已儲存");
        else _checklistToast("儲存失敗：" + d.error, true);
    } catch (e) {
        _checklistToast("儲存錯誤：" + e.message, true);
    }
}

async function delLafChecklistRow(id) {
    if (!await showConfirm("MAGI說", "確定刪除此補件項目？")) return;
    try {
        const d = await _checklistApi(`/api/osc/checklists/legal-aid/${encodeURIComponent(id)}`, "DELETE");
        if (d.ok) { _checklistToast("已刪除"); loadLafChecklist(); }
        else _checklistToast("刪除失敗：" + d.error, true);
    } catch (e) {
        _checklistToast("刪除錯誤：" + e.message, true);
    }
}

async function seedLafChecklist() {
    const caseNumber = _checklistCaseNumber("lafChecklistCaseNumber");
    if (!caseNumber) { _checklistToast("請輸入案件編號", true); return; }
    if (!await showConfirm("MAGI說", `確定要為案件「${caseNumber}」填入法扶預設補件清單？`)) return;
    try {
        const d = await _checklistApi("/api/osc/checklists/legal-aid/seed", "POST", { case_number: caseNumber });
        if (d.ok) {
            _checklistToast(`已填入 ${d.inserted_count} 項，略過 ${d.skipped_count} 項（已存在）`);
            loadLafChecklist();
        } else _checklistToast("填入失敗：" + d.error, true);
    } catch (e) {
        _checklistToast("填入錯誤：" + e.message, true);
    }
}

async function addLafChecklistItem() {
    const caseNumber = _checklistCaseNumber("lafChecklistCaseNumber");
    if (!caseNumber) { _checklistToast("請先輸入案件編號並載入", true); return; }
    const item_label = (document.getElementById("lafChecklistNewLabel")?.value || "").trim();
    if (!item_label) { _checklistToast("請輸入項目標籤", true); return; }
    const status = document.getElementById("lafChecklistNewStatus")?.value || "待補";
    const notes = (document.getElementById("lafChecklistNewNotes")?.value || "").trim();
    try {
        const d = await _checklistApi("/api/osc/checklists/legal-aid", "POST", { case_number: caseNumber, item_label, status, notes });
        if (d.ok) {
            _checklistToast("已新增");
            document.getElementById("lafChecklistNewLabel").value = "";
            document.getElementById("lafChecklistNewNotes").value = "";
            loadLafChecklist();
        } else _checklistToast("新增失敗：" + d.error, true);
    } catch (e) {
        _checklistToast("新增錯誤：" + e.message, true);
    }
}

// ── Case Checklist ────────────────────────────────────────────────────────────

async function loadCaseChecklist() {
    const caseNumber = _checklistCaseNumber("caseChecklistCaseNumber");
    if (!caseNumber) { _checklistToast("請輸入案件編號", true); return; }
    try {
        const data = await _checklistApi(`/api/osc/checklists/case?case_number=${encodeURIComponent(caseNumber)}`);
        if (!data.ok) { _checklistToast("載入失敗：" + data.error, true); return; }
        renderCaseChecklistRows(data.items);
    } catch (e) {
        _checklistToast("載入錯誤：" + e.message, true);
    }
}

function renderCaseChecklistRows(items) {
    const tbody = document.getElementById("caseChecklistBody");
    if (!tbody) return;
    if (!items || items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" class="muted">尚無補正項目。</td></tr>';
        return;
    }
    tbody.innerHTML = items.map(item => {
    const id = _escAttr(item.id);
    return `<tr>
        <td>${_escHtml(item.item_label)}</td>
        <td>
            <select class="case-cl-status" data-id="${id}" style="width:80px">
                ${["待補","已備齊","不適用"].map(s =>
                    `<option${item.status===s?" selected":""}>${s}</option>`
                ).join("")}
            </select>
        </td>
        <td><input class="case-cl-notes" data-id="${id}" value="${_escAttr(item.notes||"")}" style="width:140px" placeholder="備註"></td>
        <td>
            <button class="btn btn-sm" type="button" data-checklist-action="case-save" data-id="${id}">儲存</button>
            <button class="btn btn-sm warn" type="button" data-checklist-action="case-delete" data-id="${id}">刪除</button>
        </td>
    </tr>`;
    }).join("");
}

async function updateCaseChecklistRow(id) {
    const status = document.querySelector(`.case-cl-status[data-id="${id}"]`)?.value;
    const notes = document.querySelector(`.case-cl-notes[data-id="${id}"]`)?.value || "";
    try {
        const d = await _checklistApi(`/api/osc/checklists/case/${encodeURIComponent(id)}`, "PUT", { status, notes });
        if (d.ok) _checklistToast("已儲存");
        else _checklistToast("儲存失敗：" + d.error, true);
    } catch (e) {
        _checklistToast("儲存錯誤：" + e.message, true);
    }
}

async function delCaseChecklistRow(id) {
    if (!await showConfirm("MAGI說", "確定刪除（軟刪除）此補正項目？")) return;
    try {
        const d = await _checklistApi(`/api/osc/checklists/case/${encodeURIComponent(id)}`, "DELETE");
        if (d.ok) { _checklistToast("已刪除"); loadCaseChecklist(); }
        else _checklistToast("刪除失敗：" + d.error, true);
    } catch (e) {
        _checklistToast("刪除錯誤：" + e.message, true);
    }
}

async function addCaseChecklistItem() {
    const caseNumber = _checklistCaseNumber("caseChecklistCaseNumber");
    if (!caseNumber) { _checklistToast("請先輸入案件編號並載入", true); return; }
    const item_label = (document.getElementById("caseChecklistNewLabel")?.value || "").trim();
    if (!item_label) { _checklistToast("請輸入項目標籤", true); return; }
    const status = document.getElementById("caseChecklistNewStatus")?.value || "待補";
    const notes = (document.getElementById("caseChecklistNewNotes")?.value || "").trim();
    try {
        const d = await _checklistApi("/api/osc/checklists/case", "POST", { case_number: caseNumber, item_label, status, notes });
        if (d.ok) {
            _checklistToast("已新增");
            document.getElementById("caseChecklistNewLabel").value = "";
            document.getElementById("caseChecklistNewNotes").value = "";
            loadCaseChecklist();
        } else _checklistToast("新增失敗：" + d.error, true);
    } catch (e) {
        _checklistToast("新增錯誤：" + e.message, true);
    }
}

document.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-checklist-action]");
    if (!button) return;
    const id = button.dataset.id || "";
    if (!id) return;
    const action = button.dataset.checklistAction;
    if (action === "laf-save") return await updateLafChecklistRow(id);
    if (action === "laf-delete") return await delLafChecklistRow(id);
    if (action === "case-save") return await updateCaseChecklistRow(id);
    if (action === "case-delete") return await delCaseChecklistRow(id);
});

// ── Escape helpers ────────────────────────────────────────────────────────────

function _escHtml(s) {
    return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
function _escAttr(s) {
    return String(s).replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}
