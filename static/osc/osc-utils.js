/* osc-utils.js – Utility functions */
function esc(v) {
    return String(v ?? "").replace(/[&<>\"']/g, s => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[s]));
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
        "已存原始JSON",
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

async function shareFileLink(path, label = "檔案") {
    const rawPath = String(path || "").trim();
    if (!rawPath) {
        showToast("請先選取要分享的檔案。", "warn");
        return null;
    }
    const resp = await fetch("/api/osc/files/share", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
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
        window.prompt("分享連結（不含檔案路徑）：", data.url);
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
        alert("複製失敗，請手動複製");
    }
}

function _csrfToken() {
    const m = document.cookie.match(/(?:^|;\s*)X-CSRF-Token=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
}

// session expired 時直接 redirect 到 login（每 30s 內只做一次，避免 setInterval 風暴）
function _handleSessionExpired() {
    const now = Date.now();
    const last = parseInt(sessionStorage.getItem("_paperclip_session_redirect_at") || "0", 10);
    if (now - last < 30000) return;  // 30s 內已 redirect 過則跳過（讓律師有時間互動）
    sessionStorage.setItem("_paperclip_session_redirect_at", String(now));
    location.href = "/login?next=" + encodeURIComponent(location.pathname);
}

async function api(path, method = "GET", body = null) {
    const opts = { method, headers: {}, redirect: "manual" };  // redirect:manual 才能偵測 302
    const csrf = _csrfToken();
    if (csrf) opts.headers["X-CSRF-Token"] = csrf;
    if (body !== null) {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);

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
        const detail = shortText(data.detail || data.body || "", 240);
        let message = data.error || res.statusText || `HTTP ${res.status}`;
        if (detail && !message.includes(detail)) message = `${message}：${detail}`;
        throw new Error(message);
    }
    return data;
}

async function apiForm(path, formData) {
    const hdrs = {};
    const csrf = _csrfToken();
    if (csrf) hdrs["X-CSRF-Token"] = csrf;
    const res = await fetch(path, { method: "POST", headers: hdrs, body: formData });
    const txt = await res.text();
    let data = {};
    try { data = txt ? JSON.parse(txt) : {}; } catch { data = { ok: false, error: txt || res.statusText }; }
    if (!res.ok) {
        const err = new Error(data.error || res.statusText || "request_failed");
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
