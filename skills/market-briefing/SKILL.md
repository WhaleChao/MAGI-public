---
name: market-briefing
description: 每日 08:30 台股/美股追蹤預測技能，含同業比較、產業分析、Excel 報表輸出。
---

# market-briefing

每日 08:30 台股/美股追蹤預測技能，含同業比較、產業分析、Excel 報表輸出。

## 能力
- 第一次啟用時先詢問要追蹤的股票清單。
- 設定追蹤後，隔天開始自動晨報。
- 支援自然語句新增/移除/查看追蹤清單。
- 預測資料來源：
  - 價格：Yahoo Finance chart API
  - 台股財報訊號：TWSE OpenAPI（營收/季報欄位）
  - 美股財報訊號：SEC submissions（10-K/10-Q/8-K 最新申報）
  - 新聞情緒：Google News RSS 搜尋標題（短期快取，含來源/日期/URL；無標題時情緒分析降級為資料不足）

## 任務
- `--task prompt`：主動詢問使用者追蹤股票
- `--task list`：列出目前清單
- `--task set --text "台積電、AAPL"`：覆蓋追蹤清單
- `--task add --text "MSFT"`：新增追蹤
- `--task remove --text "AAPL"`：移除追蹤
- `--task briefing [--notify 1] [--force 1] [--mode deep|technical|quick]`：產生晨報（預設深度模式）
- `--task performance`：查看模型近期命中率/MAE/權重與自動校準狀態
- `--task backtest`：回測模型參數，自動選擇最佳權重
- `--task comps --text "台積電"`：同業比較分析（自動選取同產業公司）
- `--task sector --text "半導體"`：產業板塊分析
- `--task export [--mode deep|technical|quick]`：匯出追蹤清單分析為 Excel (.xlsx)

## 分析模式
| 模式 | 內容 |
|------|------|
| **quick** | EMA 趨勢、動量、波動率、財報訊號 |
| **technical** | quick + RSI(14)、MACD、BBands |
| **deep** | technical + 支撐/阻力位、成交量趨勢、ADX 趨勢強度 + 有來源標題的委員會分析 |

## 同業比較分析方法論 (Comps Analysis)

同業比較（Comparable Company Analysis）透過相似公司的市場估值指標進行相對估值：

### 核心指標
| 指標 | 計算方式 | 用途 |
|------|----------|------|
| **P/E (本益比)** | 股價 ÷ EPS | 盈利估值 — 同業 P/E 中位數可判斷高估/低估 |
| **P/B (股價淨值比)** | 股價 ÷ 每股淨值 | 資產估值 — 適用金融/資產密集型產業 |
| **營收 YoY%** | (本月營收 - 去年同月) ÷ 去年同月 | 成長動能比較 |
| **EPS 成長率** | (本季 EPS - 去年同季) ÷ 去年同季 | 獲利動能比較 |
| **殖利率** | 年度股息 ÷ 股價 | 現金流回報比較 |

### 分析流程
1. **選定目標公司**及其所屬產業
2. **自動篩選同業**：同 TSE 產業分類、市值規模相近
3. **拉取指標**：Yahoo Finance + TWSE OpenAPI
4. **計算中位數 / 平均數**：標記高於或低於同業均值
5. **產出比較表**：含排名、色碼標記、估值結論

## 產業分析方法論 (Sector Analysis)

### 分析架構
1. **產業概覽**：成分股數量、總市值、近期均漲跌幅
2. **龍頭股表現**：市值前 5 大公司的近期走勢
3. **技術面共識**：多數成分股的 RSI/MACD 方向是否一致
4. **資金流向**：成交量趨勢是集中於龍頭或擴散至中小型
5. **催化劑**：近期財報季、法規變動、國際事件

### 台股產業分類
水泥、食品、塑膠、紡織、電機、電器電纜、化學、玻璃、造紙、鋼鐵、橡膠、
汽車、電子（半導體/光電/通信/電子零組件/電腦周邊/資訊服務/其他電子）、
建材、航運、觀光、金融保險、貿易百貨、油電燃氣、其他

## 財報分析方法論 (Earnings Analysis)

當公司公布財報時，應從以下維度分析：

1. **營收 vs 預期**：是否超越/低於市場共識？差距幅度？
2. **獲利品質**：毛利率、營業利益率的趨勢（擴張/收縮）
3. **前瞻指引**：管理層對下一季/年的展望是否調升/調降
4. **關鍵 KPI**：依產業不同
   - 半導體：產能利用率、先進製程營收占比、資本支出
   - 金融：淨利差(NIM)、逾放比、手續費收入
   - 零售：同店營收成長、庫存周轉天數
5. **估值影響**：財報後合理 P/E 區間是否需調整

## 通訊軟體切換
- 「快速模式」「簡報」「quick」→ quick
- 「技術分析」「MACD」「RSI」「布林通道」→ technical
- 「同業比較」「comps」→ comps
- 「產業分析」「sector」「板塊」→ sector
- 其他 / 預設 → deep

## 狀態檔
- `/Users/ai/Desktop/MAGI_v2/.agent/market_watchlist.json`
- `/Users/ai/Desktop/MAGI_v2/.agent/market_data_cache.json`
- `/Users/ai/Desktop/MAGI_v2/.agent/market_news_cache.json`
- `/Users/ai/Desktop/MAGI_v2/.agent/market_perf_history.json`

## 呼叫格式
觸發詞：股票、股市、晨報、追蹤
參數：action=動作(briefing/add/remove/list), symbol=股票代號(選填)

## 呼叫範例
使用者：今天股市怎麼樣
→ 股市 action=briefing

使用者：追蹤台積電
→ 股市 action=add symbol=2330

使用者：目前追蹤清單
→ 股市 action=list
