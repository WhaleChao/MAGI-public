---
name: screenshot-sorter-tw
description: >-
  對話截圖排序與重新命名工具。將資料夾中大量的通訊軟體對話截圖（LINE、iMessage、
  Messenger、WeChat 等）按照對話時間順序排序，並重新命名檔案使檔名反映正確順序，
  方便依序列印或閱覽。進階功能可在截圖上標註順序編號浮水印。
  觸發關鍵字包括「截圖排序」「對話截圖」「排序截圖」「截圖重新命名」「對話排序」
  「LINE截圖」「對話紀錄截圖」「截圖整理」「截圖編號」「幫我排截圖」
  「按照時間排」「重新命名截圖」或任何涉及將多張通訊對話截圖依時間順序整理的請求。
  也適用於使用者上傳多張截圖或指定截圖所在資料夾後要求排序的情境。
  不適用於單純的圖片格式轉換（應直接用 bash 處理）。
  不適用於非對話截圖的一般圖片排序。
---

# 對話截圖排序與重新命名工具

## 核心任務

接收一個資料夾路徑（或使用者上傳的多張截圖），辨識每張截圖中的對話時間與
上下文脈絡，按照對話發生的時序重新排序，並將檔案重新命名為帶有序號的檔名，
使使用者只要按檔名排列就能得到正確的對話順序。

進階功能：在每張截圖的角落標註順序編號浮水印，讓列印後也能一目了然。

---

## A. 工作流程

### 第零步：確認輸入來源

使用者可能以兩種方式提供截圖：

1. **指定資料夾路徑**：截圖已在電腦中，使用者提供路徑
2. **直接上傳**：截圖出現在 `/mnt/user-data/uploads/`

確認截圖來源後，列出所有圖檔（支援 .png, .jpg, .jpeg, .webp, .heic），
並告知使用者找到幾張截圖，準備開始分析。

```bash
# 列出資料夾中所有圖檔
find "$SOURCE_DIR" -maxdepth 1 -type f \( -iname "*.png" -o -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.webp" \) | sort
```

### 第一步：逐張分析截圖

對每一張截圖，使用 Claude 的視覺能力進行分析。由於 Claude 可以直接看圖片，
利用 Python 腳本將圖片讀入並透過描述來提取資訊。

**但更實際的做法**：使用 Python + OCR 先提取文字，再結合 Claude 的視覺判讀。

#### 分析策略

由於截圖數量可能很多（數十張），採用以下策略：

1. **先用 Python 讀取圖片的 EXIF 資料**（如果有的話），取得拍攝時間
2. **使用 view 工具直接檢視每張圖片**，Claude 可以看到圖片內容
3. **從圖片中辨識**：
   - 日期標記（LINE 會在日期改變時顯示「2024年3月15日 星期五」等）
   - 時間戳記（每則訊息旁的時間，如「下午 2:30」）
   - 對話內容（用於判斷上下文連續性）
   - 最上方和最下方的訊息時間（確定該截圖涵蓋的時間範圍）

#### 每張截圖需記錄的資訊

```
{
  "filename": "原始檔名.png",
  "date": "2024-03-15",          // 從截圖中辨識的日期，null 表示未顯示
  "time_start": "14:30",         // 截圖中最早的訊息時間
  "time_end": "14:45",           // 截圖中最晚的訊息時間
  "first_message_preview": "...", // 最上方訊息的前20字
  "last_message_preview": "...",  // 最下方訊息的前20字
  "context_notes": "...",         // 上下文備註（話題、關鍵詞）
  "confidence": "high|medium|low" // 時間判斷的信心度
}
```

### 第二步：排序

以時間為主要排序依據，上下文為輔助：

1. **主排序**：日期 + 時間（最舊到最新）
2. **次排序**：當時間相同或無法辨識時，根據對話上下文判斷——
   - 上一張的 `last_message_preview` 應該接上下一張的 `first_message_preview`
   - 話題的連續性
3. **無法判斷的截圖**：放在最後，標記為「待確認」

### 第三步：重新命名

排序確定後，將檔案複製（不是移動，保留原檔）到輸出資料夾，
使用以下命名規則：

```
{序號}_{原始檔名}
```

序號格式：
- 100 張以內：三位數零填充（001_, 002_, ...）
- 超過 100 張：依實際數量調整位數

範例：
```
001_IMG_3847.png
002_IMG_3844.png
003_IMG_3850.png
```

這樣使用者在檔案總管中按名稱排序，就是正確的對話順序。

### 第四步（進階，使用者要求時才執行）：標註序號浮水印

在每張截圖的**右上角**加上半透明的順序編號標記，方便列印後辨識。

使用 Python Pillow 實作：

