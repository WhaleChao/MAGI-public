/* tabs/admin.js – Admin panel CRUD functions */
async function loadAdminData() {
    await Promise.all([
        loadAdminSettings(),
        loadAdminCaseReasons(),
        loadAdminCourts(),
        loadAdminBranches(),
        loadAdminUserSettings(),
        loadAdminMemoryKeywords(),
        loadAdminOpponents(),
        loadAdminPdfLogs(),
        loadAdminActivityLogs(),
        loadOscBackups(),
        loadGcalStatus(),
    ]);
}

async function loadAdminSettings() {
    const q = encodeURIComponent((document.getElementById("adminSettingsQ").value || "").trim());
    const data = await api(`/api/osc/settings?limit=200&q=${q}`);
    state.adminSettings = data.items || [];
    renderSimpleRows(
        "adminSettingsBody",
        state.adminSettings.map(r => `<tr><td>${esc(r.key)}</td><td>${esc(shortText(r.value, 60))}</td><td>${esc(shortText(r.description, 60))}</td><td>${esc(r.updated_date || "")}</td><td class="actions"><button class="btn" data-act="admin-setting-edit" data-key="${esc(r.key)}">編輯</button><button class="btn danger" data-act="admin-setting-del" data-key="${esc(r.key)}">刪除</button></td></tr>`),
        5,
        "沒有 settings 資料"
    );
}

async function editAdminSetting(key) {
    const data = await api(`/api/osc/settings/${encodeURIComponent(key)}`);
    const x = data.item || {};
    document.getElementById("adminSettingKey").value = x.key || "";
    document.getElementById("adminSettingValue").value = x.value || "";
    document.getElementById("adminSettingDescription").value = x.description || "";
}

async function saveAdminSetting() {
    const body = {
        key: (document.getElementById("adminSettingKey").value || "").trim(),
        value: (document.getElementById("adminSettingValue").value || "").trim(),
        description: (document.getElementById("adminSettingDescription").value || "").trim(),
    };
    if (!body.key) return alert("請輸入 key");
    await api("/api/osc/settings", "POST", body);
    clearFields(["adminSettingKey", "adminSettingValue", "adminSettingDescription"]);
    await loadAdminSettings();
    await loadMeta();
}

async function delAdminSetting(key) {
    if (!confirm(`確定刪除設定 ${key}？`)) return;
    await api(`/api/osc/settings/${encodeURIComponent(key)}`, "DELETE");
    await loadAdminSettings();
    await loadMeta();
}

async function loadAdminCaseReasons() {
    const q = encodeURIComponent((document.getElementById("adminReasonQ").value || "").trim());
    const caseType = encodeURIComponent((document.getElementById("adminReasonTypeFilter").value || "").trim());
    const data = await api(`/api/osc/case-reason-templates?limit=200&q=${q}&case_type=${caseType}`);
    state.adminCaseReasons = data.items || [];
    renderSimpleRows(
        "adminReasonBody",
        state.adminCaseReasons.map(r => `<tr><td>${esc(r.id)}</td><td>${esc(r.case_type)}</td><td>${esc(r.reason)}</td><td>${esc(r.is_common)}</td><td class="actions"><button class="btn" data-act="admin-reason-edit" data-id="${Number(r.id)}">編輯</button><button class="btn danger" data-act="admin-reason-del" data-id="${Number(r.id)}">刪除</button></td></tr>`),
        5,
        "沒有案由模板資料"
    );
}

async function editAdminCaseReason(id) {
    const data = await api(`/api/osc/case-reason-templates/${Number(id)}`);
    const x = data.item || {};
    document.getElementById("adminReasonId").value = x.id || "";
    document.getElementById("adminReasonType").value = x.case_type || "";
    document.getElementById("adminReasonText").value = x.reason || "";
    document.getElementById("adminReasonCommon").value = x.is_common ?? 0;
}

