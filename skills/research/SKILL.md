---
name: research
description: Information gathering skills (RSS, GitHub, Searching).
metadata:
  iron_dome: true
  dependencies: [requests, xml.etree]
---

# Research Skills

## 1. RSS Reader (`rss_reader.py`)
- **Capabilities**: Subscribe to feeds, read latest news.
- **Safety**: Uses standard library XML parsing. Read-only.
- **Commands**:
  - `訂閱 <URL>`: Subscribe to a feed.
  - `閱讀新聞`: List latest items from all feeds.

## 2. GitHub Monitor (`github_monitor.py`)
- **Capabilities**: Search repos, view trending.
- **Safety**: Uses public API via `requests`. Read-only.
- **Commands**:
  - `GitHub 趨勢`: Show top projects.
  - `GitHub 搜尋 <query>`: Search repositories.
