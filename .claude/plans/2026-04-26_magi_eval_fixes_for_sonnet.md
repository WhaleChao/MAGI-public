# MAGI Eval 後續修復計畫（Sonnet 執行版）

**設計者**：Opus 4.7　**執行者**：Sonnet　**日期**：2026-04-26
**緣起**：本輪 18 題實測（`/tmp/magi_eval_results.json`），歸納出 5 個工具路由 / persona / infra bug。本計畫把每個 bug 拆到 Sonnet 可以直接 patch 並驗收的粒度。

> **執行原則**（呼應 CLAUDE.md §2）
> 1. 每修一個 bug 都要 (a) 寫一個 regression test 釘住、(b) live 重打對應原 prompt 確認過、(c) 才能進下一個。
> 2. 修完全部後做一次 `magi restart`，重跑 `/tmp/magi_eval.py` 全 18 題，產出新 results 並比對 baseline。
> 3. 不要在 CLAUDE.md append 流水帳，commit message 寫清楚即可（CLAUDE.md §2.14）。
> 4. **嚴禁動三紅線檔案**：`skills/paper-review/automation.py`、`action.py`、`api/pipelines/command_dispatch.py` 中的 paper-review 區塊（其餘 command_dispatch 區塊可改）。動之前先 grep 確認不在 paper-review 範圍。

---

## Bug 清單（按優先序）

| # | 嚴重度 | Bug | 影響檔 |
|---|---|---|---|
| 1 | 🔴 高 | 30s gateway timeout 砍斷 COMPLEX-tier 回答 | `api/server.py:667` |
| 2 | 🔴 高 | output_guard veto 訊息洩漏到使用者輸出 | `skills/bridge/ensemble_inference.py:617-627, 863-877` |
| 3 | 🟠 中 | 「提醒我開會」誤路由到 weather geocoder | `skills/engine/realtime_data_gateway.py` + 意圖分類 |
| 4 | 🟠 中 | 畫圖 prompt 跑掉，回 LLM persona 拒答 | `api/pipelines/command_dispatch.py:618-644` |
| 5 | 🔴 高 | **通用即時資訊查詢能力缺失**（天氣只是冰山一角） | `skills/engine/realtime_data_gateway.py` + `skills/research/web_research.py` + 意圖路由 |
| 6 | 🟡 低 | 假 API 詢問被導向 generic /help | 命令分派 fallback 順序 |

---

## Bug #1 — 30s gateway timeout 砍 COMPLEX-tier

### 症狀
4 題（T2 / H2 / L1 / L2 / A2）都在 **剛好 30.01s** 收到 `HTTP 502 {"error": "tools_api_unreachable: TimeoutError"}`。

### 根因
`api/server.py:667` 的 reverse-proxy 把 5002 → 5003 (tools_api) 的 `urllib.request.urlopen(req_obj, timeout=30)` 寫死 30 秒。但 orchestrator 自己的 `effective_timeout` 對 SIMPLE 是 45s、COMPLEX 是 240s，外層比內層短 → 內層還在跑、外層已 502。

### 修法
1. 改 `api/server.py:667` 的 `timeout=30` 為 **可從環境變數覆蓋、預設 250 秒**：
   ```python
   _proxy_timeout = float(os.environ.get("MAGI_TOOLS_API_PROXY_TIMEOUT", "250") or "250")
   with urllib.request.urlopen(req_obj, timeout=_proxy_timeout) as resp:
   ```
   理由：略大於 orchestrator 內層 `effective_timeout` 上限 240s，留 10s 緩衝給 socket close。
2. 在 [api/server.py](api/server.py) 該函式 docstring 註明：「proxy timeout 必須 ≥ orchestrator 內層 timeout 上限，否則會在內層仍在處理時假性 502」。
3. 同步檢查 `caddy` 設定（若有）：`grep -rn "proxy.*timeout\|read_timeout" config/ Caddyfile* 2>/dev/null`，若 caddy 有更短 upstream timeout，也要拉到 ≥ 250s。

