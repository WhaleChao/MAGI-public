---
name: bilingual-docx
description: |
  將 PDF 學術/法律文獻翻譯為繁體中文，並生成中英對照 Word 文件（雙欄表格，橫向 A4）。
  適用於：使用者上傳 PDF 並要求「翻譯」「中英對照」「雙語」「bilingual」時；
  使用者提到「對照表」「並排翻譯」「side-by-side」「landscape」「橫向」時；
  使用者要求將英文學術文獻、法律文件、國際公約評釋翻譯為繁體中文時；
  使用者提供參考翻譯檔並要求繼續翻譯或品質改善時。
  即使使用者只說「幫我翻這份 PDF」，只要涉及學術/法律文件且目標語言為繁體中文，就應觸發本技能。
  MAGI 與 Cowork 均可使用。
---

# 雙語對照文件生成技能（Bilingual Side-by-Side DOCX）

## 任務概要

將英文學術或法律文獻翻譯為繁體中文，產出橫向 A4 雙欄表格 Word 文件：左欄英文原文、右欄繁體中文翻譯。

---

## 完整流程

### 第一步：取得原文（源文提取與驗證）

這一步至關重要。**永遠不要直接從 PDF 提取文字。** 過去的慘痛教訓是：pdftotext 會靜默丟失大量內容（腳註、多層縮排引文、跨頁段落、目錄與正文混淆），導致翻譯的英文源頭本身就是殘缺的，從而產生大面積漏翻。

**標準流程：PDF → Word → 純文字**

無論使用者提供的是 PDF 還是 Word 檔，一律經過 Word 格式再提取：

1. **如果使用者提供 `.docx` 原檔**——直接用 pandoc 提取：
   ```bash
   pandoc source.docx -t plain --wrap=none > source.txt
   ```

2. **如果使用者只提供 PDF**——先轉為 Word，再提取：
   ```bash
   # 第一步：PDF → Word（使用 LibreOffice）
   python scripts/office/soffice.py --headless --convert-to docx source.pdf
   # 產出 source.docx

   # 第二步：Word → 純文字（使用 pandoc）
   pandoc source.docx -t plain --wrap=none > source.txt
   ```
   這個兩步流程遠勝直接用 pdftotext，因為 LibreOffice 在轉換時會重建文件結構（段落、標題、腳註），pandoc 再從結構化的 Word 檔提取，保真度極高。

3. **絕對禁止**：不要使用 `pdftotext`、`pdftohtml`、或任何直接從 PDF 提取純文字的工具。這些工具無法正確處理：
   - 跨頁段落（會在換頁處截斷）
   - 多欄排版（左右欄文字會交錯）
   - 腳註（可能被丟失或插入正文中間）
   - 目錄（可能與正文內容混淆，曾導致 [9.25] 內容被目錄覆蓋）

**源文完整性驗證（不可跳過）：**

提取完文字後，必須執行以下驗證。這不是可選步驟——跳過這步曾導致 93/192 段內容被截斷而不自知：

1. **段落數驗證**：計算提取出的段落標記數量（如 `[9.01]`... `[9.192]`），與預期總數比對。如有缺漏，立即停止並回報。
2. **內容長度驗證**：對照原始文件的頁數估算合理字數。如果提取文字遠少於預期（例如每頁平均不足 500 字元），代表有大量內容遺失。
3. **抽樣比對**：隨機挑選 5 個段落，將提取文字與原始文件目視比對。特別留意：腳註是否被截斷、引文區塊是否完整、段落結尾是否突然中斷。

如果驗證發現問題，向使用者回報。不要在已知殘缺的源文上開始翻譯。

### 第二步：規劃分段策略

根據文件長度決定平行處理策略：

| 總段落數 | 建議分段 | 說明 |
|---------|---------|------|
| < 30 | 單一處理 | 直接翻譯 |
| 30–100 | 2 路平行 | 各代理約 50 段 |
| 100–200 | 4 路平行 | 各代理約 50 段 |
| > 200 | 4–6 路平行 | 各代理約 40–50 段 |

每個平行子代理（subagent）負責一個連續的段落範圍。

### 第三步：翻譯（平行子代理）

每個子代理收到：
- 原文文字檔路徑
- 負責的段落範圍（如 `[9.49]` 至 `[9.96]`）
- 翻譯風格指引（見下方「翻譯風格規範」）
- 輸出檔路徑（如 `items_a.js`）

