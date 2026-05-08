/* tabs/drafts.js – Draft generation system */
function renderDraftDocSelections() {
    const items = state.draft.selectedDocuments || [];
    document.getElementById("draftSelectedDocsCount").textContent = `已選 ${items.length} 份參考書狀`;
    const box = document.getElementById("draftSelectedDocs");
    if (!items.length) {
        box.innerHTML = `<div class="muted">尚未選取參考書狀</div>`;
        return;
    }
    box.innerHTML = items.map(r => `
        <div class="selection-item">
            <div class="meta-text">
                <div>${esc(r.file_name || r.file_path || "")}</div>
                <div class="muted">${esc(r.case_number || "")} ${esc(r.kind_label || "")}</div>
            </div>
            <button class="btn ghost" data-act="draft-doc-toggle" data-id="${esc(r.id)}">移除</button>
        </div>
    `).join("");
}

function renderDraftInsightSelections() {
    const items = state.draft.selectedInsights || [];
    document.getElementById("draftSelectedInsightsCount").textContent = `已選 ${items.length} 筆實務見解`;
    const box = document.getElementById("draftSelectedInsights");
    if (!items.length) {
        box.innerHTML = `<div class="muted">尚未選取實務見解</div>`;
        return;
    }
    box.innerHTML = items.map(r => `
        <div class="selection-item">
            <div class="meta-text">
                <div>${esc(r.title || "")}</div>

                <div class="muted">${esc(r.source || "")} ${esc(r.case_number || "")}</div>
            </div>
            <button class="btn ghost" data-act="draft-insight-toggle" data-id="${esc(r.id)}">移除</button>
        </div>
    `).join("");
}

function renderDraftCases() {
    const select = document.getElementById("draftCaseSelect");
    const items = state.draft.cases || [];
    if (!items.length) {
        select.innerHTML = `<option value="">查無案件</option>`;
        return;
    }
    select.innerHTML = [`<option value="">請選擇案件</option>`, ...items.map(r => {
        const label = [r.client_name, r.case_number, r.case_reason].filter(Boolean).join("｜");
        const selected = String(state.draft.selectedCaseId || "") === String(r.id) ? " selected" : "";
        return `<option value="${esc(r.id)}"${selected}>${esc(label)}</option>`;
    })].join("");
}

function renderDraftCases() {
    const select = document.getElementById("draftCaseSelect");
    const items = state.draft.cases || [];
    if (!items.length) {
        select.innerHTML = `<option value="">查無案件</option>`;
        return;
    }
    select.innerHTML = [`<option value="">請選擇案件</option>`, ...items.map(r => {
        const label = [r.client_name, r.case_number, r.case_reason].filter(Boolean).join("｜");
        const selected = String(state.draft.selectedCaseId || "") === String(r.id) ? " selected" : "";
        return `<option value="${esc(r.id)}"${selected}>${esc(label)}</option>`;
    })].join("");
}

async function loadDraftMeta() {
    await withBusy("draftMetaRefreshBtn", "讀取中...", async () => {
        const data = await api("/api/osc/drafts/meta");
        state.draft.meta = data.meta || {};
        const meta = state.draft.meta || {};
        const providerText = meta.provider && meta.effective_provider && meta.provider !== meta.effective_provider
            ? `${meta.provider} -> ${meta.effective_provider}`
            : (meta.effective_provider || meta.provider || "casper");
        document.getElementById("draftProviderBadge").textContent = `Provider: ${providerText}${meta.ollama_model ? ` / ${meta.ollama_model}` : ""}`;
        document.getElementById("draftTemplateBadge").textContent = `模板: ${meta.template_source === "custom" ? "自訂" : "預設"}${meta.enabled === false ? " / config disabled" : ""}`;
        const select = document.getElementById("draftDocType");
        const current = select.value;
        const docTypes = data.doc_types || [];
        select.innerHTML = [`<option value="">請選擇書狀類型</option>`, ...docTypes.map(v => `<option value="${esc(v)}">${esc(v)}</option>`)].join("");
        if (current && docTypes.includes(current)) select.value = current;
        setDraftStatus("已同步草擬設定。");
    });
}