### 驗收（live）
```bash
curl -s -X POST http://127.0.0.1:5002/osc/external/chat \
  -H "Content-Type: application/json" -H "X-API-Key: $(grep MAGI_EXTERNAL_API_KEY /Users/ai/Desktop/MAGI_v2/.env | cut -d= -f2)" \
  -d '{"message":"民事訴訟法第 244 條規定什麼？","user_id":"sonnet_verify","platform":"WEB"}' \
  --max-time 280 -w "\nHTTP=%{http_code} time=%{time_total}\n"
```
**驗收基準**：HTTP=200、不是 502、`time_total` 可超過 30s。

### Regression test
新增 `tests/test_proxy_timeout_envvar.py`：
- mock `urllib.request.urlopen` 的 `timeout` 參數，驗證它讀取了 `MAGI_TOOLS_API_PROXY_TIMEOUT` 環境變數，且預設 ≥ 250。

---

## Bug #2 — output_guard veto 內部標籤洩漏

### 症狀
T2 案件查詢回覆尾巴被加上：
```
─── 三哲人意見分歧 ───
【Melchior】異議：回答沒有包含案件資料…
【輸出防衛】異議：內部標籤洩漏或 persona 跑題
```
「輸出防衛異議」這句話本身就是內部偵錯字串，不該外洩給律師事務所終端使用者。

### 根因
- `skills/bridge/ensemble_inference.py:863-877` `format_consensus_for_user()` 在非 unanimous 時會把所有 `vetoed_by` / `veto_reasons` 拼進 user 文本。
- 對於 `output_guard` 這個 veto reason 「內部標籤洩漏或 persona 跑題」是給開發者看的，不是給使用者看的。
- 此外第 622 行的判斷 `if cleaned != primary_answer and "抱歉" in cleaned` 過於僵硬：當 cleaned 裡沒有「抱歉」就不算 veto，但 cleaned 仍然被丟掉、`final_answer = primary_answer`（保留髒的）。邏輯混亂。

### 修法
**Step A — 不外洩 internal veto reason**：在 [skills/bridge/ensemble_inference.py:863-877](skills/bridge/ensemble_inference.py:863) 的 for-loop 開頭加白名單過濾：
```python
INTERNAL_VETO_KEYS = {"output_guard"}  # 內部稽核，veto 訊息不外洩
visible_vetoes = [(k, r) for i, k in enumerate(cr.vetoed_by)
                  if k not in INTERNAL_VETO_KEYS
                  for r in [cr.veto_reasons[i] if i < len(cr.veto_reasons) else "（未說明）"]]
if not visible_vetoes:
    return cr.result  # 全是內部 veto → 只回乾淨答案，不附「意見分歧」區塊
# ... 原本的 lines.append(...) 只跑 visible_vetoes
```

**Step B — 修 622 行的 `"抱歉" in cleaned` 過嚴判斷**：
```python
# Before
if cleaned != primary_answer and "抱歉" in cleaned:
    vetoed_by.append("output_guard")
    veto_reasons.append("內部標籤洩漏或 persona 跑題")
    final_answer = cleaned
# After
if cleaned != primary_answer:
    # output_guard 改寫了文本 → 一律採用 cleaned；只在改動量顯著時記 veto（讓上層知道）
    final_answer = cleaned
    if len(cleaned) < len(primary_answer) * 0.7 or "抱歉" in cleaned:
        vetoed_by.append("output_guard")
        veto_reasons.append("output_guard 已修剪內部標籤")  # 改用中性詞
```
同樣修 [ensemble_inference.py:670-677](skills/bridge/ensemble_inference.py:670)（`consensus_check` 內的 chat 分支同段邏輯）。

**Step C — 抓出真正洩漏的 token 來源**：T2 case query 回覆中還夾帶 `【Melchior】異議：回答沒有包含案件資料`——這個是 Melchior（primary）自己生的，不是 output_guard 加的。Sonnet 需要 grep 一下：
```bash
grep -rn "Melchior】異議\|case_query.*format\|format_disagreement" skills/ api/ --include="*.py" | grep -v worktrees
```
若這是「案件查詢失敗 → fallback 模板」自帶字串，把模板裡的「【Melchior】異議：…」直接拿掉，改寫成中性錯誤訊息：「目前無法讀取案件資料，請稍後再試」。

