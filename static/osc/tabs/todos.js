/* tabs/todos.js – Todo management */
async function loadTodos() {
    const q = encodeURIComponent((document.getElementById("todosQ").value || "").trim());
    const data = await api(`/api/osc/todos?limit=300&q=${q}`);
    state.todos = data.items || [];
    renderTodos();
}

function isTodoDone(status) {
    const text = String(status || '').trim().toLowerCase();
    return ['completed', 'done', '已完成', '完成', 'cancelled', 'canceled', '取消'].includes(text);
}

function renderTodos() {
    const grid = document.getElementById("todosCardGrid");
    const emptyEl = document.getElementById("todosEmpty");
    if (!grid) return;

    if (!state.todos.length) {
        grid.innerHTML = '';
        if (emptyEl) emptyEl.style.display = '';
        return;
    }
    if (emptyEl) emptyEl.style.display = 'none';

    const todayStr = fmtDate(new Date());
    // Classify: overdue, today, future, completed
    const classified = state.todos.map(r => {
        const dateStr = r.todo_date || '';
        const isDone = isTodoDone(r.status);
        let group = 3; // future
        if (isDone) group = 4;
        else if (dateStr && dateStr < todayStr) group = 1; // overdue
        else if (dateStr === todayStr) group = 2; // today
        return { ...r, _group: group, _dateStr: dateStr };
    });

    // If explicit sort column chosen, use that; otherwise default group sort
    if (state.sort.col) {
        const sorted = applySort([...classified], state.sort.col, state.sort.dir, state.sort.type);
        classified.length = 0;
        classified.push(...sorted);
    } else {
        classified.sort((a, b) => {
            if (a._group !== b._group) return a._group - b._group;
            return (a._dateStr || '9999').localeCompare(b._dateStr || '9999');
        });
    }

    const groupLabels = { 1: '逾期', 2: '今天', 3: '即將到來', 4: '已完成' };
    let html = '';
    let lastGroup = 0;
    for (const r of classified) {
        if (r._group !== lastGroup) {
            lastGroup = r._group;
            html += `<div class="todo-section-label${r._group === 1 ? ' overdue-label' : ''}">${groupLabels[r._group]}</div>`;
        }
        const cardClass = r._group === 1 ? 'overdue' : r._group === 2 ? 'today-item' : r._group === 4 ? 'completed' : '';
        const badgeClass = r._group === 1 ? 'overdue' : r._group === 2 ? 'today-badge' : r._group === 4 ? 'done' : 'future';
        const badgeText = r._group === 1 ? '逾期' : r._group === 2 ? '今天' : r._group === 4 ? '完成' : (r._dateStr || '未排期');
        html += `<div class="todo-card ${cardClass}">
            <div class="todo-header">
                <div class="todo-title">${esc(r.todo_type || '待辦')}</div>
                <span class="todo-badge ${badgeClass}">${esc(badgeText)}</span>
            </div>
            <div class="todo-meta">
                <div><span class="label">日期</span> <span class="value">${esc(r.todo_date || '-')} ${esc(r.todo_time || '')}</span></div>
                <div><span class="label">案號</span> <span class="value">${esc(r.case_number || '-')}</span></div>
                <div><span class="label">當事人</span> <span class="value">${esc(r.client_name || '-')}</span></div>
                <div><span class="label">狀態</span> <span class="value">${esc(r.status || '-')}</span></div>
            </div>
            ${r.description ? `<div class="todo-desc">${esc(r.description)}</div>` : ''}
            <div class="todo-actions">
                ${r._group === 4
                    ? `<button class="btn" data-act="todo-reopen" data-id="${Number(r.id)}">重新待辦</button>`
                    : `<button class="btn primary" data-act="todo-complete" data-id="${Number(r.id)}">已完成</button>`}
                <button class="btn" data-act="todo-edit" data-id="${Number(r.id)}">編輯</button>
                <button class="btn danger" data-act="todo-del" data-id="${Number(r.id)}">刪除</button>
            </div>
        </div>`;
    }
    grid.innerHTML = html;
}

async function editTodo(id, targetPrefix = "todo_") {
    const data = await api(`/api/osc/todos/${id}`);
    const x = data.item || {};
    if (targetPrefix === "wb_todo_") {
        document.getElementById("wb_todo_id").value = x.id || "";
        document.getElementById("wb_todo_case_number").value = x.case_number || "";
        document.getElementById("wb_todo_client_name").value = x.client_name || "";
        document.getElementById("wb_todo_type").value = x.todo_type || "";
        document.getElementById("wb_todo_date").value = x.todo_date || "";
        document.getElementById("wb_todo_time").value = x.todo_time || "";
        document.getElementById("wb_todo_status").value = x.status || "";
        document.getElementById("wb_todo_source_file").value = x.source_file || "";
        document.getElementById("wb_todo_desc").value = x.description || "";
    } else {
        writeFields("todo_", x, ["id", "case_number", "client_name", "status", "source_file"]);
        document.getElementById("todo_type").value = x.todo_type || "";
        document.getElementById("todo_date").value = x.todo_date || "";
        document.getElementById("todo_time").value = x.todo_time || "";
        document.getElementById("todo_desc").value = x.description || "";
    }
}

async function delTodo(id) {
    if (!confirm(`確定刪除待辦 ${id}？`)) return;
    await api(`/api/osc/todos/${id}`, "DELETE");
    await loadTodos();
    await loadMeta();
}

async function setTodoDone(id, done = true) {
    if (!id) return;
    await api(`/api/osc/todos/${Number(id)}`, "PUT", { status: done ? "已完成" : "待處理" });
    showToast(done ? "已標記為完成，業務概覽不再顯示。" : "已重新列為待辦。", "ok", 2600);
    const reloads = [];
    if (typeof loadTodos === "function") reloads.push(loadTodos().catch(() => {}));
    if (typeof loadDashboard === "function") reloads.push(loadDashboard().catch(() => {}));
    if (typeof loadMeta === "function") reloads.push(loadMeta().catch(() => {}));
    await Promise.all(reloads);
    if (state.wb?.mode === "case" && typeof openCaseWorkbench === "function") {
        await openCaseWorkbench(state.wb.id, done ? "已標記待辦為完成。" : "已重新列為待辦。");
    } else if (state.wb?.mode === "client" && typeof openClientWorkbench === "function") {
        await openClientWorkbench(state.wb.id, done ? "已標記待辦為完成。" : "已重新列為待辦。");
    }
}

async function saveTodo() {
    const p = readFields(["todo_id", "todo_case_number", "todo_client_name", "todo_type", "todo_date", "todo_time", "todo_desc", "todo_status", "todo_source_file"]);
    const body = {
        case_number: p.todo_case_number, client_name: p.todo_client_name, todo_type: p.todo_type,
        todo_date: p.todo_date, todo_time: p.todo_time, description: p.todo_desc, status: p.todo_status, source_file: p.todo_source_file
    };
    if ((p.todo_id || "").trim()) await api(`/api/osc/todos/${Number(p.todo_id)}`, "PUT", body);
    else await api("/api/osc/todos", "POST", body);
    clearFields(["todo_id", "todo_case_number", "todo_client_name", "todo_type", "todo_date", "todo_time", "todo_desc", "todo_status", "todo_source_file"]);
    await loadTodos();
    await loadMeta();
}
