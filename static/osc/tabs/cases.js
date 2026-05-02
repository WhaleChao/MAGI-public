/* tabs/cases.js – Case management + workbench */
async function loadCases() {
    try {
        const q = encodeURIComponent((document.getElementById("casesQ").value || "").trim());
        const category = encodeURIComponent(state.caseCategory || "全部");
        const data = await api(`/api/osc/cases?limit=300&q=${q}&category=${category}`);
        state.cases = data.items || [];
    } catch (e) { console.warn("loadCases failed:", e); }
    renderCases();
}

function renderCases() {
    const body = document.getElementById("casesBody");
    const cardGrid = document.getElementById("casesCardGrid");
    if (!state.cases.length) {
        body.innerHTML = `<tr><td colspan="8" class="muted">沒有資料</td></tr>`;
        if (cardGrid) cardGrid.innerHTML = `<div class="muted" style="padding:20px;">沒有案件資料</div>`;
        return;
    }
    const sorted = applySort([...state.cases], state.sort.col, state.sort.dir, state.sort.type);

    // Card view
    if (cardGrid) {
        const order = JSON.parse(localStorage.getItem('caseCardOrder') || '[]');
        const orderedCases = order.length ? [...sorted].sort((a, b) => {
            const ia = order.indexOf(String(a.id));
            const ib = order.indexOf(String(b.id));
            if (ia === -1 && ib === -1) return 0;
            if (ia === -1) return 1;
            if (ib === -1) return -1;
            return ia - ib;
        }) : sorted;

        cardGrid.innerHTML = orderedCases.map(r => {
            const statusLower = (r.status || "").toLowerCase();
            const isLaf = !!(r.laf_case_no || r.case_category === '法律扶助案件');
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
                    <div><span class="label">法院</span> <span class="value">${esc(r.court_case_no || '-')}</span></div>
                    <div><span class="label">種類</span> <span class="value">${esc(r.case_category || r.case_type || '-')}</span></div>
                    ${r.laf_case_no ? `<div><span class="label">法扶</span> <span class="value">${esc(r.laf_case_no)}</span></div>` : ''}
                </div>
                <div class="card-actions">
                    <button class="btn primary" data-act="case-workbench" data-id="${esc(r.id)}">工作台</button>
                    <button class="btn" data-act="case-open" data-id="${esc(r.id)}">資料夾</button>
                    <button class="btn" data-act="case-edit" data-id="${esc(r.id)}">編輯</button>
                    <button class="btn" data-act="case-address-label" data-id="${esc(r.id)}">📮 地址標籤</button>
                    <button class="btn danger" data-act="case-del" data-id="${esc(r.id)}">刪除</button>
                </div>
                <div class="card-quick-actions">
                    <button class="btn-icon" data-act="case-workbench" data-id="${esc(r.id)}" title="開啟工作台">⚙️</button>
                    <button class="btn-icon" data-act="wb-case-open-host" data-id="${esc(r.id)}" title="在本機開資料夾">📂</button>
                    <button class="btn-icon" data-act="case-edit" data-id="${esc(r.id)}" title="編輯案件">✏️</button>
                </div>
            </div>`;
        }).join("");
        initCardDrag(cardGrid);
    }

    // Table view (hidden by default)
    body.innerHTML = sorted.map(r => `
    <tr class="row-clickable" data-case-id="${esc(r.id)}">
        <td>${esc(r.case_number)}</td>
        <td>${esc(r.client_name)}</td>
        <td>${esc(r.case_category || r.case_type || "")}</td>
        <td>${esc(r.case_reason)}</td>
        <td>${esc(r.court_case_no)}</td>
        <td>${esc(r.laf_case_no)}</td>
        <td>${esc(r.status)}</td>
        <td class="actions">
            <button class="btn" data-act="case-workbench" data-id="${esc(r.id)}">工作台</button>
            <button class="btn" data-act="case-open" data-id="${esc(r.id)}">資料夾</button>
            <button class="btn" data-act="case-edit" data-id="${esc(r.id)}">編輯</button>
            <button class="btn" data-act="case-address-label" data-id="${esc(r.id)}">📮 地址標籤</button>
            <button class="btn danger" data-act="case-del" data-id="${esc(r.id)}">刪除</button>
        </td>
    </tr>
`).join("");

    const ts = document.querySelectorAll("#cases th[data-sort]");
    ts.forEach(th => {
        th.innerHTML = th.innerHTML.replace(/ [▲▼]/g, "") + renderSortArrow(th.dataset.sort);
    });
}

async function editCase(id) {
    const data = await api(`/api/osc/cases/${encodeURIComponent(id)}`);
    const x = data.item || {};
    writeFields("case_", x, ["id", "case_number", "client_name", "client_phone", "client_email", "client_id_number", "laf_case_no", "application_no", "court_case_no", "status", "folder_path", "notes"]);
    document.getElementById("case_category").value = x.case_category || "";
    document.getElementById("case_type").value = x.case_type || "";
    document.getElementById("case_stage").value = x.case_stage || "";
    document.getElementById("case_reason").value = x.case_reason || "";
}

async function delCase(id) {
    if (!confirm(`確定刪除案件 ${id}？`)) return;
    await api(`/api/osc/cases/${encodeURIComponent(id)}`, "DELETE");
    await loadCases();
    await loadMeta();
}

async function saveCase() {
    const p = readFields(["case_id", "case_case_number", "case_client_name", "case_client_phone", "case_client_email", "case_client_id_number", "case_category", "case_type", "case_stage", "case_reason", "case_laf_case_no", "case_application_no", "case_court_case_no", "case_status", "case_folder_path", "case_notes"]);
    const body = {
        id: p.case_id, case_number: p.case_case_number, client_name: p.case_client_name,
        client_phone: p.case_client_phone, client_email: p.case_client_email, client_id_number: p.case_client_id_number,
        case_category: p.case_category, case_type: p.case_type, case_stage: p.case_stage,
        case_reason: p.case_reason, laf_case_no: p.case_laf_case_no, application_no: p.case_application_no,
        court_case_no: p.case_court_case_no, status: p.case_status, folder_path: p.case_folder_path, notes: p.case_notes
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
    clearFields(["case_id", "case_case_number", "case_client_name", "case_client_phone", "case_client_email", "case_client_id_number", "case_category", "case_type", "case_stage", "case_reason", "case_laf_case_no", "case_application_no", "case_court_case_no", "case_status", "case_folder_path", "case_notes"]);
    await loadCases();
    await loadMeta();
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

        // Windows：優先 Synology Drive 本機路徑（如裝），fallback UNC
        if (isWin) {
            if (candidates.win_synology && candidates.win_synology.length) {
                const raw = candidates.win_synology[0];
                // file:/// 不認 %USERPROFILE%；落到複製對話框讓使用者貼
                if (/%USERPROFILE%/i.test(raw)) {
                    showFolderPathDialog(data.folder_path || "", candidates);
                    return data;
                }
                const path = raw.replace(/\\/g, "/");
                window.open(`file:///${path}`, "_blank");
                if (!quiet) showToast("已嘗試開啟（Synology Drive）", "ok");
                return data;
            }
            if (candidates.win_unc && candidates.win_unc.length) {
                window.open(`file:${candidates.win_unc[0]}`, "_blank");
                if (!quiet) showToast("已嘗試開啟（NAS 共享）", "ok");
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
    const safeJSON = (s) => JSON.stringify(String(s || "")).replace(/</g, "\\u003c");
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
            <div class="field"><label>案件類型</label><input id="wb_case_case_type" value="${esc(c.case_type || "")}"></div>
            <div class="field"><label>審級 / 階段</label><input id="wb_case_case_stage" value="${esc(c.case_stage || "")}"></div>
            <div class="field"><label>案由</label><input id="wb_case_case_reason" value="${esc(c.case_reason || "")}"></div>
            <div class="field"><label>法扶案號</label><input id="wb_case_laf_case_no" value="${esc(c.laf_case_no || "")}"></div>
            <div class="field"><label>申請編號</label><input id="wb_case_application_no" value="${esc(c.application_no || "")}"></div>
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
    const rel = data.current_relative_path || "";
    const folderPath = data.folder_path || "";
    const folderExists = !!data.folder_exists;
    const parentButton = rel
        ? `<button class="btn slim" data-act="wb-folder-open" data-id="${esc(c.id || "")}" data-path="${esc(data.parent_relative_path || "")}">上一層</button>`
        : "";
    const rows = entries.length ? entries.map(item => {
        const isDir = item.type === "dir";
        const targetPath = item.relative_path ? `${folderPath.replace(/\\/g, "/").replace(/\/$/, "")}/${item.relative_path}` : folderPath;
        const openBtn = isDir
            ? `<button class="btn slim" data-act="wb-folder-open" data-id="${esc(c.id || "")}" data-path="${esc(item.relative_path || "")}">進入</button>`
            : `<a class="btn slim" href="${fileContentUrl(targetPath, true)}" target="_blank" rel="noopener noreferrer">預覽</a>`;
        const downloadBtn = isDir
            ? ""
            : `<a class="btn slim" href="${fileContentUrl(targetPath)}" target="_blank" rel="noopener noreferrer">下載</a>`;
        const editBtn = (!isDir && isEditableTextFile(targetPath))
            ? `<button class="btn slim" data-act="wb-file-edit" data-id="${esc(c.id || "")}" data-path="${esc(targetPath)}" data-return-path="${esc(rel)}">編輯</button>`
            : "";
        return `
        <tr>
            <td>${isDir ? "資料夾" : "檔案"}</td>
            <td>${esc(item.name || "")}</td>
            <td>${esc(item.modified_at || "")}</td>
            <td>${esc(item.size_label || formatBytes(item.size) || "")}</td>
            <td><div class="wb-folder-actions">${openBtn}${editBtn}${downloadBtn}</div></td>
        </tr>
        `;
    }).join("") : `<tr><td colspan="5" class="muted">目前資料夾沒有可列出的內容</td></tr>`;
    return `
    <div class="card">
        <h3>案件資料夾瀏覽器</h3>
        <div class="toolbar">
            ${parentButton}
            <button class="btn slim" data-act="wb-folder-open" data-id="${esc(c.id || "")}" data-path="${esc(rel)}">重新整理</button>
            <button class="btn slim" data-act="wb-folder-upload" data-id="${esc(c.id || "")}" data-path="${esc(rel)}" data-folder-path="${esc(folderPath)}">上傳檔案</button>
            <button class="btn slim" data-act="wb-folder-copy-path" data-path="${esc(folderPath)}">複製案件路徑</button>
            <button class="btn slim" data-act="wb-case-open-host" data-id="${esc(c.id || "")}">在本機開 NAS</button>
        </div>
        <div class="wb-folder-meta">
            <div class="wb-folder-kv"><div class="k">案件</div><div class="v">${esc(c.case_number || "")}｜${esc(c.client_name || "")}</div></div>
            <div class="wb-folder-kv"><div class="k">同步狀態</div><div class="v">${folderExists ? "已同步，可直接瀏覽" : "尚未同步到伺服器本機"}</div></div>
        </div>
        <div class="wb-breadcrumb">${esc(rel ? `${folderPath} / ${rel}` : folderPath)}</div>
        ${folderExists ? `
        <div class="table-wrap wb-folder-table" style="margin-top:10px;">
            <table>
                <thead><tr><th>類型</th><th>名稱</th><th>更新時間</th><th>大小</th><th>操作</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
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
            <button class="btn slim" data-act="wb-file-editor-back" data-id="${esc(caseId)}" data-path="${esc(returnPath)}">返回資料夾</button>
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

function setCaseCategory(cat) {
    state.caseCategory = cat || "全部";
    document.querySelectorAll("#caseCategoryTabs .chip").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.cat === state.caseCategory);
    });
    loadCases().catch((e) => alert(`載入案件失敗：${e.message}`));
}

function setCaseCategory(cat) {
    state.caseCategory = cat || "全部";
    document.querySelectorAll("#caseCategoryTabs .chip").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.cat === state.caseCategory);
    });
    loadCases().catch((e) => alert(`載入案件失敗：${e.message}`));
}

