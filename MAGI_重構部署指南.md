# MAGI 重構部署指南

> 日期：2026-03-29
> 改動範圍：Channel-Aware Routing + Silent Except 修復 + Threading 修復

---

## 改動的檔案

| 檔案 | 改動內容 |
|------|---------|
| `api/orchestrator.py` | channel_context 參數、topic fast path、NL Router 條件化、ER 門檻調整、threading 修復、/help 更新 |
| `api/server.py` | Telegram message_thread_id → channel_context 傳遞 |
| `api/discord_bot.py` | Discord channel_id → channel_context 傳遞 |
| `api/channel_context.py` | **新檔案** — ChannelContext dataclass + 反查工具 |
| `skills/bridge/embedding_router.py` | 9 個 skill 新增 _EXTRA_PHRASES |
| `scripts/fix_silent_except.py` | **新檔案** — silent except 批量替換腳本 |

---

## 部署步驟

### Step 1：跑 Silent Except 修復腳本

```bash
cd ~/Desktop/MAGI

# 先看 dry run（不改任何東西，只顯示哪些會被改）
python3 scripts/fix_silent_except.py

# 確認沒問題後，加 --apply 實際寫入
python3 scripts/fix_silent_except.py --apply
```

預期結果：1,399 處 `except: pass` 被替換成 `logger.debug(...)` ，涵蓋 6,698 個檔案。

### Step 2：重啟所有進程

**重要**：因為 `globals()` singleton 的設計，不重啟不會載入新代碼。三個進程都要重啟：

```bash
# 依你的部署方式重啟，例如：
# 1. Flask Server
# 2. Discord Bot
# 3. Tools API

# 如果用 launchctl：
launchctl kickstart -k gui/$(id -u)/com.magi.server
launchctl kickstart -k gui/$(id -u)/com.magi.discord
launchctl kickstart -k gui/$(id -u)/com.magi.tools
```

### Step 3：驗證測試

依序測試以下場景：

#### 專屬頻道 Fast Path
- [ ] **TG 法扶 topic**：發「幫我做邱衣萱開辦回報」→ 應直接進 LAF handler（不走 NL Router）
- [ ] **TG 閱卷 topic**：發「下載閱卷」→ 應直接進 filereview handler
- [ ] **DC 法扶頻道**：發「邱衣萱報結回報」→ 應直接進 LAF handler
- [ ] **DC 判決頻道**：發「查判決 詐欺」→ 應直接進 judgment handler

#### 一般頻道（NL Router 已停用）
- [ ] **TG general**：發「今天天氣如何」→ 應直接到 LLM 聊天（不被攔截）
- [ ] **TG general**：發「我剛看了一個判決」→ 應正常聊天（不被 NL Router 攔截）
- [ ] **TG general**：發「幫我查張三的判決」→ 應被 EmbeddingRouter dispatch 到 judgment skill
- [ ] **DC general**：發「法扶的案件好複雜」→ 應正常聊天（不被「法扶」keyword 攔截）

#### 斜線指令（所有頻道都有效）
- [ ] `/help` → 應顯示新版指令列表（含 `/查判決`、`/翻譯` 等）
- [ ] `/查判決 詐欺` → 應觸發判決搜尋
- [ ] `/翻譯 Hello World` → 應觸發翻譯
- [ ] `/draw 一隻貓` → 應觸發圖片生成
- [ ] `/庭期` → 應顯示開庭排程

#### LINE（走 general 邏輯）
- [ ] 發「幫我翻譯這段英文」→ EmbeddingRouter dispatch（不走 NL Router）
- [ ] 發「今天有什麼行程」→ EmbeddingRouter 或 LLM 聊天
- [ ] 發 `/help` → 新版指令列表

#### Silent Except 驗證
- [ ] 開啟 DEBUG level logging，重現之前出過的 bug，確認 log 裡有 `silent-catch` 輸出

---

## 核心架構變化說明

### 之前的路由（41 道關卡瀑布）
```
訊息 → keyword 攔截 → NL Router subprocess → 50+ 個 early return
    → intent classifier → EmbeddingRouter → _handle_command → LLM
```

### 之後的路由
```
訊息
 ├─ 專屬頻道 → topic fast path → 對應 handler → (None 就 fallback 聊天)
 └─ 一般/LINE → slash cmd → pending confirm → ER(≥0.85) → classifier → LLM
```

### 為什麼 NL Router 在一般頻道停用

_NL_ROUTE_KWS 包含「法扶」「開辦」「判決」「除錯」等在法律日常對話中
極度常見的詞，且用子字串比對（`if kw in text`），導致大量正常對話被攔截
送到外部 subprocess，造成 MAGI「變白癡」。

停用後，這些功能改由：
1. **專屬頻道 fast path**（在對應 topic 裡自然語言就能觸發）
2. **EmbeddingRouter**（語意相似度 ≥ 0.85，能分辨「查判決」vs「聊到判決」）
3. **斜線指令**（`/查判決`，確定性觸發）

---

## 回滾方式

如果出問題，最簡單的回滾：

1. `git checkout api/orchestrator.py api/server.py api/discord_bot.py skills/bridge/embedding_router.py`
2. 刪除 `api/channel_context.py`
3. 重啟三個進程

Silent except 的修改如果要回滾：`git checkout api/ casper_ecosystem/ skills/`

---

## 後續可做的事（不急）

- [ ] 逐步把 `_handle_command` 的 if/elif 搬到 CommandRegistry
- [ ] 在 LINE Rich Menu 加上常用功能按鈕
- [ ] 統一 logging format（file_review_automation 的 self.log → 標準 logger）
- [ ] 拆分 orchestrator.py（等流程穩定後）
