/* tabs/documents.js – Document index + templates + keywords + forms + wizards */
async function loadLaf() {
    const q = encodeURIComponent((document.getElementById("lafQ").value || "").trim());
    const caseNumber = encodeURIComponent((document.getElementById("lafCaseNumber").value || "").trim());
    const data = await api(`/api/osc/laf?limit=500&q=${q}&case_number=${caseNumber}`);
    const items = data.items || {};
    state.laf = {
        checklist: items.checklist || [],
        lifecycle: items.lifecycle || [],
        emails: items.emails || [],
    };

    document.getElementById("lafChecklistCount").textContent = String((data.counts || {}).checklist || 0);
    document.getElementById("lafLifecycleCount").textContent = String((data.counts || {}).lifecycle || 0);
    document.getElementById("lafEmailCount").textContent = String((data.counts || {}).emails || 0);

    const checklistBody = document.getElementById("lafChecklistBody");
    if (!state.laf.checklist.length) {
        checklistBody.innerHTML = `<tr><td colspan="6" class="muted">沒有法扶補件資料</td></tr>`;
    } else {
        checklistBody.innerHTML = state.laf.checklist.map(r => `
        <tr>
            <td>${esc(r.last_updated)}</td>
            <td>${esc(r.case_number)}</td>
            <td>${esc(r.item_key)}</td>
            <td>${esc(r.item_label)}</td>
            <td>${esc(r.status)}</td>
            <td>${esc(r.notes || "")}</td>
        </tr>
    `).join("");
    }

    const lifecycleBody = document.getElementById("lafLifecycleBody");
    if (!state.laf.lifecycle.length) {
        lifecycleBody.innerHTML = `<tr><td colspan="6" class="muted">沒有法扶流程紀錄</td></tr>`;
    } else {
        lifecycleBody.innerHTML = state.laf.lifecycle.map(r => `
        <tr>
            <td>${esc(r.created_at)}</td>
            <td>${esc(r.case_number)}</td>
            <td>${esc(r.event_type)}</td>
            <td>${esc(r.status)}</td>
            <td>${esc(r.completed_at || "")}</td>
            <td>${esc(r.event_data || "")}</td>
        </tr>
    `).join("");
    }

    const emailBody = document.getElementById("lafEmailBody");
    if (!state.laf.emails.length) {
        emailBody.innerHTML = `<tr><td colspan="6" class="muted">沒有法扶信件紀錄</td></tr>`;
    } else {
        emailBody.innerHTML = state.laf.emails.map(r => `
        <tr>
            <td>${esc(r.received_at)}</td>
            <td>${esc(r.case_number || "")}</td>
            <td>${esc(r.status || "")}</td>
            <td>${esc(r.sender || "")}</td>
            <td title="${esc(r.subject || "")}">${esc(r.subject || "")}</td>
            <td>${esc(r.error_message || "")}</td>
        </tr>
    `).join("");
    }
}

async function loadDocuments() {
    const q = encodeURIComponent((document.getElementById("docsQ").value || "").trim());
    const caseNumber = encodeURIComponent((document.getElementById("docsCaseNumber").value || "").trim());
    const kind = encodeURIComponent((document.getElementById("docsKind").value || "all").trim());
    const data = await api(`/api/osc/documents?limit=400&q=${q}&case_number=${caseNumber}&kind=${kind}`);
    state.documents = data.items || [];
    const body = document.getElementById("docsBody");
    if (!state.documents.length) {
        body.innerHTML = `<tr><td colspan="7" class="muted">沒有文件資料</td></tr>`;
        return;
    }
    body.innerHTML = state.documents.map(r => {
        const fp = r.file_path || "";
        const ext = (fp.split(".").pop() || "").toLowerCase();
        const stampable = ["pdf", "docx", "doc"].includes(ext);
        const stampBtn = stampable
            ? `<button class="btn" data-act="doc-stamp" data-path="${esc(fp)}" title="蓋章製作正本/副本/繕本">📋 蓋章</button>`
            : "";
        return `
    <tr>
        <td>${esc(r.timestamp)}</td>
        <td>${esc(r.source)}</td>
        <td>${esc(r.case_number)}</td>
        <td>${esc(r.kind_label || "")}</td>
        <td>${esc(r.file_name)}</td>
        <td title="${esc(fp)}">${esc(fp)}</td>
        <td class="actions">
            <button class="btn" data-act="doc-open" data-path="${esc(fp)}">開啟</button>
            <button class="btn" data-act="doc-copy" data-path="${esc(fp)}">複製路徑</button>
            ${stampBtn}
        </td>
    </tr>`;
    }).join("");
}