```python
from PIL import Image, ImageDraw, ImageFont
import os

def add_watermark(image_path, output_path, number, total):
    img = Image.open(image_path)
    draw = ImageDraw.Draw(img)
    
    # 根據圖片尺寸決定字體大小（圖片寬度的 5%）
    font_size = max(30, img.width // 20)
    
    # 嘗試載入字體，失敗就用預設
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except:
        font = ImageFont.load_default()
    
    text = f"{number}/{total}"
    
    # 計算文字位置（右上角，留邊距）
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    margin = font_size // 2
    x = img.width - text_width - margin
    y = margin
    
    # 畫半透明背景
    padding = 10
    bg_box = [x - padding, y - padding, x + text_width + padding, y + text_height + padding]
    
    # 建立半透明圖層
    overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rounded_rectangle(bg_box, radius=10, fill=(0, 0, 0, 140))
    
    # 合成
    if img.mode != 'RGBA':
        img = img.convert('RGBA')
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)
    draw.text((x, y), text, fill=(255, 255, 255, 230), font=font)
    
    # 存檔（轉回 RGB 以相容 JPEG）
    if output_path.lower().endswith(('.jpg', '.jpeg')):
        img = img.convert('RGB')
    img.save(output_path)
```

---

## B. 實作腳本

### 主控腳本流程

整個流程用 Python 腳本串接：

```python
#!/usr/bin/env python3
"""
screenshot_sorter.py - 對話截圖排序與重新命名
用法：由 Claude 在分析完截圖後呼叫
"""

import os
import shutil
import json
import glob
from pathlib import Path

def collect_images(source_dir):
    """收集資料夾中所有圖檔"""
    extensions = ('*.png', '*.jpg', '*.jpeg', '*.webp', '*.PNG', '*.JPG', '*.JPEG')
    images = []
    for ext in extensions:
        images.extend(glob.glob(os.path.join(source_dir, ext)))
    return sorted(images)

def rename_and_copy(sorted_files, output_dir, add_watermark=False):
    """按排序結果複製並重新命名檔案"""
    os.makedirs(output_dir, exist_ok=True)
    total = len(sorted_files)
    width = len(str(total))  # 序號位數
    width = max(width, 3)    # 至少三位
    
    manifest = []
    for i, filepath in enumerate(sorted_files, 1):
        original_name = os.path.basename(filepath)
        new_name = f"{str(i).zfill(width)}_{original_name}"
        dest = os.path.join(output_dir, new_name)
        shutil.copy2(filepath, dest)
        manifest.append({
            "order": i,
            "original": original_name,
            "renamed": new_name,
        })
    
    # 輸出對照表
    manifest_path = os.path.join(output_dir, "_排序對照表.json")
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    
    return manifest
```

---

## C. 分批處理策略

截圖數量可能很多，Claude 的 view 工具每次只能看一張圖。採用以下策略：

1. **小量（≤ 15 張）**：逐張用 view 工具查看，直接在對話中完成分析
2. **中量（16-50 張）**：分批查看，每批 5-8 張，分析完一批記錄後再看下一批
3. **大量（> 50 張）**：
   - 先用 EXIF 資料做初步排序
   - 對時間不明確的截圖再用 view 工具輔助判斷
   - 告知使用者分析需要較長時間

每批分析完成後，向使用者回報進度：「已分析 12/45 張，目前進度正常。」

---

## D. 特殊情況處理

### LINE 對話的辨識要點

- **日期分隔線**：LINE 會在日期改變時顯示橫線加日期文字（如「2024年3月15日 星期五」）
- **時間顯示**：每則訊息左側或右側會有時間（如「下午 2:30」）
- **已讀標記**：「已讀」加上時間可輔助判斷
- **同一分鐘的多則訊息**：只有第一則顯示時間，後續訊息不顯示——此時需靠上下文

### iMessage / Messenger / WeChat

- 辨識邏輯類似，但時間格式和位置不同
- Claude 的視覺能力可以適應不同 app 的排版

### 無法辨識時間的截圖

- 標記 confidence 為 "low"
- 嘗試用對話內容的連續性判斷位置
- 最終無法判斷的，在對照表中標記「⚠ 順序待確認」

---

## E. 輸出成果

最終輸出到 `/mnt/user-data/outputs/sorted_screenshots/`：

1. **重新命名的截圖檔案**：`001_xxx.png`, `002_xxx.png`, ...
2. **排序對照表**（`_排序對照表.json`）：包含原始檔名、新檔名、辨識到的時間
3. **（若啟用）帶浮水印版本**：放在子資料夾 `watermarked/`

向使用者呈現時，使用 present_files 工具提供對照表，並說明：
- 共處理了幾張截圖
- 有幾張時間判斷信心度較低
- 如何使用（按檔名排序即為正確順序）

---

## F. 注意事項

- **永遠複製，不移動**：保留使用者的原始檔案不動
- **隱私敏感**：對話內容可能涉及隱私，分析過程中的對話預覽只取前 20 字，
  不在回覆中完整引述對話內容
- **HEIC 格式**：iOS 截圖可能是 HEIC，需用 `pillow-heif` 處理
- **檔名衝突**：如果加了序號後仍有同名，加上 `_a`, `_b` 後綴區分
