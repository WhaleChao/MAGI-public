# MAGI Agent 進化計畫：從 n8n 到自我進化 AI Agent

> 撰寫日期：2026-04-02
> 目標：將 MAGI 從「語意工作流引擎」轉型為「自我進化 AI Agent」
> 前提：完全捨棄 OpenClaw，回收 token 預算與架構自主權

---

## 第零章：OpenClaw 完整移除評估

### 0.1 現狀：OpenClaw 在 MAGI 裡做了什麼

| 功能 | 是否依賴 OpenClaw | MAGI 自有替代 | 移除難度 |
|------|-------------------|--------------|---------|
| Telegram 收發訊息 | **否** — MAGI 自有 webhook handler (`server.py:7834`) | `_telegram_handle_update()` + `_telegram_send_text_to()` | 無需移除 |
| Discord 收發訊息 | **否** — MAGI 自有 discord.py bot (`discord_bot.py:972`) | `on_message()` + `message.channel.send()` | 無需移除 |
| LINE 收發訊息 | **否** — MAGI 自有 webhook handler (`server.py:7157`) | `handle_message()` + `line_bot_api.reply_message()` | 無需移除 |
| Cloudflared 隧道 | **否** — MAGI 自行管理 (`server.py:10473`) | `_ensure_cloudflared()` + 自動註冊 LINE webhook | 無需移除 |
| TG bot token 讀取 | **是（fallback）** — 從 `openclaw.json` 讀 token | 改用環境變數 `OPENCLAW_TELEGRAM_BOT_TOKEN` | 極低 |
| LLM 推理（codex-distributed） | **是** — `openclaw_codex_bridge.py` 呼叫 `openclaw agent` CLI | 改為直接呼叫 oMLX HTTP API | 中等 |
| 排程任務（cron） | **是** — `openclaw_cron_runner.py` | 改用 MAGI 自有 `job_queue.py` + LaunchAgent | 低 |
| 設定檔儲存 | **是** — 部分 token/secret 存在 `openclaw.json` | 全部移入 `.env` | 低 |

### 0.2 結論：通訊層可以 100% 捨棄 OpenClaw

**訊息流程完全不經過 OpenClaw gateway (port 18789)**：

```
實際流程（現在就是這樣）：

[Telegram]  使用者 → Telegram API → cloudflared → Caddy(18790) → server.py:5002/telegram/webhook
                                                                    ↓
[Discord]   使用者 → Discord API → discord.py bot (discord_bot.py on_message)
                                                                    ↓
[LINE]      使用者 → LINE API → cloudflared → Caddy(18790) → server.py:5002/line/webhook
                                                                    ↓
                                                          orchestrator.process_message()
                                                                    ↓
                                                          回覆 → 各平台 API

OpenClaw gateway (18789) 完全不在這條路上。
```

### 0.3 移除步驟

#### Step 0-1：Token 環境變數化（10 分鐘）

將以下 token 從 `openclaw.json` 搬到 `.env`：

```bash
# .env 新增
OPENCLAW_TELEGRAM_BOT_TOKEN=<your-telegram-bot-token>
TELEGRAM_WEBHOOK_SECRET=<your-webhook-secret>
# Discord token 已在 .env（DISCORD_BOT_TOKEN）
# LINE token 已在 .env（LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET）
```

#### Step 0-2：移除 `openclaw_codex_bridge.py` 的 subprocess 呼叫（Phase 1 實作）

將 `run_prompt()` 改為直接呼叫 oMLX HTTP API（詳見 Phase 1）。

#### Step 0-3：移除 OpenClaw cron runner（30 分鐘）

```python
# 將 openclaw_cron_runner.py 的排程任務遷移到 MAGI 的 job_queue.py
# 夜間巡檢已有獨立 LaunchAgent（com.magi.night-patrol.plist）
# 週末 resummary 已有獨立 LaunchAgent
```

#### Step 0-4：停用 OpenClaw 服務

```bash
# 停用 LaunchAgent
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.magi.caddy-openclaw.plist
# 不需要刪除 ~/.openclaw/ — 保留做備份參考
```

#### Step 0-5：清理 `_load_openclaw_cfg()` 殘留

```
需修改的檔案：
- api/server.py: _load_openclaw_cfg(), _load_openclaw_telegram_token() → 改讀 .env
- api/server.py: /api/codex-distributed/* 路由 → 改指向新的直連 bridge
- daemon.py: 從 never-kill list 移除 openclaw-gateway
- skills/ops/openclaw_updater.py → 可整個刪除
- skills/ops/openclaw_cron_runner.py → 遷移後刪除
```

---

## 第一章（Phase 1）：直連 LLM — 回收 Token 預算

> 預估工期：1 週
> 效果：每次推理省下 ~3,300 tokens（60-80%），消除 subprocess 開銷

### 1.1 問題

目前 `openclaw_codex_bridge.py` 每次呼叫：
1. Fork subprocess `openclaw agent --message "..."`
2. OpenClaw 注入 SOUL.md (~390 tokens) + AGENTS.md (~175 tokens) + Tool Schema (~850 tokens) + Workspace (~1,000 tokens)
3. 建立/恢復 session、compaction、pruning
4. 實際送到 oMLX 的 prompt 已被固定開銷擠壓

TAIDE 12b 只有 32K context，**固定開銷佔 13%**。

### 1.2 方案：新建 `skills/bridge/llm_direct.py`