// ── 蓋章製作（呼叫後端 doc-producer skill）──
async function stampDocument(path) {
    if (!path) return;
    const ext = (path.split(".").pop() || "").toLowerCase();
    if (!["pdf", "docx", "doc"].includes(ext)) {
        showToast("僅支援 PDF / DOCX 蓋章", "warn");
        return;
    }

    const copyType = (prompt(
        "請選擇蓋章類型（直接按確定預設「正本」）：\n\n  正本 / 副本 / 繕本",
        "正本"
    ) || "").trim();
    if (!copyType) return;
    if (!["正本", "副本", "繕本"].includes(copyType)) {
        showToast("無效的蓋章類型，僅可填正本/副本/繕本", "warn");
        return;
    }

    let addPoa = false;
    let addSent = false;
    if (copyType === "正本") {
        addPoa = confirm("正本是否加註「附委任狀」？");
        addSent = confirm("正本是否加註「繕本已送對造」？");
    }

    const fileLabel = path.split(/[\\/]/).pop() || path;
    showToast(`蓋章中：${fileLabel} → ${copyType}${addPoa ? "（附委任狀）" : ""}${addSent ? "（繕本已送對造）" : ""}`, "info", 2400);

    try {
        const result = await api("/api/osc/documents/stamp", "POST", {
            file_path: path,
            copy_type: copyType,
            add_poa: addPoa,
            add_sent_to_opponent: addSent,
        });
        if (result && result.ok) {
            const out = result.output_path || "（無輸出路徑）";
            showToast(`✅ ${copyType}已產出：${out.split(/[\\/]/).pop()}`, "ok", 4000);
            // 自動把產出路徑放剪貼簿
            try { await copyText(out, "已複製產出檔路徑到剪貼簿。"); } catch (_) {}
        } else {
            showToast(`蓋章失敗：${result?.error || "未知錯誤"}`, "warn", 4000);
        }
    } catch (err) {
        showToast(`蓋章失敗：${err.message || err}`, "warn", 4000);
    }
}

async function loadDocumentTemplates() {
    const q = encodeURIComponent((document.getElementById("docTplQ").value || "").trim());
    const caseNumber = encodeURIComponent((document.getElementById("docTplCaseNumber").value || "").trim());
    const docType = encodeURIComponent((document.getElementById("docTplTypeFilter").value || "").trim());
    const data = await api(`/api/osc/document-templates?limit=400&q=${q}&case_number=${caseNumber}&doc_type=${docType}`);
    state.docTemplates = data.items || [];
    const body = document.getElementById("docTplBody");
    if (!state.docTemplates.length) {
        body.innerHTML = `<tr><td colspan="8" class="muted">沒有模板資料</td></tr>`;
        return;
    }
    body.innerHTML = state.docTemplates.map(r => `
    <tr>
        <td>${esc(r.id)}</td>
        <td>${esc(r.doc_type)}</td>
        <td>${esc(r.party_name)}</td>
        <td>${esc(r.case_number)}</td>
        <td>${esc(r.division)}</td>
        <td>${esc(r.use_count)}</td>
        <td>${esc(r.last_used || r.created_date || "")}</td>
        <td class="actions">
            <button class="btn" data-act="doc-tpl-edit" data-id="${Number(r.id)}">編輯</button>
            <button class="btn danger" data-act="doc-tpl-del" data-id="${Number(r.id)}">刪除</button>
        </td>
    </tr>
`).join("");
}

