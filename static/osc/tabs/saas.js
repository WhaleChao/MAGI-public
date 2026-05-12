/* tabs/saas.js - office operations workbench */
function saasSetStatus(text, tone = "info") {
    const el = document.getElementById("saasStatus");
    if (!el) return;
    el.textContent = text || "";
    el.className = `status-banner${tone === "warn" || tone === "error" ? " warn" : tone === "ok" || tone === "success" ? " ok" : ""}`;
}

function saasBadge(status) {
    const label = {
        enabled: "已開啟",
        packet_mode: "可複製文字",
        high_risk_only: "高風險紀錄",
        ready: "已就緒",
        guarded: "需確認",
        not_needed: "不啟用",
        needs_attention: "需處理",
    }[status] || status || "未設定";
    return `<span class="badge">${esc(label)}</span>`;
}

function buildSaasFallbackOverview(error) {
    const s = state.dashboard?.stats || {};
    return {
        ok: false,
        error: error?.message || "not_found",
        capabilities: [],
        readiness: {
            mode_label: "單主機 MAGI",
            status_page: {label: "NERV 上線狀態", url: "/dashboard/nerv"},
            summary: {ready: 0, guarded: 0, not_needed: 3, needs_attention: 0},
            checks: [
                {title: "NERV 上線狀態", status: "ready", detail: "請開啟 NERV 查看即時服務狀態。", actions: [{act: "open-url", url: "/dashboard/nerv", label: "開啟 NERV"}]},
                {title: "本版不啟用", status: "not_needed", detail: "多租戶、電子簽章、公開上傳入口暫不納入。", actions: []},
            ],
        },
        integration: {
            principle: "MAGI 暫時讀不到管理工具 API；下方先保留各功能入口，正式資料請以各頁籤為準。",
            items: [],
        },
        risk: {items: []},
        timeline: {items: []},
        operations: {
            total_cases: 0,
            active_cases: s.active_cases || 0,
            closed_cases: (Number(s.closed_regular || 0) + Number(s.closed_legal_aid || 0)),
            closing_pending_cases: 0,
            pending_todos: (state.dashboard?.pending_todos || []).length,
            overdue_todos: 0,
            documents: 0,
            legal_insights: 0,
            legal_aid_cases: s.legal_aid_cases || 0,
            automation: {learning_events: 0},
        },
        learning: {recent: []},
        intake: {recent: []},
        audit: {items: []},
        onboarding: {items: [], summary: {}},
        notification_preferences: {items: [], prefs: {}, policy: ""},
        workflow_templates: {templates: []},
        ai_governance: {policies: [], provenance_files: []},
        operations_text: "",
    };
}

async function loadSaasWorkbench(_options = {}) {
    const caseNumber = encodeURIComponent((document.getElementById("saasCaseNumber")?.value || "").trim());
    try {
        const data = await api(`/api/osc/saas/overview${caseNumber ? `?case_number=${caseNumber}` : ""}`);
        state.saas.overview = data;
        renderSaasWorkbench();
        saasSetStatus("管理工具已更新。", "ok");
    } catch (error) {
        console.warn("loadSaasWorkbench failed:", error);
        state.saas.overview = buildSaasFallbackOverview(error);
        renderSaasWorkbench();
        const reason = String(error?.message || "not_found").replace(/^not_found$/, "找不到管理工具 API，請重啟 MAGI 後端讓新版路由生效");
        saasSetStatus(`MAGI 無法更新管理工具：${reason}`, "warn");
    }
}

function renderSaasWorkbench() {
    const data = state.saas.overview || {};
    renderSaasReadiness(data.readiness || {});
    renderSaasCapabilities(data.capabilities || []);
    renderSaasIntegration(data.integration || {});
    renderSaasRisk(data.risk || {});
    renderSaasOps(data.operations || {}, data.audit || {});
    renderSaasTimeline(data.timeline || {});
    renderSaasLearning(data.learning || {});
    renderSaasIntakes(data.intake || {});
    renderSaasOnboarding(data.onboarding || {});
    renderSaasNotificationPrefs(data.notification_preferences || {});
    renderSaasWorkflowTemplates(data.workflow_templates || {});
    renderSaasGovernance(data.ai_governance || {}, data.operations_text || "");
}

