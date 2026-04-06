"""
command_dispatch.py — extracted from MAGIOrchestrator._handle_command + _list_skills

All logic is identical to the original methods; the only mechanical changes are:
  * ``self`` parameter renamed to ``orch``
  * ``self.`` references replaced with ``orch.``
  * recursive ``self._handle_command(`` replaced with ``handle_command(orch,``
"""
import json
import logging
import os
import re
import subprocess
import sys
import threading

from api.command_registry import CommandContext
from api.runtime_paths import get_laf_script, get_legacy_code_root, get_magi_root_dir, get_skill_python

# Fallback registry — primary path uses orch._cmd_registry (set in Orchestrator.__init__)
_cmd_registry = None

logger = logging.getLogger("Orchestrator")

_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))

# ── Lazy module-level helpers (mirrors orchestrator.py top-level) ──

def _lazy_brain(fn_name):
    def _wrapper(*a, **kw):
        import skills.brain_manager.action as _bm
        globals()[fn_name] = getattr(_bm, fn_name)
        return getattr(_bm, fn_name)(*a, **kw)
    return _wrapper

def research_topic(*a, **kw):
    from skills.research.web_research import research_topic as _fn
    globals()["research_topic"] = _fn
    return _fn(*a, **kw)

def fetch_url_content(*a, **kw):
    from skills.research.web_research import fetch_url_content as _fn
    globals()["fetch_url_content"] = _fn
    return _fn(*a, **kw)

def summarize_text(*a, **kw):
    from skills.bridge.balthasar_bridge import summarize_text as _fn
    globals()["summarize_text"] = _fn
    return _fn(*a, **kw)

switch_brain_mode = _lazy_brain("switch_brain_mode")
get_brain_status = _lazy_brain("get_brain_status")
get_melchior_runtime_status = _lazy_brain("get_melchior_runtime_status")
repair_big_brain = _lazy_brain("repair_big_brain")
calibrate_distributed_ngl = _lazy_brain("calibrate_distributed_ngl")

from skills.bridge.legal_bridge import execute_skill


