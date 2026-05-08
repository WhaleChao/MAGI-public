/* tabs/cases.js – Case management + workbench */
async function loadCaseCourtOptions(force = false) {
    if (!force && state.caseCourtOptionsLoaded) return;
    try {
        const data = await api("/api/osc/courts?limit=2000");
        state.caseCourtOptions = data.items || [];
        state.caseCourtOptionsLoaded = true;
        renderCaseCourtOptions();
    } catch (e) {
        console.warn("loadCaseCourtOptions failed:", e);
    }
}

function renderCaseCourtOptions() {
    const list = document.getElementById("caseCourtOptions");
    if (!list) return;
    const rows = state.caseCourtOptions || state.adminCourts || [];
    list.innerHTML = rows.map(r => {
        const label = [r.type, r.address].filter(Boolean).join("｜");
        return `<option value="${esc(r.name || "")}"${label ? ` label="${esc(label)}"` : ""}></option>`;
    }).join("");
}

async function loadCases() {
    try {
        loadCaseCourtOptions().catch(() => {});
        const q = encodeURIComponent((document.getElementById("casesQ").value || "").trim());
        const caseType = encodeURIComponent(state.caseType || "全部");
        const caseKind = encodeURIComponent(state.caseKind || "全部");
        const statusScope = encodeURIComponent(state.caseStatusScope || "all");
        const data = await api(`/api/osc/cases?limit=300&q=${q}&case_type=${caseType}&case_kind=${caseKind}&status_scope=${statusScope}`);
        state.cases = data.items || [];
    } catch (e) { console.warn("loadCases failed:", e); }
    renderCases();
}

function isLegalAidCaseRow(row = {}) {
    const text = `${row.case_category || ""} ${row.case_reason || ""}`;
    return text.includes("法律扶助案件") || text.includes("法律扶助") || text.includes("法扶");
}

function wbTodoDoneStatus(status) {
    const text = String(status || '').trim().toLowerCase();
    return ['completed', 'done', '已完成', '完成', 'cancelled', 'canceled', '取消'].includes(text);
}

function wbRenderTodoActions(todo = {}) {
    const id = Number(todo.id);
    if (!id) return '';
    const done = wbTodoDoneStatus(todo.status);
    const toggle = done
        ? `<button class="btn" data-act="todo-reopen" data-id="${id}">重新待辦</button>`
        : `<button class="btn primary" data-act="todo-complete" data-id="${id}">已完成</button>`;
    return `<div class="actions inline-actions">${toggle}<button class="btn" data-act="wb-todo-edit" data-id="${id}">編輯</button></div>`;
}

function renderCases() {
    const body = document.getElementById("casesBody");
    const cardGrid = document.getElementById("casesCardGrid");
    updateCaseSummary();
    if (!state.cases.length) {
        const hint = (state.caseStatusScope || "all") === "working"
            ? "目前沒有進行中 / 結案中的案件。可切到「全部狀態」檢視完整案件。"
            : "沒有符合條件的案件資料。";
        body.innerHTML = `<tr><td colspan="10" class="muted">${hint}</td></tr>`;
        if (cardGrid) cardGrid.innerHTML = `<div class="muted" style="padding:20px;">${hint}</div>`;
        return;
    }
    const sorted = applySort([...state.cases], state.sort.col, state.sort.dir, state.sort.type);

    // Card view
    if (cardGrid) {
        const order = JSON.parse(localStorage.getItem('caseCardOrder') || '[]');
        const useManualOrder = !state.sort?.col && order.length;
        const orderedCases = useManualOrder ? [...sorted].sort((a, b) => {
            const ia = order.indexOf(String(a.id));
            const ib = order.indexOf(String(b.id));
            if (ia === -1 && ib === -1) return 0;
            if (ia === -1) return 1;
            if (ib === -1) return -1;
            return ia - ib;
        }) : sorted;

        cardGrid.innerHTML = orderedCases.map(r => {
            const statusLower = (r.status || "").toLowerCase();
            const isLaf = isLegalAidCaseRow(r);
            const badgeClass = statusLower.includes('close') || statusLower.includes('結案') ? 'closed' : isLaf ? 'laf' : 'active';
            const badgeText = isLaf ? '法扶' : (r.status || 'Active');
            return `
            <div class="case-card" draggable="true" data-case-id="${esc(r.id)}">
                <div class="card-header">
                    <div class="card-title">${esc(r.client_name || '未命名')}</div>
                    <span class="card-badge ${badgeClass}">${esc(badgeText)}</span>
                </div>
                <div class="card-meta">
                    <div><span class="label">案號</span> <span class="value">${esc(r.case_number || '-')}</span></div>
                    <div><span class="label">案由</span> <span class="value">${esc(r.case_reason || '-')}</span></div>
                    <div><span class="label">法院</span> <span class="value">${esc(r.court_name || '-')}</span></div>
                    <div><span class="label">法院案號</span> <span class="value">${esc(r.court_case_no || '-')}</span></div>
                    <div><span class="label">分類</span> <span class="value">${esc(r.case_type || '-')}</span></div>
                    <div><span class="label">種類</span> <span class="value">${esc(r.case_category || '-')}</span></div>
                    ${r.laf_case_no ? `<div><span class="label">法扶</span> <span class="value">${esc(r.laf_case_no)}</span></div>` : ''}
                </div>
                <div class="card-actions">
                    <button class="btn primary" data-act="case-open" data-id="${esc(r.id)}">資料夾</button>
                    <button class="btn" data-act="case-workbench" data-id="${esc(r.id)}">工作台</button>
                    <button class="btn" data-act="case-edit" data-id="${esc(r.id)}">編輯</button>
                    <button class="btn" data-act="case-address-label" data-id="${esc(r.id)}">地址標籤</button>
                    <button class="btn danger" data-act="case-del" data-id="${esc(r.id)}">刪除</button>
                </div>
            </div>`;
        }).join("");
        initCardDrag(cardGrid);
        bindCaseCardOpen(cardGrid);
    }

    // Table view (hidden by default)
    body.innerHTML = sorted.map(r => `
    <tr class="row-clickable" data-case-id="${esc(r.id)}">
        <td>${esc(r.case_number)}</td>
        <td>${esc(r.client_name)}</td>
        <td>${esc(r.case_type || "")}</td>
        <td>${esc(r.case_category || "")}</td>
        <td>${esc(r.case_reason)}</td>
        <td>${esc(r.court_name || "")}</td>
        <td>${esc(r.court_case_no)}</td>
        <td>${esc(r.laf_case_no)}</td>
        <td>${esc(r.status)}</td>
        <td class="actions">
            <button class="btn primary" data-act="case-open" data-id="${esc(r.id)}">資料夾</button>
            <button class="btn" data-act="case-workbench" data-id="${esc(r.id)}">工作台</button>
            <button class="btn" data-act="case-edit" data-id="${esc(r.id)}">編輯</button>
            <button class="btn" data-act="case-address-label" data-id="${esc(r.id)}">📮 地址標籤</button>
            <button class="btn danger" data-act="case-del" data-id="${esc(r.id)}">刪除</button>
        </td>
    </tr>
`).join("");
    bindCaseCardOpen(body);

    const ts = document.querySelectorAll("#cases th[data-sort]");
    ts.forEach(th => {
        th.innerHTML = th.innerHTML.replace(/ [▲▼]/g, "") + renderSortArrow(th.dataset.sort);
    });
}

function updateCaseSummary() {
    const cases = state.cases || [];
    const closing = cases.filter(r => {
        const s = String(r.status || "").toLowerCase();
        return s.includes("close") || s.includes("結案");
    }).length;
    const set = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.textContent = String(value);
    };
    set("caseVisibleCount", cases.length);
    set("caseClosingVisibleCount", closing);
}

const CASE_MAGI_MODULES = {
    laf: {
        label: "法扶案件辦理",
        prompt: "法扶指令",
        actions: [
            ["開啟法扶管理", "tab:laf", "進入法扶專頁辦理開辦、補件、報結。"],
            ["法扶指令", "法扶指令", "顯示法扶模組支援的開辦、疑義、報結格式。"],
            ["二階段掃描", "二階段批次", "啟動法扶二階段批次檢查。"],
            ["報結掃描", "報結掃描", "啟動法扶報結/結案掃描。"],
        ],
    },
    file_review: {
        label: "閱卷與卷證",
        prompt: "檢查閱卷信箱",
        actions: [
            ["檢查閱卷信箱", "檢查閱卷信箱", "啟動閱卷信箱檢查。"],
            ["下載閱卷", "下載閱卷", "啟動閱卷下載流程。"],
            ["期限檢查", "閱卷到期檢查", "檢查閱卷期限與到期提醒。"],
        ],
    },
    transcript: {
        label: "筆錄調閱與整理",
        prompt: "筆錄同步",
        actions: [
            ["筆錄同步", "筆錄同步", "同步筆錄資料。"],
            ["筆錄全同步", "筆錄全同步", "完整同步筆錄資料。"],
            ["筆錄更名", "筆錄更名", "整理筆錄檔名與歸檔。"],
        ],
    },
};

function activeCaseMagiModuleKey() {
    return document.querySelector("[data-magi-module].active")?.dataset?.magiModule || "file_review";
}

