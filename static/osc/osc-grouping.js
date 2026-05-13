/* osc-grouping.js — sidebar group 展開/收合 + 自動展開 active group
 *
 * 2026-05-03 UX v3 P1：把 sidebar 平鋪 16 個 .tab-btn 重組成 7 大類分組。
 * 行為：
 *   1. 點 .group-btn → 展開/收合該 group children；展開時自動點第一個 sub-tab
 *   2. 手風琴：展開一個 group 時收合其他 group
 *   3. 切到任一 sub-tab（含被 bindTabs 觸發者）時，自動展開所屬 group
 *      （由 osc-events.js bindTabs 末尾呼叫 autoExpandGroupForTab）
 *   4. .group-single（單一 view 的 button）不受 grouping 影響
 */

function bindSidebarGroups() {
    document.querySelectorAll('[data-group-toggle]').forEach((btn) => {
        btn.addEventListener('click', () => {
            const groupName = btn.dataset.groupToggle;
            const children = document.querySelector(`[data-group-children="${groupName}"]`);
            const wasOpen = children && !children.hasAttribute('hidden');
            toggleGroup(groupName, !wasOpen);
            const isMobilePaperclip = window.matchMedia && window.matchMedia("(max-width: 760px)").matches;
            if (isMobilePaperclip) {
                return;
            }
            // 展開時自動點第一個 sub-tab（如果尚未 active）
            if (children && !children.hasAttribute('hidden')) {
                const firstSub = children.querySelector('.tab-btn[data-tab]');
                if (firstSub && !firstSub.classList.contains('active')) {
                    firstSub.click();
                }
            }
        });
    });
    updateGroupArrows();
}

function toggleGroup(groupName, openExplicit) {
    const children = document.querySelector(`[data-group-children="${groupName}"]`);
    if (!children) return;
    const shouldOpen = (typeof openExplicit === 'boolean')
        ? openExplicit
        : children.hasAttribute('hidden');

    // 手風琴：先收合其他 group（只在「展開」時做，避免單純收合也誤動其他）
    if (shouldOpen) {
        document.querySelectorAll('[data-group-children]').forEach((c) => {
            if (c !== children) c.setAttribute('hidden', '');
        });
    }

    if (shouldOpen) {
        children.removeAttribute('hidden');
    } else {
        children.setAttribute('hidden', '');
    }
    updateGroupArrows();
}

function updateGroupArrows() {
    document.querySelectorAll('[data-group-toggle]').forEach((btn) => {
        const groupName = btn.dataset.groupToggle;
        const children = document.querySelector(`[data-group-children="${groupName}"]`);
        const arrow = btn.querySelector('.group-arrow');
        if (!arrow) return;
        const isOpen = children && !children.hasAttribute('hidden');
        arrow.textContent = isOpen ? '▼' : '▶';
    });
}

// 由 bindTabs 末尾呼叫：切到 sub-tab 時自動展開所屬 group
function autoExpandGroupForTab(tabId) {
    if (!tabId) return;
    const subTab = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
    if (!subTab) return;
    const groupChildren = subTab.closest('[data-group-children]');
    if (!groupChildren) return; // group-single 直接結束
    const groupName = groupChildren.dataset.groupChildren;
    // 收合其他 group
    document.querySelectorAll('[data-group-children]').forEach((c) => {
        if (c.dataset.groupChildren !== groupName) c.setAttribute('hidden', '');
    });
    // 展開所屬
    groupChildren.removeAttribute('hidden');
    updateGroupArrows();
}
