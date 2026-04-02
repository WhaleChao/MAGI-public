---
name: memory
description: Long-term vector memory system using RAG. Use when the user wants to remember important information, store facts for later, or recall previously stored context. Handles save and recall operations to the Keeper vector database.
license: MIT
compatibility: Requires connection to Keeper node (MariaDB + vector extensions)
metadata:
  author: MAGI-Federation
  version: "1.0"
  sage: keeper
---

# Memory Skill

This skill provides long-term memory capabilities for the MAGI system using vector embeddings and RAG (Retrieval Augmented Generation).

## Capabilities

- **Save Memory**: Store important information with source tracking
- **Recall Memory**: Query memories using semantic search
- **Context Augmentation**: Inject relevant memories into conversations

## Usage

```python
from skills.memory.mem_bridge import remember, recall

# Save something
remember("The client prefers meetings on Tuesday", source="user_preference")

# Recall later
results = recall("when does the client like to meet?", top_k=3)
```

## Files

- `mem_bridge.py` - Main bridge to vector database
- `vector_store.py` - Vector embedding and storage logic

## 呼叫格式
觸發詞：記住、記憶、搜尋記憶、忘記
參數：action=動作(remember/recall/forget), content=內容, query=搜尋詞(選填)

## 呼叫範例
使用者：記住：蕭仁俊的案件是憲法訴訟
→ 記憶 action=remember content=蕭仁俊的案件是憲法訴訟

使用者：之前蕭仁俊的案件是什麼
→ 記憶 action=recall query=蕭仁俊