async function editDocumentTemplate(id) {
    const data = await api(`/api/osc/document-templates/${Number(id)}`);
    const x = data.item || {};
    document.getElementById("docTplId").value = x.id || "";
    document.getElementById("docTplType").value = x.doc_type || "";
    document.getElementById("docTplParty").value = x.party_name || "";
    document.getElementById("docTplCase").value = x.case_number || "";
    document.getElementById("docTplDivision").value = x.division || "";
    document.getElementById("docTplUseCount").value = x.use_count || 0;
    document.getElementById("docTplData").value = x.template_data || "";
}

async function saveDocumentTemplate() {
    const body = {
        id: (document.getElementById("docTplId").value || "").trim(),
        doc_type: (document.getElementById("docTplType").value || "").trim(),
        party_name: (document.getElementById("docTplParty").value || "").trim(),
        case_number: (document.getElementById("docTplCase").value || "").trim(),
        division: (document.getElementById("docTplDivision").value || "").trim(),
        use_count: (document.getElementById("docTplUseCount").value || "0").trim(),
        template_data: (document.getElementById("docTplData").value || "").trim(),
    };
    if (body.id) await api(`/api/osc/document-templates/${Number(body.id)}`, "PUT", body);
    else await api(`/api/osc/document-templates`, "POST", body);
    ["docTplId", "docTplType", "docTplParty", "docTplCase", "docTplDivision", "docTplUseCount", "docTplData"].forEach(id => {
        const el = document.getElementById(id); if (el) el.value = "";
    });
    await loadDocumentTemplates();
    await loadMeta();
}

async function delDocumentTemplate(id) {
    if (!confirm(`確定刪除書狀模板 ${id}？`)) return;
    await api(`/api/osc/document-templates/${Number(id)}`, "DELETE");
    await loadDocumentTemplates();
    await loadMeta();
}

async function loadDocumentKeywords() {
    const q = encodeURIComponent((document.getElementById("docKwQ").value || "").trim());
    const caseNumber = encodeURIComponent((document.getElementById("docKwCaseNumber").value || "").trim());
    const category = encodeURIComponent((document.getElementById("docKwCategoryFilter").value || "").trim());
    const data = await api(`/api/osc/document-keywords?limit=600&q=${q}&case_number=${caseNumber}&category=${category}`);
    state.docKeywords = data.items || [];
    const body = document.getElementById("docKwBody");
    if (!state.docKeywords.length) {
        body.innerHTML = `<tr><td colspan="9" class="muted">沒有關鍵字資料</td></tr>`;
        return;
    }
    body.innerHTML = state.docKeywords.map(r => `
    <tr>
        <td>${esc(r.id)}</td>
        <td>${esc(r.case_number)}</td>
        <td>${esc(r.keyword_name)}</td>
        <td>${esc(r.category)}</td>
        <td>${esc(r.hotkey)}</td>
        <td>${esc(r.is_case_specific)}</td>
        <td>${esc(r.usage_count)}</td>
        <td title="${esc(r.keyword_content || "")}">${esc(r.keyword_content || "")}</td>
        <td class="actions">
            <button class="btn" data-act="doc-kw-edit" data-id="${Number(r.id)}">編輯</button>
            <button class="btn danger" data-act="doc-kw-del" data-id="${Number(r.id)}">刪除</button>
        </td>
    </tr>
`).join("");
}

async function editDocumentKeyword(id) {
    const data = await api(`/api/osc/document-keywords/${Number(id)}`);
    const x = data.item || {};
    document.getElementById("docKwId").value = x.id || "";
    document.getElementById("docKwCase").value = x.case_number || "";
    document.getElementById("docKwName").value = x.keyword_name || "";
    document.getElementById("docKwCategory").value = x.category || "";
    document.getElementById("docKwHotkey").value = x.hotkey || "";
    document.getElementById("docKwCaseSpecific").value = x.is_case_specific ?? 0;
    document.getElementById("docKwUsageCount").value = x.usage_count ?? 0;
    document.getElementById("docKwContent").value = x.keyword_content || "";
}

