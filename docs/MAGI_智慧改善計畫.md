# MAGI 對話智慧改善計畫

> 2026-03-26 架構分析 + 改善建議

---

## 一、現行訊息處理流水線

```
使用者訊息（TG/DC/LINE）
  │
  ├─ 0. _sanitize + _quick_fixed_reply（regex 固定回覆）
  ├─ 1. Intercept Chain（6 個多輪流程攔截）
  ├─ 2. Iron Dome（安全檢查）
  ├─ 3. 附件路由（有附件直接走 multimedia）
  ├─ 4. Codex 分散式命令
  ├─ 5. NL Router（subprocess intent_router.py, timeout 8s）
  ├─ 6. 硬編碼快速路徑（15+ 組關鍵字 → 跳過 LLM）
  │     行程/日曆 → _get_schedule
  │     狀態/模型 → 直接回傳
  │     記住/忘記 → mem_bridge
  │     加班費/資遣費 → labor_law
  │     找判決 → judgment_collector
  ├─ 7. 對話式意圖分發（regex 偵測問句/請求）
  ├─ 8. 語意安全技能路由（embedding-based）
  ├─ 9. 畫圖/翻譯等前綴匹配
  │
  └─ 10. Intent Classification + Embedding Router（核心決策）
        │
        ├─ IntentionClassifier（三層）
        │   ├─ LRU Cache（256 條）
        │   ├─ Regex 規則（25+ 條）
        │   ├─ Heuristic 評分（keyword counting）
        │   └─ LLM 分類（oMLX TAIDE-12b, timeout 15s）
        │
        ├─ EmbeddingRouter（ModernBERT cosine, 61 skills）
        │   ├─ DIRECT ≥ 0.75 → 直接 dispatch
        │   ├─ GUIDED ≥ 0.55 → 輔助 dispatch
        │   └─ LOW < 0.55 → 不路由
        │
        └─ 路由決策矩陣
            ├─ CMD + DIRECT → dispatch skill
            ├─ QUERY + match → dispatch skill / ask_casper
            ├─ CHAT + DIRECT → 覆蓋 CHAT → dispatch skill
            └─ CHAT + miss → chat_casper
                │
                ├─ recall 記憶 (top_k=3~4)
                ├─ 記憶不足？→ web research
                ├─ _generate() → oMLX TAIDE-12b
                └─ 品質防護（垃圾/鸚鵡/幻覺偵測）
```

---

## 二、核心問題分析

### 問題 1：路由層數過多，決策不可預測

**現狀**：至少 11 層 if/elif 串聯，同一訊息可能在不同層被不同邏輯處理。
- 「幫我查天氣」可能命中 NL Router(→CMD)、快速路徑(miss)、Intent Classifier(→CHAT)，最終走 chat_casper 而不上網
- 「今天行程」命中快速路徑(→_get_schedule)，完全跳過 LLM，但如果使用者說的是「我今天有什麼事要做」就 miss，走 LLM

**問題根源**：每層獨立判斷，沒有全局仲裁。先到先得，一旦某層命中就不給其他層機會。

**改善方案**：
```
建立統一 Router Pipeline：
1. 所有 router 並行打分：
   - KeywordRouter  → (match, confidence=1.0, handler)
   - EmbeddingRouter → (match, confidence=0.78, handler)
   - IntentRouter    → (match, confidence=0.65, handler)
2. Arbitrator 綜合決策：
   - 取 confidence 最高的
   - 如果最高分 < 0.5 → fallback to chat
   - 如果多個 router 衝突 → 優先 KeywordRouter > EmbeddingRouter > IntentRouter
```

### 問題 2：oMLX 單一推理 slot 是全系統瓶頸

**現狀**：`max_num_seqs=1`（VLM 限制），所有 LLM 任務串行排隊。
- Intent 分類佔 slot → 阻塞 chat 回覆
- 記憶摘要佔 slot → 阻塞 intent 分類
- 翻譯/摘要等 heavy task 佔 slot → 全部後續請求排隊