子代理產出 JavaScript 模組，匯出物件陣列：

```javascript
module.exports = [
  { type: 'p',  // 'p' | 'h1' | 'h2' | 'h3' | 'quote' | 'title' | 'authors' | 'cite'
    en: '[9.49] The English original text...',
    zh: '[9.49] 繁體中文翻譯...' },
  // ...
];
```

### 第四步：三層品質稽核

翻譯完成後，必須通過三層稽核才能進入組建階段。使用 `scripts/audit.js`：

```bash
node scripts/audit.js items_a.js items_b.js items_c.js items_d.js
```

**第一層：形式檢查（基本門檻）**
1. 段落覆蓋率：所有段落均有對應翻譯，無遺漏
2. 長度比例：zh/en < 0.2 且 en > 200 字 → 摘要式翻譯，不合格
3. CJK 比例：zh 中拉丁字母多於 CJK 字元 → 未翻譯英文殘留

**第二層：語意核對（防止意思翻反）**

這是過去最嚴重的錯誤來源。LLM 在批量翻譯時，容易在從句中漏掉否定詞（not / no / never / without / nor），導致翻譯與原文意思完全相反。

稽核腳本會自動檢查：
- 英文含有否定結構（did not / not / no / never / without / nor / neither / fails to / unable）但中文缺乏對應否定詞（不 / 未 / 無 / 沒 / 非 / 否 / 均不 / 並不）→ 標記為 `NEGATION_MISMATCH`
- 英文不含否定但中文含有強否定詞 → 標記為 `FALSE_NEGATION`

任何被標記的段落都必須人工核對或重新翻譯。**特別留意短段落**（en < 200 字），這些是否定詞遺漏的高發區。

**第三層：術語一致性**

平行翻譯的固有問題是：不同子代理對同一術語的翻譯不一致。稽核腳本會檢查：
- 同一英文術語在不同段落的中文翻譯是否一致
- 是否存在已知的錯誤譯法（見 `scripts/normalize.js` 中的術語對照表）

### 第五步：術語正規化

在組建文件之前，必須執行術語正規化。使用 `scripts/normalize.js`：

```bash
# 預覽模式（只顯示會被修改的內容，不實際修改）
node scripts/normalize.js --preview items_a.js items_b.js ...

# 執行模式（在 build 時自動呼叫）
# normalize.js 匯出 normalizeZh(text) 函數，供 build.js 使用
```

正規化處理包含：
1. **術語統一**：根據可配置的對照表，統一全稿術語用字
2. **案名格式**：將 h3 標題中的案名格式統一為 `Name訴Country案（ORIGINAL v COUNTRY, case#）`
3. **全大寫案名轉換**：zh 欄位中的全大寫英文案名轉為 Title Case

術語對照表位於 `scripts/normalize.js` 頂部，可依專案需求擴充。每個新專案開始前，應根據文件領域調整對照表。

### 第六步：填補與重試

如有缺漏或品質不佳的段落：
1. 再次以平行子代理重新翻譯問題段落
2. 新產出的檔案加入建構流程，使用**品質分數**去重：

```
qualityScore 優先級：
  __human（使用者提供的參考翻譯）  → +10,000,000
  __full（從 Word 原檔重譯）       → +8,000,000
  __retry3（語意稽核後重譯）       → +5,000,000
  __retry（第一輪重試）             → +500,000
  非 placeholder                    → +100,000
  + zh.length + en.length/10
```

同一段落編號取最高分者。

### 第七步：組建 Word 文件

使用 `scripts/build_template.js`（或直接以 `docx` npm 套件生成）。組建流程中必須：
1. 載入所有 items 檔案並去重
2. **呼叫 normalize.js 的正規化函數**處理所有 zh 欄位
3. 生成 Word 文件

關鍵設定：

```javascript
const { Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  AlignmentType, PageOrientation, WidthType, BorderStyle, ShadingType,
  VerticalAlign } = require('docx');

// 橫向 A4
page: {
  size: { width: 11906, height: 16838, orientation: PageOrientation.LANDSCAPE },
  margin: { top: 720, right: 720, bottom: 720, left: 720 },
}

// 雙欄等寬
const COL = 7600; // DXA
columnWidths: [COL, COL]

// 字型
const enFont = 'Times New Roman';
const zhFont = { name: 'Times New Roman', eastAsia: 'PMingLiU' };
```

