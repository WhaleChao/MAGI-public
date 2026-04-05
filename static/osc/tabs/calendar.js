/* tabs/calendar.js – Calendar rendering (month/week/day views) */
async function loadCalendarEvents() {
    const q = encodeURIComponent((document.getElementById("calQ").value || "").trim());
    const caseNumber = encodeURIComponent((document.getElementById("calCaseNumber").value || "").trim());
    const startDate = encodeURIComponent((document.getElementById("calStartDate").value || "").trim());
    const endDate = encodeURIComponent((document.getElementById("calEndDate").value || "").trim());
    const data = await api(`/api/osc/calendar/events?limit=500&q=${q}&case_number=${caseNumber}&start_date=${startDate}&end_date=${endDate}`);
    state.calendarEvents = data.items || [];
    renderCalendarEvents();
}

// ── Calendar view state ──
const calState = {
    viewMode: localStorage.getItem('calViewMode') || 'month',
    viewDate: new Date(),
};

function renderCalendarEvents() {
    // Also update legacy table for data ops
    const body = document.getElementById("calBody");
    if (body) {
        if (!state.calendarEvents.length) {
            body.innerHTML = `<tr><td colspan="8" class="muted">沒有行事曆事件</td></tr>`;
        } else {
            body.innerHTML = state.calendarEvents.map(r => `
            <tr>
                <td>${esc(r.start_date)}</td><td>${esc(r.end_date)}</td>
                <td>${esc(r.title)}</td><td>${esc(r.case_number || "")}</td>
                <td>${esc(r.location || "")}</td><td>${esc(r.is_all_day)}</td>
                <td>${esc(r.reminder_minutes || 0)}</td>
                <td class="actions">
                    <button class="btn" data-act="cal-edit" data-id="${Number(r.id)}">編輯</button>
                    <button class="btn danger" data-act="cal-del" data-id="${Number(r.id)}">刪除</button>
                </td>
            </tr>`).join("");
        }
    }
    // Render calendar grid
    renderCalGrid();
}

function renderCalGrid() {
    const grid = document.getElementById('calGrid');
    const titleEl = document.getElementById('calViewTitle');
    if (!grid) return;
    const d = calState.viewDate;
    const events = state.calendarEvents || [];

    if (calState.viewMode === 'month') renderCalMonth(grid, titleEl, d, events);
    else if (calState.viewMode === 'week') renderCalWeek(grid, titleEl, d, events);
    else renderCalDay(grid, titleEl, d, events);

    // Highlight active view button
    document.querySelectorAll('.cal-views button').forEach(b => {
        b.classList.toggle('active', b.dataset.view === calState.viewMode);
    });
}