function renderCaseMagiActions(moduleKey) {
    const wrap = document.getElementById("caseMagiActions");
    if (!wrap) return;
    const meta = CASE_MAGI_MODULES[moduleKey] || CASE_MAGI_MODULES.file_review;
    wrap.innerHTML = (meta.actions || []).map(([label, command, note, withContext]) => {
        if (String(command || "").startsWith("tab:")) {
            return `<button class="case-magi-command" type="button" data-act="case-magi-tab" data-tab="${esc(command.slice(4))}">
                <strong>${esc(label)}</strong><span>${esc(note || "")}</span>
            </button>`;
        }
        return `<button class="case-magi-command" type="button" data-act="case-magi-command" data-command="${esc(command)}" data-label="${esc(label)}" data-context="${withContext ? "1" : "0"}">
            <strong>${esc(label)}</strong><span>${esc(note || "")}</span>
        </button>`;
    }).join("");
}

function selectCaseMagiModule(moduleKey) {
    const key = CASE_MAGI_MODULES[moduleKey] ? moduleKey : "file_review";
    const meta = CASE_MAGI_MODULES[key];
    document.querySelectorAll("[data-magi-module]").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.magiModule === key);
    });
    const selected = document.getElementById("caseMagiSelected");
    if (selected) selected.textContent = meta.label;
    const prompt = document.getElementById("caseMagiPrompt");
    if (prompt) prompt.value = meta.prompt;
    renderCaseMagiActions(key);
}

function buildCaseMagiContext() {
    const rows = (state.cases || []).slice(0, 40).map((r, idx) => [
        `${idx + 1}. ${r.case_number || "-"}`,
        `當事人=${r.client_name || "-"}`,
        `分類=${r.case_type || "-"}`,
        `種類=${r.case_category || "-"}`,
        `案由=${r.case_reason || "-"}`,
        `法院案號=${r.court_case_no || "-"}`,
        `法扶案號=${r.laf_case_no || "-"}`,
        `狀態=${r.status || "-"}`,
    ].join("｜"));
    return [
        `目前案件篩選：分類=${state.caseType || "全部"}；種類=${state.caseKind || "全部"}；狀態=${state.caseStatusScope || "all"}；排序=${state.sort?.col || "預設"}`,
        `目前顯示 ${state.cases?.length || 0} 筆案件。以下最多列 40 筆：`,
        rows.join("\n") || "目前沒有案件列。",
    ].join("\n");
}

function buildCaseMagiMessage(command, withContext = false) {
    const body = String(command || "").trim();
    if (!withContext) return body;
    return `${body}\n\n[案件管理頁目前資料]\n${buildCaseMagiContext()}`;
}

async function sendCaseMagiCommand(command, options = {}) {
    const resultEl = document.getElementById("caseMagiResult");
    const message = buildCaseMagiMessage(command, !!options.withContext);
    if (!message) {
        if (resultEl) resultEl.textContent = "請先輸入命令。";
        return;
    }
    if (resultEl) resultEl.textContent = `MAGI 處理中：${options.label || message}`;
    try {
        const data = await api("/api/osc/magi-modules/run", "POST", {
            module: activeCaseMagiModuleKey(),
            command: message,
        });
        if (resultEl) {
            if (data.reply_html) {
                resultEl.innerHTML = data.reply_html;
            } else {
                resultEl.innerHTML = renderWebReplyHtml(data.reply || data.message || "(MAGI 沒有回覆內容)");
            }
        }
    } catch (e) {
        if (resultEl) resultEl.textContent = `啟動失敗：${e.message || e}`;
        throw e;
    }
}

async function runCaseMagiPreset(command, label, withContext = false, btn = null) {
    await withElementBusy(btn, "啟動中...", async () => {
        await sendCaseMagiCommand(command, { label, withContext });
    });
}

async function runCaseMagiModule() {
    const promptEl = document.getElementById("caseMagiPrompt");
    const resultEl = document.getElementById("caseMagiResult");
    const btn = document.getElementById("caseMagiRunBtn");
    const instruction = (promptEl?.value || "").trim();
    if (!instruction) {
        if (resultEl) resultEl.textContent = "請先輸入命令。";
        return;
    }
    await withElementBusy(btn, "啟動中...", async () => {
        await sendCaseMagiCommand(instruction, { label: instruction });
    });
}

async function editCase(id) {
    const data = await api(`/api/osc/cases/${encodeURIComponent(id)}`);
    const x = data.item || {};
    const panel = document.getElementById("caseEditorPanel");
    if (panel) panel.open = true;
    await loadCaseCourtOptions();
    writeFields("case_", x, ["id", "case_number", "client_name", "client_phone", "client_email", "client_id_number", "laf_case_no", "application_no", "court_name", "court_case_no", "status", "folder_path", "notes"]);
    document.getElementById("case_category").value = x.case_category || "";
    document.getElementById("case_type").value = x.case_type || "";
    document.getElementById("case_stage").value = x.case_stage || "";
    document.getElementById("case_reason").value = x.case_reason || "";
}

function prepareNewCase() {
    const panel = document.getElementById("caseEditorPanel");
    if (panel) panel.open = true;
    loadCaseCourtOptions().catch(() => {});
    clearFields(["case_id", "case_case_number", "case_client_name", "case_client_phone", "case_client_email", "case_client_id_number", "case_category", "case_type", "case_stage", "case_reason", "case_laf_case_no", "case_application_no", "case_court_name", "case_court_case_no", "case_status", "case_folder_path", "case_notes"]);
    const name = document.getElementById("case_client_name");
    if (name) name.focus();
}

async function delCase(id) {
    if (!confirm(`確定刪除案件 ${id}？`)) return;
    await api(`/api/osc/cases/${encodeURIComponent(id)}`, "DELETE");
    await loadCases();
    await loadMeta();
}

async function saveCase() {
    const p = readFields(["case_id", "case_case_number", "case_client_name", "case_client_phone", "case_client_email", "case_client_id_number", "case_category", "case_type", "case_stage", "case_reason", "case_laf_case_no", "case_application_no", "case_court_name", "case_court_case_no", "case_status", "case_folder_path", "case_notes"]);
    const body = {
        id: p.case_id, case_number: p.case_case_number, client_name: p.case_client_name,
        client_phone: p.case_client_phone, client_email: p.case_client_email, client_id_number: p.case_client_id_number,
        case_category: p.case_category, case_type: p.case_type, case_stage: p.case_stage,
        case_reason: p.case_reason, laf_case_no: p.case_laf_case_no, application_no: p.case_application_no,
        court_name: p.case_court_name, court_case_no: p.case_court_case_no, status: p.case_status, folder_path: p.case_folder_path, notes: p.case_notes
    };
    const isNew = !(body.id || "").trim();
    const autoFolder = isNew && document.getElementById("case_auto_create_folder")?.checked;
    if (autoFolder) body.auto_create_folder = true;
    let resp;
    if (!isNew) resp = await api(`/api/osc/cases/${encodeURIComponent(body.id)}`, "PUT", body);
    else resp = await api("/api/osc/cases", "POST", body);
    if (autoFolder && resp?.folder?.ok) {
        showToast(`資料夾已建立：${resp.folder.path}`, "ok", 4000);
    } else if (autoFolder && resp?.folder && !resp.folder.ok) {
        showToast(`資料夾建立失敗：${resp.folder.error || "未知錯誤"}`, "warn", 4000);
    }
    showArchiveResult(resp?.archive);
    clearFields(["case_id", "case_case_number", "case_client_name", "case_client_phone", "case_client_email", "case_client_id_number", "case_category", "case_type", "case_stage", "case_reason", "case_laf_case_no", "case_application_no", "case_court_name", "case_court_case_no", "case_status", "case_folder_path", "case_notes"]);
    await loadCases();
    await loadMeta();
}

function archiveReasonText(reason) {
    return {
        moved: "已移到結案資料夾",
        already_archived: "已在結案資料夾",
        already_in_archive_base: "已在結案資料夾",
        source_missing: "找不到原案件資料夾，請確認同步或路徑",
        target_missing: "找不到結案資料夾設定",
        target_exists: "結案資料夾已有同名資料夾，未覆蓋",
        status_not_closed: "案件狀態不是結案，未搬移",
        case_not_found: "找不到案件",
    }[reason] || reason || "未搬移";
}

function showArchiveResult(archive) {
    if (!archive) return;
    const reason = archiveReasonText(archive.reason);
    if (archive.ok && !archive.skipped) {
        showToast(`結案搬移：${reason}${archive.to ? " → " + archive.to : ""}`, "ok", 6000);
        return;
    }
    if (!archive.ok) {
        showToast(`結案搬移未完成：${reason}`, "warn", 7000);
    }
}

