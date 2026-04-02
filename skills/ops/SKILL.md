---
name: ops
description: System operations, monitoring, and file management. Use when the user asks about system status, CPU/RAM usage, file search, or service health checks. All operations are read-only and sandboxed.
license: MIT
compatibility: Requires psutil for full monitoring
metadata:
  author: MAGI-Federation
  version: "2.0"
  sage: casper
  iron_dome: true
---

# Ops Skill (系統運維)

System monitoring and file management utilities.

## Capabilities

### System Monitor (based on ClawHub: system-info)
- **System Status**: CPU, RAM, Disk, Network usage
- **Service Health**: Check MAGI service statuses
- **Process List**: Top memory-consuming processes

### File Manager (based on ClawHub: file-management)
- **List Directory**: Browse files with size info
- **Search Files**: Find files by name pattern
- **File Info**: Detailed file metadata + preview
- 🛡️ Sandboxed to allowed paths only

## Usage

```python
from skills.ops.system_monitor import get_system_status, check_service_health
from skills.ops.file_manager import list_directory, search_files, file_info

# System status
print(get_system_status())

# Service health
print(check_service_health())

# File operations (sandboxed)
print(list_directory("/Users/ai/Desktop/MAGI"))
print(search_files("/Users/ai/Desktop/MAGI", "orchestrator"))
```

## Files

- `system_monitor.py` - CPU/RAM/Disk/Network monitoring
- `file_manager.py` - Sandboxed file browsing and search
