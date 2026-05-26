"""
OSC → Google Calendar 定時同步腳本
====================================
Delegates to the current osc-orchestrator task_gcal_sync implementation.
"""
import sys
import logging
from pathlib import Path

# Ensure MAGI root in sys.path
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [osc_gcal_sync] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    try:
        action_path = _ROOT / "skills" / "osc-orchestrator" / "action.py"
        sys.path.insert(0, str(action_path.parent))
        import importlib.util

        spec = importlib.util.spec_from_file_location("magi_osc_orchestrator_action", action_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load osc-orchestrator action.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        stats = module.task_gcal_sync({"limit": 120, "repair_existing": True, "repair_limit": 120, "mirror_imported": True})
        logger.info(
            "GCal sync done — inserted=%d patched=%d failed=%d",
            stats.get("inserted", 0),
            stats.get("patched", 0),
            stats.get("failed", 0),
        )
        if not stats.get("ok"):
            logger.warning("  error: %s", stats.get("error", "unknown"))
    except Exception as exc:
        logger.exception("osc_gcal_sync.py failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