async function searchDraftCases() {
    await withBusy("draftCaseSearchBtn", "搜尋中...", async () => {
        const q = encodeURIComponent((document.getElementById("draftCaseSearch").value || "").trim());
        const data = await api(`/api/osc/cases?limit=80&q=${q}`);
        state.draft.cases = data.items || [];
        renderDraftCases();
        setDraftStatus(`已載入 ${state.draft.cases.length} 筆案件候選。`);
    });
}

async function loadDraftSelectedCase() {
    await withBusy("draftCaseLoadBtn", "載入中...", async () => {
        const id = (document.getElementById("draftCaseSelect").value || "").trim();
        if (!id) return alert("請先選擇案件");
        const data = await api(`/api/osc/cases/${encodeURIComponent(id)}`);
        const x = data.item || {};
        state.draft.selectedCaseId = x.id || id;
        document.getElementById("draftCaseNumber").value = x.court_case_number || x.court_case_no || x.case_number || "";
        document.getElementById("draftDivision").value = x.court_division || "";
        document.getElementById("draftCourtName").value = x.court_name || "";
        document.getElementById("draftReason").value = x.case_reason || "";
        document.getElementById("draftPlaintiff").value = x.client_name || "";
        document.getElementById("draftDefendant").value = x.opponent_name || "";
        if (!(document.getElementById("draftFacts").value || "").trim()) {
            document.getElementById("draftFacts").value = x.description || x.notes || "";
        }
        if (!(document.getElementById("draftDocsCaseFilter").value || "").trim()) {
            document.getElementById("draftDocsCaseFilter").value = x.case_number || "";
        }
        if (!(document.getElementById("draftInsightsCaseFilter").value || "").trim()) {
            document.getElementById("draftInsightsCaseFilter").value = x.case_number || "";
        }
        if (!(document.getElementById("draftInsightsReasonFilter").value || "").trim()) {
            document.getElementById("draftInsightsReasonFilter").value = x.case_reason || "";
        }
        if (!(document.getElementById("draftSuggestedName").value || "").trim()) {
            const docType = (document.getElementById("draftDocType").value || "書狀草稿").trim();
            const shownCase = (x.court_case_number || x.court_case_no || x.case_number || "未命名").trim();
            document.getElementById("draftSuggestedName").value = `${docType}_${shownCase}`;
        }
        setDraftStatus(`已載入案件：${x.client_name || ""} / ${x.case_number || id}`);
        await Promise.all([loadDraftDocuments(), loadDraftInsights()]);
    });
}

function renderDraftDocuments() {
    const body = document.getElementById("draftDocsBody");
    const selectedIds = new Set((state.draft.selectedDocuments || []).map(x => String(x.id)));
    const items = state.draft.documents || [];
    if (!items.length) {
        body.innerHTML = `<tr><td colspan="5" class="muted">沒有檔案資料</td></tr>`;
        renderDraftDocSelections();
        return;
    }
    body.innerHTML = items.map(r => {
        const picked = selectedIds.has(String(r.id));
        return `
            <tr>
                <td><button class="btn ${picked ? "selected-toggle" : ""}" data-act="draft-doc-toggle" data-id="${esc(r.id)}">${picked ? "✓ 已選" : "加入"}</button></td>
                <td>${esc(r.case_number || "")}</td>
                <td>${esc(r.kind_label || "")}</td>
                <td>${esc(r.file_name || "")}</td>
                <td title="${esc(r.file_path || "")}">${esc(shortText(r.file_path || "", 90))}</td>
            </tr>
        `;
    }).join("");
    renderDraftDocSelections();
}

function renderDraftInsights() {
    const body = document.getElementById("draftInsightsBody");
    const selectedIds = new Set((state.draft.selectedInsights || []).map(x => String(x.id)));
    const items = state.draft.insights || [];
    if (!items.length) {
        body.innerHTML = `<tr><td colspan="5" class="muted">沒有見解資料</td></tr>`;
        renderDraftInsightSelections();
        return;
    }
    body.innerHTML = items.map(r => {
        const picked = selectedIds.has(String(r.id));
        return `
            <tr>
                <td><button class="btn ${picked ? "selected-toggle" : ""}" data-act="draft-insight-toggle" data-id="${esc(r.id)}">${picked ? "✓ 已選" : "加入"}</button></td>
                <td>${esc(r.source || "")}</td>
                <td>${esc(r.title || "")}</td>
                <td>${esc(r.case_number || "")}</td>
                <td title="${esc(r.summary || "")}">${esc(shortText(r.summary || "", 110))}</td>
            </tr>
        `;
    }).join("");
    renderDraftInsightSelections();
}