**改善方案**：
```
A. Intent 分類不用 LLM（最大改善）
   - 現有 EmbeddingRouter 已能區分大部分意圖
   - 改用 embedding similarity 做 intent 分類：
     CHAT: cosine_sim(input, chat_centroids) > threshold
     QUERY: cosine_sim(input, query_centroids) > threshold
     CMD: cosine_sim(input, cmd_centroids) > threshold
   - 完全不佔推理 slot，延遲從 15s 降到 0.1s

B. 優先佇列
   - 使用者對話 > intent 分類 > 背景任務
   - 實作：oMLX 前加 proxy queue，依 priority 排序

C. 記憶摘要用更小模型或規則
   - _summarize_memories_if_needed() 目前用 TAIDE-12b
   - 可改用截斷 + 關鍵字過濾（不需 LLM）
```

### 問題 3：Intent Classifier 三層不一致

**現狀**：Regex、Heuristic、LLM 各自獨立，先到先得。
- Regex 命中就直接返回，不給 LLM veto 機會
- 「幫我查天氣」被 `_RE_HELP_CMD` 匹配為 CMD（匹配「幫我...查」）
- 但使用者意圖是 QUERY

**改善方案**：
```
改為 ensemble 模式：
1. 三層全部跑完，各自給出 (intent, confidence)
2. 加權投票：
   - Regex 命中 → 高信心但非絕對
   - Heuristic → 中等信心
   - LLM → 低信心（因為常出錯且慢）
3. 最終取加權最高分
4. 實際上可以只用 Regex + EmbeddingRouter，省掉 LLM 那層
```

### 問題 4：搜尋觸發邏輯不夠智慧

**現狀**：
- `_needs_research()` 靠關鍵字列表（天氣、上網、最新...）
- 記憶不足時自動上網 — 但「你好嗎」沒記憶也會觸發搜尋
- 搜尋結果品質不可控

**改善方案**：
```
A. 結合 Intent 判斷
   - CHAT intent → 不觸發搜尋（閒聊不需要）
   - QUERY intent → 記憶不足時自動搜尋
   - 使用者明確要求 → 一律搜尋

B. 搜尋品質過濾
   - 搜尋結果回來後，用 embedding similarity 比對 query
   - similarity < 0.3 的結果直接丟棄
   - 避免不相關搜尋結果污染 prompt

C. 搜尋結果快取
   - 同一主題 10 分鐘內不重複搜尋
   - 減少延遲和 API 呼叫
```

### 問題 5：Heavy Task 阻塞對話

**現狀**：`_handle_chat_async` 在 heavy task 進行時用 `sleep(5)` loop 等待，最多 5 分鐘，直接佔住 channel_pool worker。

**改善方案**：
```
A. 改用 Event 通知（不 poll）
   - heavy task 完成時 set event
   - chat handler 等 event，不佔 CPU

B. 不等待，直接處理
   - heavy task 和 chat 走不同 priority
   - chat 立即開始，不管 heavy task
   - oMLX 排隊機制自然處理順序

C. 回覆分段
   - 先回「稍等，我在處理其他任務...」
   - heavy task 完成後再回 chat 結果
```

### 問題 6：_process_message_inner 過長（~2000 行）

**現狀**：法扶、存證信函、爬蟲、Obsidian 等完全不同的業務邏輯全塞在一個函數裡。

**改善方案**：
```
Plugin/Handler 模式：

class MessageHandler:
    name: str
    priority: int
    def can_handle(self, msg, context) -> float  # 0~1 信心度
    def handle(self, msg, context) -> str | None

# 註冊 handler
handlers = [
    ScheduleHandler(priority=10),   # 行程
    LaborLawHandler(priority=10),   # 勞動法
    LAFHandler(priority=10),        # 法扶
    WebSearchHandler(priority=5),   # 搜尋
    ChatHandler(priority=1),        # 閒聊（最低優先）
]

# 統一分發
for h in sorted(handlers, key=lambda x: -x.priority):
    score = h.can_handle(msg, ctx)
    if score > threshold:
        return h.handle(msg, ctx)
```

---

## 三、改善優先序（投入 vs 效益）

