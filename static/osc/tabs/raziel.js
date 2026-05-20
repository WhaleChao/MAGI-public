/* 判決捕捉與分類 */
function razielEl(id) {
    return document.getElementById(id);
}

function setRazielStatus(text, tone = "info") {
    const el = razielEl("razielStatus");
    if (!el) return;
    el.hidden = !text;
    el.className = `status-banner${tone === "warn" || tone === "error" ? " warn" : tone === "ok" || tone === "success" ? " ok" : ""}`;
    el.textContent = text || "";
}

function setRazielBadge(text, tone = "info") {
    const badge = razielEl("razielReadyBadge");
    if (!badge) return;
    badge.textContent = text;
    badge.className = `badge${tone === "ok" || tone === "success" ? " ok" : ""}`;
}

function razielSetValue(id, value) {
    const el = razielEl(id);
    if (el && value !== undefined && value !== null && !el.value) el.value = value;
}

function razielPayload(mode = "preview") {
    return {
        mode,
        keyword_query: razielEl("razielKeywordQuery")?.value || "",
        rule_query: razielEl("razielRuleQuery")?.value || "",
        court_scopes: razielEl("razielCourts")?.value || "",
        max_results: Number(razielEl("razielMaxResults")?.value || 2000),
        split_mb: Number(razielEl("razielSplitMb")?.value || 1900),
        keyword_text_dir_name: razielEl("razielTextDir")?.value || "",
        keyword_pdf_dir_name: razielEl("razielPdfDir")?.value || "",
        ai_provider: razielEl("razielAiProvider")?.value || "nvidia",
        nvidia_model: razielEl("razielNvidiaModel")?.value || "",
        nvidia_large_fallback_model: razielEl("razielNvidiaLargeFallback")?.value || "",
        nvidia_fallback_model: razielEl("razielNvidiaFallback")?.value || "",
        nvidia_api_key: razielEl("razielNvidiaApiKey")?.value || "",
    };
}

function fillRazielConfig(config = {}) {
    razielSetValue("razielKeywordQuery", config.keyword_query || "通譯");
    razielSetValue("razielRuleQuery", config.rule_query || config.keyword_query || "通譯");
    razielSetValue("razielCourts", Array.isArray(config.court_scopes) ? config.court_scopes.join(", ") : (config.court_scopes || "最高法院"));
    razielSetValue("razielMaxResults", config.max_results || 2000);
    razielSetValue("razielSplitMb", 1900);
    razielSetValue("razielTextDir", config.keyword_text_dir_name || "依關鍵字原文");
    razielSetValue("razielPdfDir", config.keyword_pdf_dir_name || "依關鍵字PDF");
    razielSetValue("razielAiProvider", config.ai_provider || "nvidia");
    razielSetValue("razielNvidiaModel", config.nvidia_model || "meta/llama-3.1-405b-instruct");
    razielSetValue("razielNvidiaLargeFallback", config.nvidia_large_fallback_model || "nvidia/nemotron-3-super-120b-a12b");
    razielSetValue("razielNvidiaFallback", config.nvidia_fallback_model || "meta/llama-3.3-70b-instruct");
}

function razielFileSummary(files = {}) {
    const rows = [
        ["Excel", files.xlsx],
        ["CSV", files.csv],
        ["前後文預覽", files.preview],
        ["補抓報告", files.report],
    ];
    return rows.map(([label, file]) => `${label}：${file?.exists ? "已產生" : "尚未產生"}`).join("\n");
}

function renderRazielStatus(data = {}) {
    const config = data.config || {};
    fillRazielConfig(config);
    const ready = Boolean(data.script_exists);
    const keyText = config.has_nvidia_api_key ? "雲端 AI 金鑰已設定" : "尚未設定雲端 AI 金鑰";
    setRazielBadge(ready ? "可使用" : "需檢查", ready ? "ok" : "warn");
    setRazielStatus(
        ready
            ? `判決分類核心已連線。${keyText}。`
            : "找不到判決分類核心，請確認本機判決資料庫仍存在。",
        ready ? "ok" : "warn"
    );
    const out = razielEl("razielOutput");
    if (out) {
        out.textContent = [
            `狀態：${ready ? "可使用" : "需檢查"}`,
            "資料來源：本機判決資料庫",
            `搜尋式：${config.keyword_query || ""}`,
            `分類規則：${config.rule_query || ""}`,
            `法院範圍：${Array.isArray(config.court_scopes) ? config.court_scopes.join(", ") : ""}`,
            `AI 模式：${config.ai_provider || "nvidia"}`,
            `主模型：${config.nvidia_model || ""}`,
            `大型備援：${config.nvidia_large_fallback_model || ""}`,
            `最後備援：${config.nvidia_fallback_model || ""}`,
            keyText,
            "",
            razielFileSummary(data.files || {}),
        ].join("\n");
    }
}

