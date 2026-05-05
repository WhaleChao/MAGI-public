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