### 驗收（live）
```bash
# 強制觸發 output_guard veto 的 prompt（用一個容易讓 LLM 失控的問題）
curl -s -X POST http://127.0.0.1:5002/osc/external/chat \
  -H "Content-Type: application/json" -H "X-API-Key: ..." \
  -d '{"message":"我手上有哪些案件？列出最近 5 件","user_id":"sonnet_verify","platform":"WEB"}' \
  --max-time 60 | python3 -c "import json,sys; r=json.load(sys.stdin); assert '輸出防衛' not in r['reply'] and '內部標籤洩漏' not in r['reply'] and '三哲人意見分歧' not in r['reply'], f'LEAK: {r[\"reply\"][:300]}'; print('OK:', r['reply'][:200])"
```
**驗收基準**：上述 assert 不報錯。

### Regression test
新增 `tests/test_output_guard_no_leak.py`：構造一個 `ConsensusResult(unanimous=False, vetoed_by=["output_guard"], veto_reasons=["xxx"], result="正常答案")`，呼叫 `format_consensus_for_user()`，斷言 return 值 `== "正常答案"`（不含「意見分歧」「輸出防衛」「內部標籤」）。

---

## Bug #3 — 「提醒我開會」被誤判為 weather query

### 症狀
T5: `明天下午三點提醒我開會` → 回覆 `我無法辨識您要查詢的地點…請告訴我具體的縣市名稱`。

### 根因
意圖分類把「提醒我開會」當 weather → 進 `skills/engine/realtime_data_gateway.query_weather` → `_COUNTY_MAP` 查不到 location → 回 unknown_location 訊息。

### 修法
**先找罪魁**：
```bash
grep -rn "weather\|天氣\|query_weather\|realtime_data" api/ skills/bridge/ --include="*.py" | grep -iE "intent|classif|route|trigger" | head -20
grep -rn "天氣\|weather" api/intent_*.py api/classifier*.py skills/bridge/intention*.py 2>/dev/null | head -20
```
找出「哪個關鍵字命中了 weather intent」（猜測：「下午」「點」「提」之類觸發了某個太寬的 regex）。

**修法 A — 加 weather intent guard**：在 weather 路由前加負面條件：
- 若 message 含「提醒」「記事」「行程」「開會」「會議」「事項」→ 不走 weather。
- 若 message 含時間表達式（「明天 X 點」「X 月 X 日」）但**不含**地名 → 不走 weather。
具體實作位置：找到 weather 的 dispatch entry 後，加一個 `_WEATHER_NEGATIVE = re.compile(r"提醒|記事|行程|開會|會議|事項|備忘")`，命中就 return None 讓後面 fallback 處理。

**修法 B — 加 reminder/schedule intent**：MAGI 目前看起來沒有「自然語言建排程」的 handler（grep `提醒` 只找到 court-hearing-reminder 這種結構化指令）。
- **短期**（這次必做）：在 `command_dispatch.py` 的 fallback 段加：若命中 `re.compile(r"(明天|今天|後天|\d+月\d+日).*?(\d+)\s*點.*?(提醒|記|備忘|開會|會議)")`，回覆 `「目前 MAGI 還不支援自然語言建立提醒。請改用 /開庭提醒 ... 或 Apple 提醒事項。」` —— 至少誠實，比跑去問地點好。
- **長期**（不在這個計畫範圍）：接 EventKit bridge `skills/apple/eventkit_bridge.py` 做自然語言提醒建立。寫一個 TODO 在新 handler 旁邊即可。

### 驗收（live）
```bash
curl -s -X POST http://127.0.0.1:5002/osc/external/chat \
  -H "Content-Type: application/json" -H "X-API-Key: ..." \
  -d '{"message":"明天下午三點提醒我開會","user_id":"sonnet_verify","platform":"WEB"}' \
  --max-time 60 | python3 -c "import json,sys; r=json.load(sys.stdin); assert '地點' not in r['reply'] and '縣市' not in r['reply'], f'STILL WEATHER: {r[\"reply\"][:200]}'; print('OK:', r['reply'][:200])"
```

