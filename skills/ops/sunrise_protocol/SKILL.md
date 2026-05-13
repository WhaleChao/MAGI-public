---
name: Sunrise Protocol
description: Automates the transition of the MAGI system from Night Talk (Local Mode) to Distributed Mode (Day Mode).
---

# Sunrise Protocol Skill

This skill allows the agent to manually or automatically trigger the "Sunrise Protocol", which restores the MAGI system to its high-performance Distributed Mode after the nightly maintenance cycle.

## Usage

### 1. Execute Sunrise Protocol
To execute the protocol, run the python wrapper which calls the core logic.

```python
from skills.magi.sunrise import execute_sunrise_protocol
print(execute_sunrise_protocol())
```

### 2. Verification
The protocol returns a markdown report. success is indicated by "System is now in Distributed Mode".

## System Context
- **Night Mode**: Local 20B Model (Casper) + Ollama (Melchior). Used for "Night Talk".
- **Day Mode**: Distributed 70B Model. Used for heavy lifting.
- **Automation**: This skill is scheduled in `crontab` to run at 06:00 AM daily.