function parseDate(s) {
    if (!s) return null;
    const d = new Date(s);
    return isNaN(d.getTime()) ? null : d;
}
function fmtDate(d) { return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; }
function fmtTime(d) { return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`; }
function sameDay(a, b) { return a.getFullYear()===b.getFullYear() && a.getMonth()===b.getMonth() && a.getDate()===b.getDate(); }
function evtColor(e) { return e.color || '#0ea5e9'; }

function eventsForDay(events, day) {
    const dayStr = fmtDate(day);
    return events.filter(e => {
        const s = parseDate(e.start_date);
        const en = parseDate(e.end_date);
        if (!s) return false;
        const sStr = fmtDate(s);
        const eStr = en ? fmtDate(en) : sStr;
        return dayStr >= sStr && dayStr <= eStr;
    });
}

function evtPill(e, showTime) {
    const s = parseDate(e.start_date);
    const timeStr = (showTime && s) ? `<span class="evt-time">${fmtTime(s)}</span> ` : '';
    return `<div class="cal-evt" style="background:${evtColor(e)}" title="${esc(e.title)}${e.location ? ' @ '+esc(e.location) : ''}" data-act="cal-edit" data-id="${Number(e.id)}">${timeStr}${esc(e.title)}</div>`;
}

function renderCalMonth(grid, titleEl, d, events) {
    const year = d.getFullYear(), month = d.getMonth();
    const today = new Date();
    titleEl.textContent = `${year}年${month+1}月`;

    const firstDay = new Date(year, month, 1);
    let startDay = firstDay.getDay(); // 0=Sun
    const daysInMonth = new Date(year, month+1, 0).getDate();

    const weekdays = ['日','一','二','三','四','五','六'];
    let html = weekdays.map(w => `<div class="cal-weekday">${w}</div>`).join('');

    // Fill leading days from prev month
    const prevMonthDays = new Date(year, month, 0).getDate();
    for (let i = startDay - 1; i >= 0; i--) {
        const dayDate = new Date(year, month-1, prevMonthDays - i);
        const dayEvts = eventsForDay(events, dayDate);
        html += `<div class="cal-day other-month"><div class="day-num">${prevMonthDays - i}</div>${dayEvts.slice(0,2).map(e => evtPill(e,false)).join('')}${dayEvts.length > 2 ? `<div class="cal-more">+${dayEvts.length-2} 更多</div>` : ''}</div>`;
    }

    // Current month days
    for (let day = 1; day <= daysInMonth; day++) {
        const dayDate = new Date(year, month, day);
        const isToday = sameDay(dayDate, today);
        const dayEvts = eventsForDay(events, dayDate);
        html += `<div class="cal-day${isToday ? ' today' : ''}"><div class="day-num">${day}</div>${dayEvts.slice(0,3).map(e => evtPill(e,false)).join('')}${dayEvts.length > 3 ? `<div class="cal-more">+${dayEvts.length-3} 更多</div>` : ''}</div>`;
    }

    // Fill trailing days
    const totalCells = startDay + daysInMonth;
    const remaining = (7 - totalCells % 7) % 7;
    for (let i = 1; i <= remaining; i++) {
        const dayDate = new Date(year, month+1, i);
        const dayEvts = eventsForDay(events, dayDate);
        html += `<div class="cal-day other-month"><div class="day-num">${i}</div>${dayEvts.slice(0,2).map(e => evtPill(e,false)).join('')}</div>`;
    }

    grid.innerHTML = `<div class="cal-month-grid">${html}</div>`;
}

function renderCalWeek(grid, titleEl, d, events) {
    const today = new Date();
    const dayOfWeek = d.getDay();
    const weekStart = new Date(d); weekStart.setDate(d.getDate() - dayOfWeek);

    const endWeek = new Date(weekStart); endWeek.setDate(weekStart.getDate() + 6);
    titleEl.textContent = `${weekStart.getMonth()+1}/${weekStart.getDate()} – ${endWeek.getMonth()+1}/${endWeek.getDate()}`;

    const weekdays = ['日','一','二','三','四','五','六'];
    let html = '<div class="cal-week-header"></div>'; // corner
    for (let i = 0; i < 7; i++) {
        const day = new Date(weekStart); day.setDate(weekStart.getDate() + i);
        const isToday = sameDay(day, today);
        html += `<div class="cal-week-header${isToday ? ' today-col' : ''}">${weekdays[i]} ${day.getDate()}</div>`;
    }

    for (let h = 0; h < 24; h++) {
        html += `<div class="cal-week-hour">${String(h).padStart(2,'0')}:00</div>`;
        for (let i = 0; i < 7; i++) {
            const day = new Date(weekStart); day.setDate(weekStart.getDate() + i);
            const isToday = sameDay(day, today);
            const dayEvts = eventsForDay(events, day).filter(e => {
                const s = parseDate(e.start_date);
                if (!s) return false;
                if (String(e.is_all_day) === '1') return h === 0;
                return s.getHours() === h;
            });
            html += `<div class="cal-week-cell${isToday ? ' today-col' : ''}">${dayEvts.map(e => evtPill(e, true)).join('')}</div>`;
        }
    }
    grid.innerHTML = `<div class="cal-week-grid">${html}</div>`;
}

function renderCalDay(grid, titleEl, d, events) {
    const weekdays = ['日','一','二','三','四','五','六'];
    titleEl.textContent = `${d.getFullYear()}年${d.getMonth()+1}月${d.getDate()}日（${weekdays[d.getDay()]}）`;

    const dayEvts = eventsForDay(events, d);
    let html = '';
    for (let h = 0; h < 24; h++) {
        const hourEvts = dayEvts.filter(e => {
            const s = parseDate(e.start_date);
            if (!s) return false;
            if (String(e.is_all_day) === '1') return h === 0;
            return s.getHours() === h;
        });
        html += `<div class="cal-day-row"><div class="cal-day-hour">${String(h).padStart(2,'0')}:00</div><div class="cal-day-slot">${hourEvts.map(e => evtPill(e, true)).join('')}</div></div>`;
    }
    grid.innerHTML = `<div class="cal-day-view">${html}</div>`;
}

function initCalendarView() {
    // View mode buttons
    document.querySelectorAll('.cal-views button').forEach(btn => {
        btn.addEventListener('click', () => {
            calState.viewMode = btn.dataset.view;
            localStorage.setItem('calViewMode', calState.viewMode);
            renderCalGrid();
        });
    });
    // Nav buttons
    document.getElementById('calPrevBtn')?.addEventListener('click', () => {
        const d = calState.viewDate;
        if (calState.viewMode === 'month') d.setMonth(d.getMonth() - 1);
        else if (calState.viewMode === 'week') d.setDate(d.getDate() - 7);
        else d.setDate(d.getDate() - 1);
        renderCalGrid();
    });
    document.getElementById('calNextBtn')?.addEventListener('click', () => {
        const d = calState.viewDate;
        if (calState.viewMode === 'month') d.setMonth(d.getMonth() + 1);
        else if (calState.viewMode === 'week') d.setDate(d.getDate() + 7);
        else d.setDate(d.getDate() + 1);
        renderCalGrid();
    });
    document.getElementById('calTodayBtn')?.addEventListener('click', () => {
        calState.viewDate = new Date();
        renderCalGrid();
    });
}

async function editCalendarEvent(id) {
    const data = await api(`/api/osc/calendar/events/${Number(id)}`);
    const x = data.item || {};
    writeFields("cal_", x, ["id", "event_id", "title", "case_number", "start_date", "end_date", "location", "color", "is_all_day", "reminder_minutes", "summary", "description", "raw_data"]);
}

async function saveCalendarEvent() {
    const p = readFields(["cal_id", "cal_event_id", "cal_title", "cal_case_number", "cal_start_date", "cal_end_date", "cal_location", "cal_color", "cal_is_all_day", "cal_reminder_minutes", "cal_summary", "cal_description", "cal_raw_data"]);
    const body = {
        event_id: p.cal_event_id,
        title: p.cal_title,
        case_number: p.cal_case_number,
        start_date: p.cal_start_date,
        end_date: p.cal_end_date,
        location: p.cal_location,
        color: p.cal_color || "#3498db",
        is_all_day: p.cal_is_all_day || "0",
        reminder_minutes: p.cal_reminder_minutes || "0",
        summary: p.cal_summary,
        description: p.cal_description,
        raw_data: p.cal_raw_data,
    };
    if (!body.title || !body.start_date || !body.end_date) return alert("請輸入標題、開始與結束時間");
    if ((p.cal_id || "").trim()) await api(`/api/osc/calendar/events/${Number(p.cal_id)}`, "PUT", body);
    else await api(`/api/osc/calendar/events`, "POST", body);
    clearFields(["cal_id", "cal_event_id", "cal_title", "cal_case_number", "cal_start_date", "cal_end_date", "cal_location", "cal_color", "cal_is_all_day", "cal_reminder_minutes", "cal_summary", "cal_description", "cal_raw_data"]);
    await loadCalendarEvents();
    await loadMeta();
}

async function delCalendarEvent(id) {
    if (!confirm(`確定刪除行事曆事件 ${id}？`)) return;
    await api(`/api/osc/calendar/events/${Number(id)}`, "DELETE");
    await loadCalendarEvents();
    await loadMeta();
}