async function saveAdminCaseReason() {
    const body = {
        case_type: (document.getElementById("adminReasonType").value || "").trim(),
        reason: (document.getElementById("adminReasonText").value || "").trim(),
        is_common: (document.getElementById("adminReasonCommon").value || "0").trim(),
    };
    const id = (document.getElementById("adminReasonId").value || "").trim();
    if (!body.case_type || !body.reason) return alert("請輸入案型與案由");
    if (id) await api(`/api/osc/case-reason-templates/${Number(id)}`, "PUT", body);
    else await api(`/api/osc/case-reason-templates`, "POST", body);
    clearFields(["adminReasonId", "adminReasonType", "adminReasonText", "adminReasonCommon"]);
    await loadAdminCaseReasons();
    await loadMeta();
}

async function delAdminCaseReason(id) {
    if (!confirm(`確定刪除案由模板 ${id}？`)) return;
    await api(`/api/osc/case-reason-templates/${Number(id)}`, "DELETE");
    await loadAdminCaseReasons();
    await loadMeta();
}

async function loadAdminCourts() {
    const q = encodeURIComponent((document.getElementById("adminCourtsQ").value || "").trim());
    const data = await api(`/api/osc/courts?limit=200&q=${q}`);
    state.adminCourts = data.items || [];
    renderSimpleRows(
        "adminCourtsBody",
        state.adminCourts.map(r => `<tr><td>${esc(r.name)}</td><td>${esc(r.type || "")}</td><td>${esc(shortText(r.address, 80))}</td><td class="actions"><button class="btn" data-act="admin-court-edit" data-id="${Number(r.id)}">編輯</button><button class="btn danger" data-act="admin-court-del" data-id="${Number(r.id)}">刪除</button></td></tr>`),
        4,
        "沒有法院資料"
    );
}

async function editAdminCourt(id) {
    const data = await api(`/api/osc/courts/${Number(id)}`);
    const x = data.item || {};
    document.getElementById("adminCourtId").value = x.id || "";
    document.getElementById("adminCourtName").value = x.name || "";
    document.getElementById("adminCourtType").value = x.type || "";
    document.getElementById("adminCourtAddress").value = x.address || "";
}

async function saveAdminCourt() {
    const body = {
        name: (document.getElementById("adminCourtName").value || "").trim(),
        type: (document.getElementById("adminCourtType").value || "").trim(),
        address: (document.getElementById("adminCourtAddress").value || "").trim(),
    };
    const id = (document.getElementById("adminCourtId").value || "").trim();
    if (!body.name || !body.address) return alert("請輸入法院名稱與地址");
    if (id) await api(`/api/osc/courts/${Number(id)}`, "PUT", body);
    else await api(`/api/osc/courts`, "POST", body);
    clearFields(["adminCourtId", "adminCourtName", "adminCourtType", "adminCourtAddress"]);
    await loadAdminCourts();
    await loadMeta();
}

async function delAdminCourt(id) {
    if (!confirm(`確定刪除法院 ${id}？`)) return;
    await api(`/api/osc/courts/${Number(id)}`, "DELETE");
    await loadAdminCourts();
    await loadMeta();
}

async function loadAdminBranches() {
    const q = encodeURIComponent((document.getElementById("adminBranchesQ").value || "").trim());
    const data = await api(`/api/osc/legal-aid-branches?limit=200&q=${q}`);
    state.adminBranches = data.items || [];
    renderSimpleRows(
        "adminBranchesBody",
        state.adminBranches.map(r => `<tr><td>${esc(r.name)}</td><td>${esc(shortText(r.address, 80))}</td><td class="actions"><button class="btn" data-act="admin-branch-edit" data-id="${Number(r.id)}">編輯</button><button class="btn danger" data-act="admin-branch-del" data-id="${Number(r.id)}">刪除</button></td></tr>`),
        3,
        "沒有法扶分會資料"
    );
}

async function editAdminBranch(id) {
    const data = await api(`/api/osc/legal-aid-branches/${Number(id)}`);
    const x = data.item || {};
    document.getElementById("adminBranchId").value = x.id || "";
    document.getElementById("adminBranchName").value = x.name || "";
    document.getElementById("adminBranchAddress").value = x.address || "";
}

async function saveAdminBranch() {
    const body = {
        name: (document.getElementById("adminBranchName").value || "").trim(),
        address: (document.getElementById("adminBranchAddress").value || "").trim(),
    };
    const id = (document.getElementById("adminBranchId").value || "").trim();
    if (!body.name || !body.address) return alert("請輸入分會名稱與地址");
    if (id) await api(`/api/osc/legal-aid-branches/${Number(id)}`, "PUT", body);
    else await api(`/api/osc/legal-aid-branches`, "POST", body);
    clearFields(["adminBranchId", "adminBranchName", "adminBranchAddress"]);
    await loadAdminBranches();
    await loadMeta();
}