// ── Cross-platform open folder（2026-05-03 UX v3 P0）──────────────────────
// 後端 /open-folder 不再 server-side open，只回 candidates。前端依
// navigator.platform 觸發 smb:// (mac) / file:// (Win Synology Drive) /
// file: UNC (Win NAS) / 多候選路徑複製對話框 (iPad / 其他)。
async function openCaseFolderHost(id, quiet = false) {
    try {
        const data = await api(`/api/osc/cases/${encodeURIComponent(id)}/open-folder`, "POST", {});

        // 失敗（error_kind） — 與舊行為一致
        if (!data.ok) {
            if (!quiet) handleOpenFolderError(data);
            return data;
        }

        const candidates = data.candidates || {
            // 後備：相容舊 payload 格式
            smb_url: data.smb_candidates || (data.smb_url ? [data.smb_url] : []),
            mac_synology: data.local_candidates || [],
            win_unc: [],
            win_synology: [],
        };
        const platform = (navigator.userAgentData?.platform || navigator.platform || "").toLowerCase();
        const ua = navigator.userAgent || "";
        const isMac = /mac/i.test(platform) && !/iPad|iPhone/.test(ua);
        const isWin = /win/i.test(platform);
        const isIpad = /iPad/.test(ua) || (isMac && navigator.maxTouchPoints > 1);

        // mac：smb:// → Finder 自動 mount + 開
        if (isMac && !isIpad && candidates.smb_url && candidates.smb_url.length) {
            try { window.location.href = candidates.smb_url[0]; }
            catch (_) { /* ignore */ }
            if (!quiet) showToast("已嘗試開啟（NAS / Finder）", "ok");
            return data;
        }

        // Windows：優先 Synology Drive 本機路徑（如裝且不含 %USERPROFILE%），
        // 否則 fallback 到 UNC \\\\nas\\share（file:// 形式）。
        // 注意：Chrome/Edge 在 https origin 會 BLOCK file://，window.open 會靜默失敗；
        // 因此**永遠**同時把複製對話框留作保險（300ms 後檢查焦點未變即彈）。
        if (isWin) {
            const tryOpen = (url, label) => {
                try {
                    const w = window.open(url, "_blank");
                    // 多數現代瀏覽器會 return null 或立刻 close，視為失敗 → 0.4s 後彈對話框
                    setTimeout(() => {
                        if (!w || w.closed) showFolderPathDialog(data.folder_path || "", candidates);
                    }, 400);
                    if (!quiet) showToast(`已嘗試開啟（${label}），若沒反應請從對話框複製貼到 Explorer`, "ok");
                } catch (_) {
                    showFolderPathDialog(data.folder_path || "", candidates);
                }
            };
            if (candidates.win_synology && candidates.win_synology.length) {
                const raw = candidates.win_synology[0];
                if (!/%USERPROFILE%/i.test(raw)) {
                    tryOpen(`file:///${raw.replace(/\\/g, "/")}`, "Synology Drive");
                    return data;
                }
                // %USERPROFILE% 無法解析 → 改試 UNC，再不行對話框
            }
            if (candidates.win_unc && candidates.win_unc.length) {
                tryOpen(`file:${candidates.win_unc[0]}`, "NAS 共享");
                return data;
            }
        }

        // iPad / 其他 / fallback：顯示路徑 + 複製按鈕
        showFolderPathDialog(data.folder_path || "", candidates);
        return data;

    } catch (e) {
        if (!quiet) showAlert("❌ 系統錯誤", `無法呼叫開啟資料夾 API：${e.message || e}`);
        throw e;
    }
}

// ── Open case folder inside Paperclip's file manager (Phase 2 commit 10) ──
// 跨電腦友善：律師遠端在 iPad / Win Chrome 也能直接巡覽案件資料夾，
// 不依賴本機檔案管理 / smb 協定。把 NAS 案件路徑塞進 #fileManager tab。
async function openCaseInFileManager(id) {
    try {
        // 先呼 /open-folder：後端已含 DB → _osc_guess_case_folder（含 NAS 掃描）
        // 三層 fallback，能找到就找到。前端不再自己判 folder 為空。
        const data = await api(`/api/osc/cases/${encodeURIComponent(id)}/open-folder`, "POST", {});
        let folder = (data && data.folder_path || "").trim();
        if (!folder) {
            // 後端三層都找不到 → 顯示 friendly 警告，提供「建立資料夾」按鈕
            const cdata = await api(`/api/osc/cases/${encodeURIComponent(id)}`);
            const c = (cdata && cdata.item) || {};
            showAlert(
                "⚠️ 找不到此案件的 NAS 資料夾",
                `${c.client_name || "此案件"}（${c.case_number || id}）在 NAS 上沒有對應資料夾。`,
                "可在「案件編輯」頁勾選「自動建立資料夾」儲存，MAGI 會自動建好結構。"
            );
            return;
        }
        // 1. 先切到 fileManager view。若側欄為精簡版沒有獨立按鈕，直接啟用 view。
        const fmTabBtn = document.querySelector('.tab-btn[data-tab="fileManager"]');
        if (fmTabBtn) {
            fmTabBtn.click();
        } else {
            document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
            document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
            const fmView = document.getElementById("fileManager");
            if (fmView) fmView.classList.add("active");
            if (typeof state !== "undefined") state.activeTab = "fileManager";
            const titleEl = document.getElementById("pageTitle");
            if (titleEl) titleEl.textContent = "檔案管理";
        }
        // 2. setRoot 到該案件資料夾（FileManager init 完成後）
        const tryOpen = () => {
            if (window.FileManager && typeof window.FileManager.openWithBasePath === "function") {
                const caseMeta = data.case || {};
                const opened = window.FileManager.openWithBasePath(folder, {
                    caseNumber: caseMeta.case_number,
                    clientName: caseMeta.client_name,
                    label: [caseMeta.case_number, caseMeta.client_name].filter(Boolean).join(" ")
                });
                if (opened && typeof opened.then === "function") {
                    opened.then(() => showToast(`已切換到檔案管理：${folder}`, "ok", 2500));
                } else {
                    showToast(`已切換到檔案管理：${folder}`, "ok", 2500);
                }
            } else {
                // FileManager 還沒載入 → 50ms 後重試（init 走 DOMContentLoaded）
                setTimeout(tryOpen, 50);
            }
        };
        tryOpen();
    } catch (e) {
        showAlert("❌ 系統錯誤", `無法開啟檔案管理：${e.message || e}`);
    }
}

// 把後端 error_kind 統一彈窗（從原 openCaseFolderHost 抽出來）
function handleOpenFolderError(data) {
    const kind = data.error_kind || "open_failed";
    const msg = data.message || "開啟資料夾失敗";
    const cands = [...(data.smb_candidates || []), ...(data.local_candidates || [])];
    const detail = cands.length > 0
        ? `已嘗試路徑：\n${cands.slice(0, 4).join("\n")}`
        : "";
    if (kind === "no_nas_no_synology") {
        showAlert("❌ 無法開啟資料夾", msg, detail || undefined);
    } else if (kind === "folder_not_found") {
        showAlert("⚠️ 找不到案件資料夾", msg,
            detail || "建議：確認案號、當事人姓名與 NAS 資料夾名稱相符，或用「建立資料夾」按鈕建立預設結構。");
    } else if (kind === "folder_path_empty") {
        const clientName = (data.case || {}).client_name || "此案件";
        showAlert("⚠️ 案件未設定資料夾", `${clientName} 尚未設定資料夾路徑，請先用「建立資料夾」按鈕建立預設結構。`);
    } else {
        showAlert("❌ 開啟資料夾失敗", msg, detail || undefined);
    }
}

