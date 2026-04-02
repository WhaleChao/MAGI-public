#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.brain_manager import action as brain_manager
from skills.bridge import melchior_client


def main() -> int:
    model = (os.environ.get("MAGI_MAIN_MODEL") or "taide-12b").strip()
    ok_remote, msg_remote = brain_manager.check_remote_health()
    probe = melchior_client.chat(
        "Reply with exactly: OK",
        model=model,
        timeout=30,
    )

    out = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": model,
        "mode": brain_manager.get_brain_mode(),
        "remote_health": {"ok": bool(ok_remote), "message": msg_remote},
        "probe": probe,
        "runtime": brain_manager.get_melchior_runtime_status(),
    }

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if (ok_remote and probe.get("success")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
