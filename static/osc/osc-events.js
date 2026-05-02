/* osc-events.js – Event binding, initialization, boot */
function bindTabs() {
    document.querySelectorAll(".tab-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            const tabId = btn.dataset.tab;
            document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));

            const targetView = document.getElementById(tabId);
            if (targetView) {
                targetView.classList.add("active");
                targetView.scrollTop = 0;
            }
            state.activeTab = tabId;

            const titleEl = document.getElementById("pageTitle");
            if (titleEl) {
                titleEl.textContent = btn.textContent.replace(/^[\p{Emoji}\s]+/u, '');
            }

            if (tabId === "dashboard") loadDashboard();
            if (tabId === "cases") loadCases();
            if (tabId === "laf") loadLaf();
            if (tabId === "clients") loadClients();
            if (tabId === "meetings") loadMeetings();
            if (tabId === "calendar") loadCalendarEvents();
            if (tabId === "todos") loadTodos();
            if (tabId === "documents") {
                loadDocuments();
                loadDocumentTemplates();
                loadDocumentKeywords();
                loadDocumentReplacements();
            }
            if (tabId === "drafts") loadDraftComposer();
            if (tabId === "forms") {
                const now = new Date();
                const d = now.toISOString().slice(0, 10);
                if (!document.getElementById("formDate").value) document.getElementById("formDate").value = d;
                syncFormTypeFields();
            }
            if (tabId === "lafWizard") {
                // no-op: user-triggered actions only
            }
            if (tabId === "archiveWizard") {
                loadArchivePreview();
            }
            if (tabId === "accounting") {
                loadTransactions();
                loadExpenseDefaults();
                loadRecurringExpenses();
            }
            if (tabId === "quotations") {
                loadQuotations();
                loadQuotationTemplates();
            }
            if (tabId === "insights") loadInsights();
            if (tabId === "admin") {
                loadAdminData();
                loadDiscordWebhook();
            }
        });
    });
}

async function dispatchDelegatedAction(act, t) {
    const id = t.dataset.id;
    if (act === "case-edit") return await editCase(id);
    if (act === "case-del") return await delCase(id);
    if (act === "case-open") return await openCaseFolder(id);
    if (act === "case-workbench") return await openCaseWorkbench(id);
    if (act === "case-address-label") return addressLabelDialog(id);

    if (act === "client-edit") return await editClient(id);
    if (act === "client-del") return await delClient(id);
    if (act === "client-workbench") return await openClientWorkbench(id);

    if (act === "meeting-edit") return await editMeeting(Number(id));
    if (act === "meeting-del") return await delMeeting(Number(id));
    if (act === "cal-edit") return await editCalendarEvent(Number(id));
    if (act === "cal-del") return await delCalendarEvent(Number(id));

    if (act === "todo-edit") return await editTodo(Number(id));
    if (act === "todo-del") return await delTodo(Number(id));

    if (act === "doc-open") return await openDocumentPath(t.dataset.path || "");
    if (act === "doc-copy") return await copyDocumentPath(t.dataset.path || "");
    if (act === "doc-stamp") return await stampDocument(t.dataset.path || "");
    if (act === "doc-tpl-edit") return await editDocumentTemplate(Number(id));
    if (act === "doc-tpl-del") return await delDocumentTemplate(Number(id));
    if (act === "doc-kw-edit") return await editDocumentKeyword(Number(id));
    if (act === "doc-kw-del") return await delDocumentKeyword(Number(id));
    if (act === "doc-rp-del") return await delDocumentReplacement(Number(id));
    if (act === "draft-doc-toggle") return toggleDraftDocument(id);
    if (act === "draft-insight-toggle") return toggleDraftInsight(id);

    if (act === "tx-edit") return await editTransaction(Number(id));
    if (act === "tx-del") return await delTransaction(Number(id));
    if (act === "tx-def-edit") return await editExpenseDefault(Number(id));
    if (act === "tx-def-del") return await delExpenseDefault(Number(id));
    if (act === "tx-rec-edit") return await editRecurringExpense(Number(id));
    if (act === "tx-rec-del") return await delRecurringExpense(Number(id));
    if (act === "qt-edit") return await editQuotation(id);
    if (act === "qt-del") return await delQuotation(id);
    if (act === "qt-tpl-edit") return await editQuotationTemplate(Number(id));
    if (act === "qt-tpl-del") return await delQuotationTemplate(Number(id));

    if (act === "insight-toggle") return await toggleInsight(id);
    if (act === "insight-copy") return await copyInsight(id);
    if (act === "insight-fetch") return await fetchInsightFullById(id);

    if (act === "admin-setting-edit") return await editAdminSetting(t.dataset.key || "");
    if (act === "admin-setting-del") return await delAdminSetting(t.dataset.key || "");
    if (act === "admin-reason-edit") return await editAdminCaseReason(Number(id));
    if (act === "admin-reason-del") return await delAdminCaseReason(Number(id));
    if (act === "admin-court-edit") return await editAdminCourt(Number(id));
    if (act === "admin-court-del") return await delAdminCourt(Number(id));
    if (act === "admin-branch-edit") return await editAdminBranch(Number(id));
    if (act === "admin-branch-del") return await delAdminBranch(Number(id));
    if (act === "admin-user-setting-edit") return await editAdminUserSetting(Number(id));
    if (act === "admin-user-setting-del") return await delAdminUserSetting(Number(id));
    if (act === "admin-memory-edit") return await editAdminMemoryKeyword(t.dataset.case || "", t.dataset.hotkey || "");
    if (act === "admin-memory-del") return await delAdminMemoryKeyword(t.dataset.case || "", t.dataset.hotkey || "");
    if (act === "admin-opponent-edit") return await editAdminOpponent(Number(id));
    if (act === "admin-opponent-del") return await delAdminOpponent(Number(id));
    if (act === "admin-pdf-log-del") return await delAdminPdfLog(Number(id));
    if (act === "admin-activity-del") return await delAdminActivityLog(Number(id));

    // P3: Backup / Restore
    if (act === "osc-backup-dry-run") return await restoreOscBackup(t.dataset.filename || "", true);
    if (act === "osc-backup-restore") return await restoreOscBackup(t.dataset.filename || "", false);
    if (act === "osc-backup-del") return await delOscBackup(t.dataset.filename || "");

    if (act === "wb-case-open") return await openCaseFolder(id);
    if (act === "wb-case-open-host") return await openCaseFolderHost(id);
    if (act === "wb-case-workbench") return await openCaseWorkbench(id);
    if (act === "wb-case-save") return await saveWorkbenchCase();
    if (act === "wb-case-create-folder") return await createCaseFolder(id);
    if (act === "wb-case-action") return await wbQuickAction(t.dataset.action || "");
    if (act === "wb-folder-open") return await openCaseFolder(id, t.dataset.path || "");
    if (act === "wb-folder-upload") return promptFolderUpload(id, t.dataset.folderPath || "", t.dataset.path || "");
    if (act === "wb-folder-copy-path") return await copyText(t.dataset.path || "", "路徑已複製。");
    if (act === "wb-file-edit") return await openTextFileEditor(id, t.dataset.path || "", t.dataset.returnPath || "");
    if (act === "wb-file-save") return await saveTextFileEditor(id, t.dataset.path || "", t.dataset.returnPath || "");
    if (act === "wb-file-editor-back") return await openCaseFolder(id, t.dataset.path || "");
    if (act === "wb-todo-edit") return await editTodo(Number(id), "wb_todo_");
    if (act === "wb-todo-save") return await wbSaveTodoAndRefresh();
    if (act === "wb-todo-reset") {
        ["wb_todo_id", "wb_todo_case_number", "wb_todo_client_name", "wb_todo_type", "wb_todo_date", "wb_todo_time", "wb_todo_status", "wb_todo_source_file", "wb_todo_desc"].forEach(x => {
            const el = document.getElementById(x); if (el) el.value = "";
        });
        wbSetStatus("已清空工作台待辦表單。", "ok");
    }
}