async function delAdminBranch(id) {
    if (!confirm(`確定刪除分會 ${id}？`)) return;
    await api(`/api/osc/legal-aid-branches/${Number(id)}`, "DELETE");
    await loadAdminBranches();
    await loadMeta();
}

async function loadAdminUserSettings() {
    const q = encodeURIComponent((document.getElementById("adminUserSettingsQ").value || "").trim());
    const data = await api(`/api/osc/user-settings?limit=200&q=${q}`);
    state.adminUserSettings = data.items || [];
    renderSimpleRows(
        "adminUserSettingsBody",
        state.adminUserSettings.map(r => `<tr><td>${esc(r.hostname)}</td><td>${esc(r.setting_key)}</td><td>${esc(shortText(r.setting_value, 80))}</td><td class="actions"><button class="btn" data-act="admin-user-setting-edit" data-id="${Number(r.id)}">編輯</button><button class="btn danger" data-act="admin-user-setting-del" data-id="${Number(r.id)}">刪除</button></td></tr>`),
        4,
        "沒有使用者設定資料"
    );
}

async function editAdminUserSetting(id) {
    const data = await api(`/api/osc/user-settings/${Number(id)}`);
    const x = data.item || {};
    document.getElementById("adminUserSettingId").value = x.id || "";
    document.getElementById("adminUserSettingHost").value = x.hostname || "";
    document.getElementById("adminUserSettingKey").value = x.setting_key || "";
    document.getElementById("adminUserSettingValue").value = x.setting_value || "";
}

async function saveAdminUserSetting() {
    const body = {
        hostname: (document.getElementById("adminUserSettingHost").value || "").trim(),
        setting_key: (document.getElementById("adminUserSettingKey").value || "").trim(),
        setting_value: (document.getElementById("adminUserSettingValue").value || "").trim(),
    };
    const id = (document.getElementById("adminUserSettingId").value || "").trim();
    if (!body.hostname || !body.setting_key) return alert("請輸入 hostname 與 setting_key");
    if (id) await api(`/api/osc/user-settings/${Number(id)}`, "PUT", body);
    else await api(`/api/osc/user-settings`, "POST", body);
    clearFields(["adminUserSettingId", "adminUserSettingHost", "adminUserSettingKey", "adminUserSettingValue"]);
    await loadAdminUserSettings();
    await loadMeta();
}

async function delAdminUserSetting(id) {
    if (!confirm(`確定刪除使用者設定 ${id}？`)) return;
    await api(`/api/osc/user-settings/${Number(id)}`, "DELETE");
    await loadAdminUserSettings();
    await loadMeta();
}

async function loadAdminMemoryKeywords() {
    const q = encodeURIComponent((document.getElementById("adminMemoryKeywordsQ").value || "").trim());
    const caseNumber = encodeURIComponent((document.getElementById("adminMemoryCaseFilter").value || "").trim());
    const data = await api(`/api/osc/memory-keywords?limit=200&q=${q}&case_number=${caseNumber}`);
    state.adminMemoryKeywords = data.items || [];
    renderSimpleRows(
        "adminMemoryKeywordsBody",
        state.adminMemoryKeywords.map(r => `<tr><td>${esc(r.case_number)}</td><td>${esc(r.hotkey)}</td><td>${esc(r.name || "")}</td><td>${esc(shortText(r.value, 80))}</td><td class="actions"><button class="btn" data-act="admin-memory-edit" data-case="${esc(r.case_number)}" data-hotkey="${esc(r.hotkey)}">編輯</button><button class="btn danger" data-act="admin-memory-del" data-case="${esc(r.case_number)}" data-hotkey="${esc(r.hotkey)}">刪除</button></td></tr>`),
        5,
        "沒有案件熱鍵資料"
    );
}

