/* osc-utils.js – Utility functions */
function esc(v) {
    return String(v ?? "").replace(/[&<>\"']/g, s => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[s]));
}

function oscTodoSourceKey(row) {
    const sourceKind = String(row?.source_kind || "").trim();
    if (sourceKind) return sourceKind;
    const source = String(row?.source_file || "").trim();
    if (source.startsWith("gcal_import")) return "gcal_import";
    if (String(row?.todo_type || "").trim() === "行事曆事件") return "calendar_todo";
    return "case_todos";
}

function oscTodoIsCalendarSource(row) {
    return ["gcal_import", "calendar_todo"].includes(oscTodoSourceKey(row));
}

function oscTodoSourceLabel(row) {
    const label = String(row?.source_label || "").trim();
    if (label) return label;
    const key = oscTodoSourceKey(row);
    if (key === "gcal_import") return "Google 日曆匯入";
    if (key === "calendar_todo") return "行事曆事件待辦";
    return "OSC 建立";
}

function safeWebUrl(rawUrl) {
    const text = String(rawUrl || "").trim();
    try {
        const parsed = new URL(text, window.location.origin);
        if (["http:", "https:", "mailto:"].includes(parsed.protocol)) return text;
    } catch { }
    return "";
}

function formatWebInlineText(text) {
    const raw = String(text || "");
    const linkRe = /\[([^\]]{1,180})\]\(([^)\s]{1,600})\)/g;
    let html = "";
    let pos = 0;
    const fmt = chunk => esc(chunk)
        .replace(/`([^`]{1,160})`/g, "<code>$1</code>")
        .replace(/\*\*([^*]{1,220})\*\*/g, "<strong>$1</strong>")
        .replace(/__([^_]{1,220})__/g, "<strong>$1</strong>");
    let match;
    while ((match = linkRe.exec(raw)) !== null) {
        html += fmt(raw.slice(pos, match.index));
        const url = safeWebUrl(match[2]);
        html += url
            ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${fmt(match[1])}</a>`
            : fmt(match[1]);
        pos = match.index + match[0].length;
    }
    html += fmt(raw.slice(pos));
    return html;
}

