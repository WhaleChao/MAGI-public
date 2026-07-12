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

function msg(role, text, html) {
    const box = document.getElementById("messages");
    const div = document.createElement("div");
    div.className = `msg ${role}`;
    if (role !== "user" && (html || role === "casper")) {
        div.innerHTML = html || renderWebReplyHtml(text || "");
    } else {
        div.textContent = text;
    }
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
        msg("casper", data.reply || "(無回覆)", data.reply_html || "");
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
    const setMobileStatus = (text, tone = "") => {
        const mobileBadge = document.getElementById("mobileStatusBadge");
        if (!mobileBadge) return;
        mobileBadge.textContent = text || "";
        mobileBadge.className = `paperclip-mobile-status${tone ? ` ${tone}` : ""}`;
    };
    try {
        // redirect:"manual" 讓 302 不被 fetch 自動跟隨，否則拿到 /login HTML 會被誤判為 DB 失敗
        const res = await fetch("/api/osc/meta", { redirect: "manual" });
        // 偵測 session expired：fetch 拿到 0/opaqueredirect/3xx → 強制跳 login
        if (res.type === "opaqueredirect" || res.status === 0 || (res.status >= 300 && res.status < 400)) {
            dbBadge.classList.remove("ok");
            dbBadge.innerHTML = `資料庫：<a href="/login?next=${encodeURIComponent(location.pathname)}" style="color:var(--apple-blue,#007aff);text-decoration:underline;">請重新登入</a>`;
            if (countBadge) countBadge.textContent = "登入逾時，請點上方連結重新登入";
            setMobileStatus("請重新登入", "warn");
            return;
        }
        const txt = await res.text();
        // 偵測 HTML response（被 redirect 跟隨拿到 login 頁）
        if (txt.trim().startsWith("<")) {
            dbBadge.classList.remove("ok");
            dbBadge.innerHTML = `資料庫：<a href="/login?next=${encodeURIComponent(location.pathname)}" style="color:var(--apple-blue,#007aff);text-decoration:underline;">請重新登入</a>`;
            if (countBadge) countBadge.textContent = "登入逾時，請點上方連結重新登入";
            setMobileStatus("請重新登入", "warn");
            return;
        }
        let data = {};
        try { data = txt ? JSON.parse(txt) : {}; } catch { data = { ok: false, error: txt || res.statusText }; }
        const fo = data.failover || {};
        const foTag = (fo.failover_active ? " [本機備援]" : "") + (fo.syncing ? " [同步中]" : "");
        if (!res.ok || !data.ok) {
            dbBadge.classList.remove("ok");
            let hint = "";
            if (fo.remote_ok === false) hint = " [遠端不可達]";
            dbBadge.textContent = `資料庫：連線失敗 (${data.error || res.statusText})${foTag}${hint}`;
            setMobileStatus(`DB 失敗${hint || foTag}`, "warn");
            return;
        }
        const db = data.db || {};
        dbBadge.classList.add("ok");
        dbBadge.textContent = `資料庫：已連線${foTag}`;
        setMobileStatus(`DB 已連線${foTag}`, "ok");
        const c = data.counts || {};
        countBadge.textContent = `案件 ${c.cases ?? "-"} | 當事人 ${c.clients ?? "-"} | 會議 ${c.meetings ?? "-"} | 行事曆 ${c.calendar_events ?? "-"} | 待辦 ${c.case_todos ?? "-"} | 法扶清單 ${c.legal_aid_checklists ?? "-"} | 法扶流程 ${c.laf_lifecycle_log ?? "-"} | 法扶信件 ${c.laf_email_records ?? "-"} | 見解 ${c.legal_insights ?? "-"} | 裁判 ${c.court_judgments ?? "-"} | 帳務 ${c.case_transactions ?? "-"} | 檔案 ${c.document_index ?? "-"} | 書狀模板 ${c.document_templates ?? "-"} | 關鍵字 ${c.document_keywords ?? "-"} | 固定支出 ${c.recurring_expenses ?? "-"} | 報價 ${c.quotations ?? "-"} | 報價模板 ${c.quotation_templates ?? "-"}`;
    } catch (e) {
        dbBadge.classList.remove("ok");
        dbBadge.textContent = `資料庫：連線失敗 (${e.message})`;
        setMobileStatus("DB 失敗", "warn");
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
    if (text.includes("產生") || text.includes("製作")) return "產生中...";
    if (text.includes("套用")) return "套用中...";
    return "處理中...";
}

function reportUiError(actionLabel, error) {
    console.error(error);
    const body = `${actionLabel || "操作"}失敗：${error.message}`;
    showAlert("MAGI說", body);
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
    } else if (act === "tab-jump") {
        meta.busyLabel = "";
        meta.successLabel = "已開啟";
        meta.flash = false;
        meta.showToast = false;
        meta.applyWorkbenchStatus = false;
    } else if (act.startsWith("saas-")) {
        meta.busyLabel = "載入原功能...";
        meta.successLabel = "已帶入";
        meta.showToast = true;
        meta.applyWorkbenchStatus = false;
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
        el.textContent = "AI 產生結果";
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
    const titleEl = document.getElementById("wbTitle");
    const bodyEl = document.getElementById("wbBody");
    const maskEl = document.getElementById("wbMask");
    if (!titleEl || !bodyEl || !maskEl) {
        const msg = "MAGI 工作區尚未載入。請從 Paperclip /osc 正式入口開啟，才能使用資料夾、預覽、下載、上傳、刪除與分享連結。";
        if (typeof showToast === "function") showToast(msg, "warn", 5000);
        else if (typeof showAlert === "function") showAlert("MAGI說", msg);
        else if (typeof window !== "undefined" && typeof window.alert === "function") window.alert(`MAGI說：${msg}`);
        return false;
    }
    titleEl.textContent = title;
    bodyEl.innerHTML = html;
    maskEl.classList.add("show");
    requestAnimationFrame(() => {
        const closeBtn = document.getElementById("wbCloseBtn");
        if (closeBtn) closeBtn.focus({ preventScroll: true });
    });
    return true;
}

function wbClose() {
    state.wb = { mode: null, id: null, data: null };
    document.getElementById("wbMask").classList.remove("show");
    document.getElementById("wbBody").innerHTML = "";
    wbSetStatus("");
}

/**
 * showAlert(title, body, detail?)
 * 顯示一個簡易警告彈窗（用 <dialog> element）。
 * 若瀏覽器不支援 <dialog>，fallback 到 window.alert()。
 */
function showAlert(title, body, detail) {
    // 嘗試用 <dialog>
    try {
        const existing = document.getElementById("_oscAlertDialog");
        if (existing) existing.remove();
        const dlg = document.createElement("dialog");
        dlg.id = "_oscAlertDialog";
        dlg.style.cssText = [
            "padding:0", "border:none", "border-radius:12px",
            "box-shadow:0 8px 32px rgba(0,0,0,0.22)", "max-width:480px", "width:90vw",
            "font-family:var(--apple-font,-apple-system,sans-serif)",
        ].join(";");
        const detailHtml = detail ? `<pre style="margin:10px 0 0;padding:10px;background:#f5f5f7;border-radius:6px;font-size:12px;white-space:pre-wrap;color:#555;max-height:160px;overflow-y:auto">${esc ? esc(detail) : detail}</pre>` : "";
        dlg.innerHTML = `
<div style="padding:24px 24px 16px">
  <div style="font-size:17px;font-weight:700;color:#1d1d1f;margin-bottom:10px">${esc ? esc(title) : title}</div>
  <div style="font-size:14px;color:#3d3d3f;line-height:1.6;white-space:pre-wrap">${esc ? esc(body) : body}</div>
  ${detailHtml}
</div>
<div style="display:flex;justify-content:flex-end;padding:8px 24px 20px">
  <button id="_oscAlertOk" style="
    background:#007aff;color:#fff;border:none;border-radius:8px;
    padding:9px 24px;font-size:15px;font-weight:600;cursor:pointer
  ">了解</button>
</div>`;
        document.body.appendChild(dlg);
        dlg.showModal();
        dlg.querySelector("#_oscAlertOk").addEventListener("click", () => dlg.close());
        dlg.addEventListener("close", () => dlg.remove());
    } catch (_e) {
        // fallback
        const txt = [title, body, detail].filter(Boolean).join("\n\n");
        window.alert(txt);
    }
}

function showConfirm(title, body, opts = {}) {
    return new Promise((resolve) => {
        try {
            const existing = document.getElementById("_oscConfirmDialog");
            if (existing) existing.remove();
            const dlg = document.createElement("dialog");
            dlg.id = "_oscConfirmDialog";
            dlg.style.cssText = [
                "padding:0", "border:none", "border-radius:12px",
                "box-shadow:0 8px 32px rgba(0,0,0,0.22)", "max-width:520px", "width:90vw",
                "font-family:var(--apple-font,-apple-system,sans-serif)",
            ].join(";");
            const okText = opts.okText || "確定";
            const cancelText = opts.cancelText || "取消";
            dlg.innerHTML = `
<div style="padding:24px 24px 16px">
  <div style="font-size:17px;font-weight:700;color:#1d1d1f;margin-bottom:10px">${esc(title || "MAGI說")}</div>
  <div style="font-size:14px;color:#3d3d3f;line-height:1.6;white-space:pre-wrap">${esc(body || "")}</div>
</div>
<div style="display:flex;justify-content:flex-end;gap:10px;padding:8px 24px 20px;border-top:1px solid #f0f0f2">
  <button id="_oscConfirmCancel" style="background:#fff;color:#1d1d1f;border:1px solid #d2d7df;border-radius:8px;padding:9px 20px;font-size:15px;cursor:pointer">${esc(cancelText)}</button>
  <button id="_oscConfirmOk" style="background:#007aff;color:#fff;border:none;border-radius:8px;padding:9px 24px;font-size:15px;font-weight:600;cursor:pointer">${esc(okText)}</button>
</div>`;
            document.body.appendChild(dlg);
            let settled = false;
            const finish = (value) => {
                if (settled) return;
                settled = true;
                resolve(value);
                dlg.close();
            };
            dlg.querySelector("#_oscConfirmCancel").addEventListener("click", () => finish(false));
            dlg.querySelector("#_oscConfirmOk").addEventListener("click", () => finish(true));
            dlg.addEventListener("cancel", (ev) => {
                ev.preventDefault();
                finish(false);
            });
            dlg.addEventListener("close", () => {
                if (!settled) resolve(false);
                dlg.remove();
            });
            dlg.showModal();
        } catch (_e) {
            resolve(false);
        }
    });
}

function showPrompt(title, body, defaultValue = "", opts = {}) {
    return new Promise((resolve) => {
        try {
            const existing = document.getElementById("_oscPromptDialog");
            if (existing) existing.remove();
            const dlg = document.createElement("dialog");
            dlg.id = "_oscPromptDialog";
            dlg.style.cssText = [
                "padding:0", "border:none", "border-radius:12px",
                "box-shadow:0 8px 32px rgba(0,0,0,0.22)", "max-width:540px", "width:90vw",
                "font-family:var(--apple-font,-apple-system,sans-serif)",
            ].join(";");
            dlg.innerHTML = `
<form method="dialog">
  <div style="padding:24px 24px 16px">
    <div style="font-size:17px;font-weight:700;color:#1d1d1f;margin-bottom:10px">${esc(title || "MAGI說")}</div>
    <div style="font-size:14px;color:#3d3d3f;line-height:1.6;white-space:pre-wrap;margin-bottom:12px">${esc(body || "")}</div>
    <input id="_oscPromptInput" style="box-sizing:border-box;width:100%;border:1px solid #d2d7df;border-radius:8px;padding:10px 12px;font-size:15px" value="${esc(defaultValue || "")}">
  </div>
  <div style="display:flex;justify-content:flex-end;gap:10px;padding:8px 24px 20px;border-top:1px solid #f0f0f2">
    <button id="_oscPromptCancel" type="button" style="background:#fff;color:#1d1d1f;border:1px solid #d2d7df;border-radius:8px;padding:9px 20px;font-size:15px;cursor:pointer">${esc(opts.cancelText || "取消")}</button>
    <button id="_oscPromptOk" value="ok" style="background:#007aff;color:#fff;border:none;border-radius:8px;padding:9px 24px;font-size:15px;font-weight:600;cursor:pointer">${esc(opts.okText || "確定")}</button>
  </div>
</form>`;
            document.body.appendChild(dlg);
            const input = dlg.querySelector("#_oscPromptInput");
            let settled = false;
            const finish = (value) => {
                if (settled) return;
                settled = true;
                resolve(value);
                dlg.close();
            };
            dlg.querySelector("#_oscPromptCancel").addEventListener("click", () => finish(null));
            dlg.querySelector("#_oscPromptOk").addEventListener("click", (ev) => {
                ev.preventDefault();
                finish(input.value);
            });
            input.addEventListener("keydown", (ev) => {
                if (ev.key === "Enter") {
                    ev.preventDefault();
                    finish(input.value);
                }
            });
            dlg.addEventListener("cancel", (ev) => {
                ev.preventDefault();
                finish(null);
            });
            dlg.addEventListener("close", () => {
                if (!settled) resolve(null);
                dlg.remove();
            });
            dlg.showModal();
            input.focus();
            input.select();
        } catch (_e) {
            resolve(null);
        }
    });
}

function showChoice(title, body, choices = [], defaultValue = "") {
    return new Promise((resolve) => {
        try {
            const existing = document.getElementById("_oscChoiceDialog");
            if (existing) existing.remove();
            const dlg = document.createElement("dialog");
            dlg.id = "_oscChoiceDialog";
            dlg.style.cssText = [
                "padding:0", "border:none", "border-radius:12px",
                "box-shadow:0 8px 32px rgba(0,0,0,0.22)", "max-width:560px", "width:90vw",
                "font-family:var(--apple-font,-apple-system,sans-serif)",
            ].join(";");
            const buttons = (choices || []).map(choice => {
                const value = typeof choice === "string" ? choice : choice.value;
                const label = typeof choice === "string" ? choice : (choice.label || choice.value);
                const primary = String(value) === String(defaultValue);
                return `<button type="button" data-choice="${esc(value)}" style="${primary
                    ? "background:#007aff;color:#fff;border:none;"
                    : "background:#fff;color:#1d1d1f;border:1px solid #d2d7df;"}border-radius:8px;padding:9px 18px;font-size:15px;cursor:pointer">${esc(label)}</button>`;
            }).join("");
            dlg.innerHTML = `
<div style="padding:24px 24px 16px">
  <div style="font-size:17px;font-weight:700;color:#1d1d1f;margin-bottom:10px">${esc(title || "MAGI說")}</div>
  <div style="font-size:14px;color:#3d3d3f;line-height:1.6;white-space:pre-wrap">${esc(body || "")}</div>
</div>
<div style="display:flex;justify-content:flex-end;flex-wrap:wrap;gap:10px;padding:8px 24px 20px;border-top:1px solid #f0f0f2">
  <button type="button" data-choice="" style="background:#fff;color:#1d1d1f;border:1px solid #d2d7df;border-radius:8px;padding:9px 18px;font-size:15px;cursor:pointer">取消</button>
  ${buttons}
</div>`;
            document.body.appendChild(dlg);
            let settled = false;
            const finish = (value) => {
                if (settled) return;
                settled = true;
                resolve(value || null);
                dlg.close();
            };
            dlg.querySelectorAll("[data-choice]").forEach(btn => btn.addEventListener("click", () => finish(btn.dataset.choice || null)));
            dlg.addEventListener("cancel", (ev) => {
                ev.preventDefault();
                finish(null);
            });
            dlg.addEventListener("close", () => {
                if (!settled) resolve(null);
                dlg.remove();
            });
            dlg.showModal();
        } catch (_e) {
            resolve(null);
        }
    });
}

/**
 * showCustomDialog(title, bodyHtml)
 * 與 showAlert 類似，但 body 接收 HTML 字串（caller 須自行 esc）。
 * 用於需要互動內容（按鈕 / input / 多段資訊）的彈窗，例如跨平台
 * 開資料夾時的多候選路徑複製對話框。
 */
function showCustomDialog(title, bodyHtml) {
    try {
        const existing = document.getElementById("_oscCustomDialog");
        if (existing) existing.remove();
        const dlg = document.createElement("dialog");
        dlg.id = "_oscCustomDialog";
        dlg.style.cssText = [
            "padding:0", "border:none", "border-radius:12px",
            "box-shadow:0 8px 32px rgba(0,0,0,0.22)", "max-width:600px", "width:92vw",
            "font-family:var(--apple-font,-apple-system,sans-serif)",
        ].join(";");
        dlg.innerHTML = `
<div style="padding:20px 24px 12px">
  <div style="font-size:17px;font-weight:700;color:#1d1d1f;margin-bottom:12px">${esc ? esc(title) : title}</div>
  <div style="font-size:14px;color:#3d3d3f;line-height:1.55">${bodyHtml || ""}</div>
</div>
<div style="display:flex;justify-content:flex-end;padding:8px 24px 20px;border-top:1px solid #f0f0f2">
  <button id="_oscCustomOk" style="
    background:#007aff;color:#fff;border:none;border-radius:8px;
    padding:9px 24px;font-size:15px;font-weight:600;cursor:pointer
  ">關閉</button>
</div>`;
        document.body.appendChild(dlg);
        dlg.showModal();
        dlg.querySelector("#_oscCustomOk").addEventListener("click", () => dlg.close());
        dlg.addEventListener("close", () => dlg.remove());
        return dlg;
    } catch (_e) {
        // fallback：剝掉 HTML，丟 alert
        const txt = (bodyHtml || "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
        window.alert([title, txt].filter(Boolean).join("\n\n"));
        return null;
    }
}

function showWebReplyDialog(title, text, html) {
    const body = `<div class="osc-web-reply-dialog">${html || renderWebReplyHtml(text || "")}</div>`;
    return showCustomDialog(title || "MAGI 回覆", body);
}