function bindGlobalDelegates() {
    document.addEventListener("click", async (e) => {
        const t = e.target.closest("button,[data-act]");
        if (!t) return;
        const act = t.dataset.act;
        if (!act) return;
        const meta = getDelegatedActionFeedback(act, t);
        try {
            await withElementBusy(t, meta?.busyLabel, () => dispatchDelegatedAction(act, t));
            if (meta?.flash) flashButtonFeedback(t, meta.successLabel, meta.successTone);
            if (meta?.showToast) showToast(meta.successText, meta.successTone);
            if (meta?.applyWorkbenchStatus) wbSetStatus(meta.successText, meta.successTone);
        } catch (err) {
            const text = `${meta?.actionLabel || "操作"}失敗：${err.message}`;
            if (meta?.inWorkbench) wbSetStatus(text, "warn");
            showToast(text, "warn", 2800);
        }
    });

    document.addEventListener("dblclick", async (e) => {
        const caseRow = e.target.closest("tr[data-case-id]");
        if (caseRow) {
            const caseId = caseRow.dataset.caseId;
            if (caseId) {
                try {
                    await openCaseFolder(caseId);
                    showToast("已送出案件資料夾開啟動作。", "ok");
                } catch (err) {
                    showToast(`開啟案件資料夾失敗：${err.message}`, "warn", 2800);
                }
            }
        }
    });

    document.addEventListener("click", (e) => {
        const th = e.target.closest("th[data-sort]");
        if (th) {
            const col = th.dataset.sort;
            const type = th.dataset.type || "string";
            if (state.sort.col === col) {
                state.sort.dir = state.sort.dir === 1 ? -1 : 1;
            } else {
                state.sort.col = col;
                state.sort.dir = 1;
                state.sort.type = type;
            }

            // Sync sort bar dropdown if present
            const viewId = th.closest(".view")?.id || th.closest(".modal")?.id || "";
            const syncMap = { cases:'caseSortCol', clients:'clientSortCol', meetings:'meetingSortCol', accounting:'txSortCol', insights:'insightSortCol', todos:'todoSortCol' };
            const dirMap = { cases:'caseSortDir', clients:'clientSortDir', meetings:'meetingSortDir', accounting:'txSortDir', insights:'insightSortDir', todos:'todoSortDir' };
            const selEl = document.getElementById(syncMap[viewId]);
            if (selEl) selEl.value = col;
            const dirEl = document.getElementById(dirMap[viewId]);
            if (dirEl) dirEl.textContent = state.sort.dir === 1 ? '▲' : '▼';

            if (viewId === "cases") renderCases();
            else if (viewId === "clients") renderClients();
            else if (viewId === "meetings") renderMeetings();
            else if (viewId === "calendar") renderCalendarEvents();
            else if (viewId === "todos") renderTodos();
            else if (viewId === "accounting") renderTransactions();
            else if (viewId === "insights") renderInsights();
        }
    });
}