function renderWebReplyHtml(text) {
    const raw = String(text || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim();
    if (!raw) return '<div class="web-reply"><p>沒有可顯示內容。</p></div>';
    const blocks = [];
    let listType = "";
    let inCode = false;
    let codeLines = [];
    const closeList = () => {
        if (listType) {
            blocks.push(`</${listType}>`);
            listType = "";
        }
    };
    const openList = kind => {
        if (listType !== kind) {
            closeList();
            blocks.push(`<${kind}>`);
            listType = kind;
        }
    };
    raw.split("\n").forEach(rawLine => {
        const line = rawLine.trim();
        if (line.startsWith("```")) {
            if (inCode) {
                blocks.push(`<pre><code>${esc(codeLines.join("\n"))}</code></pre>`);
                codeLines = [];
                inCode = false;
            } else {
                closeList();
                inCode = true;
                codeLines = [];
            }
            return;
        }
        if (inCode) {
            codeLines.push(rawLine);
            return;
        }
        if (!line) {
            closeList();
            return;
        }
        if (/^[━─=\-_*]{4,}$/.test(line) || /^#{2,6}$/.test(line)) {
            closeList();
            blocks.push("<hr>");
            return;
        }
        let headingLine = line;
        const wrappedHeading = headingLine.match(/^\*\*(#{1,6}\s*[^*]+?)\*\*$/);
        if (wrappedHeading) headingLine = wrappedHeading[1].trim();
        headingLine = headingLine.replace(/\*\*$/, "").trim();
        const heading = headingLine.match(/^(#{1,6})\s*(.+)$/);
        if (heading) {
            const title = heading[2].replace(/^#+|#+$/g, "").trim();
            if (title) {
                closeList();
                const level = heading[1].length === 1 ? 3 : 4;
                blocks.push(`<h${level}>${formatWebInlineText(title)}</h${level}>`);
                return;
            }
        }
        const unordered = line.match(/^[-*•]\s+(.+)$/);
        if (unordered) {
            openList("ul");
            blocks.push(`<li>${formatWebInlineText(unordered[1])}</li>`);
            return;
        }
        const ordered = line.match(/^\d+[.)、]\s+(.+)$/);
        if (ordered) {
            openList("ol");
            blocks.push(`<li>${formatWebInlineText(ordered[1])}</li>`);
            return;
        }
        closeList();
        blocks.push(`<p>${formatWebInlineText(line)}</p>`);
    });
    if (inCode) blocks.push(`<pre><code>${esc(codeLines.join("\n"))}</code></pre>`);
    closeList();
    return `<div class="web-reply">${blocks.join("")}</div>`;
}

function textify(v) {
    if (v === null || v === undefined) return "";
    if (typeof v === "string") return v;
    try { return JSON.stringify(v); } catch { return String(v); }
}

function shortText(v, n = 80) {
    const s = textify(v);
    return s.length > n ? `${s.slice(0, n)}...` : s;
}

function isNonExtractableInsight(item) {
    const markers = [
        "本件無可擷取之實務見解",
        "本判決無可擷取之實務見解",
        "本裁定無可擷取之實務見解",
        "無可擷取之實務見解",
        "無可擷取實務見解",
        "無實務見解",
        "沒有實務見解",
        "未擷取實務見解",
        "不能擷取之實務見解",
        "不可擷取之實務見解",
        "原始資料未提供全文文字",
        "已保留原始資料",
        "請提供您需要我摘要的判決書全文",
        "請您提供需要我處理的判決書全文",
        "請您提供需要分析的判決書全文",
        "請您提供判決書全文",
        "請您現在貼上判決書",
        "請將判決書貼於此",
        "判決書貼於下方"
    ];
    const noInsightMarkers = ["無實務見解", "無可擷取", "不能擷取", "不可擷取", "未擷取"];
    const promptEchoMarkers = [
        "請您現在貼上",
        "請將判決書貼",
        "判決書貼於下方",
        "我已理解",
        "我將會",
        "我將立即",
        "我將為您",
        "AI助理",
        "作為MAGI",
        "MAGI系統"
    ];
    const promptEchoContextMarkers = ["判決書", "實務見解", "引用裁判", "適用法條", "逐字擷取", "嚴格依照", "輸出格式"];
    const text = [
        item?.title,
        item?.summary,
        item?.insight_text,
        item?.full_text,
        item?.case_reason,
        item?.court,
        item?.source
    ].map(v => String(v || "")).join("").replace(/\s+/g, "");
    if (!text) return true;
    return markers.some(m => text.includes(m)) ||
        (text.includes("程序性文書") && noInsightMarkers.some(m => text.includes(m))) ||
        (promptEchoMarkers.some(m => text.includes(m)) && promptEchoContextMarkers.some(m => text.includes(m)));
}

function filterDisplayableInsights(items) {
    return (items || []).filter(item => !isNonExtractableInsight(item));
}

function isLocalConsole() {
    const host = (window.location.hostname || "").toLowerCase();
    return host === "localhost" || host === "127.0.0.1" || host === "::1";
}

function fileContentUrl(path, inline = false) {
    const q = encodeURIComponent(String(path || "").trim());
    return `/api/osc/files/content?path=${q}${inline ? "&inline=1" : ""}`;
}

function filePreviewUrl(path) {
    const q = encodeURIComponent(String(path || "").trim());
    return `/api/osc/files/preview?path=${q}`;
}

function isFileAuthRedirect(response) {
    if (!response) return false;
    // V2 currently redirects unauthenticated browser requests to /login.  V3
    // may eventually use a JSON 401 for API requests, so the shared caller
    // must fail closed for both contracts.
    if (response.status === 401) return true;
    if (response.type === "opaqueredirect" || response.status === 0) return true;
    if (response.status >= 300 && response.status < 400) return true;
    if (!response.redirected || !response.url) return false;
    try {
        return new URL(response.url, window.location.origin).pathname === "/login";
    } catch (_) {
        return false;
    }
}

function rejectFileAuthRedirect(response) {
    if (!isFileAuthRedirect(response)) return;
    if (typeof _handleSessionExpired === "function") _handleSessionExpired();
    const error = new Error("登入已逾時，正在跳轉登入頁...");
    error.code = "authentication_required";
    throw error;
}

async function fetchFileRoute(url, options = {}) {
    const suppliedHeaders = options && options.headers ? options.headers : {};
    const response = await fetch(url, {
        ...(options || {}),
        credentials: "same-origin",
        headers: csrfHeaders(suppliedHeaders),
        redirect: "manual",
    });
    rejectFileAuthRedirect(response);
    return response;
}

function fileRouteName(path) {
    return String(path || "").replace(/[\\/]+$/, "").split(/[\\/]/).filter(Boolean).pop() || "檔案";
}

function _fileRouteStatus(message, isError, options = {}) {
    if (typeof options.onStatus === "function") {
        options.onStatus(message, !!isError);
        return;
    }
    if (typeof showToast === "function") {
        showToast(message, isError ? "error" : "ok", 3200);
    }
}

function _fileRouteFailureText(action, error) {
    // Response/exception details can include internal paths or trace IDs.
    // Authentication is already handled by rejectFileAuthRedirect; all other
    // failures get a useful, stable instruction instead of raw diagnostics.
    if (error && error.code === "authentication_required") return "登入已逾時，正在跳轉登入頁…";
    return `${action}暫時無法完成，請稍後再試。`;
}

async function startFileDownload(fullPath, name, options = {}) {
    const path = String(fullPath || "").trim();
    if (!path) {
        _fileRouteStatus("下載失敗：缺少檔案路徑", true, options);
        return false;
    }
    const url = fileContentUrl(path);
    try {
        const probe = await fetchFileRoute(url, {
            method: "HEAD",
            headers: { Accept: "application/octet-stream" },
        });
        if (!probe.ok) throw new Error(`HTTP ${probe.status}`);
    } catch (error) {
        _fileRouteStatus(_fileRouteFailureText("下載", error), true, options);
        return false;
    }
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = name || fileRouteName(path);
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    return true;
}

let _oscFilePreviewBlobUrl = null;

function _clearFilePreviewBlob() {
    if (!_oscFilePreviewBlobUrl) return;
    URL.revokeObjectURL(_oscFilePreviewBlobUrl);
    _oscFilePreviewBlobUrl = null;
}

function _renderFilePdfPreview(src) {
    const safeSrc = esc(src || "");
    return '<div class="fm-preview-mobile-help">'
        + "<span>PDF 可在此上下滑動預覽；若手機瀏覽器顯示空白，請改用開新分頁或下載。</span>"
        + '<a class="fm-preview-open-link" href="' + safeSrc + '" target="_blank" rel="noopener">開新分頁</a>'
        + "</div>"
        + '<iframe class="fm-preview-pdf" title="PDF 預覽" src="' + safeSrc + '"></iframe>';
}

function _renderFileJsonError(payload) {
    return '<div class="fm-empty">預覽失敗：' + esc(payload?.error || "unknown") + "<br><br>"
        + '<button type="button" class="btn-mini" data-fm-preview-download>⬇ 直接下載原檔</button></div>';
}

function _renderFileCsvPreview(payload) {
    const headers = payload.headers || [];
    const rows = payload.rows || [];
    let html = '<div class="fm-preview-section"><span class="label">列數</span><span class="val">'
        + rows.length + (payload.truncated ? "+ (前 500 列)" : "") + "</span></div>";
    html += '<div style="overflow:auto;"><table class="fm-preview-table">';
    if (headers.length) {
        html += "<thead><tr>";
        headers.forEach(header => { html += "<th>" + esc(header) + "</th>"; });
        html += "</tr></thead>";
    }
    html += "<tbody>";
    rows.forEach(row => {
        html += "<tr>";
        row.forEach(cell => { html += '<td title="' + esc(cell) + '">' + esc(cell) + "</td>"; });
        html += "</tr>";
    });
    return html + "</tbody></table></div>";
}

function _renderFileEmailPreview(payload) {
    let html = "";
    ["from", "to", "cc", "subject", "date"].forEach(key => {
        const value = payload[key] || "";
        if (!value) return;
        html += '<div class="fm-preview-section"><span class="label">' + key.toUpperCase()
            + '</span><span class="val">' + esc(value) + "</span></div>";
    });
    if (payload.attachments && payload.attachments.length) {
        html += '<div class="fm-preview-section"><span class="label">附件</span><span class="val">'
            + payload.attachments.length + "</span></div><ul class=\"fm-preview-attachments\">";
        payload.attachments.forEach(attachment => {
            html += "<li>📎 " + esc(attachment.filename || "(unnamed)")
                + ' <span style="color:#888;font-size:11px;">(' + esc(attachment.content_type || "")
                + (attachment.size ? ", " + Math.round(attachment.size / 1024) + " KB" : "") + ")</span></li>";
        });
        html += "</ul>";
    }
    const body = payload.body_text || payload.body_html || "";
    if (!body) return html;
    if (payload.body_html) {
        return html + '<div class="fm-preview-section"><span class="label">內文 (HTML)</span></div>'
            + '<iframe class="fm-preview-iframe" sandbox srcdoc="' + esc(body)
            + '" style="height:60vh;border-top:1px solid #eee;"></iframe>';
    }
    return html + '<div class="fm-preview-section"><span class="label">內文</span></div>'
        + '<pre class="fm-preview-text">' + esc(body) + "</pre>";
}

function _renderFileZipPreview(payload) {
    const items = payload.items || [];
    let html = '<div class="fm-preview-section"><span class="label">項目數</span><span class="val">'
        + items.length + (payload.truncated ? "+" : "") + "</span></div>";
    html += '<div style="overflow:auto;"><table class="fm-preview-table">'
        + "<thead><tr><th>名稱</th><th>大小</th><th>壓縮</th><th>修改</th></tr></thead><tbody>";
    items.forEach(item => {
        html += '<tr><td title="' + esc(item.name) + '">' + (item.is_dir ? "📁 " : "📄 ")
            + esc(item.name) + "</td><td>"
            + (item.size != null ? Math.round(item.size / 1024) + " KB" : "") + "</td><td>"
            + (item.compressed_size != null ? Math.round(item.compressed_size / 1024) + " KB" : "")
            + "</td><td>" + esc(item.modified || "") + "</td></tr>";
    });
    return html + "</tbody></table></div>";
}

function _renderFileHexPreview(payload, name) {
    let html = '<div class="fm-preview-section"><span class="label">檔名</span><span class="val">'
        + esc(name) + "</span></div>";
    if (payload.size != null) html += '<div class="fm-preview-section"><span class="label">大小</span><span class="val">' + payload.size + " bytes</span></div>";
    if (payload.mime) html += '<div class="fm-preview-section"><span class="label">MIME</span><span class="val">' + esc(payload.mime) + "</span></div>";
    html += '<div class="fm-preview-section"><span class="label">前 ' + (payload.shown_bytes || 256) + " bytes (hex dump)</span></div>";
    return html + '<pre class="fm-preview-text">' + esc(payload.hex || "") + "</pre>";
}

function closeFilePreview() {
    const modal = document.getElementById("fmPreviewModal");
    if (modal) modal.hidden = true;
    document.body?.classList?.remove("fm-preview-open");
    _clearFilePreviewBlob();
}

async function openFilePreview(fullPath, name, options = {}) {
    const path = String(fullPath || "").trim();
    const modal = document.getElementById("fmPreviewModal");
    const title = document.getElementById("fmPreviewTitle");
    const body = document.getElementById("fmPreviewBody");
    const download = document.getElementById("fmPreviewDownload");
    if (!path || !modal || !body) {
        _fileRouteStatus("預覽失敗：找不到預覽視窗或檔案路徑", true, options);
        return false;
    }

    ["fmPreviewMove", "fmPreviewTrash", "fmPreviewShare"].forEach(id => {
        const control = document.getElementById(id);
        if (control) control.hidden = options.fileManagerActions !== true;
    });
    modal.hidden = false;
    document.body?.classList?.add("fm-preview-open");
    if (title) title.textContent = name || fileRouteName(path);
    body.classList?.remove("padded");
    body.innerHTML = '<div class="fm-preview-loading"><div class="spinner"></div>'
        + "正在載入預覽…<br><span style=\"font-size:11px;\">Office 檔案首次轉檔需要 3–8 秒</span></div>";

    if (download) {
        download.href = fileContentUrl(path);
        download.onclick = async event => {
            event.preventDefault();
            await startFileDownload(path, name || fileRouteName(path), options);
        };
    }

    let response;
    try {
        response = await fetchFileRoute(filePreviewUrl(path));
    } catch (error) {
        body.innerHTML = '<div class="fm-empty">' + esc(_fileRouteFailureText("預覽", error)) + "</div>";
        return false;
    }

    const contentType = String(response.headers.get("Content-Type") || "").toLowerCase();
    if (!response.ok && !contentType.includes("application/json")) {
        body.innerHTML = '<div class="fm-empty">預覽失敗：HTTP ' + response.status + "</div>";
        return false;
    }
    if (!contentType.includes("application/json")) {
        const blob = await response.blob();
        if (!blob || blob.size <= 0) {
            body.innerHTML = '<div class="fm-empty">預覽回傳為空</div>';
            return false;
        }
        _clearFilePreviewBlob();
        _oscFilePreviewBlobUrl = URL.createObjectURL(blob);
        if (contentType.includes("application/pdf")) {
            body.innerHTML = _renderFilePdfPreview(_oscFilePreviewBlobUrl);
        } else if (contentType.startsWith("image/")) {
            body.classList?.add("padded");
            body.innerHTML = '<img class="fm-preview-img" src="' + esc(_oscFilePreviewBlobUrl) + '">';
        } else {
            body.innerHTML = '<embed class="fm-preview-pdf" src="' + esc(_oscFilePreviewBlobUrl)
                + '" type="' + esc(contentType) + '">';
        }
        return true;
    }

    const payload = await response.json();
    if (!response.ok || payload?.ok === false) {
        body.innerHTML = _renderFileJsonError(payload || { error: `HTTP ${response.status}` });
        const fallback = body.querySelector?.("[data-fm-preview-download]");
        if (fallback) fallback.addEventListener("click", () => startFileDownload(path, name || fileRouteName(path), options));
        return false;
    }

    const kind = payload.kind || "";
    const contentUrl = payload.content_url || fileContentUrl(path, true);
    if (["pdf", "image", "audio", "video"].includes(kind)) {
        try {
            const contentProbe = await fetchFileRoute(contentUrl, {
                method: "HEAD",
                headers: { Accept: kind === "pdf" ? "application/pdf" : "*/*" },
            });
            if (!contentProbe.ok) throw new Error(`HTTP ${contentProbe.status}`);
        } catch (error) {
            body.innerHTML = '<div class="fm-empty">' + esc(_fileRouteFailureText("預覽", error)) + "</div>";
            return false;
        }
    }
    if (kind === "pdf") body.innerHTML = _renderFilePdfPreview(contentUrl);
    else if (kind === "image") {
        body.classList?.add("padded");
        body.innerHTML = '<img class="fm-preview-img" src="' + esc(contentUrl) + '">';
    } else if (kind === "audio") {
        body.classList?.add("padded");
        body.innerHTML = '<audio class="fm-preview-media" controls src="' + esc(contentUrl) + '"></audio>';
    } else if (kind === "video") {
        body.classList?.add("padded");
        body.innerHTML = '<video class="fm-preview-media" controls src="' + esc(contentUrl) + '"></video>';
    } else if (kind === "text") {
        body.classList?.add("padded");
        try {
            const textResponse = await fetchFileRoute(contentUrl);
            if (!textResponse.ok) throw new Error(`HTTP ${textResponse.status}`);
            const text = await textResponse.text();
            body.innerHTML = '<pre class="fm-preview-text">' + esc(text.slice(0, 500000)) + "</pre>";
        } catch (error) {
            body.innerHTML = '<div class="fm-empty">' + esc(_fileRouteFailureText("文字載入", error)) + "</div>";
            return false;
        }
    } else if (kind === "csv") body.innerHTML = _renderFileCsvPreview(payload);
    else if (kind === "email") body.innerHTML = _renderFileEmailPreview(payload);
    else if (kind === "zip") body.innerHTML = _renderFileZipPreview(payload);
    else if (kind === "other") body.innerHTML = _renderFileHexPreview(payload, name || fileRouteName(path));
    else {
        body.innerHTML = '<div class="fm-empty">不支援的預覽類型：' + esc(kind) + "</div>";
        return false;
    }
    return true;
}

async function shareFileLink(path, label = "檔案") {
    const rawPath = String(path || "").trim();
    if (!rawPath) {
        showToast("請先選取要分享的檔案。", "warn");
        return null;
    }
    const resp = await fetch("/api/osc/files/share", {
        method: "POST",
        credentials: "same-origin",
        headers: csrfHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ path: rawPath }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.ok || !data.url) {
        const msg = data.error === "share_public_base_required"
            ? "尚未設定獨立分享入口。為避免洩漏 MAGI/Paperclip 主控台外網網址，請先到 MAGI 調整頁面設定分享入口。"
            : (data.message || data.error || `HTTP ${resp.status}`);
        showToast(`分享連結建立失敗：${msg}`, "error");
        return null;
    }
    try {
        await navigator.clipboard.writeText(data.url);
        showToast(`已建立並複製分享連結：${label || data.name || "檔案"}`, "ok", 3500);
    } catch {
        showCustomDialog("MAGI說｜分享連結", `
            <p>瀏覽器暫時不允許自動複製，請手動複製下列分享連結。</p>
            <input value="${esc(data.url)}" readonly style="box-sizing:border-box;width:100%;border:1px solid #d2d7df;border-radius:8px;padding:10px 12px;font-size:14px">
        `);
        showToast(`已建立分享連結：${label || data.name || "檔案"}`, "ok", 3500);
    }
    return data;
}

function isEditableTextFile(path) {
    const s = String(path || "").toLowerCase();
    return [".txt", ".md", ".json", ".csv", ".tsv", ".yaml", ".yml", ".xml", ".html", ".htm", ".log", ".py", ".js", ".ts", ".css"].some(ext => s.endsWith(ext));
}

function formatBytes(v) {
    const n = Number(v || 0);
    if (!Number.isFinite(n) || n <= 0) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let idx = 0;
    let x = n;
    while (x >= 1024 && idx < units.length - 1) {
        x /= 1024;
        idx += 1;
    }
    return idx === 0 ? `${Math.round(x)}${units[idx]}` : `${x.toFixed(1)}${units[idx]}`;
}

// === UX v3 P3: 全域 loading overlay helper ===
let _oscLoadingCount = 0;
function showLoading(text) {
    _oscLoadingCount += 1;
    const el = document.getElementById("tabLoadingOverlay");
    if (!el) return;
    const t = el.querySelector(".loading-text");
    if (t && text) t.textContent = String(text);
    el.hidden = false;
}
function hideLoading() {
    _oscLoadingCount = Math.max(0, _oscLoadingCount - 1);
    if (_oscLoadingCount > 0) return;
    const el = document.getElementById("tabLoadingOverlay");
    if (!el) return;
    el.hidden = true;
    const t = el.querySelector(".loading-text");
    if (t) t.textContent = "載入中...";
}

async function copyText(text, message = "已複製到剪貼簿。") {
    const value = String(text || "").trim();
    if (!value) return;
    try {
        await navigator.clipboard.writeText(value);
        showToast(message, "ok");
    } catch {
        showAlert("MAGI說", "複製失敗，請手動複製");
    }
}

function _csrfToken() {
    const m = document.cookie.match(/(?:^|;\s*)X-CSRF-Token=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
}

function csrfHeaders(extra = {}) {
    const headers = { ...(extra || {}) };
    const csrf = _csrfToken();
    if (csrf) headers["X-CSRF-Token"] = csrf;
    return headers;
}

function apiErrorMessage(data, fallback = "request_failed") {
    const payload = data && typeof data === "object" ? data : {};
    const detail = shortText(payload.message || payload.detail || payload.body || "", 240);
    let message = payload.error || payload.reason || fallback || "request_failed";
    if (detail && !String(message).includes(detail)) message = `${message}：${detail}`;
    return String(message || "request_failed");
}

// session expired 時直接 redirect 到 login（每 30s 內只做一次，避免 setInterval 風暴）
function _handleSessionExpired() {
    const now = Date.now();
    const last = parseInt(sessionStorage.getItem("_paperclip_session_redirect_at") || "0", 10);
    if (now - last < 30000) return;  // 30s 內已 redirect 過則跳過（讓律師有時間互動）
    sessionStorage.setItem("_paperclip_session_redirect_at", String(now));
    location.href = "/login?next=" + encodeURIComponent(location.pathname);
}

const _OSC_READONLY_FETCH_RETRY_DELAYS_MS = [120, 360];

function _oscReadableNetworkError() {
    return new Error("網路連線暫時中斷，MAGI 已自動重試；請確認連線後再試一次。");
}

async function _oscFetchWithReadonlyRetry(path, opts) {
    const method = String((opts && opts.method) || "GET").toUpperCase();
    const retryDelays = (method === "GET" || method === "HEAD")
        ? _OSC_READONLY_FETCH_RETRY_DELAYS_MS
        : [];
    let attempt = 0;
    while (true) {
        try {
            return await fetch(path, opts);
        } catch (_) {
            if (attempt >= retryDelays.length) throw _oscReadableNetworkError();
            const delay = retryDelays[attempt++];
            await new Promise(resolve => setTimeout(resolve, delay));
        }
    }
}

async function api(path, method = "GET", body = null) {
    const opts = { method, headers: csrfHeaders(), credentials: "same-origin", redirect: "manual" };  // redirect:manual 才能偵測 302
    if (body !== null) {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(body);
    }
    // 僅重試安全的唯讀請求；POST/PUT/DELETE 絕不自動重送，
    // 避免網路斷線時產生重複新增、重複移動或重複刪除。
    const res = await _oscFetchWithReadonlyRetry(path, opts);

    // 偵測 session expired：opaqueredirect（manual mode 下 302 會變這個）/ status=0 / 3xx
    if (res.type === "opaqueredirect" || res.status === 0 || (res.status >= 300 && res.status < 400)) {
        _handleSessionExpired();
        throw new Error("登入已逾時，正在跳轉登入頁...");
    }

    const txt = await res.text();

    // 雙重保險：拿到 HTML（被 redirect 跟隨後）也視為 session expired
    if (txt.trim().startsWith("<")) {
        _handleSessionExpired();
        throw new Error("登入已逾時，正在跳轉登入頁...");
    }

    let data = {};
    try { data = txt ? JSON.parse(txt) : {}; } catch { data = { ok: false, error: txt || res.statusText }; }
    const rawErr = String(data.error || "");
    if (!res.ok && (rawErr.includes("/login?next=") || rawErr.includes("Redirecting"))) {
        _handleSessionExpired();
        throw new Error("登入已逾時，正在跳轉登入頁...");
    }
    if (!res.ok) {
        throw new Error(apiErrorMessage(data, res.statusText || `HTTP ${res.status}`));
    }
    if (data && data.ok === false) {
        const err = new Error(apiErrorMessage(data, "request_failed"));
        err.payload = data;
        err.status = res.status;
        throw err;
    }
    return data;
}

async function apiForm(path, formData) {
    const res = await fetch(path, { method: "POST", credentials: "same-origin", redirect: "manual", headers: csrfHeaders(), body: formData });
    if (res.type === "opaqueredirect" || res.status === 0 || (res.status >= 300 && res.status < 400)) {
        _handleSessionExpired();
        throw new Error("登入已逾時，正在跳轉登入頁...");
    }
    const txt = await res.text();
    if (txt.trim().startsWith("<")) {
        _handleSessionExpired();
        throw new Error("登入已逾時，正在跳轉登入頁...");
    }
    let data = {};
    try { data = txt ? JSON.parse(txt) : {}; } catch { data = { ok: false, error: txt || res.statusText }; }
    const rawErr = String(data.error || "");
    if (!res.ok && (rawErr.includes("/login?next=") || rawErr.includes("Redirecting"))) {
        _handleSessionExpired();
        throw new Error("登入已逾時，正在跳轉登入頁...");
    }
    if (!res.ok) {
        const err = new Error(apiErrorMessage(data, res.statusText || "request_failed"));
        err.payload = data;
        err.status = res.status;
        throw err;
    }
    if (data && data.ok === false) {
        const err = new Error(apiErrorMessage(data, "request_failed"));
        err.payload = data;
        err.status = res.status;
        throw err;
    }
    return data;
}

const _oscNaturalCollator = new Intl.Collator("zh-Hant", {
    numeric: true,
    sensitivity: "base",
});

function naturalCompare(a, b) {
    return _oscNaturalCollator.compare(String(a ?? ""), String(b ?? ""));
}

function applySort(arr, col, dir, type) {
    if (!col || !arr.length) return arr;
    return arr.sort((a, b) => {
        let va = a[col] ?? "";
        let vb = b[col] ?? "";
        if (type === "number") {
            return (Number(va) - Number(vb)) * dir;
        } else if (type === "date") {
            return (new Date(va || 0).getTime() - new Date(vb || 0).getTime()) * dir;
        } else {
            return naturalCompare(va, vb) * dir;
        }
    });
}

function renderSortArrow(col) {
    if (state.sort.col !== col) return "";
    return state.sort.dir === 1 ? " ▲" : " ▼";
}