// 跨平台路徑複製對話框（iPad / Win 不裝 Synology / Linux 等情境）
function showFolderPathDialog(folderPath, candidates) {
    const c = candidates || {};
    const items = [
        ...(c.smb_url || []).map(u => ({ label: "📡 NAS (SMB)", value: u, hint: "mac Finder 直接點開" })),
        ...(c.win_unc || []).map(u => ({ label: "🪟 Windows 共享 (UNC)", value: u, hint: "Win+R 或 Explorer 貼網址" })),
        ...(c.win_synology || []).map(u => ({
            label: "💾 Win Synology Drive",
            value: u.replace(/%USERPROFILE%/g, "C:\\Users\\<您的帳號>"),
            hint: "若裝了 Synology Drive 用本機路徑",
        })),
        ...(c.mac_synology || []).map(u => ({ label: "🍎 mac Synology Drive", value: u, hint: "本機已同步路徑" })),
    ];
    // 必須 HTML-escape `"`，否則 JSON 字串裡的 `"` 會提前關閉外層 onclick="..." 屬性
    // 導致後半段 HTML 被當純文字渲染。JSON.stringify 的輸出本身不會含 `&`，所以僅需處理 " 與 <
    const safeJSON = (s) => JSON.stringify(String(s || ""))
        .replace(/</g, "\\u003c")
        .replace(/"/g, "&quot;");
    const html = `
<div style="max-width:560px">
  <p style="margin:0 0 12px;color:#666;font-size:13px">
    瀏覽器無法直接開啟此資料夾。請複製路徑到 Finder（mac）/ Explorer（Win）：
  </p>
  ${items.length ? items.map(it => `
    <div style="margin:8px 0;padding:10px;background:#f5f5f7;border-radius:8px">
      <div style="font-weight:600;font-size:12px;margin-bottom:6px">
        ${esc(it.label)}${it.hint ? `<span style="color:#86868b;font-weight:normal;font-size:11px"> — ${esc(it.hint)}</span>` : ""}
      </div>
      <div style="display:flex;gap:6px">
        <input type="text" readonly value="${esc(it.value)}"
          style="flex:1;font-family:'SF Mono',Menlo,monospace;font-size:11px;padding:5px 8px;border:1px solid #d2d2d7;border-radius:6px"
          onclick="this.select()">
        <button class="btn-secondary"
          onclick="(navigator.clipboard?.writeText(${safeJSON(it.value)}) || Promise.resolve()).then(()=>showToast('已複製','ok'))"
          style="white-space:nowrap;padding:5px 10px;font-size:12px">📋 複製</button>
      </div>
    </div>
  `).join("") : `<div class="muted">無可用路徑（請先用「建立資料夾」按鈕建立）</div>`}
  <div style="margin-top:12px;font-size:12px;color:#86868b">
    原始路徑：<code style="background:#f0f0f3;padding:2px 6px;border-radius:4px">${esc(folderPath || "")}</code>
  </div>
</div>`;
    if (typeof showCustomDialog === "function") {
        showCustomDialog("📂 開啟資料夾", html);
    } else {
        // fallback
        const txt = items.map(it => `${it.label}: ${it.value}`).join("\n");
        showAlert("📂 開啟資料夾", `路徑：${folderPath}\n\n${txt}`);
    }
}

async function createCaseFolder(id) {
    if (!id) { showToast("缺少案件 ID", "warn"); return; }
    try {
        const data = await api(`/api/osc/cases/${encodeURIComponent(id)}/create-folder`, "POST", {});
        if (data.ok) {
            showToast(`資料夾已建立：${data.folder_path}`, "ok", 4000);
            await openCaseWorkbench(id);
        } else {
            showToast(`建立失敗：${data.error || "未知錯誤"}`, "warn", 4000);
        }
    } catch (e) {
        showToast(`建立資料夾失敗：${e.message}`, "warn", 3000);
    }
}

function renderWorkbenchCaseEditor(c) {
    return `
    <div class="card">
        <h3>案件主資料快速編輯</h3>
        <div class="field-grid cols-4">
            <div class="field"><label>案件編號</label><input id="wb_case_case_number" value="${esc(c.case_number || "")}"></div>
            <div class="field"><label>當事人</label><input id="wb_case_client_name" value="${esc(c.client_name || "")}"></div>
            <div class="field"><label>案件種類</label><input id="wb_case_case_category" value="${esc(c.case_category || "")}"></div>
            <div class="field"><label>案件分類</label><input id="wb_case_case_type" value="${esc(c.case_type || "")}"></div>
            <div class="field"><label>審級 / 階段</label><input id="wb_case_case_stage" value="${esc(c.case_stage || "")}"></div>
            <div class="field"><label>案由</label><input id="wb_case_case_reason" value="${esc(c.case_reason || "")}"></div>
            <div class="field"><label>法扶案號</label><input id="wb_case_laf_case_no" value="${esc(c.laf_case_no || "")}"></div>
            <div class="field"><label>申請編號</label><input id="wb_case_application_no" value="${esc(c.application_no || "")}"></div>
            <div class="field"><label>法院 / 地檢署</label><input id="wb_case_court_name" list="caseCourtOptions" value="${esc(c.court_name || "")}" placeholder="可輸入或選擇"></div>
            <div class="field"><label>法院案號</label><input id="wb_case_court_case_no" value="${esc(c.court_case_no || "")}"></div>
            <div class="field"><label>狀態</label><input id="wb_case_status" value="${esc(c.status || "")}"></div>
            <div class="field" style="grid-column: span 2;"><label>案件資料夾</label><input id="wb_case_folder_path" value="${esc(c.folder_path || "")}" placeholder="Y:\\lumi\\01_案件\\..."></div>
            <div class="field" style="grid-column: span 2;"><label>備註</label><input id="wb_case_notes" value="${esc(c.notes || "")}"></div>
        </div>
        <div class="toolbar" style="margin-top:10px; margin-bottom:0;">
            <button class="btn primary" data-act="wb-case-save" data-id="${esc(c.id || "")}">儲存案件資料</button>
            <button class="btn" data-act="wb-case-create-folder" data-id="${esc(c.id || "")}"${c.folder_path ? ' title="已有資料夾路徑，點此可重新建立子資料夾結構"' : ""}>建立資料夾</button>
        </div>
    </div>
    `;
}

function renderCaseFolderBrowser(data) {
    const c = data.case || {};
    const entries = data.entries || [];
    const dirs = entries.filter(item => item.type === "dir");
    const files = entries.filter(item => item.type !== "dir");
    const rel = data.current_relative_path || "";
    const folderPath = data.folder_path || "";
    const folderExists = !!data.folder_exists;
    const normalizedFolderPath = folderPath.replace(/\\/g, "/").replace(/\/$/, "");
    const parentButton = rel
        ? `<button class="case-drive-node" data-act="wb-folder-open" data-id="${esc(c.id || "")}" data-path="${esc(data.parent_relative_path || "")}">↩ 上一層</button>`
        : "";
    const treeNodes = [
        `<button class="case-drive-node ${rel ? "" : "active"}" data-act="wb-folder-open" data-id="${esc(c.id || "")}" data-path="">📁 ${esc(c.case_number || "案件根目錄")}</button>`,
        parentButton,
        ...dirs.map(item => `<button class="case-drive-node" data-act="wb-folder-open" data-id="${esc(c.id || "")}" data-path="${esc(item.relative_path || "")}">📁 ${esc(item.name || "")}</button>`),
    ].filter(Boolean).join("");
    const rows = entries.length ? entries.map(item => {
        const isDir = item.type === "dir";
        const targetPath = item.relative_path ? `${normalizedFolderPath}/${item.relative_path}` : folderPath;
        const openBtn = isDir
            ? `<button class="btn slim" data-act="wb-folder-open" data-id="${esc(c.id || "")}" data-path="${esc(item.relative_path || "")}">進入</button>`
            : `<a class="btn slim" href="${fileContentUrl(targetPath, true)}" target="_blank" rel="noopener noreferrer">預覽</a>`;
        const downloadBtn = isDir
            ? ""
            : `<a class="btn slim" href="${fileContentUrl(targetPath)}" target="_blank" rel="noopener noreferrer">下載</a>`;
        const shareBtn = isDir
            ? ""
            : `<button class="btn slim" data-act="wb-file-share" data-id="${esc(c.id || "")}" data-path="${esc(targetPath)}" data-name="${esc(item.name || "")}">分享連結</button>`;
        const editBtn = (!isDir && isEditableTextFile(targetPath))
            ? `<button class="btn slim" data-act="wb-file-edit" data-id="${esc(c.id || "")}" data-path="${esc(targetPath)}" data-return-path="${esc(rel)}">編輯</button>`
            : "";
        return `
        <tr>
            <td>${isDir ? "資料夾" : "檔案"}</td>
            <td>${esc(item.name || "")}</td>
            <td>${esc(item.modified_at || "")}</td>
            <td>${esc(item.size_label || formatBytes(item.size) || "")}</td>
            <td><div class="wb-folder-actions">${openBtn}${editBtn}${shareBtn}${downloadBtn}</div></td>
        </tr>
        `;
    }).join("") : `<tr><td colspan="5" class="muted">目前資料夾沒有可列出的內容</td></tr>`;
    return `
    <div class="card case-drive-card">
        <div class="case-drive-head">
            <div>
                <h3>案件資料夾</h3>
                <div class="muted">像雲端硬碟一樣從左側資料夾往下點，右側顯示目前資料夾內容。</div>
            </div>
            <div class="toolbar case-drive-actions">
                <button class="btn slim" data-act="wb-folder-open" data-id="${esc(c.id || "")}" data-path="${esc(rel)}">重新整理</button>
                <button class="btn slim" data-act="wb-folder-upload" data-id="${esc(c.id || "")}" data-path="${esc(rel)}" data-folder-path="${esc(folderPath)}">上傳檔案</button>
                <button class="btn slim" data-act="wb-folder-copy-path" data-path="${esc(folderPath)}">複製案件路徑</button>
                <button class="btn slim" data-act="wb-case-open-host" data-id="${esc(c.id || "")}">在本機開啟</button>
            </div>
        </div>
        <div class="wb-folder-meta">
            <div class="wb-folder-kv"><div class="k">案件</div><div class="v">${esc(c.case_number || "")}｜${esc(c.client_name || "")}</div></div>
            <div class="wb-folder-kv"><div class="k">同步狀態</div><div class="v">${folderExists ? "已同步，可直接瀏覽" : "尚未同步到伺服器本機"}</div></div>
        </div>
        <div class="wb-breadcrumb">${esc(rel ? `${folderPath} / ${rel}` : folderPath)}</div>
        ${folderExists ? `
        <div class="case-drive-shell">
            <aside class="case-drive-tree" aria-label="案件資料夾結構">
                ${treeNodes || `<div class="muted">沒有子資料夾</div>`}
            </aside>
            <div class="case-drive-list table-wrap wb-folder-table">
                <table>
                    <thead><tr><th>類型</th><th>名稱</th><th>更新時間</th><th>大小</th><th>操作</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
        </div>` : `
        <div class="status-banner warn" style="margin-top:10px;">這個案件資料夾目前沒有同步到伺服器本機，所以外網無法直接列出內容。你仍可複製 NAS 路徑，或在本機點「在本機開 NAS」。</div>`}
    </div>
    `;
}

async function openCaseFolder(id, relativePath = "") {
    const query = relativePath ? `?path=${encodeURIComponent(relativePath)}` : "";
    const data = await api(`/api/osc/cases/${encodeURIComponent(id)}/folder-browser${query}`);
    const c = data.case || {};
    state.wb = { mode: "case-folder", id, data };
    wbShow(`案件資料夾｜${c.case_number || id}`, renderCaseFolderBrowser(data));
    wbSetStatus(
        data.folder_exists
            ? `已載入案件資料夾，路徑 ${data.current_relative_path || "/" }。`
            : "案件資料夾尚未同步到伺服器本機，已提供 NAS 路徑與本機開啟選項。",
        data.folder_exists ? "ok" : "warn"
    );
}

function renderTextFileEditor(caseId, rawPath, content, returnPath = "") {
    const fileName = String(rawPath || "").split(/[\\/]/).pop() || rawPath || "file";
    return `
    <div class="card">
        <h3>文字檔編輯器</h3>
        <div class="wb-breadcrumb">${esc(rawPath)}</div>
        <div class="toolbar" style="margin-top:10px;">
            <button class="btn slim" data-act="wb-file-editor-back" data-id="${esc(caseId)}" data-path="${esc(returnPath)}">回到資料夾</button>
            <a class="btn slim" href="${fileContentUrl(rawPath)}" target="_blank" rel="noopener noreferrer">下載原檔</a>
            <button class="btn primary" data-act="wb-file-save" data-id="${esc(caseId)}" data-path="${esc(rawPath)}" data-return-path="${esc(returnPath)}">儲存回本機</button>
        </div>
        <div class="muted" style="margin-bottom:10px;">目前直接支援文字檔編輯；Office / PDF 等檔案請下載後修改，再回到資料夾用上傳覆蓋。</div>
        <textarea id="wbFileEditorContent" class="wb-editor-area">${esc(content || "")}</textarea>
        <input id="wbFileEditorPath" type="hidden" value="${esc(rawPath)}">
        <input id="wbFileEditorReturnPath" type="hidden" value="${esc(returnPath)}">
        <input id="wbFileEditorName" type="hidden" value="${esc(fileName)}">
    </div>
    `;
}

async function openTextFileEditor(caseId, rawPath, returnPath = "") {
    const data = await api(`/api/osc/files/text?path=${encodeURIComponent(rawPath)}`);
    state.wb = { mode: "file-editor", id: caseId, data: { path: rawPath, returnPath } };
    wbShow(`文字檔編輯｜${(rawPath || "").split(/[\\/]/).pop() || "file"}`, renderTextFileEditor(caseId, rawPath, data.content || "", returnPath));
    wbSetStatus(`已載入文字檔，可直接儲存回本機。編碼：${data.encoding || "utf-8"}`, "ok");
}

async function saveTextFileEditor(caseId, rawPath, returnPath = "") {
    const content = document.getElementById("wbFileEditorContent")?.value || "";
    await api("/api/osc/files/text", "PUT", { path: rawPath, content });
    wbSetStatus("文字檔已存回本機同步資料夾。", "ok");
    showToast("文字檔已存回本機同步資料夾。", "ok");
    await openTextFileEditor(caseId, rawPath, returnPath);
}

function promptFolderUpload(caseId, folderPath, relativePath = "") {
    state.folderUpload = { caseId, folderPath, relativePath };
    const input = document.getElementById("wbFolderUploadInput");
    if (!input) return;
    input.value = "";
    input.click();
}

async function handleFolderUpload(file, opts = {}) {
    const ctx = state.folderUpload || {};
    const folderPath = String(ctx.folderPath || "").trim();
    if (!file || !folderPath) return;
    const form = new FormData();
    form.append("folder_path", folderPath);
    form.append("relative_path", String(ctx.relativePath || ""));
    if (opts.overwrite) form.append("overwrite", "1");
    form.append("file", file, file.name);
    try {
        const data = await apiForm("/api/osc/files/upload", form);
        const saved = (data.saved || [])[0] || {};
        showToast(`已上傳 ${saved.file_name || file.name}。`, "ok");
        await openCaseFolder(ctx.caseId, ctx.relativePath || "");
        wbSetStatus(`檔案 ${saved.file_name || file.name} 已上傳到本機同步資料夾。`, "ok");
    } catch (e) {
        if (e.status === 409 && e.payload && e.payload.error === "file_exists") {
            const confirmOverwrite = confirm(`檔案「${e.payload.file_name || file.name}」已存在，是否覆蓋？`);
            if (confirmOverwrite) {
                return await handleFolderUpload(file, { overwrite: true });
            }
            wbSetStatus("已取消覆蓋既有檔案。", "warn");
            return;
        }
        wbSetStatus(`上傳失敗：${e.message}`, "warn");
        alert(`上傳失敗：${e.message}`);
    }
}

function setCaseType(type) {
    state.caseType = type || "全部";
    document.querySelectorAll("#caseTypeTabs .chip").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.type === state.caseType);
    });
    loadCases().catch((e) => alert(`載入案件失敗：${e.message}`));
}

