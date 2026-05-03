# BALTHASAR 部署計畫 (Deployment Plan)

## 節點資訊
- **角色**: Balthasar (行動秘書 / Pragmatist)
- **硬體**: MacBook Air M4 (16GB RAM)
- **位置**: 行動裝置，透過 Tailscale VPN 連接

## 部署步驟

### 1. 安裝 Ollama
```bash
# 安裝 Ollama
curl -fsSL https://ollama.ai/install.sh | sh

# 下載模型
ollama pull qwen2.5:14b
```

### 2. 設定 Ollama 監聽所有介面
```bash
# 設定環境變數
launchctl setenv OLLAMA_HOST 0.0.0.0:11434

# 或在 ~/.zshrc 加入
export OLLAMA_HOST=0.0.0.0:11434
```

### 3. 安裝 OpenClaw
> ⚠️ DEPRECATED 2026-05-03：OpenClaw 已棄用，本段保留為歷史參考。
```bash
# 使用 Homebrew
brew install openclaw

# 或 npm
npm install -g openclaw
```

### 4. 複製設定檔
從 Casper 複製 BALTHASAR 專屬設定：
```bash
scp casper@100.97.29.92:/Users/ai/Desktop/MAGI_v2/SOUL_BALTHASAR.md ~/Desktop/
```

### 5. 設定 OpenClaw Agent
> ⚠️ DEPRECATED 2026-05-03：OpenClaw 已棄用，本段保留為歷史參考。
```bash
mkdir -p ~/.openclaw/agents/main/agent
cp ~/Desktop/SOUL_BALTHASAR.md ~/.openclaw/agents/main/agent/boot.md
```

### 6. 啟動服務
```bash
# 終端機 1: Ollama
ollama serve

# 終端機 2: OpenClaw
openclaw gateway start
```

### 7. 驗證連線
在 Casper 上測試：
```bash
curl http://100.128.235.126:11434/api/version
```

## 開機自動啟動 (Optional)

### Ollama (launchd)
已內建於 Ollama 安裝程式。

### OpenClaw Gateway
> ⚠️ DEPRECATED 2026-05-03：OpenClaw 已棄用，本段保留為歷史參考。
建立 `~/Library/LaunchAgents/com.openclaw.gateway.plist`：
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.openclaw.gateway</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/openclaw</string>
        <string>gateway</string>
        <string>start</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
```

載入：
```bash
launchctl load ~/Library/LaunchAgents/com.openclaw.gateway.plist
```

---
*MAGI Federation Deployment Guide*