async function saveDocumentKeyword() {
    const body = {
        id: (document.getElementById("docKwId").value || "").trim(),
        case_number: (document.getElementById("docKwCase").value || "").trim(),
        keyword_name: (document.getElementById("docKwName").value || "").trim(),
        category: (document.getElementById("docKwCategory").value || "").trim(),
        hotkey: (document.getElementById("docKwHotkey").value || "").trim(),
        is_case_specific: (document.getElementById("docKwCaseSpecific").value || "0").trim(),
        usage_count: (document.getElementById("docKwUsageCount").value || "0").trim(),
        keyword_content: (document.getElementById("docKwContent").value || "").trim(),
    };
    if (!body.keyword_name) return alert("請先輸入 keyword_name");
    if (body.id) await api(`/api/osc/document-keywords/${Number(body.id)}`, "PUT", body);
    else await api(`/api/osc/document-keywords`, "POST", body);
    ["docKwId", "docKwCase", "docKwName", "docKwCategory", "docKwHotkey", "docKwCaseSpecific", "docKwUsageCount", "docKwContent"].forEach(id => {
        const el = document.getElementById(id); if (el) el.value = "";
    });
    await loadDocumentKeywords();
    await loadMeta();
}

async function delDocumentKeyword(id) {
    if (!confirm(`確定刪除關鍵字 ${id}？`)) return;
    await api(`/api/osc/document-keywords/${Number(id)}`, "DELETE");
    await loadDocumentKeywords();
    await loadMeta();
}

async function loadDocumentReplacements() {
    const q = encodeURIComponent((document.getElementById("docRpQ").value || "").trim());
    const caseNumber = encodeURIComponent((document.getElementById("docRpCaseNumber").value || "").trim());
    const data = await api(`/api/osc/document-replacements?limit=400&q=${q}&case_number=${caseNumber}`);
    state.docReplacements = data.items || [];
    const body = document.getElementById("docRpBody");
    if (!state.docReplacements.length) {
        body.innerHTML = `<tr><td colspan="8" class="muted">沒有替換紀錄</td></tr>`;
        return;
    }
    body.innerHTML = state.docReplacements.map(r => `
    <tr>
        <td>${esc(r.replaced_date || "")}</td>
        <td>${esc(r.template_file || "")}</td>
        <td>${esc(r.new_case_number || "")}</td>
        <td>${esc(r.old_client_name || "")}</td>
        <td>${esc(r.new_client_name || "")}</td>
        <td>${esc(r.old_data || "")}</td>
        <td>${esc(r.new_data || "")}</td>
        <td class="actions">
            <button class="btn danger" data-act="doc-rp-del" data-id="${Number(r.id)}">刪除</button>
        </td>
    </tr>
`).join("");
}

async function delDocumentReplacement(id) {
    if (!confirm(`確定刪除替換紀錄 ${id}？`)) return;
    await api(`/api/osc/document-replacements/${Number(id)}`, "DELETE");
    await loadDocumentReplacements();
    await loadMeta();
}

async function openDocumentPath(path) {
    if (!isLocalConsole()) {
        window.open(fileContentUrl(path, true), "_blank", "noopener,noreferrer");
        return;
    }
    const data = await api("/api/osc/documents/open", "POST", { path });
    const result = data.open_result || {};
    if (result.ok) return;
    const smb = (data.smb_candidates || [])[0] || "";
    if (smb) window.open(smb, "_blank");
    alert(`無法直接開啟，請手動使用路徑：\n${path}`);
}

