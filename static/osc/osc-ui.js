/* osc-ui.js – UI helpers: toast, modal, busy state, button feedback */
function syncFormTypeFields() {
    const formType = document.getElementById("formType");
    if (!formType) return;
    const isLal = formType.value === "legal_attest";
    ["formSenderNameField", "formSenderAddrField", "formReceiverNameField", "formReceiverAddrField"].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.display = isLal ? "" : "none";
    });
}

function msg(role, text) {
    const box = document.getElementById("messages");
    const div = document.createElement("div");
    div.className = `msg ${role}`;
    div.textContent = text;
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
}

async function sendChat() {
    const el = document.getElementById("chatInput");
    const text = (el.value || "").trim();
    if (!text) return;
    el.value = "";
    msg("user", text);
    try {
        const data = await api("/api/osc/chat", "POST", { message: text });
        msg("casper", data.reply || "(無回覆)");
    } catch (e) {
        msg("sys", `送出失敗：${e.message}`);
    }
}

async function pollChat() {
    try {
        const data = await api("/api/osc/poll");
        (data.messages || []).forEach(m => msg("casper", m));
    } catch (_e) { }
    setTimeout(pollChat, 3000);
}

async function loadMeta() {
    const dbBadge = document.getElementById("dbBadge");
    const countBadge = document.getElementById("countBadge");
    try {
        const res = await fetch("/api/osc/meta");
        const data = await res.json().catch(() => ({ ok: false, error: res.statusText }));
        const fo = data.failover || {};
        const foTag = (fo.failover_active ? " [本機備援]" : "") + (fo.syncing ? " [同步中]" : "");
        if (!res.ok || !data.ok) {
            dbBadge.classList.remove("ok");
            let hint = "";
            if (fo.remote_ok === false) hint = " [遠端不可達]";
            dbBadge.textContent = `DB: 連線失敗 (${data.error || res.statusText})${foTag}${hint}`;
            return;
        }
        const db = data.db || {};
        dbBadge.classList.add("ok");
        dbBadge.textContent = `DB: ${db.host}:${db.port}/${db.database} (${db.user})${foTag}`;
        const c = data.counts || {};
        countBadge.textContent = `案件 ${c.cases ?? "-"} | 當事人 ${c.clients ?? "-"} | 會議 ${c.meetings ?? "-"} | 行事曆 ${c.calendar_events ?? "-"} | 待辦 ${c.case_todos ?? "-"} | 法扶清單 ${c.legal_aid_checklists ?? "-"} | 法扶流程 ${c.laf_lifecycle_log ?? "-"} | 法扶信件 ${c.laf_email_records ?? "-"} | 見解 ${c.legal_insights ?? "-"} | 裁判 ${c.court_judgments ?? "-"} | 帳務 ${c.case_transactions ?? "-"} | 文件 ${c.document_index ?? "-"} | 書狀模板 ${c.document_templates ?? "-"} | 關鍵字 ${c.document_keywords ?? "-"} | 固定支出 ${c.recurring_expenses ?? "-"} | 報價 ${c.quotations ?? "-"} | 報價模板 ${c.quotation_templates ?? "-"}`;
    } catch (e) {
        dbBadge.classList.remove("ok");
        dbBadge.textContent = `DB: 連線失敗 (${e.message})`;
    }
}

function renderSimpleRows(targetId, rows, colspan, emptyText) {
    const body = document.getElementById(targetId);
    if (!body) return;
    if (!rows.length) {
        body.innerHTML = `<tr><td colspan="${colspan}" class="muted">${emptyText}</td></tr>`;
        return;
    }
    body.innerHTML = rows.join("");
}

function setDraftStatus(text, tone = "info") {
    const el = document.getElementById("draftStatus");
    if (!el) return;
    el.textContent = text || "";
    el.className = `status-banner${tone === "warn" || tone === "error" ? " warn" : ""}`;
}

async function withBusy(buttonId, busyLabel, fn) {
    const btn = buttonId ? document.getElementById(buttonId) : null;
    if (btn && btn.disabled) return; // prevent duplicate calls
    const original = btn ? btn.textContent : "";
    if (btn) {
        btn.disabled = true;
        if (busyLabel) btn.textContent = busyLabel;
    }
    try {
        return await fn();
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = original;
        }
    }
}