版面設計：
- 標題列（`title`）：跨兩欄、置中、淺藍底色 `EAF3FB`
- 表頭：`English (Original)` / `繁體中文（翻譯）`、底色 `D5E8F0`
- `h1`：粗體、底色 `D5E8F0`
- `h2`：粗體、底色 `EAF3FB`
- `h3`：粗體、底色 `F4F8FB`
- `p`（正文）：左欄 24pt、右欄 22pt
- `quote`（引文）：斜體、左縮排 360
- `note`（譯者附記）：跨兩欄、底色 `FFF4E0`

### 第八步：最終驗證與交付

```bash
python scripts/office/validate.py output.docx
```

驗證通過後，將最終檔案存至使用者桌面，並以 `computer://` 連結提供。

---

## 翻譯風格規範

這是整個技能中最關鍵的部分。翻譯品質直接決定最終成品的價值。

### 核心原則

1. **完整翻譯，不可摘要**：每一段都必須完整翻譯，不得以摘要或概述替代。翻譯後的中文長度應為英文的 30%–60%（因為中文較為精簡，但不應過短）。

2. **法律專業術語標註**：首次出現的專業術語，使用粗體標註中文並附上英文原文：
   - `**酷刑**（torture）`
   - `**不可減損的權利**（non-derogable right）`
   - `**應盡注意義務**（due diligence）`

3. **案例名稱格式**：案名翻譯為中文，括號內保留完整英文原名和案號：
   - `**Alzery 訴瑞典案**（Alzery v Sweden, 1416/05）`
   - `***Dzemajl* 等人訴南斯拉夫案**（Dzemajl et al. v Yugoslavia, CAT 161/00）`

4. **機構/組織名稱**：首次出現時附上英文全稱：
   - `**人權事務委員會**（Human Rights Committee, HRC）`——注意是「人權**事務**委員會」，不是「人權委員會」
   - `**禁止酷刑委員會**（CAT Committee）`

5. **條約/文件名稱**：
   - `《公民與政治權利國際公約》（ICCPR）`
   - `《禁止酷刑公約》（CAT）`

6. **引文段落**：條約條文、委員會意見原文以 `quote` type 呈現，保持引文格式。

7. **Inline markup**：使用 `**粗體**` 和 `*斜體*` 標記，build 時會解析為 Word 格式。

### 絕對禁止的錯誤

以下錯誤曾在實際案例中發生，每一條都基於真實的慘痛教訓：

- ❌ **漏掉否定詞**：英文 "did not breach" 翻成「構成違反」——意思完全相反。翻譯任何含有 not / no / never / without 的句子時，必須逐字確認否定詞已出現在中文中。
- ❌ **zh 欄位只放中文標題然後直接接英文原文**（如：`**調查義務** (Duty to Investigate): The corresponding duty in CAT is...`）
- ❌ **zh 欄位用「待翻譯」placeholder**
- ❌ **只翻譯前幾句就截斷**
- ❌ **把案例引文的段落號碼（¶3.7, ¶6.5 等）直接略過不翻**
- ❌ **同一份文件中對同一術語使用不同翻譯**（如 degrading treatment 有時譯「有辱人格之待遇」有時譯「侮辱性待遇」）
- ❌ **「人權事務委員會」寫成「人權委員會」**——缺了「事務」二字是台灣法律翻譯中的常見錯誤

### 術語一致性要求

每個專案翻譯開始前，應先建立該專案的術語表。以下是國際人權法領域的常見標準譯法（可根據專案領域調整）：

| 英文 | 標準譯法 |
|------|---------|
| Human Rights Committee | 人權事務委員會 |
| degrading treatment or punishment | 有辱人格之待遇或處罰 |
| acquiescence | 默許 |
| ill-treatment | 虐待 |
| solitary confinement | 單獨監禁 |
| detention incommunicado | 與外界隔絕之拘禁 |
| lawful sanctions rider | 合法制裁但書 |
| State party | 締約國 |

### 品質自檢