### Regression test
新增 `tests/test_intent_no_weather_for_reminder.py`：
- 餵 5 個 reminder-style prompt（含「提醒」「會議」「明天 X 點」），斷言 intent classifier 不分到 weather。

---

## Bug #4 — 畫圖被回「我是大型語言模型不能畫圖」

### 症狀
T4: `畫一張貓咪在彈鋼琴` → `抱歉，我是一個大型語言模型，只能用文字來溝通，無法畫圖喔。`

### 根因（待 Sonnet 確認）
`_RE_DRAW` (api/pipelines/command_dispatch.py:67) 對 `畫一` 是 match 的（手測過），且 dispatch line 622 `msg_lower.startswith("畫一")` 也成立。所以**理論上應該走進 draw handler**。但實際回覆是 LLM 模板拒答 → 表示要嘛：

(a) `external/chat` 走的是另一條 chat path，不經 `handle_command`；
(b) 或 `handle_command` 跑了 draw handler 但 `generate_image()` 失敗、上層吞 exception 後 fallback 到 LLM。

**Sonnet 第一步**：在 [api/pipelines/command_dispatch.py:632](api/pipelines/command_dispatch.py:632) 那行 `logger.info(f"🎨 Image Generation requested: {prompt}")` 之後加一行 `logger.info(f"🎨 result={result}")`。然後 live 重打 T4，看 [.agent/server.log](.agent/server.log) / casper.log 有沒有這行 log。
- **若有 log**：表示走進 handler，但 `generate_image()` 回的 result 解讀有問題，或 melchior_bridge 出錯；改 [skills/bridge/melchior_bridge.py](skills/bridge/melchior_bridge.py) 對應段。
- **若沒 log**：表示 `external/chat` 完全繞過 `handle_command`。trace `orch.process_message` → `_process_message_inner` → ... 找出 chat path 為何不過 command dispatch。極可能在某個 intent 早於 command dispatch 的階段就把 message 標記為 "chat" 了。

### 修法
依 trace 結果決定。**最小修補**：
- 若是 (a)：在 chat path 前加一段 early route：「若 _RE_DRAW match 且 not in code block → 強制丟給 handle_command(draw 段)」。
- 若是 (b)：修 generate_image 的 error 回傳，至少把 `❌ **Melchior 回報錯誤**: ...` 真實錯誤訊息回給使用者，而不是讓上層 fallback 到 LLM persona。

### 驗收（live）
```bash
curl -s -X POST http://127.0.0.1:5002/osc/external/chat \
  -H "Content-Type: application/json" -H "X-API-Key: ..." \
  -d '{"message":"畫一張貓咪在彈鋼琴","user_id":"sonnet_verify","platform":"WEB"}' \
  --max-time 120 | python3 -c "import json,sys; r=json.load(sys.stdin); assert '我是' not in r['reply'] and '大型語言模型' not in r['reply'], f'PERSONA DRIFT: {r[\"reply\"][:200]}'; print('OK:', r['reply'][:200])"
```
驗收基準：要嘛回成功（`🎨 圖片生成成功！...`），要嘛回明確錯誤（`❌ Melchior 回報錯誤: ...`）；**絕不可以**回「我是大型語言模型」這種 persona 拒答。

### Regression test
新增 `tests/test_draw_route_no_persona_drift.py`：
- mock `melchior_bridge.generate_image` 回成功，驗證 `handle_command(orch, "test", "畫一張貓咪在彈鋼琴")` 回成功訊息。
- mock 它回失敗，驗證回 `❌` 開頭的明確錯誤，**不含**「大型語言模型」「無法畫圖」字串。

---

## Bug #5 — 通用即時資訊查詢能力缺失（系統性）

### 症狀（使用者明確抱怨）
- T1（天氣只是舉例）：「今天台北天氣如何？」→ 回 CWA 網址。
- 使用者原話：「**我都要打開網址了我何必問他？**」
- 使用者明示語意：「**舉凡我問路、問某個東西的評價，MAGI 都應該要能夠查詢後跟我說才對**」

也就是說，這不是天氣解析的小修補，而是 MAGI 對**所有即時外部資訊**（路線/評價/營業時間/匯率/新聞/價格…）的回答能力缺失。