取代 `openclaw_codex_bridge.py`，直接呼叫 LLM API。支援多 provider（本地 oMLX + 雲端 Claude API），不需要 OpenClaw：

```python
"""
llm_direct.py — 多 Provider LLM 直連，取代 OpenClaw codex-distributed agent

Provider 架構：
  ┌─────────────────────────────────────────────────────┐
  │  llm_direct.chat(feature="summary", provider=...)   │
  │                                                     │
  │  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
  │  │  oMLX    │  │  Claude  │  │  未來可擴充       │  │
  │  │ (本地)   │  │  (API)   │  │  OpenAI/Gemini..│  │
  │  │ 免費     │  │  付費    │  │                  │  │
  │  │ TAIDE    │  │  Sonnet  │  │                  │  │
  │  │ 32K ctx  │  │  200K ctx│  │                  │  │
  │  └──────────┘  └──────────┘  └──────────────────┘  │
  └─────────────────────────────────────────────────────┘

使用場景路由：
  - intent 分類 → oMLX（輕量、免費、低延遲）
  - 翻譯/摘要  → oMLX 優先，失敗 fallback Claude
  - ReAct 推理 → Claude（需要強推理能力）
  - 夜間批次   → oMLX（免費，不消耗 API quota）
"""
import os, json, logging, time
from typing import Any

logger = logging.getLogger("LLMDirect")

# ── Provider 設定 ──────────────────────────────────────

PROVIDERS = {
    "omlx": {
        "base_url": os.environ.get("OMLX_BASE_URL", "http://127.0.0.1:8080/v1"),
        "api_key": os.environ.get("OMLX_API_KEY", "omlx-local"),
        "default_model": os.environ.get("MAGI_DEFAULT_MODEL", "TAIDE-12b-Chat-mlx-4bit"),
        "api_format": "openai",       # OpenAI-compatible /v1/chat/completions
        "max_context": 32768,
        "cost_per_1k": 0,             # 本地免費
    },
    "claude": {
        "base_url": "https://api.anthropic.com",
        "api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "default_model": os.environ.get("MAGI_CLAUDE_MODEL", "claude-sonnet-4-20250514"),
        "api_format": "anthropic",    # Anthropic Messages API
        "max_context": 200000,
        "cost_per_1k": 0.003,         # $3/M input (Sonnet)
    },
}

# ── Feature → Provider 路由 ────────────────────────────
# 每個 feature 指定 primary provider + fallback
FEATURE_ROUTING = {
    "intent":     {"primary": "omlx", "fallback": None},        # 輕量，不值得花錢
    "translate":  {"primary": "omlx", "fallback": "claude"},     # oMLX 夠用，失敗才 fallback
    "summary":    {"primary": "omlx", "fallback": "claude"},
    "transcript": {"primary": "omlx", "fallback": None},
    "vision":     {"primary": "omlx", "fallback": "claude"},
    "general":    {"primary": "omlx", "fallback": "claude"},
    "react":      {"primary": "claude", "fallback": "omlx"},    # ReAct 需要強推理 → Claude 優先
}

DEFAULT_TIMEOUT = 90
MAX_TOKENS = 4096

# 精簡的 system prompt — 只保留必要指令，不注入 SOUL/AGENTS/TOOLS
SYSTEM_PROMPTS = {
    "intent": "你是意圖分類器。只回覆一個標籤：CHAT、QUERY、CMD 或 DANGER。不要解釋。",
    "translate": "你是專業翻譯。直接輸出翻譯結果，不加任何前綴或解釋。使用繁體中文（台灣用語）。",
    "summary": "你是法律文件摘要助理。輸出結構化摘要：裁判要旨→事實摘要→爭點→法院見解→適用法條。繁體中文。",
    "transcript": "你是逐字稿校正助理。保留內容，只改善標點、斷句和說話者標記。繁體中文。",
    "vision": "描述圖片內容。繁體中文。",
    "general": "你是 CASPER，MAGI 系統的 AI 助理。簡潔、準確、使用繁體中文（台灣用語）。",
    "react": "你是 CASPER，MAGI 系統的推理引擎。使用繁體中文（台灣用語）。",
}


def _call_openai_format(provider_cfg: dict, messages: list, **kwargs) -> dict:
    """呼叫 OpenAI-compatible API（oMLX、OpenAI、Groq 等）"""
    import requests
    resp = requests.post(
        f"{provider_cfg['base_url']}/chat/completions",
        headers={"Authorization": f"Bearer {provider_cfg['api_key']}"},
        json={"model": kwargs.get("model", provider_cfg["default_model"]),
              "messages": messages,
              "temperature": kwargs.get("temperature", 0.3),
              "max_tokens": kwargs.get("max_tokens", MAX_TOKENS)},
        timeout=kwargs.get("timeout", DEFAULT_TIMEOUT),
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "text": data["choices"][0]["message"]["content"].strip(),
        "usage": data.get("usage", {}),
    }


def _call_anthropic_format(provider_cfg: dict, messages: list, **kwargs) -> dict:
    """呼叫 Anthropic Messages API（Claude）"""
    import requests
    # Anthropic 格式：system 獨立，messages 只有 user/assistant
    system_msg = ""
    chat_messages = []
    for m in messages:
        if m["role"] == "system":
            system_msg += m["content"] + "\n"
        else:
            chat_messages.append({"role": m["role"], "content": m["content"]})

    resp = requests.post(
        f"{provider_cfg['base_url']}/v1/messages",
        headers={
            "x-api-key": provider_cfg["api_key"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": kwargs.get("model", provider_cfg["default_model"]),
            "system": system_msg.strip(),
            "messages": chat_messages,
            "temperature": kwargs.get("temperature", 0.3),
            "max_tokens": kwargs.get("max_tokens", MAX_TOKENS),
        },
        timeout=kwargs.get("timeout", DEFAULT_TIMEOUT),
    )
    resp.raise_for_status()
    data = resp.json()
    text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            text += block["text"]
    return {
        "text": text.strip(),
        "usage": data.get("usage", {}),
    }


_API_DISPATCHERS = {
    "openai": _call_openai_format,
    "anthropic": _call_anthropic_format,
}


def chat(
    *,
    prompt: str,
    feature: str = "general",
    temperature: float = 0.3,
    max_tokens: int = MAX_TOKENS,
    timeout: int = DEFAULT_TIMEOUT,
    model: str = "",
    provider: str = "",          # 強制指定 provider（空字串 = 自動路由）
) -> dict[str, Any]:
    """直接呼叫 LLM，不經過 OpenClaw。支援 oMLX / Claude / 未來擴充。"""

    # 決定 provider
    routing = FEATURE_ROUTING.get(feature, FEATURE_ROUTING["general"])
    primary = provider or routing["primary"]
    fallback = routing.get("fallback")

    system_prompt = SYSTEM_PROMPTS.get(feature, SYSTEM_PROMPTS["general"])
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    # 嘗試 primary → fallback
    for attempt_provider in [primary, fallback]:
        if not attempt_provider:
            continue
        cfg = PROVIDERS.get(attempt_provider)
        if not cfg or not cfg.get("api_key"):
            continue

        dispatcher = _API_DISPATCHERS.get(cfg["api_format"])
        if not dispatcher:
            continue

        started = time.monotonic()
        try:
            use_model = model or cfg["default_model"]
            result = dispatcher(cfg, messages, model=use_model,
                                temperature=temperature, max_tokens=max_tokens, timeout=timeout)
            elapsed = time.monotonic() - started
            return {
                "success": True,
                "text": result["text"],
                "feature": feature,
                "provider": attempt_provider,
                "model": use_model,
                "usage": result.get("usage", {}),
                "elapsed_sec": round(elapsed, 2),
            }
        except Exception as exc:
            logger.warning("LLM %s failed: %s, trying fallback", attempt_provider, exc)
            continue

    return {"success": False, "error": "all_providers_failed", "feature": feature}
    system_prompt = SYSTEM_PROMPTS.get(feature, SYSTEM_PROMPTS["general"])

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    started = time.monotonic()
    try:
        resp = requests.post(
            f"{OMLX_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {OMLX_API_KEY}"},
            json={
                "model": use_model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        usage = data.get("usage", {})
        elapsed = time.monotonic() - started

        return {
            "success": True,
            "text": text,
            "feature": feature,
            "model": use_model,
            "usage": {
                "input": usage.get("prompt_tokens", 0),
                "output": usage.get("completion_tokens", 0),
                "total": usage.get("total_tokens", 0),
            },
            "elapsed_sec": round(elapsed, 2),
        }
    except Exception as exc:
        return {
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
            "feature": feature,
            "model": use_model,
            "elapsed_sec": round(time.monotonic() - started, 2),
        }


# === 對外 API（與 openclaw_codex_bridge 相同介面，方便逐步替換）===

def translate(prompt: str, timeout_sec: int = 90) -> dict:
    return chat(prompt=prompt, feature="translate", timeout=timeout_sec)

def summarize(prompt: str, timeout_sec: int = 90) -> dict:
    return chat(prompt=prompt, feature="summary", timeout=timeout_sec)

def classify_intent(prompt: str, timeout_sec: int = 30) -> dict:
    result = chat(prompt=prompt, feature="intent", timeout=timeout_sec, max_tokens=16)
    if result["success"]:
        label = result["text"].upper().strip()
        for valid in ("CHAT", "QUERY", "CMD", "DANGER"):
            if valid in label:
                result["intent"] = valid
                break
        else:
            result["intent"] = "CHAT"
    return result

def vision(prompt: str, timeout_sec: int = 120) -> dict:
    return chat(prompt=prompt, feature="vision", timeout=timeout_sec)

def polish_transcript(prompt: str, timeout_sec: int = 120) -> dict:
    return chat(prompt=prompt, feature="transcript", timeout=timeout_sec)
```

