---
name: brain_manager
description: Control System for Casper's Distributed Brain Architecture. Manages switching between Local (20B) and Distributed (70B/GLM-4) modes, and delegating engineering tasks to Melchior.
author: CASPER
created: 2026-02-09
---

# Brain Manager (Cortex Control)

System for dynamic intelligence scaling, allowing Casper to switch between a low-latency local brain (Mac) and a high-intelligence distributed brain (Mac + Windows).

## 🧠 Brain Modes

| Mode | Model | Hardware | Use Case |
| :--- | :--- | :--- | :--- |
| **Distributed** (Default) | **70B / GLM-4** | Mac (Router) + Win (GPU) | Deep thinking, complex reasoning, creative writing. |
| **Local** | **GPT-OSS 20B** | Mac M4 (Neural Engine) | Fast responses, simple chat, or when Melchior is busy. |

## 🛡️ Safety Protocols (Resource Guard)

The system automatically enforces:
1.  **RAM Check**: Local switch requires >6GB Free RAM (Mac).
2.  **API Check**: Distributed switch requires Melchior API (`MAGI_MELCHIOR_IP:8080`) to be online.
3.  **Cool-down**: 2-3s delay between switches to flush VRAM.

## 🕹️ Instructions (Triggers)

Casper (OpenClaw) should use this skill when the user explicitly requests a mode change or when a task requires specific capabilities.

### 1. Switch to Distributed Brain
**Trigger (Discord/LINE/Web)**: 
- "Activate big brain"
- "Connect to cluster"
- "Switch to distributed mode"
- "Maximum power"
**Action**:
```python
from skills.brain_manager.action import switch_brain_mode
status = switch_brain_mode("distributed")
print(status)
# Output: "Successfully switched to distributed mode. Active API: http://MAGI_MELCHIOR_IP:8080/v1"
```

### 2. Switch to Local Brain
**Trigger (Discord/LINE/Web)**: 
- "Disconnect"
- "Go local"
- "Release the engineer"
- "Work independently"
- "Switch to 20B"
**Action**:
```python
from skills.brain_manager.action import switch_brain_mode
status = switch_brain_mode("local")
print(status)
# Output: "Successfully switched to local mode. Active API: http://localhost:8080/v1"
```

### 3. Delegate Task to Engineer (Melchior)
**Trigger**: "Have Melchior write a script", "Ask the engineer to...", "Run this code on Windows".
**Action**:
```python
from skills.brain_manager.action import delegate_task
response = delegate_task(
    instruction="Write a Python script to analyze this data...",
    context="User wants to find trends in the CSV file..."
)
print(response)
```
*Note: This automatically handles the [Switch Local] -> [Delegate] -> [Switch Distributed] workflow.*

### 4. Report Status
**Trigger**: "Which brain are you using?", "Report status", "Are you in big brain mode?", "Status check".
**Action**:
```python
from skills.brain_manager.action import get_brain_status
status = get_brain_status()
print(status)
```

## 📂 Implementation Details

-   **Controller**: `skills/brain_manager/action.py`
-   **RPC Binary**: `~/Desktop/MAGI_v2/bin/rpc-server`
-   **Startup Script**: `~/Desktop/MAGI_v2/start_rpc.sh`
-   **Remote API**: `http://MAGI_MELCHIOR_IP:8080/v1`