async function editAdminMemoryKeyword(caseNumber, hotkey) {
    const data = await api(`/api/osc/memory-keywords/${encodeURIComponent(caseNumber)}/${encodeURIComponent(hotkey)}`);
    const x = data.item || {};
    document.getElementById("adminMemoryCaseNumber").value = x.case_number || "";
    document.getElementById("adminMemoryHotkey").value = x.hotkey || "";
    document.getElementById("adminMemoryName").value = x.name || "";
    document.getElementById("adminMemoryValue").value = x.value || "";
}

async function saveAdminMemoryKeyword() {
    const body = {
        case_number: (document.getElementById("adminMemoryCaseNumber").value || "").trim(),
        hotkey: (document.getElementById("adminMemoryHotkey").value || "").trim(),
        name: (document.getElementById("adminMemoryName").value || "").trim(),
        value: (document.getElementById("adminMemoryValue").value || "").trim(),
    };
    if (!body.case_number || !body.hotkey) return alert("請輸入案件編號與 hotkey");
    await api("/api/osc/memory-keywords", "POST", body);
    clearFields(["adminMemoryCaseNumber", "adminMemoryHotkey", "adminMemoryName", "adminMemoryValue"]);
    await loadAdminMemoryKeywords();
    await loadMeta();
}

async function delAdminMemoryKeyword(caseNumber, hotkey) {
    if (!confirm(`確定刪除熱鍵 ${caseNumber}/${hotkey}？`)) return;
    await api(`/api/osc/memory-keywords/${encodeURIComponent(caseNumber)}/${encodeURIComponent(hotkey)}`, "DELETE");
    await loadAdminMemoryKeywords();
    await loadMeta();
}

async function loadAdminOpponents() {
    const q = encodeURIComponent((document.getElementById("adminOpponentsQ").value || "").trim());
    const caseNumber = encodeURIComponent((document.getElementById("adminOpponentsCaseFilter").value || "").trim());
    const data = await api(`/api/osc/opponents?limit=200&q=${q}&case_number=${caseNumber}`);
    state.adminOpponents = data.items || [];
    renderSimpleRows(
        "adminOpponentsBody",
        state.adminOpponents.map(r => `<tr><td>${esc(r.case_number)}</td><td>${esc(r.name)}</td><td>${esc(shortText(r.address, 80))}</td><td>${esc(r.is_active)}</td><td class="actions"><button class="btn" data-act="admin-opponent-edit" data-id="${Number(r.id)}">編輯</button><button class="btn danger" data-act="admin-opponent-del" data-id="${Number(r.id)}">刪除</button></td></tr>`),
        5,
        "沒有對造資料"
    );
}

async function editAdminOpponent(id) {
    const data = await api(`/api/osc/opponents/${Number(id)}`);
    const x = data.item || {};
    document.getElementById("adminOpponentId").value = x.id || "";
    document.getElementById("adminOpponentCaseNumber").value = x.case_number || "";
    document.getElementById("adminOpponentName").value = x.name || "";
    document.getElementById("adminOpponentAddress").value = x.address || "";
    document.getElementById("adminOpponentActive").value = x.is_active ?? 1;
}

async function saveAdminOpponent() {
    const body = {
        case_number: (document.getElementById("adminOpponentCaseNumber").value || "").trim(),
        name: (document.getElementById("adminOpponentName").value || "").trim(),
        address: (document.getElementById("adminOpponentAddress").value || "").trim(),
        is_active: (document.getElementById("adminOpponentActive").value || "1").trim(),
    };
    const id = (document.getElementById("adminOpponentId").value || "").trim();
    if (!body.case_number || !body.name) return alert("請輸入案件編號與對造姓名");
    if (id) await api(`/api/osc/opponents/${Number(id)}`, "PUT", body);
    else await api(`/api/osc/opponents`, "POST", body);
    clearFields(["adminOpponentId", "adminOpponentCaseNumber", "adminOpponentName", "adminOpponentAddress", "adminOpponentActive"]);
    await loadAdminOpponents();
    await loadMeta();
}

async function delAdminOpponent(id) {
    if (!confirm(`確定刪除對造 ${id}？`)) return;
    await api(`/api/osc/opponents/${Number(id)}`, "DELETE");
    await loadAdminOpponents();
    await loadMeta();
}