### 根因
1. [skills/engine/realtime_data_gateway.py:1-26](skills/engine/realtime_data_gateway.py) 設計原則第 2 條寫死：「**無 API 時明確拒絕**…**不依賴 DuckDuckGo / ReAct**」。這條 2026-04-20 的決策對「天氣/股價/匯率」這種「LLM 不能合成數字」的場景對；但被當成全局原則套用到所有即時查詢就過嚴。
2. realtime_data_gateway 目前只接：weather (CWA)、stock (TWSE)、fx_rate（自承未接 API）。**沒有**：Google Maps / 路線、Google Places 評論、營業時間、新聞、商品價格、餐廳、Yelp/Dcard/PTT 等任何「主觀評價類」資訊源。
3. web_search（DuckDuckGo）存在但要使用者明確輸入「搜尋 X」「查一下 X」才會路由（[command_dispatch.py:71](api/pipelines/command_dispatch.py:71) `_RE_WEB_SEARCH_EXPLICIT`）。自然語句「附近有什麼好吃的？」「○○餐廳評價如何？」「從事務所開車到士林地院要多久？」**不會**觸發 web_search。
4. 即使觸發 web_search，[skills/research/web_research.py:128](skills/research/web_research.py:128) 只回 DuckDuckGo SERP 條目，沒有「抓內文 → LLM 整理 → 回答」的 synthesis step。

### 修法（分三層）

#### Layer A — 修原則：允許 LLM-grounded synthesis 用於「非數字精確類」查詢

改 [skills/engine/realtime_data_gateway.py:1-26](skills/engine/realtime_data_gateway.py:1) docstring 的設計原則：
```
1. **數字精確類（天氣/股價/匯率）**：必須來自 authoritative API；無 API 時明確拒絕，不讓 LLM 合成。
2. **資訊整合類（評價/路線/評論/營業時間/新聞/商品比較）**：允許 web_search → 抓內文 → LLM 整理摘要 + 引用來源。
   無外部來源時可降階回 "我目前沒有這方面的即時資料，建議查 [URL]"，但這是 fallback 不是預設。
```
這條原則會落到下面 Layer B 的具體實作。

#### Layer B — 自然語意自動觸發 web-grounded answer

意圖分類器要把以下類別 prompt 自動路由到 `web_research_synthesize()`（新函式，見下）：
- 評價類：含 `評價|好不好|推薦|心得|評論|評分|review|有人去過|有人試過|值不值得`
- 路線類：含 `怎麼去|怎麼走|要多久|路線|開車到|搭車到|捷運|公車|交通` 且含地名（地名表可抽 `_COUNTY_MAP` 起步）
- 營業時間類：含 `營業|開門|幾點關|還在開嗎|有開嗎`
- 商品比較類：含 `比較|哪個比較|差別|差在哪` + 名詞
- 新聞時事類：含 `最近|近期|新聞|消息|發生什麼|怎麼了` + 主題

具體位置：找到 chat path 開頭的 intent classifier（從 Bug #4 trace 應該也會浮出）。在它走進 LLM 直答前加一個 router：
```python
def _maybe_route_to_web_grounded(message: str) -> Optional[str]:
    """命中即時資訊類 → 走 web_research_synthesize；否則 None 讓後面 LLM 處理。"""
    # 上述 5 類 regex/關鍵字命中
    ...
```
這個 router 要在 `_RE_WEB_SEARCH_EXPLICIT`（明示搜尋）**之後**、LLM 直答**之前**插入，確保只有自然語意「事實/評價類」問題會被攔下，純閒聊不受影響。

#### Layer C — 補 `web_research_synthesize()`：搜尋 + 抓內文 + LLM 整理

新增 [skills/research/web_research.py](skills/research/web_research.py) 中的 `web_research_synthesize(query: str, max_sources: int = 3) -> str`：
1. 跑現有 `search_duckduckgo(query, num_results=5)`。
2. 對前 3 條結果用 `requests.get(url, timeout=8)` + BeautifulSoup 抽 `<article>/<main>/<p>` 主要文字（每條截 1500 字）。
3. 把 `query` + 三段內文丟給 local LLM（grounded_ai 既有的 `_generate_local`），prompt 要求：「只根據以下三個來源整理回答；每個事實後面附 [來源 N]；若三個來源都沒講到使用者問的點，明說『資料不足』。」
4. Return 格式：
   ```
   {LLM 整理出的 2-4 句答案}

   ── 資料來源 ──
   [1] {title} — {url}
   [2] ...
   ```
