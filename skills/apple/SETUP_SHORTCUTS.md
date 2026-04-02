# Apple AI Shortcuts 設定指南

請在 Mac 上的「捷徑」App 中建立以下捷徑，讓 CASPER 能夠使用 Apple 的裝置端能力與 Apple Intelligence（若你已啟用）來協作。

最低需求（舊版 4 個）可以做：PDF 取字、OCR、螢幕掃描、錄音聽寫。
若你希望「摘要」與「音檔轉逐字稿（檔案）」也走 Apple Intelligence，請一併建立新增的 2 個捷徑。

---

## 1. MAGI 語音辨識 (Speech-to-Text)

**功能**：錄音並轉換為文字

**步驟**：
1. 開啟「捷徑」App → 新增捷徑
2. 命名為：`MAGI 語音辨識`
3. 加入動作：
   ```
   📥 取得捷徑輸入 (秒數)
   🎤 錄製音訊 (時間長度 = 捷徑輸入)
   📝 聽寫文字 (語言: 繁體中文)
   📤 輸出結果
   ```

**動作順序**：
| # | 動作 | 設定 |
|---|---|---|
| 1 | 取得捷徑輸入 | - |
| 2 | 錄製音訊 | 時間長度 = 捷徑輸入 秒 |
| 3 | Dictate Text / 聽寫文字 | 語言 = 繁體中文 |
| 4 | 停止並輸出 | 輸出 = 聽寫結果 |

---

## 2. MAGI 讀取 PDF (PDF Text Extraction)

**功能**：從 PDF 檔案擷取文字

**步驟**：
1. 新增捷徑，命名為：`MAGI 讀取 PDF`
2. 加入動作：
   ```
   📥 取得捷徑輸入 (檔案路徑)
   📄 取得 PDF 的文字
   📤 輸出結果
   ```

**動作順序**：
| # | 動作 | 設定 |
|---|---|---|
| 1 | 取得捷徑輸入 | 類型 = 檔案 |
| 2 | Make PDF / 製作 PDF | (如果輸入不是 PDF) |
| 3 | Get Text from PDF | - |
| 4 | 停止並輸出 | 輸出 = PDF 文字 |

---

## 3. MAGI OCR 掃描 (Image OCR)

**功能**：從圖片中辨識文字 (Live Text)

**步驟**：
1. 新增捷徑，命名為：`MAGI OCR 掃描`
2. 加入動作：
   ```
   📥 取得捷徑輸入 (圖片路徑)
   🔍 Extract Text from Image / 從影像擷取文字
   📤 輸出結果
   ```

**動作順序**：
| # | 動作 | 設定 |
|---|---|---|
| 1 | 取得捷徑輸入 | 類型 = 影像 |
| 2 | Extract Text from Image | (Vision 框架，支援中英文) |
| 3 | 停止並輸出 | 輸出 = 辨識文字 |

---

## 4. MAGI 螢幕掃描 (Screenshot + OCR)

**功能**：擷取螢幕畫面並辨識文字

**步驟**：
1. 新增捷徑，命名為：`MAGI 螢幕掃描`
2. 加入動作：
   ```
   📸 擷取螢幕截圖
   🔍 Extract Text from Image
   📤 輸出結果
   ```

---

## 7. MAGI GoodNotes 建立資料夾 (Create Folder)

**功能**：在 GoodNotes 中建立新資料夾（用於同步案件結構）。

**步驟**：
1. 新增捷徑，命名為：`MAGI GoodNotes 建立資料夾`
2. 加入動作：
   ```
   📥 取得捷徑輸入（資料夾名稱）
   📂 GoodNotes: Create new folder（名稱 = 捷徑輸入）
   📤 輸出結果
   ```

**動作細節**：
- Create new folder: 確保展開參數，名稱設為「捷徑輸入」。

---

## 8. MAGI GoodNotes 匯入文件 (Import Document)

**功能**：將 PDF 匯入到 GoodNotes（注意：預設匯入至根目錄，需手動歸檔或依賴 UI 自動化）。

**步驟**：
1. 新增捷徑，命名為：`MAGI GoodNotes 匯入文件`
2. 加入動作：
   ```
   📥 取得捷徑輸入（檔案路徑）
   📄 GoodNotes: Open document（文件 = 捷徑輸入）
   📤 輸出結果
   ```

