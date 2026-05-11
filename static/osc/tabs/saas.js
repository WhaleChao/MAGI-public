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
        packet_mode: "資料包模式",
        high_risk_only: "高風險紀錄",
    }[status] || status || "未設定";
    return `<span class="badge">${esc(label)}</span>`;
}

async function loadSaasWorkbench() {
    const caseNumber = encodeURIComponent((document.getElementById("saasCaseNumber")?.value || "").trim());
    const data = await api(`/api/osc/saas/overview${caseNumber ? `?case_number=${caseNumber}` : ""}`);
    state.saas.overview = data;
    renderSaasWorkbench();
    saasSetStatus("事務所營運工作台已更新。", "ok");
}

function renderSaasWorkbench() {
    const data = state.saas.overview || {};
    renderSaasCapabilities(data.capabilities || []);
    renderSaasRisk(data.risk || {});
    renderSaasOps(data.operations || {}, data.audit || {});
    renderSaasTimeline(data.timeline || {});
    renderSaasLearning(data.learning || {});
    renderSaasIntakes(data.intake || {});
}

function renderSaasCapabilities(items) {
    const host = document.getElementById("saasCapabilityGrid");
    if (!host) return;
    host.innerHTML = (items || []).map(x => `
        <div class="stat-card">
            <div class="stat-label">${esc(x.title)}</div>
            <div class="stat-value" style="font-size:16px;">${saasBadge(x.status)}</div>
        </div>
    `).join("");
}

function renderSaasRisk(risk) {
    const rows = (risk.items || []).map(x => `<tr>
        <td>${esc(x.severity || "")}</td>
        <td style="white-space:nowrap">${esc(x.date || "")}</td>
        <td style="white-space:nowrap">${esc(x.case_number || "")}</td>
        <td>${esc(shortText(x.title || "", 42))}</td>
        <td>${esc(shortText(x.reason || x.detail || "", 70))}</td>
    </tr>`);
    renderSimpleRows("saasRiskBody", rows, 5, "目前沒有風險項目");
}

function renderSaasOps(ops, audit) {
    const host = document.getElementById("saasOpsGrid");
    if (host) {
        const items = [
            ["進行中案件", ops.active_cases],
            ["已結案", ops.closed_cases],
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
    </tr>`);
    renderSimpleRows("saasTimelineBody", rows, 5, "尚無文件索引資料");
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
        ${(data.matches || []).slice(0, 12).map(x => `<div class="selection-item"><div class="meta-text"><div>${esc(x.term)}｜${esc(x.side)}｜${esc(x.case_number || x.client_name || x.opponent_name || "")}</div><div class="muted">${esc(x.case_reason || x.status || x.notes || "")}</div></div></div>`).join("")}
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
    if (!text.trim()) return showToast("沒有可複製的資料包。", "warn");
    await navigator.clipboard.writeText(text);
    showToast("已複製對外資料包。", "ok");
}