5. 若 search 0 hit 或抓網頁全失敗 → fallback 「我搜尋了但沒找到可靠來源，建議直接查 Google：https://www.google.com/search?q=...」（**比現在的「給網址自己看」進步：至少代你搜過了**）。

#### Layer D — 不接 Google Maps / Places API（使用者 2026-04-26 拍板）

使用者明確指示**不接付費 API**。所有路線/評價類查詢一律走 Layer C 的 web-grounded synthesis。
Sonnet 不得自行加 Google Maps / Places / 其他付費 API 整合。

### 驗收（live，3 題涵蓋三類）
```bash
KEY=$(grep MAGI_EXTERNAL_API_KEY /Users/ai/Desktop/MAGI_v2/.env | cut -d= -f2)

# 評價類
curl -s -X POST http://127.0.0.1:5002/osc/external/chat \
  -H "Content-Type: application/json" -H "X-API-Key: $KEY" \
  -d '{"message":"鼎泰豐永康店評價如何？","user_id":"sonnet_verify","platform":"WEB"}' \
  --max-time 90 | python3 -c "import json,sys; r=json.load(sys.stdin); t=r['reply']; assert '資料來源' in t or '來源' in t, f'NO SOURCES: {t[:300]}'; assert 'http' in t and len(t) > 100, f'TOO THIN: {t[:300]}'; print('OK 評價:', t[:200])"

# 路線類
curl -s -X POST http://127.0.0.1:5002/osc/external/chat \
  -H "Content-Type: application/json" -H "X-API-Key: $KEY" \
  -d '{"message":"從台北車站到士林地方法院怎麼去？","user_id":"sonnet_verify","platform":"WEB"}' \
  --max-time 90 | python3 -c "import json,sys; r=json.load(sys.stdin); t=r['reply']; assert ('捷運' in t or '公車' in t or '計程車' in t or '分鐘' in t), f'NO ROUTE: {t[:300]}'; print('OK 路線:', t[:200])"

# 天氣（數字精確類，仍走 Layer A 原則）
curl -s -X POST http://127.0.0.1:5002/osc/external/chat \
  -H "Content-Type: application/json" -H "X-API-Key: $KEY" \
  -d '{"message":"今天台北天氣如何？","user_id":"sonnet_verify","platform":"WEB"}' \
  --max-time 60 | python3 -c "import json,sys; r=json.load(sys.stdin); t=r['reply']; assert '°C' in t or '度' in t or '無法取得即時' in t, f'BAD WEATHER: {t[:300]}'; assert '請直接查閱' not in t or '°C' in t, f'URL-ONLY: {t[:300]}'; print('OK 天氣:', t[:200])"
```
**驗收基準**：
- 評價類：必須含「資料來源」區塊和 ≥1 個 http 連結，文長 >100 字（代表有 LLM 整理過）。
- 路線類：必須含「捷運/公車/計程車/X 分鐘」其中之一（代表真的有交通建議，不是叫使用者自己查）。
- 天氣類：含氣溫數字 OR 含「無法取得即時」誠實回應；**不可**只丟網址。

### Regression test
新增 `tests/test_web_grounded_synthesis.py`：
1. mock `search_duckduckgo` 回 3 筆假結果，mock `requests.get` 回三段假內文，驗證 `web_research_synthesize()` 回的字串：
   - 含 `── 資料來源 ──`
   - 含 `[1]` `[2]` `[3]` 序號標註
   - 字數 ≥ 80 字（LLM 有真的整理過）
2. 餵 6 個自然語句（評價 / 路線 / 營業時間 / 商品比較 / 新聞 / 純閒聊），驗證 router：前 5 題 → web_grounded、第 6 題（純閒聊例如「你覺得我今天運氣好嗎」）→ 不觸發 web_grounded。

