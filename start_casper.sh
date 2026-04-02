#!/bin/bash
# 🤰 Casper (Global OpenClaw) Launcher

# 1. Ensure Node.js v22 (LTS) is used
export PATH="/opt/homebrew/opt/node@22/bin:$PATH"
export ASSISTANT_NAME="Casper"

# 1.5 Load .env for all subprocesses (tools_api/skills rely on it)
if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

# Normalize LINE env names used across MAGI/OpenClaw components.
if [ -z "${LINE_CHANNEL_ACCESS_TOKEN:-}" ] && [ -n "${MAGI_LINE_CHANNEL_ACCESS_TOKEN:-}" ]; then
  export LINE_CHANNEL_ACCESS_TOKEN="${MAGI_LINE_CHANNEL_ACCESS_TOKEN}"
fi
if [ -z "${LINE_CHANNEL_SECRET:-}" ] && [ -n "${MAGI_LINE_CHANNEL_SECRET:-}" ]; then
  export LINE_CHANNEL_SECRET="${MAGI_LINE_CHANNEL_SECRET}"
fi
if [ -z "${OPENCLAW_LINE_CHANNEL_ACCESS_TOKEN:-}" ] && [ -n "${LINE_CHANNEL_ACCESS_TOKEN:-}" ]; then
  export OPENCLAW_LINE_CHANNEL_ACCESS_TOKEN="${LINE_CHANNEL_ACCESS_TOKEN}"
fi
if [ -z "${OPENCLAW_LINE_CHANNEL_SECRET:-}" ] && [ -n "${LINE_CHANNEL_SECRET:-}" ]; then
  export OPENCLAW_LINE_CHANNEL_SECRET="${LINE_CHANNEL_SECRET}"
fi

# 2. Check for Auth (Optional for Global CLI, but good to keep in mind)
# if [ ! -d "~/.openclaw/store/auth" ]; then
#     echo "⚠️  No WhatsApp Session Found! (Global Mode)"
# fi

# 3. Set System Prompt to Traditional Chinese
# export OPENCLAW_SYSTEM_PROMPT="..." (REMOVED: Using boot.md instead)

# Start the OpenClaw Gateway
# Kill existing Python components to prevent duplicates
echo "🧹 Cleaning up old processes..."
pkill -f "api/discord_bot.py" || true
pkill -f "api/server.py" || true
pkill -f "api/tools_api.py" || true
pkill -f "http.server 8000" || true
sleep 1

# Start (or restart) OpenClaw Gateway as LOCAL-ONLY service (no public funnel).
# Public access is routed through Caddy reverse proxy on :18790 which blocks
# the OpenClaw dashboard/chat while allowing webhook & API traffic.
echo "🤰 Awakening Casper Gateway Service (loopback only)..."
openclaw gateway install --port 18789 --bind loopback >> casper.log 2>&1 || true
openclaw gateway restart --port 18789 --bind loopback >> casper.log 2>&1
echo "Casper Gateway service restarted (loopback only)."

# Start Caddy reverse proxy – blocks OpenClaw dashboard, proxies webhooks/API
echo "🛡️ Starting Caddy reverse proxy (OpenClaw dashboard blocked)..."
caddy stop 2>/dev/null || true
caddy start --config Caddyfile_openclaw >> casper.log 2>&1
echo "Caddy proxy running on :18790."

# Expose Caddy proxy (NOT raw OpenClaw) via Tailscale Funnel
echo "🌐 Setting up Tailscale Funnel → Caddy :18790..."
tailscale funnel --bg 18790 >> casper.log 2>&1
echo "Tailscale Funnel active → :18790 (OpenClaw dashboard blocked)."

# Start the LINE Webhook Server (Port 5002)
echo "💬 Starting LINE Webhook Server..."
nohup ./venv/bin/python3 api/server.py > server.log 2>&1 &
echo "LINE Server is running (PID: $!)."

# Start Tools API (Port 5003)
echo "🧰 Starting Tools API (Port 5003)..."
nohup ./venv/bin/python3 api/tools_api.py > tools_api.log 2>&1 &
echo "Tools API is running (PID: $!)."

# Start Discord Bot & Scheduler
echo "🤖 Starting Discord Bot..."
# Ensure single instance check again just in case
pkill -f "api/discord_bot.py" || true 
nohup ./venv/bin/python3 api/discord_bot.py > discord.log 2>&1 &
echo "Discord Bot is running (PID: $!)."

# Start OpenClaw cron bridge runner (OpenClaw cron -> local autopilot command)
echo "🕒 Starting OpenClaw Cron Runner..."
pkill -f "skills/ops/openclaw_cron_runner.py" || true
nohup ./venv/bin/python3 skills/ops/openclaw_cron_runner.py > openclaw_cron_runner.log 2>&1 &
echo "OpenClaw Cron Runner is running (PID: $!)."


# Start Dashboard Server (to avoid CORS)
echo "📊 Starting Dashboard Server (Port 8000)..."
nohup ./venv/bin/python3 -m http.server 8000 --bind 127.0.0.1 > dashboard.log 2>&1 &
echo "Dashboard available at http://localhost:8000/MAGI_Dashboard.html"

echo "Monitor logs with: tail -f casper.log server.log tools_api.log"
