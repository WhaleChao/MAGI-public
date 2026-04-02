#!/bin/bash
# Start the RPC Server for Distributed Inference
# Using locally compiled binary in ./bin

cd "$(dirname "$0")"
export DYLD_LIBRARY_PATH="$(pwd)/bin:$DYLD_LIBRARY_PATH"

TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || echo "127.0.0.1")
./bin/rpc-server -H "$TAILSCALE_IP" -p 50052 > rpc_server.log 2>&1 &
echo $! > rpc_server.pid
