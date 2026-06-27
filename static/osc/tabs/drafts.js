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
        await loadDraftFeedback();
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
        if (!id) return showAlert("MAGI說", "請先選擇案件");
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

function draftReuseIsDocx(item) {
    const raw = String(item?.file_path || item?.file_name || "").trim().toLowerCase();
    return raw.endsWith(".docx");
}

function draftReuseFolderPath(path) {
    const raw = String(path || "").trim();
    if (!raw) return "";
    const idx = Math.max(raw.lastIndexOf("/"), raw.lastIndexOf("\\"));
    return idx > 0 ? raw.slice(0, idx) : raw;
}

function renderDraftReuseSelection() {
    const selected = state.draft.selectedReuseDocument || null;
    const label = document.getElementById("draftReuseSelectedLabel");
    const box = document.getElementById("draftReuseSelected");
    if (!label || !box) return;
    if (!selected) {
        label.textContent = "尚未選取來源書狀";
        box.innerHTML = `<div class="muted">請從我方歷次書狀 Word 索引選取一份 DOCX 書狀。</div>`;
        return;
    }
    label.textContent = "已選取來源書狀";
    box.innerHTML = `
        <div class="selection-item">
            <div class="meta-text">
                <div>${esc(selected.file_name || selected.file_path || "")}</div>
                <div class="muted">來源案號：${esc(selected.case_number || "-")}｜${esc(selected.kind_label || "書狀")}</div>
                <div class="muted">${esc(shortText(selected.file_path || "", 160))}</div>
            </div>
            <div class="inline-actions">
                <button class="btn ghost" data-act="draft-reuse-clear">移除</button>
                <button class="btn" data-act="doc-open" data-path="${esc(selected.file_path || "")}">開啟來源</button>
            </div>
        </div>
    `;
}

function renderDraftReuseResult() {
    const box = document.getElementById("draftReuseResult");
    if (!box) return;
    const result = state.draft.lastReuseResult || null;
    if (!result) {
        box.innerHTML = `<div class="muted">尚未產生沿用檔案。</div>`;
        return;
    }
    const outputPath = result.output_path || result.path || "";
    const folderPath = result.output_dir || draftReuseFolderPath(outputPath);
    const replacementItems = result.replacements || result.replacement_summary || [];
    const replacementText = Array.isArray(replacementItems)
        ? replacementItems
            .filter(x => Number(x.count || 0) > 0)
            .slice(0, 8)
            .map(x => `${esc(x.field || x.label || x.key || x.source || "欄位")} ${Number(x.count || 0)} 次`)
            .join("、")
        : "";
    const warnings = result.warnings || [];
    box.innerHTML = `
        <div class="selection-item">
            <div class="meta-text">
                <div>${esc(result.file_name || (outputPath ? outputPath.split(/[\\/]/).pop() : "沿用書狀"))}</div>
                <div class="muted">${esc(shortText(outputPath, 180))}</div>
                ${replacementText ? `<div class="muted">替換：${replacementText}</div>` : ""}
                ${warnings.length ? `<div class="status-banner warn" style="margin-top:6px;">${esc(warnings.join("；"))}</div>` : ""}
            </div>
            <div class="inline-actions">
                ${outputPath ? `<button class="btn" data-act="doc-open" data-path="${esc(outputPath)}">開啟新檔</button>` : ""}
                ${folderPath ? `<button class="btn" data-act="doc-open-folder" data-path="${esc(folderPath)}">開資料夾</button>` : ""}
                ${outputPath ? `<button class="btn ghost" data-act="doc-copy" data-path="${esc(outputPath)}">複製路徑</button>` : ""}
            </div>
        </div>
    `;
}

