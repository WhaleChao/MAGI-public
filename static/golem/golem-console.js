(function () {
    const $ = (id) => document.getElementById(id);
    const terminal = $("terminal-output");
    const diagnostic = $("diagnostic-summary");
    const THEME_STORAGE_KEY = "magi.golem.theme";
    const SKILL_LABELS = {
        web_search: "網路搜尋",
        deep_research: "深度研究",
        fetch_url: "讀取網站內容",
        analyze_image: "圖片辨識",
        summarize_text: "文字摘要",
        translate_text: "翻譯",
        ocr_image: "圖片文字辨識",
        parse_document: "文件解析",
        search_memory: "記憶查找",
        stock_analysis: "股票分析",
    };
    const SKILL_PURPOSES = {
        web_search: "查找最新資訊、新聞與公開資料。",
        deep_research: "整合多個來源，產出較完整的研究整理。",
        fetch_url: "讀取指定網站內容並交給 MAGI 分析。",
        analyze_image: "辨識圖片內容、截圖資訊或影像中的文字。",
        summarize_text: "把長篇文字整理成重點摘要。",
        translate_text: "協助翻譯文字內容。",
        ocr_image: "從圖片或掃描檔擷取文字。",
        parse_document: "讀取文件內容並整理可用資訊。",
        search_memory: "從既有記憶與資料庫中查找相關內容。",
        stock_analysis: "整理股票與市場分析報告。",
    };
    const SAGE_LABELS = {
        casper: "CASPER 主控",
        balthasar: "BALTHASAR 推論",
        melchior: "MELCHIOR 分析",
        shared: "MAGI 共用",
    };

    function initThemeToggle() {
        const button = $("themeToggleBtn");
        if (!button) return;
        const readStoredTheme = () => {
            try {
                return localStorage.getItem(THEME_STORAGE_KEY);
            } catch (error) {
                return null;
            }
        };
        const storeTheme = (theme) => {
            try {
                localStorage.setItem(THEME_STORAGE_KEY, theme);
            } catch (error) {
                // Theme persistence is optional; the page should still work in restricted browser contexts.
            }
        };
        const applyTheme = (theme) => {
            const dark = theme === "dark";
            document.body.classList.toggle("theme-dark", dark);
            button.textContent = dark ? "☀️" : "🌙";
            button.setAttribute("aria-label", dark ? "切換日間模式" : "切換夜間模式");
            button.title = dark ? "切換日間模式" : "切換夜間模式";
        };
        const saved = readStoredTheme() || "light";
        applyTheme(saved);
        button.addEventListener("click", () => {
            const next = document.body.classList.contains("theme-dark") ? "light" : "dark";
            storeTheme(next);
            applyTheme(next);
        });
    }

    function initLiveClock() {
        const clock = $("top-clock");
        if (!clock) return;
        let lastText = "";
        const render = () => {
            const now = new Date();
            const text = new Intl.DateTimeFormat("zh-TW", {
                year: "numeric",
                month: "2-digit",
                day: "2-digit",
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit",
                hour12: false,
            }).format(now);
            if (text !== lastText) {
                clock.setAttribute("datetime", now.toISOString());
                clock.textContent = text;
                lastText = text;
            }
        };
        render();
        window.setInterval(render, 1000);
    }

    function fmtBytes(value) {
        const n = Number(value || 0);
        if (n < 1024) return `${n} B`;
        if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
        return `${(n / 1024 / 1024).toFixed(1)} MB`;
    }

    function writeTerminal(label, payload) {
        const body = typeof payload === "string" ? payload : toChineseSummary(payload);
        const target = diagnostic || terminal;
        if (target) target.textContent = `[${new Date().toLocaleTimeString()}] ${label}\n${body}`;
    }

    function normalizeCommand(command) {
        const text = String(command || "").trim().toLowerCase();
        const map = {
            "系統狀態": "status",
            "狀態": "status",
            "技能清單": "skills",
            "技能": "skills",
            "最近紀錄": "logs",
            "紀錄": "logs",
            "股票報告": "market",
            "股票": "market",
            "向量記憶": "memory",
            "記憶": "memory",
        };
        return map[text] || text || "status";
    }

    function commandLabel(command) {
        return {
            status: "系統狀態",
            skills: "技能清單",
            logs: "最近紀錄",
            market: "股票報告",
            memory: "向量記憶",
        }[command] || command;
    }

    function toChineseSummary(payload) {
        if (!payload || typeof payload !== "object") return String(payload || "");
        const lines = [];
        if (payload.process) {
            const summary = payload.process.summary || payload.process;
            lines.push("服務狀態：");
            lines.push(`- 核心程序：${summary.core_count || 0}`);
            lines.push(`- 背景工作：${summary.worker_count || 0}`);
            lines.push(`- 孤兒程序：${summary.orphan_count || 0}`);
            lines.push(`- 重複程序群組：${summary.duplicate_groups || 0}`);
        }
        if (payload.memory) {
            lines.push("", "向量記憶：");
            lines.push(`- 文件數：${payload.memory.doc_count || 0}`);
            lines.push(`- 來源數：${payload.memory.source_count || 0}`);
            lines.push(`- 更新時間：${payload.memory.updated || "尚無資料"}`);
        }
        if (payload.skills && Array.isArray(payload.skills.items)) {
            lines.push("", "技能清單：");
            payload.skills.items.slice(0, 10).forEach((item) => {
                lines.push(`- ${humanSkillName(item)}：${humanSkillPurpose(item)}`);
            });
        }
        if (Array.isArray(payload.items)) {
            lines.push("", "技能清單：");
            payload.items.slice(0, 10).forEach((item) => {
                lines.push(`- ${humanSkillName(item)}：${humanSkillPurpose(item)}`);
            });
        }
        if (Array.isArray(payload.market_reports)) {
            lines.push("", "股票完整報告：");
            if (!payload.market_reports.length) {
                lines.push("- 尚無匯出報告");
            } else {
                payload.market_reports.slice(0, 8).forEach((file) => {
                    lines.push(`- ${file.name} (${file.mtime || ""})`);
                });
            }
        }
        if (payload.latest_market_report) {
            lines.push("", `最新股票報告：${payload.latest_market_report.name || "尚無"}`);
        }
        if (payload.server || payload.daemon || payload.market) {
            lines.push("最近紀錄已更新到下方紀錄區。");
        }
        if (payload.message) lines.push(String(payload.message));
        if (payload.error) lines.push(`錯誤：${payload.error}`);
        return lines.filter((line, idx, arr) => line || arr[idx - 1]).join("\n") || JSON.stringify(payload, null, 2);
    }

    async function fetchJson(url, options) {
        const response = await fetch(url, options);
        const text = await response.text();
        let data = {};
        try {
            data = text ? JSON.parse(text) : {};
        } catch (error) {
            data = { ok: false, error: text || String(error) };
        }
        if (!response.ok) {
            throw new Error(data.error || data.message || `HTTP ${response.status}`);
        }
        return data;
    }

    function renderNodes(process) {
        const host = process && process.summary ? process.summary : {};
        const core = Array.isArray(process && process.core) ? process.core : [];
        const rows = [
            { name: "核心程序", value: `${host.core_count || 0} 個運作中`, down: !host.core_count },
            { name: "背景工作", value: `${host.worker_count || 0} 個執行中`, down: false },
            { name: "孤兒程序", value: `${host.orphan_count || 0} 個需留意`, down: Number(host.orphan_count || 0) > 0 },
            { name: "重複程序", value: `${host.duplicate_groups || 0} 組`, down: Number(host.duplicate_groups || 0) > 0 },
        ];
        const coreRows = core.slice(0, 5).map((item) => ({
            name: item.label || "程序",
            value: `PID ${item.pid} / ${item.age || "--"}`,
            down: false,
        }));
        $("node-list").innerHTML = rows.concat(coreRows).map((row) => `
            <div class="node-item ${row.down ? "down" : ""}">
                <div class="node-name">${escapeHtml(row.name)}</div>
                <div class="node-meta">${escapeHtml(row.value)}</div>
            </div>
        `).join("");
        renderMagiTriad(host);
    }

    function renderMagiTriad(summary) {
        const online = Number(summary && summary.core_count ? summary.core_count : 0) > 0;
        const labels = [
            ["status-local-melchior", "melchior-node"],
            ["status-local-balthasar", "balthasar-node"],
            ["status-local-casper", "casper-node"],
        ];
        labels.forEach(([statusId, className]) => {
            const status = $(statusId);
            const node = document.querySelector(`.${className}`);
            if (status) status.textContent = online ? "運作中" : "需檢查";
            if (node) {
                node.classList.toggle("online", online);
                node.classList.toggle("warn", !online);
            }
        });
        const count = $("vote-count-element");
        if (count) count.textContent = online ? "3" : "0";
        const center = document.querySelector(".magi-center");
        if (center) center.classList.toggle("online", online);
    }

    function humanSkillName(item) {
        const raw = String((item && item.name) || "").trim();
        if (!raw) return "未命名技能";
        if (SKILL_LABELS[raw]) return SKILL_LABELS[raw];
        const text = raw.toLowerCase();
        if (text.includes("research")) return "深度研究";
        if (text.includes("search")) return "資料搜尋";
        if (text.includes("fetch") || text.includes("url")) return "網站讀取";
        if (text.includes("image") || text.includes("vision")) return "圖片辨識";
        if (text.includes("ocr")) return "文字辨識";
        if (text.includes("summar")) return "摘要整理";
        if (text.includes("translat")) return "翻譯";
        if (text.includes("client")) return "客戶資料";
        if (text.includes("meeting") || text.includes("calendar")) return "行程安排";
        if (text.includes("mail") || text.includes("gmail")) return "郵件處理";
        if (text.includes("judgment")) return "裁判查詢";
        if (text.includes("statute")) return "法規查詢";
        if (text.includes("ocr")) return "文字辨識";
        if (text.includes("export")) return "匯出文件";
        if (text.includes("translate")) return "翻譯";
        if (text.includes("doc") || text.includes("pdf") || text.includes("file")) return "文件處理";
        if (text.includes("case") || text.includes("laf") || text.includes("law")) return "案件與法律資料";
        if (text.includes("market") || text.includes("stock")) return "股票分析";
        if (text.includes("memory")) return "記憶查找";
        return "MAGI 技能";
    }

    function humanSkillPurpose(item) {
        const raw = String((item && item.name) || "").trim();
        if (SKILL_PURPOSES[raw]) return SKILL_PURPOSES[raw];
        const desc = String((item && item.description) || "").toLowerCase();
        const haystack = `${raw.toLowerCase()} ${desc}`;
        if (haystack.includes("research")) return "整理多個來源，形成較完整的分析。";
        if (haystack.includes("search")) return "查找外部資料並回傳重點。";
        if (haystack.includes("url") || haystack.includes("website")) return "讀取指定網站內容。";
        if (haystack.includes("image") || haystack.includes("vision")) return "辨識圖片、截圖或影像內容。";
        if (haystack.includes("ocr")) return "從圖片或掃描資料擷取文字。";
        if (haystack.includes("summar")) return "將長內容整理成容易閱讀的摘要。";
        if (haystack.includes("translat")) return "翻譯文字並保留原意。";
        if (haystack.includes("client")) return "協助查找或整理客戶與案件資料。";
        if (haystack.includes("meeting") || haystack.includes("calendar")) return "協助處理行程與時間安排。";
        if (haystack.includes("mail") || haystack.includes("gmail")) return "協助處理郵件內容。";
        if (haystack.includes("document") || haystack.includes("pdf") || haystack.includes("file")) return "協助讀取、整理或產生文件。";
        if (haystack.includes("stock") || haystack.includes("market")) return "整理市場資訊與分析結果。";
        if (haystack.includes("memory")) return "查找 MAGI 已儲存的資料。";
        return "提供 MAGI 自動化工作能力。";
    }

    function skillCategory(item) {
        const raw = `${String((item && item.name) || "")} ${String((item && item.description) || "")}`.toLowerCase();
        if (raw.includes("client") || raw.includes("case") || raw.includes("laf") || raw.includes("law") || raw.includes("judgment") || raw.includes("statute")) return "案件與法律";
        if (raw.includes("doc") || raw.includes("pdf") || raw.includes("file") || raw.includes("ocr") || raw.includes("summar") || raw.includes("translat")) return "文件處理";
        if (raw.includes("search") || raw.includes("research") || raw.includes("url") || raw.includes("web")) return "資料查找";
        if (raw.includes("meeting") || raw.includes("calendar") || raw.includes("mail") || raw.includes("gmail")) return "辦公協作";
        if (raw.includes("stock") || raw.includes("market")) return "市場分析";
        if (raw.includes("memory")) return "記憶";
        return "系統工具";
    }

    function humanSkillOwner(item) {
        const sage = String((item && item.sage) || "shared").toLowerCase();
        return SAGE_LABELS[sage] || "MAGI 共用";
    }

    function renderSkills(skills) {
        const items = Array.isArray(skills && skills.items) ? skills.items : [];
        const count = skills && skills.count ? skills.count : items.length;
        const countEl = $("skill-count");
        if (countEl) countEl.textContent = String(count || 0);
        const summaryEl = $("skill-summary");
        if (summaryEl) {
            const groups = items.reduce((acc, item) => {
                const key = skillCategory(item);
                acc[key] = (acc[key] || 0) + 1;
                return acc;
            }, {});
            summaryEl.innerHTML = Object.entries(groups).slice(0, 6).map(([name, total]) => `
                <div class="skill-summary-item">
                    <strong>${escapeHtml(total)}</strong>
                    <span>${escapeHtml(name)}</span>
                </div>
            `).join("") || `<div class="skill-summary-item"><strong>0</strong><span>尚無能力資料</span></div>`;
        }
    }

    async function loadFullSkills(expectedCount) {
        const limit = Math.max(75, Number(expectedCount || 75));
        try {
            const data = await fetchJson(`/api/golem/skills?limit=${encodeURIComponent(limit)}`);
            renderSkills({
                items: Array.isArray(data.items) ? data.items : [],
                count: expectedCount || (Array.isArray(data.items) ? data.items.length : 0),
            });
        } catch (error) {
            writeTerminal("技能清單", `完整能力清單讀取失敗：${error.message}`);
        }
    }

    function renderFiles(id, files, emptyText) {
        const rows = Array.isArray(files) ? files : [];
        $(id).innerHTML = rows.map((file) => `
            <div class="file-item">
                <div class="file-name"><a href="${escapeAttr(file.url || "#")}" target="_blank" rel="noopener">${escapeHtml(file.name || "")}</a></div>
                <div class="file-meta">${escapeHtml(file.mtime || "")} · ${fmtBytes(file.size)}</div>
            </div>
        `).join("") || `<div class="file-item"><div class="file-meta">${escapeHtml(emptyText)}</div></div>`;
    }

    function renderApiKeys(items) {
        const list = $("api-key-list");
        const state = $("api-key-state");
        const rows = Array.isArray(items) ? items : [];
        const nvidia = rows.find((item) => item.id === "nvidia_nim") || rows[0] || {};
        if (state) {
            state.textContent = nvidia.configured ? (nvidia.enabled ? "已啟用" : "已設定") : "未設定";
        }
        if (!list) return;
        list.innerHTML = rows.map((item) => `
            <div class="api-key-item">
                <div class="api-key-name">${escapeHtml(item.label || item.env_key || "")}</div>
                <div class="api-key-meta">
                    ${escapeHtml(item.env_key || "")} · ${item.configured ? "已設定 " + escapeHtml(item.masked || "") : "未設定"} · ${item.enabled ? "啟用中" : "停用"}
                </div>
            </div>
        `).join("") || `<div class="api-key-item"><div class="api-key-meta">尚無可管理的 API 金鑰</div></div>`;
        const enable = $("nvidia-api-enable");
        if (enable && typeof nvidia.enabled === "boolean") enable.checked = nvidia.enabled;
    }

    function setApiKeyMessage(text, tone) {
        const el = $("api-key-message");
        if (!el) return;
        el.className = `form-message ${tone || ""}`.trim();
        el.textContent = text || "";
    }

    function renderLogs(data) {
        $("server-log").textContent = (data.server || []).slice(-40).join("\n") || "尚無網頁服務紀錄";
        $("daemon-log").textContent = (data.daemon || []).slice(-40).join("\n") || "尚無守護程序紀錄";
    }

    function escapeHtml(value) {
        return String(value || "").replace(/[&<>"']/g, (ch) => ({
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;",
        }[ch]));
    }

    function escapeAttr(value) {
        return escapeHtml(value).replace(/`/g, "&#96;");
    }

    async function refreshStatus() {
        const data = await fetchJson("/api/golem/status");
        const pill = $("golem-health-pill");
        pill.textContent = data.ok ? "運作中" : "需檢查";
        pill.classList.toggle("warn", !data.ok);
        $("golem-root").textContent = `${data.hostname || "localhost"} · ${data.root || ""} · ${data.ts || ""}`;
        renderNodes(data.process || {});
        renderSkills(data.skills || {});
        const expectedSkillCount = Number(data.skills && data.skills.count ? data.skills.count : 0);
        if (expectedSkillCount > ((data.skills && data.skills.items && data.skills.items.length) || 0)) {
            loadFullSkills(expectedSkillCount);
        }
        renderApiKeys(data.api_keys || []);
        renderFiles("market-list", data.market_reports || [], "尚無股票完整報告");
        renderFiles("export-list", data.exports || [], "尚無近期匯出檔");
        writeTerminal("系統狀態", {
            process: data.process && data.process.summary,
            memory: data.memory,
            latest_market_report: data.market_reports && data.market_reports[0],
        });
    }

    async function refreshLogs() {
        const data = await fetchJson("/api/golem/logs");
        renderLogs(data);
    }

    async function saveNvidiaApiKey(event) {
        event.preventDefault();
        const input = $("nvidia-api-key");
        const enable = $("nvidia-api-enable");
        const apiKey = (input && input.value ? input.value : "").trim();
        if (!apiKey) {
            setApiKeyMessage("請先貼上 nvapi- 開頭的新金鑰。", "warn");
            return;
        }
        setApiKeyMessage("正在儲存...", "");
        try {
            const data = await fetchJson("/api/golem/api-keys", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ id: "nvidia_nim", api_key: apiKey, enable: !!(enable && enable.checked) }),
            });
            input.value = "";
            renderApiKeys(data.item ? [data.item] : []);
            setApiKeyMessage("已儲存 NVIDIA NIM 金鑰。背景服務若已載入舊環境，請重啟 MAGI 後完全套用。", "ok");
        } catch (error) {
            const msg = String(error.message || "");
            if (msg.includes("admin_required")) {
                setApiKeyMessage("只有管理員可以更新 API 金鑰。", "warn");
            } else if (msg.includes("invalid_prefix")) {
                setApiKeyMessage("NVIDIA NIM 金鑰格式不正確，應以 nvapi- 開頭。", "warn");
            } else {
                setApiKeyMessage(`儲存失敗：${msg}`, "warn");
            }
        }
    }

    async function runCommand(command) {
        const data = await fetchJson("/api/golem/command", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ command }),
        });
        writeTerminal(commandLabel(command), data);
        if (data.process || data.skills || data.items || data.market_reports || data.memory || data.server || data.daemon) {
            if (data.process) renderNodes(data.process);
            if (data.skills) renderSkills(data.skills);
            if (data.items) renderSkills({ items: data.items, count: data.count || data.items.length });
            if (data.market_reports) renderFiles("market-list", data.market_reports, "尚無股票完整報告");
            if (data.server || data.daemon) renderLogs(data);
        }
    }

    function appendMagiMessage(role, text) {
        const box = $("magi-chat-messages");
        if (!box) return;
        const row = document.createElement("div");
        row.className = `chat-message ${role}`;
        row.textContent = text;
        box.appendChild(row);
        box.scrollTop = box.scrollHeight;
    }

    async function sendMagiChat(event) {
        event.preventDefault();
        const input = $("magi-chat-input");
        const fileInput = $("magi-chat-file");
        const state = $("magi-chat-state");
        const text = String((input && input.value) || "").trim();
        const file = fileInput && fileInput.files && fileInput.files.length ? fileInput.files[0] : null;
        if (!text && !file) return;
        appendMagiMessage("user", file ? `${text || "請處理這份檔案"}\n附件：${file.name}` : text);
        input.value = "";
        if (state) state.textContent = "思考中";
        try {
            let data;
            if (file) {
                const form = new FormData();
                form.append("message", text || "請摘要這份檔案，並整理可供後續翻譯或分析的重點。");
                form.append("file", file);
                data = await fetchJson("/api/osc/chat/upload", {
                    method: "POST",
                    body: form,
                });
                clearMagiChatFile();
            } else {
                data = await fetchJson("/api/osc/chat", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ message: text }),
                });
            }
            appendMagiMessage("assistant", data.reply || "MAGI 已收到，但沒有回傳內容。");
            if (state) state.textContent = "就緒";
        } catch (error) {
            appendMagiMessage("system", `送出失敗：${error.message}`);
            if (state) state.textContent = "需檢查";
        }
    }

    function clearMagiChatFile() {
        const fileInput = $("magi-chat-file");
        const fileName = $("magi-chat-file-name");
        if (fileInput) fileInput.value = "";
        if (fileName) fileName.textContent = "尚未選擇檔案";
    }

    document.querySelectorAll("[data-command]").forEach((button) => {
        button.addEventListener("click", () => runCommand(button.dataset.command).catch((error) => writeTerminal("錯誤", error.message)));
    });

    const commandForm = $("command-form");
    if (commandForm) {
        commandForm.addEventListener("submit", (event) => {
            event.preventDefault();
            const input = $("command-input");
            const command = normalizeCommand(input ? input.value : "");
            runCommand(command).catch((error) => writeTerminal("錯誤", error.message));
        });
    }

    const refreshBtn = $("refresh-btn");
    if (refreshBtn) refreshBtn.addEventListener("click", () => refreshStatus().catch((error) => writeTerminal("錯誤", error.message)));
    const apiKeyForm = $("api-key-form");
    if (apiKeyForm) apiKeyForm.addEventListener("submit", saveNvidiaApiKey);
    const magiChatForm = $("magi-chat-form");
    if (magiChatForm) magiChatForm.addEventListener("submit", sendMagiChat);
    const magiChatInput = $("magi-chat-input");
    if (magiChatInput) {
        magiChatInput.addEventListener("keydown", (event) => {
            if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                magiChatForm && magiChatForm.requestSubmit();
            }
        });
    }
    const magiChatFile = $("magi-chat-file");
    if (magiChatFile) {
        magiChatFile.addEventListener("change", () => {
            const fileName = $("magi-chat-file-name");
            const file = magiChatFile.files && magiChatFile.files.length ? magiChatFile.files[0] : null;
            if (fileName) fileName.textContent = file ? `${file.name} (${fmtBytes(file.size)})` : "尚未選擇檔案";
        });
    }
    const magiChatClear = $("magi-chat-clear");
    if (magiChatClear) {
        magiChatClear.addEventListener("click", () => {
            const box = $("magi-chat-messages");
            if (box) box.innerHTML = '<div class="chat-message system">對話已清空，可直接輸入新的指示。</div>';
            clearMagiChatFile();
        });
    }
    const logDetails = document.querySelector(".log-details");
    if (logDetails) {
        logDetails.addEventListener("toggle", () => {
            if (logDetails.open) refreshLogs().catch((error) => writeTerminal("錯誤", error.message));
        });
    }

    initThemeToggle();
    initLiveClock();
    refreshStatus().catch((error) => writeTerminal("錯誤", error.message));
})();