function setCaseKind(kind) {
    state.caseKind = kind || "全部";
    state.caseCategory = state.caseKind;
    document.querySelectorAll("#caseKindTabs .chip").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.kind === state.caseKind);
    });
    loadCases().catch((e) => alert(`載入案件失敗：${e.message}`));
}

function setCaseCategory(cat) {
    setCaseKind(cat);
}

function setCaseStatusScope(scope) {
    state.caseStatusScope = scope || "all";
    document.querySelectorAll("#caseStatusTabs .chip").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.scope === state.caseStatusScope);
    });
    loadCases().catch((e) => alert(`載入案件失敗：${e.message}`));
}

function wbRenderTodoForm(defaultCaseNumber = "", defaultClientName = "") {
    return `
    <div class="card">
        <h3>待辦（檢視 / 新增 / 修改）</h3>
        <div class="grid-4">
            <input id="wb_todo_id" type="hidden">
            <input id="wb_todo_case_number" placeholder="案件編號" value="${esc(defaultCaseNumber)}">
            <input id="wb_todo_client_name" placeholder="當事人" value="${esc(defaultClientName)}">
            <input id="wb_todo_type" placeholder="類型（如 開庭、補件）">
            <input id="wb_todo_date" type="date" placeholder="日期">
            <input id="wb_todo_time" type="time" placeholder="時間">
            <input id="wb_todo_status" placeholder="狀態（待處理 / 已完成 / 取消）">
            <input id="wb_todo_source_file" type="hidden">
        </div>
        <textarea id="wb_todo_desc" placeholder="詳細說明（選填）"></textarea>
        <div class="toolbar" style="margin-top:8px;">
            <button class="btn primary" data-act="wb-todo-save">儲存待辦</button>
            <button class="btn warn" data-act="wb-todo-reset">清空表單</button>
        </div>
    </div>
`;
}

async function wbSaveTodoAndRefresh() {
    const body = {
        case_number: (document.getElementById("wb_todo_case_number").value || "").trim(),
        client_name: (document.getElementById("wb_todo_client_name").value || "").trim(),
        todo_type: (document.getElementById("wb_todo_type").value || "").trim(),
        todo_date: (document.getElementById("wb_todo_date").value || "").trim(),
        todo_time: (document.getElementById("wb_todo_time").value || "").trim(),
        description: (document.getElementById("wb_todo_desc").value || "").trim(),
        status: (document.getElementById("wb_todo_status").value || "").trim(),
        source_file: (document.getElementById("wb_todo_source_file").value || "").trim(),
    };
    const id = (document.getElementById("wb_todo_id").value || "").trim();
    if (!body.case_number || !body.todo_type) {
        wbSetStatus("請至少填入案件編號與類型。", "warn");
        return;
    }
    if (id) await api(`/api/osc/todos/${Number(id)}`, "PUT", body);
    else await api(`/api/osc/todos`, "POST", body);
    if (state.wb.mode === "client") await openClientWorkbench(state.wb.id, "已儲存待辦並重新整理工作台。");
    if (state.wb.mode === "case") await openCaseWorkbench(state.wb.id, "已儲存待辦並重新整理工作台。");
    await loadMeta();
}

async function wbQuickAction(action) {
    if (state.wb.mode !== "case") return;
    const data = await api(`/api/osc/cases/${encodeURIComponent(state.wb.id)}/quick-action`, "POST", { action });
    const text = data.reply || "已完成動作";
    wbSetStatus("已完成動作，結果已開啟。", "ok");
    showToast("已完成動作。", "ok");
    showWebReplyDialog("MAGI 案件整理", text, data.reply_html || "");
}