**動作細節**：
- Open document: 輸入需為檔案（若捷徑輸入是文字路徑，可能需先用「取得檔案」動作轉換）。

---

## 9. MAGI GoodNotes 搜尋 (Search)

**功能**：在 GoodNotes 中搜尋文件。

**步驟**：
1. 新增捷徑，命名為：`MAGI GoodNotes 搜尋`
2. 加入動作：
   ```
   📥 取得捷徑輸入（搜尋關鍵字）
   🔍 GoodNotes: Search（Query = 捷徑輸入）
   📤 輸出結果
   ```

---

## 更新後的驗證指令

```bash
# 檢查所有捷徑（含 GoodNotes）
python /Users/ai/Desktop/MAGI/skills/apple/apple_ai.py check

# 測試 GoodNotes 同步 (Self Test)
python /Users/ai/Desktop/MAGI/skills/goodnotes-sync/action.py --task self_test
```

**動作順序**：
| # | 動作 | 設定 |
|---|---|---|
| 1 | Take Screenshot | 選取區域 = 是 (互動式) 或 否 (全螢幕) |
| 2 | Extract Text from Image | - |
| 3 | 停止並輸出 | 輸出 = 辨識文字 |

---

## 驗證安裝

建立完成後，在終端機執行：
```bash
python /Users/ai/Desktop/MAGI/skills/apple/apple_ai.py check
```

預期結果：
```json
{
  "ready": true,
  "shortcuts": {
    "MAGI 語音辨識": true,
    "MAGI 讀取 PDF": true,
    "MAGI OCR 掃描": true,
    "MAGI 螢幕掃描": true
  }
}
```

---

## 5. MAGI 摘要（Apple Intelligence / Writing Tools）

**功能**：將輸入文字摘要成重點（建議 5-10 點）

> 這個捷徑用來讓 `/summarize` API 可以「先嘗試 Apple Intelligence」，失敗才回退到 Balthasar。

**步驟**：
1. 新增捷徑，命名為：`MAGI 摘要`
2. 加入動作（概念）：
   ```
   📥 取得捷徑輸入（檔案：.txt）
   📄 取得檔案內容（文字）
   ✍️ 使用寫作工具 / Apple Intelligence：摘要（建議固定選一種，例如「製作重點摘要」）
   📤 輸出結果
   ```

注意：不同 macOS 版本的捷徑動作名稱可能略有差異，重點是「用 Apple Intelligence 做摘要」並把結果輸出。

避免互動視窗（很重要）：
- 你的捷徑裡**不要**出現「要求輸入」「從清單選擇」「顯示選單」這類動作，否則 `shortcuts run` 會卡住需要人手點選。
- 若你的「書寫工具」動作跳出「哪一個？生成摘要 / 製作重點摘要」，代表你尚未把動作參數固定好。請回到捷徑編輯畫面，把它固定成其中一種（建議：製作重點摘要）。

---

## 6. MAGI 音檔轉文字（檔案逐字稿）

**功能**：輸入音檔路徑（或檔案），輸出轉文字結果。

> 用於讓 `/collab/transcribe` 可以優先走 Apple（捷徑具有隱私權限與 UI 授權），避免 Python 直接呼叫 Speech.framework 造成崩潰。

**步驟**：
1. 新增捷徑，命名為：`MAGI 音檔轉文字`
2. 加入動作（概念）：
   ```
   📥 取得捷徑輸入（檔案）
   🎤 轉錄音訊 / 辨識語音（繁體中文）
   📤 輸出結果
   ```

提示：若找不到「轉錄音訊」動作，可用「聽寫文字」搭配音檔輸入（視系統支援而定）。

---

## 使用範例

```bash
# 語音辨識 (錄音 5 秒)
python apple_ai.py stt 5

# 讀取 PDF
python apple_ai.py pdf /path/to/document.pdf

# 圖片 OCR
python apple_ai.py ocr /path/to/image.png

# 螢幕截圖 OCR
python apple_ai.py screen

# 摘要（需建立 MAGI 摘要）
python apple_intelligence.py summarize "這是一段很長的文字..."

# 音檔轉文字（需建立 MAGI 音檔轉文字）
python apple_intelligence.py stt /path/to/audio.aiff
```