function renderSaasReadiness(readiness) {
    const host = document.getElementById("saasReadinessGrid");
    if (!host) return;
    const summary = readiness.summary || {};
    const checks = readiness.checks || [];
    const statusPage = readiness.status_page || {};
    const headline = `
        <div class="stat-card">
            <div class="stat-label">開放前檢查</div>
            <div class="stat-value" style="font-size:16px;">${esc(readiness.mode_label || "單主機 MAGI")}</div>
            <div class="muted" style="margin-top:6px;">NERV 作為正式上線狀態頁；多租戶、電子簽章、公開上傳入口不啟用。</div>
            <div class="inline-actions" style="margin-top:8px;">
                <button class="btn slim" data-act="open-url" data-url="${esc(statusPage.url || "/dashboard/nerv")}">${esc(statusPage.label || "開啟 NERV")}</button>
                <button class="btn slim" data-act="open-url" data-url="${esc(statusPage.health_api || "/dashboard/nerv/api/health")}">健康 API</button>
            </div>
        </div>
        <div class="stat-card">
            <div class="stat-label">檢查結果</div>
            <div class="stat-value" style="font-size:16px;">就緒 ${Number(summary.ready || 0)} / 需確認 ${Number(summary.guarded || 0)}</div>
            <div class="muted" style="margin-top:6px;">不啟用 ${Number(summary.not_needed || 0)}；需處理 ${Number(summary.needs_attention || 0)}；近期高風險紀錄 ${Number(summary.high_risk_recent || 0)}</div>
        </div>`;
    host.innerHTML = headline + checks.map(x => `
        <div class="stat-card">
            <div class="stat-label">${esc(x.title || "")}</div>
            <div class="stat-value" style="font-size:16px;">${saasBadge(x.status)}</div>
            <div class="muted" style="margin-top:6px;">${esc(shortText(x.detail || "", 92))}</div>
            ${saasActionButtons(x.actions || [])}
        </div>
    `).join("");
}

function renderSaasCapabilities(items) {
    const host = document.getElementById("saasCapabilityGrid");
    if (!host) return;
    host.innerHTML = (items || []).map(x => `
        <div class="stat-card">
            <div class="stat-label">${esc(x.title)}</div>
            <div class="stat-value" style="font-size:16px;">${saasBadge(x.status)}</div>
            <div class="muted" style="margin-top:6px;">主體：${esc(x.owner || "既有模組")}</div>
            <div class="muted">${esc(shortText(x.role || "", 72))}</div>
            ${saasCapabilityButtons(x)}
        </div>
    `).join("");
}

function saasCapabilityButtons(item) {
    const actions = [];
    if (item?.primary_action) actions.push(item.primary_action);
    if (Array.isArray(item?.secondary_actions)) actions.push(...item.secondary_actions);
    if (!actions.length && item?.tab) actions.push({act: "tab-jump", tab: item.tab, label: "進入功能"});
    if (!actions.length) return "";
    return `<div class="inline-actions" style="margin-top:8px;">${actions.map(action => {
        const act = action.act || "tab-jump";
        const tab = action.tab || "";
        const section = action.section || action.target || "";
        const url = action.url || "";
        return `<button class="btn slim" data-${"act"}="${esc(act)}"${tab ? ` data-tab="${esc(tab)}"` : ""}${section ? ` data-section="${esc(section)}"` : ""}${url ? ` data-url="${esc(url)}"` : ""}>${esc(action.label || "查看")}</button>`;
    }).join("")}</div>`;
}

