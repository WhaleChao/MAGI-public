// checklists.js — OSC P1 法扶補件清單 + 案件補正清單
// Handles: legal_aid_checklists (5 endpoints) + case_checklists (4 endpoints)

// ── Helpers ──────────────────────────────────────────────────────────────────

function _checklistToast(msg, isError) {
    if (typeof showToast === "function") {
        showToast(msg, isError ? "error" : "success");
    } else {
        alert(msg);
    }
}

function _checklistCaseNumber(inputId) {
    return (document.getElementById(inputId)?.value || "").trim();
}

// ── LAF Checklist ─────────────────────────────────────────────────────────────

function loadLafChecklist() {
    const caseNumber = _checklistCaseNumber("lafChecklistCaseNumber");
    if (!caseNumber) { _checklistToast("請輸入案件編號", true); return; }
    fetch(`/api/osc/checklists/legal-aid?case_number=${encodeURIComponent(caseNumber)}`)
        .then(r => r.json())
        .then(data => {
            if (!data.ok) { _checklistToast("載入失敗：" + data.error, true); return; }
            renderLafChecklistRows(caseNumber, data.items);
        })
        .catch(e => _checklistToast("載入錯誤：" + e.message, true));
}

function renderLafChecklistRows(caseNumber, items) {
    const tbody = document.getElementById("lafChecklistMgmtBody");
    if (!tbody) return;
    if (!items || items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="muted">尚無補件項目。可使用「填入預設清單」快速建立。</td></tr>';
        return;
    }
    tbody.innerHTML = items.map(item => {
        const lu = item.last_updated ? item.last_updated.replace("T", " ").slice(0, 16) : "-";
        return `<tr>
            <td>${_escHtml(item.item_label || item.item_key)}</td>
            <td>
                <select class="laf-cl-status" data-id="${item.id}" style="width:80px">
                    ${["待補","已備齊","不適用"].map(s =>
                        `<option${item.status===s?" selected":""}>${s}</option>`
                    ).join("")}
                </select>
            </td>
            <td><input class="laf-cl-notes" data-id="${item.id}" value="${_escAttr(item.notes||"")}" style="width:140px" placeholder="備註"></td>
            <td class="muted" style="font-size:11px">${lu}</td>
            <td>
                <button class="btn btn-sm" onclick="updateLafChecklistRow(${item.id})">儲存</button>
                <button class="btn btn-sm warn" onclick="delLafChecklistRow(${item.id},'${_escAttr(caseNumber)}')">刪除</button>
            </td>
        </tr>`;
    }).join("");
}

