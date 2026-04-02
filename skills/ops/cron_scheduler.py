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

logger = logging.getLogger("CronScheduler")

JOB_FILE = f"{_MAGI_ROOT}/cron_jobs.json"

class CronScheduler:
    def __init__(self):
        self.jobs = []
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

    def _save_jobs(self):
        """Save jobs to JSON file."""
        try:
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
                    
            except Exception as e:
                logger.error(f"Error checking job {job['id']}: {e}")
                continue
        
        if due_jobs:
            self._save_jobs()
            
        return due_jobs

if __name__ == "__main__":
    s = CronScheduler()
    print(s.list_jobs())
