#!/usr/bin/env python3
"""
批次為 SKILL.md 加上「呼叫格式」區段，讓 TAIDE 能照格式呼叫。
"""
import os

MAGI_ROOT = os.environ.get("MAGI_ROOT_DIR") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# skill_name → (觸發關鍵字, 參數說明, 範例)
SKILL_INVOCATIONS = {
    "judgment-collector": {
        "trigger": "查判決、找判決、搜尋判決、實務見解",
        "params": "keyword=關鍵字, court=法院(選填), year=年度(選填)",
        "examples": [
            ("查關於詐欺的最高法院判決", "查判決 keyword=詐欺 court=最高法院"),
            ("112年度的侵權行為判決", "查判決 keyword=侵權行為 year=112"),
            ("找監護權改定的實務見解", "查判決 keyword=監護權改定"),
        ],
    },
    "file-review-orchestrator": {
        "trigger": "閱卷、下載卷證、繳費、聲請閱卷",
        "params": "action=動作(check/download/apply), case=案號(選填)",
        "examples": [
            ("檢查閱卷信箱", "閱卷 action=check"),
            ("下載 2025-0004 的卷證", "閱卷 action=download case=2025-0004"),
            ("可下載案件", "閱卷 action=downloadable"),
        ],
    },
    "translator": {
        "trigger": "翻譯、translate",
        "params": "text=要翻譯的文字, target=目標語言(預設繁體中文), file=檔案路徑(選填)",
        "examples": [
            ("翻譯這段英文：The court held that...", "翻譯 text=The court held that... target=繁體中文"),
            ("翻譯這個檔案 /tmp/doc.pdf", "翻譯 file=/tmp/doc.pdf"),
        ],
    },
    "labor-law-calculator": {
        "trigger": "加班費、特休、資遣費、勞基法計算",
        "params": "type=計算類型(overtime/leave/severance), salary=月薪, hours=加班時數(選填), years=年資(選填)",
        "examples": [
            ("月薪 35000 加班 3 小時多少錢", "勞基法 type=overtime salary=35000 hours=3"),
            ("年資 5 年的特休有幾天", "勞基法 type=leave years=5"),
            ("月薪 42000 年資 3 年的資遣費", "勞基法 type=severance salary=42000 years=3"),
        ],
    },
    "market-briefing": {
        "trigger": "股票、股市、晨報、追蹤",
        "params": "action=動作(briefing/add/remove/list), symbol=股票代號(選填)",
        "examples": [
            ("今天股市怎麼樣", "股市 action=briefing"),
            ("追蹤台積電", "股市 action=add symbol=2330"),
            ("目前追蹤清單", "股市 action=list"),
        ],
    },
    "transcript-downloader": {
        "trigger": "筆錄、同步筆錄、下載筆錄",
        "params": "action=動作(sync/download/rename), case=案號(選填)",
        "examples": [
            ("同步筆錄", "筆錄 action=sync"),
            ("下載 2025-0004 的筆錄", "筆錄 action=download case=2025-0004"),
            ("重新命名筆錄", "筆錄 action=rename"),
        ],
    },
    "laf-orchestrator": {
        "trigger": "法扶、開辦、結案、報結、疑義、撤回",
        "params": "action=動作(go_live/closing/inquiry/withdrawal/fee), laf_no=法扶案號(選填), client=當事人(選填), reason=原因(選填)",
        "examples": [
            ("幫蕭仁俊做開辦", "法扶 action=go_live client=蕭仁俊"),
            ("1150206-A-042 結案", "法扶 action=closing laf_no=1150206-A-042"),
            ("[當事人L] 疑義回報 原因 文件不齊", "法扶 action=inquiry client=[當事人L] reason=文件不齊"),
        ],
    },
    "brief-gen": {
        "trigger": "書狀、撰狀、草稿、起訴狀、答辯狀",
        "params": "case=案號, type=書狀類型(選填), requirements=要求(選填)",
        "examples": [
            ("幫 2025-0087 寫民事準備書狀", "撰狀 case=2025-0087 type=民事準備書狀"),
            ("2025-0004 的刑事聲明抗告狀", "撰狀 case=2025-0004 type=刑事聲明抗告狀"),
        ],
    },
    "pdf-namer": {
        "trigger": "命名、PDF命名、歸檔",
        "params": "path=檔案路徑, action=動作(analyze/rename/batch)",
        "examples": [
            ("命名這個 PDF /tmp/doc.pdf", "PDF命名 action=analyze path=/tmp/doc.pdf"),
            ("批次命名資料夾", "PDF命名 action=batch"),
        ],
    },
    "osc-orchestrator": {
        "trigger": "案件、待辦、掃描案件",
        "params": "action=動作(scan/flush/status), case=案號(選填)",
        "examples": [
            ("掃描案件待辦", "案件 action=scan"),
            ("待辦佇列狀態", "案件 action=status"),
        ],
    },
    "memory": {
        "trigger": "記住、記憶、搜尋記憶、忘記",
        "params": "action=動作(remember/recall/forget), content=內容, query=搜尋詞(選填)",
        "examples": [
            ("記住：蕭仁俊的案件是憲法訴訟", "記憶 action=remember content=蕭仁俊的案件是憲法訴訟"),
            ("之前蕭仁俊的案件是什麼", "記憶 action=recall query=蕭仁俊"),
        ],
    },
    "statutes-vdb": {
        "trigger": "法規、法條、查法條",
        "params": "query=查詢內容",
        "examples": [
            ("民法第 184 條的規定", "查法條 query=民法第184條"),
            ("強制執行法管轄規定", "查法條 query=強制執行法管轄"),
        ],
    },
    "docx": {
        "trigger": "產生DOCX、輸出Word",
        "params": "content=內容, title=標題(選填), template=模板(選填)",
        "examples": [
            ("把這段內容輸出成 Word", "產生DOCX content=... title=書狀草稿"),
        ],
    },
    "xlsx": {
        "trigger": "產生Excel、輸出試算表",
        "params": "data=資料, title=標題(選填)",
        "examples": [
            ("把案件清單輸出成 Excel", "產生Excel data=案件清單 title=案件總覽"),
        ],
    },
    "pdf": {
        "trigger": "摘要、PDF摘要、summarize",
        "params": "path=檔案路徑, action=動作(summarize/extract)",
        "examples": [
            ("摘要這個 PDF /tmp/judgment.pdf", "PDF摘要 action=summarize path=/tmp/judgment.pdf"),
        ],
    },
    "crawler-targets": {
        "trigger": "爬蟲、新增爬蟲、移除爬蟲",
        "params": "action=動作(list/add/remove), url=網址(選填)",
        "examples": [
            ("爬蟲清單", "爬蟲 action=list"),
            ("新增爬蟲 https://example.com", "爬蟲 action=add url=https://example.com"),
        ],
    },
    "court-hearing-reminder": {
        "trigger": "開庭、開庭提醒、庭期",
        "params": "action=動作(check/remind), date=日期(選填)",
        "examples": [
            ("今天有開庭嗎", "庭期 action=check date=today"),
            ("這週的開庭行程", "庭期 action=check date=this_week"),
        ],
    },
    "contract-review": {
        "trigger": "審閱契約、合約審查",
        "params": "path=檔案路徑",
        "examples": [
            ("審閱這份合約 /tmp/contract.pdf", "審閱契約 path=/tmp/contract.pdf"),
        ],
    },
}