async function copyDocumentPath(path) {
    const text = String(path || "").trim();
    if (!text) return;
    try {
        await navigator.clipboard.writeText(text);
        showToast("檔案路徑已複製。", "ok");
    } catch {
        alert("複製失敗，請手動複製");
    }
}

async function runDocCaseAction(action) {
    const caseId = (document.getElementById("docActionCaseId").value || "").trim();
    if (!caseId) {
        alert("請先輸入案件 ID");
        return;
    }
    const data = await api(`/api/osc/cases/${encodeURIComponent(caseId)}/quick-action`, "POST", { action });
    alert(data.reply || "已完成");
}

function collectFormPayload() {
    return {
        form_type: (document.getElementById("formType").value || "").trim(),
        case_id: (document.getElementById("formCaseId").value || "").trim(),
        case_number: (document.getElementById("formCaseNumber").value || "").trim(),
        client_name: (document.getElementById("formClientName").value || "").trim(),
        fields: {
            date: (document.getElementById("formDate").value || "").trim(),
            receipt_no: (document.getElementById("formReceiptNo").value || "").trim(),
            amount: (document.getElementById("formAmount").value || "").trim(),
            item: (document.getElementById("formItem").value || "").trim(),
            payment_method: (document.getElementById("formPaymentMethod").value || "").trim(),
            lawyer_name: (document.getElementById("formLawyerName").value || "").trim(),
            court_case_no: (document.getElementById("formCourtCaseNo").value || "").trim(),
            laf_case_no: (document.getElementById("formLafCaseNo").value || "").trim(),
            sender_name: (document.getElementById("formSenderName")?.value || "").trim(),
            sender_addr: (document.getElementById("formSenderAddr")?.value || "").trim(),
            receiver_name: (document.getElementById("formReceiverName")?.value || "").trim(),
            receiver_addr: (document.getElementById("formReceiverAddr")?.value || "").trim(),
            notes: (document.getElementById("formNotes").value || "").trim(),
        }
    };
}

function renderFormPreview(data) {
    const meta = document.getElementById("formPreviewMeta");
    const box = document.getElementById("formPreviewText");
    const c = data.case || {};
    const exPdf = data.export_pdf || {};
    const exDocx = data.export_docx || {};
    const links = [];
    if (exPdf.url) links.push(`PDF：${exPdf.url}`);
    if (exDocx.url) links.push(`WORD：${exDocx.url}`);
    meta.textContent = `類型：${data.title || data.form_type || "-"} ｜ 案號：${c.case_number || "-"} ｜ 當事人：${c.client_name || "-"}${links.length ? ` ｜ 下載：${links.join(" ｜ ")}` : ""}`;
    box.textContent = data.preview_text || "(空白)";
}

async function previewForm() {
    const payload = collectFormPayload();
    const data = await api("/api/osc/forms/preview", "POST", payload);
    state.formPreview = data;
    renderFormPreview(data);
}

async function exportForm() {
    const payload = collectFormPayload();
    const data = await api("/api/osc/forms/export", "POST", payload);
    state.formPreview = data;
    renderFormPreview(data);
    const urls = [];
    if (data.export_pdf?.url) urls.push(data.export_pdf.url);
    if (data.export_docx?.url) urls.push(data.export_docx.url);
    if (urls.length) {
        urls.forEach((u) => window.open(u, "_blank"));
        return;
    }
    const errs = Array.isArray(data.export_errors) ? data.export_errors : [];
    const errText = errs.map((e) => `${e.type || "file"}: ${e.error || "unknown_error"}`).join("\n");
    if (errText) {
        alert(`匯出失敗：\n${errText}`);
    } else {
        alert("已產出檔案，但目前沒有公開下載網址。");
    }
}