function bindEvents() {
    [
        ["dashboardRefreshBtn", loadDashboard, "儀表板重新整理"],
        ["casesSearchBtn", loadCases, "案件搜尋"],
        ["casesRefreshBtn", loadCases, "案件重新整理"],
        ["caseSaveBtn", saveCase, "案件儲存"],
        ["lafSearchBtn", loadLaf, "法扶清單搜尋"],
        ["lafRefreshBtn", loadLaf, "法扶清單重新整理"],
        ["clientsSearchBtn", loadClients, "當事人搜尋"],
        ["clientsRefreshBtn", loadClients, "當事人重新整理"],
        ["clientSaveBtn", saveClient, "當事人儲存"],
        ["meetingsSearchBtn", loadMeetings, "會議搜尋"],
        ["meetingsRefreshBtn", loadMeetings, "會議重新整理"],
        ["meetingSaveBtn", saveMeeting, "會議儲存"],
        ["calSearchBtn", loadCalendarEvents, "行事曆搜尋"],
        ["calRefreshBtn", loadCalendarEvents, "行事曆重新整理"],
        ["calSaveBtn", saveCalendarEvent, "行事曆儲存"],
        ["todosSearchBtn", loadTodos, "待辦搜尋"],
        ["todosRefreshBtn", loadTodos, "待辦重新整理"],
        ["todoSaveBtn", saveTodo, "待辦儲存"],
        ["docsSearchBtn", loadDocuments, "文件搜尋"],
        ["docsRefreshBtn", loadDocuments, "文件重新整理"],
        ["docGeneratePoaBtn", () => runDocCaseAction("generate_power_of_attorney"), "製作委任狀"],
        ["docGenerateReceiptBtn", () => runDocCaseAction("generate_receipt"), "製作收據"],
        ["docClosingOverviewBtn", () => runDocCaseAction("closing_overview"), "結案資料彙整"],
        ["docLafProgressBtn", () => runDocCaseAction("laf_progress_summary"), "法扶進度盤點"],
        ["docLafClosingBtn", () => runDocCaseAction("laf_closing_status"), "結案狀況盤點"],
        ["lafWizardPreviewBtn", () => runLafWizard("preview"), "法扶精靈預覽"],
        ["lafWizardDraftBtn", () => runLafWizard("draft"), "法扶精靈存檔"],
        ["lafWizardSubmitBtn", () => runLafWizard("submit"), "法扶精靈送出"],
        ["archivePreviewBtn", loadArchivePreview, "結案搬移預覽"],
        ["archiveExecuteBtn", executeArchiveMove, "結案搬移執行"],
        ["docTplSearchBtn", loadDocumentTemplates, "書狀模板搜尋"],
        ["docTplRefreshBtn", loadDocumentTemplates, "書狀模板重新整理"],
        ["docTplSaveBtn", saveDocumentTemplate, "書狀模板儲存"],
        ["docKwSearchBtn", loadDocumentKeywords, "書狀關鍵字搜尋"],
        ["docKwRefreshBtn", loadDocumentKeywords, "書狀關鍵字重新整理"],
        ["docKwSaveBtn", saveDocumentKeyword, "書狀關鍵字儲存"],
        ["docRpSearchBtn", loadDocumentReplacements, "替換紀錄搜尋"],
        ["docRpRefreshBtn", loadDocumentReplacements, "替換紀錄重新整理"],
        ["accountingSearchBtn", loadTransactions, "帳務搜尋"],
        ["accountingRefreshBtn", loadTransactions, "帳務重新整理"],
        ["accountingPeriodBtn", applyAccountingPeriod, "帳務區間套用"],
        ["txSaveBtn", saveTransaction, "帳務儲存"],
        ["txDefSearchBtn", loadExpenseDefaults, "預設帳務搜尋"],
        ["txDefRefreshBtn", loadExpenseDefaults, "預設帳務重新整理"],
        ["txDefSaveBtn", saveExpenseDefault, "預設帳務儲存"],
        ["txRecurringSearchBtn", loadRecurringExpenses, "固定支出搜尋"],
        ["txRecurringRefreshBtn", loadRecurringExpenses, "固定支出重新整理"],
        ["txRecurringSaveBtn", saveRecurringExpense, "固定支出儲存"],
        ["qtSearchBtn", loadQuotations, "報價搜尋"],
        ["qtRefreshBtn", loadQuotations, "報價重新整理"],
        ["qtSaveBtn", saveQuotation, "報價儲存"],
        ["qtTplSearchBtn", loadQuotationTemplates, "報價模板搜尋"],
        ["qtTplRefreshBtn", loadQuotationTemplates, "報價模板重新整理"],
        ["qtTplSaveBtn", saveQuotationTemplate, "報價模板儲存"],
        ["insightsSearchBtn", loadInsights, "實務見解搜尋"],
        ["insightsRefreshBtn", loadInsights, "實務見解重新整理"],
        ["insightSaveBtn", saveInsight, "新增見解"],
        ["insightFetchBtn", fetchInsightFullManual, "抓取見解來源"],
        ["adminSettingsSearchBtn", loadAdminSettings, "系統設定搜尋"],
        ["adminSettingsRefreshBtn", loadAdminSettings, "系統設定重新整理"],
        ["adminSettingSaveBtn", saveAdminSetting, "系統設定儲存"],
        ["discordWebhookSaveBtn", saveDiscordWebhook, "Discord 設定儲存"],
        ["discordWebhookTestBtn", testDiscordWebhook, "Discord Test 推播"],
        ["gcalSaveCredsBtn", saveGcalCreds, "GCal 憑證儲存"],
        ["gcalConnectBtn", connectGcal, "GCal 連線授權"],
        ["gcalSyncDryRunBtn", () => syncGcal(true), "GCal Dry-run 同步"],
        ["gcalSyncBtn", () => syncGcal(false), "GCal 立即同步"],
        ["gcalDisconnectBtn", disconnectGcal, "GCal 解除授權"],
        ["oscBackupCreateBtn", createOscBackup, "立即備份"],
        ["oscBackupRefreshBtn", loadOscBackups, "備份列表重新整理"],
        ["adminReasonSearchBtn", loadAdminCaseReasons, "案由模板搜尋"],
        ["adminReasonRefreshBtn", loadAdminCaseReasons, "案由模板重新整理"],
        ["adminReasonSaveBtn", saveAdminCaseReason, "案由模板儲存"],
        ["adminCourtsSearchBtn", loadAdminCourts, "法院搜尋"],
        ["adminCourtsRefreshBtn", loadAdminCourts, "法院重新整理"],
        ["adminCourtSaveBtn", saveAdminCourt, "法院儲存"],
        ["adminBranchesSearchBtn", loadAdminBranches, "法扶分會搜尋"],
        ["adminBranchesRefreshBtn", loadAdminBranches, "法扶分會重新整理"],
        ["adminBranchSaveBtn", saveAdminBranch, "法扶分會儲存"],
        ["adminUserSettingsSearchBtn", loadAdminUserSettings, "使用者設定搜尋"],
        ["adminUserSettingsRefreshBtn", loadAdminUserSettings, "使用者設定重新整理"],
        ["adminUserSettingSaveBtn", saveAdminUserSetting, "使用者設定儲存"],
        ["adminMemoryKeywordsSearchBtn", loadAdminMemoryKeywords, "案件熱鍵搜尋"],
        ["adminMemoryKeywordsRefreshBtn", loadAdminMemoryKeywords, "案件熱鍵重新整理"],
        ["adminMemoryKeywordSaveBtn", saveAdminMemoryKeyword, "案件熱鍵儲存"],
        ["adminOpponentsSearchBtn", loadAdminOpponents, "對造搜尋"],
        ["adminOpponentsRefreshBtn", loadAdminOpponents, "對造重新整理"],
        ["adminOpponentSaveBtn", saveAdminOpponent, "對造儲存"],
        ["adminPdfLogsSearchBtn", loadAdminPdfLogs, "PDF 紀錄搜尋"],
        ["adminPdfLogsRefreshBtn", loadAdminPdfLogs, "PDF 紀錄重新整理"],
        ["adminActivityLogsSearchBtn", loadAdminActivityLogs, "活動紀錄搜尋"],
        ["adminActivityLogsRefreshBtn", loadAdminActivityLogs, "活動紀錄重新整理"],
    ].forEach(([buttonId, fn, actionLabel]) => bindBusyClick(buttonId, fn, { actionLabel }));

    document.getElementById("caseResetBtn").addEventListener("click", () => clearFields(["case_id", "case_case_number", "case_client_name", "case_client_phone", "case_client_email", "case_client_id_number", "case_category", "case_type", "case_stage", "case_reason", "case_laf_case_no", "case_application_no", "case_court_case_no", "case_status", "case_folder_path", "case_notes"]));
    document.querySelectorAll("#caseCategoryTabs .chip").forEach(btn => {
        btn.addEventListener("click", () => setCaseCategory(btn.dataset.cat || "全部"));
    });

    // Cases CSV
    const casesImportBtn = document.getElementById("casesImportCsvBtn");
    const casesExportBtn = document.getElementById("casesExportCsvBtn");
    const casesFileInput = document.getElementById("casesImportCsvFile");
    if (casesImportBtn) casesImportBtn.addEventListener("click", importCasesCsv);
    if (casesExportBtn) casesExportBtn.addEventListener("click", exportCasesCsv);
    if (casesFileInput) casesFileInput.addEventListener("change", e => handleCasesCsvUpload(e.target.files[0]));

    // Clients CSV
    const clientsImportBtn = document.getElementById("clientsImportCsvBtn");
    const clientsExportBtn = document.getElementById("clientsExportCsvBtn");
    const clientsFileInput = document.getElementById("clientsImportCsvFile");
    if (clientsImportBtn) clientsImportBtn.addEventListener("click", importClientsCsv);
    if (clientsExportBtn) clientsExportBtn.addEventListener("click", exportClientsCsv);
    if (clientsFileInput) clientsFileInput.addEventListener("change", e => handleClientsCsvUpload(e.target.files[0]));

    // LAF Checklist
    const lafClLoadBtn = document.getElementById("lafChecklistLoadBtn");
    const lafClSeedBtn = document.getElementById("lafChecklistSeedBtn");
    const lafClAddBtn  = document.getElementById("lafChecklistAddBtn");
    if (lafClLoadBtn) lafClLoadBtn.addEventListener("click", loadLafChecklist);
    if (lafClSeedBtn) lafClSeedBtn.addEventListener("click", seedLafChecklist);
    if (lafClAddBtn)  lafClAddBtn.addEventListener("click", addLafChecklistItem);

    // Case Checklist
    const caseClLoadBtn = document.getElementById("caseChecklistLoadBtn");
    const caseClAddBtn  = document.getElementById("caseChecklistAddBtn");
    if (caseClLoadBtn) caseClLoadBtn.addEventListener("click", loadCaseChecklist);
    if (caseClAddBtn)  caseClAddBtn.addEventListener("click", addCaseChecklistItem);

    document.getElementById("clientResetBtn").addEventListener("click", () => clearFields(["client_id", "client_name", "client_contact_person", "client_phone", "client_email", "client_address", "client_tax_id", "client_notes", "client_status"]));
    document.getElementById("meetingResetBtn").addEventListener("click", () => clearFields(["meeting_id", "meeting_case_number", "meeting_client_name", "meeting_type", "meeting_datetime", "meeting_duration", "meeting_location", "meeting_notes", "meeting_status"]));
    document.getElementById("calResetBtn").addEventListener("click", () => clearFields(["cal_id", "cal_event_id", "cal_title", "cal_case_number", "cal_start_date", "cal_end_date", "cal_location", "cal_color", "cal_is_all_day", "cal_reminder_minutes", "cal_summary", "cal_description", "cal_raw_data"]));
    document.getElementById("todoResetBtn").addEventListener("click", () => clearFields(["todo_id", "todo_case_number", "todo_client_name", "todo_type", "todo_date", "todo_time", "todo_desc", "todo_status", "todo_source_file"]));
    document.getElementById("docsKind").addEventListener("change", () => runBusyAction("docsSearchBtn", loadDocuments, { actionLabel: "文件搜尋" }));

    document.getElementById("draftMetaRefreshBtn").addEventListener("click", () => loadDraftMeta().catch(reportDraftError));
    document.getElementById("draftCaseSearchBtn").addEventListener("click", () => searchDraftCases().catch(reportDraftError));
    document.getElementById("draftCaseLoadBtn").addEventListener("click", () => loadDraftSelectedCase().catch(reportDraftError));
    document.getElementById("draftDocsSearchBtn").addEventListener("click", () => loadDraftDocuments().catch(reportDraftError));
    document.getElementById("draftDocsClearBtn").addEventListener("click", () => {
        state.draft.selectedDocuments = [];
        renderDraftDocuments();
        setDraftStatus("已清除參考書狀選取。");
    });
    document.getElementById("draftInsightsSearchBtn").addEventListener("click", () => loadDraftInsights().catch(reportDraftError));
    document.getElementById("draftInsightsAutoBtn").addEventListener("click", () => autoDraftInsights().catch(reportDraftError));
    document.getElementById("draftInsightsClearBtn").addEventListener("click", () => {
        state.draft.selectedInsights = [];
        renderDraftInsights();
        setDraftStatus("已清除實務見解選取。");
    });
    document.getElementById("draftPreviewBtn").addEventListener("click", () => previewDraftPrompt().catch(reportDraftError));
    document.getElementById("draftGenerateBtn").addEventListener("click", () => generateDraft().catch(reportDraftError));
    document.getElementById("draftCopyBtn").addEventListener("click", () => copyDraftResult().catch(reportDraftError));
    document.getElementById("draftExportBtn").addEventListener("click", () => exportDraftResult().catch(reportDraftError));
    document.getElementById("draftClearBtn").addEventListener("click", clearDraftResult);
    document.getElementById("draftDocType").addEventListener("change", () => {
        if (!(document.getElementById("draftSuggestedName").value || "").trim()) {
            const docType = (document.getElementById("draftDocType").value || "書狀草稿").trim();
            const caseNo = (document.getElementById("draftCaseNumber").value || "未命名").trim();
            document.getElementById("draftSuggestedName").value = `${docType}_${caseNo}`;
        }
    });
    bindEnterSubmit(["draftCaseSearch"], "draftCaseSearchBtn", searchDraftCases, { actionLabel: "案件搜尋", onError: reportDraftError });
    bindEnterSubmit(["draftDocsQ", "draftDocsCaseFilter"], "draftDocsSearchBtn", loadDraftDocuments, { actionLabel: "書狀搜尋", onError: reportDraftError });
    bindEnterSubmit(["draftInsightsQ", "draftInsightsCaseFilter", "draftInsightsReasonFilter"], "draftInsightsSearchBtn", loadDraftInsights, { actionLabel: "見解搜尋", onError: reportDraftError });
    document.getElementById("draftResult").addEventListener("input", updateDraftCharCount);

    document.getElementById("formPreviewBtn").addEventListener("click", () => withBusy("formPreviewBtn", "預覽中...", previewForm).catch(e => alert(`預覽失敗：${e.message}`)));
    document.getElementById("formExportBtn").addEventListener("click", () => withBusy("formExportBtn", "匯出中...", exportForm).catch(e => alert(`匯出失敗：${e.message}`)));
    document.getElementById("formType").addEventListener("change", syncFormTypeFields);
    document.getElementById("docTplResetBtn").addEventListener("click", () => {
        ["docTplId", "docTplType", "docTplParty", "docTplCase", "docTplDivision", "docTplUseCount", "docTplData"].forEach(x => {
            const el = document.getElementById(x); if (el) el.value = "";
        });
    });
    document.getElementById("docKwResetBtn").addEventListener("click", () => {
        ["docKwId", "docKwCase", "docKwName", "docKwCategory", "docKwHotkey", "docKwCaseSpecific", "docKwUsageCount", "docKwContent"].forEach(x => {
            const el = document.getElementById(x); if (el) el.value = "";
        });
    });
    document.getElementById("txResetBtn").addEventListener("click", () => clearFields(["tx_id", "tx_case_id", "tx_date", "tx_type", "tx_sub_type", "tx_category", "tx_amount", "tx_description"]));
    document.getElementById("txDefResetBtn").addEventListener("click", () => {
        ["txDefId", "txDefCategory", "txDefAmount", "txDefDescription"].forEach(x => {
            const el = document.getElementById(x); if (el) el.value = "";
        });
    });
    document.getElementById("txRecurringResetBtn").addEventListener("click", () => {
        ["txRecurringId", "txRecurringCategory", "txRecurringSubType", "txRecurringDescription", "txRecurringAmount", "txRecurringDay", "txRecurringStartDate", "txRecurringEndDate", "txRecurringActive", "txRecurringLastMonth"].forEach(x => {
            const el = document.getElementById(x); if (el) el.value = "";
        });
    });
    document.getElementById("txRecurringOnlyActive").addEventListener("change", () => runBusyAction("txRecurringSearchBtn", loadRecurringExpenses, { actionLabel: "固定支出搜尋" }));
    document.getElementById("qtResetBtn").addEventListener("click", () => {
        clearFields(["qt_id", "qt_client_name", "qt_project_name", "qt_contact", "qt_phone", "qt_email", "qt_address", "qt_tax_id", "qt_date", "qt_expiry", "qt_subtotal", "qt_discount", "qt_tax", "qt_total", "qt_status", "qt_notes", "qt_items", "qt_extended_data"]);
    });
    document.getElementById("qtTplResetBtn").addEventListener("click", () => {
        clearFields(["qtTplId", "qtTplName", "qtTplDefault", "qtTplDescription", "qtTplItems", "qtTplNotes"]);
    });
    document.getElementById("adminSettingResetBtn").addEventListener("click", () => clearFields(["adminSettingKey", "adminSettingValue", "adminSettingDescription"]));
    document.getElementById("adminReasonResetBtn").addEventListener("click", () => clearFields(["adminReasonId", "adminReasonType", "adminReasonText", "adminReasonCommon"]));
    document.getElementById("adminCourtResetBtn").addEventListener("click", () => clearFields(["adminCourtId", "adminCourtName", "adminCourtType", "adminCourtAddress"]));
    document.getElementById("adminBranchResetBtn").addEventListener("click", () => clearFields(["adminBranchId", "adminBranchName", "adminBranchAddress"]));
    document.getElementById("adminUserSettingResetBtn").addEventListener("click", () => clearFields(["adminUserSettingId", "adminUserSettingHost", "adminUserSettingKey", "adminUserSettingValue"]));
    document.getElementById("adminMemoryKeywordResetBtn").addEventListener("click", () => clearFields(["adminMemoryCaseNumber", "adminMemoryHotkey", "adminMemoryName", "adminMemoryValue"]));
    document.getElementById("adminOpponentResetBtn").addEventListener("click", () => clearFields(["adminOpponentId", "adminOpponentCaseNumber", "adminOpponentName", "adminOpponentAddress", "adminOpponentActive"]));

    document.getElementById("wbCloseBtn").addEventListener("click", wbClose);
    const wbFolderUploadInput = document.getElementById("wbFolderUploadInput");
    if (wbFolderUploadInput) {
        wbFolderUploadInput.addEventListener("change", async (e) => {
            const file = e.target.files && e.target.files[0];
            if (!file) return;
            await handleFolderUpload(file);
            e.target.value = "";
        });
    }
    document.getElementById("wbMask").addEventListener("click", (e) => {
        if (e.target.id === "wbMask") wbClose();
    });

    [
        { inputs: ["casesQ"], buttonId: "casesSearchBtn", fn: loadCases, actionLabel: "案件搜尋" },
        { inputs: ["lafQ"], buttonId: "lafSearchBtn", fn: loadLaf, actionLabel: "法扶清單搜尋" },
        { inputs: ["clientsQ"], buttonId: "clientsSearchBtn", fn: loadClients, actionLabel: "當事人搜尋" },
        { inputs: ["meetingsQ"], buttonId: "meetingsSearchBtn", fn: loadMeetings, actionLabel: "會議搜尋" },
        { inputs: ["calQ"], buttonId: "calSearchBtn", fn: loadCalendarEvents, actionLabel: "行事曆搜尋" },
        { inputs: ["todosQ"], buttonId: "todosSearchBtn", fn: loadTodos, actionLabel: "待辦搜尋" },
        { inputs: ["docsQ", "docsCaseNumber"], buttonId: "docsSearchBtn", fn: loadDocuments, actionLabel: "文件搜尋" },
        { inputs: ["docTplQ", "docTplCaseNumber", "docTplTypeFilter"], buttonId: "docTplSearchBtn", fn: loadDocumentTemplates, actionLabel: "書狀模板搜尋" },
        { inputs: ["docKwQ", "docKwCaseNumber", "docKwCategoryFilter"], buttonId: "docKwSearchBtn", fn: loadDocumentKeywords, actionLabel: "書狀關鍵字搜尋" },
        { inputs: ["docRpQ", "docRpCaseNumber"], buttonId: "docRpSearchBtn", fn: loadDocumentReplacements, actionLabel: "替換紀錄搜尋" },
        { inputs: ["accountingQ", "accountingCaseNumber", "accountingStartDate", "accountingEndDate"], buttonId: "accountingSearchBtn", fn: loadTransactions, actionLabel: "帳務搜尋" },
        { inputs: ["txDefQ"], buttonId: "txDefSearchBtn", fn: loadExpenseDefaults, actionLabel: "預設帳務搜尋" },
        { inputs: ["txRecurringQ"], buttonId: "txRecurringSearchBtn", fn: loadRecurringExpenses, actionLabel: "固定支出搜尋" },
        { inputs: ["qtQ", "qtStatusFilter"], buttonId: "qtSearchBtn", fn: loadQuotations, actionLabel: "報價搜尋" },
        { inputs: ["qtTplQ"], buttonId: "qtTplSearchBtn", fn: loadQuotationTemplates, actionLabel: "報價模板搜尋" },
        { inputs: ["insightsQ"], buttonId: "insightsSearchBtn", fn: loadInsights, actionLabel: "實務見解搜尋" },
        { inputs: ["insight_fetch_url"], buttonId: "insightFetchBtn", fn: fetchInsightFullManual, actionLabel: "抓取見解來源" },
        { inputs: ["adminSettingsQ"], buttonId: "adminSettingsSearchBtn", fn: loadAdminSettings, actionLabel: "系統設定搜尋" },
        { inputs: ["adminReasonQ", "adminReasonTypeFilter"], buttonId: "adminReasonSearchBtn", fn: loadAdminCaseReasons, actionLabel: "案由模板搜尋" },
        { inputs: ["adminCourtsQ"], buttonId: "adminCourtsSearchBtn", fn: loadAdminCourts, actionLabel: "法院搜尋" },
        { inputs: ["adminBranchesQ"], buttonId: "adminBranchesSearchBtn", fn: loadAdminBranches, actionLabel: "法扶分會搜尋" },
        { inputs: ["adminUserSettingsQ"], buttonId: "adminUserSettingsSearchBtn", fn: loadAdminUserSettings, actionLabel: "使用者設定搜尋" },
        { inputs: ["adminMemoryKeywordsQ", "adminMemoryCaseFilter"], buttonId: "adminMemoryKeywordsSearchBtn", fn: loadAdminMemoryKeywords, actionLabel: "案件熱鍵搜尋" },
        { inputs: ["adminOpponentsQ", "adminOpponentsCaseFilter"], buttonId: "adminOpponentsSearchBtn", fn: loadAdminOpponents, actionLabel: "對造搜尋" },
        { inputs: ["adminPdfLogsQ", "adminPdfLogsCaseFilter"], buttonId: "adminPdfLogsSearchBtn", fn: loadAdminPdfLogs, actionLabel: "PDF 紀錄搜尋" },
        { inputs: ["adminActivityLogsQ", "adminActivityTypeFilter"], buttonId: "adminActivityLogsSearchBtn", fn: loadAdminActivityLogs, actionLabel: "活動紀錄搜尋" },
    ].forEach(({ inputs, buttonId, fn, actionLabel }) => bindEnterSubmit(inputs, buttonId, fn, { actionLabel }));

    bindGlobalDelegates();
}