### 1.3 Token 對比

| 項目 | OpenClaw 路徑 | 直連 oMLX | 直連 Claude API |
|------|-------------|----------|----------------|
| System prompt | ~3,300 tokens | ~50-150 tokens | ~50-150 tokens |
| Subprocess 開銷 | ~200ms fork | 0（HTTP 直連） | 0（HTTP 直連） |
| Session 管理 | compaction/pruning | 無（stateless） | 無（stateless） |
| **每次 intent 分類** | ~3,500 tokens | ~200 tokens | 不走 Claude（浪費） |
| **每次翻譯** | ~4,500 tokens | ~500 + 內容 | ~500 + 內容 |
| **每次摘要** | ~5,500 tokens | ~800 + 內容 | ~800 + 內容 |
| **ReAct 多步推理** | 不支援 | 勉強可用 | 最佳選擇（200K ctx）|
| 費用 | 免費 | 免費 | ~$3/M input tokens |

### 1.4 Provider 路由策略

```
按任務自動分流：

  輕量任務（免費、低延遲）          重型任務（需要強推理）
  ┌────────────────────┐          ┌────────────────────┐
  │ intent 分類        │          │ ReAct 多步推理      │
  │ 逐字稿校正        │ → oMLX   │ 複雜法律分析       │ → Claude API
  │ 夜間批次摘要      │  (本地)   │ 長文翻譯(>5000字)  │  (雲端)
  │ 簡單翻譯/摘要     │          │ 技能組合規劃       │
  └────────────────────┘          └────────────────────┘

  Fallback 策略：
  - oMLX 失敗 → Claude（如果有 API key）
  - Claude 失敗 → oMLX（降級但不中斷）
  - 全部失敗 → 回傳錯誤（不靜默吞掉）

  成本控制：
  - 沒設 ANTHROPIC_API_KEY → 全走 oMLX，零成本
  - 有設 API key → 只有 ReAct/複雜任務才用 Claude
  - 環境變數 MAGI_CLAUDE_BUDGET_DAILY=1.00 可設每日上限
```

