/* tabs/hearing_conflict.js – confirmed-hearing conflicts and manual leave drafts */

function hearingConflictState() {
    if (!state.hearingConflict) {
        state.hearingConflict = { cases: [], items: [], upcoming: [], selectedCase: null, lastResult: null };
    }
    return state.hearingConflict;
}

function hearingConflictIsLegalAid(row) {
    const text = [
        row?.case_category, row?.case_type, row?.case_reason, row?.laf_case_no,
        row?.application_no, row?.legal_aid_status,
    ].map(v => String(v || "")).join(" ");
    return /法扶|法律扶助|\d{6,8}-[A-Z]-\d{3}/i.test(text);
}

function hearingConflictLocalValue(value) {
    const text = String(value || "").trim().replace(" ", "T");
    return text.length >= 16 ? text.slice(0, 16) : text;
}

function hearingConflictAddHour(value) {
    const d = new Date(String(value || ""));
    if (Number.isNaN(d.getTime())) return "";
    d.setHours(d.getHours() + 1);
    const pad = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function setHearingConflictStatus(message, tone = "") {
    const box = document.getElementById("hearingConflictStatus");
    if (!box) return;
    box.hidden = false;
    box.className = `status-banner${tone ? ` ${tone}` : ""}`;
    box.textContent = message;
}

function hearingConflictSelectedCase() {
    const value = String(document.getElementById("hearingConflictCase")?.value || "").trim();
    return hearingConflictState().cases.find(row => String(row.id || row.case_number || "") === value) || null;
}

function syncHearingConflictCase() {
    const row = hearingConflictSelectedCase();
    hearingConflictState().selectedCase = row;
    const set = (id, value) => { const el = document.getElementById(id); if (el) el.value = value || ""; };
    set("hearingConflictCaseNumber", row?.case_number || "");
    set("hearingConflictLawyer", row?.lawyer || "");
    set("hearingConflictAid", row ? (hearingConflictIsLegalAid(row) ? "扶助律師" : "委任律師") : "");
}

function hearingConflictCandidate() {
    const title = String(document.getElementById("hearingConflictTitle")?.value || "").trim();
    const start = String(document.getElementById("hearingConflictStart")?.value || "").trim();
    let end = String(document.getElementById("hearingConflictEnd")?.value || "").trim();
    const caseNumber = String(document.getElementById("hearingConflictCaseNumber")?.value || "").trim();
    if (!caseNumber) throw new Error("請先選擇案件");
    if (!title) throw new Error("請填行程標題");
    if (!start) throw new Error("請填新庭期開始時間");
    if (!end) {
        end = hearingConflictAddHour(start);
        const endEl = document.getElementById("hearingConflictEnd");
        if (endEl) endEl.value = end;
    }
    return {
        case_number: caseNumber,
        title,
        start_date: start,
        end_date: end,
        hearing_type: String(document.getElementById("hearingConflictTargetLabel")?.value || "開庭").trim(),
        source_kind: "osc_hearing_conflict_ui",
    };
}

async function loadHearingConflictWorkbench() {
    setHearingConflictStatus("正在載入案件與現有排程。", "");
    const [casesData, conflictData] = await Promise.all([
        api("/api/osc/cases?limit=500&status_scope=open"),
        api("/api/osc/hearing-conflicts"),
    ]);
    const hc = hearingConflictState();
    hc.cases = (casesData.items || []).filter(row => !row.is_template_case);
    hc.upcoming = conflictData.items || [];
    const select = document.getElementById("hearingConflictCase");
    if (select) {
        const current = String(select.value || "");
        select.innerHTML = `<option value="">請選擇案件</option>` + hc.cases.map(row => (
            `<option value="${esc(row.id || row.case_number || "")}">${esc(row.case_number || "")}｜${esc(row.client_name || "")}｜${esc(row.court_case_no || row.court_case_number || "")}</option>`
        )).join("");
        if ([...select.options].some(option => option.value === current)) select.value = current;
    }
    syncHearingConflictCase();
    renderHearingConflictUpcoming();
    setHearingConflictStatus(
        `已掃描 ${Number(conflictData.candidate_count || 0)} 筆已確認庭期，發現 ${Number(conflictData.conflict_count || 0)} 組衝突；「待確認」已排除。`,
        Number(conflictData.conflict_count || 0) ? "warn" : "ok",
    );
}

function renderHearingConflictUpcoming() {
    const box = document.getElementById("hearingConflictUpcoming");
    if (!box) return;
    const items = hearingConflictState().upcoming || [];
    if (!items.length) {
        box.innerHTML = `<div class="muted">目前掃描範圍沒有已成立的衝庭。</div>`;
        return;
    }
    box.innerHTML = items.slice(0, 100).map(item => {
        const candidate = item.candidate || {};
        const existing = item.existing || {};
        const action = item.action === "generate_leave_request" ? "應產生請假狀" : "僅通知 DC／TG";
        return `<div class="soft-block">
            <strong>${esc(action)}</strong>
            <div>新庭期：${esc(candidate.start || "")}｜${esc(candidate.title || "")}｜${esc(candidate.case_number || "")}</div>
            <div>既有行程：${esc(existing.start || "")}｜${esc(existing.title || "")}｜${esc(existing.case_number || "")}</div>
        </div>`;
    }).join("");
}

function renderHearingConflictResults() {
    const box = document.getElementById("hearingConflictResults");
    if (!box) return;
    const items = hearingConflictState().items || [];
    if (!items.length) {
        box.innerHTML = `<div class="muted">沒有可成立的衝突。若仍需聲請改期，可使用「人工產生請假狀」。</div>`;
        return;
    }
    box.innerHTML = items.map((item, index) => {
        const existing = item.existing || {};
        const canGenerate = item.action === "generate_leave_request";
        return `<div class="soft-block">
            <strong>${canGenerate ? "較早行程是開庭：可產生請假狀" : "較早行程不是開庭：僅通知 DC／TG"}</strong>
            <div>${esc(existing.start || "")} 至 ${esc(existing.end || "")}｜${esc(existing.title || "")}｜${esc(existing.case_number || "")}</div>
            <div class="muted">${esc(item.reason || "")}</div>
            ${canGenerate ? `<button class="btn primary" type="button" data-act="hearing-conflict-generate" data-index="${index}">產生 Word 草稿</button>` : ""}
        </div>`;
    }).join("");
}

async function checkHearingConflict() {
    const candidate = hearingConflictCandidate();
    const result = await api("/api/osc/hearing-conflicts/check", "POST", { candidate });
    hearingConflictState().items = result.items || [];
    hearingConflictState().lastResult = result;
    renderHearingConflictResults();
    if (result.excluded) {
        setHearingConflictStatus("這筆行程未被視為已確認開庭，因此不參與自動衝庭；仍可使用人工產生功能。", "warn");
    } else {
        setHearingConflictStatus(
            result.conflict_count ? `發現 ${result.conflict_count} 組衝突。` : "沒有發現較早排定且時間重疊的行程。",
            result.conflict_count ? "warn" : "ok",
        );
    }
    return result;
}

function hearingConflictGenerationCommon() {
    const row = hearingConflictSelectedCase();
    if (!row) throw new Error("請先選擇案件");
    return {
        case_id: row.id || "",
        case_number: row.case_number || "",
        lawyer_name: String(document.getElementById("hearingConflictLawyer")?.value || "").trim(),
        party_role: String(document.getElementById("hearingConflictPartyRole")?.value || "當事人").trim(),
        target_hearing_label: String(document.getElementById("hearingConflictTargetLabel")?.value || "開庭").trim(),
    };
}

function showHearingConflictGenerated(result) {
    hearingConflictState().lastResult = result;
    const link = document.getElementById("hearingConflictDownload");
    if (link) {
        link.hidden = !result.download_url;
        link.href = result.download_url || "#";
        link.textContent = `下載 ${result.file_name || "Word 草稿"}`;
    }
    setHearingConflictStatus(
        `${result.created ? "已產生" : "已找到既有"} Word 草稿：${result.file_name || ""}。送出法院前請人工核對並補齊附件。`,
        "ok",
    );
    showToast("請假狀 Word 草稿已準備完成。", "ok");
}

async function generateHearingConflictAutomatic(index) {
    const item = hearingConflictState().items[Number(index)];
    if (!item || item.action !== "generate_leave_request") throw new Error("找不到可產生的開庭衝突");
    const body = {
        ...hearingConflictGenerationCommon(),
        mode: "automatic_conflict",
        candidate: item.candidate,
        existing: item.existing,
    };
    const result = await api("/api/osc/hearing-conflicts/generate", "POST", body);
    showHearingConflictGenerated(result);
    return result;
}

async function generateHearingConflictManual() {
    const targetStart = String(document.getElementById("hearingConflictStart")?.value || "").trim();
    const priorStart = String(document.getElementById("hearingConflictPriorStart")?.value || "").trim();
    const priorCourt = String(document.getElementById("hearingConflictPriorCourt")?.value || "").trim();
    if (!targetStart) throw new Error("請填本案欲改定的庭期時間");
    if (!priorStart) throw new Error("請填既有庭期時間");
    if (!priorCourt) throw new Error("請填既有庭期法院");
    const body = {
        ...hearingConflictGenerationCommon(),
        mode: "manual",
        target_start: targetStart,
        prior_start: priorStart,
        prior_court_name: priorCourt,
        prior_court_case_no: String(document.getElementById("hearingConflictPriorCaseNo")?.value || "").trim(),
        prior_hearing_label: String(document.getElementById("hearingConflictPriorLabel")?.value || "開庭").trim(),
        conflict_statement: String(document.getElementById("hearingConflictManualReason")?.value || "").trim(),
    };
    const result = await api("/api/osc/hearing-conflicts/generate", "POST", body);
    showHearingConflictGenerated(result);
    return result;
}

function initHearingConflictControls() {
    const select = document.getElementById("hearingConflictCase");
    if (select && !select.dataset.bound) {
        select.dataset.bound = "1";
        select.addEventListener("change", syncHearingConflictCase);
    }
    const start = document.getElementById("hearingConflictStart");
    if (start && !start.dataset.bound) {
        start.dataset.bound = "1";
        start.addEventListener("change", () => {
            const end = document.getElementById("hearingConflictEnd");
            if (end && !end.value) end.value = hearingConflictAddHour(start.value);
        });
    }
}
