#!/bin/bash
# 🔬 Melchior (NanoClaw) Launcher

# 1. Ensure Node.js v22 (LTS) is used
export PATH="/opt/homebrew/opt/node@22/bin:$PATH"
export ASSISTANT_NAME="Melchior"

# 2. Go to Directory
cd ~/Desktop/MAGI/nanoclaw

# 3. Check for Auth (Optional for non-primary nodes, but kept for consistency)
if [ ! -d "store/auth" ]; then
    echo "⚠️  No WhatsApp Session Found!"
    echo "   Note: Melchior might not strictly need WA if operating via Discord."
fi

# 4. Start the Scientist
echo "🔬 Awakening Melchior..."
npm run dev