### 1.4 遷移策略

逐一替換呼叫點，使用 feature flag 控制：

```python
# .env
MAGI_LLM_DIRECT=1  # 開啟直連模式

# 各呼叫點改法（以 intention_classifier.py 為例）：
# 原本：
from skills.bridge.openclaw_codex_bridge import classify_intent_with_codex
# 改為：
if os.environ.get("MAGI_LLM_DIRECT", "0") == "1":
    from skills.bridge.llm_direct import classify_intent
else:
    from skills.bridge.openclaw_codex_bridge import classify_intent_with_codex as classify_intent
```

需要替換的呼叫點（共 11 處）：

| 檔案 | 功能 | 優先級 |
|------|------|--------|
| `skills/bridge/intention_classifier.py:324` | intent 分類 | P0（最高頻） |
| `api/handlers/translation_handler.py:118` | 翻譯 | P0 |
| `api/handlers/summary_handler.py:154` | 摘要 | P0 |
| `api/orchestrator.py:6408` | 逐字稿潤飾 | P1 |
| `skills/bridge/tri_sage_collab.py:185` | 翻譯協作 | P1 |
| `skills/bridge/balthasar_bridge.py:393` | 摘要協作 | P1 |
| `skills/bridge/inference_gateway.py:765` | 推理閘道 | P1 |
| `skills/bridge/inference_gateway.py:897` | 推理閘道 | P1 |
| `skills/judgment-collector/action.py:2972` | 判決摘要 | P2 |
| `scripts/weekend_resummary.py:215` | 週末 resummary | P2 |
| `scripts/reprocess_insights.py:315` | 見解重處理 | P2 |

---

## 第二章（Phase 2）：ReAct 推理引擎 — 讓 MAGI 會「想」

> 預估工期：2-3 週
> 效果：從「一問一答」升級為「思考→行動→觀察→再思考」

### 2.1 問題

目前 orchestrator 的決策邏輯：

```
使用者輸入 → 9 層 if/elif 路由 → 選一個技能 → 執行 → 回覆
```

這是 **dispatcher 模式**（n8n），不是 **agent 模式**。Agent 應該：

```
使用者輸入 → LLM 思考該怎麼做 → 選工具執行 → 觀察結果 → 決定下一步 → ... → 最終回覆
```

### 2.2 方案：ReAct Loop Engine

新建 `skills/engine/react_engine.py`：

```python
"""
ReAct 推理引擎
Reason → Act → Observe → Reason → ... → Final Answer

核心原則：
1. LLM 決定下一步，不是 if/elif
2. 技能是 LLM 的「工具」，由 LLM 呼叫
3. 有觀察回饋，可中途調整策略
4. 設定最大步數防止無限迴圈
"""

MAX_STEPS = 8          # 最大推理步數
TIMEOUT_SEC = 120      # 整體超時

TOOL_SCHEMA = '''
你可以使用以下工具。每次回覆時，先在 <think> 中分析，再決定：
- 使用工具：回覆 ACTION: tool_name(參數)
- 已有答案：回覆 FINAL: 你的最終回答

可用工具：
{tool_list}

規則：
- 每次只能呼叫一個工具
- 觀察工具結果後再決定下一步
- 不確定時先搜尋/查詢，不要猜測
'''

class ReActEngine:
    def __init__(self, llm_fn, tools: dict):
        """
        llm_fn: 呼叫 LLM 的函數 (messages) -> str
        tools: {"tool_name": {"fn": callable, "desc": str, "params": str}}
        """
        self.llm = llm_fn
        self.tools = tools

    def run(self, user_query: str, context: str = "") -> dict:
        tool_list = self._format_tool_list()
        system = TOOL_SCHEMA.format(tool_list=tool_list)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_query},
        ]
        if context:
            messages.insert(1, {"role": "system", "content": f"背景資訊：\n{context}"})

        trace = []  # 推理軌跡

        for step in range(MAX_STEPS):
            response = self.llm(messages)

            if "FINAL:" in response:
                answer = response.split("FINAL:", 1)[1].strip()
                trace.append({"step": step + 1, "type": "final", "content": answer})
                return {"success": True, "answer": answer, "trace": trace, "steps": step + 1}

            if "ACTION:" in response:
                action_str = response.split("ACTION:", 1)[1].strip()
                tool_name, params = self._parse_action(action_str)

                if tool_name not in self.tools:
                    observation = f"錯誤：工具 '{tool_name}' 不存在。可用工具：{list(self.tools.keys())}"
                else:
                    try:
                        observation = self.tools[tool_name]["fn"](params)
                    except Exception as e:
                        observation = f"工具執行錯誤：{e}"

                trace.append({"step": step + 1, "type": "action", "tool": tool_name, "params": params})
                trace.append({"step": step + 1, "type": "observation", "content": str(observation)[:2000]})

                messages.append({"role": "assistant", "content": response})
                messages.append({"role": "user", "content": f"OBSERVATION: {observation}"})
            else:
                # LLM 沒有遵循格式，提醒它
                messages.append({"role": "assistant", "content": response})
                messages.append({"role": "user", "content": "請使用 ACTION: 或 FINAL: 格式回覆。"})

        return {"success": False, "error": "max_steps_reached", "trace": trace, "steps": MAX_STEPS}
```

