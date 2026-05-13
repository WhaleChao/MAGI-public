# -*- coding: utf-8 -*-
"""
Cron Scheduler Skill (自動化排程)
Iron Dome Audit: ✅ SAFE — Local JSON storage, no external execution unless via Orchestrator

Provides: Job management (add/remove/list/check)
Schedules are stored in MAGI/cron_jobs.json
"""

import json
import os
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import logging
from datetime import datetime
import time
import uuid
from typing import Dict

# === R3: runtime_dir 接入 ===
try:
    from api.platforms import runtime_dir as _rd
except Exception:
    _rd = None


def _use_runtime_dir() -> bool:
    if _rd is None:
        return False
    return os.environ.get("MAGI_USE_RUNTIME_DIR", "0").strip().lower() in {"1", "true", "on", "yes"}


def _load_cron_state() -> Dict[str, Dict[str, str]]:
    if not _use_runtime_dir():
        return {}
    p = _rd.cron_state()
    if not p.exists():
        return {}
    try:
        import json as _j
        return _j.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_cron_state(state: Dict[str, Dict[str, str]]) -> None:
    if not _use_runtime_dir():
        return
    _rd.atomic_write_json(_rd.cron_state(), state)

logger = logging.getLogger("CronScheduler")

_DEFAULT_CATCHUP_SKIP_IDS = {
    # These jobs can scan NAS/case folders or open portal automation. Running
    # them immediately after reboot stacks IO on top of SMB remount recovery.
    "job_laf_nightly_audit",
    "job_pdf_namer_nightly",
    "job_weekend_bookmark",
    "job_nightly_bookmark_regex",
    "job_benchmark_pdf_namer",
    "job_obsidian_ingest",
    "job_osc_scan_cases",
    "job_insight_sync",
}


def _catchup_skip_ids() -> set[str]:
    raw = os.environ.get("MAGI_CRON_CATCHUP_SKIP_IDS", "").strip()
    extra = {x.strip() for x in raw.split(",") if x.strip()}
    return _DEFAULT_CATCHUP_SKIP_IDS | extra

JOB_FILE = f"{_MAGI_ROOT}/cron_jobs.json"