async function loadAdminPdfLogs() {
    const q = encodeURIComponent((document.getElementById("adminPdfLogsQ").value || "").trim());
    const caseNumber = encodeURIComponent((document.getElementById("adminPdfLogsCaseFilter").value || "").trim());
    const data = await api(`/api/osc/pdf-generation-log?limit=200&q=${q}&case_number=${caseNumber}`);
    state.adminPdfLogs = data.items || [];
    renderSimpleRows(
        "adminPdfLogsBody",
        state.adminPdfLogs.map(r => `<tr><td>${esc(r.log_timestamp)}</td><td>${esc(r.case_number)}</td><td>${esc(r.file_name || "")}</td><td>${esc(r.status || "")}</td><td>${esc(shortText(r.error_message, 80))}</td><td class="actions"><button class="btn danger" data-act="admin-pdf-log-del" data-id="${Number(r.id)}">刪除</button></td></tr>`),
        6,
        "沒有 PDF 產生紀錄"
    );
}

async function delAdminPdfLog(id) {
    if (!confirm(`確定刪除 PDF log ${id}？`)) return;
    await api(`/api/osc/pdf-generation-log/${Number(id)}`, "DELETE");
    await loadAdminPdfLogs();
    await loadMeta();
}

async function loadAdminActivityLogs() {
    const q = encodeURIComponent((document.getElementById("adminActivityLogsQ").value || "").trim());
    const entityType = encodeURIComponent((document.getElementById("adminActivityTypeFilter").value || "").trim());
    const data = await api(`/api/osc/activity-logs?limit=200&q=${q}&entity_type=${entityType}`);
    state.adminActivityLogs = data.items || [];
    renderSimpleRows(
        "adminActivityLogsBody",
        state.adminActivityLogs.map(r => `<tr><td>${esc(r.timestamp)}</td><td>${esc(r.action)}</td><td>${esc(r.entity_type || "")}</td><td>${esc(r.entity_id || "")}</td><td>${esc(r.user || "")}</td><td>${esc(shortText(r.details, 100))}</td><td class="actions"><button class="btn danger" data-act="admin-activity-del" data-id="${Number(r.id)}">刪除</button></td></tr>`),
        7,
        "沒有活動紀錄"
    );
}

async function delAdminActivityLog(id) {
    if (!confirm(`確定刪除活動紀錄 ${id}？`)) return;
    await api(`/api/osc/activity-logs/${Number(id)}`, "DELETE");
    await loadAdminActivityLogs();
    await loadMeta();
}

// ── Discord Webhook UI（P2 對應 PaperClip osc.py:22790 SettingsDialog discord_group）──

async function loadDiscordWebhook() {
    const input = document.getElementById("discordWebhookUrl");
    if (!input) return;
    try {
        const data = await api("/api/osc/settings/discord_webhook_url");
        if (data && data.ok && data.item) {
            input.value = data.item.value || "";
        }
    } catch (_) {
        // 沒這 key 是正常情況
    }
}

async function saveDiscordWebhook() {
    const url = (document.getElementById("discordWebhookUrl").value || "").trim();
    const status = document.getElementById("discordWebhookStatus");
    if (!url) {
        if (status) status.textContent = "Webhook URL 為空。";
        return;
    }
    if (!/^https:\/\/(discord\.com|discordapp\.com)\/api\/webhooks\//.test(url)) {
        if (status) status.textContent = "❌ 無效的 Discord webhook URL（必須是 https://discord.com/api/webhooks/...）。";
        return;
    }
    try {
        await api("/api/osc/settings", "POST", {
            key: "discord_webhook_url",
            value: url,
            description: "Discord webhook URL（Paperclip 通知推播用）",
        });
        if (status) status.textContent = "✅ 已儲存到 settings.discord_webhook_url";
        showToast("Discord webhook 設定已儲存。", "ok");
    } catch (err) {
        if (status) status.textContent = `❌ 儲存失敗：${err.message}`;
    }
}