function inferBusyLabel(btn) {
    const text = (btn?.textContent || "").trim();
    if (!text) return "處理中...";
    if (text.includes("搜尋")) return "搜尋中...";
    if (text.includes("重新整理") || text.includes("載入")) return "讀取中...";
    if (text.includes("儲存")) return "儲存中...";
    if (text.includes("預覽")) return "預覽中...";
    if (text.includes("匯出")) return "匯出中...";
    if (text.includes("送出")) return "送出中...";
    if (text.includes("執行")) return "執行中...";
    if (text.includes("抓")) return "抓取中...";
    if (text.includes("生成") || text.includes("製作")) return "生成中...";
    if (text.includes("套用")) return "套用中...";
    return "處理中...";
}

function reportUiError(actionLabel, error) {
    console.error(error);
    alert(`${actionLabel || "操作"}失敗：${error.message}`);
}

async function runBusyAction(buttonId, fn, opts = {}) {
    const btn = buttonId ? document.getElementById(buttonId) : null;
    const actionLabel = opts.actionLabel || (btn?.textContent || "").trim() || "操作";
    const busyLabel = opts.busyLabel || inferBusyLabel(btn);
    try {
        return await withBusy(buttonId, busyLabel, fn);
    } catch (error) {
        if (typeof opts.onError === "function") {
            return opts.onError(error);
        }
        reportUiError(actionLabel, error);
    }
}

function bindBusyClick(buttonId, fn, opts = {}) {
    const btn = document.getElementById(buttonId);
    if (!btn) return;
    btn.addEventListener("click", () => runBusyAction(buttonId, fn, opts));
}

function bindEnterSubmit(inputIds, buttonId, fn, opts = {}) {
    inputIds.forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        el.addEventListener("keydown", (e) => {
            if (e.key !== "Enter" || e.shiftKey) return;
            e.preventDefault();
            runBusyAction(buttonId, fn, opts);
        });
    });
}

function showToast(text, tone = "info", duration = 2200) {
    const host = document.getElementById("toastStack");
    if (!host || !text) return;
    const item = document.createElement("div");
    item.className = `toast${tone === "warn" || tone === "error" ? " warn" : tone === "ok" || tone === "success" ? " ok" : ""}`;
    item.textContent = text;
    host.appendChild(item);
    requestAnimationFrame(() => item.classList.add("show"));
    setTimeout(() => {
        item.classList.remove("show");
        setTimeout(() => item.remove(), 180);
    }, duration);
}

function wbSetStatus(text, tone = "info") {
    const el = document.getElementById("wbStatus");
    if (!el) return;
    if (!text) {
        el.hidden = true;
        el.textContent = "";
        el.className = "status-banner";
        return;
    }
    el.hidden = false;
    el.textContent = text;
    el.className = `status-banner${tone === "warn" || tone === "error" ? " warn" : tone === "ok" || tone === "success" ? " ok" : ""}`;
}

async function withElementBusy(el, busyLabel, fn) {
    const btn = el || null;
    const original = btn ? btn.textContent : "";
    const originalClass = btn ? btn.className : "";
    if (btn) {
        btn.disabled = true;
        if (busyLabel) btn.textContent = busyLabel;
    }
    try {
        return await fn();
    } finally {
        if (btn && btn.isConnected) {
            btn.disabled = false;
            btn.textContent = original;
            btn.className = originalClass;
        }
    }
}

function flashButtonFeedback(el, text, tone = "ok", duration = 1000) {
    if (!el || !el.isConnected || !text) return;
    const originalText = el.textContent;
    const originalClass = el.className;
    el.textContent = text;
    if (tone === "warn" || tone === "error") el.classList.add("warn");
    else if (tone === "ok" || tone === "success") el.classList.add("ok");
    setTimeout(() => {
        if (!el.isConnected) return;
        el.textContent = originalText;
        el.className = originalClass;
    }, duration);
}

function normalizeActionText(text) {
    return String(text || "").replace(/（.*?）/g, "").replace(/\s+/g, " ").trim();
}

