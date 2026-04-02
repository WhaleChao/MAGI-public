# MAGI 計劃 - 五機聯邦實作計畫 (v2.3.0)

> 📅 **版本**: v2.3.0 (Dell Pure Storage Edition)
> 🎯 **目標**: 達成商用 API **~98%** 水準 + 智慧圖書館 (RAG)

---

## 📋 硬體清單 (v2.3.0)

| 節點 | 硬體規格 | 角色 | 備註 |
|------|---------|------|------|
| **CASPER** | Mac Mini M4 **24GB** | 總理 | 主力機 (All-in-One AI) |
| **MELCHIOR** | Windows RTX 3060 12GB | 工程師 | 需升級顯卡 |
| **KEEPER** | Dell **24GB** (i3-1305U) | 智慧史官 | **純資料庫 (No AI)** |
| **BALTHASAR** | MBA M4 16GB | 行動秘書 | Apple Intelligence |
| **WATCHER** | M1 Air 8GB | 外交官 | 輕量級輔助 |

---

## 📦 軟體部署需求 (Software Requirements)

為了確保「五機聯邦」順利運作，**所有機器** (包含 Mac Mini, Windows, MacBook Airs, Dell) 都必須安裝以下基礎軟體：

1.  **MAGI Core (based on NanoClaw)**:
    *   本專案採用 **NanoClaw** (OpenClaw 的輕量化版本) 作為核心架構。
    *   **每一台機器**都需要 clone 本專案程式碼。
    *   **角色**: 這是「大腦的邏輯層」，負責調度、記憶與執行工具。
    *   *註*: [OpenClaw](https://openclaw.ai/) 是一個完整的 AI 代理作業系統，而我們使用其輕量版概念。

2.  **Ollama (The Engine)**:
    *   **除 Dell 外的機器** 都要安裝 Ollama。
    *   **角色**: 這是「大腦的引擎」，負責跑模型 (Generative AI)。
    *   **關係**: MAGI (NanoClaw) 呼叫 Ollama API 進行思考。
    *   **各機模型配置**:
        *   CASPER (Mac Mini): `qwen2.5:14b`, `nomic-embed-text` (推理核心), **`llama-3-taiwan-8b-instruct` (本土顧問)**
        *   MELCHIOR (Windows): `mistral-nemo:12b`, `qwen2.5-coder:7b` (程式與遊戲)
        *   KEEPER (Dell): 🚫 不安裝 / 禁用 Ollama (專注 I/O)
        *   **BALTHASAR (M4 Air 16GB)**:
            *   **雙重模式 (Dual Mode)**:
                *   **日卓模式 (Day Mode)**: 機會主義同步，處理使用者雜事。
                *   **夜議模式 (Nightly Council)**: 每日 **03:00 AM** 準時上線，參與三哲人共識會議 (The Vote) 與系統進化。
            *   **模型**: 回歸 `qwen2.5:14b`，充分利用 M4 NPU 算力。

3.  **Tailscale**:
    *   **每一台機器** 都要安裝並登入同一個 Tailnet。
    *   **原因**: 確保五機在虛擬內網中互通，不受實體地點限制。

---

## 🚀 實作步驟 (更新版)

### Phase 1: 基礎設施 (Week 1)

#### 1.1 KEEPER (Dell) - 極速資料庫 (Pure Storage)
> ✅ **任務**: 將 24GB RAM 轉化為極致的 DB Cache。

```bash
# 1. 安裝資料庫 (MariaDB LTS)
sudo apt install mariadb-server

# 2. 優化配置 (關鍵步驟)
# 編輯 /etc/mysql/mariadb.conf.d/50-server.cnf
# [mysqld]
# innodb_buffer_pool_size = 12G  # 分配 50% RAM 給 DB
# innodb_log_file_size = 2G      # 加快寫入速度
# max_connections = 500          # 允許更多並發
# query_cache_type = 0           # 讓 InnoDB 處理 Cache
# query_cache_size = 0

# 3. 禁用不必要的服務
sudo systemctl disable --now bluetooth
sudo systemctl disable --now cups
# 確保沒有安裝 Ollama
```

#### 1.2 技術深度說明 (Technical Specification)
*   **Vector Search**: 我們將使用 CASPER (Mac Mini) 進行 Embeddings 計算，然後將結果存入 KEEPER 的 `vector` table (BLOB or JSON)。
*   **檢索邏輯**: CASPER 透過 SQL 查詢 KEEPER (WHERE clause)，再於 CASPER 本地記憶體中做最後的 Cosine Similarity 排序 (因 `nump` 在 M4 上極快)，減輕 Dell 負擔。
*   **備份策略**: 每日 3:00 AM 執行 `mariabackup` (Hot Backup)，不鎖表，並同步到 NAS。

*(其餘步驟保持 v2.1.0 設定)*

### Phase 2: The Neural Link (Mission Control) - v2.4.0 New!
> 🧠 **核心升級**: 引入 "Mission Control" 架構，將 Keeper 升級為聯邦的「共享大腦」。

#### 2.1 雙腦橋接：審計與保護 (Audited Bridge)
為了讓 MAGI 協助工作，同時防止資料遺失或被惡意竄改，我們實施 **「禁刪除、全審計 (No-Delete & Full-Audit)」** 策略：

1.  **資料庫 A (`osc`) - 權限鎖定**:
    *   **允許**: `SELECT` (讀), `INSERT` (增), `UPDATE` (改)。
    *   **禁止**: `DELETE`, `DROP`, `TRUNCATE`。 **(物理層面禁止 MAGI 刪除任何資料)**
    *   **保護**: 若 MAGI 嘗試刪除資料，資料庫會直接拒絕並報錯。

2.  **資料庫 B (`magi_brain`) - 審計黑盒子**:
    *   **Audit Log**: 任何對 `osc` 的修改 (UPDATE)，在執行前**必須**先將「修改前的值 (Before)」與「修改後的值 (After)」寫入 `magi_brain.audit_log`。
    *   **還原機制**: 透過 Audit Log，管理者可以隨時「一鍵還原」任何被 MAGI 修改錯的資料。

```sql
/* 1. 建立 MAGI 專用帳號權限 */
CREATE USER 'magi_agent'@'%' IDENTIFIED BY 'secure_password';
GRANT SELECT, INSERT, UPDATE ON osc.* TO 'magi_agent'@'%';
/* 關鍵：不給予 DELETE 權限 */
REVOKE DELETE, DROP ON osc.* FROM 'magi_agent'@'%';

/* 2. 建立審計表 (在 magi_brain) */
CREATE TABLE audit_log (
    id INT AUTO_INCREMENT PRIMARY KEY,
    agent_name VARCHAR(50),
    table_name VARCHAR(50),
    record_id INT,
    operation ENUM('INSERT', 'UPDATE'),
    old_value JSON, /* 修改前的快照 */
    new_value JSON, /* 修改後的內容 */
    reason TEXT,    /* MAGI 修改的原因 */
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

/* 3. 建立身份系統 (Federation Identity) */
CREATE TABLE users (
    id VARCHAR(50) PRIMARY KEY, -- e.g. 'Lumi6'
    role ENUM('admin', 'guest') DEFAULT 'guest',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

#### 2.2 靈魂賦予 (Project SOUL)
為五機創造 `SOUL.md` (Personality & Protocol)，明確定義行為模式。
*   **Casper**: `SOUL.md` (The Governor) - 穩定、安全優先。
*   **Melchior**: `SOUL.md` (The Engineer) - 邏輯、代碼優化。
*   **Balthasar**: `SOUL.md` (The Coordinator) - 速度、溝通。
*   **Watcher**: `SOUL.md` (The Auditor) - 懷疑論、找錯。

#### 2.3 心跳機制 (Heartbeat System)
*   **Casper (Mac Mini)**: 接手 **「時鐘塔 (The Clock Tower)」** 職責，每 15 分鐘執行一次 Cron Job，負責所有定時任務調度。
*   **Balthasar (Mobile)**: 改為 **「機會主義同步 (Opportunistic Sync)」**。不強制背景運作，僅在您打開電腦使用時，順便同步資料。
*   **Watcher**: 維持 30 分鐘一次的審計檢查。
#### 2.4 進化與版控 (Evolution & Version Control)
*   **範圍 (Scope)**:
    *   **Core**: MAGI 的核心架構 (Casper/Melchior/Balthasar)。
    *   **Legacy**: 現有的業務腳本 (如 `osc.py`, `爬蟲.py`) **包含在進化範圍內**。Melchior 會分析這些舊程式碼並提出重構建議 (例如將同步處理改為非同步)。
*   **Git Flow**:
    *   所有的自我修正都必須透過 `git` 操作。
    *   `Auto-Commit Message`: 需標準化格式 `[Self-Improvement] Melchior fixed issue #123`.
*   **安全網 (Safety Net)**:
#### 2.5 LINE 身份綁定 (The First Contact)
利用 Castper 上正在運行的 **OpenClaw** 進行無縫綁定：

1.  **Castper 端 (Token 生成)**:
    執行 `python3 generate_binding_token.py`。
    系統會產生 `MAGI-XXXX` 並寫入 DB 的 `pending_registrations` 表。

2.  **OpenClaw 端 (Webhook Hook)**:
    在 OpenClaw 的訊息處理核心 (`gateway` 或 `skills`) 加入一段邏輯：
    ```python
    # 🔎 OpenClaw 身份識別邏輯 (虛擬碼)
    def identify_user(line_user_id, message_text):
        # 1. 檢查是否為 Admin
        admin = db.query("SELECT * FROM users WHERE line_user_id=%s AND role='admin'", line_user_id)
        if admin:
            return "ADMIN"
            
        # 2. 檢查是否正在嘗試綁定 (First Contact)
        if message_text.startswith("MAGI-"):
            token_data = db.query("SELECT * FROM pending_registrations WHERE token=%s", message_text)
            if token_data:
                db.execute("UPDATE users SET line_user_id=%s WHERE role='admin'", line_user_id)
                db.execute("DELETE FROM pending_registrations")
                return "ADMIN_JUST_BOUND"
        
        # 3. 預設皆為訪客
        return "GUEST"
    ```
    *(註：CASPER 的 SOUL 會根據此回傳身份，自動切換對話模式)*。

#### 2.6 自然語言意圖路由 (Natural Intent Routing) - v2.5 New!
為了達成 "No Wake Word" (無需關鍵字) 的流暢體驗：
1.  **直接監聽 (Direct Listening)**: 
    *   在 LINE/Discord 私訊 (DM) 中，**每一則訊息**都會直接送入 Casper 的推理核心。
    *   不需輸入 "Hey Casper" 或 "/search"。
2.  **意圖判斷 (Intent Classification)**:
    *   Casper 會先進行「意圖分類」：
        *   "幫我查王小明" -> **[TOOL_USE]** (查詢資料庫)
        *   "今天天氣如何" -> **[CHAT]** (閒聊)
        *   "把這個功能改成..." -> **[EVOLUTION]** (自我進化)
#### 2.7 業務邏輯整合 (Business Skill Integration) - v2.6 New!
為了讓 Castper 能「活用」現有的 Python 腳本 (如 `osc.py`) 來處理業務：

1.  **技能封裝 (Skill Wrapping)**:
    *   我們不重寫舊程式，而是建立一個 **`skills/legacy_bridge.py`**。
    *   這個檔案負責將您原本的 Python 函式 (例如 `check_court_date()`, `update_case_status()`) 包裝成 Casper 看得懂的 **Tools**。

2.  **主動監控 (Active Monitoring)**:
    *   利用 Casper 的 **Cron Job (每 15 分鐘)**。
    *   他在檢查 `magi_brain` 之餘，會順便呼叫 `legacy_bridge.check_updates()`。
    *   如果發現案件有更新，直接透過 LINE 通知 Admin。

#### 2.7 業務邏輯整合 (The Digestive System: Containerized Skills) - v2.7 (Real NanoClaw)
利用 NanoClaw 的容器特性，我們將舊的 Python 腳本封裝為一個獨立的 **"Legacy Agent Container"**。

1.  **攝取 (Ingestion)**:
    *   建立一個專用的 Agent 目錄：`MAGI/groups/legal_ops`.
    *   將 `/legacy_src` (`osc.py`, `Pager/`) 掛載 (Mount) 進這個容器。
2.  **內化 (Internalization)**:
    *   在 `legal_ops` 容器中定義 `SKILL.md` (NanoClaw 標準)。
    *   告訴系統："若要查庭期，請執行 `python3 /legacy/osc.py check_schedule`"。
3.  **安全 (Isolation)**:
    *   舊程式碼在容器內運行，**無法破壞 Host OS**。
    *   這比原本的設計更符合 "Iron Dome" 精神。

