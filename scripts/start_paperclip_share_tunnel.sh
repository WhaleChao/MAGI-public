#!/usr/bin/env bash
# Paperclip share-only Cloudflare Quick Tunnel.
set -euo pipefail

MAGI_ROOT="${MAGI_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PORT="${PAPERCLIP_SHARE_GATEWAY_PORT:-5014}"
TARGET="${PAPERCLIP_SHARE_GATEWAY_TARGET:-http://127.0.0.1:5002}"
PY="${MAGI_SKILL_PYTHON:-$MAGI_ROOT/venv/bin/python}"
LOG_DIR="$MAGI_ROOT/logs"
AGENT_DIR="$MAGI_ROOT/.agent"
RUNTIME_DIR="$MAGI_ROOT/.runtime"
GATEWAY_LOG="$LOG_DIR/paperclip_share_gateway.log"
TUNNEL_LOG="$LOG_DIR/paperclip_share_cloudflared.log"
GATEWAY_PID_FILE="$AGENT_DIR/paperclip_share_gateway.pid"
TUNNEL_PID_FILE="$AGENT_DIR/paperclip_share_cloudflared.pid"
URL_FILE="$RUNTIME_DIR/osc_share_public_base_url.txt"

mkdir -p "$LOG_DIR" "$AGENT_DIR" "$RUNTIME_DIR"

stop_pid_file() {
  local file="$1"
  if [ -f "$file" ]; then
    local pid
    pid="$(cat "$file" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      sleep 1
    fi
    rm -f "$file"
  fi
}

stop_pid_file "$GATEWAY_PID_FILE"
stop_pid_file "$TUNNEL_PID_FILE"
pkill -f "scripts/share_gateway.py --port $PORT" 2>/dev/null || true
pkill -f "cloudflared tunnel --url http://127.0.0.1:$PORT" 2>/dev/null || true

nohup env PAPERCLIP_SHARE_GATEWAY_TARGET="$TARGET" "$PY" "$MAGI_ROOT/scripts/share_gateway.py" --port "$PORT" >"$GATEWAY_LOG" 2>&1 &
GATEWAY_PID=$!
echo "$GATEWAY_PID" > "$GATEWAY_PID_FILE"

for _ in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$GATEWAY_PID" 2>/dev/null; then
    echo "ERROR: share gateway exited. See $GATEWAY_LOG" >&2
    exit 1
  fi
  sleep 0.25
done

: > "$TUNNEL_LOG"
nohup cloudflared tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate > /dev/null 2>"$TUNNEL_LOG" &
TUNNEL_PID=$!
echo "$TUNNEL_PID" > "$TUNNEL_PID_FILE"

SHARE_URL=""
for _ in $(seq 1 45); do
  SHARE_URL="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -1 || true)"
  if [ -n "$SHARE_URL" ]; then
    break
  fi
  if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
    echo "ERROR: cloudflared exited. See $TUNNEL_LOG" >&2
    exit 1
  fi
  sleep 1
done

if [ -z "$SHARE_URL" ]; then
  echo "ERROR: Could not obtain Cloudflare share URL. See $TUNNEL_LOG" >&2
  exit 1
fi

printf '%s\n' "$SHARE_URL" > "$URL_FILE"
printf '%s\n' "$SHARE_URL" > "$AGENT_DIR/paperclip_share_tunnel_url.txt"

echo "Paperclip share URL: $SHARE_URL"
echo "Gateway PID: $GATEWAY_PID"
echo "Tunnel PID: $TUNNEL_PID"