function renderDraftReuseDocuments() {
    const body = document.getElementById("draftReuseDocsBody");
    if (!body) return;
    const selectedId = String(state.draft.selectedReuseDocument?.id || "");
    const items = state.draft.reuseDocuments || [];
    if (!items.length) {
        body.innerHTML = `<tr><td colspan="5" class="muted">尚未搜尋，或沒有符合條件的書狀。</td></tr>`;
        renderDraftReuseSelection();
        renderDraftReuseResult();
        return;
    }
    body.innerHTML = items.map(r => {
        const picked = selectedId && selectedId === String(r.id);
        const isDocx = draftReuseIsDocx(r);
        const selectLabel = !isDocx ? "僅 DOCX" : (picked ? "✓ 已選" : "沿用");
        return `
            <tr>
                <td><button class="btn ${picked ? "selected-toggle" : ""}" data-act="draft-reuse-select" data-id="${esc(r.id)}" ${isDocx ? "" : "disabled"}>${selectLabel}</button></td>
                <td>${esc(r.case_number || "")}</td>
                <td>${esc(r.kind_label || "")}</td>
                <td>${esc(r.file_name || "")}</td>
                <td title="${esc(r.file_path || "")}">${esc(shortText(r.file_path || "", 120))}</td>
            </tr>
        `;
    }).join("");
    renderDraftReuseSelection();
    renderDraftReuseResult();
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

async function loadDraftReuseDocuments() {
    await withBusy("draftReuseSearchBtn", "搜尋中...", async () => {
        const q = encodeURIComponent((document.getElementById("draftReuseQ").value || "").trim());
        const caseNumber = encodeURIComponent((document.getElementById("draftReuseCaseFilter").value || "").trim());
        const kind = encodeURIComponent((document.getElementById("draftReuseKind").value || "own_pleading_word").trim());
        const data = await api(`/api/osc/documents?limit=200&q=${q}&case_number=${caseNumber}&kind=${kind}&reuse_scope=own_pleading_word`);
        state.draft.reuseDocuments = data.items || [];
        renderDraftReuseDocuments();
        setDraftStatus(`我方歷次書狀 Word 索引搜尋完成，共 ${state.draft.reuseDocuments.length} 筆。`);
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

function selectDraftReuseDocument(id) {
    const sid = String(id || "");
    const item = (state.draft.reuseDocuments || []).find(x => String(x.id) === sid);
    if (!item) return;
    if (!draftReuseIsDocx(item)) {
        showAlert("MAGI說", "沿用舊書狀目前只支援 DOCX 來源檔。");
        return;
    }
    state.draft.selectedReuseDocument = { ...item };
    renderDraftReuseDocuments();
    setDraftStatus(`已選取沿用來源：${item.file_name || item.file_path || ""}`);
}

function clearDraftReuseSelection() {
    state.draft.selectedReuseDocument = null;
    renderDraftReuseDocuments();
    setDraftStatus("已清除沿用來源。");
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

async function reuseDraftDocument() {
    await withBusy("draftReuseRunBtn", "另存中...", async () => {
        const source = state.draft.selectedReuseDocument || null;
        if (!source) return showAlert("MAGI說", "請先從我方歷次書狀 Word 索引選取一份 DOCX 書狀。");
        if (!draftReuseIsDocx(source)) return showAlert("MAGI說", "沿用舊書狀目前只支援 DOCX 來源檔。");
        const payload = collectDraftPayload();
        payload.source_document = source;
        payload.source_path = source.file_path || "";
        payload.source_document_id = source.id || "";
        payload.source_case_number = source.case_number || "";
        payload.suggested_filename = (document.getElementById("draftSuggestedName").value || "").trim()
            || `${(document.getElementById("draftDocType").value || "沿用書狀").trim()}_${(document.getElementById("draftCaseNumber").value || "未命名").trim()}`;
        setDraftStatus("正在沿用舊書狀並另存新檔...");
        const data = await api("/api/osc/drafts/reuse-document", "POST", payload);
        state.draft.lastReuseResult = data.result || data;
        renderDraftReuseResult();
        setDraftStatus(`沿用完成：${state.draft.lastReuseResult.file_name || "已產生新檔"}`);
    });
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
        state.draft.originalResult = "";
        state.draft.resultMode = "preview";
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
        state.draft.originalResult = text;
        state.draft.resultMode = "generated";
        state.draft.lastProvider = data.provider || "";
        state.draft.lastModel = data.model || "";
        updateDraftFeedbackPanel();
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
    if (!text) return showAlert("MAGI說", "沒有可複製內容");
    try {
        await navigator.clipboard.writeText(text);
        setDraftStatus("已複製產生結果到剪貼簿。");
    } catch {
        showAlert("MAGI說", "複製失敗，請手動複製");
    }
}

async function exportDraftResult() {
    await withBusy("draftExportBtn", "匯出中...", async () => {
        const draftText = (document.getElementById("draftResult").value || "").trim();
        if (!draftText) return showAlert("MAGI說", "沒有內容可以匯出");
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

async function loadDraftFeedback() {
    const panel = document.getElementById("draftFeedbackList");
    if (panel) panel.innerHTML = `<div class="muted">讀取修正紀錄...</div>`;
    try {
        const data = await api("/api/osc/drafts/feedback?limit=8");
        state.draft.feedback = data.items || [];
        state.draft.feedbackSummary = data.summary || {};
        updateDraftFeedbackPanel();
    } catch (e) {
        if (panel) panel.innerHTML = `<div class="muted">修正紀錄讀取失敗：${esc(e.message || e)}</div>`;
    }
}

function currentDraftCorrectionDelta() {
    const original = state.draft.originalResult || "";
    const corrected = document.getElementById("draftResult")?.value || "";
    return {
        original,
        corrected,
        changed: !!original && original.trim() !== corrected.trim(),
        charDelta: corrected.length - original.length,
    };
}

function renderDraftLessons(items) {
    if (!items || !items.length) return `<div class="muted">尚無修正紀錄；記錄後會自動回到下一次 Prompt。</div>`;
    return items.map(x => {
        const stats = x.stats || {};
        const lessons = (x.lessons || []).slice(0, 2).map(l => {
            const before = l.before ? `原：${esc(l.before)}` : "";
            const after = l.after ? `改：${esc(l.after)}` : "";
            return `<div class="muted">${before}${before && after ? " / " : ""}${after}</div>`;
        }).join("");
        return `
            <div class="selection-item">
                <div class="meta-text">
                    <div>${esc(x.doc_type || "書狀")} ${esc(x.case_number || "")}</div>
                    <div class="muted">${esc(x.note || "人工修正")}｜字數 ${Number(stats.original_chars || 0)} → ${Number(stats.corrected_chars || 0)}</div>
                    ${lessons}
                </div>
            </div>
        `;
    }).join("");
}

function updateDraftFeedbackPanel() {
    const delta = currentDraftCorrectionDelta();
    const deltaEl = document.getElementById("draftCorrectionDelta");
    if (deltaEl) {
        if (!state.draft.originalResult) {
            deltaEl.textContent = "尚未產生 AI 原稿。";
        } else if (!delta.changed) {
            deltaEl.textContent = "尚未偵測到修正。";
        } else {
            deltaEl.textContent = `已偵測到修正：字數差 ${delta.charDelta >= 0 ? "+" : ""}${delta.charDelta}`;
        }
    }
    const list = document.getElementById("draftFeedbackList");
    if (list) list.innerHTML = renderDraftLessons(state.draft.feedback || []);
    const summary = document.getElementById("draftFeedbackSummary");
    if (summary) {
        const s = state.draft.feedbackSummary || {};
        summary.textContent = `已累積 ${Number(s.count || 0)} 筆修正${s.latest_at ? `，最新 ${String(s.latest_at).slice(0, 10)}` : ""}`;
    }
}

async function submitDraftFeedback() {
    await withBusy("draftFeedbackSaveBtn", "記錄中...", async () => {
        const delta = currentDraftCorrectionDelta();
        if (!delta.original) return showAlert("MAGI說", "請先產生一次 AI 書狀，再修改結果。");
        if (!delta.changed) return showAlert("MAGI說", "尚未偵測到修正內容。");
        const payload = collectDraftPayload();
        payload.original_text = delta.original;
        payload.corrected_text = delta.corrected;
        payload.note = (document.getElementById("draftFeedbackNote").value || "").trim();
        payload.provider = state.draft.lastProvider || "";
        payload.model = state.draft.lastModel || "";
        const data = await api("/api/osc/drafts/feedback", "POST", payload);
        state.draft.originalResult = delta.corrected;
        document.getElementById("draftFeedbackNote").value = "";
        setDraftStatus(`已記錄修正：${(data.event?.lessons || []).length} 條差異會進入後續學習。`);
        await loadDraftFeedback();
    });
}

function clearDraftResult() {
    document.getElementById("draftResult").value = "";
    state.draft.result = "";
    state.draft.originalResult = "";
    state.draft.resultMode = "";
    setDraftStatus("已清除產生結果。");
    updateDraftCharCount();
    setDraftModeIndicator(null);
    updateDraftFeedbackPanel();
}

function updateDraftCharCount() {
    const el = document.getElementById("draftCharCount");
    if (!el) return;
    const text = (document.getElementById("draftResult").value || "");
    el.textContent = `${text.length} 字`;
    state.draft.result = text;
    updateDraftFeedbackPanel();
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
        renderDraftReuseDocuments();
        renderDraftInsights();
        renderDraftDocSelections();
        renderDraftInsightSelections();
        updateDraftFeedbackPanel();
    } catch (e) {
        setDraftStatus(`草擬頁初始化失敗：${e.message}`, "warn");
    }
}

function reportDraftError(e) {
    const msg = e?.message ? String(e.message) : String(e || "unknown_error");
    setDraftStatus(`草擬流程失敗：${msg}`, "warn");
}
