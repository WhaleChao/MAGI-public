#!/usr/bin/env bash
# Cloudflare Quick Tunnel for MAGI LINE Webhook
# Starts cloudflared, extracts the URL, and registers it with LINE.
set -euo pipefail

MAGI_ROOT="${MAGI_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
if [ -n "${MAGI_CLOUDFLARED_LOG_PATH:-}" ]; then
  LOG="$MAGI_CLOUDFLARED_LOG_PATH"
elif [ -n "${MAGI_RUNTIME_DIR:-}" ]; then
  LOG="$MAGI_RUNTIME_DIR/logs/cloudflared.log"
else
  LOG="$MAGI_ROOT/logs/cloudflared.log"
fi
AGENT_DIR="${MAGI_AGENT_DIR:-$MAGI_ROOT/.agent}"
mkdir -p "$(dirname "$LOG")" "$AGENT_DIR"

if [ "${MAGI_CLOUDFLARE_TUNNEL_DRY_RUN:-0}" = "1" ]; then
  printf 'LOG=%s\nAGENT_DIR=%s\n' "$LOG" "$AGENT_DIR"
  exit 0
fi

if [ -f "$MAGI_ROOT/.env" ]; then
  set -a
  source "$MAGI_ROOT/.env" 2>/dev/null || true
  set +a
fi

LINE_TOKEN="${MAGI_LINE_CHANNEL_ACCESS_TOKEN:-}"
LOCAL_PORT="${1:-${MAGI_WEBHOOK_PROXY_PORT:-${MAGI_TAILSCALE_PORT:-18790}}}"

if [ -z "$LINE_TOKEN" ]; then
  LINE_TOKEN="${MAGI_LINE_CHANNEL_ACCESS_TOKEN:-}"
fi

if [ -z "$LINE_TOKEN" ]; then
  echo "ERROR: MAGI_LINE_CHANNEL_ACCESS_TOKEN not set" >&2
  exit 1
fi

# Kill only the MAGI webhook tunnel for this port; other tunnels may be in use.
pkill -f "cloudflared tunnel --url http://127.0.0.1:${LOCAL_PORT}" 2>/dev/null || true
sleep 1

# Start tunnel and capture output
cloudflared tunnel --url "http://127.0.0.1:${LOCAL_PORT}" --no-autoupdate 2>"$LOG" &
CF_PID=$!
echo "cloudflared PID: $CF_PID"

# Wait for URL to appear (up to 30 seconds)
CF_URL=""
for i in $(seq 1 30); do
  CF_URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG" 2>/dev/null | head -1 || true)
  if [ -n "$CF_URL" ]; then
    break
  fi
  sleep 1
done

if [ -z "$CF_URL" ]; then
  echo "ERROR: Could not get tunnel URL after 30s" >&2
  kill $CF_PID 2>/dev/null
  exit 1
fi

WEBHOOK_URL="${CF_URL}/line/webhook"
echo "Tunnel URL: $CF_URL"
echo "Webhook URL: $WEBHOOK_URL"

# Register with LINE
RESULT=$(curl -s -X PUT \
  -H "Authorization: Bearer ${LINE_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"endpoint\":\"${WEBHOOK_URL}\"}" \
  https://api.line.me/v2/bot/channel/webhook/endpoint 2>&1)
echo "LINE registration: $RESULT"

# Test webhook
sleep 2
TEST=$(curl -s \
  -H "Authorization: Bearer ${LINE_TOKEN}" \
  -H "Content-Type: application/json" \
  -X POST https://api.line.me/v2/bot/channel/webhook/test \
  -d '{}' 2>&1)
echo "Webhook test: $TEST"

# Save URL for health monitoring
echo "$WEBHOOK_URL" > "$AGENT_DIR/line_webhook_url.txt"
echo "$CF_URL" > "$AGENT_DIR/cloudflare_tunnel_url.txt"

# Wait for tunnel process
wait $CF_PID
