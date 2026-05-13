#!/usr/bin/env python3
"""Run a command with explicit environment assignments, without a shell."""

from __future__ import annotations

import os
import sys


def main(argv: list[str]) -> int:
    env = dict(os.environ)
    idx = 0
    while idx < len(argv):
        item = argv[idx]
        if item == "--":
            idx += 1
            break
        if "=" not in item or item.startswith("="):
            print(f"invalid env assignment: {item}", file=sys.stderr)
            return 2
        key, value = item.split("=", 1)
        if not key.replace("_", "").isalnum() or key[0].isdigit():
            print(f"invalid env key: {key}", file=sys.stderr)
            return 2
        env[key] = value
        idx += 1

    command = argv[idx:]
    if not command:
        print("missing command after --", file=sys.stderr)
        return 2
    os.execvpe(command[0], command, env)
    return 127


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
