#!/bin/bash
# 🦅 Balthasar (NanoClaw) Launcher

# 1. Ensure Node.js v22 (LTS) is used
export PATH="/opt/homebrew/opt/node@22/bin:$PATH"
export ASSISTANT_NAME="Balthasar"

# 2. Go to Directory
cd ~/Desktop/MAGI_v2/nanoclaw

# 3. Check for Auth
if [ ! -d "store/auth" ]; then
    echo "⚠️  No WhatsApp Session Found!"
    echo "   Please scan the QR code that will appear shortly."
fi

# 4. Start the Coordinator
echo "🦅 Awakening Balthasar..."
npm run dev