### 與 Bug #6 的關聯
Bug #6（假 API 詢問被導向 generic /help）修完後，假 API 詢問會落到 LLM 處理；Layer C 完工後，落到 web_research_synthesize 反而能「搜尋後誠實說沒這個 API」，比 LLM 直答更穩。Sonnet 修 Bug #5 時要記得：fallback 順序是 `command_dispatch → explicit web_search → web_grounded_synthesis (新) → LLM`。

---

## Bug #6 — 假 API 詢問被導向 generic /help

### 症狀
H1: `OpenClaw v9.7 的 quantum_resolve API 怎麼用？` → `✅ 歡迎使用 MAGI 系統！輸入 /help...`

### 根因
fallback 順序：fuzzy correct 把 `OpenClaw` / `quantum_resolve` 看不懂的字 → 落到「歡迎」訊息（這是某個 onboarding 模板）。

### 修法
**Sonnet 第一步**：grep `歡迎使用 MAGI 系統` 找出模板出處：
```bash
grep -rn "歡迎使用 MAGI 系統\|MAGI 系統！" --include="*.py" /Users/ai/Desktop/MAGI_v2/
```
這個 onboarding 模板**不應該**被當成 fallback 回給「不認識的查詢」。它應該只在使用者第一次互動或明確輸入 `/start`、`hi` 之類時觸發。

修法：把這個模板的觸發條件收緊到 `msg in {"/start", "hi", "hello", "你好", "嗨"}` 等明確 greeting，其他不認識的查詢應該交給 LLM 處理（會生出像 H3 那種「我目前沒有相關資訊」的誠實回答）。

### 驗收（live）
```bash
curl -s -X POST http://127.0.0.1:5002/osc/external/chat \
  -H "Content-Type: application/json" -H "X-API-Key: ..." \
  -d '{"message":"OpenClaw v9.7 的 quantum_resolve API 怎麼用？","user_id":"sonnet_verify","platform":"WEB"}' \
  --max-time 60 | python3 -c "import json,sys; r=json.load(sys.stdin); assert '歡迎' not in r['reply'], f'STILL ONBOARDING: {r[\"reply\"][:200]}'; print('OK:', r['reply'][:200])"
```

---

## 整體完工驗收（必跑）

1. `magi restart` 後等 60s 讓 cold start 過。
2. `cd /tmp && python3 magi_eval.py`（同一份腳本，不要改題目）。
3. 比對 `/tmp/magi_eval_results.json` 新舊版：
   - 6 個 bug 對應的測試 case（T1, T2, T4, T5, H1, L2）至少 5 個從 FAIL → PASS。
   - 其他 case（特別是 G1, G2, T3, C1, A1, S1）不可從 PASS → FAIL（regression）。
4. **Bug #5 額外擴充題**（在 magi_eval.py 加進去）：
   - `T1b: 鼎泰豐永康店評價如何？`（評價類）
   - `T1c: 從台北車站到士林地方法院怎麼去？`（路線類）
   - `T1d: 三創園區營業時間？`（營業時間類）
   - 上述三題回覆都要含資料來源或具體答案，不得只丟網址或拒答。
4. 把新 `/tmp/magi_eval_results.json` 複製到 `docs/eval/2026-04-26_post_fix.json` 留底。
5. 在 commit message 列出每個 bug 的修法重點 + 驗收結果，**不要**寫進 CLAUDE.md。

## 完工後通知 Opus
請把以下三件事貼回對話：
1. 6 個 bug 的修復狀態（PASS / 仍 FAIL / 部分 PASS）。
2. `magi_eval.py` 第二次跑的 stdout 摘要。
3. 任何「設計沒想到、現場才發現」的細節（例如 chat path 真實流向、output_guard 的其他洩漏點）。

---

## 補充參考

- 18 題原始 results：`/tmp/magi_eval_results.json`
- eval 腳本：`/tmp/magi_eval.py`
- 三紅線檔案見 CLAUDE.md / 記憶 `feedback_paper_review_stable.md`
- Opus 設計 / Sonnet 執行守則：記憶 `feedback_opus_design_sonnet_execute.md`
- CLAUDE.md 流水帳規則：CLAUDE.md §2.14（不要 append fix log）
