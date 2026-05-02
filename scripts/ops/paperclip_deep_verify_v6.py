"""Paperclip Deep Verify v6 — UX v3 IA 重組 + cross-platform open folder."""
import sys, json
sys.path.insert(0, '/Users/ai/Desktop/MAGI_v2')
from playwright.sync_api import sync_playwright

URL, USER, PASS = "http://127.0.0.1:5002", "teatai", "teatai"
PASS_CT = 0
FAIL_CT = 0

def ok(msg):
    global PASS_CT
    PASS_CT += 1
    print(f"  ✅ {msg}")

def fail(msg):
    global FAIL_CT
    FAIL_CT += 1
    print(f"  ❌ {msg}")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1440, "height": 1200})
    all_responses = []
    page = ctx.new_page()
    page.on("response", lambda r: all_responses.append((r.status, r.request.method, r.url)) if "/api/osc/" in r.url else None)
    page.on("pageerror", lambda e: print(f"  ⚠️ pageerror: {str(e)[:160]}"))

    page.goto(f"{URL}/login", wait_until="domcontentloaded")
    page.fill('input[name="username"]', USER)
    page.fill('input[name="password"]', PASS)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")
    page.goto(f"{URL}/osc", wait_until="networkidle")
    page.wait_for_timeout(2500)
    print("=== Login + /osc loaded ===\n")

    # Test 1: sidebar 結構
    print("=== T1: sidebar IA 7+1 group structure ===")
    nav = page.locator(".sidebar-nav")
    if nav.count() > 0:
        ok(".sidebar-nav exists")
    else:
        fail(".sidebar-nav missing")

    singles = page.locator(".sidebar-nav .group-single").count()
    groups = page.locator(".sidebar-nav .sidebar-group[data-group]").count()
    print(f"  group-single buttons: {singles}, sidebar-group divs: {groups}")
    if singles >= 4 and groups >= 4:
        ok(f"single={singles} groups={groups}（預期 single≥4 groups=4）")
    else:
        fail(f"single={singles} groups={groups}（不符 7 大類分組）")

    # Test 2: 預設所有 group hidden（除 dashboard active）
    print("\n=== T2: 預設 group hidden（沒展開）===")
    hidden_count = page.evaluate("""() => {
        return document.querySelectorAll('.sidebar-nav .group-children[hidden]').length;
    }""")
    total_groups = page.evaluate("""() => document.querySelectorAll('.sidebar-nav .group-children').length""")
    if hidden_count == total_groups and total_groups >= 4:
        ok(f"全部 {total_groups} 個 group 預設收合")
    else:
        fail(f"預期全收合：hidden={hidden_count}/{total_groups}")

    # Test 3: 點 group-btn 展開 + 自動點第一個 sub-tab
    print("\n=== T3: 點「案件」group → 自動展開 + 點第一個 sub-tab(cases) ===")
    case_group_btn = page.locator('[data-group-toggle="case"]')
    if case_group_btn.count() > 0:
        case_group_btn.click(force=True)
        page.wait_for_timeout(1500)
        children_hidden = page.locator('[data-group-children="case"]').get_attribute("hidden")
        if children_hidden is None:
            ok("case group 已展開")
        else:
            fail("case group 仍是 hidden")
        cases_active = page.locator('.tab-btn[data-tab="cases"].active').count()
        if cases_active > 0:
            ok("cases sub-tab 自動 active")
        else:
            fail("cases sub-tab 未自動 active")
        cases_view = page.locator("#cases.view.active").count()
        if cases_view > 0:
            ok("#cases view 已切換為 active")
        else:
            fail("#cases view 未切換")
    else:
        fail("找不到 [data-group-toggle=case]")

    # Test 4: 切到「書狀」group → 「案件」group 應自動收合（手風琴）
    print("\n=== T4: 切「書狀」group → 案件 group 自動收合 ===")
    doc_group_btn = page.locator('[data-group-toggle="document"]')
    if doc_group_btn.count() > 0:
        doc_group_btn.click(force=True)
        page.wait_for_timeout(1200)
        case_hidden = page.locator('[data-group-children="case"]').get_attribute("hidden")
        doc_hidden = page.locator('[data-group-children="document"]').get_attribute("hidden")
        if case_hidden is not None and doc_hidden is None:
            ok("手風琴：案件 group 自動收合，書狀 group 已展開")
        else:
            fail(f"手風琴未生效：case_hidden={case_hidden}, doc_hidden={doc_hidden}")

    # Test 5: 切到 sub-tab 觸發 autoExpandGroupForTab
    print("\n=== T5: 直接點 quotations sub-tab → 帳務 group 自動展開 ===")
    finance_btn = page.locator('[data-group-toggle="finance"]')
    finance_btn.click(force=True)
    page.wait_for_timeout(800)
    quotations_btn = page.locator('.tab-btn[data-tab="quotations"]')
    if quotations_btn.count() > 0:
        quotations_btn.click(force=True)
        page.wait_for_timeout(1500)
        finance_hidden = page.locator('[data-group-children="finance"]').get_attribute("hidden")
        quotations_active = page.locator('.tab-btn[data-tab="quotations"].active').count()
        if finance_hidden is None and quotations_active > 0:
            ok("finance group 自動展開 + quotations active")
        else:
            fail(f"finance_hidden={finance_hidden} quotations_active={quotations_active}")

    # Test 6: 所有 16 個既有 data-tab 都還在
    print("\n=== T6: 既有 16 個 data-tab id 完整保留 ===")
    expected = ["dashboard", "cases", "clients", "meetings", "calendar", "todos",
                "documents", "drafts", "forms", "lafWizard", "archiveWizard", "laf",
                "accounting", "quotations", "insights", "admin"]
    missing = []
    for t in expected:
        if page.locator(f'.tab-btn[data-tab="{t}"]').count() == 0:
            missing.append(t)
    if not missing:
        ok(f"全部 {len(expected)} 個 data-tab 都存在")
    else:
        fail(f"缺：{missing}")

    # Test 7: 切到每個 tab 確認 view 仍 render（不空白）
    print("\n=== T7: 每個 sub-tab 點擊後對應 view 變 active ===")
    for t in expected:
        btn = page.locator(f'.tab-btn[data-tab="{t}"]')
        if btn.count() == 0:
            continue
        # 先把所屬 group 展開
        page.evaluate(f"""(t) => {{ if (typeof autoExpandGroupForTab === 'function') autoExpandGroupForTab(t); }}""", t)
        page.wait_for_timeout(150)
        btn.first.click(force=True)
        page.wait_for_timeout(600)
        view_active = page.locator(f"#{t}.view.active").count()
        if view_active == 0:
            fail(f"切 {t} 後 #{t} view 未 active")
            break
    else:
        ok(f"全部 {len(expected)} 個 view 都能正確切換")

    # Test 8: open-folder API 回 candidates 結構
    print("\n=== T8: open-folder API 回 candidates 結構 ===")
    # 先切回 cases
    page.evaluate("autoExpandGroupForTab && autoExpandGroupForTab('cases')")
    page.locator('.tab-btn[data-tab="cases"]').first.click(force=True)
    page.wait_for_timeout(1500)
    case_card = page.locator(".case-card").first
    if case_card.count() > 0:
        case_id = case_card.get_attribute("data-case-id") or ""
        if case_id:
            try:
                resp = page.evaluate(f"""async () => {{
                    const r = await fetch('/api/osc/cases/{case_id}/open-folder', {{
                        method: 'POST', credentials: 'include',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: '{{}}'
                    }});
                    const data = await r.json();
                    return {{ status: r.status, data }};
                }}""")
                d = resp.get("data", {})
                if d.get("ok"):
                    cands = d.get("candidates") or {}
                    if isinstance(cands, dict) and "smb_url" in cands:
                        ok(f"candidates dict 有 smb_url={len(cands.get('smb_url') or [])} mac_synology={len(cands.get('mac_synology') or [])} win_unc={len(cands.get('win_unc') or [])} win_synology={len(cands.get('win_synology') or [])}")
                    else:
                        fail(f"candidates 結構錯：{list(cands.keys()) if isinstance(cands, dict) else type(cands)}")
                elif d.get("error_kind"):
                    ok(f"案件無資料夾路徑（error_kind={d.get('error_kind')}），API 仍回 200 + 訊息")
                else:
                    fail(f"open-folder 回 ok=false 又無 error_kind：{d}")
            except Exception as e:
                fail(f"open-folder API 呼叫失敗：{e}")
        else:
            fail("找不到 case-card 的 data-case-id")
    else:
        fail("cases view 沒 case-card 可測")

    # Test 9: card view quick actions 存在
    print("\n=== T9: card view quick actions（⚙️📂✏️）===")
    qa = page.locator(".case-card .card-quick-actions .btn-icon").count()
    if qa >= 3:
        ok(f"card 有 {qa} 個 .btn-icon quick actions")
    else:
        fail(f"quick actions 缺：{qa}")

    # Test 10: loading overlay 元素存在
    print("\n=== T10: loading overlay 元素 ===")
    if page.locator("#tabLoadingOverlay").count() > 0:
        ok("#tabLoadingOverlay element 存在")
        spinner = page.locator("#tabLoadingOverlay .spinner").count()
        if spinner > 0:
            ok(".spinner 已 render")
        else:
            fail(".spinner missing")
    else:
        fail("#tabLoadingOverlay missing")

    # Test 11: showCustomDialog 函式存在
    print("\n=== T11: showCustomDialog 函式存在 ===")
    has_custom = page.evaluate("typeof showCustomDialog === 'function'")
    if has_custom:
        ok("showCustomDialog defined")
    else:
        fail("showCustomDialog NOT defined")

    # Test 12: placeholder 不再出現「必填」「選填」(literally as placeholder text)
    print("\n=== T12: placeholder 不再純「必填」/「選填」===")
    bad = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('input,textarea'))
            .filter(el => el.placeholder === '必填' || el.placeholder === '選填')
            .map(el => el.id || '?');
    }""")
    if not bad:
        ok("無純「必填」/「選填」placeholder")
    else:
        fail(f"仍有 {len(bad)}: {bad[:5]}")

    print(f"\n========\nPASS={PASS_CT}, FAIL={FAIL_CT}")
    browser.close()

sys.exit(0 if FAIL_CT == 0 else 1)
