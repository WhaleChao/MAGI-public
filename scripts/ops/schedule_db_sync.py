
import sys
import os

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from skills.ops.cron_scheduler import CronScheduler
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

scheduler = CronScheduler()

command = f"{_MAGI_ROOT}/venv/bin/python3 <_MAGI_ROOT>/scripts/ops/sync_keeper_db.py"
cron_expr = "0 1 * * *" # Daily at 1 AM

print(scheduler.add_job(cron_expr, command, description="Sync Keeper DB (Law Firm Data)"))
