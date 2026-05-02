/* ==========================================================================
   Paperclip NAS File Manager — Phase 1 (UI shell)
   - Sidebar item 📁 NAS 檔案 → activates this tab
   - Dual-pane: left = lazy-load tree, right = entries (folders before files)
   - Breadcrumb (clickable)
   - Hidden-file toggle, refresh, "open in case folder via case picker (TODO Phase 2)"
   - For Phase 1 only: a simple text input lets the user paste a base_path to start.
     Phase 2 will integrate with case picker / drag-drop / preview modal.
   ========================================================================== */

(function () {
    'use strict';

    const FM = window.FileManager = {
        basePath: '',          // current root (NAS path string)
        currentRel: '',        // relative path under base
        showHidden: false,
        viewMode: 'detail',    // detail | grid | compact (Phase 2 commit 7)
        sort: 'mtime_desc',    // mtime_desc | mtime_asc | name_asc | name_desc | size_desc | size_asc | type_group
        loading: false,
        lastEntries: { folders: [], files: [] },
        selectedRel: null,
        selectedType: null,
    };

    // ── Icons ──────────────────────────────────────────────────────────
    const ICON_FOLDER = '📂';
    const ICON_BY_EXT = {
        '.pdf': '📕', '.doc': '📘', '.docx': '📘',
        '.xls': '📊', '.xlsx': '📊', '.csv': '📊', '.tsv': '📊',
        '.ppt': '📙', '.pptx': '📙',
        '.jpg': '🖼', '.jpeg': '🖼', '.png': '🖼', '.gif': '🖼', '.webp': '🖼',
        '.bmp': '🖼', '.tiff': '🖼', '.tif': '🖼', '.heic': '🖼', '.heif': '🖼', '.svg': '🖼',
        '.mp3': '🎵', '.wav': '🎵', '.m4a': '🎵', '.aac': '🎵', '.flac': '🎵', '.ogg': '🎵',
        '.mp4': '🎬', '.mov': '🎬', '.webm': '🎬', '.m4v': '🎬', '.avi': '🎬', '.mkv': '🎬',
        '.zip': '🗜', '.7z': '🗜', '.rar': '🗜', '.tar': '🗜', '.gz': '🗜',
        '.eml': '📧', '.msg': '📧',
        '.txt': '📝', '.md': '📝', '.json': '📝', '.log': '📝', '.xml': '📝', '.yml': '📝', '.yaml': '📝',
        '.py': '📝', '.js': '📝', '.html': '📝', '.css': '📝', '.sql': '📝',
    };
    function iconFor(entry) {
        if (entry.type === 'dir') return ICON_FOLDER;
        return ICON_BY_EXT[(entry.ext || '').toLowerCase()] || '📄';
    }

    function escapeHTML(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function setStatus(msg, isError) {
        const el = document.getElementById('fmStatus');
        if (!el) return;
        if (!msg) { el.style.display = 'none'; el.textContent = ''; return; }
        el.style.display = 'block';
        el.className = 'fm-status' + (isError ? ' error' : '');
        el.textContent = msg;
    }

    // ── API helpers ────────────────────────────────────────────────────
    async function apiBrowse(basePath, relativePath) {
        const url = '/api/osc/folders/browse?'
            + 'base_path=' + encodeURIComponent(basePath)
            + '&relative_path=' + encodeURIComponent(relativePath || '')
            + '&show_hidden=' + (FM.showHidden ? '1' : '0');
        const r = await fetch(url, { credentials: 'same-origin' });
        return r.json();
    }

    async function apiTree(basePath, relativePath) {
        const url = '/api/osc/folders/tree?'
            + 'base_path=' + encodeURIComponent(basePath)
            + '&relative_path=' + encodeURIComponent(relativePath || '')
            + '&show_hidden=' + (FM.showHidden ? '1' : '0');
        const r = await fetch(url, { credentials: 'same-origin' });
        return r.json();
    }

    // ── Render: breadcrumb ─────────────────────────────────────────────
    function renderBreadcrumb() {
        const bc = document.getElementById('fmBreadcrumb');
        if (!bc) return;
        const parts = (FM.currentRel || '').split('/').filter(Boolean);
        const pieces = [];
        pieces.push('<span class="crumb' + (parts.length === 0 ? ' current' : '')
            + '" data-rel="">🏠 根目錄</span>');
        let acc = '';
        parts.forEach((p, i) => {
            acc = acc ? acc + '/' + p : p;
            const last = i === parts.length - 1;
            pieces.push('<span class="sep">/</span>');
            pieces.push('<span class="crumb' + (last ? ' current' : '')
                + '" data-rel="' + escapeHTML(acc) + '">' + escapeHTML(p) + '</span>');
        });
        bc.innerHTML = pieces.join('');
        bc.querySelectorAll('.crumb').forEach(el => {
            if (el.classList.contains('current')) return;
            el.addEventListener('click', () => navigateTo(el.dataset.rel || ''));
        });
    }

    // ── Sort entries (Phase 2 commit 7) ───────────────────────────────
    function sortEntries(entries) {
        const out = entries.slice();
        switch (FM.sort) {
            case 'name_asc':  out.sort((a, b) => a.name.localeCompare(b.name, 'zh-hant')); break;
            case 'name_desc': out.sort((a, b) => b.name.localeCompare(a.name, 'zh-hant')); break;
            case 'mtime_asc': out.sort((a, b) => (a.mtime_ts || 0) - (b.mtime_ts || 0)); break;
            case 'size_desc': out.sort((a, b) => (b.size || b.child_total_size || 0) - (a.size || a.child_total_size || 0)); break;
            case 'size_asc':  out.sort((a, b) => (a.size || a.child_total_size || 0) - (b.size || b.child_total_size || 0)); break;
            case 'type_group':
                out.sort((a, b) => {
                    const ea = (a.ext || '').toLowerCase();
                    const eb = (b.ext || '').toLowerCase();
                    if (ea === eb) return a.name.localeCompare(b.name, 'zh-hant');
                    return ea.localeCompare(eb);
                });
                break;
            case 'mtime_desc':
            default: out.sort((a, b) => (b.mtime_ts || 0) - (a.mtime_ts || 0));
        }
        return out;
    }

    // ── Render: entries (depending on view mode) ─────────────────────
    function renderEntries(data) {
        const main = document.getElementById('fmEntriesArea');
        if (!main) return;
        if (!data || data.ok === false) {
            main.innerHTML = '<div class="fm-empty">無法載入：' + escapeHTML((data && data.error) || '未知錯誤') + '</div>';
            return;
        }
        FM.lastEntries.folders = data.folders || [];
        FM.lastEntries.files = data.files || [];

        const folders = sortEntries(FM.lastEntries.folders);
        const files = sortEntries(FM.lastEntries.files);

        if (folders.length === 0 && files.length === 0) {
            main.innerHTML = '<div class="fm-empty">此資料夾為空</div>';
            return;
        }

        let html = '';
        if (FM.viewMode === 'grid') {
            html = renderGrid(folders, files, data);
        } else if (FM.viewMode === 'compact') {
            html = renderCompact(folders, files, data);
        } else {
            html = renderDetail(folders, files, data);
        }
        main.innerHTML = html;
        bindEntryClicks(main);
    }

    function renderDetail(folders, files, data) {
        let html = '';
        if (folders.length > 0) {
            html += '<div class="fm-section-title">📁 資料夾 (' + folders.length + ')</div>';
            html += '<table class="fm-table"><thead><tr>'
                + '<th>名稱</th><th>內容</th><th>修改時間</th></tr></thead><tbody>';
            for (const f of folders) {
                const meta = (f.child_files != null)
                    ? (f.child_files + ' 檔 ' + (f.child_folders ? '/ ' + f.child_folders + ' 子夾 ' : '')
                       + (f.child_size_label || ''))
                    : '';
                html += '<tr class="fm-row dir" data-rel="' + escapeHTML(f.relative_path) + '" data-type="dir" data-name="' + escapeHTML(f.name) + '">'
                    + '<td><span class="fm-icon">' + iconFor(f) + '</span><span class="fm-name" title="'
                    + escapeHTML(f.name) + '">' + escapeHTML(f.name) + '</span></td>'
                    + '<td class="fm-meta">' + escapeHTML(meta) + '</td>'
                    + '<td class="fm-meta">' + escapeHTML(f.modified_at || '') + '</td>'
                    + '</tr>';
            }
            html += '</tbody></table>';
        }
        if (files.length > 0) {
            html += '<div class="fm-section-title">📄 檔案 (' + files.length + ')</div>';
            html += '<table class="fm-table"><thead><tr>'
                + '<th>名稱</th><th>大小</th><th>修改時間</th></tr></thead><tbody>';
            for (const f of files) {
                html += '<tr class="fm-row file" data-rel="' + escapeHTML(f.relative_path) + '" data-type="file" data-name="' + escapeHTML(f.name) + '">'
                    + '<td><span class="fm-icon">' + iconFor(f) + '</span><span class="fm-name" title="'
                    + escapeHTML(f.name) + '">' + escapeHTML(f.name) + '</span></td>'
                    + '<td class="fm-meta">' + escapeHTML(f.size_label || '') + '</td>'
                    + '<td class="fm-meta">' + escapeHTML(f.modified_at || '') + '</td>'
                    + '</tr>';
            }
            html += '</tbody></table>';
        }
        if (data.hidden_count && !FM.showHidden) html += hiddenHint(data.hidden_count);
        return html;
    }

    function renderGrid(folders, files, data) {
        let html = '';
        if (folders.length > 0) {
            html += '<div class="fm-section-title">📁 資料夾 (' + folders.length + ')</div>';
            html += '<div class="fm-grid">';
            for (const f of folders) html += gridItem(f);
            html += '</div>';
        }
        if (files.length > 0) {
            html += '<div class="fm-section-title">📄 檔案 (' + files.length + ')</div>';
            html += '<div class="fm-grid">';
            for (const f of files) html += gridItem(f);
            html += '</div>';
        }
        if (data.hidden_count && !FM.showHidden) html += hiddenHint(data.hidden_count);
        return html;
    }

    function gridItem(f) {
        const meta = f.type === 'dir'
            ? ((f.child_files != null) ? (f.child_files + ' 檔') : '')
            : (f.size_label || '');
        return '<div class="fm-grid-item ' + f.type + '" data-rel="' + escapeHTML(f.relative_path)
            + '" data-type="' + f.type + '" data-name="' + escapeHTML(f.name) + '">'
            + '<span class="fm-grid-icon">' + iconFor(f) + '</span>'
            + '<div class="fm-grid-name" title="' + escapeHTML(f.name) + '">' + escapeHTML(f.name) + '</div>'
            + '<div class="fm-grid-meta">' + escapeHTML(meta) + '</div>'
            + '</div>';
    }

    function renderCompact(folders, files, data) {
        let html = '<ul class="fm-compact">';
        for (const f of folders) html += compactItem(f);
        for (const f of files) html += compactItem(f);
        html += '</ul>';
        if (data.hidden_count && !FM.showHidden) html += hiddenHint(data.hidden_count);
        return html;
    }

    function compactItem(f) {
        const meta = f.type === 'dir'
            ? ((f.child_files != null) ? (f.child_files + ' 檔') : '')
            : (f.size_label || '');
        return '<li class="fm-compact-item ' + f.type + '" data-rel="' + escapeHTML(f.relative_path)
            + '" data-type="' + f.type + '" data-name="' + escapeHTML(f.name) + '">'
            + '<span class="fm-icon">' + iconFor(f) + '</span>'
            + '<span class="fm-compact-name" title="' + escapeHTML(f.name) + '">' + escapeHTML(f.name) + '</span>'
            + '<span class="fm-compact-meta">' + escapeHTML(meta) + '</span>'
            + '</li>';
    }

    function hiddenHint(n) {
        return '<div class="fm-empty" style="font-size:11px;padding:8px;">'
            + '隱藏 ' + n + ' 個系統暫存檔（.DS_Store / ~$tmp / Thumbs.db 等）'
            + ' — 勾選「顯示暫存檔」可顯示</div>';
    }

    function bindEntryClicks(main) {
        const allItems = main.querySelectorAll('[data-rel][data-type]');
        allItems.forEach(el => {
            const rel = el.dataset.rel;
            const type = el.dataset.type;
            el.addEventListener('click', (ev) => {
                if (ev.shiftKey || ev.metaKey || ev.ctrlKey) return;
                selectEntry(rel, type, el);
                if (type === 'dir') {
                    navigateTo(rel);
                } else {
                    // Preview will be wired in Phase 2 commit 8
                    if (typeof openPreview === 'function') openPreview(rel, el.dataset.name);
                    else setStatus('檔案預覽功能將於下個 commit 啟用。');
                }
            });
        });
    }

    function selectEntry(rel, type, el) {
        FM.selectedRel = rel;
        FM.selectedType = type;
        const main = document.getElementById('fmEntriesArea');
        if (main) main.querySelectorAll('.selected').forEach(n => n.classList.remove('selected'));
        if (el) el.classList.add('selected');
    }

    // ── Render: tree (lazy-load) ──────────────────────────────────────
    async function renderTreeRoot() {
        const root = document.getElementById('fmTree');
        if (!root) return;
        root.innerHTML = '<div class="fm-loading-inline">載入樹狀…</div>';
        const data = await apiTree(FM.basePath, '');
        if (!data || data.ok === false) {
            root.innerHTML = '<div class="fm-empty">樹狀載入失敗：' + escapeHTML((data && data.error) || '') + '</div>';
            return;
        }
        root.innerHTML = '';
        const homeNode = makeTreeNode({ name: '🏠 根目錄', relative_path: '', has_subdirs: true }, true);
        root.appendChild(homeNode);
        const childrenWrap = document.createElement('div');
        childrenWrap.className = 'fm-tree-children';
        homeNode.appendChild(childrenWrap);
        for (const child of (data.children || [])) {
            childrenWrap.appendChild(makeTreeNode(child, false));
        }
    }

    function makeTreeNode(node, isRoot) {
        const wrap = document.createElement('div');
        const head = document.createElement('div');
        head.className = 'fm-tree-node';
        head.dataset.rel = node.relative_path || '';
        const tw = document.createElement('span');
        tw.className = 'tw';
        tw.textContent = node.has_subdirs ? '▶' : '';
        head.appendChild(tw);
        const lbl = document.createElement('span');
        lbl.textContent = (isRoot ? '' : '📁 ') + node.name;
        head.appendChild(lbl);
        wrap.appendChild(head);

        let expanded = false;
        let childWrap = null;
        head.addEventListener('click', async (ev) => {
            ev.stopPropagation();
            if (!isRoot) navigateTo(node.relative_path || '');
            highlightTree(node.relative_path || '');
            if (!node.has_subdirs) return;
            if (!expanded) {
                expanded = true;
                tw.textContent = '▼';
                childWrap = document.createElement('div');
                childWrap.className = 'fm-tree-children';
                childWrap.innerHTML = '<div class="fm-loading-inline">…</div>';
                wrap.appendChild(childWrap);
                const data = await apiTree(FM.basePath, node.relative_path || '');
                childWrap.innerHTML = '';
                if (data && data.ok && (data.children || []).length) {
                    for (const c of data.children) {
                        childWrap.appendChild(makeTreeNode(c, false));
                    }
                } else {
                    childWrap.innerHTML = '<div class="fm-loading-inline">（無子資料夾）</div>';
                }
            } else {
                expanded = false;
                tw.textContent = '▶';
                if (childWrap) { childWrap.remove(); childWrap = null; }
            }
        });
        return wrap;
    }

    function highlightTree(rel) {
        const root = document.getElementById('fmTree');
        if (!root) return;
        root.querySelectorAll('.fm-tree-node').forEach(n => {
            if ((n.dataset.rel || '') === (rel || '')) n.classList.add('active');
            else n.classList.remove('active');
        });
    }

    // ── Navigation ────────────────────────────────────────────────────
    async function navigateTo(rel) {
        if (!FM.basePath) return;
        if (FM.loading) return;
        FM.loading = true;
        setStatus('載入中…');
        const data = await apiBrowse(FM.basePath, rel);
        FM.loading = false;
        if (!data || data.ok === false) {
            setStatus('載入失敗：' + ((data && data.error) || '未知錯誤'), true);
            renderEntries(data);
            return;
        }
        FM.currentRel = data.current_relative_path || '';
        setStatus('');
        renderBreadcrumb();
        renderEntries(data);
        highlightTree(FM.currentRel);
    }

    async function setRoot(basePath) {
        if (!basePath) {
            setStatus('請輸入 NAS 案件資料夾路徑（例：Z:\\lumi63181107\\01_案件\\...）', true);
            return;
        }
        FM.basePath = basePath;
        FM.currentRel = '';
        setStatus('解析路徑…');
        await renderTreeRoot();
        await navigateTo('');
    }

    // ── Preview Modal (Phase 2 commit 8) ──────────────────────────────
    async function apiPreview(filePath) {
        const url = '/api/osc/files/preview?path=' + encodeURIComponent(filePath);
        const r = await fetch(url, { credentials: 'same-origin' });
        const ct = (r.headers.get('Content-Type') || '').toLowerCase();
        if (ct.includes('application/json')) {
            return { json: await r.json(), blob: null, contentType: ct, status: r.status };
        }
        return { json: null, blob: await r.blob(), contentType: ct, status: r.status };
    }

    function buildLocalPath(rel) {
        const sep = FM.basePath.includes('\\') ? '\\' : '/';
        const r = (rel || '').replace(/\//g, sep);
        if (!r) return FM.basePath;
        return FM.basePath.replace(/[\\/]+$/, '') + sep + r;
    }

    let _previewBlobUrl = null;
    function clearPreviewBlob() {
        if (_previewBlobUrl) { URL.revokeObjectURL(_previewBlobUrl); _previewBlobUrl = null; }
    }

    async function openPreview(rel, name) {
        const modal = document.getElementById('fmPreviewModal');
        const title = document.getElementById('fmPreviewTitle');
        const body = document.getElementById('fmPreviewBody');
        const dl = document.getElementById('fmPreviewDownload');
        if (!modal || !body) return;
        modal.hidden = false;
        title.textContent = name || rel;
        body.classList.remove('padded');
        body.innerHTML = '<div class="fm-preview-loading"><div class="spinner"></div>'
            + '正在載入預覽…<br><span style="font-size:11px;">Office 檔案首次轉檔需要 3–8 秒</span></div>';

        const fullPath = buildLocalPath(rel);
        if (dl) dl.href = '/api/osc/files/content?path=' + encodeURIComponent(fullPath);

        let res;
        try {
            res = await apiPreview(fullPath);
        } catch (e) {
            body.innerHTML = '<div class="fm-empty">預覽失敗：' + escapeHTML(String(e && e.message || e)) + '</div>';
            return;
        }

        if (res.blob && res.blob.size > 0) {
            clearPreviewBlob();
            _previewBlobUrl = URL.createObjectURL(res.blob);
            const ct = res.contentType;
            if (ct.includes('application/pdf')) {
                body.innerHTML = '<embed class="fm-preview-pdf" type="application/pdf" src="' + _previewBlobUrl + '">';
            } else if (ct.startsWith('image/')) {
                body.classList.add('padded');
                body.innerHTML = '<img class="fm-preview-img" src="' + _previewBlobUrl + '">';
            } else {
                body.innerHTML = '<embed class="fm-preview-pdf" src="' + _previewBlobUrl + '" type="' + ct + '">';
            }
            return;
        }

        const j = res.json;
        if (!j) { body.innerHTML = '<div class="fm-empty">預覽回傳為空</div>'; return; }
        if (j.ok === false) { body.innerHTML = renderJsonError(j, fullPath); return; }
        const kind = j.kind || '';
        if (kind === 'pdf' || kind === 'image' || kind === 'audio' || kind === 'video' || kind === 'text') {
            const url = j.content_url || ('/api/osc/files/content?path=' + encodeURIComponent(fullPath) + '&inline=1');
            if (kind === 'pdf') {
                body.innerHTML = '<embed class="fm-preview-pdf" type="application/pdf" src="' + url + '">';
            } else if (kind === 'image') {
                body.classList.add('padded');
                body.innerHTML = '<img class="fm-preview-img" src="' + url + '">';
            } else if (kind === 'audio') {
                body.classList.add('padded');
                body.innerHTML = '<audio class="fm-preview-media" controls src="' + url + '"></audio>';
            } else if (kind === 'video') {
                body.classList.add('padded');
                body.innerHTML = '<video class="fm-preview-media" controls src="' + url + '"></video>';
            } else if (kind === 'text') {
                body.classList.add('padded');
                try {
                    const tr = await fetch(url, { credentials: 'same-origin' });
                    const txt = await tr.text();
                    body.innerHTML = '<pre class="fm-preview-text">' + escapeHTML(txt.slice(0, 500000)) + '</pre>';
                } catch (e) {
                    body.innerHTML = '<div class="fm-empty">文字載入失敗</div>';
                }
            }
            return;
        }
        if (kind === 'csv') { body.innerHTML = renderCsvPreview(j); return; }
        if (kind === 'email') { body.innerHTML = renderEmailPreview(j); return; }
        if (kind === 'zip') { body.innerHTML = renderZipPreview(j); return; }
        if (kind === 'other') { body.innerHTML = renderHexPreview(j, name || rel); return; }
        body.innerHTML = '<div class="fm-empty">不支援的預覽類型：' + escapeHTML(kind) + '</div>';
    }

    function renderJsonError(j, fullPath) {
        return '<div class="fm-empty">預覽失敗：' + escapeHTML(j.error || 'unknown') + '<br><br>'
            + '<a class="btn-mini" href="/api/osc/files/content?path=' + encodeURIComponent(fullPath)
            + '" download>⬇ 直接下載原檔</a></div>';
    }

    function renderCsvPreview(j) {
        const headers = j.headers || [];
        const rows = j.rows || [];
        let html = '<div class="fm-preview-section"><span class="label">列數</span><span class="val">'
            + rows.length + (j.truncated ? '+ (前 500 列)' : '') + '</span></div>';
        html += '<div style="overflow:auto;"><table class="fm-preview-table">';
        if (headers.length) {
            html += '<thead><tr>';
            headers.forEach(h => html += '<th>' + escapeHTML(h) + '</th>');
            html += '</tr></thead>';
        }
        html += '<tbody>';
        rows.forEach(r => {
            html += '<tr>';
            r.forEach(c => html += '<td title="' + escapeHTML(c) + '">' + escapeHTML(c) + '</td>');
            html += '</tr>';
        });
        html += '</tbody></table></div>';
        return html;
    }

    function renderEmailPreview(j) {
        let html = '';
        ['from', 'to', 'cc', 'subject', 'date'].forEach(k => {
            const v = j[k] || '';
            if (!v) return;
            html += '<div class="fm-preview-section"><span class="label">' + k.toUpperCase()
                + '</span><span class="val">' + escapeHTML(v) + '</span></div>';
        });
        if (j.attachments && j.attachments.length) {
            html += '<div class="fm-preview-section"><span class="label">附件</span><span class="val">'
                + j.attachments.length + '</span></div>';
            html += '<ul class="fm-preview-attachments">';
            j.attachments.forEach(a => {
                html += '<li>📎 ' + escapeHTML(a.filename || '(unnamed)')
                    + ' <span style="color:#888;font-size:11px;">(' + escapeHTML(a.content_type || '')
                    + (a.size ? ', ' + Math.round(a.size / 1024) + ' KB' : '') + ')</span></li>';
            });
            html += '</ul>';
        }
        const body = j.body_text || j.body_html || '';
        if (body) {
            if (j.body_html) {
                html += '<div class="fm-preview-section"><span class="label">內文 (HTML)</span></div>';
                html += '<iframe class="fm-preview-iframe" sandbox srcdoc="' + escapeHTML(body)
                    + '" style="height:60vh;border-top:1px solid #eee;"></iframe>';
            } else {
                html += '<div class="fm-preview-section"><span class="label">內文</span></div>';
                html += '<pre class="fm-preview-text">' + escapeHTML(body) + '</pre>';
            }
        }
        return html;
    }

    function renderZipPreview(j) {
        const items = j.items || [];
        let html = '<div class="fm-preview-section"><span class="label">項目數</span><span class="val">'
            + items.length + (j.truncated ? '+' : '') + '</span></div>';
        html += '<div style="overflow:auto;"><table class="fm-preview-table">'
            + '<thead><tr><th>名稱</th><th>大小</th><th>壓縮</th><th>修改</th></tr></thead><tbody>';
        items.forEach(it => {
            html += '<tr><td title="' + escapeHTML(it.name) + '">' + (it.is_dir ? '📁 ' : '📄 ')
                + escapeHTML(it.name) + '</td>'
                + '<td>' + (it.size != null ? Math.round(it.size / 1024) + ' KB' : '') + '</td>'
                + '<td>' + (it.compressed_size != null ? Math.round(it.compressed_size / 1024) + ' KB' : '') + '</td>'
                + '<td>' + escapeHTML(it.modified || '') + '</td></tr>';
        });
        html += '</tbody></table></div>';
        return html;
    }

    function renderHexPreview(j, name) {
        let html = '<div class="fm-preview-section"><span class="label">檔名</span><span class="val">'
            + escapeHTML(name) + '</span></div>';
        if (j.size != null) html += '<div class="fm-preview-section"><span class="label">大小</span><span class="val">' + j.size + ' bytes</span></div>';
        if (j.mime) html += '<div class="fm-preview-section"><span class="label">MIME</span><span class="val">' + escapeHTML(j.mime) + '</span></div>';
        html += '<div class="fm-preview-section"><span class="label">前 ' + (j.shown_bytes || 256) + ' bytes (hex dump)</span></div>';
        html += '<pre class="fm-preview-text">' + escapeHTML(j.hex || '') + '</pre>';
        return html;
    }

    function closePreview() {
        const modal = document.getElementById('fmPreviewModal');
        if (modal) modal.hidden = true;
        clearPreviewBlob();
    }

    // ── Public init (called when sidebar tab activates) ───────────────
    FM.init = function () {
        const inp = document.getElementById('fmBasePathInput');
        const goBtn = document.getElementById('fmBasePathGoBtn');
        const refreshBtn = document.getElementById('fmRefreshBtn');
        const hiddenToggle = document.getElementById('fmShowHiddenToggle');

        if (goBtn && !goBtn._fmBound) {
            goBtn._fmBound = true;
            goBtn.addEventListener('click', () => setRoot((inp && inp.value || '').trim()));
            if (inp) inp.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') setRoot(inp.value.trim());
            });
        }
        if (refreshBtn && !refreshBtn._fmBound) {
            refreshBtn._fmBound = true;
            refreshBtn.addEventListener('click', () => navigateTo(FM.currentRel));
        }
        if (hiddenToggle && !hiddenToggle._fmBound) {
            hiddenToggle._fmBound = true;
            hiddenToggle.addEventListener('change', () => {
                FM.showHidden = !!hiddenToggle.checked;
                if (FM.basePath) navigateTo(FM.currentRel);
            });
        }

        // Phase 2 commit 7: view modes + sort
        const sortSelect = document.getElementById('fmSortSelect');
        const viewBtns = document.querySelectorAll('.fm-view-btn');
        if (sortSelect && !sortSelect._fmBound) {
            sortSelect._fmBound = true;
            sortSelect.addEventListener('change', () => {
                FM.sort = sortSelect.value;
                renderEntries({
                    folders: FM.lastEntries.folders,
                    files: FM.lastEntries.files,
                    hidden_count: 0, ok: true,
                });
            });
        }
        viewBtns.forEach(b => {
            if (b._fmBound) return;
            b._fmBound = true;
            b.addEventListener('click', () => {
                viewBtns.forEach(x => x.classList.remove('active'));
                b.classList.add('active');
                FM.viewMode = b.dataset.view;
                renderEntries({
                    folders: FM.lastEntries.folders,
                    files: FM.lastEntries.files,
                    hidden_count: 0, ok: true,
                });
            });
        });

        // Phase 2 commit 8: preview modal close handlers
        const previewClose = document.getElementById('fmPreviewClose');
        const previewModal = document.getElementById('fmPreviewModal');
        if (previewClose && !previewClose._fmBound) {
            previewClose._fmBound = true;
            previewClose.addEventListener('click', closePreview);
        }
        if (previewModal && !previewModal._fmBound) {
            previewModal._fmBound = true;
            previewModal.addEventListener('click', (ev) => {
                if (ev.target.classList.contains('fm-modal-backdrop')) closePreview();
            });
        }
        if (!document._fmEscBound) {
            document._fmEscBound = true;
            document.addEventListener('keydown', (ev) => {
                if (ev.key !== 'Escape') return;
                const m = document.getElementById('fmPreviewModal');
                if (m && !m.hidden) closePreview();
            });
        }
    };

    // Auto-init when this tab becomes visible
    document.addEventListener('DOMContentLoaded', () => {
        FM.init();
    });

    // Allow other tabs to open this view with a preset case folder
    FM.openWithBasePath = function (basePath) {
        const inp = document.getElementById('fmBasePathInput');
        if (inp) inp.value = basePath;
        setRoot(basePath);
    };
})();
