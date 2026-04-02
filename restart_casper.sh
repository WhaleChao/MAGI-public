#!/bin/bash
# Helper script to restart Casper (OpenClaw)
# Handles switching to the correct directory and killing old processes.

echo "🔄 Restarting Casper..."

# 1. Switch to the correct directory
cd "$(dirname "$0")"

# 2. Kill existing process (Port 18789)
echo "Stopping old process..."
lsof -ti:18789 | xargs kill -9 2>/dev/null
sleep 1

# 3. Start Casper
echo "Starting new instance..."
./start_casper.sh

echo "Done! Monitor logs with: tail -f casper.log"