async function runLafWizard(mode) {
    const payload = {
        mode,
        action: (document.getElementById("lafWizardAction").value || "").trim(),
        case_id: (document.getElementById("lafWizardCaseId").value || "").trim(),
        case_number: (document.getElementById("lafWizardCaseNumber").value || "").trim(),
        laf_case_no: (document.getElementById("lafWizardLafCaseNo").value || "").trim(),
        client_name: (document.getElementById("lafWizardClientName").value || "").trim(),
        reason: (document.getElementById("lafWizardReason").value || "").trim(),
        fields: parseMaybeJson(document.getElementById("lafWizardFields").value || ""),
    };
    if (mode === "submit") {
        if (payload.action !== "go_live") {
            alert("送出模式目前僅允許開辦（go_live）。");
            return;
        }
        if (!confirm("確定要送出？此動作可能影響正式法扶資料。")) return;
    }
    const data = await api("/api/osc/laf-wizard/run", "POST", payload);
    state.lafWizardResult = data;
    const sum = document.getElementById("lafWizardSummary");
    const rs = data.result || {};
    sum.textContent = `模式：${data.mode || "-"} ｜ 動作：${data.action || "-"} ｜ 結果：${data.ok ? "成功" : "失敗"}${rs.error ? ` ｜ 錯誤：${rs.error}` : ""}`;
    document.getElementById("lafWizardResult").textContent = JSON.stringify(data, null, 2);
    const links = document.getElementById("lafWizardLinks");
    links.innerHTML = "";
    const art = data.artifact || {};
    const png = art.png_export?.url || "";
    const html = art.html_export?.url || "";
    if (png) {
        const b = document.createElement("button");
        b.className = "btn";
        b.textContent = "開啟預覽截圖";
        b.onclick = () => window.open(png, "_blank");
        links.appendChild(b);
    }
    if (html) {
        const b = document.createElement("button");
        b.className = "btn";
        b.textContent = "開啟頁面 HTML";
        b.onclick = () => window.open(html, "_blank");
        links.appendChild(b);
    }
}

async function loadArchivePreview() {
    const data = await api("/api/osc/archive-wizard/preview?limit=500");
    state.archivePreview = data.items || [];
    const sum = data.summary || {};
    document.getElementById("archiveSummary").textContent = `封存根目錄：${data.archive_local || data.archive_base || "-"} ｜ 總數 ${sum.total || 0} ｜ 可搬移 ${sum.ready || 0} ｜ 缺來源 ${sum.missing_source || 0} ｜ 目標已存在 ${sum.target_exists || 0}`;
    const body = document.getElementById("archiveBody");
    if (!state.archivePreview.length) {
        body.innerHTML = `<tr><td colspan="7" class="muted">沒有可檢視資料</td></tr>`;
        return;
    }
    body.innerHTML = state.archivePreview.map(r => `
    <tr>
        <td><input type="checkbox" class="archive-pick" data-id="${esc(r.id)}" ${r.ready ? "checked" : ""}></td>
        <td>${esc(r.case_number || "")}</td>
        <td>${esc(r.client_name || "")}</td>
        <td>${esc(r.status || "")}</td>
        <td title="${esc(r.source_local || r.source_path || "")}">${esc(r.source_local || r.source_path || "")}</td>
        <td title="${esc(r.target_local || "")}">${esc(r.target_local || "")}</td>
        <td>${esc(r.reason || "")}</td>
    </tr>
`).join("");
}

async function executeArchiveMove() {
    const picks = Array.from(document.querySelectorAll(".archive-pick:checked")).map(el => String(el.dataset.id || "").trim()).filter(Boolean);
    if (!picks.length) {
        alert("請先勾選要搬移的案件。");
        return;
    }
    if (!confirm(`確定搬移 ${picks.length} 筆已結案案件？`)) return;
    const force = !!document.getElementById("archiveForce").checked;
    const data = await api("/api/osc/archive-wizard/execute", "POST", { confirm: true, case_ids: picks, force });
    const s = data.summary || {};
    alert(`搬移完成：已搬移 ${s.moved || 0}，略過 ${s.skipped || 0}，錯誤 ${s.errors || 0}`);
    await loadArchivePreview();
    await loadCases();
    await loadMeta();
}
