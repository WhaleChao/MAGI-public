
import os
import subprocess
import datetime
import logging

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 11, exc_info=True)


# Configure logging
logger = logging.getLogger("KeeperDBSync")

# Config
try:
    from api.routing.node_registry import get_node_ip as _get_node_ip
    _db_fallback = _get_node_ip("nas") or ""
except Exception:
    _db_fallback = ""
REMOTE_HOST = os.environ.get("DB_HOST", _db_fallback)
REMOTE_USER = os.environ.get("DB_USER", "casper_service")
REMOTE_PASS = os.environ.get("DB_PASSWORD", "")
DB_NAME = "law_firm_data"

LOCAL_USER = "ai"
LOCAL_PASS = "" # No password for user 'ai' (Socket Auth)

def sync_keeper_db():
    """
    Syncs the database from Remote Keeper (MAGI_REMOTE_DB_HOST) to Local Casper.
    Method: mysqldump | mysql
    
    Returns:
        bool: True if successful, False otherwise
    """
    logger.info(f"🚀 Starting Database Sync for {DB_NAME} from {REMOTE_HOST}...")
    
    # Construct command
    # Using pipe to avoid intermediate file for security and speed
    # mysqldump -h [remote] ... | mysql -u [local] ...
    
    dump_cmd = [
        "mysqldump",
        f"-h{REMOTE_HOST}",
        f"-u{REMOTE_USER}",
        f"-p{REMOTE_PASS}",
        "--single-transaction", # Good for InnoDB
        "--quick",
        "--compress",
        DB_NAME
    ]
    
    restore_cmd = [
        "mysql",
        f"-u{LOCAL_USER}",
        DB_NAME
    ]
    
    try:
        # Start Dump Process
        dump_proc = subprocess.Popen(
            dump_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        # Start Restore Process, piping from Dump
        restore_proc = subprocess.Popen(
            restore_cmd,
            stdin=dump_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        # Allow dump_proc to receive SIGPIPE if restore_proc exits
        dump_proc.stdout.close()
        
        output, error = restore_proc.communicate()
        dump_err = dump_proc.stderr.read()
        
        if restore_proc.returncode == 0 and dump_proc.wait() == 0:
            logger.info("✅ Database Sync Completed Successfully!")
            return True
        else:
            logger.error(f"❌ Custom Sync Failed.")
            if dump_err: logger.error(f"Dump Error: {dump_err.decode()}")
            if error: logger.error(f"Restore Error: {error.decode()}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Critical Error during sync: {e}")
        return False

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sync_keeper_db()
