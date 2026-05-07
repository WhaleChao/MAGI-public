#!/bin/bash
# Start the RPC Server for Distributed Inference.
# Default mode keeps legacy background behavior; use --foreground for launchd.

set -euo pipefail

cd "$(dirname "$0")"

RPC_BIN="$(pwd)/bin/rpc-server"
RPC_HOST="${MAGI_RPC_HOST:-127.0.0.1}"
RPC_PORT="${MAGI_RPC_PORT:-50052}"
MODE="${1:---background}"

if [ ! -x "$RPC_BIN" ]; then
    echo "rpc-server binary not found or not executable: $RPC_BIN" >&2
    exit 1
fi

export DYLD_LIBRARY_PATH="$(pwd)/bin${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"

case "$MODE" in
    --foreground)
        exec "$RPC_BIN" -H "$RPC_HOST" -p "$RPC_PORT"
        ;;
    --background|"")
        "$RPC_BIN" -H "$RPC_HOST" -p "$RPC_PORT" > rpc_server.log 2>&1 &
        echo $! > rpc_server.pid
        ;;
    *)
        echo "Usage: $0 [--foreground|--background]" >&2
        exit 2
        ;;
esac
