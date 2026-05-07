#!/usr/bin/env python3
"""
遠端 Windows DB → 本機 law_firm_data 同步腳本。
每 10 分鐘從遠端拉最新資料到本機，確保本機 DB 與遠端一致。
同步前先備份本機 DB，備份保留 72 小時，超過自動刪除。
"""
import subprocess, sys, time, os, signal, glob

sys.stdout.reconfigure(line_buffering=True)

LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 3306
try:
    from api.routing.node_registry import get_node_ip as _get_node_ip
    REMOTE_HOST = _get_node_ip("nas") or ""
except Exception:
    REMOTE_HOST = ""
REMOTE_PORT = 3306
DB_USER = os.environ.get("MAGI_SYNC_DB_USER", "python_user")
DB_PASS = os.environ.get("MAGI_SYNC_DB_PASS", "")
REMOTE_USER = os.environ.get("MAGI_SYNC_REMOTE_USER", "root")
REMOTE_PASS = os.environ.get("MAGI_SYNC_REMOTE_PASS", "")
DB_NAME = "law_firm_data"
SYNC_INTERVAL = 600  # 10 minutes
DUMP_PATH = "/tmp/law_firm_data_sync.sql"
BACKUP_DIR = "/Users/ai/Desktop/DATA/db_backups"
BACKUP_MAX_AGE = 72 * 3600  # 72 hours

running = True

def _signal_handler(sig, frame):
    global running
    print(f"Received signal {sig}, stopping...", flush=True)
    running = False

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


def remote_reachable():
    """Check if remote DB is reachable."""
    try:
        result = subprocess.run(
            ["mysql", "-h", REMOTE_HOST, "-P", str(REMOTE_PORT),
             "-u", REMOTE_USER, f"-p{REMOTE_PASS}", DB_NAME,
             "-e", "SELECT 1;"],
            capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0
    except Exception:
        return False


def backup_local():
    """Backup local DB before sync."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(BACKUP_DIR, f"law_firm_data_{timestamp}.sql")
    result = subprocess.run(
        ["mysqldump", "-h", LOCAL_HOST, "-P", str(LOCAL_PORT),
         "-u", DB_USER, f"-p{DB_PASS}",
         "--single-transaction", "--skip-triggers", "--skip-routines",
         DB_NAME],
        capture_output=True, timeout=120
    )
    if result.returncode == 0:
        with open(backup_file, "wb") as f:
            f.write(result.stdout)
        size_mb = len(result.stdout) / 1024 / 1024
        print(f"  Backup OK: {backup_file} ({size_mb:.1f} MB)", flush=True)
        return True
    else:
        print(f"  Backup FAILED: {result.stderr.decode()}", flush=True)
        return False


def cleanup_old_backups():
    """Delete backups older than 72 hours."""
    if not os.path.isdir(BACKUP_DIR):
        return
    now = time.time()
    removed = 0
    for f in glob.glob(os.path.join(BACKUP_DIR, "law_firm_data_*.sql")):
        if now - os.path.getmtime(f) > BACKUP_MAX_AGE:
            os.remove(f)
            removed += 1
    if removed:
        print(f"  Cleaned {removed} old backup(s)", flush=True)


def dump_remote():
    """Dump remote DB to file."""
    result = subprocess.run(
        ["mysqldump", "-h", REMOTE_HOST, "-P", str(REMOTE_PORT),
         "-u", REMOTE_USER, f"-p{REMOTE_PASS}",
         "--single-transaction", "--skip-triggers", "--skip-routines",
         DB_NAME],
        capture_output=True, timeout=120
    )
    if result.returncode == 0:
        with open(DUMP_PATH, "wb") as f:
            f.write(result.stdout)
        size_mb = len(result.stdout) / 1024 / 1024
        print(f"  Remote dump OK: {size_mb:.1f} MB", flush=True)
        return True
    else:
        print(f"  Remote dump FAILED: {result.stderr.decode()}", flush=True)
        return False


def push_to_local():
    """Push dump to local DB."""
    with open(DUMP_PATH, "rb") as f:
        result = subprocess.run(
            ["mysql", "-h", LOCAL_HOST, "-P", str(LOCAL_PORT),
             "-u", DB_USER, f"-p{DB_PASS}", DB_NAME],
            stdin=f, capture_output=True, text=True, timeout=120
        )
    if result.returncode == 0:
        print("  Push to local OK", flush=True)
        return True
    else:
        print(f"  Push to local FAILED: {result.stderr}", flush=True)
        return False


def sync_once():
    """One full sync cycle: Backup local → Pull remote → Push to local."""
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Checking remote...", flush=True)
    if not remote_reachable():
        print("  Remote unreachable, skip.", flush=True)
        return False

    print("  Backing up local before sync...", flush=True)
    backup_local()
    cleanup_old_backups()

    print("  Remote reachable, dumping remote...", flush=True)
    if not dump_remote():
        return False

    return push_to_local()


if __name__ == "__main__":
    print("=== DB Sync: Remote → Local (with backup) ===", flush=True)
    print(f"Interval: {SYNC_INTERVAL}s | Backup retention: {BACKUP_MAX_AGE // 3600}h", flush=True)

    while running:
        try:
            sync_once()
        except Exception as e:
            print(f"  Sync error: {e}", flush=True)

        for _ in range(SYNC_INTERVAL):
            if not running:
                break
            time.sleep(1)

    print("Sync stopped.", flush=True)
    if os.path.exists(DUMP_PATH):
        os.remove(DUMP_PATH)