### 2.3 將現有技能包裝為 ReAct 工具

```python
# skills/engine/tool_registry.py

TOOLS = {
    "search_memory": {
        "fn": lambda q: mem_bridge.recall(q, top_k=5),
        "desc": "搜尋 MAGI 記憶庫",
        "params": "query: str — 搜尋關鍵字",
    },
    "web_search": {
        "fn": lambda q: deep_research.search(q),
        "desc": "網路搜尋",
        "params": "query: str — 搜尋關鍵字",
    },
    "read_file": {
        "fn": lambda p: Path(p).read_text()[:3000],
        "desc": "讀取檔案內容",
        "params": "path: str — 檔案路徑",
    },
    "summarize_text": {
        "fn": lambda t: llm_direct.summarize(t),
        "desc": "摘要一段文字",
        "params": "text: str — 要摘要的文字",
    },
    "remember": {
        "fn": lambda c: mem_bridge.remember(c),
        "desc": "將資訊存入長期記憶",
        "params": "content: str — 要記住的內容",
    },
    "query_cases": {
        "fn": lambda q: osc_db.search_cases(q),
        "desc": "查詢案件資料庫",
        "params": "query: str — 案件關鍵字或案號",
    },
    "send_notification": {
        "fn": lambda msg: red_phone.send(msg),
        "desc": "發送通知給律師（TG + DC）",
        "params": "message: str — 通知內容",
    },
    "run_skill": {
        "fn": lambda name_and_args: skill_runner.execute(name_and_args),
        "desc": "執行 MAGI 技能（如 pdf-namer, file-review, laf-automation 等）",
        "params": "skill_name(args) — 技能名稱和參數",
    },
    "get_schedule": {
        "fn": lambda d: google_calendar.get_events(d),
        "desc": "查詢 Google Calendar 行程",
        "params": "date: str — 日期（YYYY-MM-DD）",
    },
}
```

### 2.4 整合到 Orchestrator

```python
# orchestrator.py — 在 intent 判定為 QUERY 或 COMPLEX 時啟用 ReAct

def _handle_complex_query(self, user_id, message, context):
    """複雜查詢走 ReAct engine，簡單查詢走原本的直接回覆"""

    from skills.engine.react_engine import ReActEngine
    from skills.engine.tool_registry import TOOLS
    from skills.bridge.llm_direct import chat

    def llm_fn(messages):
        # ReAct 推理用 Claude（200K context + 強推理）
        # 沒有 Claude API key 時 fallback 到 oMLX
        result = chat(prompt=messages[-1]["content"], feature="react")
        return result.get("text", "")

    engine = ReActEngine(llm_fn=llm_fn, tools=TOOLS)
    memory_context = mem_bridge.recall(message, top_k=3)
    result = engine.run(user_query=message, context=memory_context)

    if result["success"]:
        return result["answer"]
    else:
        # Fallback 到原本的單輪回覆
        return self._handle_chat_async(user_id, message)
```

### 2.5 ReAct 與現有路由的共存

```
改造後的訊息流程：

使用者輸入
    ↓
[Layer 1] Quick Reply（問候/狀態/幫助）→ 直接回覆（不走 LLM）
    ↓
[Layer 2] Iron Dome（危險指令偵測）→ 攔截
    ↓
[Layer 3] Embedding Router — 高信心(≥0.85)直接 dispatch 技能
    ↓
[Layer 4] Intent Classification
    ├── CMD → 直接執行指令
    ├── DANGER → 攔截
    ├── CHAT（簡單）→ grounded_ai 單輪回覆
    └── QUERY / CHAT（複雜）→ **ReAct Engine**（新）
                                ├── 思考 → 選工具
                                ├── 執行 → 觀察結果
                                ├── 再思考 → ...
                                └── 最終回答
```

**關鍵改變**：embedding router 的 DIRECT 閾值從 0.75 提高到 0.85，把更多查詢交給 ReAct engine 處理，讓 LLM 決定要不要用技能，而不是 cosine similarity 替 LLM 做決定。

---

## 第三章（Phase 3）：技能組合 — 讓 MAGI 能「串」

> 預估工期：2-3 週
> 效果：一個指令觸發多技能鏈式執行

### 3.1 問題

目前每個技能都是獨立的 `action.py`，不能組合：

```
使用者：「幫我把這份判決書翻譯摘要後存進記憶」
目前：只能選一個技能執行（翻譯 OR 摘要 OR 存記憶）
目標：翻譯 → 摘要 → 存記憶，自動串起來
```

### 3.2 方案：ReAct Engine 天然支援技能組合

Phase 2 的 ReAct engine 已經解決了這個問題：