// ── Sort bar controls ──
function initSortBars() {
    // Generic helper: wire a sort-bar (select + dir button) to state.sort + re-render
    function wire(selectId, dirBtnId, renderFn, defaultType) {
        const sel = document.getElementById(selectId);
        const btn = document.getElementById(dirBtnId);
        if (!sel || !btn) return;
        sel.addEventListener('change', () => {
            state.sort.col = sel.value;
            state.sort.type = defaultType || 'string';
            // Check for date/number types from the option
            const opt = sel.selectedOptions[0];
            if (opt && opt.dataset.type) state.sort.type = opt.dataset.type;
            renderFn();
        });
        btn.addEventListener('click', () => {
            state.sort.dir = state.sort.dir === 1 ? -1 : 1;
            btn.textContent = state.sort.dir === 1 ? '▲' : '▼';
            renderFn();
        });
    }
    wire('caseSortCol', 'caseSortDir', renderCases, 'string');
    wire('todoSortCol', 'todoSortDir', renderTodos, 'string');
    wire('clientSortCol', 'clientSortDir', renderClients, 'string');
    wire('meetingSortCol', 'meetingSortDir', renderMeetings, 'string');
    wire('txSortCol', 'txSortDir', renderTransactions, 'string');
    wire('insightSortCol', 'insightSortDir', renderInsights, 'string');
}