INVOCATION_MARKER = "## 呼叫格式"

def add_invocation(skill_name, info):
    skill_md = os.path.join(MAGI_ROOT, "skills", skill_name, "SKILL.md")
    if not os.path.exists(skill_md):
        print(f"  ❌ {skill_name}: SKILL.md 不存在")
        return False

    with open(skill_md, "r", encoding="utf-8") as f:
        content = f.read()

    if INVOCATION_MARKER in content:
        print(f"  ⏭️ {skill_name}: 已有呼叫格式")
        return False

    lines = [
        "",
        INVOCATION_MARKER,
        f"觸發詞：{info['trigger']}",
        f"參數：{info['params']}",
        "",
        "## 呼叫範例",
    ]
    for user_say, invoke in info["examples"]:
        lines.append(f"使用者：{user_say}")
        lines.append(f"→ {invoke}")
        lines.append("")

    with open(skill_md, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"  ✅ {skill_name}")
    return True


if __name__ == "__main__":
    print("=== 為 SKILL.md 加入呼叫格式 ===")
    updated = 0
    for skill, info in sorted(SKILL_INVOCATIONS.items()):
        if add_invocation(skill, info):
            updated += 1
    print(f"\n更新 {updated} / {len(SKILL_INVOCATIONS)} 個 skill")
