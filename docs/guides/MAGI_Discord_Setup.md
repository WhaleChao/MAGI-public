# MAGI Federation: Discord Configuration Guide 🦅
(適用於 Balthasar, Melchior, Casper 節點)

為了讓賢者系統 (MAGI) 能在 Discord 進行統一辯論，各節點需要以下三項資訊。

## 1. 取得 Bot Token (讓機器人「聽」與「說」)
1. 前往 **[Discord Developer Portal](https://discord.com/developers/applications)**。
2. 點擊右上角 **"New Application"**，輸入名稱 (如 `MAGI-Balthasar`) 並建立。
3. 左側選單點擊 **"Bot"**。
4. **取得 Token**:
   - 點擊 **"Reset Token"**。
   - 複製顯示的 Token 字串 (這就是 `Bot Token`)。
   - **重要**：這是機器人的靈魂，請勿外洩。
5. **開啟權限 (Privileged Gateway Intents)** (向下捲動):
   - 勾選 **"Message Content Intent"** (必須開啟，否則無法讀取指令)。
   - 勾選 **"Server Members Intent"** (建議開啟)。
   - 點擊 **"Save Changes"**。

## 2. 邀請機器人進伺服器
1. 左側選單點擊 **"OAuth2"** -> **"URL Generator"**。
2. **Scopes**: 勾選 `bot`。
3. **Bot Permissions**: 勾選：
   - `Send Messages`
   - `Read Message History`
   - `Embed Links` (發送排版漂亮的投票結果用)
4. 複製底部的 **Generated URL**。
5. 在瀏覽器貼上該連結，選擇您的 Discord 伺服器並授權。

## 3. 取得 Webhook URL (用於廣播投票結果)
1. 在 Discord 桌面版/網頁版，進入您的伺服器。
2. 點擊 **「伺服器設定 (Server Settings)」** -> **「整合 (Integrations)」**。
3. 點擊 **「Webhooks」** -> **「建立 Webhook」**。
4. 選擇目標頻道 (例如 `#magi-council`)。
5. 點擊 **「複製 Webhook 網址 (Copy Webhook URL)」**。

## 4. 取得 Channel ID (頻道 ID)
1. 開啟 **使用者設定 (User Settings)** -> **「進階 (Advanced)」**。
2. 開啟 **「開發者模式 (Developer Mode)」**。
3. 回到伺服器，對著目標頻道 (例如 `#magi-council`) 點擊 **右鍵**。
4. 選擇 **「複製頻道 ID (Copy Channel ID)」**。

---

##設定範例 (Dashboard)
取得上述資訊後，填入 Balthasar Dashboard (http://localhost:5001)：

*   **Webhook URL**: `https://discord.com/api/webhooks/133.../abc...`
*   **Bot Token**: `MTE...`
*   **Channel ID**: `133...`