function renderSaasIntegration(integration) {
    const note = document.getElementById("saasIntegrationNote");
    if (note) note.textContent = integration.principle || "這裡集中顯示常用資訊；實際新增與修改仍在各對應頁籤完成。";
    const host = document.getElementById("saasIntegrationGrid");
    if (!host) return;
    const items = (integration.items || []).length ? integration.items : [
        {area: "案件資料", mode: "新增、查詢、開資料夾", source: "案件列表、當事人", target_tabs: [{tab: "cases", label: "案件列表"}, {tab: "clients", label: "當事人"}]},
        {area: "期限與待辦", mode: "待辦、日曆、風險提醒", source: "待辦事項、行事曆", target_tabs: [{tab: "todos", label: "待辦事項"}, {tab: "calendar", label: "行事曆"}]},
        {area: "法扶流程", mode: "派案、開辦、二階段、結案", source: "法扶管理", target_tab: "laf", target_label: "法扶管理"},
        {area: "文件與書狀", mode: "索引、草擬、人工修正學習", source: "書狀索引、AI 草擬", target_tabs: [{tab: "documents", label: "書狀索引"}, {tab: "drafts", label: "AI 草擬"}]},
    ];
    host.innerHTML = items.length ? items.map(x => `
        <div class="stat-card">
            <div class="stat-label">${esc(x.area || "")}</div>
            <div class="stat-value" style="font-size:15px;">${esc(x.mode || "")}</div>
            <div class="muted" style="margin-top:6px;">來源：${esc(x.source || "")}</div>
            ${saasIntegrationButtons(x)}
        </div>
    `).join("") : `<div class="muted">目前沒有資料來源設定。</div>`;
}

function saasIntegrationButtons(item) {
    const targets = Array.isArray(item?.target_tabs) && item.target_tabs.length
        ? item.target_tabs
        : item?.target_tab
            ? [{tab: item.target_tab, label: item.target_label || "前往處理"}]
            : [];
    if (!targets.length) return "";
    return `<div class="inline-actions" style="margin-top:8px;">${targets.map(x => `
        <button class="btn slim" data-${"act"}="${esc(x.act || "tab-jump")}" data-tab="${esc(x.tab)}" data-section="${esc(x.section || x.tab || "")}">${esc(x.label || "前往處理")}</button>
    `).join("")}</div>`;
}

function saasActionButtons(actions) {
    const items = (actions || []).filter(x => x && x.act);
    if (!items.length) return "";
    return `<div class="inline-actions">${items.map(x => {
        const attrs = [
            `data-${"act"}="${esc(x.act)}"`,
            x.id !== undefined ? `data-id="${esc(x.id)}"` : "",
            x.tab ? `data-tab="${esc(x.tab)}"` : "",
            x.case ? `data-case="${esc(x.case)}"` : "",
            x.path ? `data-path="${esc(x.path)}"` : "",
            x.keyword ? `data-keyword="${esc(x.keyword)}"` : "",
            x.module ? `data-module="${esc(x.module)}"` : "",
            x.url ? `data-url="${esc(x.url)}"` : "",
        ].filter(Boolean).join(" ");
        return `<button class="btn slim" ${attrs}>${esc(x.label || "開啟")}</button>`;
    }).join("")}</div>`;
}

function renderSaasOnboarding(onboarding) {
    const summaryEl = document.getElementById("saasOnboardingSummary");
    const list = document.getElementById("saasOnboardingList");
    if (!list) return;
    const summary = onboarding.summary || {};
    if (summaryEl) {
        const ready = summary.ready ? "已完成必要檢查" : "仍有必要檢查未完成";
        summaryEl.textContent = `${ready}：${Number(summary.required_done || 0)} / ${Number(summary.required || 0)}`;
        summaryEl.className = `status-banner${summary.ready ? " ok" : " warn"}`;
    }
    const items = onboarding.items || [];
    list.innerHTML = items.length ? items.map(x => `
        <label class="selection-item" style="cursor:pointer;">
            <input type="checkbox" data-saas-onboarding="${esc(x.key || "")}" ${x.done ? "checked" : ""}>
            <div class="meta-text">
                <div>${esc(x.title || "")}</div>
                <div class="muted">${esc(x.category || "")}${x.required ? "｜必要" : ""}${x.updated_at ? "｜" + esc(x.updated_at) : ""}</div>
            </div>
        </label>
    `).join("") : `<div class="muted">尚無導入檢查項目。</div>`;
}