// ── Global search ──
function initGlobalSearch() {
    const input = document.getElementById('globalSearchInput');
    if (!input) return;
    let timer = null;
    input.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(async () => {
            const q = (input.value || '').trim();
            if (!q) return;
            // Switch to cases tab and search
            const casesQ = document.getElementById('casesQ');
            if (casesQ) casesQ.value = q;
            // Click cases tab
            const casesTab = document.querySelector('.tab-btn[data-tab="cases"]');
            if (casesTab) casesTab.click();
        }, 400);
    });
    input.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
            e.preventDefault();
            const q = (input.value || '').trim();
            if (!q) return;
            const casesQ = document.getElementById('casesQ');
            if (casesQ) casesQ.value = q;
            const casesTab = document.querySelector('.tab-btn[data-tab="cases"]');
            if (casesTab) casesTab.click();
        }
    });
}

// ── Theme toggle (P2 對應 PaperClip ThemeManager line 507) ──
function initThemeToggle() {
    const STORAGE_KEY = "magi.osc.theme";
    const btn = document.getElementById("themeToggleBtn");

    function applyTheme(name) {
        if (name === "dark") {
            document.body.classList.add("theme-dark");
            if (btn) btn.textContent = "☀️";
        } else {
            document.body.classList.remove("theme-dark");
            if (btn) btn.textContent = "🌙";
        }
    }

    let saved = "light";
    try { saved = localStorage.getItem(STORAGE_KEY) || "light"; } catch (_) {}
    applyTheme(saved);

    if (btn) {
        btn.addEventListener("click", () => {
            const cur = document.body.classList.contains("theme-dark") ? "dark" : "light";
            const next = cur === "dark" ? "light" : "dark";
            applyTheme(next);
            try { localStorage.setItem(STORAGE_KEY, next); } catch (_) {}
        });
    }
}