```
LLM 思考：使用者想要翻譯、摘要、存記憶三件事
  ↓
Step 1: ACTION: summarize_text(判決書全文)
OBSERVATION: 摘要結果...
  ↓
Step 2: ACTION: translate(摘要結果)  ← 前一步的 observation 成為這一步的輸入
OBSERVATION: 翻譯結果...
  ↓
Step 3: ACTION: remember(翻譯後的摘要)
OBSERVATION: 已存入記憶
  ↓
Step 4: FINAL: 已完成：1) 摘要判決書 2) 翻譯為繁體中文 3) 存入長期記憶
```

### 3.3 進階：Skill Pipeline DSL（未來）

對於重複性高的組合，可定義 pipeline：

```yaml
# skills/pipelines/judgment_ingest.yaml
name: judgment_ingest
description: 判決書完整處理流程
steps:
  - skill: pdf_extract
    input: file_path
    output: raw_text
  - skill: summarize
    input: $raw_text
    output: summary
  - skill: extract_insights
    input: $raw_text
    output: insights
  - skill: remember
    input: "$summary\n---\n$insights"
  - skill: notify
    input: "判決書已處理完成：{case_id}"
```

但這是 **Phase 5 以後**的事。Phase 3 先靠 ReAct engine 的多步推理來實現動態組合。

---

## 第四章（Phase 4）：回饋學習 — 讓 MAGI 會「學」

> 預估工期：2-3 週
> 效果：從使用者互動中學習，逐步改善路由和回覆品質

### 4.1 路由回饋學習

```python
# skills/engine/feedback_loop.py

class RoutingFeedback:
    """從使用者反應學習路由權重"""

    FEEDBACK_DB = MAGI_ROOT / ".agent" / "routing_feedback.json"

    def record(self, query: str, routed_skill: str, outcome: str):
        """
        outcome: "correct" | "wrong_skill" | "no_response_needed" | "user_corrected"
        """
        entry = {
            "query": query,
            "routed_skill": routed_skill,
            "outcome": outcome,
            "timestamp": datetime.now().isoformat(),
        }
        self._append(entry)

    def compute_skill_adjustments(self) -> dict[str, float]:
        """根據歷史回饋計算每個技能的信心調整值"""
        entries = self._load_all()
        skill_stats = defaultdict(lambda: {"correct": 0, "wrong": 0})

        for e in entries[-500:]:  # 只看最近 500 筆
            skill = e["routed_skill"]
            if e["outcome"] == "correct":
                skill_stats[skill]["correct"] += 1
            elif e["outcome"] in ("wrong_skill", "user_corrected"):
                skill_stats[skill]["wrong"] += 1

        adjustments = {}
        for skill, stats in skill_stats.items():
            total = stats["correct"] + stats["wrong"]
            if total >= 5:  # 至少 5 筆才調整
                accuracy = stats["correct"] / total
                # 準確率高 → 正向調整（降低閾值）
                # 準確率低 → 負向調整（提高閾值）
                adjustments[skill] = (accuracy - 0.7) * 0.1  # ±0.03 範圍
        return adjustments
```

### 4.2 隱式回饋訊號

不需要使用者明確按讚/倒讚，從對話行為推斷：

```python
IMPLICIT_SIGNALS = {
    # 正面訊號
    "correct": [
        "使用者繼續聊天（沒有糾正）",
        "使用者說「好」「謝謝」「對」",
        "使用者照著建議做了",
    ],
    # 負面訊號
    "wrong": [
        "使用者重新問同一個問題（→ 第一次沒回答好）",
        "使用者說「不是」「不對」「我是問...」",
        "使用者在 30 秒內再發一則更具體的訊息",
    ],
}
```

```python
# orchestrator.py 中加入回饋偵測

def _detect_implicit_feedback(self, user_id, current_msg, last_response):
    """從使用者的後續訊息推斷前一次回覆是否正確"""
    negative_patterns = ["不是", "不對", "我是問", "我說的是", "你搞錯了", "重新"]
    positive_patterns = ["好", "謝謝", "對", "收到", "了解"]

    msg = current_msg.strip()
    if any(p in msg for p in negative_patterns):
        return "wrong_skill"
    if any(p in msg for p in positive_patterns) and len(msg) < 10:
        return "correct"
    return None
```

### 4.3 Prompt 自動調優（簡易版）

```python
# skills/engine/prompt_tuner.py

class PromptTuner:
    """追蹤不同 system prompt 版本的效果"""

    def __init__(self):
        self.variants = {}   # feature → [variant_a, variant_b]
        self.stats = {}      # (feature, variant_idx) → {success: int, fail: int}

    def get_prompt(self, feature: str) -> str:
        """A/B test — 隨機選一個 variant"""
        variants = self.variants.get(feature, [SYSTEM_PROMPTS[feature]])
        idx = hash(time.time()) % len(variants)
        return variants[idx], idx

    def record_result(self, feature: str, variant_idx: int, success: bool):
        key = (feature, variant_idx)
        if key not in self.stats:
            self.stats[key] = {"success": 0, "fail": 0}
        self.stats[key]["success" if success else "fail"] += 1

    def promote_best(self, feature: str, min_samples: int = 20):
        """樣本數夠多時，淘汰表現差的 variant"""
        variants = self.variants.get(feature, [])
        if len(variants) < 2:
            return
        scores = []
        for idx in range(len(variants)):
            s = self.stats.get((feature, idx), {"success": 0, "fail": 0})
            total = s["success"] + s["fail"]
            if total < min_samples:
                return  # 樣本不夠，先不動
            scores.append(s["success"] / total)
        # 保留最好的
        best_idx = scores.index(max(scores))
        self.variants[feature] = [variants[best_idx]]
```