function renderSaasNotificationPrefs(data) {
    const policy = document.getElementById("saasNotificationPolicy");
    const list = document.getElementById("saasNotificationList");
    if (!list) return;
    if (policy) policy.textContent = data.policy || "通知偏好載入完成。";
    const labels = {enabled: "啟用", system_only: "只送系統通知", business_only: "只送業務", silent: "不主動推播"};
    list.innerHTML = (data.items || []).map(x => `
        <div class="field">
            <label for="saasNotify_${esc(x.key)}">${esc(x.title || x.key || "")}</label>
            <select id="saasNotify_${esc(x.key)}" data-saas-notify="${esc(x.key)}">
                ${(x.options || []).map(opt => `<option value="${esc(opt)}" ${opt === x.value ? "selected" : ""}>${esc(labels[opt] || opt)}</option>`).join("")}
            </select>
        </div>
    `).join("") || `<div class="muted">尚無通知偏好。</div>`;
}

function renderSaasWorkflowTemplates(data) {
    const list = document.getElementById("saasWorkflowList");
    if (!list) return;
    const templates = data.templates || [];
    list.innerHTML = templates.length ? templates.map(t => `
        <div class="selection-item">
            <div class="meta-text">
                <div>${esc(t.title || "")}｜${esc(t.scope || "")}</div>
                <div class="muted">${(t.steps || []).map((s, i) => `${i + 1}. ${esc(s)}`).join("　")}</div>
                ${saasActionButtons(t.entry_actions || [])}
            </div>
        </div>
    `).join("") : `<div class="muted">尚無流程樣板。</div>`;
}

function renderSaasGovernance(governance, reportText) {
    const list = document.getElementById("saasGovernanceList");
    const text = document.getElementById("saasOpsReportText");
    if (text) text.value = reportText || "";
    if (!list) return;
    const policies = (governance.policies || []).map(x => `
        <div class="selection-item"><div class="meta-text"><div>${esc(x)}</div></div></div>
    `).join("");
    const files = (governance.provenance_files || []).map(x => `
        <div class="selection-item"><div class="meta-text"><div>${x.ready ? "已啟用" : "需檢查"}｜${esc(x.path || "")}</div></div></div>
    `).join("");
    list.innerHTML = policies + files || `<div class="muted">尚無 AI 來源治理資訊。</div>`;
}

function renderSaasRisk(risk) {
    const rows = (risk.items || []).map(x => `<tr>
        <td>${esc(x.owner || x.type || "")}</td>
        <td>${esc(x.severity || "")}</td>
        <td style="white-space:nowrap">${esc(x.date || "")}</td>
        <td style="white-space:nowrap">${esc(x.case_number || "")}</td>
        <td>${esc(shortText(x.title || "", 42))}</td>
        <td>${esc(shortText(x.reason || x.detail || "", 70))}</td>
        <td>${saasActionButtons(x.actions || [])}</td>
    </tr>`);
    renderSimpleRows("saasRiskBody", rows, 7, "目前沒有風險項目");
}