function buildActionSuccessText(label, successLabel) {
    if (successLabel === "已帶入") return `${label}已帶入表單。`;
    if (successLabel === "已刪除") return `${label}已刪除。`;
    if (successLabel === "已複製") return `${label}已複製。`;
    if (successLabel === "已開啟") return `${label}已送出開啟動作。`;
    if (successLabel === "已更新") return `${label}已更新。`;
    if (successLabel === "已儲存") return `${label}已儲存。`;
    if (successLabel === "已清空") return `${label}已清空。`;
    if (successLabel === "已送出") return `${label}已送出。`;
    return `${label}完成。`;
}

function getDelegatedActionFeedback(act, button) {
    if (!act) return null;
    const label = normalizeActionText(button?.textContent || "") || "操作";
    const inWorkbench = !!button?.closest(".modal");
    const meta = {
        actionLabel: label,
        busyLabel: inferBusyLabel(button),
        successLabel: "完成",
        successTone: "ok",
        successText: `${label}完成。`,
        flash: true,
        showToast: false,
        inWorkbench,
        applyWorkbenchStatus: inWorkbench,
    };
    if (act.endsWith("-edit")) {
        meta.busyLabel = "載入中...";
        meta.successLabel = "已帶入";
    } else if (act.endsWith("-del")) {
        meta.busyLabel = "刪除中...";
        meta.successLabel = "已刪除";
        meta.showToast = true;
    } else if (act.endsWith("-open")) {
        meta.busyLabel = "開啟中...";
        meta.successLabel = "已開啟";
        meta.showToast = true;
    } else if (act.endsWith("-copy")) {
        meta.busyLabel = "複製中...";
        meta.successLabel = "已複製";
    } else if (act.endsWith("-workbench")) {
        meta.busyLabel = "載入中...";
        meta.successLabel = "已開啟";
        meta.applyWorkbenchStatus = false;
    } else if (act.endsWith("-fetch")) {
        meta.busyLabel = "抓取中...";
        meta.successLabel = "已更新";
    } else if (act.endsWith("-toggle")) {
        meta.busyLabel = "更新中...";
        meta.successLabel = "已更新";
        meta.flash = false;
    } else if (act.endsWith("-save")) {
        meta.busyLabel = "儲存中...";
        meta.successLabel = "已儲存";
        meta.showToast = true;
    } else if (act === "wb-case-action") {
        meta.busyLabel = "處理中...";
        meta.successLabel = "已送出";
        meta.applyWorkbenchStatus = false;
    } else if (act === "wb-todo-reset") {
        meta.busyLabel = "";
        meta.successLabel = "已清空";
        meta.applyWorkbenchStatus = false;
    } else if (act === "wb-todo-save") {
        meta.applyWorkbenchStatus = false;
    }
    meta.successText = buildActionSuccessText(label, meta.successLabel);
    return meta;
}

function setDraftModeIndicator(mode) {
    const el = document.getElementById("draftModeIndicator");
    if (!el) return;
    if (!mode) {
        el.style.display = "none";
        el.textContent = "";
        return;
    }
    el.style.display = "inline-block";
    if (mode === "preview") {
        el.className = "draft-mode-indicator preview";
        el.textContent = "Prompt 預覽";
    } else {
        el.className = "draft-mode-indicator generated";
        el.textContent = "AI 生成結果";
    }
}

function readFields(ids) {
    const out = {};
    ids.forEach(id => out[id] = document.getElementById(id).value || "");
    return out;
}

function writeFields(prefix, obj, fields) {
    fields.forEach(f => {
        const el = document.getElementById(`${prefix}${f}`);
        if (!el) return;
        let v = obj?.[f] ?? "";
        if ((el.type === "datetime-local") && v) v = String(v).replace(" ", "T").slice(0, 16);
        el.value = v;
    });
}

function clearFields(ids) {
    ids.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = "";
    });
}

function parseMaybeJson(text) {
    const s = String(text || "").trim();
    if (!s) return {};
    try { return JSON.parse(s); } catch { return {}; }
}

function fmtAmount(v) {
    const n = Number(v || 0);
    if (!Number.isFinite(n)) return "0";
    return n.toLocaleString("zh-TW", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
}

function wbShow(title, html) {
    document.getElementById("wbTitle").textContent = title;
    document.getElementById("wbBody").innerHTML = html;
    document.getElementById("wbMask").classList.add("show");
}

function wbClose() {
    state.wb = { mode: null, id: null, data: null };
    document.getElementById("wbMask").classList.remove("show");
    document.getElementById("wbBody").innerHTML = "";
    wbSetStatus("");
}