async function loadDraftDocuments() {
    await withBusy("draftDocsSearchBtn", "搜尋中...", async () => {
        const q = encodeURIComponent((document.getElementById("draftDocsQ").value || "").trim());
        const caseNumber = encodeURIComponent((document.getElementById("draftDocsCaseFilter").value || document.getElementById("draftInsightsCaseFilter").value || "").trim());
        const kind = encodeURIComponent((document.getElementById("draftDocsKind").value || "all").trim());
        const data = await api(`/api/osc/documents?limit=120&q=${q}&case_number=${caseNumber}&kind=${kind}`);
        state.draft.documents = data.items || [];
        renderDraftDocuments();
        setDraftStatus(`參考書狀搜尋完成，共 ${state.draft.documents.length} 筆。`);
    });
}

async function loadDraftInsights() {
    await withBusy("draftInsightsSearchBtn", "搜尋中...", async () => {
        const q = encodeURIComponent((document.getElementById("draftInsightsQ").value || "").trim());
        const caseNumber = encodeURIComponent((document.getElementById("draftInsightsCaseFilter").value || "").trim());
        const caseReason = encodeURIComponent((document.getElementById("draftInsightsReasonFilter").value || "").trim());
        const data = await api(`/api/osc/insights?limit=120&q=${q}&case_number=${caseNumber}&case_reason=${caseReason}`);
        state.draft.insights = filterDisplayableInsights(data.items || []);
        renderDraftInsights();
        setDraftStatus(`實務見解搜尋完成，共 ${state.draft.insights.length} 筆。`);
    });
}

function toggleDraftDocument(id) {
    const sid = String(id || "");
    const idx = (state.draft.selectedDocuments || []).findIndex(x => String(x.id) === sid);
    if (idx >= 0) {
        state.draft.selectedDocuments.splice(idx, 1);
    } else {
        const item = (state.draft.documents || []).find(x => String(x.id) === sid);
        if (item) state.draft.selectedDocuments.push({ ...item });
    }
    renderDraftDocuments();
}

function toggleDraftInsight(id) {
    const sid = String(id || "");
    const idx = (state.draft.selectedInsights || []).findIndex(x => String(x.id) === sid);
    if (idx >= 0) {
        state.draft.selectedInsights.splice(idx, 1);
    } else {
        const item = (state.draft.insights || []).find(x => String(x.id) === sid);
        if (item) state.draft.selectedInsights.push({ ...item });
    }
    renderDraftInsights();
}

function collectDraftPayload() {
    return {
        case_id: state.draft.selectedCaseId || (document.getElementById("draftCaseSelect").value || "").trim(),
        case_lookup_number: (document.getElementById("draftDocsCaseFilter").value || document.getElementById("draftInsightsCaseFilter").value || document.getElementById("draftCaseNumber").value || "").trim(),
        doc_type: (document.getElementById("draftDocType").value || "").trim(),
        case_number: (document.getElementById("draftCaseNumber").value || "").trim(),
        division: (document.getElementById("draftDivision").value || "").trim(),
        court_name: (document.getElementById("draftCourtName").value || "").trim(),
        reason: (document.getElementById("draftReason").value || "").trim(),
        plaintiff: (document.getElementById("draftPlaintiff").value || "").trim(),
        defendant: (document.getElementById("draftDefendant").value || "").trim(),
        case_facts: (document.getElementById("draftFacts").value || "").trim(),
        suggested_filename: (document.getElementById("draftSuggestedName").value || "").trim(),
        selected_documents: [...(state.draft.selectedDocuments || [])],
        selected_insights: [...(state.draft.selectedInsights || [])],
    };
}

async function previewDraftPrompt() {
    await withBusy("draftPreviewBtn", "預覽中...", async () => {
        const payload = collectDraftPayload();
        payload.dry_run = true;
        setDraftStatus("正在組合 Prompt...");
        const data = await api("/api/osc/drafts/generate", "POST", payload);
        const text = data.prompt_preview || "";
        document.getElementById("draftResult").value = text;
        state.draft.result = text;
        updateDraftCharCount();
        setDraftModeIndicator("preview");
        const warningCount = (data.warnings || []).length;
        setDraftStatus(`Prompt 預覽完成。檔案警告：${warningCount} 筆。`, warningCount ? "warn" : "info");
    });
}