async function testDiscordWebhook() {
    const url = (document.getElementById("discordWebhookUrl").value || "").trim();
    const status = document.getElementById("discordWebhookStatus");
    if (status) status.textContent = "推播中...";
    try {
        const res = await api("/api/osc/discord/test", "POST", {
            webhook_url: url,
            message: "✅ MAGI Paperclip Webhook 連線測試 — 此訊息確認 webhook 正常。",
        });
        if (res && res.ok) {
            if (status) status.textContent = `✅ Test 推播成功（HTTP ${res.status_code}）。請到 Discord 頻道確認。`;
            showToast("Discord 推播成功！", "ok");
        } else {
            if (status) status.textContent = `❌ Test 失敗：${res?.error || "未知錯誤"}`;
            showToast(`Discord Test 失敗：${res?.error || ""}`, "warn", 4000);
        }
    } catch (err) {
        if (status) status.textContent = `❌ Test 失敗：${err.message}`;
    }
}
// ── P3: Backup / Restore ──────────────────────────────────────────────────────

function _fmtBytes(b) {
    if (b == null) return "-";
    if (b < 1024) return `${b} B`;
    if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
    return `${(b / (1024 * 1024)).toFixed(2)} MB`;
}

function _fmtTableCounts(tc) {
    if (!tc || typeof tc !== "object") return "-";
    return Object.entries(tc).filter(([, v]) => v > 0).map(([k, v]) => `${k}:${v}`).join(" ");
}

async function loadOscBackups() {
    const data = await api("/api/osc/backups");
    const items = data.items || [];
    const tbody = document.getElementById("oscBackupBody");
    if (!tbody) return;
    if (!items.length) {
        tbody.innerHTML = `<tr><td colspan="5" class="muted">尚無備份檔案</td></tr>`;
        return;
    }
    tbody.innerHTML = items.map(item => `<tr>
        <td>${esc(item.filename)}</td>
        <td>${esc(item.created_at || "")}</td>
        <td>${esc(_fmtBytes(item.size_bytes))}</td>
        <td class="muted small">${esc(_fmtTableCounts(item.table_counts))}</td>
        <td class="actions">
            <button class="btn" data-act="osc-backup-dry-run" data-filename="${esc(item.filename)}">Dry-run</button>
            <button class="btn primary" data-act="osc-backup-restore" data-filename="${esc(item.filename)}">確認還原</button>
            <button class="btn danger" data-act="osc-backup-del" data-filename="${esc(item.filename)}">刪除</button>
        </td>
    </tr>`).join("");
}

async function createOscBackup() {
    const res = await api("/api/osc/backups", "POST", { label: "manual" });
    showToast(`備份完成：${res.filename || ""}`);
    await loadOscBackups();
}

async function restoreOscBackup(filename, dryRun) {
    if (!dryRun) {
        if (!confirm(`確定要從 ${filename} 還原資料嗎？\n（已有的紀錄不會被覆蓋，只補缺少的筆數）`)) return;
    }
    const payload = dryRun ? { dry_run: true } : { confirm: true };
    const res = await api(`/api/osc/backups/${encodeURIComponent(filename)}/restore`, "POST", payload);
    const mode = dryRun ? "Dry-run 預覽" : "還原完成";
    showToast(`${mode}：插入 ${res.inserted_count ?? 0} 筆，略過 ${res.skipped_count ?? 0} 筆${res.errors && res.errors.length ? `（${res.errors.length} 錯誤）` : ""}`);
    if (res.errors && res.errors.length) {
        console.warn("[backup restore errors]", res.errors);
    }
}

async function delOscBackup(filename) {
    if (!confirm(`確定刪除備份 ${filename}？`)) return;
    await api(`/api/osc/backups/${encodeURIComponent(filename)}`, "DELETE");
    showToast("備份已刪除");
    await loadOscBackups();
}

// ── P4: Google Calendar 同步 ──────────────────────────────────────────────────

async function loadGcalStatus() {
    const statusEl = document.getElementById("gcalStatus");
    if (!statusEl) return;
    try {
        const data = await api("/api/osc/gcal/status");
        const calInput = document.getElementById("gcalCalendarId");
        const importInput = document.getElementById("gcalImportCalendarIds");
        if (calInput && data?.calendar_id && !calInput.value) calInput.value = data.calendar_id;
        if (importInput && data?.import_calendar_ids && data.import_calendar_ids !== "全部可讀日曆" && !importInput.value) importInput.value = data.import_calendar_ids;
        if (data && data.connected) {
            statusEl.textContent = `✅ 已連線 Google Calendar（推送：${data.calendar_id || "primary"}｜匯入：${data.import_calendar_ids || "全部可讀日曆"}）${data.expires_at ? " | 到期：" + data.expires_at : ""}`;
        } else {
            statusEl.textContent = "⚪ 尚未授權 Google Calendar";
        }
    } catch (err) {
        statusEl.textContent = `⚠️ 無法取得 GCal 狀態：${err.message}`;
    }
}