def handle_command(orch, user_id, message, role="user", platform="LINE"):
    """
    Routes commands to Melchior or System Skills.
    Uses CommandRegistry for extensible dispatch, falls back to legacy if-elif.
    """
    msg_lower = message.lower()

    # Try registry-based dispatch first
    ctx = CommandContext(
        user_id=user_id,
        message=message,
        msg_lower=msg_lower,
        role=role,
        platform=platform,
        orchestrator=orch,
    )
    registry = getattr(orch, "_cmd_registry", None) or _cmd_registry
    if registry is not None:
        registry_result = registry.dispatch(ctx)
        if registry_result is not None:
            return registry_result

    # Help Command — role-aware
    if msg_lower in ["/help", "help", "指令", "說明", "功能", "menu", "helps", "/start"]:
        if role == "admin":
            return (
"🤖 **MAGI (Casper) 功能總覽 (管理員)**\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"📝 **文件產生**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `/委任狀` 或 `幫我做委任狀` — 民事／刑事／行政委任狀\n"
"• `/契約書` 或 `幫我做委任契約書` — 委任契約書\n"
"• `/收據` 或 `幫我開收據` — 律師費收據\n"
"• `/存證信函` 或 `幫我寫存證信函` — 存證信函 PDF\n"
"• `審閱契約 [上傳檔案]` — 合約風險審查\n"
"• `證據能力 [案號]` — 卷證索引證據能力自動分類\n"
"• `截圖排序 [上傳截圖]` — 對話截圖自動排序\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"⚖️ **法扶作業**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `法扶回報指令` — 顯示回報指令集\n"
"• `幫我做[姓名]開辦回報` — 自然語言回報\n"
"• `正式送出開辦/結案` — 送出（需確認）\n"
"• `法扶監控` — 法扶案件狀態\n"
"• `自動報結掃描` / `二階段批次` — 報結作業\n"
"• `/閱卷查核 <法院> <案號>` — 查核卷宗狀態\n"
"• `/閱卷聲請 <法院> <案號>` — 聲請閱卷\n"
"• `/下載閱卷 [案號]` — 下載卷宗\n"
"• `/下載筆錄 <案號>` — 下載筆錄並歸檔\n"
"• `同步筆錄` / `重命名筆錄` — 筆錄管理\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"🖼️ **視覺 & 搜尋**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `/draw [描述]` — 生成圖片\n"
"• 上傳圖片 — 自動分析內容\n"
"• `/搜尋 [關鍵字]` — 聯網搜尋\n"
"• `/抓取 [網址]` — 讀取網頁\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"⚖️ **法扶作業 / 法律工具**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `/查判決 [關鍵字]` — 搜尋判決\n"
"• `/法規搜尋 [查詢]` — 查詢法規\n"
"• `/加班費` — 勞基法試算\n"
"• `/庭期` — 開庭排程與提醒\n"
"• `/判決趨勢 [案由]` — 判決趨勢分析\n"
"• `/司法工具` — 規費/折舊/刑度試算\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"📅 **助理 & 記憶**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `/行程` — 查詢本週會議\n"
"• `/狀態` — MAGI 節點狀態\n"
"• `/翻譯 [文字/網址]` — 本地翻譯\n"
"• `/摘要 [文字/網址]` — 文件摘要（精簡/普通/詳細三級）\n"
"  ↳ `精簡摘要` 3-5點 ∣ `摘要` 5-8點 ∣ `詳細摘要` 12-15點\n"
"• 上傳音檔 — 自動產生逐字稿\n"
"• `去AI味 [文字]` — 去除 AI 痕跡\n"
"• `/記住 [內容]` — 存入長期記憶\n"
"• `/忘記 [內容]` — 刪除記憶\n"
"• `/深度思考 [問題]` — 深度分析模式\n"
"• `/obsidian [指令]` — 筆記管理\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"📊 **追蹤 & 監控**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `/股市晨報` — 股票追蹤與分析\n"
"• `/爬蟲 [指令]` — 爬蟲目標管理\n"
"• `/RSS` — 新聞訂閱\n"
"• `掃描案件待辦` — 案件待辦管理\n"
"• `單檔命名` / `批次命名` — PDF 自動命名\n"
"• `[姓名]已繳費` — 標記繳費完成\n"
"• `日曆同步` — 庭期同步 Google Calendar\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"🧬 **技能進化 (管理員)**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• 自動生成／驗證／上線新技能\n"
"• `技能CI [skill]` — 安全檢查\n"
"• `技能事件` — 執行統計\n"
"• `內化CODE` — 自動技能化\n"
"• `自動巡檢` — 修復＋內化循環\n"
"• `核心變更待審` — 核心改動審批\n"
"\n"
"💡 在一般頻道用 `/指令` 確保觸發，或在專屬頻道直接用自然語言\n"
"💡 可透過 Telegram / Discord / LINE / 網頁入口使用"
)
        else:
            return (
"🤖 **MAGI 功能總覽**\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"⚖️ **法扶作業**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `法扶回報指令` — 顯示回報指令集\n"
"• `幫我做[姓名]開辦回報` — 自然語言回報\n"
"• `正式送出開辦/結案` — 送出（需確認）\n"
"• `法扶監控` — 法扶案件狀態\n"
"• `自動報結掃描` / `二階段批次` — 報結作業\n"
"• `/閱卷查核 <法院> <案號>` — 查核卷宗狀態\n"
"• `/閱卷聲請 <法院> <案號>` — 聲請閱卷\n"
"• `/下載閱卷 [案號]` — 下載卷宗\n"
"• `/下載筆錄 <案號>` — 下載筆錄並歸檔\n"
"• `同步筆錄` / `重命名筆錄` — 筆錄管理\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"📝 **文件產生 / 處理**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `/翻譯 [文字/網址]` — 翻譯文件或網頁\n"
"• `/摘要 [文字/網址]` — 產生文件摘要（精簡/普通/詳細三級）\n"
"  ↳ `精簡摘要` 3-5點 ∣ `摘要` 5-8點 ∣ `詳細摘要` 12-15點\n"
"• 上傳音檔 — 自動產生逐字稿\n"
"• `去AI味 [文字]` — 去除 AI 痕跡\n"
"• `/委任狀` — 製作委任狀\n"
"• `/契約書` — 製作委任契約書\n"
"• `/收據` — 開收據\n"
"• `/存證信函` — 草擬存證信函\n"
"• `審閱契約` — 合約風險審查（上傳檔案）\n"
"• `證據能力 [案號]` — 卷證索引證據能力分類\n"
"• `截圖排序` — 對話截圖自動排序\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"⚖️ **法律工具**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `/查判決 [關鍵字]` — 搜尋判決\n"
"• `/判決趨勢 [案由]` — 判決趨勢分析\n"
"• `/法規搜尋 [查詢]` — 查詢法規\n"
"• `/加班費` — 勞基法試算\n"
"• `/庭期` — 開庭排程與提醒\n"
"• `/司法工具` — 規費/折舊/刑度試算\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"🖼️ **視覺 & 搜尋**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `/draw [描述]` — 生成圖片\n"
"• 上傳圖片 — 自動分析內容\n"
"• `/搜尋 [關鍵字]` — 聯網搜尋\n"
"• `/抓取 [網址]` — 讀取網頁\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"📊 **案件 & PDF**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `掃描案件待辦` / `待辦佇列狀態` — 案件待辦管理\n"
"• `日曆同步` — 庭期同步 Google Calendar\n"
"• `單檔命名 [路徑]` / `批次命名` — PDF 自動命名\n"
"• `[姓名]已繳費` — 標記繳費完成\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"📅 **助理 & 記憶**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `/行程` — 查詢本週會議\n"
"• `/狀態` — 系統狀態\n"
"• `/股市晨報` — 股票追蹤與分析\n"
"• `/爬蟲 [指令]` — 爬蟲目標管理\n"
"• `/記住 [內容]` — 存入長期記憶\n"
"• `/深度思考 [問題]` — 深度分析模式\n"
"• `備份資料庫` / `備份清單` — 資料庫備份\n"
"• 直接對話 — 一般問答\n"
"\n"
"💡 用 `/指令` 確保觸發功能，或在專屬頻道直接用自然語言\n"
"🔒 大腦管理、鐵穹、技能進化等需管理員權限"
)

    # Image Generation (Enhanced Natural Language)
    # Matches: "/draw xxx", "draw a cat", "幫我畫一隻貓", "請畫圖", "生成圖片: sunset"
    import re
    draw_pattern = re.compile(r"(?:/draw\b|畫|draw|generate image|產生圖片|绘|画圖|畫一|画一)", re.IGNORECASE)

    if draw_pattern.search(msg_lower) and len(message) > 2:
        # Extract prompt by removing common command words
        prompt = message
        for kw in ["/draw", "幫我", "請", "畫圖", "一張", "一個", "draw", "generate image", "產生圖片", "畫", "画", "a picture of", "an image of"]:
            prompt = re.sub(re.escape(kw), "", prompt, flags=re.IGNORECASE).strip()

        # If prompt became empty but message was long enough, use original message minus strict command
        if len(prompt) < 2:
             return "🎨 請描述您想要的圖片內容。例如：'畫一隻可愛的貓咪'"

        return orch._generate_image(prompt, user_id)

    # Web Research (Below help command content)
    help_extra = """
**🌐 網路研究 (Web Research)**
- `搜尋 [主題]` : 強制聯網搜尋 (e.g., "搜尋 2025 AI 趨勢")
- `抓取 [網址]` : 讀取特定網頁內容 (e.g., "抓取 https://example.com")
- *聊天時自動搜尋* : 若問題涉及新資訊，我會自動上網查。

**🧬 技能進化 (Skill Genesis)**
- `學會 [能力]` : 請求 Melchior 撰寫新工具 (e.g., "學會畫圖", "製作幣安查價技能")
- *Iron Dome 保護中* : 所有生成程式碼皆經過安全掃描。

**🔧 其他工具**
- `court` : 查詢法院庭期 (Paperclip)
- `laf` : 法扶信件監控 (Laf Monitor)
"""

    # MAGI Status Command - require system-context prefix, not bare "狀態"
    _STATUS_CMD_EXACT = {"系統狀態", "運作狀態", "節點狀態", "機器狀態", "magi狀態", "magi status",
                         "status", "大腦狀態", "目前模型", "現在模型", "使用什麼模型"}
    if msg_lower in _STATUS_CMD_EXACT or (
        ("模型" in message) and len(msg_lower) <= 12 and any(kw in msg_lower for kw in ["目前", "現在", "使用", "模式"])
    ):
        node_status = orch._get_magi_status()
        brain_status = get_brain_status()
        collab_status = orch._get_collaboration_status()
        rt = get_melchior_runtime_status()
        model_line = "（目前抓不到模型資訊）"
        models = rt.get("models") if isinstance(rt.get("models"), list) else []
        if models:
            model_line = f"目前主要模型：`{models[0]}`"
        gpu_line = ""
        if rt.get("gpu_used_mb") is not None and rt.get("gpu_total_mb") is not None:
            gpu_line = f"\nMelchior GPU：{float(rt['gpu_used_mb'])/1024.0:.2f}/{float(rt['gpu_total_mb'])/1024.0:.2f} GB"
        return f"{node_status}\n\n{brain_status}\n\n{collab_status}\n\n🧩 模型資訊：{model_line}{gpu_line}"

    # Code Auto-Fix Command
    if any(kw in msg_lower for kw in ["自動修復code", "修復code資料夾", "autofix code", "auto fix code", "修復程式碼"]):
        try:
            from skills.management.code_autofix import autofix_codebase
            target = "magi" if "magi" in msg_lower else "code"
            dry_run = any(k in msg_lower for k in ["dry run", "preview", "只分析", "僅檢查"])
            include_tests = any(k in msg_lower for k in ["含測試", "include tests", "含 tests"])
            internalize = any(k in msg_lower for k in ["內化", "internalize", "技能化"])
            result = autofix_codebase(
                target=target,
                max_files=80,
                max_rounds=2,
                dry_run=dry_run,
                include_tests=include_tests,
                task_hint=message,
                internalize_skill=internalize,
                internalize_name="casper-autofix-knowledge",
            )
            if not result.get("success") and result.get("error"):
                return f"❌ 自動修復啟動失敗: {result.get('error')}"
            lines = [
                f"🛠️ **Code Auto-Fix 完成** (`{result.get('target', target)}`)",
                f"- 掃描檔案: {result.get('scanned_files', 0)}",
                f"- 發現語法問題: {result.get('syntax_issue_files', 0)}",
                f"- 修復成功: {result.get('fixed_files', 0)}",
                f"- 修復失敗: {result.get('failed_files', 0)}",
            ]
            verify_errors = result.get("verify", {}).get("errors", [])
            if verify_errors:
                lines.append(f"⚠️ 驗證錯誤數: {len(verify_errors)}")
            if result.get("internalized", {}).get("success"):
                lines.append(f"🧬 已內化技能: `{result['internalized'].get('skill_folder')}`")
            return "\n".join(lines)
        except Exception as e:
            return f"❌ 自動修復流程失敗: {e}"

    if any(kw in msg_lower for kw in ["內化code", "code技能化", "內化 code", "skillize code", "code internalize"]):
        try:
            from skills.management.auto_skill import AutoSkill

            autoskill = AutoSkill()
            source_dir = str(get_magi_root_dir())
            if "legacy" in msg_lower or "archive" in msg_lower:
                source_dir = str(get_legacy_code_root())
            force = any(k in msg_lower for k in ["force", "重建", "重新內化"])
            result = autoskill.internalize_codebase_as_skills(
                source_dir=source_dir,
                max_files=60,
                force=force,
                auto_activate=True,
                enable_release=True,
                canary_percent=20,
                promote_min_runs=12,
                promote_max_failure_rate=0.2,
            )
            if result.get("success"):
                canary_started = 0
                stable_set = 0
                for item in result.get("items", []):
                    rel = item.get("release", {}) or {}
                    if isinstance(rel.get("canary"), dict) and rel.get("canary", {}).get("success"):
                        canary_started += 1
                    if isinstance(rel.get("stable"), dict) and rel.get("stable", {}).get("success"):
                        stable_set += 1
                return (
                    "🧬 CODE 內化完成\n"
                    f"- Source: `{result.get('source_dir')}`\n"
                    f"- 掃描: {result.get('scanned_files', 0)}\n"
                    f"- 技能新增/更新: {result.get('created_skills', 0)}\n"
                    f"- 略過: {result.get('skipped_files', 0)}\n"
                    f"- Canary 啟動: {canary_started}\n"
                    f"- Stable 設定: {stable_set}"
                )
            return f"❌ CODE 內化失敗: {result.get('message', result.get('error', 'unknown'))}"
        except Exception as e:
            return f"❌ CODE 內化流程失敗: {e}"

    if any(kw in msg_lower for kw in ["導入auto-skill", "import auto-skill", "toolsai auto-skill"]):
        try:
            from skills.management.auto_skill import AutoSkill

            autoskill = AutoSkill()
            result = autoskill.import_toolsai_auto_skill(notify_dc=True)
            if result.get("success"):
                dc = result.get("dc_notify", {}) if isinstance(result.get("dc_notify"), dict) else {}
                return (
                    "📥 Toolsai auto-skill 導入完成\n"
                    f"- 新增知識: {result.get('learned', 0)}\n"
                    f"- 檔案數: {len(result.get('imported_files', []))}\n"
                    f"- DC通知: line={dc.get('line')} discord={dc.get('discord')}"
                )
            return f"❌ 導入失敗: {result.get('message', result.get('error', 'unknown'))}"
        except Exception as e:
            return f"❌ 導入 auto-skill 流程失敗: {e}"

    if any(kw in msg_lower for kw in ["code cycle", "自動巡檢", "工作流程自動化", "流程自動化"]):
        try:
            from scripts.code_skill_cycle import run_cycle

            result = run_cycle()
            if not result.get("success"):
                return "❌ 自動巡檢流程失敗。"
            af = result.get("autofix", {})
            ci = result.get("code_internalization", {})
            return (
                "⚙️ 自動巡檢完成\n"
                f"- AutoFix: fixed={af.get('fixed_files',0)} failed={af.get('failed_files',0)}\n"
                f"- Code->Skill: created={ci.get('created_skills',0)} skipped={ci.get('skipped_files',0)}"
            )
        except Exception as e:
            return f"❌ 自動巡檢執行失敗: {e}"

    if "重試摘要佇列自動" in message or "retry_summary_queue_auto" in msg_lower:
        try:
            import json as _json
            import subprocess as _subprocess
            py = os.environ.get("MAGI_SKILL_PYTHON", f"{_MAGI_ROOT}/venv/bin/python3").strip()
            if not py or not os.path.exists(py):
                py = sys.executable or "python3"
            jc = f"{_MAGI_ROOT}/skills/judgment-collector/action.py"
            cp = _subprocess.run(
                [py, jc, "--task", "retry_summary_queue_auto {\"notify\": false}"],
                capture_output=True,
                text=True,
                timeout=420,
            )
            out = (cp.stdout or "").strip()
            if cp.returncode != 0:
                return f"❌ 摘要補跑失敗（exit={cp.returncode}）: {(cp.stderr or out)[:220]}"
            data = {}
            try:
                data = _json.loads(out or "{}")
            except Exception:
                data = {}
            return (
                "📚 摘要補跑完成\n"
                f"- 處理: {data.get('processed', 0)}\n"
                f"- 改善: {data.get('improved', 0)}\n"
                f"- 剩餘: {data.get('remaining', 0)}\n"
                f"- 模式: {data.get('mode', 'tiered')}"
            )
        except Exception as e:
            return f"❌ 摘要補跑流程失敗: {e}"

    if any(k in message for k in ["查判決", "找判決", "判決搜尋", "搜尋判決", "收集判決", "判決搜集", "搜尋最高法院判決"]):
        if orch._looks_like_capability_question(message):
            return (
                "✅ **我可以幫您查判決！**\n\n"
                "• 直接輸入：`查判決 傷害`\n"
                "• 也可提供案號：`查判決 113年度上訴字第12號`"
            )
        return orch._run_judgment_collector_command(message, notify=False)

    if message.startswith("翻譯 ") or message.lower().startswith("translate "):
        return orch._run_inline_translation_command(user_id, message)

    if any(message.startswith(p) for p in ["摘要 ", "摘要\n", "精簡摘要 ", "精簡摘要\n", "詳細摘要 ", "詳細摘要\n", "短摘要 ", "長摘要 "]) or msg_lower.startswith("summarize ") or msg_lower.startswith("summary "):
        from api.pipelines.specialized_commands import run_inline_summary_command
        return run_inline_summary_command(orch, message)

    if message.startswith("製作音樂 ") or message.startswith("生成音樂 ") or message.lower().startswith("make music "):
        try:
            from skills.bridge.tri_sage_collab import generate_music

            prompt = (
                message.replace("製作音樂 ", "", 1)
                .replace("生成音樂 ", "", 1)
                .replace("make music ", "", 1)
                .strip()
            )
            if not prompt:
                return "❓ 請提供音樂描述。"
            result = generate_music(prompt, duration_sec=30)
            if result.get("success"):
                return f"🎵 音樂已產生：`{result.get('path','')}`（{result.get('provider','tri-sage')}）"
            return f"❌ 音樂生成失敗: {result.get('error')}"
        except Exception as e:
            return f"❌ 音樂生成流程失敗: {e}"

    # Teach / Internalize Commands
    if any(message.startswith(prefix) for prefix in ["教學檔案", "@MAGI 教學檔案", "teach file", "@MAGI teach file"]):
        try:
            from skills.management.auto_skill import AutoSkill
            autoskill = AutoSkill()
            tip_file = (
                message.replace("@MAGI 教學檔案", "")
                .replace("@MAGI teach file", "")
                .replace("教學檔案", "")
                .replace("teach file", "")
                .strip()
            )
            if not tip_file:
                return "❓ 請提供教學檔案路徑。"
            result = autoskill.learn_from_file(tip_file)
            return result.get("message", "📘 教學檔案已處理。")
        except Exception as e:
            return f"❌ 教學檔案處理失敗: {e}"

    if any(message.startswith(prefix) for prefix in ["教學 ", "@MAGI 教學", "teach ", "@MAGI teach"]):
        try:
            from skills.management.auto_skill import AutoSkill
            autoskill = AutoSkill()
            lesson = (
                message.replace("@MAGI 教學", "")
                .replace("@MAGI teach", "")
                .replace("教學 ", "")
                .replace("teach ", "")
                .strip()
            )
            if not lesson:
                return "❓ 請提供教學內容。"
            result = autoskill.teach(lesson, context="user-teach", source=f"{role}:{user_id}")
            return result.get("message", "🧠 教學完成。")
        except Exception as e:
            return f"❌ 教學失敗: {e}"

    if any(message.startswith(prefix) for prefix in ["內化技能", "@MAGI 內化技能", "internalize skill", "@MAGI internalize skill"]):
        try:
            from skills.management.auto_skill import AutoSkill
            autoskill = AutoSkill()
            name = (
                message.replace("@MAGI 內化技能", "")
                .replace("@MAGI internalize skill", "")
                .replace("內化技能", "")
                .replace("internalize skill", "")
                .strip()
            )
            result = autoskill.internalize_as_skill(
                skill_name=name or "casper-learned-skill",
                description="Internalized user-taught CASPER knowledge.",
                auto_activate=True,
            )
            if result.get("success"):
                return f"{result.get('message')}\n路徑: `{result.get('skill_path')}`"
            return f"❌ 內化技能失敗: {result.get('message')}"
        except Exception as e:
            return f"❌ 內化技能失敗: {e}"

    # Web Research Commands — only trigger on explicit search intent
    _web_search_explicit = re.search(
        r"^(?:搜尋|search|research|/search|查一下|找一下|搜一下|google|幫我搜|幫我查一下|執行網路研究|進行網路研究|網路研究|網路搜尋|幫我查詢|請幫我查詢)\s*[:：]?\s*",
        msg_lower,
    )
    if _web_search_explicit:
        # Extract the topic (remove command keywords)
        topic = message
        for kw in ["research", "搜尋", "search", "/search", "查一下", "找一下", "搜一下",
                    "google", "幫我搜", "幫我查一下", "執行網路研究", "進行網路研究",
                    "網路研究", "網路搜尋", "幫我查詢", "請幫我查詢", "@MAGI", "@magi"]:
            topic = re.sub(re.escape(kw), "", topic, flags=re.IGNORECASE).strip()
        # Strip colon separators
        topic = re.sub(r"^[:：]\s*", "", topic).strip()
        # Also strip filler words
        topic = re.sub(r"^(?:請|幫我|能不能|可以|一下|幫忙)\s*", "", topic).strip()

        if len(topic) < 2:
            return "🔍 請告訴我要搜尋什麼主題。例如：'搜尋 AI agent 2024'"

        logger.info(f"🌐 Web Research requested: {topic}")
        result = research_topic(topic, depth=3)

        if result.get("sources"):
            return orch._summarize_web_results(topic, result)
        else:
            return f"🔍 找不到關於「{topic}」的資訊。"

    # URL Fetch Command — only trigger when message contains a URL
    if any(kw in msg_lower for kw in ["fetch", "抓取", "讀取網頁"]) and re.search(r'https?://', message):
        import re
        urls = re.findall(r'https?://[^\s]+', message)
        if urls:
            result = fetch_url_content(urls[0])
            if result["success"]:
                return f"📄 **{result['title']}**\n\n{result.get('content', '')[:2000]}..."
            else:
                return f"❌ 無法抓取網頁: {result['error']}"
        return "🔗 請提供要抓取的網址。"

    # Memory Command (Remember) — require keyword followed by content to memorize (space or specific content)
    # "記住 XXX" = memory write; "記住不要忘了" = natural language, not a memory write
    _is_memory_cmd = any(msg_lower.startswith(kw) for kw in ["remember ", "save memory ", "memorize "])
    if not _is_memory_cmd and msg_lower.startswith("記住"):
        # Chinese: require "記住 " (with space) or "記住我/車牌/密碼/..." (concrete object after 記住)
        _after = message[2:].strip()
        _is_memory_cmd = bool(_after) and not _after.startswith(("不", "別", "千萬", "要", "這"))
    if not _is_memory_cmd:
        _is_memory_cmd = any(msg_lower.startswith(kw) for kw in ["請記住 ", "幫我記住 "])
    if _is_memory_cmd:
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以寫入記憶（系統改動指令）。"
        content = message
        for kw in ["remember", "記住", "save memory", "memorize", "請記住", "幫我記住"]:
            content = content.replace(kw, "").strip()

        if len(content) < 2:
            return "🧠 請告訴我要記住什麼？例如：'記住我的車牌是 ABC-1234'"

        from skills.memory.mem_bridge import remember
        remember(
            content,
            source=f"user_chat_{user_id}",
            metadata={
                "verified": True,
                "confidence": 0.94,
                "source_type": "user_confirmed",
                "role": "user",
            },
        )
        return f"🧠 **已存入記憶庫**\n內容: {content}"

    # Memory Command (Forget)
    if any(kw in msg_lower for kw in ["forget", "刪除記憶", "忘記", "delete memory"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以刪除記憶（系統改動指令）。"
        content = message
        for kw in ["forget", "刪除記憶", "忘記", "delete memory", "把這段記憶刪掉", "請把這段記憶刪掉", "這是錯的"]:
            content = content.replace(kw, "").strip()

        if len(content) < 2:
            # User might just say "delete this", imply context?
            # For now require content.
            return "🧠 請告訴我要刪除哪段記憶？例如：'刪除關於夏油傑的記憶'"

        from skills.memory.mem_bridge import forget
        success, result_msg = forget(content)

        if success:
            return f"🗑️ **已刪除記憶**\n{result_msg}"
        else:
            return f"⚠️ **刪除失敗**: {result_msg}"

    # Image Generation Command — require drawing-specific prefix
    # Exclude "畫面" (screen/picture), "畫成" (turn into) which are not drawing requests
    _draw_starts = ["畫圖", "畫一", "畫個", "畫張", "畫幅", "幫我畫", "請畫", "draw ", "/draw"]
    _draw_kw = ["/draw", "generate image", "產生圖片"]
    if (any(kw in msg_lower for kw in _draw_kw) or any(msg_lower.startswith(p) for p in _draw_starts)) and \
       not msg_lower.startswith("畫面") and not msg_lower.startswith("畫成"):
        # Extract the prompt
        prompt = message
        for kw in ["/draw", "畫", "draw", "產生圖片", "generate image", "幫我", "請", "畫圖", "一張", "一個"]:
            prompt = prompt.replace(kw, "").strip()

        if len(prompt) < 2:
            return "🎨 請描述您想要的圖片內容。例如：'畫一隻可愛的貓咪'"

        logger.info(f"🎨 Image Generation requested: {prompt}")

        from skills.bridge.melchior_bridge import generate_image
        result = generate_image(prompt)

        if result.get("success"):
            return f"🎨 **圖片生成成功！ (By 3rd-Child Melchior)**\n提示詞: {prompt}\n{result.get('message', '工程部門 (Melchior) 已完成繪圖。')}"
        else:
            return f"❌ **Melchior 回報錯誤**: {result.get('error', 'Unknown error')}"

    # Brain Switching Commands
    # Triggered by: "switch to", "big brain", "local mode", "切換", "本地"
    # Note: distributed mode disabled — all inference is local-first
    if any(kw in msg_lower for kw in ["switch to", "big brain", "distributed", "分散式", "最強模式", "activate big brain"]):
         if role != "admin":
             return "⛔ 抱歉，只有管理員可以切換推理模式（系統改動指令）。"
         return "ℹ️ 目前使用本地 oMLX 推理（摘要/通用/視覺辨識）。"

    if any(kw in msg_lower for kw in ["local mode", "go local", "independent", "本地模式", "切回本地", "release engineer"]):
         if role != "admin":
             return "⛔ 抱歉，只有管理員可以切換推理模式（系統改動指令）。"
         return switch_brain_mode("local")

    # Big Brain Repair
    if any(kw in msg_lower for kw in ["修理大腦", "修復大腦", "修理melchior", "修復melchior", "repair big brain", "repair melchior"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以修復推理叢集（系統改動指令）。"
        try:
            timeout = 300
            m = re.search(r"(\d{2,4})\s*(?:秒|sec|s)", msg_lower)
            if m:
                timeout = max(60, min(int(m.group(1)), 900))
            repaired = repair_big_brain(timeout_sec=timeout, force_cycle=True)
            ok = bool(repaired.get("success"))
            mode_after = str(repaired.get("mode_after") or "unknown")
            remote_h = repaired.get("remote_health") if isinstance(repaired.get("remote_health"), dict) else {}
            remote_msg = str(remote_h.get("message") or "")
            if ok:
                return f"✅ 大腦模式修復完成\n- 目前模式：`{mode_after}`\n- 遠端健康：{remote_msg or 'OK'}"
            return f"⚠️ 修復已執行，但遠端仍未恢復\n- 目前模式：`{mode_after}`\n- 診斷：{remote_msg or 'unknown'}"
        except Exception as e:
            return f"❌ 修復大腦模式失敗：{e}"

    # NGL auto-calibration
    if any(kw in msg_lower for kw in ["校準ngl", "自動校準ngl", "ngl calibrate", "校準大腦"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以校準 NGL（系統改動指令）。"
        try:
            target = 8.0
            tol = 0.5
            m_target = re.search(r"(\d+(?:\.\d+)?)\s*gb", msg_lower)
            if m_target:
                target = max(2.0, min(float(m_target.group(1)), 24.0))
            m_tol = re.search(r"[±\+\-]\s*(\d+(?:\.\d+)?)\s*gb", msg_lower)
            if m_tol:
                tol = max(0.1, min(float(m_tol.group(1)), 4.0))
            cal = calibrate_distributed_ngl(target_gb=target, tolerance_gb=tol, max_rounds=4, min_ngl=8, max_ngl=80)
            rec = cal.get("recommended_ngl")
            note = str(cal.get("note") or "")
            best = cal.get("best_delta_gb")
            if cal.get("success"):
                return f"✅ NGL 校準完成\n- 目標：{target:.2f}GB ± {tol:.2f}GB\n- 建議 NGL：`{rec}`\n- 結果：達標"
            return (
                f"⚠️ NGL 校準已完成（最佳努力）\n"
                f"- 目標：{target:.2f}GB ± {tol:.2f}GB\n"
                f"- 建議 NGL：`{rec}`\n"
                f"- 最佳偏差：{best if best is not None else 'unknown'} GB\n"
                f"- 說明：{note or 'not_reached'}"
            )
        except Exception as e:
            return f"❌ NGL 校準失敗：{e}"

    # Night Talk Trigger (夜議模式)
    # Disconnects Melchior to allow daily tasks or independent processing
    if any(kw in msg_lower for kw in ["夜議", "night talk", "night meeting", "yiyi", "意議", "開始夜議", "start night talk"]):
         if role != "admin":
             return "⛔ 抱歉，只有管理員可以啟動夜議（系統改動指令）。"
         logger.info("🌙 Night Talk Initiated...")

         # Run in background via thread to avoid blocking
         def run_night_talk(uid):
             try:
                 from skills.magi.night_talk import start_night_talk
                 result = start_night_talk()

                 # Notify User
                 if hasattr(orch, 'notification_callback') and orch.notification_callback:
                     orch.notification_callback(uid, f"🌙 **夜議 (Yi Yi) 會議記錄**\n\n{result[:1500]}...\n(完整記錄已封存)", "Discord")
             except Exception as e:
                 logger.error(f"Night Talk Error: {e}")
                 if hasattr(orch, 'notification_callback'):
                     orch.notification_callback(uid, "❌ 夜議執行失敗，請查看日誌。", "Discord")

         orch._bg_task_pool.submit(run_night_talk, user_id)

         return "🌙 **夜議模式已啟動**\n正在切換至獨立模式 (Local Mode)...\nCasper 與 Melchior 即將開始審視今日錯誤 (請稍候)..."

    # Skill Genesis (Self-Evolution)
    # Triggered by: "learn to...", "build a skill...", "學會..."
    if any(kw in msg_lower for kw in ["learn to", "build skill", "create skill", "學會", "學習", "製作技能", "幫我寫一個", "build a skill to", "寫工具"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以觸發技能進化（系統改動指令）。"
        # Extract topic
        topic = message
        for kw in ["learn to", "build skill", "create skill", "學會", "學習", "製作技能", "幫我寫一個", "build a skill to", "可以幫我寫一個", "寫工具"]:
            topic = topic.replace(kw, "").strip()

        if len(topic) < 2:
            return "🔧 請告訴我您想讓我學會有什麼功能？例如：'學會畫圖' 或 '製作一個幣安查價技能'"

        logger.info(f"🧬 Skill Genesis Triggered: {topic}")
        return orch._start_skill_interview(
            str(user_id or ""),
            str(platform or ""),
            role,
            topic,
            trigger_reason="manual",
        )

    # Skill Version Listing / Rollback
    if any(kw in msg_lower for kw in ["技能版本", "skill versions", "list versions"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以查詢技能版本（系統改動指令）。"
        try:
            from skills.evolution.skill_genesis import list_skill_versions
            skill_name = (
                message.replace("技能版本", "")
                .replace("skill versions", "")
                .replace("list versions", "")
                .strip()
            )
            if not skill_name:
                return "🗂️ 請提供技能資料夾名稱，例如：`技能版本 generated-my-skill`"
            result = list_skill_versions(skill_name)
            if not result.get("success"):
                return f"❌ 讀取版本失敗: {result.get('error')}"
            versions = result.get("versions", [])[:8]
            if not versions:
                return "ℹ️ 此技能目前沒有可用版本快照。"
            lines = [f"🗂️ **{skill_name} 版本快照**"]
            for v in versions:
                lines.append(f"- {v.get('version_id')} ({v.get('reason', 'snapshot')})")
            return "\n".join(lines)
        except Exception as e:
            return f"❌ 技能版本查詢失敗: {e}"

    if any(kw in msg_lower for kw in ["回滾技能", "rollback skill"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以回滾技能（系統改動指令）。"
        try:
            from skills.evolution.skill_genesis import rollback_skill_version
            text = message.replace("回滾技能", "").replace("rollback skill", "").strip()
            parts = text.split()
            if not parts:
                return "♻️ 請提供技能名稱，例如：`回滾技能 generated-my-skill`"
            skill_name = parts[0]
            version_id = parts[1] if len(parts) > 1 else ""
            result = rollback_skill_version(skill_name, version_id=version_id)
            if result.get("success"):
                return (
                    f"♻️ 已回滾 `{skill_name}` 到版本 `{result.get('restored_version')}`。\n"
                    f"檔案: {', '.join(result.get('restored_files', []))}"
                )
            return f"❌ 回滾失敗: {result.get('error')}"
        except Exception as e:
            return f"❌ 回滾執行失敗: {e}"

    if any(kw in msg_lower for kw in ["技能ci", "skill ci", "技能健康檢查"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以執行技能 CI（系統改動指令）。"
        try:
            from skills.evolution.skill_genesis import run_skill_ci
            text = (
                message.replace("技能CI", "")
                .replace("技能ci", "")
                .replace("skill ci", "")
                .replace("技能健康檢查", "")
                .strip()
            )
            if not text:
                return "🧪 請提供技能名稱，例如：`技能CI generated-my-skill`"
            result = run_skill_ci(text, task="health check", attempt_repair=True)
            if result.get("success"):
                return f"✅ 技能 CI 通過：`{text}`"
            checks = result.get("checks", [])
            failed = [c for c in checks if not c.get("ok")]
            detail = failed[0].get("detail", "unknown") if failed else result.get("error", "unknown")
            return f"❌ 技能 CI 未通過：`{text}`\n原因: {detail}"
        except Exception as e:
            return f"❌ 技能 CI 執行失敗: {e}"

    if any(kw in msg_lower for kw in ["技能事件", "skill events", "技能健康總覽"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以查詢技能事件（系統改動指令）。"
        try:
            from skills.evolution.skill_genesis import get_skill_runtime_stats
            stats = get_skill_runtime_stats(limit=200)
            if not stats.get("success", True):
                return f"❌ 讀取技能事件失敗: {stats.get('error')}"
            total = stats.get("total", 0)
            by_event = stats.get("by_event", {})
            by_status = stats.get("by_status", {})
            return (
                "📊 **技能執行健康總覽**\n"
                f"- 事件總數: {total}\n"
                f"- 事件分布: {by_event}\n"
                f"- 狀態分布: {by_status}"
            )
        except Exception as e:
            return f"❌ 技能事件查詢失敗: {e}"

    if any(kw in msg_lower for kw in ["標記穩定版", "set stable"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以標記穩定版（系統改動指令）。"
        try:
            from skills.evolution.skill_genesis import set_stable_skill_version
            text = message.replace("標記穩定版", "").replace("set stable", "").strip()
            parts = text.split()
            if not parts:
                return "🏷️ 請提供技能名稱，例如：`標記穩定版 generated-my-skill`"
            skill_name = parts[0]
            version_id = parts[1] if len(parts) > 1 else ""
            result = set_stable_skill_version(skill_name, version_id=version_id, enforce=True)
            if result.get("success"):
                return f"🏷️ 已標記穩定版：`{skill_name}` -> `{result.get('stable_version')}`"
            return f"❌ 標記穩定版失敗: {result.get('error')}"
        except Exception as e:
            return f"❌ 標記穩定版執行失敗: {e}"

    if any(kw in msg_lower for kw in ["開始canary", "start canary"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以啟動 canary（系統改動指令）。"
        try:
            from skills.evolution.skill_genesis import start_canary_release
            text = message.replace("開始canary", "").replace("start canary", "").strip()
            parts = text.split()
            if len(parts) < 2:
                return "🧪 請提供技能與版本，例如：`開始canary generated-my-skill 20260213010101000000 20 12 0.15`"
            skill_name = parts[0]
            version_id = parts[1]
            canary_percent = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 10
            promote_min_runs = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
            try:
                promote_max_failure_rate = float(parts[4]) if len(parts) > 4 else None
            except Exception:
                promote_max_failure_rate = None
            result = start_canary_release(
                skill_name,
                version_id,
                canary_percent=canary_percent,
                min_runs=10,
                fail_threshold=3,
                max_failure_rate=0.5,
                auto_promote=True,
                promote_min_runs=promote_min_runs,
                promote_max_failure_rate=promote_max_failure_rate,
            )
            if result.get("success"):
                st = result.get("state", {})
                return (
                    f"🧪 Canary 已啟動：`{skill_name}` 版本 `{version_id}`，流量 {canary_percent}%\n"
                    f"Auto-Promote: runs>={st.get('promote_min_runs')} 且 failure_rate<={st.get('promote_max_failure_rate')}"
                )
            return f"❌ Canary 啟動失敗: {result.get('error')}"
        except Exception as e:
            return f"❌ Canary 啟動錯誤: {e}"

    if any(kw in msg_lower for kw in ["停止canary", "stop canary"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以停止 canary（系統改動指令）。"
        try:
            from skills.evolution.skill_genesis import stop_canary_release
            text = message.replace("停止canary", "").replace("stop canary", "").strip()
            if not text:
                return "🧪 請提供技能名稱，例如：`停止canary generated-my-skill`"
            result = stop_canary_release(text, reason="manual_stop")
            if result.get("success"):
                return f"🛑 Canary 已停止：`{text}`"
            return f"❌ 停止 Canary 失敗: {result.get('error')}"
        except Exception as e:
            return f"❌ 停止 Canary 錯誤: {e}"

    if any(kw in msg_lower for kw in ["同步技能到melchior", "sync skills to melchior", "melchior skills sync"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以同步技能到 Melchior（系統改動指令）。"
        try:
            from skills.bridge.melchior_manager import sync_skills_to_melchior

            text = (
                message.replace("同步技能到melchior", "")
                .replace("sync skills to melchior", "")
                .replace("melchior skills sync", "")
                .strip()
            )
            tokens = [t for t in text.split() if t]
            mode = ""
            force = False
            smoke = True
            for t in tokens:
                low = t.lower()
                if low in {"auto", "delta", "full"}:
                    mode = low
                elif low in {"force", "強制"}:
                    force = True
                elif low in {"nosmoke", "no-smoke"}:
                    smoke = False

            result = sync_skills_to_melchior(f"{_MAGI_ROOT}/skills", mode=mode, force=force, smoke_test=smoke)
            if result.get("success"):
                action = result.get("action", "ok")
                if action.startswith("skipped"):
                    return f"📦 Melchior 同步略過：{action}"
                ms = ""
                smoke_res = result.get("smoke") or {}
                if isinstance(smoke_res, dict) and smoke_res.get("checks"):
                    ok = smoke_res.get("ok")
                    ms = f"；smoke={'ok' if ok else 'fail'}"
                return f"📦 已同步技能到 Melchior（mode={result.get('mode','')}, files={result.get('zip_files',0)}){ms}"
            return f"❌ 同步到 Melchior 失敗: {result.get('error', 'unknown')}"
        except Exception as e:
            return f"❌ 同步到 Melchior 發生錯誤: {e}"

    if any(kw in msg_lower for kw in ["melchior狀態", "melchior status"]):
        try:
            from skills.bridge.melchior_manager import melchior_health

            h = melchior_health()
            if h.get("online"):
                models = h.get("models") or []
                return f"🟢 Melchior online ({h.get('mode','remote')}) models={models[:5]}"
            return f"🔴 Melchior offline: {h.get('error') or h.get('mode')}"
        except Exception as e:
            return f"❌ Melchior 狀態查詢失敗: {e}"

    if any(kw in msg_lower for kw in ["發布狀態", "release status"]):
        try:
            from skills.evolution.skill_genesis import get_skill_release_state
            text = message.replace("發布狀態", "").replace("release status", "").strip()
            if not text:
                return "📦 請提供技能名稱，例如：`發布狀態 generated-my-skill`"
            result = get_skill_release_state(text)
            if not result.get("success"):
                return f"❌ 讀取發布狀態失敗: {result.get('error')}"
            state = result.get("state", {})
            stats = state.get("stats", {})
            return (
                f"📦 **{text} 發布狀態**\n"
                f"- stable: {state.get('stable_version') or '未設定'}\n"
                f"- canary_active: {state.get('canary_active')}\n"
                f"- canary_version: {state.get('canary_version') or 'n/a'}\n"
                f"- canary_percent: {state.get('canary_percent', 0)}%\n"
                f"- auto_promote: {state.get('auto_promote', True)} (runs>={state.get('promote_min_runs', 10)}, failure_rate<={state.get('promote_max_failure_rate', 0.2)})\n"
                f"- last_promoted: {state.get('last_promoted_version') or 'n/a'}\n"
                f"- stats: runs={stats.get('runs',0)}, success={stats.get('success',0)}, fail={stats.get('fail',0)}"
            )
        except Exception as e:
            return f"❌ 發布狀態查詢失敗: {e}"

    # Iron Dome Dynamic Rules (Admin Only)
    if any(kw in msg_lower for kw in ["鐵穹規則", "iron dome rules", "iron_dome rules"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以查看鐵穹規則。"
        try:
            from skills.evolution.skill_genesis import list_iron_dome_patterns

            result = list_iron_dome_patterns(include_static=False, include_disabled=False, limit=40)
            if not result.get("success"):
                return f"❌ 讀取鐵穹規則失敗: {result.get('error','unknown')}"
            dynamic = result.get("dynamic", [])
            lines = [
                "🛡️ **鐵穹動態規則**",
                f"- dynamic_count: {result.get('dynamic_count', 0)}",
                f"- updated_at: {result.get('updated_at') or 'n/a'}",
            ]
            if dynamic:
                lines.append("最近規則:")
                for item in dynamic[:10]:
                    rid = item.get("id", "")
                    pat = item.get("pattern", "")[:80]
                    hits = item.get("hits", 0)
                    lines.append(f"- {rid} hits={hits} `{pat}`")
            else:
                lines.append("（目前沒有動態規則）")
            return "\n".join(lines)
        except Exception as e:
            return f"❌ 鐵穹規則查詢失敗: {e}"

    if any(kw in msg_lower for kw in ["加入鐵穹規則", "add iron dome rule"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以修改鐵穹規則。"
        try:
            from skills.evolution.skill_genesis import add_iron_dome_pattern

            pat = (
                message.replace("加入鐵穹規則", "")
                .replace("add iron dome rule", "")
                .strip()
            )
            if not pat:
                return "❓ 請提供 regex，例如：`加入鐵穹規則 rm\\s+-rf`"
            result = add_iron_dome_pattern(pat, reason="admin_add", source=f"{role}:{user_id}", enabled=True)
            if result.get("success"):
                return f"✅ 已加入鐵穹規則：`{result.get('id','')}`"
            return f"❌ 加入規則失敗: {result.get('error','unknown')}"
        except Exception as e:
            return f"❌ 加入規則流程失敗: {e}"

    if any(kw in msg_lower for kw in ["自動加固鐵穹", "auto harden iron dome"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以執行鐵穹加固。"
        try:
            from skills.evolution.skill_genesis import auto_harden_iron_dome_scope

            incident = (
                message.replace("自動加固鐵穹", "")
                .replace("auto harden iron dome", "")
                .strip()
            )
            if not incident:
                return "❓ 請貼上要用來加固的 incident 內容（錯誤訊息/攻擊樣本/日誌片段）。"
            result = auto_harden_iron_dome_scope(incident, source=f"{role}:{user_id}", max_new=3)
            added = result.get("added", [])
            return f"🛡️ 鐵穹加固完成：新增 {len(added)} 條規則。"
        except Exception as e:
            return f"❌ 鐵穹加固失敗: {e}"

    if any(kw in msg_lower for kw in ["供應鏈掃描", "supply chain scan", "supply chain audit", "npm audit", "鐵穹掃描套件"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以執行供應鏈掃描。"
        try:
            from skills.iron_dome import core as _id_core

            result = _id_core.audit_supply_chain()
            findings = result.get("findings", [])
            if result.get("ok") and not findings:
                return "🛡️ **供應鏈掃描完成：安全** ✅\n未發現已知惡意套件或可疑依賴。"
            lines = [f"🛡️ **供應鏈掃描完成：發現 {len(findings)} 項問題**"]
            for f in findings[:15]:
                sev = f.get("severity", "?")
                icon = "🚨" if sev == "CRITICAL" else "⚠️"
                lines.append(f"{icon} [{sev}] {f.get('package', '?')}@{f.get('version', '?')} — {f.get('detail', '')}")
                lines.append(f"   📁 {f.get('file', '')}")
            if len(findings) > 15:
                lines.append(f"...（還有 {len(findings) - 15} 項）")
            if not result.get("ok"):
                lines.append("\n⚠️ 建議立即移除 CRITICAL 級別的套件！")
            return "\n".join(lines)
        except Exception as e:
            return f"❌ 供應鏈掃描失敗: {e}"

    if any(kw in msg_lower for kw in ["核心變更待審", "core approvals", "pending core changes"]):
        try:
            from skills.magi.council_approval import format_pending_summary

            return format_pending_summary(limit=20)
        except Exception as e:
            return f"❌ 讀取核心待審清單失敗: {e}"

    if any(kw in msg_lower for kw in ["批准核心變更", "approve core"]):
        try:
            from skills.magi.council_approval import resolve_core_change

            text = (
                message.replace("批准核心變更", "")
                .replace("approve core", "")
                .strip()
            )
            if not text:
                return "❓ 請提供待審 ID，例如：`批准核心變更 ccr-20260213094500`"
            parts = text.split(maxsplit=1)
            approval_id = parts[0]
            note = parts[1] if len(parts) > 1 else ""
            result = resolve_core_change(approval_id, "approved", approver=user_id, note=note)
            if result.get("success"):
                return f"✅ 核心變更已核准：`{approval_id}`"
            return f"❌ 核准失敗：{result.get('error')}"
        except Exception as e:
            return f"❌ 核准流程錯誤：{e}"

    if any(kw in msg_lower for kw in ["拒絕核心變更", "reject core"]):
        try:
            from skills.magi.council_approval import resolve_core_change

            text = (
                message.replace("拒絕核心變更", "")
                .replace("reject core", "")
                .strip()
            )
            if not text:
                return "❓ 請提供待審 ID，例如：`拒絕核心變更 ccr-20260213094500 缺少回滾方案`"
            parts = text.split(maxsplit=1)
            approval_id = parts[0]
            note = parts[1] if len(parts) > 1 else ""
            result = resolve_core_change(approval_id, "rejected", approver=user_id, note=note)
            if result.get("success"):
                return f"🛑 核心變更已拒絕：`{approval_id}`"
            return f"❌ 拒絕失敗：{result.get('error')}"
        except Exception as e:
            return f"❌ 拒絕流程錯誤：{e}"

    skill_python = (os.environ.get("MAGI_SKILL_PYTHON") or "").strip()
    if not skill_python:
        skill_python = f"{_MAGI_ROOT}/venv/bin/python"
    if not os.path.exists(skill_python):
        skill_python = sys.executable or "python3"

    if any(k in msg_lower for k in ["法扶回報指令", "法扶指令", "回報指令"]):
        return orch._laf_report_command_help()

    # 法扶狀態手動更新：「[當事人E] 已開辦」「[當事人N] 已報結」
    try:
        from api.handlers.laf_handler import parse_laf_status_update
        _status_upd = parse_laf_status_update(message)
        if _status_upd and role == "admin":
            _ok = orch._update_laf_status_after_action(
                case_number=_status_upd.get("case_number", ""),
                client_name=_status_upd.get("client_name", ""),
                laf_case_no=_status_upd.get("laf_case_no", ""),
                case_reason_hint=_status_upd.get("case_reason_hint", ""),
                new_status=_status_upd["new_status"],
                action_label=f"手動更新（{_status_upd['status_label']}）",
            )
            if _ok:
                _target = _status_upd.get("client_name") or _status_upd.get("case_number") or _status_upd.get("laf_case_no")
                return f"✅ 已更新 {_target} 的法扶狀態為「{_status_upd['new_status']}」"
            else:
                # 多筆同名案件 → 提示使用者指定案號
                _hint = getattr(orch, "_ambiguous_laf_status_hint", "")
                if _hint:
                    orch._ambiguous_laf_status_hint = ""
                    return _hint
                _target = _status_upd.get("client_name") or _status_upd.get("case_number") or _status_upd.get("laf_case_no")
                return f"❌ 找不到 {_target} 的案件，無法更新狀態。請確認姓名或案號是否正確。"
    except Exception as _su_err:
        logger.debug("LAF status update parse skipped: %s", _su_err)

    laf_payload = orch._parse_laf_report_payload(message)
    if laf_payload:
        logger.info("📋 LAF report payload: %s (from message: %r)", laf_payload, message[:80])

        if not any([laf_payload.get("laf_case_no"), laf_payload.get("case_number"), laf_payload.get("client_name")]):
            return (
                "❓ 我知道你要做法扶回報，但缺少目標。\n"
                "請補：姓名、法扶案號（1140728-K-002）或案件系統編號（2026-0013）之一。"
            )

        laf_script = str(get_laf_script())
        if not os.path.exists(laf_script):
            return f"❌ 找不到法扶 orchestrator：{laf_script}"

        platform_hint = "Discord" if str(user_id).startswith("discord_") else ("Telegram" if str(user_id).startswith("telegram_") else "LINE")
        timeout_sec = int(os.environ.get("MAGI_LAF_REPORT_TIMEOUT_SEC", "2400"))

        def run_laf_report(uid: str, payload_obj: dict, platform_name: str):
            action = str(payload_obj.get("action") or "").strip()
            cmd = [skill_python, laf_script, "--mode", "portal-draft", "--action", action]
            if payload_obj.get("laf_case_no"):
                cmd.extend(["--laf-case-no", str(payload_obj.get("laf_case_no"))])
            if payload_obj.get("case_number"):
                cmd.extend(["--case", str(payload_obj.get("case_number"))])
            if payload_obj.get("client_name"):
                cmd.extend(["--client", str(payload_obj.get("client_name"))])
            if payload_obj.get("reason"):
                cmd.extend(["--reason", str(payload_obj.get("reason"))])
            fields = payload_obj.get("fields") if isinstance(payload_obj.get("fields"), dict) else {}
            if fields:
                cmd.extend(["--fields-json", json.dumps(fields, ensure_ascii=False)])
            if str(os.environ.get("MAGI_LAF_CHAT_DRY_RUN", "0")).strip().lower() in {"1", "true", "yes", "on"}:
                cmd.append("--dry-run")

            logger.info("📋 LAF subprocess cmd: %s", cmd)
            _screenshot_sent = False
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
                stdout_text = (proc.stdout or "").strip()
                stderr_text = (proc.stderr or "").strip()

                if proc.returncode != 0:
                    result_text = f"❌ 法扶{payload_obj.get('action_label','回報')}流程失敗（code={proc.returncode}）\n{(stderr_text or stdout_text)[:1200]}"
                else:
                    data = None
                    if stdout_text:
                        try:
                            data = json.loads(stdout_text)
                        except Exception:
                            m2 = re.search(r"(\{[\s\S]*\})\s*$", stdout_text)
                            if m2:
                                try:
                                    data = json.loads(m2.group(1))
                                except Exception:
                                    data = None
                    if isinstance(data, dict):
                        if data.get("ok"):
                            identity = data.get("identity") if isinstance(data.get("identity"), dict) else {}
                            cname = str(identity.get("client_name") or payload_obj.get("client_name") or "").strip()
                            laf_no = str(identity.get("laf_case_number") or payload_obj.get("laf_case_no") or "").strip()
                            osc_no = str(identity.get("case_number") or payload_obj.get("case_number") or "").strip()
                            preview = data.get("preview") if isinstance(data.get("preview"), dict) else {}
                            shot_url = ""
                            shot_path = ""
                            html_url = ""
                            if isinstance(preview.get("png_export"), dict):
                                shot_url = str(preview.get("png_export", {}).get("url") or "").strip()
                            shot_path = str(preview.get("png") or "").strip()
                            if isinstance(preview.get("html_export"), dict):
                                html_url = str(preview.get("html_export", {}).get("url") or "").strip()
                            if action == "go_live":
                                lines = [f"✅ 法扶{payload_obj.get('action_label','回報')}已完成填寫（尚未送出）"]
                            else:
                                lines = [f"✅ 法扶{payload_obj.get('action_label','回報')}已完成存檔（未送出）"]
                            target_parts = [x for x in [cname, laf_no, osc_no] if x]
                            if target_parts:
                                lines.append("目標：" + "｜".join(target_parts))
                            if payload_obj.get("reason"):
                                lines.append(f"說明：{payload_obj.get('reason')}")
                            if action != "go_live":
                                if shot_url:
                                    lines.append(f"畫面預覽：{shot_url}")
                                elif shot_path:
                                    lines.append(f"畫面截圖：{shot_path}")
                                if html_url:
                                    lines.append(f"頁面 HTML：{html_url}")
                            if action == "go_live":
                                dates = data.get("dates") if isinstance(data.get("dates"), dict) else {}
                                od = str(dates.get("opening_date") or "").strip()
                                pd = str(dates.get("poa_submit_date") or "").strip()
                                if od:
                                    lines.append(f"開辦通知日期：{od}")
                                if pd:
                                    lines.append(f"委任狀遞出日期：{pd}")
                                if shot_url:
                                    lines.append(f"畫面預覽：{shot_url}")
                                elif shot_path:
                                    lines.append(f"畫面截圖：{shot_path}")
                                if html_url:
                                    lines.append(f"頁面 HTML：{html_url}")
                                token = ""
                                try:
                                    e = orch._register_laf_go_live_submit_pending(
                                        platform=platform_name,
                                        requester_user_id=uid,
                                        payload=payload_obj,
                                        result_data=data,
                                    )
                                    token = str(e.get("token") or "").strip()
                                except Exception as reg_err:
                                    logger.warning(f"Register go_live submit pending failed: {reg_err}")
                                    token = ""
                                if token:
                                    lines.append("請確認以上畫面與資料是否正確（你或同事皆可確認）。")
                                    lines.append(f"回覆：`正確送出 {token}`")
                                    lines.append(f"取消：`取消送出 {token}`")
                            if action in {"fee", "condition"}:
                                docs = data.get("docs") if isinstance(data.get("docs"), dict) else {}
                                if action == "fee" and docs.get("pink_receipt"):
                                    lines.append(f"收據：{os.path.basename(str(docs.get('pink_receipt')))}")
                                if action == "condition" and docs.get("mediation_failure"):
                                    lines.append(f"證明：{os.path.basename(str(docs.get('mediation_failure')))}")
                            if action == "closing":
                                counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
                                if counts:
                                    # 統計摘要
                                    _stats = []
                                    for _key, _label in [
                                        ("meeting_count", "開會"), ("contact_count", "聯繫"),
                                        ("inq_count", "律見"), ("court_count", "開庭"),
                                        ("review_count", "閱卷"), ("document_count", "書狀"),
                                    ]:
                                        if _key in counts:
                                            _stats.append(f"{_label} {int(counts[_key] or 0)}")
                                    if _stats:
                                        lines.append(f"統計：{'／'.join(_stats)}")

                                    # 案號
                                    _court_name = str(counts.get("court_name") or "").strip()
                                    _case_year = str(counts.get("court_case_year") or "").strip()
                                    _case_code = str(counts.get("court_case_code") or "").strip()
                                    _case_no = str(counts.get("court_case_no") or "").strip()
                                    if _court_name and _case_year:
                                        lines.append(f"案號：{_court_name}{_case_year}年度{_case_code}字第{_case_no}號")

                                    # 結果
                                    _result = str(counts.get("closing_result") or "").strip()
                                    if _result:
                                        lines.append(f"結果：{_result[:80]}")

                                    # 裁判效力
                                    _doc_type = str(counts.get("closing_doc_type") or "").strip()
                                    _judg_eff = str(counts.get("judg_eff") or "").strip()
                                    if _doc_type or _judg_eff:
                                        lines.append(f"裁判：{_doc_type}{'，' + _judg_eff if _judg_eff else ''}")

                                    # 零值警告
                                    _label_map = {"meeting_count": "開會", "contact_count": "聯繫", "court_count": "開庭", "review_count": "閱卷", "document_count": "書狀"}
                                    _zeros = [_label_map[k] for k in _label_map if int(counts.get(k, 0) or 0) == 0]
                                    if _zeros:
                                        lines.append(f"⚠️ 以下為 0：{'、'.join(_zeros)}，請確認「扶助律師特別說明」是否需要修改")

                                # 上傳檔案數
                                _upload_bundle = data.get("upload_bundle") if isinstance(data.get("upload_bundle"), dict) else {}
                                _upload_files = _upload_bundle.get("pdf_files") or []
                                if _upload_files:
                                    lines.append(f"上傳：{len(_upload_files)} 份")

                                # 零值理由
                                _zero_reasons = data.get("zero_reasons") if isinstance(data.get("zero_reasons"), dict) else {}
                                if _zero_reasons:
                                    _zr_label_map = {"disc_times": "討論次數", "review_count": "閱卷", "court_count": "開庭", "document_count": "書狀"}
                                    lines.append("理由：")
                                    for _zk, _zv in _zero_reasons.items():
                                        lines.append(f"- {_zr_label_map.get(_zk, _zk)}：{_zv}")

                                # 安全政策
                                if os.environ.get("MAGI_LAF_DRAFT_ONLY", "1") == "1":
                                    lines.append("🔒 安全政策：目前僅暫存，不會代為送出。")
                                else:
                                    lines.append("可回覆「送出」由 CASPER 代為送出（請先確認平台畫面）。")
                            if action == "withdrawal":
                                counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
                                if counts:
                                    lines.append(
                                        "辦理情形：開會{meeting_count}／聯繫{contact_count}／開庭{court_count}／書狀{document_count}／閱卷{review_count}".format(
                                            meeting_count=int(counts.get("meeting_count", 0) or 0),
                                            contact_count=int(counts.get("contact_count", 0) or 0),
                                            court_count=int(counts.get("court_count", 0) or 0),
                                            document_count=int(counts.get("document_count", 0) or 0),
                                            review_count=int(counts.get("review_count", 0) or 0),
                                        )
                                    )
                            result_text = "\n".join(lines)
                            # 傳送截圖圖片（go_live + closing 都需要）
                            _screenshot_sent = False
                            if action in ("go_live", "closing") and shot_path and os.path.isfile(shot_path):
                                try:
                                    from skills.ops.red_phone import send_file_admin, send_discord_bot_file
                                    _caption = result_text[:800]
                                    _laf_topic = "laf_go_live" if action == "go_live" else ("laf_closing" if action == "closing" else "laf")
                                    _plat = str(platform_name or "").strip().lower()
                                    if _plat == "telegram":
                                        send_file_admin(file_path=shot_path, caption=_caption, topic_key=_laf_topic)
                                    elif _plat == "discord":
                                        send_discord_bot_file(file_path=shot_path, caption=_caption, topic_key=_laf_topic, source=_laf_topic)
                                    else:
                                        send_file_admin(file_path=shot_path, caption=_caption, topic_key=_laf_topic)
                                        send_discord_bot_file(file_path=shot_path, caption=_caption, topic_key=_laf_topic, source=_laf_topic)
                                    _screenshot_sent = True  # 避免 notification_callback 重複發送
                                except Exception as _img_err:
                                    logger.warning("LAF screenshot send failed: %s", _img_err)
                            # 回寫 DB：closing 成功 → legal_aid_status = "已報結"
                            #          withdrawal 成功 → "已報結"
                            if action in ("closing", "withdrawal") and data.get("server_verified"):
                                try:
                                    _upd_osc = osc_no or str(payload_obj.get("case_number") or "").strip()
                                    _upd_cli = cname or str(payload_obj.get("client_name") or "").strip()
                                    if _upd_osc or _upd_cli:
                                        orch._update_laf_status_after_action(
                                            case_number=_upd_osc,
                                            client_name=_upd_cli,
                                            new_status="已報結",
                                            action_label=f"報結（{action}）",
                                        )
                                except Exception as _db_err2:
                                    logger.warning("closing DB status update failed: %s", _db_err2)
                        else:
                            err = str(data.get("error") or "unknown").strip()
                            if err == "missing_target":
                                result_text = (
                                    f"❌ 法扶{payload_obj.get('action_label','回報')}失敗：缺少目標。\n"
                                    "請補上姓名、法扶案號或案件系統編號。"
                                )
                            elif err == "missing_case_folder":
                                result_text = (
                                    f"❌ 法扶{payload_obj.get('action_label','回報')}失敗：找不到案件資料夾。\n"
                                    "請先確認該案已建立資料夾並可由 DB 對應。"
                                )
                            elif err == "missing_reason":
                                result_text = (
                                    f"❌ 法扶{payload_obj.get('action_label','回報')}失敗：缺少『疑義原因』。\n"
                                    "請重送：`... 疑義回報 原因 <你的原因>`"
                                )
                            elif err == "missing_required_docs":
                                missing = data.get("missing") if isinstance(data.get("missing"), list) else []
                                miss_txt = "、".join(str(x) for x in missing) if missing else "必要文件"
                                result_text = (
                                    f"❌ 法扶{payload_obj.get('action_label','回報')}失敗：缺少文件：{miss_txt}\n"
                                    "請先把文件放入對應案件資料夾後再重試。"
                                )
                            elif err == "missing_required_dates":
                                missing = data.get("missing") if isinstance(data.get("missing"), list) else []
                                miss_txt = "、".join(str(x) for x in missing) if missing else "必要日期"
                                result_text = (
                                    f"❌ 法扶{payload_obj.get('action_label','回報')}失敗：視覺判讀日期不足（{miss_txt}）。\n"
                                    "請確認開辦通知書/委任狀內容清晰。"
                                )
                            elif err == "need_reason_for_low_counts":
                                label_map = {
                                    "meeting_count": "開會",
                                    "contact_count": "聯繫",
                                    "court_count": "開庭",
                                    "document_count": "書狀",
                                    "review_count": "閱卷",
                                }
                                lows = data.get("low_fields") if isinstance(data.get("low_fields"), list) else []
                                low_txt = "、".join(label_map.get(str(x), str(x)) for x in lows) if lows else "低值欄位"
                                result_text = (
                                    "⚠️ 結案回報暫停：以下統計 <= 0，需要你提供原因後才能存檔。\n"
                                    f"欄位：{low_txt}\n"
                                    "請回覆：`<當事人/案號> 結案回報 原因 <理由>`"
                                )
                            elif err == "portal_draft_failed":
                                result_text = (
                                    f"❌ 法扶{payload_obj.get('action_label','回報')}表單填寫失敗。\n"
                                    "可能原因：法扶網站登入逾時、頁面載入異常或按鈕找不到。\n"
                                    "請稍後重試，或手動在法扶系統確認。"
                                )
                            elif err == "identity_needs_manual_confirmation":
                                _identity = data.get("identity") or {}
                                _reason = _identity.get("manual_reason", "")
                                _conflicts = _identity.get("conflicts", [])
                                _hint_lines = [f"⚠️ 法扶{payload_obj.get('action_label','回報')}需要補充資訊："]
                                if _reason == "missing_case_or_laf_signal":
                                    _hint_lines.append("系統無法辨識案件，請補上以下任一資訊：")
                                    _hint_lines.append("• 法扶案號（如 1141223-E-021）")
                                    _hint_lines.append("• 案件系統編號（如 2025-0087）")
                                    _hint_lines.append("• 當事人姓名 + 案由")
                                    _hint_lines.append("")
                                    _hint_lines.append("範例：`1141223-E-021 結案` 或 `[當事人L] 更生 結案`")
                                elif _reason == "identity_signal_conflict":
                                    _hint_lines.append("找到的案件資訊有衝突，無法自動確認：")
                                    for _c in _conflicts[:3]:
                                        _hint_lines.append(f"• {_c.get('client_name','')} ({_c.get('laf_case_number','')}) — {_c.get('reason','')}")
                                    _hint_lines.append("")
                                    _hint_lines.append("請用更精確的法扶案號重試。")
                                elif "conflict" in _reason:
                                    _hint_lines.append(f"案件比對有衝突（{_reason}），請確認後用法扶案號重試。")
                                else:
                                    _hint_lines.append(f"原因：{_reason}")
                                    _hint_lines.append("請補上法扶案號或案件系統編號後重試。")
                                result_text = "\n".join(_hint_lines)
                            else:
                                result_text = f"❌ 法扶{payload_obj.get('action_label','回報')}存檔失敗：{err}"
                    else:
                        result_text = f"✅ 法扶{payload_obj.get('action_label','回報')}流程完成（未送出）。\n{stdout_text[:1200] if stdout_text else '(無輸出)'}"
            except subprocess.TimeoutExpired:
                result_text = f"⏳ 法扶{payload_obj.get('action_label','回報')}流程逾時（>{timeout_sec} 秒），請稍後重試。"
            except Exception as e:
                result_text = f"❌ 法扶{payload_obj.get('action_label','回報')}背景流程異常：{e}"

            try:
                if getattr(orch, "notification_callback", None) and not _screenshot_sent:
                    # 截圖已帶 caption 送出時不再重複發送文字（避免同頻道收到兩次）
                    _cb_topic = {"go_live": "laf_go_live", "closing": "laf_closing"}.get(action, "laf_dispatch")
                    orch.notification_callback(uid, result_text, platform_name, topic_key=_cb_topic)
            except Exception as notify_err:
                logger.warning(f"LAF report callback failed: {notify_err}")

        # 2026-03-29: removed local import threading (use module-level import)
        thread = threading.Thread(
            target=run_laf_report,
            args=(str(user_id), laf_payload, platform_hint),
            daemon=True,
        )
        thread.start()

        target_hint = laf_payload.get("client_name") or laf_payload.get("laf_case_no") or laf_payload.get("case_number") or "（未指定）"
        if str(laf_payload.get("action") or "") == "go_live":
            launch_line = f"⏳ 已啟動法扶{laf_payload.get('action_label','回報')}流程（先填寫並截圖，待確認後才送出）。"
        else:
            launch_line = f"⏳ 已啟動法扶{laf_payload.get('action_label','回報')}流程（只存檔不送出）。"
        return f"{launch_line}\n目標：{target_hint}\n完成後我會主動回報。"

    # ── 繳費通知手動標記已繳費 / 跳過 ──
    _dismiss_payment_kw = ""
    _dismiss_m = re.search(r"^(.+?)\s*(?:已繳費|已經繳費|繳費完畢|繳費了)\s*$", message.strip())
    if _dismiss_m:
        _dismiss_payment_kw = _dismiss_m.group(1).strip()
    else:
        for _dtrig in ("已繳費", "跳過繳費", "繳費跳過"):
            if message.strip().startswith(_dtrig):
                _dismiss_payment_kw = message.strip()[len(_dtrig):].strip()
                break
    if _dismiss_payment_kw:
        try:
            _action_script = f"{_MAGI_ROOT}/skills/file-review-orchestrator/action.py"
            _py = os.environ.get("MAGI_SKILL_PYTHON", "").strip()
            if not _py or not os.path.exists(_py):
                _py = sys.executable or "python3"
            _task_str = 'dismiss_payment ' + json.dumps({"case_keyword": _dismiss_payment_kw}, ensure_ascii=False)
            _proc = subprocess.run(
                [_py, _action_script, "--task", _task_str],
                capture_output=True, text=True, timeout=30,
            )
            _out = (_proc.stdout or "").strip()
            try:
                _result = json.loads(_out)
                _data = _result.get("data", {}) if isinstance(_result, dict) else {}
                _new = _data.get("new_dismissals", 0)
                _already = _data.get("already_dismissed", 0)
                if _new:
                    return f"✅ 已標記「{_dismiss_payment_kw}」為已繳費，後續不再通知。"
                elif _already:
                    return f"ℹ️ 「{_dismiss_payment_kw}」先前已標記為已繳費。"
                else:
                    return f"✅ 已記錄「{_dismiss_payment_kw}」為已繳費。"
            except Exception:
                return f"✅ 已標記「{_dismiss_payment_kw}」為已繳費。"
        except Exception as _e:
            logger.warning("dismiss_payment failed: %s", _e)
            return f"❌ 標記繳費狀態失敗：{type(_e).__name__}"

    # File Review Probe (chat-callable formal skill command)
    probe_aliases = ["閱卷查核", "查核閱卷", "卷宗查核", "查核卷宗", "卷宗檢核", "檢核卷宗"]
    if any(msg_lower.startswith(alias) for alias in probe_aliases):

        def _parse_probe_payload(raw_text: str):
            raw = (raw_text or "").strip()
            alias_hit = next((alias for alias in probe_aliases if raw.lower().startswith(alias)), "")
            remainder = raw[len(alias_hit):].strip() if alias_hit else raw
            if not remainder:
                return None

            # JSON payload mode.
            if remainder.startswith("{"):
                try:
                    payload = json.loads(remainder)
                    if isinstance(payload, dict):
                        return payload
                except Exception:
                    return None

            # Natural phrase mode: <法院> <案號>
            parts = remainder.split()
            if len(parts) < 2:
                return None
            court = parts[0].strip()
            case_text = parts[1].strip()
            m = re.match(r"(\d{2,3})\s*(?:年度)?\s*([^\d\s]+)\s*(?:字)?\s*(?:第)?\s*(\d+)\s*(?:號)?", case_text)
            if not m:
                return None
            case_type = re.sub(r"(字第|字|第)", "", (m.group(2) or "")).strip()
            return {
                "court_code": court,
                "year": m.group(1),
                "case_type": case_type,
                "case_number": m.group(3),
            }

        payload = _parse_probe_payload(message)
        if not payload:
            return (
                "❓ 指令格式：`閱卷查核 <法院> <案號>`\n"
                "例如：`閱卷查核 基隆 114訴1`\n"
                "或：`閱卷查核 {\"court_code\":\"KLD\",\"year\":\"114\",\"case_type\":\"訴\",\"case_number\":\"1\"}`"
            )

        action_script = f"{_MAGI_ROOT}/skills/file-review-orchestrator/action.py"
        if not os.path.exists(action_script):
            return f"❌ 找不到 skill 腳本：{action_script}"

        task_payload = {
            "court_code": str(payload.get("court_code", "")).strip(),
            "year": str(payload.get("year", "")).strip(),
            "case_type": str(payload.get("case_type", "")).strip(),
            "case_number": str(payload.get("case_number", "")).strip(),
            "client_name": str(payload.get("client_name", "")).strip(),
        }
        if not all([task_payload["court_code"], task_payload["year"], task_payload["case_type"], task_payload["case_number"]]):
            return "❌ 缺少必要欄位：court_code/year/case_type/case_number"

        platform_hint = "Discord" if str(user_id).startswith("discord_") else "LINE"
        timeout_sec = int(os.environ.get("MAGI_FILE_REVIEW_PROBE_TIMEOUT_SEC", "1800"))

        def run_probe(uid: str, payload_obj: dict, platform_name: str):
            def _sanitize_filereview_text(raw_text: str) -> str:
                t = str(raw_text or "").strip()
                if not t:
                    return ""
                tl = t.lower()
                looks_web_notice = (
                    ("尊敬的客戶" in t and "閱卷服務" in t)
                    or ("若您已完成閱卷" in t and "可下載狀態" in t)
                    or ("登入正確帳戶" in t and "雲端儲存空間" in t)
                    or ("<html" in tl and "</html>" in tl)
                    or ("<!doctype html" in tl)
                )
                if looks_web_notice:
                    return "⚠️ 偵測到網站提示頁文案（非系統通知文字），目前判定為暫無可下載檔案。"
                return t if len(t) <= 700 else (t[:700] + "…")

            task_text = f"probe {json.dumps(payload_obj, ensure_ascii=False)}"
            cmd = [skill_python, action_script, "--task", task_text]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
                stdout_text = (proc.stdout or "").strip()
                stderr_text = (proc.stderr or "").strip()

                if proc.returncode != 0:
                    result_text = f"❌ 閱卷查核失敗（code={proc.returncode}）\n{(stderr_text or stdout_text)[:1200]}"
                else:
                    data = None
                    if stdout_text:
                        try:
                            data = json.loads(stdout_text)
                        except Exception:
                            m2 = re.search(r"(\{[\s\S]*\})\s*$", stdout_text)
                            if m2:
                                try:
                                    data = json.loads(m2.group(1))
                                except Exception:
                                    data = None

                    if isinstance(data, dict):
                        if data.get("success"):
                            case_label = str(data.get("case", "")).strip()
                            status = str(data.get("result", "")).strip()
                            summary = _sanitize_filereview_text(str(data.get("message", "")))
                            if status == "Ready":
                                head = "✅ 閱卷查核完成：卷宗已可下載"
                            elif status == "Applied":
                                head = "📋 閱卷查核完成：目前為已聲請/處理中"
                            else:
                                head = f"ℹ️ 閱卷查核完成：{status or 'unknown'}"
                            result_text = "\n".join(x for x in [head, case_label, summary] if x)
                        else:
                            result_text = f"❌ 閱卷查核失敗：{str(data.get('error', 'unknown')).strip()}"
                    else:
                        result_text = f"✅ 閱卷查核流程完成。\n{stdout_text[:1200] if stdout_text else '(無輸出)'}"

            except subprocess.TimeoutExpired:
                result_text = f"⏳ 閱卷查核逾時（>{timeout_sec} 秒），請稍後重試。"
            except Exception as e:
                result_text = f"❌ 閱卷查核背景流程異常：{e}"

            try:
                if getattr(orch, "notification_callback", None):
                    orch.notification_callback(uid, result_text, platform_name)
            except Exception as notify_err:
                logger.warning(f"File-review probe callback failed: {notify_err}")

        thread = threading.Thread(
            target=run_probe,
            args=(str(user_id), task_payload, platform_hint),
            daemon=True,
        )
        thread.start()

        return (
            "⏳ 已啟動閱卷查核（只查核、不送出）。\n"
            f"目標：{task_payload['court_code']} {task_payload['year']}年{task_payload['case_type']}字第{task_payload['case_number']}號\n"
            "完成後會主動回報。"
        )

    # File Review Apply — 閱卷聲請 (chat-callable formal skill command)
    apply_aliases = ["閱卷聲請", "聲請閱卷", "申請閱卷", "聲請閱覽"]
    if any(msg_lower.startswith(alias) for alias in apply_aliases):

        def _parse_apply_payload(raw_text: str):
            raw = (raw_text or "").strip()
            alias_hit = next((alias for alias in apply_aliases if raw.lower().startswith(alias)), "")
            remainder = raw[len(alias_hit):].strip() if alias_hit else raw
            if not remainder:
                return None

            # JSON payload mode.
            if remainder.startswith("{"):
                try:
                    payload = json.loads(remainder)
                    if isinstance(payload, dict):
                        return payload
                except Exception:
                    return None

            # Natural phrase mode: <法院> <案號> [當事人]
            parts = remainder.split()
            if len(parts) < 2:
                return None
            court = parts[0].strip()
            case_text = parts[1].strip()
            m = re.match(r"(\d{2,3})\s*(?:年度)?\s*([^\d\s]+)\s*(?:字)?\s*(?:第)?\s*(\d+)\s*(?:號)?", case_text)
            if not m:
                return None
            case_type = re.sub(r"(字第|字|第)", "", (m.group(2) or "")).strip()
            result = {
                "court_code": court,
                "year": m.group(1),
                "case_type": case_type,
                "case_number": m.group(3),
            }
            # Optional: client_name or case category after case number
            if len(parts) >= 3:
                extra = parts[2].strip()
                if extra in ("刑事", "民事", "行政"):
                    pass  # category hint, already embedded in case_type
                else:
                    result["client_name"] = extra
            if len(parts) >= 4 and "client_name" not in result:
                result["client_name"] = parts[3].strip()
            return result

        payload = _parse_apply_payload(message)
        if not payload:
            return (
                "❓ 指令格式：`閱卷聲請 <法院> <案號> <當事人>`\n"
                "例如：`閱卷聲請 花蓮 115原侵訴1 王小明`\n"
                "或：`閱卷聲請 台北 114訴123 張三`\n"
                "（當事人未填時會嘗試從案件 DB 自動帶入）\n"
                "或：`閱卷聲請 {\"court_code\":\"HLD\",\"year\":\"115\",\"case_type\":\"原侵訴\",\"case_number\":\"1\",\"client_name\":\"王小明\"}`"
            )

        action_script = f"{_MAGI_ROOT}/skills/file-review-orchestrator/action.py"
        if not os.path.exists(action_script):
            return f"❌ 找不到 skill 腳本：{action_script}"

        task_payload = {
            "court_code": str(payload.get("court_code", "")).strip(),
            "year": str(payload.get("year", "")).strip(),
            "case_type": str(payload.get("case_type", "")).strip(),
            "case_number": str(payload.get("case_number", "")).strip(),
            "client_name": str(payload.get("client_name", "")).strip(),
        }
        if not all([task_payload["court_code"], task_payload["year"], task_payload["case_type"], task_payload["case_number"]]):
            return "❌ 缺少必要欄位：court_code/year/case_type/case_number"

        platform_hint = "Discord" if str(user_id).startswith("discord_") else "LINE"
        timeout_sec = int(os.environ.get("MAGI_FILE_REVIEW_APPLY_TIMEOUT_SEC", "1800"))

        # 2026-03-29: removed local import threading (use module-level import)

        def run_apply(uid: str, payload_obj: dict, platform_name: str):
            task_text = f"apply {json.dumps(payload_obj, ensure_ascii=False)}"
            cmd = [skill_python, action_script, "--task", task_text]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
                stdout_text = (proc.stdout or "").strip()
                stderr_text = (proc.stderr or "").strip()

                if proc.returncode != 0:
                    result_text = f"❌ 閱卷聲請失敗（code={proc.returncode}）\n{(stderr_text or stdout_text)[:1200]}"
                else:
                    data = None
                    if stdout_text:
                        try:
                            data = json.loads(stdout_text)
                        except Exception:
                            m2 = re.search(r"(\{[\s\S]*\})\s*$", stdout_text)
                            if m2:
                                try:
                                    data = json.loads(m2.group(1))
                                except Exception:
                                    data = None

                    if isinstance(data, dict):
                        if data.get("success"):
                            case_label = str(data.get("case", "")).strip()
                            msg = str(data.get("message", "")).strip()
                            result_text = f"📋 閱卷聲請已送出\n{case_label}\n{msg}".strip()
                        else:
                            result_text = f"❌ 閱卷聲請失敗：{str(data.get('error', 'unknown')).strip()}"
                    else:
                        result_text = f"📋 閱卷聲請流程完成。\n{stdout_text[:1200] if stdout_text else '(無輸出)'}"

            except subprocess.TimeoutExpired:
                result_text = f"⏳ 閱卷聲請逾時（>{timeout_sec} 秒），請稍後重試。"
            except Exception as e:
                result_text = f"❌ 閱卷聲請背景流程異常：{e}"

            try:
                if getattr(orch, "notification_callback", None):
                    orch.notification_callback(uid, result_text, platform_name)
            except Exception as notify_err:
                logger.warning(f"File-review apply callback failed: {notify_err}")

        thread = threading.Thread(
            target=run_apply,
            args=(str(user_id), task_payload, platform_hint),
            daemon=True,
        )
        thread.start()

        label = f"{task_payload['court_code']} {task_payload['year']}年{task_payload['case_type']}字第{task_payload['case_number']}號"
        client_hint = f"\n當事人：{task_payload['client_name']}" if task_payload.get("client_name") else ""
        return (
            f"⏳ 已啟動閱卷聲請。\n"
            f"目標：{label}{client_hint}\n"
            "完成後會主動回報。"
        )

    # Transcript downloader (chat-callable formal skill command)
    transcript_aliases = ["下載筆錄", "筆錄下載", "調閱筆錄", "筆錄調閱", "筆錄同步", "同步筆錄", "筆錄全同步", "筆錄更名", "更名筆錄"]
    if any(msg_lower.startswith(alias) for alias in transcript_aliases):

        transcript_script = f"{_MAGI_ROOT}/skills/transcript-downloader/action.py"
        if not os.path.exists(transcript_script):
            return f"❌ 找不到 skill 腳本：{transcript_script}"

        if any(msg_lower.startswith(x) for x in ["下載筆錄", "筆錄下載", "調閱筆錄", "筆錄調閱"]):
            # Require case number for direct download command.
            parts = message.strip().split(maxsplit=1)
            if len(parts) < 2:
                return "❓ 指令格式：`下載筆錄 <案號>`，例如：`下載筆錄 114年度訴字第123號`"

        task_text = message.strip()
        platform_hint = "Discord" if str(user_id).startswith("discord_") else "LINE"
        timeout_sec = int(os.environ.get("MAGI_TRANSCRIPT_TASK_TIMEOUT_SEC", "2400"))

        def run_transcript(uid: str, platform_name: str, task_value: str):
            def _basename(path_text: str) -> str:
                try:
                    s = str(path_text or "").strip()
                    return os.path.basename(s) if s else ""
                except Exception:
                    return ""

            def _format_transcript_details(payload: dict) -> list[str]:
                lines: list[str] = []
                cases = payload.get("cases")
                if isinstance(cases, list) and cases:
                    shown = 0
                    lines.append("案件明細：")
                    for row in cases:
                        if not isinstance(row, dict):
                            continue
                        if shown >= 6:
                            break
                        case_no = str(row.get("case_number") or "").strip()
                        court_case_no = str(row.get("court_case_number") or "").strip()
                        party = str(row.get("client_name") or "").strip()
                        label_parts = [x for x in [party, court_case_no or case_no] if x]
                        label = "｜".join(label_parts) if label_parts else (court_case_no or case_no or "未判斷案件")
                        files = row.get("files")
                        file_list = files if isinstance(files, list) else []
                        lines.append(f"{shown + 1}. {label}（{len(file_list)} 份）")
                        for fp in file_list[:2]:
                            bn = _basename(fp) or str(fp).strip()
                            if bn:
                                lines.append(f"- {bn}")
                        shown += 1
                    remaining = len([r for r in cases if isinstance(r, dict)]) - shown
                    if remaining > 0:
                        lines.append(f"...其餘 {remaining} 案略")
                elif isinstance(payload.get("files"), list) and payload.get("files"):
                    lines.append("檔案：")
                    for fp in payload.get("files", [])[:5]:
                        bn = _basename(fp) or str(fp).strip()
                        if bn:
                            lines.append(f"- {bn}")
                return lines

            cmd = [skill_python, transcript_script, "--task", task_value]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
                stdout_text = (proc.stdout or "").strip()
                stderr_text = (proc.stderr or "").strip()

                if proc.returncode != 0:
                    result_text = f"❌ 筆錄流程失敗（code={proc.returncode}）\n{(stderr_text or stdout_text)[:1200]}"
                else:
                    data = None
                    if stdout_text:
                        try:
                            data = json.loads(stdout_text)
                        except Exception:
                            m2 = re.search(r"(\{[\s\S]*\})\s*$", stdout_text)
                            if m2:
                                try:
                                    data = json.loads(m2.group(1))
                                except Exception:
                                    data = None

                    if isinstance(data, dict):
                        if data.get("success"):
                            lines = ["✅ 筆錄流程完成"]
                            if data.get("message"):
                                lines.append(str(data.get("message")))
                            if "downloaded_count" in data:
                                lines.append(f"下載數量：{data.get('downloaded_count', 0)}")
                            lines.extend(_format_transcript_details(data))
                            result_text = "\n".join(lines)
                        else:
                            result_text = f"❌ 筆錄流程失敗：{str(data.get('error', 'unknown')).strip()}"
                    else:
                        result_text = f"✅ 筆錄流程完成。\n{stdout_text[:1200] if stdout_text else '(無輸出)'}"
            except subprocess.TimeoutExpired:
                result_text = f"⏳ 筆錄流程逾時（>{timeout_sec} 秒），請稍後重試。"
            except Exception as e:
                result_text = f"❌ 筆錄流程背景執行異常：{e}"

            try:
                if getattr(orch, "notification_callback", None):
                    orch.notification_callback(uid, result_text, platform_name)
            except Exception as notify_err:
                logger.warning(f"Transcript callback failed: {notify_err}")

        thread = threading.Thread(
            target=run_transcript,
            args=(str(user_id), platform_hint, task_text),
            daemon=True,
        )
        thread.start()

        return "⏳ 已啟動筆錄流程，完成後會主動回報。"

    # Mock skill test (可從 TG/DC 呼叫，用模擬站驗證所有技能)
    mock_test_aliases = [
        "模擬測試", "mock test", "mock_test", "模擬站測試",
        "閱卷模擬測試", "法扶模擬測試", "模擬測試閱卷", "模擬測試法扶",
    ]
    if any(msg_lower.startswith(a) for a in mock_test_aliases):
        import subprocess as _sp, threading as _thr

        mock_skill_script = f"{_MAGI_ROOT}/skills/mock-test/action.py"
        skills_arg = "all"
        for alias in ("閱卷", "file_review", "file-review"):
            if alias in msg_lower:
                skills_arg = "file_review"
                break
        for alias in ("法扶", "laf"):
            if alias in msg_lower:
                skills_arg = "laf"
                break

        _pname = "Discord" if str(user_id).startswith("discord_") else "LINE"

        def _run_mock_test(uid, skills, pname):
            try:
                r = _sp.run(
                    [str(get_skill_python()),
                     mock_skill_script, "--task", skills],
                    capture_output=True, text=True, timeout=600,
                )
                out = r.stdout.strip()
                # Find summary line
                summary = ""
                for line in out.splitlines():
                    if "PASS" in line and "FAIL" in line and "共" in line:
                        summary = line.strip()
                reply = summary or out[-300:]
                orch.notification_callback(uid, f"✅ 模擬測試完成\n{reply}", pname)
            except Exception as e:
                orch.notification_callback(uid, f"❌ 模擬測試失敗: {e}", pname)

        t = _thr.Thread(target=_run_mock_test, args=(user_id, skills_arg, _pname), daemon=True)
        t.start()
        scope = {"all": "全套", "file_review": "閱卷", "laf": "法扶"}.get(skills_arg, "全套")
        return f"⏳ 正在執行{scope}模擬測試，完成後會主動回報結果…"

    # File-review download/check commands (chat-callable formal skill command)
    review_dl_aliases = ["下載閱卷", "閱卷下載", "檢查閱卷信箱", "閱卷到期檢查", "閱卷到期", "閱卷期限"]
    if any(msg_lower.startswith(alias) for alias in review_dl_aliases):

        review_script = f"{_MAGI_ROOT}/skills/file-review-orchestrator/action.py"
        if not os.path.exists(review_script):
            return f"❌ 找不到 skill 腳本：{review_script}"

        task_text = message.strip()
        platform_hint = "Discord" if str(user_id).startswith("discord_") else "LINE"
        timeout_sec = int(os.environ.get("MAGI_FILE_REVIEW_TASK_TIMEOUT_SEC", "2400"))

        def run_file_review(uid: str, platform_name: str, task_value: str):
            def _sanitize_filereview_text(raw_text: str) -> str:
                t = str(raw_text or "").strip()
                if not t:
                    return ""
                tl = t.lower()
                looks_web_notice = (
                    ("尊敬的客戶" in t and "閱卷服務" in t)
                    or ("若您已完成閱卷" in t and "可下載狀態" in t)
                    or ("登入正確帳戶" in t and "雲端儲存空間" in t)
                    or ("<html" in tl and "</html>" in tl)
                    or ("<!doctype html" in tl)
                )
                if looks_web_notice:
                    return "⚠️ 偵測到網站提示頁文案（非系統通知文字），目前判定為暫無可下載檔案。"
                return t if len(t) <= 700 else (t[:700] + "…")

            def _basename(path_text: str) -> str:
                try:
                    s = str(path_text or "").strip()
                    return os.path.basename(s) if s else ""
                except Exception:
                    return ""

            def _format_filereview_details(payload: dict) -> list[str]:
                lines: list[str] = []
                items = payload.get("items")
                if not isinstance(items, list):
                    items = []
                if items:
                    groups = {}
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        party = str(it.get("party") or "").strip()
                        court_case_no = str(it.get("court_case_no") or "").strip()
                        folder = str(it.get("folder") or "").strip()
                        key = (party, court_case_no, folder)
                        groups.setdefault(key, []).append(it)

                    if groups:
                        lines.append("案件明細：")
                        idx = 0
                        for (party, court_case_no, folder), grouped_items in groups.items():
                            if idx >= 6:
                                break
                            label_parts = [x for x in [party, court_case_no] if x]
                            if not label_parts and folder:
                                label_parts.append(os.path.basename(folder))
                            label = "｜".join(label_parts) if label_parts else "未判斷案件"
                            lines.append(f"{idx + 1}. {label}（{len(grouped_items)} 份）")
                            for it in grouped_items[:2]:
                                fn = str(it.get("file") or "").strip()
                                dst = str(it.get("dst") or "").strip()
                                if fn:
                                    lines.append(f"- {fn}")
                                elif dst:
                                    lines.append(f"- {_basename(dst) or dst}")
                            idx += 1
                        remaining = len(groups) - idx
                        if remaining > 0:
                            lines.append(f"...其餘 {remaining} 案略")
                elif isinstance(payload.get("files"), list) and payload.get("files"):
                    lines.append("檔案：")
                    for fp in payload.get("files", [])[:5]:
                        bn = _basename(fp) or str(fp).strip()
                        if bn:
                            lines.append(f"- {bn}")

                archive_summary = payload.get("archive_summary")
                if isinstance(archive_summary, dict):
                    unresolved = int(archive_summary.get("unresolved_count") or 0)
                    if unresolved > 0:
                        lines.append(f"⚠️ 待歸檔：{unresolved} 份")
                return lines

            cmd = [skill_python, review_script, "--task", task_value]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
                stdout_text = (proc.stdout or "").strip()
                stderr_text = (proc.stderr or "").strip()

                if proc.returncode != 0:
                    result_text = f"❌ 閱卷流程失敗（code={proc.returncode}）\n{(stderr_text or stdout_text)[:1200]}"
                else:
                    data = None
                    if stdout_text:
                        try:
                            data = json.loads(stdout_text)
                        except Exception:
                            m2 = re.search(r"(\{[\s\S]*\})\s*$", stdout_text)
                            if m2:
                                try:
                                    data = json.loads(m2.group(1))
                                except Exception:
                                    data = None

                    if isinstance(data, dict):
                        if data.get("success"):
                            lines = ["✅ 閱卷流程完成"]
                            if data.get("message"):
                                lines.append(_sanitize_filereview_text(str(data.get("message"))))
                            if "downloaded_count" in data:
                                lines.append(f"下載數量：{data.get('downloaded_count', 0)}")
                            lines.extend(_format_filereview_details(data))
                            result_text = "\n".join(lines)
                        else:
                            result_text = f"❌ 閱卷流程失敗：{str(data.get('error', 'unknown')).strip()}"
                    else:
                        result_text = f"✅ 閱卷流程完成。\n{stdout_text[:1200] if stdout_text else '(無輸出)'}"
            except subprocess.TimeoutExpired:
                result_text = f"⏳ 閱卷流程逾時（>{timeout_sec} 秒），請稍後重試。"
            except Exception as e:
                result_text = f"❌ 閱卷流程背景執行異常：{e}"

            try:
                if getattr(orch, "notification_callback", None):
                    orch.notification_callback(uid, result_text, platform_name)
            except Exception as notify_err:
                logger.warning(f"File-review callback failed: {notify_err}")

        thread = threading.Thread(
            target=run_file_review,
            args=(str(user_id), platform_hint, task_text),
            daemon=True,
        )
        thread.start()

        return "⏳ 已啟動閱卷流程，完成後會主動回報。"

    # Existing commands
    if "court" in msg_lower or "schedule" in msg_lower:
         return execute_skill("paperclip-control", [message])
    elif "laf" in msg_lower:
         return execute_skill("laf-monitor", [message])
    elif "meeting" in msg_lower:
         return execute_skill("meetings", ["list"])
    elif "summarize" in msg_lower or "summary" in msg_lower or "balthasar" in msg_lower:
         try:
             summary_result = summarize_text(message)
             if summary_result and summary_result.get("success", True):
                 text = summary_result.get("text") or summary_result.get("summary") or ""
                 if text:
                     return f"🍏 Balthasar: {text}"
             return "⚠️ Balthasar 摘要服務無可用結果，請稍後再試。"
         except Exception as e:
             logger.warning(f"Balthasar summary fallback due to error: {e}")
             from skills.bridge.grounded_ai import chat_casper
             return f"🍏 Balthasar 暫時不可用，改由 Casper 摘要：\n{chat_casper('請用繁體中文摘要：' + message)}"
    elif "melchior" in msg_lower and "vision" in msg_lower:
         return "👁️ Please send me an image for Melchior to analyze."

    # Code Analysis Command
    # Triggered by: "analyze code", "讀取程式碼", "code folder", "code 資料夾"
    if any(kw in msg_lower for kw in ["analyze code", "讀取程式碼", "code folder", "code資料夾", "連動模式", "改善建議", "read code"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以執行程式碼分析（避免洩漏系統內部指令/結構）。"

        # Extract basic params
        target = "code"
        if "magi" in msg_lower:
            target = "magi"

        logger.info(f"🧐 Parsing Codebase ({target})...")

        from skills.bridge.code_analysis import analyze_code
        # 2026-03-29: removed local import threading (use module-level import)

        platform_hint = "Discord" if str(user_id).startswith("discord_") else "LINE"

        def run_code_analysis(uid: str, platform_name: str, tgt: str, msg: str):
            try:
                report = analyze_code(tgt, msg)
                result_text = f"🧐 **程式碼分析報告**\n\n{report}"
            except Exception as e:
                result_text = f"❌ 程式碼分析失敗：{e}"

            try:
                if getattr(orch, "notification_callback", None):
                    orch.notification_callback(uid, result_text, platform_name)
            except Exception as notify_err:
                logger.warning(f"Code analysis callback failed: {notify_err}")

        orch._bg_task_pool.submit(run_code_analysis, str(user_id), platform_hint, target, message)

        return (
            "⏳ 已啟動程式碼分析，完成後會主動回報。\n"
            f"目標：{target}\n"
            "（此流程可能需要 1-3 分鐘，視資料夾大小而定）"
        )

    # No specific command matched. Check if auto skill genesis should trigger.
    # Trigger conditions:
    #   1. Explicit skill-related keywords (original behavior)
    #   2. EmbeddingRouter returned LOW tier but message looks actionable (new)
    _skill_genesis_kws = ["建立技能", "建立skill", "create skill", "自動化", "automate",
                          "寫一個", "寫個", "implement", "build a", "製作工具"]
    _explicit_skill_req = any(k in msg_lower for k in _skill_genesis_kws)

    _embed_low_but_actionable = False
    if not _explicit_skill_req and orch._should_attempt_auto_acquire(message, msg_lower):
        try:
            from skills.bridge.embedding_router import get_router as _get_embed_router
            _er = _get_embed_router()
            _er_result = _er.route(message) if _er.is_ready else None
            if _er_result:
                _er_skill, _er_score, _er_tier = _er_result
                if _er_tier == "LOW" and _er_score < 0.50:
                    _embed_low_but_actionable = True
                    logger.info(f"🧬 EmbeddingRouter LOW ({_er_score:.3f}), may trigger auto-acquire")
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8825, exc_info=True)

    if (
        orch._should_attempt_auto_acquire(message, msg_lower)
        and not orch._looks_like_capability_question(message)
        and (_explicit_skill_req or _embed_low_but_actionable)
    ):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以啟動自主演化/自動上線技能（系統改動指令）。"
        logger.info("🧩 Skill request or embedding gap detected, starting interview-driven skill creation...")
        return orch._start_skill_interview(
            str(user_id or ""),
            str(platform or ""),
            role,
            message,
            trigger_reason="gap",
        )

    # Everything else: let LLM handle it conversationally.
    logger.info("💬 No command matched, routing to LLM chat")
    return orch._handle_chat_async(user_id, message, platform_hint=platform)



def list_skills(orch):
    """
    Dynamically lists available skills by parsing SKILL.md frontmatter.
    """
    import os
    from skills.catalog import iter_top_level_skill_dirs

    skill_roots = [
        (f"{_MAGI_ROOT}/skills", "magi"),
        (os.path.join(os.path.expanduser("~"), ".openclaw", "skills"), "openclaw"),
    ]
    skills_found = []

    # Scan for all SKILL.md files
    try:
        for skills_dir, source in skill_roots:
            if not os.path.isdir(skills_dir):
                continue
            for entry in iter_top_level_skill_dirs(skills_dir):
                skill_path = os.path.join(entry.path, "SKILL.md")
                try:
                    with open(skill_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    # Simple frontmatter parsing (no yaml dependency)
                    name = entry.name
                    desc = "No description"
                    if content.startswith("---"):
                        parts = content.split("---", 2)
                        if len(parts) >= 3:
                            for line in parts[1].strip().split("\n"):
                                line = line.strip()
                                if line.startswith("name:"):
                                    name = line.split(":", 1)[1].strip().strip("'\"")
                                elif line.startswith("description:"):
                                    desc = line.split(":", 1)[1].strip().strip("'\"")
                    # Truncate long descriptions
                    if len(desc) > 80:
                        desc = desc[:77] + "..."
                    skills_found.append({"name": name, "desc": desc, "source": source})
                except Exception:
                    skills_found.append({"name": entry.name, "desc": "(Unable to parse)", "source": source})
    except Exception as e:
        logger.error(f"Error scanning skills: {e}")
        return "❌ 無法讀取技能列表。"

    # Format Output
    response = f"🧩 **MAGI 技能列表 (Skill Matrix)**\n"
    response += f"📦 已安裝 **{len(skills_found)}** 個技能模組\n\n"

    # Emoji map
    emoji_map = {
        "bridge": "🌉", "memory": "🧠", "research": "🌐",
        "law-firm": "⚖️", "browser": "🖥️", "identity": "🪪",
        "evolution": "🧬", "apple": "🍎", "ops": "⚙️",
        "maintenance": "🔧", "source_control": "📂", "synology": "💾",
        "brain_manager": "🧠"
    }

    for skill in sorted(skills_found, key=lambda s: s["name"]):
        emoji = emoji_map.get(skill["name"], "📌")
        src = str(skill.get("source") or "magi")
        response += f"{emoji} **{skill['name']}** [{src}]\n"
        response += f"  _{skill['desc']}_\n\n"

    response += "💡 *您可以直接對我下達相關指令，例如「查詢行程」、「分析程式碼」等。*"
    return response

