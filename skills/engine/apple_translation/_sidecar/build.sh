#!/bin/bash
# Build the MAGI Apple Translation sidecar binary.
#
# Output: ./magi_translator_sidecar (next to this script)
# Requires: Xcode CLT (Swift 6+), macOS 15+ SDK.

set -euo pipefail
cd "$(dirname "$0")"

OUTPUT="magi_translator_sidecar"

swiftc \
  -O \
  -framework Translation \
  -framework SwiftUI \
  -framework AppKit \
  -o "$OUTPUT" \
  main.swift

chmod +x "$OUTPUT"
echo "built: $(pwd)/$OUTPUT"
file "$OUTPUT"