function renderSaasOps(ops, audit) {
    const host = document.getElementById("saasOpsGrid");
    if (host) {
        const items = [
            ["案件總數", ops.total_cases],
            ["進行中案件", ops.active_cases],
            ["已結案", ops.closed_cases],
            ["報結/送出中", ops.closing_pending_cases],
            ["待辦", ops.pending_todos],
            ["逾期待辦", ops.overdue_todos],
            ["文件索引", ops.documents],
            ["實務見解", ops.legal_insights],
            ["法扶案件", ops.legal_aid_cases],
            ["學習紀錄", ops.automation?.learning_events],
        ];
        host.innerHTML = items.map(([label, value]) => `
            <div class="stat-card">
                <div class="stat-label">${esc(label)}</div>
                <div class="stat-value">${Number(value || 0)}</div>
            </div>
        `).join("");
    }
    const auditHost = document.getElementById("saasAuditList");
    if (auditHost) {
        const rows = audit.items || [];
        auditHost.innerHTML = rows.length ? rows.map(x => `
            <div class="selection-item">
                <div class="meta-text">
                    <div>${esc(x.action || "")} ${esc(x.entity_type || "")}</div>
                    <div class="muted">${esc(x.timestamp || "")}｜${esc(shortText(x.details || x.entity_id || "", 90))}</div>
                </div>
            </div>
        `).join("") : `<div class="muted">完整開啟/瀏覽稽核預設關閉；目前只顯示高風險操作。</div>`;
    }
}

function renderSaasTimeline(timeline) {
    const rows = (timeline.items || []).map(x => `<tr>
        <td style="white-space:nowrap">${esc(x.date || "")}</td>
        <td style="white-space:nowrap">${esc(x.case_number || "")}</td>
        <td>${esc(x.kind || "")}</td>
        <td>${esc(shortText(x.title || "", 60))}</td>
        <td>${esc(shortText(x.evidence_hint || "", 60))}</td>
        <td>${saasActionButtons(x.actions || [])}</td>
    </tr>`);
    renderSimpleRows("saasTimelineBody", rows, 6, "尚無文件索引資料");
}

function renderSaasLearning(learning) {
    const host = document.getElementById("saasLearningList");
    if (!host) return;
    const items = learning.recent || [];
    host.innerHTML = items.length ? items.map(x => `
        <div class="selection-item">
            <div class="meta-text">
                <div>${esc(x.doc_type || "修正")}｜${esc(x.reason || "未指定案由")}｜${esc(x.case_number || "")}</div>
                <div class="muted">${esc(x.created_at || "")}｜${esc(shortText(x.note || "人工修正", 120))}</div>
            </div>
        </div>
    `).join("") : `<div class="muted">尚無人工修正紀錄。</div>`;
}

function renderSaasIntakes(intake) {
    const host = document.getElementById("saasIntakeResult");
    if (!host || state.saas.intake) return;
    const items = intake.recent || [];
    host.innerHTML = items.length ? items.map(x => `
        <div class="selection-item">
            <div class="meta-text">
                <div>${esc(x.client_name || "諮詢")}｜${esc(x.case_reason || "")}｜${esc(x.conflict_risk || "")}</div>
                <div class="muted">${esc(x.created_at || "")}｜${esc(shortText(x.summary || "", 90))}</div>
            </div>
        </div>
    `).join("") : `<div class="muted">尚無諮詢紀錄。</div>`;
}

async function runSaasConflictCheck() {
    const payload = {
        client_name: document.getElementById("saasConflictClient").value,
        opponent_name: document.getElementById("saasConflictOpponent").value,
        related_names: document.getElementById("saasConflictRelated").value,
        contact: document.getElementById("saasConflictContact").value,
    };
    const data = await api("/api/osc/saas/conflict-check", "POST", payload);
    state.saas.conflict = data;
    const host = document.getElementById("saasConflictResult");
    host.innerHTML = `
        <div class="selection-item"><div class="meta-text"><div>風險：${esc(data.risk)}</div><div class="muted">${esc(data.summary || "")}</div></div></div>
        ${(data.matches || []).slice(0, 12).map(x => `<div class="selection-item"><div class="meta-text"><div>${esc(x.term)}｜${esc(x.side)}｜${esc(x.case_number || x.client_name || x.opponent_name || "")}</div><div class="muted">${esc(x.case_reason || x.status || x.notes || "")}</div>${saasActionButtons(x.actions || [])}</div></div>`).join("")}
    `;
}

