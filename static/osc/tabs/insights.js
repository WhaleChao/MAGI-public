/* tabs/insights.js – Legal insights */
async function loadInsights() {
    const q = encodeURIComponent((document.getElementById("insightsQ").value || "").trim());
    const data = await api(`/api/osc/insights?q=${q}`);
    state.insights = data.items || [];
    renderInsights();
}

function renderInsights() {
    const body = document.getElementById("insightsBody");
    if (!state.insights.length) {
        body.innerHTML = `<tr><td colspan="6" class="muted">沒有資料</td></tr>`;
        return;
    }
    const sorted = applySort([...state.insights], state.sort.col, state.sort.dir, state.sort.type);
    body.innerHTML = sorted.map(r => {
        const hasUrl = !!(r.url || "").trim();
        const hasLookupKeys = hasUrl || !!(r.case_number || "").trim() || !!(r.title || "").trim();
        const actions = [
            `<button class="btn" data-act="insight-toggle" data-id="${esc(r.id)}">展開/收合</button>`,
            `<button class="btn" data-act="insight-copy" data-id="${esc(r.id)}">複製全文</button>`
        ];
        if (hasLookupKeys) {
            actions.push(`<button class="btn" data-act="insight-fetch" data-id="${esc(r.id)}">抓全文摘要</button>`);
        }
        if (hasUrl) {
            actions.push(`<a class="btn ghost" target="_blank" href="${esc(r.url)}">來源</a>`);
        }
        return `
        <tr>
            <td>${esc(r.timestamp)}</td>
            <td>${esc(r.source)}</td>
            <td>${esc(r.title)}</td>
            <td>${esc(r.case_number)}</td>
            <td>${esc(r.summary)}</td>
            <td class="actions">${actions.join("")}</td>
        </tr>
        <tr id="insightRow_${esc(r.id)}" style="display:none;">
            <td colspan="6"><div class="insight-full">${esc(r.full_text || "(無全文)")}</div></td>
        </tr>
    `;
    }).join("");

    const ts = document.querySelectorAll("#insights th[data-sort]");
    ts.forEach(th => {
        th.innerHTML = th.innerHTML.replace(/ [▲▼]/g, "") + renderSortArrow(th.dataset.sort);
    });
}

function needHydrateInsight(item) {
    if (!item) return false;
    const hasUrl = !!String(item.url || "").trim();
    const full = String(item.full_text || "");
    if (!full.trim()) return true;
    if (full.length < 260) return true;
    return hasUrl;
}

async function hydrateInsightByDetail(id) {
    const idx = state.insights.findIndex(x => String(x.id) === String(id));
    if (idx < 0) return;
    const item = state.insights[idx];
    if (!needHydrateInsight(item)) return;
    const detail = await api(`/api/osc/insights/${encodeURIComponent(id)}`);
    const d = detail.item || {};
    const fullText = (d.raw_text || d.full_text || d.insight_text || item.full_text || "").trim();
    const summary = (d.insight_text || d.summary || item.summary || "").trim();
    state.insights[idx] = { ...item, full_text: fullText, summary: summary || fullText.slice(0, 350) };
    const tr = document.getElementById(`insightRow_${id}`);
    if (tr) {
        const box = tr.querySelector(".insight-full");
        if (box) box.textContent = state.insights[idx].full_text || "(無全文)";
    }
}

async function toggleInsight(id) {
    const item = state.insights.find(x => String(x.id) === String(id));
    if (needHydrateInsight(item)) {
        try {
            await hydrateInsightByDetail(id);
            const refreshedItem = state.insights.find(x => String(x.id) === String(id));
            if (
                needHydrateInsight(refreshedItem) &&
                refreshedItem &&
                (
                    (refreshedItem.url || "").trim() ||
                    (refreshedItem.case_number || "").trim() ||
                    (refreshedItem.title || "").trim()
                )
            ) {
                await fetchInsightFullById(id, { silent: true });
                const refreshed = await api(`/api/osc/insights?q=${encodeURIComponent((document.getElementById("insightsQ").value || "").trim())}`);
                state.insights = refreshed.items || [];
            }
        } catch (e) {
            alert(`抓全文失敗，先顯示現有內容：${e.message}`);
        }
    }
    const tr = document.getElementById(`insightRow_${id}`);
    if (!tr) return;
    tr.style.display = (tr.style.display === "none" || !tr.style.display) ? "table-row" : "none";
}

async function copyInsight(id) {
    const item = state.insights.find(x => String(x.id) === String(id));
    const text = (item?.full_text || item?.summary || "").trim();
    if (!text) return alert("沒有可複製內容");
    try {
        await navigator.clipboard.writeText(text);
        showToast("見解全文已複製到剪貼簿。", "ok");
    } catch {
        alert("複製失敗，請手動複製");
    }
}

async function fetchInsightFullById(id, opts = {}) {
    const silent = !!opts.silent;
    const item = state.insights.find(x => String(x.id) === String(id));
    if (!item) return;
    const body = {
        url: (item.url || "").trim(),
        title: (item.title || "").trim(),
        case_number: (item.case_number || "").trim(),
        case_reason: (item.case_reason || "").trim(),
    };
    if (!body.url && !body.case_number && !body.title) {
        if (!silent) alert("這筆見解沒有可用的來源網址、標題或案號，無法補抓全文。");
        return;
    }
    const data = await api("/api/osc/insights/fetch-full", "POST", body);
    if (!silent) {
        const source = (data?.item?.source || "").trim();
        showToast(source ? `已補抓判決全文（${source}）。` : "已補抓判決全文並寫入見解庫。", "ok");
    }
    await loadInsights();
    await loadMeta();
    if (data?.item?.summary) {
        msg("sys", `已更新見解：${item.title || item.case_number || "裁判見解"}`);
    }
}

async function fetchInsightFullManual() {
    const body = {
        url: (document.getElementById("insight_fetch_url").value || "").trim(),
        title: (document.getElementById("insight_title").value || "").trim(),
        case_number: (document.getElementById("insight_case_number").value || "").trim(),
        case_reason: (document.getElementById("insight_case_reason").value || "").trim(),
    };
    if (!body.url && !body.case_number && !body.title) {
        return alert("請至少輸入來源網址、標題或案件編號");
    }
    const data = await api("/api/osc/insights/fetch-full", "POST", body);
    await loadInsights();
    await loadMeta();
    const source = (data?.item?.source || "").trim();
    showToast(source ? `全文與摘要已完成（${source}）。` : "全文與摘要已完成。", "ok");
}

async function saveInsight() {
    const body = {
        case_number: (document.getElementById("insight_case_number").value || "").trim(),
        title: (document.getElementById("insight_title").value || "").trim(),
        case_reason: (document.getElementById("insight_case_reason").value || "").trim(),
        insight_text: (document.getElementById("insight_text").value || "").trim(),
    };
    if (!body.insight_text) return alert("請輸入見解全文");
    await api("/api/osc/insights", "POST", body);
    document.getElementById("insight_text").value = "";
    await loadInsights();
    await loadMeta();
}
