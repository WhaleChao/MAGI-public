---
name: bridge
description: Inter-node communication bridges for MAGI Federation. Provides connectivity to Melchior (GPU/Vision), Balthasar (Summarization), Casper (Decision), and Watcher (Audit). Use when delegating tasks to specialized nodes.
license: MIT
compatibility: Requires Tailscale VPN connection to federation nodes
metadata:
  author: MAGI-Federation
  version: "1.0"
  sage: all
allowed-tools:
  - melchior_bridge
  - balthasar_bridge
  - casper_bridge
  - watcher_bridge
---

# Bridge Skill

Communication bridges connecting MAGI Federation nodes.

## Node Bridges

| Bridge | Node | Capabilities |
|--------|------|--------------|
| `melchior_bridge` | MELCHIOR (Windows GPU) | Vision analysis, code generation, Iron Dome search |
| `balthasar_bridge` | BALTHASAR (Mac Mobile) | Text summarization |
| `casper_bridge` | CASPER (Mac Mini M4) | LLM inference, decision making |
| `watcher_bridge` | WATCHER (MacBook Air M1) | Audit log collection, anomaly detection |

## Usage

```python
from skills.bridge.melchior_bridge import analyze_image, melchior_search
from skills.bridge.balthasar_bridge import summarize_text
from skills.bridge.watcher_bridge import get_watcher_status

# Analyze image via Melchior GPU
description = analyze_image("/path/to/image.jpg", "What is in this image?")

# Search with Iron Dome filtering
results = melchior_search("legal precedents Taiwan")

# Summarize text via Balthasar
summary = summarize_text(long_text)
```

## Files

- `melchior_bridge.py` - Melchior GPU node connection
- `balthasar_bridge.py` - Balthasar summarization
- `casper_bridge.py` - Casper LLM interface
- `watcher_bridge.py` - Watcher audit node
- `intention_classifier.py` - Intent classification
- `iron_dome.py` - Security filtering