async function saveGcalCreds() {
    const clientId = (document.getElementById("gcalClientId").value || "").trim();
    const clientSecret = (document.getElementById("gcalClientSecret").value || "").trim();
    const calendarId = (document.getElementById("gcalCalendarId").value || "").trim() || "primary";
    const importCalendarIds = (document.getElementById("gcalImportCalendarIds")?.value || "").trim();
    const statusEl = document.getElementById("gcalStatus");

    if (!clientId || !clientSecret) {
        if (statusEl) statusEl.textContent = "❌ Client ID 與 Client Secret 均為必填";
        return;
    }
    try {
        await api("/api/osc/settings", "POST", { key: "gcal_client_id", value: clientId, description: "Google Calendar OAuth client_id（P4）" });
        await api("/api/osc/settings", "POST", { key: "gcal_client_secret", value: clientSecret, description: "Google Calendar OAuth client_secret（P4）" });
        await api("/api/osc/settings", "POST", { key: "gcal_calendar_id", value: calendarId, description: "Google Calendar target calendar id（P4）" });
        await api("/api/osc/settings", "POST", { key: "gcal_import_calendar_ids", value: importCalendarIds, description: "Google Calendar import calendar IDs；空白代表全部可讀日曆" });
        showToast("GCal 憑證已儲存", "ok");
        if (statusEl) statusEl.textContent = "✅ 憑證已儲存，可點「連線授權」開始 OAuth 流程；若曾用舊版授權，請重新授權以允許讀取其他日曆。";
    } catch (err) {
        if (statusEl) statusEl.textContent = `❌ 儲存失敗：${err.message}`;
    }
}

async function connectGcal() {
    const statusEl = document.getElementById("gcalStatus");
    try {
        const res = await api("/api/osc/gcal/auth/start", "POST", {});
        if (res && res.auth_url) {
            window.open(res.auth_url, "_blank", "width=600,height=700");
            if (statusEl) statusEl.textContent = "🔗 已開啟授權視窗，完成後請重新整理狀態";
            // Poll status after 5s
            setTimeout(() => loadGcalStatus(), 5000);
        } else {
            if (statusEl) statusEl.textContent = `❌ 無法取得授權 URL：${res?.error || ""}`;
        }
    } catch (err) {
        if (statusEl) statusEl.textContent = `❌ 連線失敗：${err.message}`;
    }
}

async function syncGcal(dryRun) {
    const statusEl = document.getElementById("gcalStatus");
    try {
        const res = await api("/api/osc/gcal/sync", "POST", { dry_run: dryRun });
        if (res && res.ok) {
            const mode = dryRun ? "Dry-run" : "同步";
            const msg = `${mode} 完成 — 匯入 ${res.imported ?? 0} 筆，推送 ${res.pushed ?? 0} 筆，略過 ${res.skipped ?? 0} 筆${(res.errors && res.errors.length) || (res.import_errors && res.import_errors.length) ? `（${(res.errors || []).length + (res.import_errors || []).length} 錯誤）` : ""}`;
            if (statusEl) statusEl.textContent = (dryRun ? "🔍 " : "✅ ") + msg;
            showToast(msg, "ok");
        } else {
            if (statusEl) statusEl.textContent = `❌ 同步失敗：${res?.error || ""}`;
        }
    } catch (err) {
        if (statusEl) statusEl.textContent = `❌ 同步失敗：${err.message}`;
    }
}

async function disconnectGcal() {
    if (!confirm("確定解除 Google Calendar 授權？將刪除本機 token。")) return;
    const statusEl = document.getElementById("gcalStatus");
    try {
        await api("/api/osc/gcal/disconnect", "POST", {});
        showToast("Google Calendar 授權已解除", "ok");
        await loadGcalStatus();
    } catch (err) {
        if (statusEl) statusEl.textContent = `❌ 解除失敗：${err.message}`;
    }
}
