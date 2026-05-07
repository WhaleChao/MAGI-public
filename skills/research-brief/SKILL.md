---
name: research-brief
description: 學術／人權／通譯／族群／語言政策多語文獻與新聞日報爬蟲；可擴充命名空間。抓取 → 關鍵字過濾 → 翻譯（Apple→NIM fallback）→ 繁中摘要 → Discord 專屬頻道推播。
---

# research-brief

## 命名空間（user-extensible query namespace）

可擴充的資料來源分類；每個命名空間有獨立的來源清單、關鍵字清單、以及可選的 Discord 子頻道。

**預設命名空間**（可自行增刪）：

- `通譯` — 司法通譯、法庭口譯、法律通譯倫理
- `族群人類學` — 族群、原住民族、人類學、文化研究
- `人權公約` — 兩公約、UN 公約、區域人權機制、NGO 報告
- `語言政策` — 語言權、少數語言、瀕危語言、多元文化政策
- `東亞法學與語言` — 日／韓／港 法學、語言學、人權相關（不含中國大陸官方來源，除非手動加入）

## 指令

```
研究爬蟲 清單                                列出所有命名空間
研究爬蟲 清單 <namespace>                    列出命名空間內所有來源
研究爬蟲 新增 <namespace> <url>              加入來源（可選 note=xxx, lang=ja, type=rss|html|json）
研究爬蟲 移除 <namespace> <url>              移除來源（不刪除已抓取的向量資料）
研究爬蟲 新增命名空間 <name>                 建立新命名空間
研究爬蟲 移除命名空間 <name>                 刪除空命名空間（內有來源時拒絕）
研究爬蟲 關鍵字 <namespace> 新增 <kw>        加入過濾關鍵字（zh 或 en 皆可）
研究爬蟲 關鍵字 <namespace> 移除 <kw>
研究爬蟲 今日摘要                            手動觸發當日摘要（跨命名空間）
研究爬蟲 今日摘要 <namespace>                只觸發單一命名空間摘要
研究爬蟲 查詢 <namespace> "<keyword>"        即席搜尋已抓取內容
self_test                                    健康檢查
```

## CLI 任務

```bash
python3 action.py --task list
python3 action.py --task list_namespace --namespace 通譯
python3 action.py --task add_source --namespace 人權公約 --url "https://..." --lang en --type rss
python3 action.py --task remove_source --namespace 人權公約 --url "https://..."
python3 action.py --task add_namespace --namespace 歐洲人權
python3 action.py --task remove_namespace --namespace 歐洲人權
python3 action.py --task add_keyword --namespace 通譯 --keyword "court interpreter"
python3 action.py --task remove_keyword --namespace 通譯 --keyword "court interpreter"
python3 action.py --task fetch --namespace 人權公約   # 只抓，不摘要
python3 action.py --task digest --namespace 人權公約  # 抓 + 摘要 + 通知
python3 action.py --task digest_all                   # 所有命名空間
python3 action.py --task query --namespace 通譯 --keyword "死刑"
python3 action.py --task self_test
```

## 摘要格式（嚴格遵循）

```
**<繁中標題> / <original title>**
<繁中摘要 120 字內>
🏷 <tag1> <tag2> <tag3>
🔗 <原文連結> · <lang> · <source>
```

## 翻譯管線

- 原文語言為 zh/zh-Hant → 不翻
- 其他語言（en/ja/ko/de/fr/es/ru/ar/th/vi/it/pt/nl/pl/tr 等）→ Apple Translation sidecar 翻成 zh-Hant
- Apple Translation 失敗 → NIM 405B fallback（degraded=true，標於摘要末）

## 狀態檔

- `.runtime/research_brief/namespaces/<name>.json` — 使用者可編輯的來源/關鍵字清單
- `.runtime/research_brief/seen.json` — 已推播 URL 雜湊（dedupe）
- `.runtime/research_brief/last_digest.jsonl` — 歷史摘要紀錄

## Discord 路由

| topic_key | 頻道名稱 | 說明 |
|-----------|---------|------|
| `research_daily` | `研究-每日摘要` | 所有命名空間合併摘要（07:00） |
| `research_interpretation` | `研究-通譯` | `通譯` 命名空間 |
| `research_ethno` | `研究-族群人類學` | `族群人類學` 命名空間 |
| `research_humanrights` | `研究-人權公約` | `人權公約` 命名空間 |
| `research_language` | `研究-語言政策` | `語言政策` 命名空間 |
| `research_eastasia` | `研究-東亞` | `東亞法學與語言` 命名空間 |

Category: `📚 研究通訊`

## 呼叫格式

觸發詞：研究爬蟲、研究來源、研究命名空間、研究摘要、研究關鍵字