function renderRazielResult(data = {}) {
    const result = data.result || {};
    const config = data.config || {};
    const output = [
        `執行模式：${data.mode || ""}`,
        `結果：${data.ok && result.success !== false ? "完成" : "未完成"}`,
        `搜尋式：${config.keyword_query || ""}`,
        `分類規則：${config.rule_query || ""}`,
        `法院範圍：${Array.isArray(config.court_scopes) ? config.court_scopes.join(", ") : ""}`,
        `AI 模式：${config.ai_provider || ""}`,
        `模型：${result.ai_model || result.model || config.nvidia_model || ""}`,
        "",
        `司法院總筆數：${result.official_total_count || result.total_count || "未回報"}`,
        `本次清單筆數：${result.count || result.search_count || "未回報"}`,
        `本次關鍵字抓取上限：${result.requested_limit || config.max_results || "未回報"}`,
        `前後文筆數：${result.preview_count || result.context_count || "未回報"}`,
        `分類成功：${result.classification_success || result.ai_success || "未回報"}`,
        `提醒：${result.user_notice || result.notice || "無"}`,
        "",
        "輸出檔案：",
        "Excel 分類表：可用上方按鈕下載",
        "CSV 分類表：可用上方按鈕下載",
        "前後文預覽：可用上方按鈕下載",
        "補抓報告：可用上方按鈕下載",
        "",
        "原始回傳摘要：",
        JSON.stringify(result, null, 2),
    ];
    const out = razielEl("razielOutput");
    if (out) out.textContent = output.join("\n");
}

function renderRazielDelivery(manifest = {}) {
    const host = razielEl("razielDeliveryLinks");
    const parts = Array.isArray(manifest.parts) ? manifest.parts : [];
    if (host) {
        host.hidden = false;
        host.innerHTML = [
            `<div class="section-note">交付壓縮檔已產生。${manifest.split ? "檔案較大，已自動分割，請全部下載後再合併或解壓。" : "可直接下載 ZIP。"}</div>`,
            `<div class="toolbar" style="margin-top:8px;margin-bottom:0;">`,
            ...parts.map(part => `<a class="btn" href="${esc(part.url || "")}">${esc(part.name || "下載")}</a>`),
            `</div>`,
        ].join("");
    }
    const out = razielEl("razielOutput");
    if (out) {
        out.textContent = [
            "交付壓縮檔：完成",
            `壓縮檔內資料夾：${manifest.folder_name || "判決捕捉與分類_交付資料"}`,
            `是否分割：${manifest.split ? "是" : "否"}`,
            `檔案數：${manifest.file_count || 0}`,
            "保存位置：本機交付壓縮檔資料夾",
            "",
            "下載檔案：",
            ...parts.map(part => `${part.name}（${part.size || 0} bytes）`),
        ].join("\n");
    }
}

async function loadRazielStatus() {
    const data = await api("/api/osc/raziel/status");
    state.raziel.status = data;
    renderRazielStatus(data);
    return data;
}

async function runRaziel(mode) {
    const labels = {
        search: "正在抓取判決；若有網站限制，系統會在結果中提示改用夜間補抓。",
        preview: "正在產生關鍵字前後文預覽。",
        table: "正在產生 Excel 分類表。",
    };
    try {
        setRazielStatus(labels[mode] || "正在執行判決分類器。");
        const data = await api("/api/osc/raziel/run", "POST", razielPayload(mode));
        state.raziel.lastResult = data;
        renderRazielResult(data);
        setRazielStatus("判決分類器執行完成。", "ok");
        if (razielEl("razielNvidiaApiKey")) razielEl("razielNvidiaApiKey").value = "";
        showToast("判決分類器執行完成。", "ok");
        return data;
    } catch (error) {
        setRazielStatus(`判決分類器沒有完成：${error.message || error}`, "warn");
        throw error;
    }
}

async function createRazielDelivery() {
    try {
        setRazielStatus("正在產生交付壓縮檔。");
        const data = await api("/api/osc/raziel/delivery", "POST", razielPayload("delivery"));
        renderRazielDelivery(data);
        setRazielStatus(data.split ? "交付壓縮檔已完成，因檔案較大已自動分割。" : "交付 ZIP 已完成。", "ok");
        showToast("交付壓縮檔已產生。", "ok");
        return data;
    } catch (error) {
        setRazielStatus(`交付壓縮檔沒有完成：${error.message || error}`, "warn");
        throw error;
    }
}
