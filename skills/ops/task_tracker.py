
import json
import os
import time
from datetime import datetime

class TaskTracker:
    def __init__(self, task_file="active_tasks.json"):
        # Store in MAGI static folder so frontend can potentially read it seamlessly via heartbeat
        # But heartbeat.py will read this file and append to magi_status.json
        self.task_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "active_tasks.json")
        
    def _load_tasks(self):
        if not os.path.exists(self.task_file):
            return {}
        try:
            with open(self.task_file, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_tasks(self, tasks):
        with open(self.task_file, "w") as f:
            json.dump(tasks, f, indent=2)

    def update_task(self, task_id, name, progress=0, status="Starting...", type="job"):
        """
        Updates (or creates) a task.
        progress: 0-100 (int)
        """
        tasks = self._load_tasks()
        
        tasks[task_id] = {
            "id": task_id,
            "name": name,
            "progress": progress,
            "status": status,
            "type": type,
            "last_update": time.time(),
            "timestamp": datetime.now().isoformat()
        }
        
        self._save_tasks(tasks)
        print(f"✅ [TaskTracker] {name}: {progress}% - {status}")

    def complete_task(self, task_id):
        """
        Removes a task from the active list.
        """
        tasks = self._load_tasks()
        if task_id in tasks:
            del tasks[task_id]
            self._save_tasks(tasks)
            print(f"✅ [TaskTracker] Task {task_id} Completed.")

# Singleton
tracker = TaskTracker()
