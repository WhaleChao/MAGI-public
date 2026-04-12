---
name: doc-producer
label: 書狀製作
version: 1.0.0
description: DOCX→PDF 轉換、正本/副本/繕本標記、PDF 合併
author: MAGI
---

# 書狀製作 (doc-producer)

## 功能
- `convert`: DOCX → PDF 轉換
- `mark`: 在 PDF 加上正本/副本/繕本標記（右上角）
- `merge`: 合併多個 PDF 為一份
- `produce`: 完整流程（轉換 → 標記 → 合併）

## 用法
- `convert {"input": "/path/to/file.docx"}`
- `mark {"input": "/path/to/file.pdf", "copy_type": "正本", "add_poa": true, "add_sent_to_opponent": true}`
- `merge {"inputs": ["/path/a.pdf", "/path/b.pdf"], "output": "/path/merged.pdf"}`
- `produce {"input": "/path/to/file.docx", "copy_type": "正本", "merge_with": ["/path/judgment.pdf"]}`