| 優先 | 項目 | 效益 | 工作量 | 備註 |
|------|------|------|--------|------|
| 1 | Intent 分類改用 embedding（不用 LLM） | 延遲降 10-15s，不佔推理 slot | 中 | 已有 EmbeddingRouter 基礎 |
| 2 | 搜尋觸發結合 Intent（CHAT 不搜尋） | 避免閒聊等搜尋，減少無謂延遲 | 小 | 改 grounded_ai.py |
| 3 | Heavy task 不阻塞 chat | 對話不再等 5 分鐘 | 小 | 改 _handle_chat_async |
| 4 | 統一 Router Pipeline | 路由可預測、可 debug | 大 | 重構 orchestrator |
| 5 | Handler 模式拆分 | 可維護性、可擴展 | 大 | 重構 orchestrator |
| 6 | oMLX 優先佇列 | 使用者對話不被背景任務阻塞 | 中 | 需改 oMLX proxy |

---

## 四、快速見效的改動（可立即做）

### 4.1 Intent 分類：LLM fallback → embedding-only

```python
# intention_classifier.py 修改思路
def classify(self, text):
    # 1. Cache
    cached = self._cache.get(text)
    if cached: return cached

    # 2. Regex（保留，但不直接返回，存分數）
    regex_result, regex_conf = self._regex_classify(text)

    # 3. Embedding（取代 LLM）
    from skills.bridge.embedding_router import route
    er_result = route(text)
    embed_intent = "CMD" if er_result.tier == "DIRECT" else "QUERY" if er_result.score > 0.5 else "CHAT"
    embed_conf = er_result.score

    # 4. Ensemble
    if regex_conf > 0.9: return regex_result
    if embed_conf > 0.7: return embed_intent
    return self._heuristic_classify(text)  # 不呼叫 LLM
```

### 4.2 搜尋觸發：加入 Intent 過濾

```python
# grounded_ai.py chat_casper 修改
memories_insufficient = len(memories) == 0
# 閒聊不觸發搜尋，除非使用者明確要求
is_casual_chat = not _needs_research(message) and not memories_insufficient
if not is_casual_chat and (memories_insufficient or _needs_research(message)):
    # do web research
```

更精確的做法：
```python
# 判斷是否為需要事實的問題（而非閒聊）
def _is_factual_question(text):
    """問題需要事實回答時才上網"""
    factual_signals = [
        "什麼", "多少", "幾", "哪", "怎麼", "如何", "為什麼",
        "是否", "有沒有", "能不能",
        "天氣", "溫度", "新聞", "價格", "時間",
    ]
    return any(k in text for k in factual_signals)

# 在 chat_casper 中：
should_research = (
    _needs_research(message)  # 明確關鍵字
    or (memories_insufficient and _is_factual_question(message))  # 沒記憶 + 事實問題
)
```

### 4.3 記憶摘要：不用 LLM

```python
# grounded_ai.py _summarize_memories_if_needed 修改
def _summarize_memories_if_needed(query, memories, max_len=2500):
    raw_text = _format_memories(memories)
    if len(raw_text) <= max_len:
        return raw_text
    # 不用 LLM，改用截斷 + 相關性排序
    # 記憶已經按 recall score 排序，直接取前 N 筆
    truncated = []
    total = 0
    for m in memories:
        content = m.get('content', '')
        if total + len(content) > max_len:
            break
        truncated.append(content)
        total += len(content)
    return "\n".join(truncated) or "無相關記憶。"
```

---

## 五、關鍵檔案索引

| 元件 | 路徑 | 行數 | 說明 |
|------|------|------|------|
| Orchestrator | `api/orchestrator.py` | ~10000 | 訊息路由主體，需重構 |
| Intent Classifier | `skills/bridge/intention_classifier.py` | ~400 | 三層分類器 |
| Embedding Router | `skills/bridge/embedding_router.py` | ~300 | ModernBERT cosine 路由 |
| Grounded AI | `skills/bridge/grounded_ai.py` | ~500 | ask/chat + 品質防護 |
| Memory Bridge | `skills/memory/mem_bridge.py` | ~600 | recall/remember |
| Web Research | `skills/research/web_research.py` | ~200 | 網路搜尋 |
| Thread Pools | `api/thread_pools.py` | ~50 | pool 大小定義 |
| Melchior Client | `skills/bridge/melchior_client.py` | ~1500 | oMLX/遠端推理 |
| Server (TG/LINE) | `api/server.py` | ~10000 | 訊息收發 |
| Discord Bot | `api/discord_bot.py` | ~1000 | DC 訊息收發 |