async function saveWorkbenchCase() {
    const id = state.wb.id;
    if (!id) return;
    const body = {
        case_number: (document.getElementById("wb_case_case_number")?.value || "").trim(),
        client_name: (document.getElementById("wb_case_client_name")?.value || "").trim(),
        case_category: (document.getElementById("wb_case_case_category")?.value || "").trim(),
        case_type: (document.getElementById("wb_case_case_type")?.value || "").trim(),
        case_stage: (document.getElementById("wb_case_case_stage")?.value || "").trim(),
        case_reason: (document.getElementById("wb_case_case_reason")?.value || "").trim(),
        laf_case_no: (document.getElementById("wb_case_laf_case_no")?.value || "").trim(),
        application_no: (document.getElementById("wb_case_application_no")?.value || "").trim(),
        court_name: (document.getElementById("wb_case_court_name")?.value || "").trim(),
        court_case_no: (document.getElementById("wb_case_court_case_no")?.value || "").trim(),
        status: (document.getElementById("wb_case_status")?.value || "").trim(),
        folder_path: (document.getElementById("wb_case_folder_path")?.value || "").trim(),
        notes: (document.getElementById("wb_case_notes")?.value || "").trim(),
    };
    if (!body.client_name) {
        wbSetStatus("當事人欄位不能空白。", "warn");
        return;
    }
    const resp = await api(`/api/osc/cases/${encodeURIComponent(id)}`, "PUT", body);
    const archive = resp?.archive;
    if (archive && archive.ok && !archive.skipped) {
        wbSetStatus(`案件資料已儲存，結案搬移：${archiveReasonText(archive.reason)}。`, "ok");
    } else if (archive && !archive.ok) {
        wbSetStatus(`案件資料已儲存，但結案搬移未完成：${archiveReasonText(archive.reason)}。`, "warn");
    } else {
        wbSetStatus("案件資料已儲存。", "ok");
    }
    showToast("案件資料已儲存。", "ok");
    showArchiveResult(archive);
    await loadCases();
    await loadMeta();
    if (state.wb.mode === "case") {
        await openCaseWorkbench(id, "案件資料已儲存並重新整理工作台。");
    }
}


function renderLafProgress(rows) {
    const items = rows || [];
    if (!items.length) return `<div class="muted">目前無法扶流程紀錄</div>`;
    return `
    <div class="table-wrap"><table>
        <thead><tr><th>時間</th><th>事件</th><th>狀態</th><th>內容</th></tr></thead>
        <tbody>
            ${items.map(x => {
                const detail = typeof x.event_data === "string" ? x.event_data : JSON.stringify(x.event_data || {});
                return `<tr><td>${esc(x.created_at || "")}</td><td>${esc(x.event_type || "")}</td><td>${esc(x.status || "")}</td><td title="${esc(detail)}">${esc(shortText(detail, 120))}</td></tr>`;
            }).join("")}
        </tbody>
    </table></div>
`;
}

function renderChecklist(rows) {
    const items = rows || [];
    if (!items.length) return `<div class="muted">目前無補件清單資料</div>`;
    return `
    <div class="table-wrap"><table>
        <thead><tr><th>項目</th><th>狀態</th><th>備註</th><th>更新時間</th></tr></thead>
        <tbody>
            ${items.map(x => `<tr><td>${esc(x.item_label || x.item_key || "")}</td><td>${esc(x.status || "")}</td><td>${esc(x.notes || "")}</td><td>${esc(x.last_updated || "")}</td></tr>`).join("")}
        </tbody>
    </table></div>
    `;
}

function isPendingChecklistItem(item) {
    const status = String((item && item.status) || "");
    if (!status) return true;
    return status.includes("待") || status.includes("缺") || status.includes("補");
}

function renderDocsByKeyword(docs, keywords) {
    const hits = (docs || []).filter(d => {
        const s = `${d.file_name || ""} ${d.subfolder_name || ""} ${d.reason || ""}`;
        return keywords.some(k => s.includes(k));
    });
    if (!hits.length) return `<div class="muted">尚未索引到相關檔案</div>`;
    return `
    <div class="table-wrap"><table>
        <thead><tr><th>檔名</th><th>子資料夾</th><th>操作</th></tr></thead>
        <tbody>
            ${hits.map(x => `<tr>
                <td>${esc(x.file_name || "")}</td>
                <td>${esc(x.subfolder_name || "")}</td>
                <td class="actions">
                    <a class="btn slim" href="${fileContentUrl(x.file_path || "", true)}" target="_blank" rel="noopener noreferrer">預覽</a>
                    <button class="btn slim" type="button" data-act="wb-file-share" data-path="${esc(x.file_path || "")}" data-name="${esc(x.file_name || "")}">分享連結</button>
                    <a class="btn slim" href="${fileContentUrl(x.file_path || "")}" target="_blank" rel="noopener noreferrer">下載</a>
                    <button class="btn slim" type="button" data-act="wb-folder-copy-path" data-path="${esc(x.file_path || "")}">複製路徑</button>
                </td>
            </tr>`).join("")}
        </tbody>
    </table></div>
`;
}

async function openClientWorkbench(id, statusText = "") {
    const data = await api(`/api/osc/clients/${encodeURIComponent(id)}/workbench`);
    const c = data.client || {};
    state.wb = { mode: "client", id, data };
    const caseRows = data.cases || [];
    const todoRows = data.todos || [];
    const modalHtml = `
    <div class="card">
        <div class="grid-3">
            <div><strong>姓名</strong><div>${esc(c.name || "")}</div></div>
            <div><strong>電話</strong><div>${esc(c.phone || "")}</div></div>
            <div><strong>Email</strong><div>${esc(c.email || "")}</div></div>
        </div>
    </div>
    <div class="card">
        <h3>當事人案件（可開工作台或直接瀏覽案件資料夾）</h3>
        <div class="table-wrap"><table>
            <thead><tr><th>案號</th><th>案件種類</th><th>案由</th><th>法院案號</th><th>法扶案號</th><th>狀態</th><th>操作</th></tr></thead>
            <tbody id="wbClientCasesBody">
                ${caseRows.map(r => `<tr class="row-clickable" data-case-id="${esc(r.id)}"><td>${esc(r.case_number)}</td><td>${esc(r.case_category)}</td><td>${esc(r.case_reason)}</td><td>${esc(r.court_case_no)}</td><td>${esc(r.laf_case_no)}</td><td>${esc(r.status)}</td><td class="actions"><button class="btn" data-act="wb-case-workbench" data-id="${esc(r.id)}">案件工作台</button><button class="btn" data-act="wb-case-open" data-id="${esc(r.id)}">開資料夾</button></td></tr>`).join("") || `<tr><td colspan="7" class="muted">查無案件</td></tr>`}
            </tbody>
        </table></div>
    </div>
    ${wbRenderTodoForm(caseRows[0]?.case_number || "", c.name || "")}
    <div class="card">
        <h3>待辦列表</h3>
        <div class="table-wrap"><table>
            <thead><tr><th>日期</th><th>案號</th><th>類型</th><th>描述</th><th>狀態</th><th>操作</th></tr></thead>
            <tbody>
                ${todoRows.map(t => `<tr><td>${esc(t.todo_date)} ${esc(t.todo_time)}</td><td>${esc(t.case_number)}</td><td>${esc(t.todo_type)}</td><td>${esc(t.description)}</td><td>${esc(t.status)}</td><td>${wbRenderTodoActions(t)}</td></tr>`).join("") || `<tr><td colspan="6" class="muted">目前沒有待辦</td></tr>`}
            </tbody>
        </table></div>
    </div>
    <div class="grid-2">
        <div class="card"><h3>法扶進度</h3>${renderLafProgress(data.laf_progress || [])}</div>
        <div class="card"><h3>法扶補件/案件補正清單</h3>${renderChecklist(data.legal_aid_checklist || [])}${renderChecklist(data.case_checklist || [])}</div>
    </div>
`;
    wbShow(`當事人工作台｜${c.name || id}`, modalHtml);
    wbSetStatus(statusText || `已載入當事人工作台，共 ${caseRows.length} 筆案件、${todoRows.length} 筆待辦。`, statusText ? "ok" : "info");
}

