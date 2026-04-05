/* osc-utils.js – Utility functions */
function esc(v) {
    return String(v ?? "").replace(/[&<>\"']/g, s => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[s]));
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

function isLocalConsole() {
    const host = (window.location.hostname || "").toLowerCase();
    return host === "localhost" || host === "127.0.0.1" || host === "::1";
}

function fileContentUrl(path, inline = false) {
    const q = encodeURIComponent(String(path || "").trim());
    return `/api/osc/files/content?path=${q}${inline ? "&inline=1" : ""}`;
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

async function api(path, method = "GET", body = null) {
    const opts = { method, headers: {} };
    const csrf = _csrfToken();
    if (csrf) opts.headers["X-CSRF-Token"] = csrf;
    if (body !== null) {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    const txt = await res.text();
    let data = {};
    try { data = txt ? JSON.parse(txt) : {}; } catch { data = { ok: false, error: txt || res.statusText }; }
    const rawErr = String(data.error || "");
    if (!res.ok && (rawErr.includes("/login?next=") || rawErr.includes("Redirecting"))) {
        throw new Error("登入已逾時，請重新登入後再操作。");
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
            return String(va).localeCompare(String(vb), "zh-Hant") * dir;
        }
    });
}

function renderSortArrow(col) {
    if (state.sort.col !== col) return "";
    return state.sort.dir === 1 ? " ▲" : " ▼";
}