---

## 第五章（Phase 5）：自主夜巡 — 讓夜巡有「腦」

> 預估工期：2-3 週
> 效果：夜巡從 ETL pipeline 升級為 agent-driven 巡檢

### 5.1 問題

目前 `casper_night_patrol.py` 是純 for-loop：

```python
# 現在
for target in crawl_targets:
    result = crawl(target)
    if result.changed:
        notify(result)  # 全部通知，不分輕重
```

### 5.2 方案：Agent-Driven Night Patrol

```python
# scripts/casper_night_patrol_v2.py

class AgentNightPatrol:
    """用 ReAct engine 做夜間巡檢決策"""

    PATROL_TOOLS = {
        "check_court_updates": {...},     # 查司法院更新
        "check_laf_cases": {...},         # 查法扶案件狀態
        "check_payment_deadlines": {...}, # 查繳費期限
        "assess_urgency": {...},          # 評估緊急程度
        "notify_lawyer": {...},           # 通知律師
        "write_report": {...},            # 寫巡檢報告
        "skip_notification": {...},       # 判斷不需通知
    }

    def run(self):
        engine = ReActEngine(llm_fn=llm_direct.chat, tools=self.PATROL_TOOLS)

        result = engine.run(
            user_query="執行夜間巡檢。檢查所有案件更新，評估每個更新的緊急程度，"
                       "只通知真正需要律師關注的事項。寫一份精簡的巡檢報告。",
            context=self._get_patrol_context(),
        )

        # Agent 自行決定：
        # - 哪些更新值得通知（不是全部都推）
        # - 通知的優先級和措辭
        # - 報告的詳細程度
```

### 5.3 智能通知決策

```
目前：有更新就通知（使用者被洗訊息）
改後：LLM 評估重要性

Example ReAct trace:
  Step 1: ACTION: check_court_updates()
  OBS: 3 件更新 — 裁定書(案號A)、開庭通知(案號B)、繳費通知(案號C)

  Step 2: ACTION: assess_urgency("繳費通知 案號C，期限 2026-04-10")
  OBS: 高優先 — 距期限 8 天，需立即通知

  Step 3: ACTION: assess_urgency("開庭通知 案號B，開庭日 2026-05-20")
  OBS: 低優先 — 還有 48 天，可放入晨報

  Step 4: ACTION: notify_lawyer("【急】案號C 繳費期限 4/10，請儘速處理")
  OBS: 已通知

  Step 5: ACTION: write_report("巡檢完成。1 件緊急通知已發送，2 件低優先納入晨報。")
  OBS: 報告已寫入

  Step 6: FINAL: 夜間巡檢完成。
```

---

## 第六章（Phase 6）：持續知識進化 — 讓 MAGI 會「長」

> 預估工期：2-3 週
> 效果：從批次蒸餾 → 持續學習 + 記憶管理

### 6.1 即時知識擷取（取代週末批次）

```python
# skills/engine/knowledge_extractor.py

class ContinuousKnowledgeExtractor:
    """從每次對話中即時擷取可複用知識"""

    def extract_from_conversation(self, query: str, answer: str, source: str):
        """判斷這段對話是否包含值得保存的知識"""

        # 用 LLM 判斷（輕量呼叫）
        prompt = f"""以下對話是否包含可供未來案件參考的法律知識或實務見解？
只回答 YES 或 NO，如果 YES 則附上一行摘要。

Q: {query}
A: {answer}"""

        result = llm_direct.chat(prompt=prompt, feature="intent", max_tokens=100)
        if "YES" in result.get("text", ""):
            summary = result["text"].split("YES", 1)[1].strip()
            mem_bridge.remember(
                content=summary,
                source=source,
                tags=["auto_extracted", "conversation"],
            )
```

### 6.2 記憶品質管理

```python
class MemoryManager:
    """記憶不是越多越好 — 要管理品質和新鮮度"""

    def decay_old_memories(self):
        """降低舊記憶的檢索權重"""
        # 每月執行
        # 超過 6 個月未被召回的記憶 → 降權
        # 超過 12 個月 → 歸檔（不刪除，但不參與檢索）

    def merge_similar_memories(self):
        """合併相似記憶，減少冗餘"""
        # 用 embedding 找 cosine > 0.92 的記憶對
        # LLM 合併為一條更完整的記憶

    def validate_memory_accuracy(self):
        """定期驗證記憶是否過時"""
        # 例：法條修改 → 舊的法條記憶標記為過時
```

---

## 第七章：實作順序與里程碑

### 總覽

```
Phase 0: 移除 OpenClaw          ██████░░░░ 1 週     ← 先做這個
Phase 1: 直連 LLM               ████░░░░░░ 1 週     ← 與 Phase 0 同步
Phase 2: ReAct Engine            ██████████ 2-3 週   ← 核心改造
Phase 3: 技能組合                ████░░░░░░ 1-2 週   ← ReAct 天然支援
Phase 4: 回饋學習                ██████░░░░ 2-3 週
Phase 5: 自主夜巡                ██████░░░░ 2-3 週
Phase 6: 持續知識進化            ██████░░░░ 2-3 週
```

### Phase 0+1 完成標準（第 1-2 週）