class CronScheduler:
    def __init__(self):
        self.jobs = []
        self._last_file_mtime = 0.0
        self._load_jobs()

    def _new_job_id(self) -> str:
        return f"job_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"

    def _normalize_jobs(self):
        normalized = []
        seen_ids = set()
        changed = False
        for raw in self.jobs:
            if not isinstance(raw, dict):
                changed = True
                continue
            job = dict(raw)
            job_id = str(job.get("id", "")).strip()
            if not job_id or job_id in seen_ids:
                job_id = self._new_job_id()
                changed = True
            seen_ids.add(job_id)
            job["id"] = job_id
            job.setdefault("cron", "0 9 * * *")
            job.setdefault("command", "")
            job.setdefault("desc", "")
            job.setdefault("channel_id", None)
            job.setdefault("last_run", None)
            job.setdefault("last_run_minute", None)
            job.setdefault("enabled", True)
            normalized.append(job)
        self.jobs = normalized
        if changed:
            self._save_jobs()

    def _load_jobs(self):
        """Load jobs from JSON file."""
        if os.path.exists(JOB_FILE):
            try:
                self._last_file_mtime = os.path.getmtime(JOB_FILE)
                with open(JOB_FILE, 'r', encoding='utf-8') as f:
                    self.jobs = json.load(f)
                if not isinstance(self.jobs, list):
                    self.jobs = []
            except Exception as e:
                logger.error(f"Failed to load jobs: {e}")
                self.jobs = []
        else:
            self.jobs = []
        self._normalize_jobs()
        # --- R3 merge：若 cron_state.json 存在，用它覆蓋 last_run / last_run_minute ---
        if _use_runtime_dir():
            state = _load_cron_state()
            for j in self.jobs:
                jid = j.get("id")
                if jid and jid in state:
                    j["last_run"] = state[jid].get("last_run", j.get("last_run"))
                    j["last_run_minute"] = state[jid].get("last_run_minute", j.get("last_run_minute"))

    def _hot_reload_if_changed(self):
        """Reload jobs from disk if the file was modified externally."""
        try:
            if not os.path.exists(JOB_FILE):
                return
            mtime = os.path.getmtime(JOB_FILE)
            if mtime > self._last_file_mtime:
                old_count = len(self.jobs)
                old_ids = {j["id"] for j in self.jobs}
                self._load_jobs()
                new_ids = {j["id"] for j in self.jobs}
                added = new_ids - old_ids
                if added:
                    logger.info("🔄 Hot-reloaded cron_jobs.json: %d→%d jobs (+%s)",
                                old_count, len(self.jobs), ", ".join(added))
        except Exception as e:
            logger.warning("Hot-reload check failed: %s", e)

    def _save_jobs(self):
        """Save jobs to JSON file (merge-safe: preserves externally-added jobs)."""
        try:
            # Merge: read disk first to preserve jobs added externally since last load
            disk_jobs = []
            if os.path.exists(JOB_FILE):
                try:
                    with open(JOB_FILE, 'r', encoding='utf-8') as f:
                        disk_jobs = json.load(f)
                    if not isinstance(disk_jobs, list):
                        disk_jobs = []
                except Exception:
                    disk_jobs = []

            # Build lookup of our in-memory jobs (authoritative for last_run etc.)
            mem_by_id = {j["id"]: j for j in self.jobs if j.get("id")}

            # Merge: start with in-memory state, then append any disk-only jobs
            merged = list(self.jobs)
            merged_ids = {j["id"] for j in merged if j.get("id")}
            for dj in disk_jobs:
                djid = (dj.get("id") or "").strip()
                if djid and djid not in merged_ids:
                    merged.append(dj)
                    merged_ids.add(djid)
                    logger.info("🔄 Preserved externally-added job: %s", djid)

            self.jobs = merged

            # --- R3：flag 開時把 last_run/last_run_minute 從寫出 payload 清乾淨 ---
            if _use_runtime_dir():
                for j in self.jobs:
                    j["last_run"] = None
                    j["last_run_minute"] = None

            with open(JOB_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.jobs, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save jobs: {e}")

    def _normalize_cron_expr(self, cron_expr: str):
        raw = (cron_expr or "").strip().lower()
        if not raw:
            return False, "", "❌ 缺少 cron 表達式。"

        if raw.startswith("daily"):
            try:
                if " " in raw:
                    time_part = raw.split()[1]
                    hour, minute = map(int, time_part.split(":"))
                    return True, f"{minute} {hour} * * *", ""
                return True, "0 9 * * *", ""
            except Exception:
                return False, "", "❌ 無效的時間格式。請使用 `daily HH:MM`"
        if raw == "hourly":
            return True, "0 * * * *", ""
        if raw == "every2h":
            return True, "0 */2 * * *", ""

        parts = raw.split()
        if len(parts) != 5:
            return False, "", "❌ cron 格式需為 5 欄位，例如 `0 */2 * * *`"
        return True, raw, ""

    def _field_match(self, expr: str, value: int, min_v: int, max_v: int) -> bool:
        field = (expr or "").strip()
        if field == "*":
            return True

        def _single_match(token: str) -> bool:
            tok = token.strip()
            if not tok:
                return False
            step = 1
            if "/" in tok:
                base, step_s = tok.split("/", 1)
                step = int(step_s)
                tok = base or "*"
                if step <= 0:
                    return False

            if tok == "*":
                start, end = min_v, max_v
            elif "-" in tok:
                a, b = tok.split("-", 1)
                start, end = int(a), int(b)
            else:
                single = int(tok)
                return single == value

            if value < start or value > end:
                return False
            return ((value - start) % step) == 0

        try:
            for part in field.split(","):
                if _single_match(part):
                    return True
            return False
        except Exception:
            return False

    def add_job(self, cron_expr, command, channel_id=None, description=""):
        """
        Add a new cron job.
        cron_expr example: "0 9 * * *" (Daily at 9:00)
        Supports simplified format: "daily 9:00", "hourly"
        """
        ok, cron_expr, err = self._normalize_cron_expr(str(cron_expr))
        if not ok:
            return err

        job_id = self._new_job_id()
        job = {
            "id": job_id,
            "cron": cron_expr,
            "command": command,
            "desc": description,
            "channel_id": channel_id,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True
        }
        self.jobs.append(job)
        self._save_jobs()
        return f"✅ 已新增排程: `{command}` ({cron_expr})"

    def ensure_job(self, cron_expr, command, channel_id=None, description=""):
        """
        Idempotent add/update by (command, description).
        """
        ok, cron_expr, err = self._normalize_cron_expr(str(cron_expr))
        if not ok:
            return {"success": False, "message": err}

        cmd = (command or "").strip()
        desc = (description or "").strip()
        if not cmd:
            return {"success": False, "message": "❌ command 不可空白。"}

        for job in self.jobs:
            if (job.get("command", "").strip() == cmd) and (job.get("desc", "").strip() == desc):
                job["cron"] = cron_expr
                job["channel_id"] = channel_id
                job["enabled"] = True
                self._save_jobs()
                return {
                    "success": True,
                    "action": "updated",
                    "job_id": job.get("id"),
                    "cron": cron_expr,
                    "command": cmd,
                    "desc": desc,
                }

        add_msg = self.add_job(cron_expr, cmd, channel_id=channel_id, description=desc)
        created = next((j for j in reversed(self.jobs) if j.get("command") == cmd and j.get("desc") == desc), None)
        return {
            "success": True,
            "action": "created",
            "job_id": created.get("id") if created else "",
            "cron": cron_expr,
            "command": cmd,
            "desc": desc,
            "message": add_msg,
        }

    def remove_job(self, job_id):
        """Remove a job by ID."""
        original_len = len(self.jobs)
        self.jobs = [j for j in self.jobs if j["id"] != job_id]
        if len(self.jobs) < original_len:
            self._save_jobs()
            return f"✅ 已刪除任務: `{job_id}`"
        return f"❌ 找不到任務: `{job_id}`"

    def list_jobs(self):
        """List all active jobs."""
        if not self.jobs:
            return "📭 目前沒有排程任務。"
        
        report = "📅 **自動化排程清單**\n\n"
        for j in self.jobs:
            status = "🟢" if j["enabled"] else "🔴"
            last = j["last_run"] or "Never"
            report += f"{status} **{j['desc'] or '未命名'}** (`{j['id']}`)\n"
            report += f"   - ⏰ 時間: `{j['cron']}`\n"
            report += f"   - 🤖 指令: `{j['command']}`\n"
            report += f"   - 🕒 上次執行: {last}\n\n"
        return report

    def check_due_jobs(self):
        """
        Check which jobs are due to run.
        Returns a list of due jobs.
        Updates last_run timestamp.
        """
        self._hot_reload_if_changed()
        now = datetime.now()
        due_jobs = []
        current_minute_str = now.strftime("%Y-%m-%d %H:%M")
        
        for job in self.jobs:
            if not job.get("enabled", True):
                continue

            # Cron parsing logic
            try:
                parts = job["cron"].split()
                if len(parts) != 5: continue
                
                min_f, hour_f, day_f, month_f, dow_f = parts
                
                cron_dow = (now.weekday() + 1) % 7  # Python Mon=0..Sun=6 -> cron Sun=0..Sat=6
                is_due = (
                    self._field_match(min_f, now.minute, 0, 59)
                    and self._field_match(hour_f, now.hour, 0, 23)
                    and self._field_match(day_f, now.day, 1, 31)
                    and self._field_match(month_f, now.month, 1, 12)
                    and self._field_match(dow_f, cron_dow, 0, 6)
                )
                
                # Check if already ran this minute
                last_run_minute = job.get("last_run_minute")
                
                if is_due and last_run_minute != current_minute_str:
                    due_jobs.append(job)
                    job["last_run"] = now.isoformat()
                    job["last_run_minute"] = current_minute_str
                    # R3: 寫到 cron_state.json，cron_jobs.json 由 strip 腳本清乾淨
                    if _use_runtime_dir():
                        st = _load_cron_state()
                        st[job["id"]] = {
                            "last_run": job["last_run"],
                            "last_run_minute": job["last_run_minute"],
                        }
                        _save_cron_state(st)

            except Exception as e:
                logger.error(f"Error checking job {job['id']}: {e}")
                continue
        
        if due_jobs:
            self._save_jobs()

        return due_jobs

    def get_missed_jobs(self, catchup_window_hours: int = 8, min_hour: int = 6) -> list:
        """
        Return jobs that were due in the past catchup_window_hours but were NOT executed.

        Called once at startup (2nd scheduler loop, ~60s after start) to catch up jobs
        missed while MAGI was offline (kernel panic, restart, maintenance, etc.).

        Rules:
        - Looks back at most catchup_window_hours (default 8h) from now
        - Does NOT look earlier than today's min_hour:00 (default 06:00) to avoid
          re-running nightly/overnight jobs at the wrong time
        - Respects per-job ``"no_catchup": true`` flag for timing-sensitive jobs
        - Each job appears at most once (the most recent missed occurrence)
        - Skips jobs whose ``last_run_minute >= most_recent_due`` (already ran)

        Args:
            catchup_window_hours: How many hours back to search (env: MAGI_CRON_CATCHUP_HOURS)
            min_hour: Skip jobs scheduled before this hour today (env: MAGI_CRON_CATCHUP_MIN_HOUR)

        Returns:
            List of job dicts sorted chronologically (oldest missed first).
        """
        from datetime import timedelta
        self._hot_reload_if_changed()
        now = datetime.now()

        # Effective search window: [effective_start, window_end]
        # Leave a 2-minute grace at the end to avoid racing with check_due_jobs.
        window_end = now - timedelta(minutes=2)
        window_back = now - timedelta(hours=catchup_window_hours)
        today_floor = now.replace(hour=min_hour, minute=0, second=0, microsecond=0)
        effective_start = max(window_back, today_floor)

        if window_end <= effective_start:
            return []

        total_minutes = int((window_end - effective_start).total_seconds() / 60) + 1
        missed = []  # list of (due_datetime, job_dict)

        for job in self.jobs:
            if not job.get("enabled", True):
                continue
            if job.get("no_catchup", False):
                continue
            if str(job.get("id") or "") in _catchup_skip_ids():
                continue
            try:
                parts = job["cron"].split()
                if len(parts) != 5:
                    continue
                min_f, hour_f, day_f, month_f, dow_f = parts
            except Exception:
                continue

            # Walk backwards minute-by-minute to find the most recent occurrence.
            most_recent_due = None
            check_dt = window_end.replace(second=0, microsecond=0)
            for _ in range(total_minutes + 1):
                if check_dt < effective_start:
                    break
                cron_dow = (check_dt.weekday() + 1) % 7  # cron Sun=0..Sat=6
                if (self._field_match(min_f, check_dt.minute, 0, 59)
                        and self._field_match(hour_f, check_dt.hour, 0, 23)
                        and self._field_match(day_f, check_dt.day, 1, 31)
                        and self._field_match(month_f, check_dt.month, 1, 12)
                        and self._field_match(dow_f, cron_dow, 0, 6)):
                    most_recent_due = check_dt
                    break
                check_dt -= timedelta(minutes=1)

            if most_recent_due is None:
                continue

            # Skip if the job already ran at or after this occurrence.
            due_str = most_recent_due.strftime("%Y-%m-%d %H:%M")
            last_run_minute = job.get("last_run_minute")
            if last_run_minute and last_run_minute >= due_str:
                continue

            missed.append((most_recent_due, job))
            logger.debug(
                "🔄 Catch-up candidate: %s (due %s, last_run_minute=%s)",
                job.get("id"), due_str, last_run_minute,
            )

        # Sort oldest-first so jobs execute in their natural chronological order.
        missed.sort(key=lambda x: x[0])
        return [j for _, j in missed]

if __name__ == "__main__":
    s = CronScheduler()
    print(s.list_jobs())