async function runSaasIntake() {
    const payload = {
        client_name: document.getElementById("saasIntakeClient").value,
        opponent_name: document.getElementById("saasIntakeOpponent").value,
        case_reason: document.getElementById("saasIntakeReason").value,
        contact: document.getElementById("saasIntakeContact").value,
        summary: document.getElementById("saasIntakeSummary").value,
    };
    const data = await api("/api/osc/saas/intake", "POST", payload);
    state.saas.intake = data;
    document.getElementById("saasIntakeResult").innerHTML = `
        <div class="selection-item"><div class="meta-text"><div>已建立：${esc(data.event?.id || "")}</div><div class="muted">衝突風險：${esc(data.conflict?.risk || "")}｜${esc(data.conflict?.summary || "")}</div></div></div>
    `;
    await loadSaasWorkbench();
}

async function runSaasQualityCheck() {
    const payload = {
        case_number: document.getElementById("saasQualityCase").value,
        reason: document.getElementById("saasQualityReason").value,
        text: document.getElementById("saasQualityText").value,
    };
    const data = await api("/api/osc/saas/quality-check", "POST", payload);
    state.saas.quality = data;
    document.getElementById("saasQualityResult").innerHTML = `
        <div class="selection-item"><div class="meta-text"><div>${data.pass ? "通過" : "需修正"}｜分數 ${Number(data.score || 0)}</div><div class="muted">字數 ${Number(data.stats?.chars || 0)}｜引用 ${Number(data.stats?.citations || 0)}</div></div></div>
        ${(data.issues || []).map(x => `<div class="selection-item"><div class="meta-text"><div>${esc(x.severity)}｜${esc(x.code)}</div><div class="muted">${esc(x.message)}</div></div></div>`).join("")}
    `;
}

async function runSaasClientPacket() {
    const payload = {
        case_number: document.getElementById("saasPacketCase").value,
        client_name: document.getElementById("saasPacketClient").value,
        reason: document.getElementById("saasPacketReason").value,
    };
    const data = await api("/api/osc/saas/client-packet", "POST", payload);
    state.saas.packet = data;
    document.getElementById("saasPacketText").value = data.copy_text || "";
}

async function copySaasPacket() {
    const text = document.getElementById("saasPacketText").value || "";
    if (!text.trim()) return showToast("沒有可複製的對外資料。", "warn");
    await navigator.clipboard.writeText(text);
    showToast("已複製對外資料。", "ok");
}

async function reloadSaasOnboarding() {
    const data = await api("/api/osc/saas/onboarding");
    const overview = state.saas.overview || {};
    overview.onboarding = data;
    state.saas.overview = overview;
    renderSaasOnboarding(data);
}

async function toggleSaasOnboarding(key, done) {
    const data = await api("/api/osc/saas/onboarding", "POST", {key, done});
    const overview = state.saas.overview || {};
    overview.onboarding = data;
    state.saas.overview = overview;
    renderSaasOnboarding(data);
}

async function saveSaasNotificationPrefs() {
    const prefs = {};
    document.querySelectorAll("[data-saas-notify]").forEach(el => {
        prefs[el.dataset.saasNotify] = el.value;
    });
    const data = await api("/api/osc/saas/notification-prefs", "POST", {prefs});
    const overview = state.saas.overview || {};
    overview.notification_preferences = data;
    state.saas.overview = overview;
    renderSaasNotificationPrefs(data);
    showToast("通知偏好已儲存。", "ok");
}

function downloadSaasDiagnosticPack() {
    window.open("/api/osc/saas/diagnostic-pack", "_blank", "noopener");
}

async function copySaasOpsReport() {
    const text = document.getElementById("saasOpsReportText")?.value || "";
    if (!text.trim()) return showToast("沒有可複製的統計文字。", "warn");
    await navigator.clipboard.writeText(text);
    showToast("已複製事務統計文字。", "ok");
}