- [ ] `.env` 包含所有 token，不再讀 `openclaw.json`
- [ ] `llm_direct.py` 上線，所有 11 個呼叫點切換完成
- [ ] OpenClaw gateway 停用，通訊不受影響
- [ ] 每次 intent 分類 token 從 ~3,500 降到 ~200
- [ ] 每次翻譯/摘要 token 降低 60%+

### Phase 2 完成標準（第 3-4 週）

- [ ] ReAct engine 可處理多步驟查詢
- [ ] 工具註冊表包含 10+ 核心工具
- [ ] 複雜查詢走 ReAct，簡單查詢走原路徑
- [ ] 推理軌跡可追蹤（trace log）

### Phase 3 完成標準（第 4-5 週）

- [ ] 使用者可用自然語言觸發多技能組合
- [ ] 測試案例：「翻譯這份判決書並存摘要」→ 自動串接 3 個技能

### Phase 4 完成標準（第 6-7 週）

- [ ] 隱式回饋偵測上線
- [ ] 路由準確率可量化追蹤
- [ ] 至少一個 feature 的 prompt 經過 A/B test 優化

### Phase 5 完成標準（第 8-9 週）

- [ ] 夜巡改為 agent-driven
- [ ] 通知量減少 50%+（只推重要的）
- [ ] 巡檢報告由 LLM 生成

### Phase 6 完成標準（第 10-11 週）

- [ ] 即時知識擷取上線
- [ ] 記憶衰減機制啟用
- [ ] 相似記憶自動合併

---

## 第八章：風險與注意事項

### 8.1 模型限制與分工

**TAIDE 12b（本地 oMLX）：**

| 限制 | 影響 | 應對 |
|------|------|------|
| 32K context | ReAct 多步推理消耗 context 快 | ReAct 走 Claude，不走 TAIDE |
| 推理能力有限 | 複雜任務可能推理錯誤 | 只做簡單任務（intent/翻譯/摘要） |
| 無原生 tool-use | 需要 prompt engineering 模擬 | 簡單任務不需要 tool-use |
| 中文偏繁體台灣 | 法律用語有優勢 | 正好是 MAGI 的使用場景 |

**Claude API（雲端）：**

| 限制 | 影響 | 應對 |
|------|------|------|
| 付費（$3/M input） | 大量呼叫會燒錢 | 只用在 ReAct/複雜推理，日預算上限 |
| 網路延遲 ~1-3s | 比本地慢 | ReAct 本來就是複雜任務，使用者可接受等待 |
| API key 管理 | 需要安全儲存 | 放 `.env`，不進 git |
| 有 rate limit | 高頻呼叫可能被限 | 批次任務走 oMLX，Claude 只做互動式推理 |

**分工原則：能本地做的不上雲，需要「想」的才用 Claude。**

### 8.2 安全考量

```
ReAct engine 的工具呼叫必須經過 Iron Dome：
- read_file: 只允許讀 MAGI 工作目錄和案件資料夾
- run_skill: 不可執行未註冊的技能
- send_notification: 繼承現有冷卻機制
- remember: 繼承反幻覺過濾

新增 Iron Dome 規則：
- ReAct 最大步數限制（防止無限迴圈消耗資源）
- 單次 ReAct 總 token 上限（防止 context 爆炸）
- 工具呼叫頻率限制（防止短時間內大量 API 呼叫）
```

### 8.3 回退策略

每個 Phase 都有 feature flag，可隨時回退：

```bash
# .env
MAGI_LLM_DIRECT=1        # Phase 1: 直連 LLM（0=回退到 OpenClaw）
MAGI_REACT_ENABLED=1      # Phase 2: ReAct engine（0=回退到原路由）
MAGI_FEEDBACK_LEARNING=1  # Phase 4: 回饋學習（0=關閉）
MAGI_AGENT_PATROL=1       # Phase 5: agent 夜巡（0=回退到 ETL）
MAGI_CONTINUOUS_LEARN=1   # Phase 6: 持續學習（0=回退到批次）
```

---

## 附錄 A：現狀 vs 目標對照

| 維度 | 現在（n8n 式） | 目標（AI Agent） |
|------|---------------|-----------------|
| 決策 | 9 層 if/elif 路由 | LLM ReAct 推理 |
| 技能 | 一次一個，不可組合 | LLM 動態選擇與串接 |
| 學習 | 靜態 prompt、固定閾值 | 回饋調整、prompt A/B test |
| 記憶 | 被動存取 | 主動擷取、品質管理、衰減 |
| 夜巡 | ETL for-loop | Agent 評估重要性 |
| 知識 | 週末批次蒸餾 | 即時擷取 + 品質驗證 |
| 推理 | 單輪問答 | 多步推理（Reason→Act→Observe） |
| 通訊 | 雙層重疊（MAGI + OpenClaw） | MAGI 單層直連 |
| Token | ~4,000-6,000/次 | ~500-2,000/次 |

## 附錄 B：可刪除的 OpenClaw 相關檔案

```
可刪除：
  skills/ops/openclaw_updater.py          — OpenClaw 更新工具
  skills/ops/openclaw_cron_runner.py      — 遷移後可刪
  scripts/ops/toggle_codex_distributed_mode.py
  scripts/ops/check_codex_distributed_health.py

可大幅精簡：
  skills/bridge/openclaw_codex_bridge.py  — 替換為 llm_direct.py 後保留空殼做相容

不動：
  ~/.openclaw/                            — 保留做備份參考，不啟動服務即可
```
