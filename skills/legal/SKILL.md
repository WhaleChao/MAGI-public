---
name: legal
description: 法律自動化執行器 — 透過 CLI 執行司法院登入/閱卷 (judicial) 及法扶 (LAF) 自動化任務，含 LLM 自我修復診斷。
license: MIT
compatibility: Python 3.10+, Selenium, config.json
metadata:
  author: MAGI
  version: "1.0"
  sage: keeper
---

# legal

Legacy CLI runner，統一入口執行 judicial（司法院 SSO 登入、電子筆錄調閱、閱卷系統）與 LAF（法扶基金會入口登入、文件下載）兩大子任務。執行失敗時自動呼叫 Melchior 進行錯誤診斷與修復建議。

## Capabilities

- **judicial** — 透過 `LawyerSSO` 登入律師單一登入入口 (portal.ezlawyer.com.tw)，整合電子筆錄調閱與線上閱卷系統
- **laf** — 透過 `LAFWebAutomation` 登入法扶律師線上操作系統 (lawyer.laf.org.tw)，支援 RapidOCR 驗證碼辨識與文件下載
- **doc_analysis** — 使用 LLM 分析法律文件表格內容，識別可替換欄位並回傳對應填充建議
- **self-healing** — 執行失敗時自動將 traceback 傳送至 Melchior，產生修復建議並存檔

## Usage

```bash
python runner.py judicial    # 執行司法院 SSO 登入測試
python runner.py laf         # 執行法扶系統登入測試
```

## Commands

1. `judicial` — 讀取 config.json `judicial` 區塊的帳密，執行 SSO 登入
2. `laf` — 讀取 config.json `laf` 區塊的帳密，執行法扶登入

## Dependencies

- `skills/legal/judicial.py` — 司法院自動化模組 (LawyerSSO, CourtRecordDownloader)
- `skills/legal/laf.py` — 法扶自動化模組 (LAFWebAutomation)
- `skills/legal/doc_analysis.py` — LLM 文件分析
- `skills/bridge/melchior_client` — LLM 診斷介面
- `config.json` — 帳號密碼設定（judicial / laf 區塊）
