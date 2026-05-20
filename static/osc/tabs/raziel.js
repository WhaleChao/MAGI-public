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
        max_results: Number(razielEl("razielMaxResults")?.value || 812),
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
    razielSetValue("razielMaxResults", config.max_results || 812);
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
            : "找不到判決分類核心腳本，請確認桌面專案資料夾仍存在。",
        ready ? "ok" : "warn"
    );
    const out = razielEl("razielOutput");
    if (out) {
        out.textContent = [
            `狀態：${ready ? "可使用" : "需檢查"}`,
            `專案資料夾：${data.root || ""}`,
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
        `抓取筆數：${result.total || result.count || result.search_count || "未回報"}`,
        `前後文筆數：${result.preview_count || result.context_count || "未回報"}`,
        `分類成功：${result.classification_success || result.ai_success || "未回報"}`,
        `提醒：${result.user_notice || result.notice || "無"}`,
        "",
        "輸出檔案：",
        `Excel：${data.paths?.xlsx || ""}`,
        `CSV：${data.paths?.csv || ""}`,
        `前後文預覽：${data.paths?.preview || ""}`,
        `補抓報告：${data.paths?.report || ""}`,
        "",
        "原始回傳摘要：",
        JSON.stringify(result, null, 2),
    ];
    const out = razielEl("razielOutput");
    if (out) out.textContent = output.join("\n");
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