async function generateDraft() {
    await withBusy("draftGenerateBtn", "產生中...", async () => {
        const payload = collectDraftPayload();
        setDraftStatus("正在呼叫 AI 產生書狀，請稍候...");
        const data = await api("/api/osc/drafts/generate", "POST", payload);
        const text = data.draft_text || "";
        document.getElementById("draftResult").value = text;
        state.draft.result = text;
        updateDraftCharCount();
        setDraftModeIndicator("generated");
        if (data.suggested_filename && !(document.getElementById("draftSuggestedName").value || "").trim()) {
            document.getElementById("draftSuggestedName").value = data.suggested_filename;
        }
        const degraded = text.includes("系統降級回覆") || text.includes("忙碌或逾時");
        setDraftStatus(`產生完成。Provider: ${data.provider || "-"}${data.model ? ` / ${data.model}` : ""}`, degraded ? "warn" : "info");
    });
}

async function copyDraftResult() {
    const text = (document.getElementById("draftResult").value || "").trim();
    if (!text) return alert("沒有可複製內容");
    try {
        await navigator.clipboard.writeText(text);
        setDraftStatus("已複製產生結果到剪貼簿。");
    } catch {
        alert("複製失敗，請手動複製");
    }
}

async function exportDraftResult() {
    await withBusy("draftExportBtn", "匯出中...", async () => {
        const draftText = (document.getElementById("draftResult").value || "").trim();
        if (!draftText) return alert("沒有內容可以匯出");
        const body = {
            draft_text: draftText,
            doc_type: (document.getElementById("draftDocType").value || "").trim(),
            case_number: (document.getElementById("draftDocsCaseFilter").value || document.getElementById("draftCaseNumber").value || "").trim(),
            suggested_filename: (document.getElementById("draftSuggestedName").value || "").trim(),
            title: (document.getElementById("draftDocType").value || "書狀草稿").trim(),
        };
        setDraftStatus("正在匯出 DOCX/PDF...");
        const data = await api("/api/osc/drafts/export", "POST", body);
        const urls = [data?.export_docx?.url, data?.export_pdf?.url].filter(Boolean);
        if (urls.length) urls.forEach(u => window.open(u, "_blank"));
        setDraftStatus(`匯出完成。狀態：${data.status || "success"}`, data.status === "partial_success" ? "warn" : "info");
        await loadAdminPdfLogs().catch(() => { });
    });
}

function clearDraftResult() {
    document.getElementById("draftResult").value = "";
    state.draft.result = "";
    setDraftStatus("已清除產生結果。");
    updateDraftCharCount();
    setDraftModeIndicator(null);
}

function updateDraftCharCount() {
    const el = document.getElementById("draftCharCount");
    if (!el) return;
    const text = (document.getElementById("draftResult").value || "");
    el.textContent = `${text.length} 字`;
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

async function autoDraftInsights() {
    await withBusy("draftInsightsAutoBtn", "帶入中...", async () => {
        if (!(document.getElementById("draftInsightsReasonFilter").value || "").trim()) {
            document.getElementById("draftInsightsReasonFilter").value = (document.getElementById("draftReason").value || "").trim();
        }
        if (!(document.getElementById("draftInsightsQ").value || "").trim()) {
            document.getElementById("draftInsightsQ").value = (document.getElementById("draftReason").value || "").trim();
        }
        await loadDraftInsights();
    });
}

async function loadDraftComposer() {
    try {
        await loadDraftMeta();
        if (!(state.draft.cases || []).length) {
            await searchDraftCases();
        } else {
            renderDraftCases();
        }
        renderDraftDocuments();
        renderDraftInsights();
        renderDraftDocSelections();
        renderDraftInsightSelections();
    } catch (e) {
        setDraftStatus(`草擬頁初始化失敗：${e.message}`, "warn");
    }
}

function reportDraftError(e) {
    const msg = e?.message ? String(e.message) : String(e || "unknown_error");
    setDraftStatus(`草擬流程失敗：${msg}`, "warn");
}
