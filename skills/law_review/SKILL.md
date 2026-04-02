---
name: law_review
description: 使用臺灣本地模型 (TAIDE) 校正法律用語，確保輸出符合臺灣法規慣用語
---

# 臺灣法規用語校正 (Taiwan Legal Review)

## 用途
將 LLM 產出的文字（尤其是可能含有中國大陸法律用語的內容）透過臺灣本地模型 (TAIDE) 進行校正，
確保所有法律術語符合臺灣慣用語。

## 使用方式

### 作為 Python 模組
```python
from skills.law_review.tw_legal_review import review_legal_text

corrected = review_legal_text("根據人民法院的判決，該勞動合同無效。")
# 輸出: "根據法院的判決，該勞動契約無效。"
```

### 作為命令列工具
```bash
python ~/Desktop/MAGI/skills/law_review/tw_legal_review.py "被告侵犯了原告的知識產權"
```

## 需求
- Ollama 已安裝並運行
- 模型: `jcai/llama-3-taiwan-8b-instruct`