async function openCaseWorkbench(id, statusText = "") {
    const data = await api(`/api/osc/cases/${encodeURIComponent(id)}/workbench`);
    const c = data.case || {};
    await loadCaseCourtOptions();
    state.wb = { mode: "case", id, data };
    const s = data.stats || {};
    const pendingChecklist = [
        ...(data.legal_aid_checklist || []),
        ...(data.case_checklist || []),
    ].filter(isPendingChecklistItem);
    const modalHtml = `
    <div class="card">
        <div class="grid-3">
            <div><strong>案號</strong><div>${esc(c.case_number || "")}</div></div>
            <div><strong>當事人</strong><div>${esc(c.client_name || "")}</div></div>
            <div><strong>狀態</strong><div>${esc(c.status || "")}</div></div>
            <div><strong>法院 / 地檢署</strong><div>${esc(c.court_name || "")}</div></div>
            <div><strong>法院案號</strong><div>${esc(c.court_case_no || c.court_case_number || "")}</div></div>
            <div><strong>法扶案號</strong><div>${esc(c.laf_case_no || c.legal_aid_number || "")}</div></div>
            <div><strong>案由</strong><div>${esc(c.case_reason || "")}</div></div>
        </div>
    </div>
    <div class="stat-grid">
        <div class="stat-card"><div class="k">待辦總數</div><div class="v">${esc(s.todo_total || 0)}</div></div>
        <div class="stat-card"><div class="k">待處理</div><div class="v">${esc(s.todo_pending || 0)}</div></div>
        <div class="stat-card"><div class="k">已完成</div><div class="v">${esc(s.todo_completed || 0)}</div></div>
        <div class="stat-card"><div class="k">會議</div><div class="v">${esc(s.meeting_total || 0)}</div></div>
        <div class="stat-card"><div class="k">索引檔案</div><div class="v">${esc(s.docs_indexed || 0)}</div></div>
    </div>
    <div class="card">
        <h3>快捷功能（委任狀/收據/結案整理）</h3>
        <div class="toolbar">
            <button class="btn primary" data-act="case-open" data-id="${esc(id)}">📁 開啟案件資料夾</button>
            <button class="btn" data-act="wb-case-action" data-action="generate_power_of_attorney">製作委任狀（交給 CASPER）</button>
            <button class="btn" data-act="wb-case-action" data-action="generate_receipt">製作收據（交給 CASPER）</button>
            <button class="btn warn" data-act="wb-case-action" data-action="closing_overview">結案狀況整理</button>
        </div>
        <div class="muted">註：此區會把案件資料帶給 CASPER，產出草稿/缺漏清單，不會直接送出外部系統。</div>
    </div>
    ${renderWorkbenchCaseEditor(c)}
    ${wbRenderTodoForm(c.case_number || "", c.client_name || "")}
    <div class="grid-2">
        <div class="card">
            <h3>法扶進度</h3>
            ${renderLafProgress(data.laf_progress || [])}
        </div>
        <div class="card">
            <h3>結案狀況</h3>
            <div><strong>案件狀態：</strong>${esc(c.status || "")}</div>
            <div><strong>待補/待辦件數：</strong>${pendingChecklist.length}</div>
            ${renderChecklist(pendingChecklist)}
        </div>
    </div>
    <div class="grid-2">
        <div class="card">
            <h3>委任狀/開辦資料</h3>
            ${renderDocsByKeyword(data.documents || [], ["委任", "委託", "開辦通知", "開辦資料", "接案通知", "法扶資料"])}
        </div>
        <div class="card">
            <h3>判決/結案相關檔案</h3>
            ${renderDocsByKeyword(data.documents || [], ["判決", "裁定", "調解不成立", "結案", "收據", "繳費", "法院通知"])}
        </div>
    </div>
    <div class="grid-2">
        <div class="card">
            <h3>對造資料</h3>
            <div class="table-wrap"><table>
                <thead><tr><th>姓名</th><th>地址</th><th>啟用</th></tr></thead>
                <tbody>
                    ${(data.opponents || []).map(x => `<tr><td>${esc(x.name || "")}</td><td>${esc(x.address || "")}</td><td>${esc(x.is_active ?? "")}</td></tr>`).join("") || `<tr><td colspan="3" class="muted">尚無對造資料</td></tr>`}
                </tbody>
            </table></div>
        </div>
        <div class="card">
            <h3>PDF 產生紀錄</h3>
            <div class="table-wrap"><table>
                <thead><tr><th>時間</th><th>檔名</th><th>狀態</th><th>錯誤</th></tr></thead>
                <tbody>
                    ${(data.pdf_generation_log || []).map(x => `<tr><td>${esc(x.log_timestamp || "")}</td><td>${esc(x.file_name || "")}</td><td>${esc(x.status || "")}</td><td>${esc(shortText(x.error_message, 60))}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">尚無 PDF 產生紀錄</td></tr>`}
                </tbody>
            </table></div>
        </div>
    </div>
    <div class="card">
        <h3>待辦列表</h3>
        <div class="table-wrap"><table>
            <thead><tr><th>日期</th><th>類型</th><th>描述</th><th>狀態</th><th>操作</th></tr></thead>
            <tbody>
                ${(data.todos || []).map(t => `<tr><td>${esc(t.todo_date)} ${esc(t.todo_time)}</td><td>${esc(t.todo_type)}</td><td>${esc(t.description)}</td><td>${esc(t.status)}</td><td>${wbRenderTodoActions(t)}</td></tr>`).join("") || `<tr><td colspan="5" class="muted">沒有待辦</td></tr>`}
            </tbody>
        </table></div>
    </div>
`;
    wbShow(`案件工作台｜${c.case_number || id}`, modalHtml);
    wbSetStatus(statusText || `已載入案件工作台，待辦 ${s.todo_total || 0} 筆、索引檔案 ${s.docs_indexed || 0} 份。`, statusText ? "ok" : "info");
}

// ── Card drag-and-drop ──
function initCardDrag(container) {
    let dragEl = null;
    container.querySelectorAll('.case-card').forEach(card => {
        card.addEventListener('dragstart', e => {
            dragEl = card;
            card.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
        });
        card.addEventListener('dragend', () => {
            if (dragEl) dragEl.classList.remove('dragging');
            dragEl = null;
            // Save order
            const order = [...container.querySelectorAll('.case-card')].map(c => c.dataset.caseId);
            localStorage.setItem('caseCardOrder', JSON.stringify(order));
        });
        card.addEventListener('dragover', e => {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            if (!dragEl || dragEl === card) return;
            const rect = card.getBoundingClientRect();
            const mid = rect.top + rect.height / 2;
            if (e.clientY < mid) container.insertBefore(dragEl, card);
            else container.insertBefore(dragEl, card.nextSibling);
        });
        card.addEventListener('dblclick', () => {
            const id = card.dataset.caseId;
            if (id) openCaseFolder(id);
        });
    });
}

function bindCaseCardOpen(container) {
    container.querySelectorAll('[data-act="case-open"]').forEach(btn => {
        if (btn._caseOpenDirectBound) return;
        btn._caseOpenDirectBound = true;
        btn.addEventListener('click', async (e) => {
            e.preventDefault();
            e.stopPropagation();
            const caseId = btn.dataset.id;
            if (!caseId) return;
            try {
                await openCaseFolder(caseId);
            } catch (err) {
                showToast(`開啟案件資料夾失敗：${err.message}`, "warn", 2800);
            }
        });
    });
    container.querySelectorAll('.case-card').forEach(card => {
        if (card._caseOpenBound) return;
        card._caseOpenBound = true;
        card.addEventListener('click', async (e) => {
            if (e.target.closest('button,a,input,select,textarea,[data-act]')) return;
            const caseId = card.dataset.caseId;
            if (!caseId) return;
            try {
                await openCaseFolder(caseId);
            } catch (err) {
                showToast(`開啟案件資料夾失敗：${err.message}`, "warn", 2800);
            }
        });
    });
}

// ── Card/Table view toggle ──
function initCaseViewToggle() {
    const btn = document.getElementById('caseViewToggle');
    if (!btn) return;
    const savedView = localStorage.getItem('caseViewMode') || 'card';
    applyCaseView(savedView);
    btn.addEventListener('click', () => {
        const current = localStorage.getItem('caseViewMode') || 'card';
        const next = current === 'card' ? 'table' : 'card';
        localStorage.setItem('caseViewMode', next);
        applyCaseView(next);
    });
}
function applyCaseView(mode) {
    const cardGrid = document.getElementById('casesCardGrid');
    const tableWrap = document.getElementById('casesTableWrap');
    const btn = document.getElementById('caseViewToggle');
    if (mode === 'table') {
        if (cardGrid) cardGrid.style.display = 'none';
        if (tableWrap) tableWrap.style.display = '';
        if (btn) btn.textContent = '切換卡片';
    } else {
        if (cardGrid) cardGrid.style.display = '';
        if (tableWrap) tableWrap.style.display = 'none';
        if (btn) btn.textContent = '切換表格';
    }
}

/* ── Clients & Meetings ── */
async function loadClients() {
    const q = encodeURIComponent((document.getElementById("clientsQ").value || "").trim());
    const data = await api(`/api/osc/clients?limit=300&q=${q}`);
    state.clients = data.items || [];
    renderClients();
}

function renderClients() {
    const body = document.getElementById("clientsBody");
    if (!state.clients.length) {
        body.innerHTML = `<tr><td colspan="6" class="muted">沒有資料</td></tr>`;
        return;
    }
    const sorted = applySort([...state.clients], state.sort.col, state.sort.dir, state.sort.type);
    body.innerHTML = sorted.map(r => `
    <tr class="row-clickable" data-client-id="${esc(r.id)}">
        <td>${esc(r.name)}</td>
        <td>${esc(r.phone)}</td>
        <td>${esc(r.email)}</td>
        <td>${esc(r.address)}</td>
        <td>${esc(r.status)}</td>
        <td class="actions">
            <button class="btn" data-act="client-workbench" data-id="${esc(r.id)}">工作台</button>
            <button class="btn" data-act="client-edit" data-id="${esc(r.id)}">編輯</button>
            <button class="btn danger" data-act="client-del" data-id="${esc(r.id)}">刪除</button>
        </td>
    </tr>
`).join("");

    const ts = document.querySelectorAll("#clients th[data-sort]");
    ts.forEach(th => {
        th.innerHTML = th.innerHTML.replace(/ [▲▼]/g, "") + renderSortArrow(th.dataset.sort);
    });
}

async function editClient(id) {
    const data = await api(`/api/osc/clients/${encodeURIComponent(id)}`);
    writeFields("client_", data.item || {}, ["id", "name", "contact_person", "phone", "email", "address", "tax_id", "notes", "status"]);
}

async function delClient(id) {
    if (!confirm(`確定刪除當事人 ${id}？`)) return;
    await api(`/api/osc/clients/${encodeURIComponent(id)}`, "DELETE");
    await loadClients();
    await loadMeta();
}

async function saveClient() {
    const p = readFields(["client_id", "client_name", "client_contact_person", "client_phone", "client_email", "client_address", "client_tax_id", "client_notes", "client_status"]);
    const body = {
        id: p.client_id, name: p.client_name, contact_person: p.client_contact_person, phone: p.client_phone,
        email: p.client_email, address: p.client_address, tax_id: p.client_tax_id, notes: p.client_notes, status: p.client_status
    };
    if ((body.id || "").trim()) await api(`/api/osc/clients/${encodeURIComponent(body.id)}`, "PUT", body);
    else await api("/api/osc/clients", "POST", body);
    clearFields(["client_id", "client_name", "client_contact_person", "client_phone", "client_email", "client_address", "client_tax_id", "client_notes", "client_status"]);
    await loadClients();
    await loadMeta();
}

async function loadMeetings() {
    const q = encodeURIComponent((document.getElementById("meetingsQ").value || "").trim());
    const data = await api(`/api/osc/meetings?limit=300&q=${q}`);
    state.meetings = data.items || [];
    renderMeetings();
}

function renderMeetings() {
    const body = document.getElementById("meetingsBody");
    if (!state.meetings.length) {
        body.innerHTML = `<tr><td colspan="7" class="muted">沒有資料</td></tr>`;
        return;
    }
    const sorted = applySort([...state.meetings], state.sort.col, state.sort.dir, state.sort.type);
    body.innerHTML = sorted.map(r => `
    <tr>
        <td>${esc(r.datetime)}</td>
        <td>${esc(r.case_number)}</td>
        <td>${esc(r.client_name)}</td>
        <td>${esc(r.type)}</td>
        <td>${esc(r.location)}</td>
        <td>${esc(r.status)}</td>
        <td class="actions">
            <button class="btn" data-act="meeting-edit" data-id="${Number(r.id)}">編輯</button>
            <button class="btn danger" data-act="meeting-del" data-id="${Number(r.id)}">刪除</button>
        </td>
    </tr>
`).join("");

    const ts = document.querySelectorAll("#meetings th[data-sort]");
    ts.forEach(th => {
        th.innerHTML = th.innerHTML.replace(/ [▲▼]/g, "") + renderSortArrow(th.dataset.sort);
    });
}

async function editMeeting(id) {
    const data = await api(`/api/osc/meetings/${id}`);
    writeFields("meeting_", data.item || {}, ["id", "case_number", "client_name", "type", "datetime", "duration", "location", "notes", "status"]);
}

async function delMeeting(id) {
    if (!confirm(`確定刪除會議 ${id}？`)) return;
    await api(`/api/osc/meetings/${id}`, "DELETE");
    await loadMeetings();
    await loadMeta();
}

/* ── Cases CSV Import / Export ── */
async function importCasesCsv() {
    const f = document.getElementById("casesImportCsvFile");
    f.value = "";
    f.click();
}

async function handleCasesCsvUpload(file) {
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    showToast("匯入中...", "info", 2000);
    try {
        const res = await fetch("/api/osc/cases/import-csv", { method: "POST", body: fd });
        const data = await res.json();
        if (data.ok) {
            const errMsg = (data.errors || []).slice(0, 3).map(e => `第 ${e.row} 行: ${e.reason}`).join("\n");
            showToast(`✅ 匯入完成：成功 ${data.imported} 筆 / 跳過 ${data.skipped}${errMsg ? "\n" + errMsg : ""}`, "ok", 5000);
            await loadCases();
        } else {
            showToast(`匯入失敗：${data.error || "未知錯誤"}`, "warn", 4000);
        }
    } catch (err) {
        showToast(`匯入失敗：${err.message}`, "warn", 4000);
    }
}

function exportCasesCsv() {
    window.location.href = "/api/osc/cases/export-csv";
}

/* ── Clients CSV Import / Export ── */
async function importClientsCsv() {
    const f = document.getElementById("clientsImportCsvFile");
    f.value = "";
    f.click();
}

async function handleClientsCsvUpload(file) {
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    showToast("匯入中...", "info", 2000);
    try {
        const res = await fetch("/api/osc/clients/import-csv", { method: "POST", body: fd });
        const data = await res.json();
        if (data.ok) {
            const errMsg = (data.errors || []).slice(0, 3).map(e => `第 ${e.row} 行: ${e.reason}`).join("\n");
            showToast(`✅ 匯入完成：成功 ${data.imported} 筆 / 跳過 ${data.skipped}${errMsg ? "\n" + errMsg : ""}`, "ok", 5000);
            await loadClients();
        } else {
            showToast(`匯入失敗：${data.error || "未知錯誤"}`, "warn", 4000);
        }
    } catch (err) {
        showToast(`匯入失敗：${err.message}`, "warn", 4000);
    }
}

function exportClientsCsv() {
    window.location.href = "/api/osc/clients/export-csv";
}

async function saveMeeting() {
    const p = readFields(["meeting_id", "meeting_case_number", "meeting_client_name", "meeting_type", "meeting_datetime", "meeting_duration", "meeting_location", "meeting_notes", "meeting_status"]);
    const body = {
        case_number: p.meeting_case_number, client_name: p.meeting_client_name, type: p.meeting_type, datetime: p.meeting_datetime,
        duration: p.meeting_duration, location: p.meeting_location, notes: p.meeting_notes, status: p.meeting_status
    };
    if ((p.meeting_id || "").trim()) await api(`/api/osc/meetings/${Number(p.meeting_id)}`, "PUT", body);
    else await api("/api/osc/meetings", "POST", body);
    clearFields(["meeting_id", "meeting_case_number", "meeting_client_name", "meeting_type", "meeting_datetime", "meeting_duration", "meeting_location", "meeting_notes", "meeting_status"]);
    await loadMeetings();
    await loadMeta();
}

// ── 地址標籤 Dialog ──────────────────────────────────────────────────────────

function addressLabelDialog(caseId) {
    const existingDlg = document.getElementById("address-label-dialog");
    if (existingDlg) existingDlg.remove();

    const dlg = document.createElement("dialog");
    dlg.id = "address-label-dialog";
    dlg.innerHTML = `
        <div style="min-width:360px;padding:16px;">
            <h3 style="margin:0 0 12px 0;">📮 地址標籤</h3>
            <p style="margin:0 0 10px 0;color:#555;">選擇收件人類型：</p>
            <div style="display:flex;gap:8px;margin-bottom:14px;">
                <button class="btn" id="al-btn-court">🏛 法院地址</button>
                <button class="btn" id="al-btn-defendant">👤 對造地址</button>
                <button class="btn" id="al-btn-laf">📋 法扶分會</button>
            </div>
            <div id="al-preview" style="margin-bottom:12px;text-align:center;min-height:40px;"></div>
            <div style="display:flex;gap:8px;justify-content:flex-end;">
                <button class="btn" id="al-download-btn" style="display:none;">📥 下載</button>
                <button class="btn" id="al-close-btn">關閉</button>
            </div>
        </div>
    `;
    document.body.appendChild(dlg);
    dlg.showModal();

    let currentRecipient = null;

    async function selectRecipient(recipient) {
        currentRecipient = recipient;
        const previewDiv = document.getElementById("al-preview");
        previewDiv.innerHTML = `<span style="color:#888">載入中…</span>`;
        document.getElementById("al-download-btn").style.display = "none";
        try {
            const url = `/api/osc/cases/${encodeURIComponent(caseId)}/address-label?mode=preview&recipient=${recipient}`;
            const resp = await fetch(url, { credentials: "same-origin" });
            if (!resp.ok) {
                const json = await resp.json().catch(() => ({}));
                previewDiv.innerHTML = `<span style="color:red">錯誤：${json.error || resp.statusText}</span>`;
                return;
            }
            const blob = await resp.blob();
            const objUrl = URL.createObjectURL(blob);
            previewDiv.innerHTML = `<img src="${objUrl}" style="max-width:100%;border:1px solid #ddd;border-radius:4px;" />`;
            document.getElementById("al-download-btn").style.display = "inline-block";
        } catch (e) {
            previewDiv.innerHTML = `<span style="color:red">載入失敗：${e.message}</span>`;
        }
    }

    document.getElementById("al-btn-court").addEventListener("click", () => selectRecipient("court"));
    document.getElementById("al-btn-defendant").addEventListener("click", () => selectRecipient("defendant"));
    document.getElementById("al-btn-laf").addEventListener("click", () => selectRecipient("laf"));

    document.getElementById("al-download-btn").addEventListener("click", () => {
        if (!currentRecipient) return;
        window.location.href = `/api/osc/cases/${encodeURIComponent(caseId)}/address-label?mode=download&recipient=${currentRecipient}`;
    });

    document.getElementById("al-close-btn").addEventListener("click", () => {
        dlg.close();
        dlg.remove();
    });
}
