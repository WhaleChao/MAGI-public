#!/usr/bin/env python3
"""Canonical V3 runtime-interface inventory generator.

The implementation is re-exported from the historical generator module so
archived release tooling remains import-compatible while the active validation
matrix no longer presents a V2 artifact as current production truth.
"""

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.architecture.generate_v2_inventory import (  # noqa: F401
    build_inventory,
    collect_daemon_children,
    collect_launchagents,
    collect_portable_cron_bytes,
    collect_routes,
    collect_skills,
    main,
    project_inventory_to_release,
    semantic_inventory_projection,
)


if __name__ == "__main__":
    raise SystemExit(main())