翻譯完每個段落後，自我檢查：
- zh 裡是否有超過一行的連續英文？→ 如果有，那段沒翻完
- zh 長度是否不到 en 的 20%？→ 可能是摘要而非翻譯
- 術語有標註嗎？→ 重要法律概念需要中英對照
- **原文有否定詞嗎？中文對應的否定有出現嗎？**→ 這是最容易犯也最致命的錯誤

---

## 處理使用者提供的參考翻譯

如果使用者上傳了已有部分翻譯的參考檔案（如 `.md` 檔案）：

1. 先解析該檔案，提取已翻譯的段落
2. 將其存為 `items_human.js`，標記 `__human = true`
3. 在品質分數中給予最高優先級（+10,000,000）
4. 從參考翻譯結束處繼續翻譯

這確保使用者自己校正過的翻譯永遠不會被機器翻譯覆蓋。

---

## 處理不同來源格式

**所有格式最終都經 pandoc 從 Word 提取純文字——這是唯一認可的提取路徑。**

| 使用者提供 | 處理流程 |
|-----------|---------|
| `.docx` 原檔 | 直接 `pandoc source.docx -t plain --wrap=none` |
| `.pdf` 檔案 | 先 `soffice --convert-to docx`，再 pandoc 提取 |
| `.doc`（舊格式） | 先 `soffice --convert-to docx`，再 pandoc 提取 |
| `.epub` / `.odt` | 先 `soffice --convert-to docx`，再 pandoc 提取 |

如果翻譯已進行一半才收到更好的原檔版本，需要：
- 比對新版本與舊版本各段落的字數差異
- 對所有內容明顯不同的段落重新翻譯
- 重新翻譯的結果標記為 `__full = true`，品質分數 +8,000,000

---

## 檔案結構

一個完整的翻譯專案通常包含：

```
project/
├── source.pdf          # 使用者提供的 PDF（如有）
├── source.docx         # Word 格式（使用者提供或從 PDF 轉換）
├── source.txt          # pandoc 從 Word 提取的純文字（唯一認可的提取路徑）
├── source_validation.log # 源文完整性驗證結果
├── items_a.js          # 子代理 A 的翻譯結果
├── items_b.js          # 子代理 B 的翻譯結果
├── items_c.js          # ...
├── items_d.js
├── items_human.js      # 使用者參考翻譯（如有）
├── items_full_*.js     # 從 Word 原檔重譯（如有）
├── items_retry_*.js    # 重試翻譯
├── audit.js            # 品質稽核腳本
├── normalize.js        # 術語正規化腳本
└── build.js            # 文件建構腳本
```

---

## 附帶腳本

本技能包含以下腳本（位於 `scripts/` 目錄）：

- **`audit.js`**：三層品質稽核工具——形式檢查、語意核對（否定詞一致性）、術語一致性
- **`normalize.js`**：術語正規化工具——統一全稿術語用字和案名格式
- **`build_template.js`**：文件建構範本，整合正規化步驟
- **`parse_reference.js`**：解析使用者提供的參考翻譯 .md 檔

使用前確保已安裝 `docx` 套件：`npm install -g docx`

---

## 給子代理的 Prompt 範本

當需要派出平行翻譯代理時，使用以下範本：

```
你是一個專業的法律/學術翻譯代理。請閱讀以下英文原文檔案，翻譯段落 [X.YY] 至 [X.ZZ]。

原文路徑：{source_path}
輸出路徑：{output_path}

翻譯要求：
1. 每段完整翻譯為繁體中文，不可摘要
2. 法律術語首次出現時加粗並附英文：**酷刑**（torture）
3. 案名翻譯格式：**Alzery 訴瑞典案**（Alzery v Sweden, 1416/05）
4. 機構統一用詞：「人權事務委員會」（不是「人權委員會」）、「締約國」（不是「國家當事人」）
5. 條約名稱：《公民與政治權利國際公約》（ICCPR）
6. 輸出為 JavaScript module.exports = [...] 格式
7. type 欄位：h1/h2/h3 用於標題，p 用於正文，quote 用於引文

品質紅線（違反任何一條即不合格）：
- zh 長度應為 en 的 30%–60%
- zh 中不應有超過一行的連續英文
- 不得使用 placeholder 如「待翻譯」
- 英文有否定詞（not/no/never/without）時，中文必須有對應否定（不/未/無/沒/非/並不）——漏掉否定詞會導致意思完全相反
- 全稿同一術語必須使用一致的譯法
```