function updateLafChecklistRow(id) {
    const status = document.querySelector(`.laf-cl-status[data-id="${id}"]`)?.value;
    const notes = document.querySelector(`.laf-cl-notes[data-id="${id}"]`)?.value || "";
    fetch(`/api/osc/checklists/legal-aid/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status, notes }),
    })
        .then(r => r.json())
        .then(d => {
            if (d.ok) _checklistToast("已儲存");
            else _checklistToast("儲存失敗：" + d.error, true);
        })
        .catch(e => _checklistToast("儲存錯誤：" + e.message, true));
}

function delLafChecklistRow(id, caseNumber) {
    if (!confirm("確定刪除此補件項目？")) return;
    fetch(`/api/osc/checklists/legal-aid/${id}`, { method: "DELETE" })
        .then(r => r.json())
        .then(d => {
            if (d.ok) { _checklistToast("已刪除"); loadLafChecklist(); }
            else _checklistToast("刪除失敗：" + d.error, true);
        })
        .catch(e => _checklistToast("刪除錯誤：" + e.message, true));
}

function seedLafChecklist() {
    const caseNumber = _checklistCaseNumber("lafChecklistCaseNumber");
    if (!caseNumber) { _checklistToast("請輸入案件編號", true); return; }
    if (!confirm(`確定要為案件「${caseNumber}」填入法扶預設補件清單？`)) return;
    fetch("/api/osc/checklists/legal-aid/seed", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ case_number: caseNumber }),
    })
        .then(r => r.json())
        .then(d => {
            if (d.ok) {
                _checklistToast(`已填入 ${d.inserted_count} 項，略過 ${d.skipped_count} 項（已存在）`);
                loadLafChecklist();
            } else _checklistToast("填入失敗：" + d.error, true);
        })
        .catch(e => _checklistToast("填入錯誤：" + e.message, true));
}

function addLafChecklistItem() {
    const caseNumber = _checklistCaseNumber("lafChecklistCaseNumber");
    if (!caseNumber) { _checklistToast("請先輸入案件編號並載入", true); return; }
    const item_label = (document.getElementById("lafChecklistNewLabel")?.value || "").trim();
    if (!item_label) { _checklistToast("請輸入項目標籤", true); return; }
    const status = document.getElementById("lafChecklistNewStatus")?.value || "待補";
    const notes = (document.getElementById("lafChecklistNewNotes")?.value || "").trim();
    fetch("/api/osc/checklists/legal-aid", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ case_number: caseNumber, item_label, status, notes }),
    })
        .then(r => r.json())
        .then(d => {
            if (d.ok) {
                _checklistToast("已新增");
                document.getElementById("lafChecklistNewLabel").value = "";
                document.getElementById("lafChecklistNewNotes").value = "";
                loadLafChecklist();
            } else _checklistToast("新增失敗：" + d.error, true);
        })
        .catch(e => _checklistToast("新增錯誤：" + e.message, true));
}

// ── Case Checklist ────────────────────────────────────────────────────────────

function loadCaseChecklist() {
    const caseNumber = _checklistCaseNumber("caseChecklistCaseNumber");
    if (!caseNumber) { _checklistToast("請輸入案件編號", true); return; }
    fetch(`/api/osc/checklists/case?case_number=${encodeURIComponent(caseNumber)}`)
        .then(r => r.json())
        .then(data => {
            if (!data.ok) { _checklistToast("載入失敗：" + data.error, true); return; }
            renderCaseChecklistRows(caseNumber, data.items);
        })
        .catch(e => _checklistToast("載入錯誤：" + e.message, true));
}

function renderCaseChecklistRows(caseNumber, items) {
    const tbody = document.getElementById("caseChecklistBody");
    if (!tbody) return;
    if (!items || items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" class="muted">尚無補正項目。</td></tr>';
        return;
    }
    tbody.innerHTML = items.map(item => `<tr>
        <td>${_escHtml(item.item_label)}</td>
        <td>
            <select class="case-cl-status" data-id="${item.id}" style="width:80px">
                ${["待補","已備齊","不適用"].map(s =>
                    `<option${item.status===s?" selected":""}>${s}</option>`
                ).join("")}
            </select>
        </td>
        <td><input class="case-cl-notes" data-id="${item.id}" value="${_escAttr(item.notes||"")}" style="width:140px" placeholder="備註"></td>
        <td>
            <button class="btn btn-sm" onclick="updateCaseChecklistRow(${item.id})">儲存</button>
            <button class="btn btn-sm warn" onclick="delCaseChecklistRow(${item.id},'${_escAttr(caseNumber)}')">刪除</button>
        </td>
    </tr>`).join("");
}

function updateCaseChecklistRow(id) {
    const status = document.querySelector(`.case-cl-status[data-id="${id}"]`)?.value;
    const notes = document.querySelector(`.case-cl-notes[data-id="${id}"]`)?.value || "";
    fetch(`/api/osc/checklists/case/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status, notes }),
    })
        .then(r => r.json())
        .then(d => {
            if (d.ok) _checklistToast("已儲存");
            else _checklistToast("儲存失敗：" + d.error, true);
        })
        .catch(e => _checklistToast("儲存錯誤：" + e.message, true));
}

function delCaseChecklistRow(id, caseNumber) {
    if (!confirm("確定刪除（軟刪除）此補正項目？")) return;
    fetch(`/api/osc/checklists/case/${id}`, { method: "DELETE" })
        .then(r => r.json())
        .then(d => {
            if (d.ok) { _checklistToast("已刪除"); loadCaseChecklist(); }
            else _checklistToast("刪除失敗：" + d.error, true);
        })
        .catch(e => _checklistToast("刪除錯誤：" + e.message, true));
}

function addCaseChecklistItem() {
    const caseNumber = _checklistCaseNumber("caseChecklistCaseNumber");
    if (!caseNumber) { _checklistToast("請先輸入案件編號並載入", true); return; }
    const item_label = (document.getElementById("caseChecklistNewLabel")?.value || "").trim();
    if (!item_label) { _checklistToast("請輸入項目標籤", true); return; }
    const status = document.getElementById("caseChecklistNewStatus")?.value || "待補";
    const notes = (document.getElementById("caseChecklistNewNotes")?.value || "").trim();
    fetch("/api/osc/checklists/case", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ case_number: caseNumber, item_label, status, notes }),
    })
        .then(r => r.json())
        .then(d => {
            if (d.ok) {
                _checklistToast("已新增");
                document.getElementById("caseChecklistNewLabel").value = "";
                document.getElementById("caseChecklistNewNotes").value = "";
                loadCaseChecklist();
            } else _checklistToast("新增失敗：" + d.error, true);
        })
        .catch(e => _checklistToast("新增錯誤：" + e.message, true));
}

// ── Escape helpers ────────────────────────────────────────────────────────────

function _escHtml(s) {
    return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
function _escAttr(s) {
    return String(s).replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}
