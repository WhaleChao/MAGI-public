"""
OSC → Google Calendar 定時同步腳本
====================================
每 30 分鐘由 cron 呼叫，推送未來 30 天內的 case_todos 到 GCal。
enabled=false by default — 使用者完成 OAuth 後手動在 Admin 啟用。
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
        gcal_sync_path = _ROOT / "skills" / "osc-orchestrator"
        sys.path.insert(0, str(gcal_sync_path))
        from gcal_sync import run_sync  # type: ignore

        stats = run_sync(dry_run=False)
        logger.info(
            "GCal sync done — pushed=%d skipped=%d errors=%d",
            stats.get("pushed", 0),
            stats.get("skipped", 0),
            len(stats.get("errors", [])),
        )
        if stats.get("errors"):
            for err in stats["errors"][:5]:
                logger.warning("  error: %s", err)
    except Exception as exc:
        logger.exception("osc_gcal_sync.py failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
