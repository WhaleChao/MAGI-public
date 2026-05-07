#!/usr/bin/env bash
# ============================================================
# MAGI Startup Script for macOS / Linux
# ============================================================
set -e
cd "$(dirname "$0")"

# Resolve venv python
if [ -f "venv/bin/python3" ]; then
    PYTHON="venv/bin/python3"
elif [ -f ".venv/bin/python3" ]; then
    PYTHON=".venv/bin/python3"
else
    echo "[ERROR] Python venv not found. Please run:"
    echo "  python3 -m venv venv"
    echo "  venv/bin/pip install -r requirements.txt"
    exit 1
fi

# Check if .env exists
if [ ! -f ".env" ]; then
    echo "[INFO] First-time setup detected. Launching Setup Wizard..."
    $PYTHON setup_wizard.py
fi

# Start MAGI daemon
echo "[INFO] Starting MAGI..."
exec $PYTHON daemon.py
