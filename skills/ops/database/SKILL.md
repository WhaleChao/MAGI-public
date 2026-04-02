---
name: Database Operations
description: Database management and synchronization tools.
compatibility: Requires local MariaDB and connection to Keeper
metadata:
  iron_dome: true
  role: ops
---

# Database Operations Skill

This skill manages the synchronization and maintenance of MAGI's database infrastructure, specifically syncing the `law_firm_data` from the Keeper node.

## Usage

### 1. Manual Synchronization
Trigger a rigorous synchronization from Keeper (Remote) to Casper (Local).

```python
from skills.ops.database.sync import sync_keeper_db
result = sync_keeper_db()
if result:
    print("Sync Successful")
```

### 2. Scheduled Job
This skill is automatically scheduled to run daily at 01:00 AM via `cron_scheduler`.

### 3. Daily Backup / Restore
Use `backup_restore.py` for safe rotating backups and explicit restores.

```bash
# backup remote + local, keep 30 days
/Users/ai/Desktop/code/.venv/bin/python /Users/ai/Desktop/MAGI_v2/skills/ops/database/backup_restore.py --task backup --target both --keep-days 30

# list recent backups
/Users/ai/Desktop/code/.venv/bin/python /Users/ai/Desktop/MAGI_v2/skills/ops/database/backup_restore.py --task list --limit 20

# restore a backup to remote (requires explicit confirmation flag)
/Users/ai/Desktop/code/.venv/bin/python /Users/ai/Desktop/MAGI_v2/skills/ops/database/backup_restore.py --task restore --file /abs/path/to/backup.sql.gz --restore-target remote --yes-i-understand
```