function wbRenderTodoForm(defaultCaseNumber = "", defaultClientName = "") {
    return `
    <div class="card">
        <h3>待辦（查看 / 新增 / 修改）</h3>
        <div class="grid-4">
            <input id="wb_todo_id" placeholder="待辦ID（更新時使用）">
            <input id="wb_todo_case_number" placeholder="案件編號" value="${esc(defaultCaseNumber)}">
            <input id="wb_todo_client_name" placeholder="當事人" value="${esc(defaultClientName)}">
            <input id="wb_todo_type" placeholder="類型（如 開庭、補件）">
            <input id="wb_todo_date" type="date" placeholder="日期">
            <input id="wb_todo_time" type="time" placeholder="時間">
            <input id="wb_todo_status" placeholder="狀態 (pending/completed/cancelled)">
            <input id="wb_todo_source_file" placeholder="來源檔名（選填）">
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
    wbSetStatus(text, "ok");
    showToast(text, "ok");
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
        court_case_no: (document.getElementById("wb_case_court_case_no")?.value || "").trim(),
        status: (document.getElementById("wb_case_status")?.value || "").trim(),
        folder_path: (document.getElementById("wb_case_folder_path")?.value || "").trim(),
        notes: (document.getElementById("wb_case_notes")?.value || "").trim(),
    };
    if (!body.client_name) {
        wbSetStatus("當事人欄位不能空白。", "warn");
        return;
    }
    await api(`/api/osc/cases/${encodeURIComponent(id)}`, "PUT", body);
    wbSetStatus("案件資料已儲存。", "ok");
    showToast("案件資料已儲存。", "ok");
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
            ${items.map(x => `<tr><td>${esc(x.created_at || "")}</td><td>${esc(x.event_type || "")}</td><td>${esc(x.status || "")}</td><td>${esc(typeof x.event_data === "string" ? x.event_data : JSON.stringify(x.event_data || {}))}</td></tr>`).join("")}
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

function renderDocsByKeyword(docs, keywords) {
    const hits = (docs || []).filter(d => {
        const s = `${d.file_name || ""} ${d.subfolder_name || ""} ${d.reason || ""}`;
        return keywords.some(k => s.includes(k));
    });
    if (!hits.length) return `<div class="muted">尚未索引到相關文件</div>`;
    return `
    <div class="table-wrap"><table>
        <thead><tr><th>檔名</th><th>子資料夾</th><th>操作</th></tr></thead>
        <tbody>
            ${hits.map(x => `<tr>
                <td>${esc(x.file_name || "")}</td>
                <td>${esc(x.subfolder_name || "")}</td>
                <td class="actions">
                    <a class="btn slim" href="${fileContentUrl(x.file_path || "", true)}" target="_blank" rel="noopener noreferrer">預覽</a>
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
                ${todoRows.map(t => `<tr><td>${esc(t.todo_date)} ${esc(t.todo_time)}</td><td>${esc(t.case_number)}</td><td>${esc(t.todo_type)}</td><td>${esc(t.description)}</td><td>${esc(t.status)}</td><td class="actions"><button class="btn" data-act="wb-todo-edit" data-id="${Number(t.id)}">編輯</button></td></tr>`).join("") || `<tr><td colspan="6" class="muted">目前沒有待辦</td></tr>`}
            </tbody>
        </table></div>
    </div>
    <div class="grid-2">
        <div class="card"><h3>法扶進度</h3>${renderLafProgress(data.laf_progress || [])}</div>
        <div class="card"><h3>法扶補件/案件清單</h3>${renderChecklist(data.legal_aid_checklist || [])}${renderChecklist(data.case_checklist || [])}</div>
    </div>
`;
    wbShow(`當事人工作台｜${c.name || id}`, modalHtml);
    wbSetStatus(statusText || `已載入當事人工作台，共 ${caseRows.length} 筆案件、${todoRows.length} 筆待辦。`, statusText ? "ok" : "info");
}

async function openCaseWorkbench(id, statusText = "") {
    const data = await api(`/api/osc/cases/${encodeURIComponent(id)}/workbench`);
    const c = data.case || {};
    state.wb = { mode: "case", id, data };
    const s = data.stats || {};
    const pendingChecklist = (data.legal_aid_checklist || []).filter(x => String(x.status || "").includes("待") || String(x.status || "").includes("缺"));
    const modalHtml = `
    <div class="card">
        <div class="grid-3">
            <div><strong>案號</strong><div>${esc(c.case_number || "")}</div></div>
            <div><strong>當事人</strong><div>${esc(c.client_name || "")}</div></div>
            <div><strong>狀態</strong><div>${esc(c.status || "")}</div></div>
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
        <div class="stat-card"><div class="k">索引文件</div><div class="v">${esc(s.docs_indexed || 0)}</div></div>
    </div>
    <div class="card">
        <h3>快捷功能（委任狀/收據/結案整理）</h3>
        <div class="toolbar">
            <button class="btn" data-act="wb-case-open" data-id="${esc(id)}">瀏覽案件資料夾</button>
            <button class="btn" data-act="wb-case-open-host" data-id="${esc(id)}">在本機開 NAS</button>
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
            <h3>判決/結案相關文件</h3>
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
            <h3>PDF 生成紀錄</h3>
            <div class="table-wrap"><table>
                <thead><tr><th>時間</th><th>檔名</th><th>狀態</th><th>錯誤</th></tr></thead>
                <tbody>
                    ${(data.pdf_generation_log || []).map(x => `<tr><td>${esc(x.log_timestamp || "")}</td><td>${esc(x.file_name || "")}</td><td>${esc(x.status || "")}</td><td>${esc(shortText(x.error_message, 60))}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">尚無 PDF 生成紀錄</td></tr>`}
                </tbody>
            </table></div>
        </div>
    </div>
    <div class="card">
        <h3>待辦列表</h3>
        <div class="table-wrap"><table>
            <thead><tr><th>日期</th><th>類型</th><th>描述</th><th>狀態</th><th>操作</th></tr></thead>
            <tbody>
                ${(data.todos || []).map(t => `<tr><td>${esc(t.todo_date)} ${esc(t.todo_time)}</td><td>${esc(t.todo_type)}</td><td>${esc(t.description)}</td><td>${esc(t.status)}</td><td><button class="btn" data-act="wb-todo-edit" data-id="${Number(t.id)}">編輯</button></td></tr>`).join("") || `<tr><td colspan="5" class="muted">沒有待辦</td></tr>`}
            </tbody>
        </table></div>
    </div>
`;
    wbShow(`案件工作台｜${c.case_number || id}`, modalHtml);
    wbSetStatus(statusText || `已載入案件工作台，待辦 ${s.todo_total || 0} 筆、索引文件 ${s.docs_indexed || 0} 份。`, statusText ? "ok" : "info");
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
