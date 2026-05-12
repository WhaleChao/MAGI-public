/* ==========================================================================
   Paperclip NAS File Manager — Phase 1 (UI shell)
   - Sidebar item 📁 檔案管理 → activates this tab
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
        viewMode: (() => {
            try { return localStorage.getItem('fmViewMode') || 'detail'; }
            catch (_) { return 'detail'; }
        })(),    // detail | grid | compact
        sort: 'mtime_desc',    // mtime_desc | mtime_asc | name_asc | name_desc | size_desc | size_asc | type_group
        rootLabel: '',
        loading: false,
        lastEntries: { folders: [], files: [] },
        lastHiddenCount: 0,
        hasLoadedEntries: false,
        navSeq: 0,
        treeSeq: 0,
        selectedRel: null,
        selectedType: null,
        selectedName: '',
        movePending: null,
        lastCaseResults: [],
        driveRoots: [],
    };

    // ── Icons ──────────────────────────────────────────────────────────
    const ICON_FOLDER = '📂';
    const ICON_BY_EXT = {
        '.pdf': '📕', '.doc': '📘', '.docx': '📘', '.odt': '📘',
        '.xls': '📊', '.xlsx': '📊', '.csv': '📊', '.tsv': '📊', '.ods': '📊', '.numbers': '📊',
        '.ppt': '📙', '.pptx': '📙', '.odp': '📙', '.key': '📙',
        '.jpg': '🖼', '.jpeg': '🖼', '.png': '🖼', '.gif': '🖼', '.webp': '🖼',
        '.bmp': '🖼', '.tiff': '🖼', '.tif': '🖼', '.heic': '🖼', '.heif': '🖼', '.svg': '🖼', '.ico': '🖼',
        '.mp3': '🎵', '.wav': '🎵', '.m4a': '🎵', '.aac': '🎵', '.flac': '🎵', '.ogg': '🎵', '.opus': '🎵',
        '.mp4': '🎬', '.mov': '🎬', '.webm': '🎬', '.m4v': '🎬', '.avi': '🎬', '.mkv': '🎬', '.flv': '🎬',
        '.zip': '🗜', '.7z': '🗜', '.rar': '🗜', '.tar': '🗜', '.gz': '🗜', '.bz2': '🗜', '.xz': '🗜',
        '.eml': '📧', '.msg': '📧', '.mbox': '📧',
        '.txt': '📝', '.md': '📝', '.json': '📝', '.log': '📝', '.xml': '📝', '.yml': '📝', '.yaml': '📝',
        '.py': '📝', '.js': '📝', '.ts': '📝', '.html': '📝', '.css': '📝', '.sql': '📝', '.sh': '📝',
        '.exe': '⚙', '.dmg': '💿', '.iso': '💿', '.app': '⚙', '.dll': '⚙',
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

    function pathBaseName(path) {
        return String(path || '').replace(/[\\/]+$/, '').split(/[\\/]/).filter(Boolean).pop() || '';
    }

    function caseRootLabel(meta) {
        const m = meta || {};
        const label = String(m.label || '').trim();
        if (label) return label;
        const caseNumber = String(m.caseNumber || m.case_number || '').trim();
        const clientName = String(m.clientName || m.client_name || '').trim();
        return [caseNumber, clientName].filter(Boolean).join(' ');
    }

    function rootDisplayName() {
        return FM.rootLabel || pathBaseName(FM.basePath) || '目前資料夾';
    }

    function viewModeLabel(view) {
        return view === 'grid' ? '卡片' : (view === 'compact' ? '清單' : '詳細');
    }

    function updateViewModeStatus() {
        const el = document.getElementById('fmViewModeStatus');
        if (el) el.textContent = '目前：' + viewModeLabel(FM.viewMode);
        const main = document.getElementById('fmEntriesArea');
        if (main) main.dataset.viewMode = FM.viewMode;
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

    async function apiCaseSearch(query) {
        const q = (query || '').trim();
        const statusScope = q ? 'all' : 'working';
        const url = '/api/osc/cases?limit=120&category=全部&status_scope=' + encodeURIComponent(statusScope)
            + (q ? '&q=' + encodeURIComponent(q) : '');
        const r = await fetch(url, { credentials: 'same-origin' });
        return r.json();
    }

    async function apiDriveRoots() {
        const r = await fetch('/api/osc/folders/roots', { credentials: 'same-origin' });
        return r.json();
    }

    function caseResultMeta(c) {
        return [
            c.case_number,
            c.laf_case_no ? '法扶 ' + c.laf_case_no : '',
            c.court_case_no,
            c.case_reason,
        ].filter(Boolean).join(' / ');
    }

    function renderCaseResults(items, query) {
        const box = document.getElementById('fmCaseSearchResults');
        if (!box) return;
        FM.lastCaseResults = items || [];
        if (!items || !items.length) {
            const msg = (query || '').trim()
                ? '找不到符合「' + escapeHTML(query) + '」的案件。'
                : '目前沒有進行中 / 結案中的案件。';
            box.innerHTML = '<div class="fm-case-empty">' + msg + '</div>';
            return;
        }
        box.innerHTML = items.map(c => {
            const title = c.client_name || c.case_number || c.id || '未命名案件';
            const sub = caseResultMeta(c);
            const status = c.status || '進行中';
            const folderKnown = (c.folder_path || '').trim() ? '有資料夾' : '待建立資料夾';
            return '<button type="button" class="fm-case-item" data-fm-case-id="' + escapeHTML(c.id) + '">'
                + '<div class="fm-case-main">'
                + '<div class="fm-case-title" title="' + escapeHTML(title) + '">' + escapeHTML(title) + '</div>'
                + '<div class="fm-case-sub" title="' + escapeHTML(sub) + '">' + escapeHTML(sub || '尚無案號資料') + '</div>'
                + '</div>'
                + '<div class="fm-case-tags">'
                + '<span class="fm-case-status">' + escapeHTML(status) + '</span>'
                + '<span class="fm-case-folder">' + escapeHTML(folderKnown) + '</span>'
                + '</div>'
                + '</button>';
        }).join('');
        box.querySelectorAll('[data-fm-case-id]').forEach(btn => {
            btn.addEventListener('click', () => {
                const id = btn.dataset.fmCaseId || '';
                box.querySelectorAll('.fm-case-item.active').forEach(n => n.classList.remove('active'));
                btn.classList.add('active');
                if (typeof openCaseInFileManager === 'function') openCaseInFileManager(id);
            });
        });
    }

    async function searchCases(query) {
        const box = document.getElementById('fmCaseSearchResults');
        if (!box) return;
        const q = (query || '').trim();
        if (!q) {
            FM.lastCaseResults = [];
            box.innerHTML = '<div class="fm-case-empty">也可以直接從下方「進行中案件 / 已結案案件」資料夾慢慢展開。</div>';
            return;
        }
        box.innerHTML = '<div class="fm-case-empty">案件載入中...</div>';
        try {
            const data = await apiCaseSearch(q);
            renderCaseResults((data && data.items) || [], q);
        } catch (e) {
            box.innerHTML = '<div class="muted">案件入口載入失敗：' + escapeHTML(e.message || e) + '</div>';
        }
    }

    const loadCaseShortcuts = () => searchCases(document.getElementById('fmCaseSearchInput')?.value || '');

    function rootById(rootId) {
        return (FM.driveRoots || []).find(r => r.id === rootId) || null;
    }

    function renderRootBreadcrumb() {
        const bc = document.getElementById('fmBreadcrumb');
        if (!bc) return;
        bc.innerHTML = '<span class="crumb current" data-rel="">案件資料夾</span>';
    }

    function renderDriveOverview(roots) {
        const main = document.getElementById('fmEntriesArea');
        if (!main) return;
        const items = (roots || []).filter(r => r.path);
        if (!items.length) {
            main.innerHTML = '<div class="fm-empty">找不到案件母資料夾路徑，請到「進階」手動開啟路徑。</div>';
            updateSelectionControls();
            return;
        }
        main.innerHTML = '<div class="fm-drive-overview">'
            + items.map(r => {
                const childCount = (r.children || []).length;
                const status = r.exists ? (childCount + ' 個分類') : '路徑未連線';
                return '<button type="button" class="fm-drive-root-card" data-root-id="' + escapeHTML(r.id) + '">'
                    + '<span class="fm-drive-root-icon">📁</span>'
                    + '<span class="fm-drive-root-main">'
                    + '<strong>' + escapeHTML(r.label) + '</strong>'
                    + '<span>' + escapeHTML(r.folder_name || '') + '</span>'
                    + '<em>' + escapeHTML(status) + '</em>'
                    + '</span>'
                    + '</button>';
            }).join('')
            + '</div>';
        main.querySelectorAll('[data-root-id]').forEach(btn => {
            btn.addEventListener('click', () => openDriveRoot(btn.dataset.rootId || ''));
        });
        updateSelectionControls();
    }

    function renderDriveTree(roots) {
        const tree = document.getElementById('fmTree');
        if (!tree) return;
        const items = (roots || []).filter(r => r.path);
        if (!items.length) {
            tree.innerHTML = '<div class="fm-empty">找不到案件母資料夾</div>';
            return;
        }
        tree.innerHTML = '';
        items.forEach(root => tree.appendChild(makeDriveRootNode(root)));
    }

    function makeDriveRootNode(root) {
        const wrap = document.createElement('div');
        const head = document.createElement('div');
        head.className = 'fm-tree-node fm-drive-root-node';
        head.dataset.rootId = root.id || '';
        const tw = document.createElement('span');
        tw.className = 'tw';
        tw.textContent = '▶';
        head.appendChild(tw);
        const lbl = document.createElement('span');
        lbl.textContent = '📁 ' + (root.label || root.folder_name || '案件資料夾');
        head.appendChild(lbl);
        wrap.appendChild(head);

        let expanded = false;
        let childWrap = null;
        head.addEventListener('click', async () => {
            if (!root.path) return;
            if (!expanded) {
                expanded = true;
                tw.textContent = '▼';
                childWrap = document.createElement('div');
                childWrap.className = 'fm-tree-children';
                childWrap.innerHTML = '<div class="fm-loading-inline">載入分類…</div>';
                wrap.appendChild(childWrap);
                const children = (root.children && root.children.length)
                    ? root.children
                    : ((await apiTree(root.path, '')).children || []);
                childWrap.innerHTML = '';
                if (!children.length) {
                    childWrap.innerHTML = '<div class="fm-loading-inline">（沒有分類資料夾）</div>';
                } else {
                    children.forEach(c => childWrap.appendChild(makeDriveChildNode(root, c)));
                }
            } else {
                expanded = false;
                tw.textContent = '▶';
                if (childWrap) { childWrap.remove(); childWrap = null; }
            }
            openDriveRoot(root.id || '');
        });
        return wrap;
    }

    function makeDriveChildNode(root, node) {
        const wrap = document.createElement('div');
        const head = document.createElement('div');
        head.className = 'fm-tree-node';
        head.dataset.rootId = root.id || '';
        head.dataset.rel = node.relative_path || '';
        const tw = document.createElement('span');
        tw.className = 'tw';
        tw.textContent = node.has_subdirs ? '▶' : '';
        head.appendChild(tw);
        const lbl = document.createElement('span');
        lbl.textContent = '📁 ' + node.name;
        head.appendChild(lbl);
        wrap.appendChild(head);

        let expanded = false;
        let childWrap = null;
        head.addEventListener('click', async (ev) => {
            ev.stopPropagation();
            await openDrivePath(root, node.relative_path || '');
            if (!node.has_subdirs) return;
            if (!expanded) {
                expanded = true;
                tw.textContent = '▼';
                childWrap = document.createElement('div');
                childWrap.className = 'fm-tree-children';
                childWrap.innerHTML = '<div class="fm-loading-inline">…</div>';
                wrap.appendChild(childWrap);
                const data = await apiTree(root.path, node.relative_path || '');
                childWrap.innerHTML = '';
                ((data && data.children) || []).forEach(c => childWrap.appendChild(makeDriveChildNode(root, c)));
                if (!childWrap.children.length) childWrap.innerHTML = '<div class="fm-loading-inline">（無子資料夾）</div>';
            } else {
                expanded = false;
                tw.textContent = '▶';
                if (childWrap) { childWrap.remove(); childWrap = null; }
            }
        });
        return wrap;
    }

    async function loadDriveRoots() {
        const tree = document.getElementById('fmTree');
        if (tree) tree.innerHTML = '<div class="fm-loading-inline">載入母資料夾…</div>';
        try {
            const data = await apiDriveRoots();
            FM.driveRoots = (data && data.items) || [];
            renderRootBreadcrumb();
            renderDriveTree(FM.driveRoots);
            renderDriveOverview(FM.driveRoots);
        } catch (e) {
            if (tree) tree.innerHTML = '<div class="fm-empty">母資料夾載入失敗：' + escapeHTML(e.message || e) + '</div>';
        }
    }

    function showDriveOverview() {
        FM.navSeq++;
        FM.treeSeq++;
        FM.basePath = '';
        FM.currentRel = '';
        FM.rootLabel = '';
        FM.selectedRel = null;
        FM.selectedType = null;
        FM.selectedName = '';
        cancelMovePending(false);
        FM.hasLoadedEntries = false;
        FM.lastEntries = { folders: [], files: [] };
        FM.lastHiddenCount = 0;
        setStatus('');
        renderRootBreadcrumb();
        renderDriveTree(FM.driveRoots);
        renderDriveOverview(FM.driveRoots);
    }

    async function openDriveRoot(rootId) {
        const root = rootById(rootId);
        if (!root || !root.path) return;
        await setRoot(root.path, { label: root.label || root.folder_name });
    }

    async function openDrivePath(root, rel) {
        if (!root || !root.path) return;
        await setRoot(root.path, { label: root.label || root.folder_name });
        if (rel) await navigateTo(rel);
    }

    // ── Render: breadcrumb ─────────────────────────────────────────────
    function renderBreadcrumb() {
        const bc = document.getElementById('fmBreadcrumb');
        if (!bc) return;
        const parts = (FM.currentRel || '').split('/').filter(Boolean);
        const pieces = [];
        const rootName = rootDisplayName();
        pieces.push('<span class="crumb' + (parts.length === 0 ? ' current' : '')
            + '" data-rel="" title="' + escapeHTML(rootName) + '">📁 ' + escapeHTML(rootName) + '</span>');
        let acc = '';
        parts.forEach((p, i) => {
            acc = acc ? acc + '/' + p : p;
            const last = i === parts.length - 1;
            pieces.push('<span class="sep">›</span>');
            pieces.push('<span class="crumb' + (last ? ' current' : '')
                + '" data-rel="' + escapeHTML(acc) + '" title="' + escapeHTML(p) + '">' + escapeHTML(p) + '</span>');
        });
        bc.innerHTML = pieces.join('');
        bc.querySelectorAll('.crumb').forEach(el => {
            if (el.classList.contains('current')) return;
            el.addEventListener('click', () => navigateTo(el.dataset.rel || ''));
        });
    }

    // ── Sort entries (Phase 2 commit 7；2026-05-03 加入筆畫排序) ──────
    // Intl.Collator with Unicode 'stroke' collation 在 Chrome/Edge/Safari 全支援
    // （ICU CLDR co=stroke），按繁中筆畫數排序檔名首字
    const _strokeCollator = (() => {
        try { return new Intl.Collator('zh-Hant-u-co-stroke'); }
        catch (_) { return new Intl.Collator('zh-Hant'); }  // fallback
    })();
    function sortEntries(entries) {
        const out = entries.slice();
        switch (FM.sort) {
            case 'name_asc':    out.sort((a, b) => a.name.localeCompare(b.name, 'zh-Hant')); break;
            case 'name_desc':   out.sort((a, b) => b.name.localeCompare(a.name, 'zh-Hant')); break;
            case 'stroke_asc':  out.sort((a, b) => _strokeCollator.compare(a.name, b.name)); break;
            case 'stroke_desc': out.sort((a, b) => _strokeCollator.compare(b.name, a.name)); break;
            case 'mtime_asc':   out.sort((a, b) => (a.mtime_ts || 0) - (b.mtime_ts || 0)); break;
            case 'size_desc':   out.sort((a, b) => (b.size || b.child_total_size || 0) - (a.size || a.child_total_size || 0)); break;
            case 'size_asc':    out.sort((a, b) => (a.size || a.child_total_size || 0) - (b.size || b.child_total_size || 0)); break;
            case 'type_group':
                out.sort((a, b) => {
                    const ea = (a.ext || '').toLowerCase();
                    const eb = (b.ext || '').toLowerCase();
                    if (ea === eb) return a.name.localeCompare(b.name, 'zh-Hant');
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
            const friendly = (data && data.message) || (data && data.error) || '未知錯誤';
            const diagHtml = (data && data.diagnostic && data.diagnostic.candidates && data.diagnostic.candidates.length)
                ? '<details style="margin-top:8px;font-size:11px;color:#888"><summary>診斷（嘗試過的本地路徑）</summary>'
                  + data.diagnostic.candidates.map(c => '<div>' + (c.exists ? '✓' : '✗') + ' ' + escapeHTML(c.path) + '</div>').join('')
                  + '</details>'
                : '';
            main.innerHTML = '<div class="fm-empty">⚠️ ' + escapeHTML(friendly) + diagHtml + '</div>';
            updateSelectionControls();
            return;
        }
        FM.lastEntries.folders = data.folders || [];
        FM.lastEntries.files = data.files || [];
        FM.lastHiddenCount = data.hidden_count || 0;
        FM.hasLoadedEntries = true;

        const folders = sortEntries(FM.lastEntries.folders);
        const files = sortEntries(FM.lastEntries.files);

        if (folders.length === 0 && files.length === 0) {
            main.innerHTML = '<div class="fm-empty">此資料夾為空</div>';
            updateSelectionControls();
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
        if (typeof bindContextMenu === 'function') bindContextMenu();
        updateSelectionControls();
    }

    function applyViewMode(view, rerender) {
        FM.viewMode = view || 'detail';
        try { localStorage.setItem('fmViewMode', FM.viewMode); } catch (_) {}
        document.querySelectorAll('.fm-view-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.view === FM.viewMode);
        });
        updateViewModeStatus();
        const label = viewModeLabel(FM.viewMode);
        if (rerender && FM.hasLoadedEntries) {
            renderEntries({
                folders: FM.lastEntries.folders,
                files: FM.lastEntries.files,
                hidden_count: FM.lastHiddenCount,
                ok: true,
            });
            setStatus('已切換為「' + label + '」檢視。');
            setTimeout(() => setStatus(''), 1200);
        } else if (rerender) {
            setStatus('已切換為「' + label + '」檢視；開啟資料夾後會套用。');
            setTimeout(() => setStatus(''), 1600);
        }
    }

    function renderDetail(folders, files, data) {
        let html = '';
        if (folders.length > 0) {
            html += '<div class="fm-section-title">📁 資料夾 (' + folders.length + ')</div>';
            html += '<table class="fm-table"><thead><tr>'
                + '<th>名稱</th><th>內容</th><th>修改時間</th><th class="fm-actions-head">操作</th></tr></thead><tbody>';
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
                    + '<td class="fm-actions-cell">' + fileActionButtons(f) + '</td>'
                    + '</tr>';
            }
            html += '</tbody></table>';
        }
        if (files.length > 0) {
            html += '<div class="fm-section-title">📄 檔案 (' + files.length + ')</div>';
            html += '<table class="fm-table"><thead><tr>'
                + '<th>名稱</th><th>大小</th><th>修改時間</th><th class="fm-actions-head">操作</th></tr></thead><tbody>';
            for (const f of files) {
                html += '<tr class="fm-row file" data-rel="' + escapeHTML(f.relative_path) + '" data-type="file" data-name="' + escapeHTML(f.name) + '">'
                    + '<td><span class="fm-icon">' + iconFor(f) + '</span><span class="fm-name" title="'
                    + escapeHTML(f.name) + '">' + escapeHTML(f.name) + '</span></td>'
                    + '<td class="fm-meta">' + escapeHTML(f.size_label || '') + '</td>'
                    + '<td class="fm-meta">' + escapeHTML(f.modified_at || '') + '</td>'
                    + '<td class="fm-actions-cell">' + fileActionButtons(f) + '</td>'
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
            + fileActionButtons(f)
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
            + fileActionButtons(f)
            + '</li>';
    }

    function fileActionButtons(entry) {
        const rel = escapeHTML(entry.relative_path || '');
        const name = escapeHTML(entry.name || pathBaseName(entry.relative_path));
        const type = entry.type || '';
        if (type === 'dir') {
            return '<div class="fm-file-actions">'
                + '<button type="button" class="fm-action-btn" data-fm-action="open" data-rel="' + rel
                + '" data-type="dir" data-name="' + name + '" title="開啟資料夾">開啟</button>'
                + '</div>';
        }
        return '<div class="fm-file-actions">'
            + '<button type="button" class="fm-action-btn" data-fm-action="preview" data-rel="' + rel
            + '" data-type="file" data-name="' + name + '" title="預覽檔案">預覽</button>'
            + '<button type="button" class="fm-action-btn share" data-fm-action="share" data-rel="' + rel
            + '" data-type="file" data-name="' + name + '" title="建立並複製分享連結">分享</button>'
            + '<button type="button" class="fm-action-btn" data-fm-action="download" data-rel="' + rel
            + '" data-type="file" data-name="' + name + '" title="下載檔案">下載</button>'
            + '</div>';
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
                if (ev.target.closest('.fm-action-btn')) return;
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
        FM.selectedName = (el && el.dataset && el.dataset.name) || pathBaseName(rel);
        const main = document.getElementById('fmEntriesArea');
        if (main) main.querySelectorAll('.selected').forEach(n => n.classList.remove('selected'));
        if (el) el.classList.add('selected');
        updateSelectionControls();
    }

    function clearSelection() {
        FM.selectedRel = null;
        FM.selectedType = null;
        FM.selectedName = '';
        const main = document.getElementById('fmEntriesArea');
        if (main) main.querySelectorAll('.selected').forEach(n => n.classList.remove('selected'));
        updateSelectionControls();
    }

    function updateSelectionControls() {
        const nameEl = document.getElementById('fmSelectedName');
        const moveBtn = document.getElementById('fmMoveBtn');
        const trashBtn = document.getElementById('fmTrashBtn');
        const shareBtn = document.getElementById('fmShareBtn');
        const hasSelection = !!(FM.basePath && FM.selectedRel);
        const hasFileSelection = hasSelection && FM.selectedType === 'file';
        if (nameEl) {
            nameEl.textContent = hasSelection ? ('已選取：' + (FM.selectedName || pathBaseName(FM.selectedRel))) : '尚未選取檔案';
            nameEl.title = hasSelection ? FM.selectedRel : '';
        }
        if (moveBtn) moveBtn.disabled = !hasSelection;
        if (trashBtn) trashBtn.disabled = !hasSelection;
        if (shareBtn) shareBtn.disabled = !hasFileSelection;
        updateMovePendingBar();
    }

    // ── Render: tree (lazy-load) ──────────────────────────────────────
    async function renderTreeRoot() {
        const root = document.getElementById('fmTree');
        if (!root) return;
        const seq = ++FM.treeSeq;
        const baseAtStart = FM.basePath;
        root.innerHTML = '<div class="fm-loading-inline">載入樹狀…</div>';
        const data = await apiTree(FM.basePath, '');
        if (seq !== FM.treeSeq || baseAtStart !== FM.basePath) return;
        if (!data || data.ok === false) {
            const friendly = (data && data.message) || (data && data.error) || '未知錯誤';
            const diagHtml = (data && data.diagnostic && data.diagnostic.candidates && data.diagnostic.candidates.length)
                ? '<details style="margin-top:8px;font-size:11px;color:#888"><summary>診斷（嘗試過的本地路徑）</summary>'
                  + data.diagnostic.candidates.map(c => '<div>' + (c.exists ? '✓' : '✗') + ' ' + escapeHTML(c.path) + '</div>').join('')
                  + '</details>'
                : '';
            root.innerHTML = '<div class="fm-empty">⚠️ ' + escapeHTML(friendly) + diagHtml + '</div>';
            return;
        }
        root.innerHTML = '';
        const homeNode = makeTreeNode({ name: rootDisplayName(), relative_path: '', has_subdirs: true }, true);
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
        lbl.textContent = '📁 ' + node.name;
        head.appendChild(lbl);
        wrap.appendChild(head);

        let expanded = false;
        let childWrap = null;
        head.addEventListener('click', async (ev) => {
            ev.stopPropagation();
            navigateTo(node.relative_path || '');
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
        const seq = ++FM.navSeq;
        const baseAtStart = FM.basePath;
        FM.loading = true;
        FM.selectedRel = null;
        FM.selectedType = null;
        FM.selectedName = '';
        updateSelectionControls();
        setStatus('載入中…');
        const data = await apiBrowse(FM.basePath, rel);
        if (seq !== FM.navSeq || baseAtStart !== FM.basePath) return;
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

    async function setRoot(basePath, meta) {
        if (!basePath) {
            setStatus('請輸入 NAS 案件資料夾路徑（例：Z:\\MAGI_NAS_SHARE\\01_案件\\...）', true);
            return;
        }
        FM.navSeq++;
        FM.treeSeq++;
        FM.basePath = basePath;
        FM.rootLabel = caseRootLabel(meta) || pathBaseName(basePath) || '目前資料夾';
        FM.currentRel = '';
        FM.selectedRel = null;
        FM.selectedType = null;
        FM.selectedName = '';
        cancelMovePending(false);
        FM.hasLoadedEntries = false;
        FM.lastEntries = { folders: [], files: [] };
        FM.lastHiddenCount = 0;
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

    function isPdfName(nameOrRel) {
        return /\.pdf$/i.test(String(nameOrRel || ''));
    }

    function openPdfToolFromFileManager(rel) {
        const fullPath = buildLocalPath(rel);
        const pdfTab = document.querySelector('.tab-btn[data-tab="pdfTools"]');
        if (pdfTab) pdfTab.click();
        window.setTimeout(() => {
            if (typeof setPdfToolPath === 'function') {
                setPdfToolPath(fullPath);
            } else {
                setStatus('PDF 工具尚未載入，請切到 PDF 工具後再試一次。', true);
            }
        }, 80);
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

    async function refresh() {
        if (FM.basePath) await navigateTo(FM.currentRel);
    }

    // ── Rename / move-to-trash API (Phase 2 commit 11) ───────────────
    async function apiRename(basePath, relativePath, newName) {
        const r = await fetch('/api/osc/folders/rename', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ base_path: basePath, relative_path: relativePath, new_name: newName }),
        });
        return r.json();
    }
    async function apiMoveToTrash(basePath, relativePath) {
        const r = await fetch('/api/osc/folders/move', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ base_path: basePath, source_relative_path: relativePath, to_trash: true }),
        });
        return r.json();
    }
    async function apiMove(basePath, sourceRelativePath, targetRelativePath) {
        const r = await fetch('/api/osc/folders/move', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                base_path: basePath,
                source_relative_path: sourceRelativePath,
                target_relative_path: targetRelativePath || '',
            }),
        });
        return r.json();
    }
    async function apiShareFile(fullPath) {
        const r = await fetch('/api/osc/files/share', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: fullPath }),
        });
        return r.json();
    }

    async function createShareLink(rel, name) {
        if (!rel) {
            setStatus('請先選取要分享的檔案。', true);
            return;
        }
        const fullPath = buildLocalPath(rel);
        const r = await apiShareFile(fullPath);
        if (!r || !r.ok) {
            const msg = r && r.error === 'share_public_base_required'
                ? '尚未設定獨立分享入口。為避免洩漏 MAGI/Paperclip 主控台外網網址，請先到 MAGI 調整頁面設定分享入口。'
                : ((r && (r.message || r.error)) || '未知');
            setStatus('分享連結建立失敗：' + msg, true);
            return;
        }
        try {
            await navigator.clipboard.writeText(r.url);
            setStatus('已建立並複製分享連結：' + (name || pathBaseName(rel)));
        } catch (e) {
            window.prompt('分享連結（不含檔案路徑）：', r.url);
            setStatus('已建立分享連結：' + (name || pathBaseName(rel)));
        }
        setTimeout(() => setStatus(''), 3500);
    }

    function updateMovePendingBar() {
        const bar = document.getElementById('fmMovePendingBar');
        const nameEl = document.getElementById('fmMovePendingName');
        const hereBtn = document.getElementById('fmMoveHereBtn');
        if (!bar) return;
        const pending = FM.movePending;
        bar.hidden = !pending;
        if (nameEl && pending) {
            nameEl.textContent = pending.name || pathBaseName(pending.rel);
            nameEl.title = pending.rel;
        }
        if (hereBtn && pending) {
            const target = FM.currentRel || '';
            const fromParent = parentRel(pending.rel);
            const invalidNested = pending.type === 'dir' && (target === pending.rel || target.startsWith(pending.rel + '/'));
            hereBtn.disabled = !FM.basePath || target === fromParent || invalidNested;
            hereBtn.title = invalidNested ? '不能把資料夾移到自己底下' : (target === fromParent ? '已在這個資料夾' : '移到目前資料夾');
        }
    }

    function parentRel(rel) {
        const parts = String(rel || '').split('/').filter(Boolean);
        parts.pop();
        return parts.join('/');
    }

    function startMoveSelected(rel, type, name) {
        if (!FM.basePath || !rel) {
            setStatus('請先選取要移動的檔案或資料夾。', true);
            return;
        }
        FM.movePending = {
            basePath: FM.basePath,
            rel,
            type: type || FM.selectedType || 'file',
            name: name || FM.selectedName || pathBaseName(rel),
        };
        updateMovePendingBar();
        setStatus('已準備移動「' + FM.movePending.name + '」；請切到目標資料夾後按「移到目前資料夾」。');
    }

    function cancelMovePending(showStatus) {
        FM.movePending = null;
        updateMovePendingBar();
        if (showStatus) {
            setStatus('已取消移動。');
            setTimeout(() => setStatus(''), 1200);
        }
    }

    async function movePendingHere() {
        const pending = FM.movePending;
        if (!pending) return;
        if (!FM.basePath || FM.basePath !== pending.basePath) {
            setStatus('目標資料夾不在同一個案件根目錄，請重新選取。', true);
            return;
        }
        const target = FM.currentRel || '';
        const fromParent = parentRel(pending.rel);
        if (target === fromParent) {
            setStatus('這個檔案已經在目前資料夾。', true);
            return;
        }
        if (pending.type === 'dir' && (target === pending.rel || target.startsWith(pending.rel + '/'))) {
            setStatus('不能把資料夾移到自己底下。', true);
            return;
        }
        const r = await apiMove(FM.basePath, pending.rel, target);
        if (r && r.ok) {
            const movedName = pending.name || pathBaseName(pending.rel);
            FM.movePending = null;
            clearSelection();
            setStatus('已移動到目前資料夾：' + movedName);
            await navigateTo(target);
            setTimeout(() => setStatus(''), 2500);
        } else {
            setStatus('移動失敗：' + ((r && r.error) || '未知'), true);
        }
        updateMovePendingBar();
    }

    // ── Context menu + keyboard (Phase 2 commit 11) ──────────────────
    function bindContextMenu() {
        const main = document.getElementById('fmEntriesArea');
        if (!main) return;
        if (main._fmCtxBound) return;
        main._fmCtxBound = true;
        main.addEventListener('contextmenu', (ev) => {
            const el = ev.target.closest('[data-rel][data-type]');
            if (!el) return;
            ev.preventDefault();
            const rel = el.dataset.rel;
            const type = el.dataset.type;
            const name = el.dataset.name;
            selectEntry(rel, type, el);
            openContextMenu(ev.clientX, ev.clientY, rel, type, name);
        });
    }

    function openContextMenu(x, y, rel, type, name) {
        const menu = document.getElementById('fmContextMenu');
        if (!menu) return;
        const items = [];
        if (type === 'file') {
            items.push({ label: '👁 預覽', act: 'preview' });
            if (isPdfName(name || rel)) items.push({ label: 'PDF 工具', act: 'pdf-tool' });
            items.push({ label: '⬇ 下載', act: 'download' });
            items.push({ label: '🔗 分享連結', act: 'share' });
            items.push({ sep: true });
        }
        items.push({ label: '移動到其他資料夾', act: 'move' });
        items.push({ label: '✏ 重命名 (F2)', act: 'rename' });
        items.push({ label: '📋 複製路徑', act: 'copy-path' });
        items.push({ sep: true });
        items.push({ label: '🗑 移到回收桶 (Del)', act: 'trash', danger: true });

        menu.innerHTML = items.map(it => {
            if (it.sep) return '<li class="sep"></li>';
            return '<li class="' + (it.danger ? 'danger' : '') + '" data-act="' + it.act + '">'
                + escapeHTML(it.label) + '</li>';
        }).join('');
        menu.hidden = false;
        const w = menu.offsetWidth || 200;
        const h = menu.offsetHeight || 200;
        const vx = Math.min(x, window.innerWidth - w - 10);
        const vy = Math.min(y, window.innerHeight - h - 10);
        menu.style.left = vx + 'px';
        menu.style.top = vy + 'px';

        menu.querySelectorAll('li[data-act]').forEach(li => {
            li.addEventListener('click', async () => {
                closeContextMenu();
                await runContextAction(li.dataset.act, rel, type, name);
            });
        });
    }
    function closeContextMenu() {
        const menu = document.getElementById('fmContextMenu');
        if (menu) menu.hidden = true;
    }
    document.addEventListener('click', closeContextMenu);
    document.addEventListener('scroll', closeContextMenu, true);

    async function runContextAction(act, rel, type, name) {
        const fullPath = buildLocalPath(rel);
        if (act === 'preview' && type === 'file') return openPreview(rel, name);
        if (act === 'pdf-tool' && type === 'file') return openPdfToolFromFileManager(rel);
        if (act === 'download') {
            const url = '/api/osc/files/content?path=' + encodeURIComponent(fullPath);
            const a = document.createElement('a');
            a.href = url;
            a.download = name || '';
            document.body.appendChild(a);
            a.click();
            a.remove();
            return;
        }
        if (act === 'share' && type === 'file') return createShareLink(rel, name);
        if (act === 'copy-path') {
            try {
                await navigator.clipboard.writeText(fullPath);
                setStatus('已複製路徑：' + fullPath);
                setTimeout(() => setStatus(''), 2500);
            } catch (e) {
                setStatus('複製失敗：' + (e.message || e), true);
            }
            return;
        }
        if (act === 'move') return startMoveSelected(rel, type, name);
        if (act === 'rename') return renameSelected(rel, name);
        if (act === 'trash') return trashSelected(rel, name);
    }

    async function runFileAction(act, rel, type, name) {
        if (!rel) return;
        const el = document.querySelector('#fmEntriesArea [data-rel="' + cssEsc(rel) + '"]');
        if (el) selectEntry(rel, type, el);
        if (act === 'open' && type === 'dir') return navigateTo(rel);
        if (act === 'preview' && type === 'file') return openPreview(rel, name);
        if (act === 'share' && type === 'file') return createShareLink(rel, name);
        if (act === 'download' && type === 'file') {
            const url = '/api/osc/files/content?path=' + encodeURIComponent(buildLocalPath(rel));
            const a = document.createElement('a');
            a.href = url;
            a.download = name || '';
            document.body.appendChild(a);
            a.click();
            a.remove();
        }
    }

    async function renameSelected(rel, oldName) {
        if (!rel) return;
        const newName = prompt('新名稱：', oldName || '');
        if (newName == null) return;
        const trimmed = newName.trim();
        if (!trimmed || trimmed === oldName) return;
        const r = await apiRename(FM.basePath, rel, trimmed);
        if (r && r.ok) { setStatus('已重命名為：' + trimmed); refresh(); setTimeout(() => setStatus(''), 2500); }
        else setStatus('重命名失敗：' + ((r && r.error) || '未知'), true);
    }

    async function trashSelected(rel, name) {
        if (!rel) return;
        if (!confirm('將「' + (name || rel) + '」移到回收桶（.trash 子資料夾）？\n\n（此操作可從 .trash 內手動還原；不會永久刪除。）')) return;
        const r = await apiMoveToTrash(FM.basePath, rel);
        if (r && r.ok) { setStatus('已移到回收桶：' + r.new_relative_path); refresh(); setTimeout(() => setStatus(''), 3000); }
        else setStatus('移到回收桶失敗：' + ((r && r.error) || '未知'), true);
    }

    function cssEsc(s) { return String(s || '').replace(/(["\\])/g, '\\$1'); }

    // ── Upload / mkdir / move (Phase 2 commit 9) ──────────────────────
    const CHUNK_THRESHOLD = 10 * 1024 * 1024;
    const CHUNK_SIZE = 5 * 1024 * 1024;
    const MAX_PARALLEL = 3;
    let _uploadConflictPolicy = null;     // null | overwrite-all | skip-all
    const _ensuredFolders = new Set();

    async function apiMkdir(basePath, relativePath, name) {
        const r = await fetch('/api/osc/folders/mkdir', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ base_path: basePath, relative_path: relativePath, name }),
        });
        return r.json();
    }

    function openConflictDialog(name) {
        return new Promise(resolve => {
            const m = document.getElementById('fmConflictModal');
            const body = document.getElementById('fmConflictBody');
            if (!m || !body) return resolve('skip');
            body.innerHTML = '<b>' + escapeHTML(name) + '</b> 已存在於目前資料夾。請選擇處理方式：';
            m.hidden = false;
            const handler = (ev) => {
                const btn = ev.target.closest('[data-conflict-act]');
                if (!btn) return;
                const a = btn.dataset.conflictAct;
                m.hidden = true;
                m.removeEventListener('click', handler);
                if (a === 'overwrite-all') _uploadConflictPolicy = 'overwrite-all';
                if (a === 'skip-all') _uploadConflictPolicy = 'skip-all';
                resolve(a);
            };
            m.addEventListener('click', handler);
        });
    }

    function showQueue() {
        const q = document.getElementById('fmUploadQueue');
        if (q) q.style.display = 'flex';
    }
    function hideQueue() {
        const q = document.getElementById('fmUploadQueue');
        if (q) q.style.display = 'none';
    }
    function addQueueRow(id, name) {
        const body = document.getElementById('fmUploadQueueBody');
        if (!body) return;
        const item = document.createElement('div');
        item.className = 'fm-queue-item';
        item.id = 'fmQ_' + id;
        item.innerHTML = '<div class="fm-queue-name" title="' + escapeHTML(name) + '">' + escapeHTML(name) + '</div>'
            + '<div class="fm-queue-bar"><div class="fm-queue-bar-fill" style="width:0%"></div></div>'
            + '<div class="fm-queue-status">等待中…</div>';
        body.appendChild(item);
    }
    function setQueueProgress(id, pct, statusText, klass) {
        const item = document.getElementById('fmQ_' + id);
        if (!item) return;
        const fill = item.querySelector('.fm-queue-bar-fill');
        const status = item.querySelector('.fm-queue-status');
        if (fill) fill.style.width = pct + '%';
        if (status && statusText) status.textContent = statusText;
        if (klass) {
            item.classList.remove('ok', 'err');
            item.classList.add(klass);
        }
    }

    async function uploadFiles(fileList) {
        if (!FM.basePath) { setStatus('尚未開啟資料夾', true); return; }
        if (!fileList || !fileList.length) return;
        _uploadConflictPolicy = null;
        showQueue();

        const queue = Array.from(fileList).map((f, i) => ({
            id: 'u' + Date.now() + '_' + i,
            file: f,
            relPath: (f.webkitRelativePath || '').split('/').slice(0, -1).join('/'),
        }));
        queue.forEach(q => addQueueRow(q.id, (q.relPath ? q.relPath + '/' : '') + q.file.name));

        let uploadedAny = false;

        async function uploadOne(q) {
            let targetRel = FM.currentRel || '';
            if (q.relPath) {
                targetRel = (targetRel ? targetRel + '/' : '') + q.relPath;
                await ensureFolderChain(q.relPath, FM.currentRel);
            }
            try {
                let result;
                if (q.file.size > CHUNK_THRESHOLD) result = await uploadChunked(q, targetRel);
                else result = await uploadSingle(q, targetRel);
                if (result.ok) {
                    setQueueProgress(q.id, 100, '完成', 'ok');
                    uploadedAny = true;
                } else if (result.skipped) {
                    setQueueProgress(q.id, 0, '略過', 'err');
                } else {
                    setQueueProgress(q.id, 0, '失敗：' + (result.error || ''), 'err');
                }
            } catch (e) {
                setQueueProgress(q.id, 0, '錯誤：' + (e.message || e), 'err');
            }
        }

        let cursor = 0;
        async function worker() {
            while (cursor < queue.length) {
                const idx = cursor++;
                await uploadOne(queue[idx]);
            }
        }
        const workers = [];
        for (let i = 0; i < MAX_PARALLEL; i++) workers.push(worker());
        await Promise.all(workers);

        if (uploadedAny) await refresh();
    }

    async function ensureFolderChain(relPath, base) {
        const parts = relPath.split('/').filter(Boolean);
        let acc = base || '';
        for (const p of parts) {
            const key = (acc || '') + '|' + p;
            if (_ensuredFolders.has(key)) {
                acc = (acc ? acc + '/' : '') + p;
                continue;
            }
            try { await apiMkdir(FM.basePath, acc, p); } catch (_) {}
            _ensuredFolders.add(key);
            acc = (acc ? acc + '/' : '') + p;
        }
    }

    async function uploadSingle(q, targetRel) {
        let overwrite = (_uploadConflictPolicy === 'overwrite-all');
        for (let attempt = 0; attempt < 3; attempt++) {
            const fd = new FormData();
            fd.append('base_path', FM.basePath);
            fd.append('relative_path', targetRel);
            if (overwrite) fd.append('overwrite', '1');
            fd.append('files', q.file, q.file.name);

            const r = await xhrUpload('/api/osc/files/upload-multi', fd, q.id);
            if (r.json && r.json.results && r.json.results[0]) {
                const r0 = r.json.results[0];
                if (r0.ok) return { ok: true, path: r0.path };
                if (r0.error === 'file_exists') {
                    if (_uploadConflictPolicy === 'skip-all') return { skipped: true };
                    if (_uploadConflictPolicy === 'overwrite-all') { overwrite = true; continue; }
                    const choice = await openConflictDialog(q.file.name);
                    if (choice === 'overwrite' || choice === 'overwrite-all') { overwrite = true; continue; }
                    if (choice === 'skip' || choice === 'skip-all') return { skipped: true };
                    if (choice === 'rename') {
                        const baseName = q.file.name.replace(/(\.[^.]+)$/, '');
                        const ext = (q.file.name.match(/\.[^.]+$/) || [''])[0];
                        const newName = baseName + '_' + Date.now() + ext;
                        const newFile = new File([q.file], newName, { type: q.file.type });
                        const fd2 = new FormData();
                        fd2.append('base_path', FM.basePath);
                        fd2.append('relative_path', targetRel);
                        fd2.append('files', newFile, newName);
                        const r2 = await xhrUpload('/api/osc/files/upload-multi', fd2, q.id);
                        if (r2.json && r2.json.results && r2.json.results[0] && r2.json.results[0].ok) return { ok: true };
                        return { error: (r2.json && r2.json.results && r2.json.results[0] && r2.json.results[0].error) || 'rename_failed' };
                    }
                    return { skipped: true };
                }
                return { error: r0.error || 'unknown' };
            }
            return { error: (r.json && r.json.error) || 'no_result' };
        }
        return { error: 'too_many_attempts' };
    }

    function xhrUpload(url, fd, qid) {
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open('POST', url);
            xhr.withCredentials = true;
            xhr.upload.onprogress = (ev) => {
                if (!ev.lengthComputable) return;
                const pct = Math.round(ev.loaded / ev.total * 95);
                setQueueProgress(qid, pct, '上傳中… ' + pct + '%');
            };
            xhr.onload = () => {
                let json = null;
                try { json = JSON.parse(xhr.responseText); } catch (_) {}
                resolve({ status: xhr.status, json });
            };
            xhr.onerror = () => reject(new Error('network_error'));
            xhr.send(fd);
        });
    }

    async function uploadChunked(q, targetRel) {
        const sessionId = 'sess_' + Date.now() + '_' + Math.random().toString(36).slice(2, 10);
        const total = Math.ceil(q.file.size / CHUNK_SIZE);
        let overwrite = (_uploadConflictPolicy === 'overwrite-all');
        for (let i = 0; i < total; i++) {
            const start = i * CHUNK_SIZE;
            const end = Math.min(start + CHUNK_SIZE, q.file.size);
            const blob = q.file.slice(start, end);
            const fd = new FormData();
            fd.append('session_id', sessionId);
            fd.append('chunk_index', String(i));
            fd.append('total_chunks', String(total));
            fd.append('filename', q.file.name);
            fd.append('base_path', FM.basePath);
            fd.append('relative_path', targetRel);
            if (overwrite) fd.append('overwrite', '1');
            fd.append('chunk', blob, q.file.name + '.part' + i);

            const r = await fetch('/api/osc/files/upload-chunked', {
                method: 'POST', credentials: 'same-origin', body: fd,
            });
            const j = await r.json();
            if (!j.ok) {
                if (j.error === 'file_exists') {
                    if (_uploadConflictPolicy === 'skip-all') return { skipped: true };
                    const choice = await openConflictDialog(q.file.name);
                    if (choice === 'overwrite' || choice === 'overwrite-all') {
                        overwrite = true; i = -1; continue;
                    }
                    if (choice === 'skip' || choice === 'skip-all') return { skipped: true };
                }
                return { error: j.error || 'chunk_failed' };
            }
            const pct = Math.round(((i + 1) / total) * 100);
            setQueueProgress(q.id, pct, '上傳中 ' + (i + 1) + '/' + total + ' (' + pct + '%)');
            if (j.finalized) return { ok: true, path: j.path };
        }
        return { error: 'chunked_no_finalize' };
    }

    function bindDropZone() {
        const main = document.querySelector('#fileManager .fm-main');
        const dz = document.getElementById('fmDropZone');
        if (!main || !dz) return;
        let depth = 0;
        main.addEventListener('dragenter', (e) => {
            if (!e.dataTransfer || !Array.from(e.dataTransfer.types || []).includes('Files')) return;
            depth++;
            dz.style.display = 'flex';
        });
        main.addEventListener('dragleave', () => {
            depth = Math.max(0, depth - 1);
            if (depth === 0) dz.style.display = 'none';
        });
        main.addEventListener('dragover', (e) => {
            if (!e.dataTransfer || !Array.from(e.dataTransfer.types || []).includes('Files')) return;
            e.preventDefault();
        });
        main.addEventListener('drop', async (e) => {
            depth = 0;
            dz.style.display = 'none';
            if (!e.dataTransfer) return;
            e.preventDefault();
            const dt = e.dataTransfer;
            const items = dt.items ? Array.from(dt.items) : [];
            const hasDirEntry = items.some(it => it.webkitGetAsEntry && (it.webkitGetAsEntry() || {}).isDirectory);
            if (hasDirEntry) {
                const collected = [];
                for (const it of items) {
                    const ent = it.webkitGetAsEntry && it.webkitGetAsEntry();
                    if (!ent) continue;
                    await collectEntries(ent, '', collected);
                }
                if (collected.length) await uploadFiles(collected);
                return;
            }
            const files = Array.from(dt.files || []);
            if (files.length) await uploadFiles(files);
        });
    }

    function collectEntries(entry, prefix, out) {
        return new Promise(resolve => {
            if (entry.isFile) {
                entry.file(file => {
                    try {
                        Object.defineProperty(file, 'webkitRelativePath', {
                            value: prefix + file.name, writable: false, configurable: true,
                        });
                    } catch (_) {}
                    out.push(file);
                    resolve();
                }, () => resolve());
            } else if (entry.isDirectory) {
                const reader = entry.createReader();
                const allEntries = [];
                const readBatch = () => {
                    reader.readEntries(async (batch) => {
                        if (!batch.length) {
                            for (const sub of allEntries) {
                                await collectEntries(sub, prefix + entry.name + '/', out);
                            }
                            resolve();
                        } else {
                            allEntries.push.apply(allEntries, batch);
                            readBatch();
                        }
                    }, () => resolve());
                };
                readBatch();
            } else { resolve(); }
        });
    }

    // ── Public init (called when sidebar tab activates) ───────────────
    FM.init = function () {
        const inp = document.getElementById('fmBasePathInput');
        const goBtn = document.getElementById('fmBasePathGoBtn');
        const refreshBtn = document.getElementById('fmRefreshBtn');
        const caseRefreshBtn = document.getElementById('fmCaseRefreshBtn');
        const caseSearchInput = document.getElementById('fmCaseSearchInput');
        const rootOverviewBtn = document.getElementById('fmRootOverviewBtn');
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
        if (caseRefreshBtn && !caseRefreshBtn._fmBound) {
            caseRefreshBtn._fmBound = true;
            caseRefreshBtn.addEventListener('click', loadCaseShortcuts);
        }
        if (rootOverviewBtn && !rootOverviewBtn._fmBound) {
            rootOverviewBtn._fmBound = true;
            rootOverviewBtn.addEventListener('click', () => {
                if (FM.driveRoots && FM.driveRoots.length) showDriveOverview();
                else loadDriveRoots();
            });
        }
        if (caseSearchInput && !caseSearchInput._fmBound) {
            caseSearchInput._fmBound = true;
            let searchTimer = null;
            caseSearchInput.addEventListener('input', () => {
                window.clearTimeout(searchTimer);
                searchTimer = window.setTimeout(() => searchCases(caseSearchInput.value), 220);
            });
            caseSearchInput.addEventListener('keydown', (e) => {
                if (e.key !== 'Enter') return;
                e.preventDefault();
                if (FM.lastCaseResults && FM.lastCaseResults.length === 1 && typeof openCaseInFileManager === 'function') {
                    openCaseInFileManager(FM.lastCaseResults[0].id);
                } else {
                    searchCases(caseSearchInput.value);
                }
            });
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
        const viewGroup = document.querySelector('.fm-toolbar-group[aria-label="檢視模式"]');
        if (sortSelect && !sortSelect._fmBound) {
            sortSelect._fmBound = true;
            sortSelect.addEventListener('change', () => {
                FM.sort = sortSelect.value;
                if (!FM.hasLoadedEntries) return;
                renderEntries({
                    folders: FM.lastEntries.folders,
                    files: FM.lastEntries.files,
                    hidden_count: FM.lastHiddenCount, ok: true,
                });
            });
        }
        if (viewGroup && !viewGroup._fmBound) {
            viewGroup._fmBound = true;
            viewGroup.addEventListener('click', (ev) => {
                const btn = ev.target.closest('.fm-view-btn');
                if (!btn) return;
                ev.preventDefault();
                applyViewMode(btn.dataset.view || 'detail', true);
            });
        }
        document.querySelectorAll('.fm-view-btn').forEach(btn => {
            if (btn._fmBoundDirect) return;
            btn._fmBoundDirect = true;
            btn.addEventListener('click', (ev) => {
                ev.preventDefault();
                ev.stopPropagation();
                applyViewMode(btn.dataset.view || 'detail', true);
            });
        });
        applyViewMode(FM.viewMode, false);

        // Phase 2 commit 8: preview modal close handlers
        const previewClose = document.getElementById('fmPreviewClose');
        const previewMove = document.getElementById('fmPreviewMove');
        const previewTrash = document.getElementById('fmPreviewTrash');
        const previewShare = document.getElementById('fmPreviewShare');
        const previewModal = document.getElementById('fmPreviewModal');
        if (previewClose && !previewClose._fmBound) {
            previewClose._fmBound = true;
            previewClose.addEventListener('click', closePreview);
        }
        if (previewMove && !previewMove._fmBound) {
            previewMove._fmBound = true;
            previewMove.addEventListener('click', () => {
                const rel = FM.selectedRel;
                const type = FM.selectedType;
                const name = FM.selectedName || pathBaseName(rel);
                closePreview();
                startMoveSelected(rel, type, name);
            });
        }
        if (previewTrash && !previewTrash._fmBound) {
            previewTrash._fmBound = true;
            previewTrash.addEventListener('click', () => {
                const rel = FM.selectedRel;
                const name = FM.selectedName || pathBaseName(rel);
                closePreview();
                trashSelected(rel, name);
            });
        }
        if (previewShare && !previewShare._fmBound) {
            previewShare._fmBound = true;
            previewShare.addEventListener('click', () => {
                const rel = FM.selectedRel;
                const name = FM.selectedName || pathBaseName(rel);
                createShareLink(rel, name);
            });
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

        // Phase 2 commit 9: upload buttons + drop zone + queue close
        const mkdirBtn = document.getElementById('fmMkdirBtn');
        const uploadBtn = document.getElementById('fmUploadBtn');
        const uploadFolderBtn = document.getElementById('fmUploadFolderBtn');
        const moveBtn = document.getElementById('fmMoveBtn');
        const trashBtn = document.getElementById('fmTrashBtn');
        const shareBtn = document.getElementById('fmShareBtn');
        const moveHereBtn = document.getElementById('fmMoveHereBtn');
        const moveCancelBtn = document.getElementById('fmMoveCancelBtn');
        const fileInput = document.getElementById('fmFileInput');
        const folderInput = document.getElementById('fmFolderInput');
        const queueClose = document.getElementById('fmUploadQueueClose');
        if (mkdirBtn && !mkdirBtn._fmBound) {
            mkdirBtn._fmBound = true;
            mkdirBtn.addEventListener('click', async () => {
                if (!FM.basePath) { setStatus('請先開啟資料夾', true); return; }
                const name = prompt('新資料夾名稱：');
                if (!name) return;
                const r = await apiMkdir(FM.basePath, FM.currentRel, name.trim());
                if (r && r.ok) { setStatus('已建立資料夾：' + name); refresh(); setTimeout(() => setStatus(''), 2000); }
                else setStatus('建立失敗：' + ((r && r.error) || '未知'), true);
            });
        }
        if (uploadBtn && !uploadBtn._fmBound) {
            uploadBtn._fmBound = true;
            uploadBtn.addEventListener('click', () => fileInput && fileInput.click());
        }
        if (uploadFolderBtn && !uploadFolderBtn._fmBound) {
            uploadFolderBtn._fmBound = true;
            uploadFolderBtn.addEventListener('click', () => folderInput && folderInput.click());
        }
        if (moveBtn && !moveBtn._fmBound) {
            moveBtn._fmBound = true;
            moveBtn.addEventListener('click', () => startMoveSelected(FM.selectedRel, FM.selectedType, FM.selectedName));
        }
        if (trashBtn && !trashBtn._fmBound) {
            trashBtn._fmBound = true;
            trashBtn.addEventListener('click', () => trashSelected(FM.selectedRel, FM.selectedName));
        }
        if (shareBtn && !shareBtn._fmBound) {
            shareBtn._fmBound = true;
            shareBtn.addEventListener('click', () => createShareLink(FM.selectedRel, FM.selectedName));
        }
        const entriesArea = document.getElementById('fmEntriesArea');
        if (entriesArea && !entriesArea._fmActionBound) {
            entriesArea._fmActionBound = true;
            entriesArea.addEventListener('click', (ev) => {
                const btn = ev.target.closest('.fm-action-btn');
                if (!btn) return;
                ev.preventDefault();
                ev.stopPropagation();
                runFileAction(btn.dataset.fmAction, btn.dataset.rel, btn.dataset.type, btn.dataset.name);
            });
        }
        if (moveHereBtn && !moveHereBtn._fmBound) {
            moveHereBtn._fmBound = true;
            moveHereBtn.addEventListener('click', movePendingHere);
        }
        if (moveCancelBtn && !moveCancelBtn._fmBound) {
            moveCancelBtn._fmBound = true;
            moveCancelBtn.addEventListener('click', () => cancelMovePending(true));
        }
        if (fileInput && !fileInput._fmBound) {
            fileInput._fmBound = true;
            fileInput.addEventListener('change', () => {
                if (fileInput.files && fileInput.files.length) uploadFiles(fileInput.files);
                fileInput.value = '';
            });
        }
        if (folderInput && !folderInput._fmBound) {
            folderInput._fmBound = true;
            folderInput.addEventListener('change', () => {
                if (folderInput.files && folderInput.files.length) uploadFiles(folderInput.files);
                folderInput.value = '';
            });
        }
        if (queueClose && !queueClose._fmBound) {
            queueClose._fmBound = true;
            queueClose.addEventListener('click', hideQueue);
        }
        updateSelectionControls();
        bindDropZone();

        // Phase 2 commit 11: keyboard shortcuts (F2 rename / Del trash)
        if (!document._fmKeysBound) {
            document._fmKeysBound = true;
            document.addEventListener('keydown', (ev) => {
                const fmEl = document.getElementById('fileManager');
                if (!fmEl || fmEl.offsetParent === null) return;  // tab not visible
                const previewModal = document.getElementById('fmPreviewModal');
                if (previewModal && !previewModal.hidden) return;  // preview eats keys
                if (ev.target && /input|textarea|select/i.test(ev.target.tagName)) return;
                if (!FM.selectedRel) return;
                if (ev.key === 'F2') {
                    ev.preventDefault();
                    const el = document.querySelector('#fmEntriesArea [data-rel="' + cssEsc(FM.selectedRel) + '"]');
                    const name = el && el.dataset.name;
                    renameSelected(FM.selectedRel, name);
                } else if (ev.key === 'Delete' || ev.key === 'Backspace') {
                    ev.preventDefault();
                    const el = document.querySelector('#fmEntriesArea [data-rel="' + cssEsc(FM.selectedRel) + '"]');
                    const name = el && el.dataset.name;
                    trashSelected(FM.selectedRel, name);
                }
            });
        }
        if (!FM._caseShortcutsLoaded) {
            FM._caseShortcutsLoaded = true;
            loadDriveRoots();
        }
    };

    // Auto-init when this tab becomes visible
    // 重要：若 script 在 DOMContentLoaded 已觸發後才載入（外網慢、deferred eval），
    // addEventListener 不會 fire → FM.init 永遠不跑 → openCaseInFileManager 卡死
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => FM.init());
    } else {
        // DOM 已就緒 → 立即 init
        try { FM.init(); } catch (e) { console.warn('FM.init failed:', e); }
    }

    // Allow other tabs to open this view with a preset case folder
    FM.openWithBasePath = async function (basePath, meta) {
        // 防呆：若 FM.init 還沒跑（DOM listener race），這裡補一次
        if (!FM._initialized) {
            try { FM.init(); FM._initialized = true; } catch (e) { console.warn('FM.init in openWithBasePath:', e); }
        }
        const inp = document.getElementById('fmBasePathInput');
        if (inp) inp.value = basePath;
        await setRoot(basePath, meta);
        const shell = document.querySelector('#fileManager .fm-shell');
        if (shell) shell.scrollIntoView({ behavior: 'smooth', block: 'start' });
    };
    FM.loadCaseShortcuts = loadCaseShortcuts;
    FM.loadDriveRoots = loadDriveRoots;
})();
