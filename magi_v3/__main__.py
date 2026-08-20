"""Read-only-by-default command line entry point for ``python -m magi_v3``."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .config import load_settings
from .runtime import CoreRuntime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MAGI V3 lightweight core probe")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument(
        "--initialize-ledger",
        action="store_true",
        help="explicitly create/migrate the isolated V3 ledger",
    )
    parser.add_argument("--ready", action="store_true", help="run readiness instead of liveness")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    if args.state_dir is not None:
        env["MAGI_V3_STATE_DIR"] = str(args.state_dir.expanduser().resolve())
    settings = load_settings(env)
    runtime = CoreRuntime.build(settings)
    if args.initialize_ledger:
        runtime.initialize()
    report = runtime.health.readiness() if args.ready else runtime.health.liveness()
    print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
