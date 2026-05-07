#!/bin/bash
# ============================================================
# DEPRECATED — 2026-04-20 (Phase 5 of cleanup plan)
# ============================================================
# This script is the legacy "Casper (Global OpenClaw) Launcher".
# OpenClaw Gateway / Caddy / Tailscale Funnel chain has been
# removed from MAGI v2. The canonical startup path is now:
#
#   launchctl kickstart -kp gui/$(id -u)/com.magi.daemon
#
# Or via the `magi` CLI:
#
#   magi restart
#
# DO NOT use this script for production startup. It is kept
# only as a reference for the old bootstrap sequence.
# All live lines below are commented out.
# ============================================================

echo "⚠️  start_casper.sh is DEPRECATED (2026-04-20)."
echo "    Use: magi restart   (LaunchAgent com.magi.daemon)"
exit 1

# --- Legacy body (disabled) ---------------------------------
# export PATH="/opt/homebrew/opt/node@22/bin:$PATH"
# export ASSISTANT_NAME="Casper"
#
# if [ -f ".env" ]; then
#   set -a
#   # shellcheck disable=SC1091
#   source ".env"
#   set +a
# fi
#
# if [ -z "${LINE_CHANNEL_ACCESS_TOKEN:-}" ] && [ -n "${MAGI_LINE_CHANNEL_ACCESS_TOKEN:-}" ]; then
#   export LINE_CHANNEL_ACCESS_TOKEN="${MAGI_LINE_CHANNEL_ACCESS_TOKEN}"
# fi
# if [ -z "${LINE_CHANNEL_SECRET:-}" ] && [ -n "${MAGI_LINE_CHANNEL_SECRET:-}" ]; then
#   export LINE_CHANNEL_SECRET="${MAGI_LINE_CHANNEL_SECRET}"
# fi
# if [ -z "${OPENCLAW_LINE_CHANNEL_ACCESS_TOKEN:-}" ] && [ -n "${LINE_CHANNEL_ACCESS_TOKEN:-}" ]; then
#   export OPENCLAW_LINE_CHANNEL_ACCESS_TOKEN="${LINE_CHANNEL_ACCESS_TOKEN}"
# fi
# if [ -z "${OPENCLAW_LINE_CHANNEL_SECRET:-}" ] && [ -n "${LINE_CHANNEL_SECRET:-}" ]; then
#   export OPENCLAW_LINE_CHANNEL_SECRET="${LINE_CHANNEL_SECRET}"
# fi
#
# echo "🧹 Cleaning up old processes..."
# pkill -f "api/discord_bot.py" || true
# pkill -f "api/server.py" || true
# pkill -f "api/tools_api.py" || true
# pkill -f "http.server 8000" || true
# sleep 1
#
# echo "🤰 Awakening Casper Gateway Service (loopback only)..."
# openclaw gateway install --port 18789 --bind loopback >> casper.log 2>&1 || true
# openclaw gateway restart --port 18789 --bind loopback >> casper.log 2>&1
#
# echo "🛡️ Starting Caddy reverse proxy (OpenClaw dashboard blocked)..."
# caddy stop 2>/dev/null || true
# caddy start --config Caddyfile_openclaw >> casper.log 2>&1
#
# echo "🌐 Setting up Tailscale Funnel → Caddy :18790..."
# tailscale funnel --bg 18790 >> casper.log 2>&1
#
# echo "💬 Starting LINE Webhook Server..."
# nohup ./venv/bin/python3 api/server.py > server.log 2>&1 &
#
# echo "🧰 Starting Tools API (Port 5003)..."
# nohup ./venv/bin/python3 api/tools_api.py > tools_api.log 2>&1 &
#
# echo "🤖 Starting Discord Bot..."
# pkill -f "api/discord_bot.py" || true
# nohup ./venv/bin/python3 api/discord_bot.py > discord.log 2>&1 &
#
# echo "🕒 Starting OpenClaw Cron Runner..."
# pkill -f "skills/ops/openclaw_cron_runner.py" || true
# nohup ./venv/bin/python3 skills/ops/openclaw_cron_runner.py > openclaw_cron_runner.log 2>&1 &
#
# echo "📊 Starting Dashboard Server (Port 8000)..."
# nohup ./venv/bin/python3 -m http.server 8000 --bind 127.0.0.1 > dashboard.log 2>&1 &
#
# echo "Monitor logs with: tail -f casper.log server.log tools_api.log"
