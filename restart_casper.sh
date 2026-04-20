#!/bin/bash
# ============================================================
# DEPRECATED — 2026-04-20 (Phase 5 of cleanup plan)
# ============================================================
# restart_casper.sh was the OpenClaw Gateway restart helper.
# OpenClaw has been removed. Canonical restart path is now:
#
#   magi restart
#   # or
#   launchctl kickstart -kp gui/$(id -u)/com.magi.daemon
# ============================================================

echo "⚠️  restart_casper.sh is DEPRECATED (2026-04-20)."
echo "    Use: magi restart   (LaunchAgent com.magi.daemon)"
exit 1

# --- Legacy body (disabled) ---------------------------------
# cd "$(dirname "$0")"
# echo "Stopping old process..."
# lsof -ti:18789 | xargs kill -9 2>/dev/null
# sleep 1
# ./start_casper.sh