async function boot() {
    // bug fix 2026-05-02：原本 await loadMeta() 在 init chain 之後，任一 init 拋例外
    // 整個 boot() 不繼續，dbBadge 卡「連線中」永不更新（律師外網看到的問題）
    // 改：loadMeta fire-and-forget + 每個 init 包 try/catch（一個壞不影響其他）
    const _safeLoadMeta = () => {
        try {
            loadMeta().catch((e) => {
                console.error("loadMeta failed:", e);
                const dbBadge = document.getElementById("dbBadge");
                if (dbBadge) dbBadge.textContent = `DB: 連線失敗 (${e.message || e})`;
            });
        } catch (e) {
            console.error("loadMeta sync error:", e);
            const dbBadge = document.getElementById("dbBadge");
            if (dbBadge) dbBadge.textContent = `DB: 連線失敗 (${e.message || e})`;
        }
    };
    _safeLoadMeta();

    const _safe = (label, fn) => {
        try { fn(); } catch (e) { console.error(`${label} failed:`, e); }
    };
    _safe("bindTabs", bindTabs);
    _safe("bindEvents", bindEvents);
    _safe("initCaseViewToggle", initCaseViewToggle);
    _safe("initCalendarView", initCalendarView);
    _safe("initSortBars", initSortBars);
    _safe("initGlobalSearch", initGlobalSearch);
    _safe("initThemeToggle", initThemeToggle);
    _safe("syncFormTypeFields", syncFormTypeFields);
    _safe("applyAccountingPeriod", applyAccountingPeriod);

    try { await loadDashboard(); } catch (e) { console.error("loadDashboard failed:", e); }
    setInterval(_safeLoadMeta, 30000);
}
boot();
// bfcache: reload active tab data when returning via back/forward
window.addEventListener('pageshow', function(e) {
    if (e.persisted) {
        const tab = state.activeTab || 'dashboard';
        if (tab === 'dashboard') loadDashboard();
        else if (tab === 'cases') loadCases();
    }
});
