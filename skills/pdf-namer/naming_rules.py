# -*- coding: utf-8 -*-
"""
pdf-namer / naming_rules.py
============================
法律事務所 PDF 命名規則引擎。
從 Synology Drive 01_案件 中 1,447 份已正確命名 PDF 歸納而來。

核心格式:  YYYYMMDD 文件類型(補充資訊).pdf
"""

import re
from typing import Dict, Optional, Tuple, List

# ─────────────────────────────── 文件類型分類 ──────────────────────────────

DOC_CATEGORIES: Dict[str, dict] = {
    "判決": {
        "keywords": ["判決", "判決書"],
        "template": "{date} {court}{case_no}{case_type}判決（{party}；{summary}）",
        "folder": "03_對造歷次書狀/判決",
        "example": "20250707 花蓮地方法院113年度原易字第179號刑事判決（余秋菊；主文：施用第一級毒品罪）.pdf",
    },
    "裁定": {
        "keywords": ["裁定", "裁定書"],
        "template": "{date} {court}{case_no}{case_type}裁定({party}；{summary})",
        "folder": "03_對造歷次書狀/裁定",
        "example": "20250910 高雄高等行政法院113年度監簡字第25號裁定(吳榮林；主文：停止訴訟程序).pdf",
    },
    "庭通知書": {
        "keywords": ["庭通知書", "庭通知", "開庭通知"],
        "template": "{date} {court}{case_no}{case_type}庭通知書（{party}；{summary}）",
        "folder": "01_法院通知公函",
        "example": "20250925 高等法院花蓮分院114年度原上易字第30號刑事庭通知書（余秋菊；訂11月4日上午10時）.pdf",
    },
    "函文": {
        "keywords": ["函", "主旨"],
        "template": "{date} {court}{case_no}函（{party}；主旨：{summary}）",
        "folder": "01_法院通知公函",
        "example": "20250812 花蓮地方法院113年度原易字第179號函（余秋菊；主旨：被告余秋菊不服判決）.pdf",
    },
    "書狀_我方": {
        "keywords": ["書狀", "聲請狀", "答辯狀", "上訴狀", "抗告狀", "理由狀", "陳報狀", "準備狀"],
        "template": "{date} {doc_subtype}({party}){suffix}",
        "folder": "02_我方歷次書狀",
        "example": "20250721 刑事聲明上訴暨上訴理由狀(余秋菊)存底.pdf",
    },
    "書狀_對造": {
        "keywords": ["對造", "原告", "被告", "上訴人"],
        "template": "{date} 對造{doc_subtype}",
        "folder": "03_對造歷次書狀",
    },
    "信件": {
        "keywords": ["信件", "來信", "書信"],
        "template": "{date} {party}信件",
        "folder": "04_往來信函",
        "example": "20250107 余秋菊信件.pdf",
    },
    "契約": {
        "keywords": ["契約", "委任契約", "委任書", "借貸契約"],
        "template": "{date} {doc_subtype}({party}){suffix}",
        "folder": "05_委任相關",
        "example": "20230212 鑫源企業社民事一審委任契約書(詹文傑)_已用印.pdf",
    },
    "收據": {
        "keywords": ["收據", "酬金", "領款"],
        "template": "{date} {doc_subtype}（{summary}）",
        "folder": "05_委任相關/收據",
        "example": "20240401 律師酬金收據（法顧費用18025）.pdf",
    },
    "法扶表單": {
        "keywords": ["法扶", "委任狀_", "審查表_", "准予扶助", "法律扶助", "結案回報", "案件概述"],
        "template": "{original}",  # 保持法扶系統原格式
        "folder": "06_法扶相關",
        "example": "委任狀_1150122-J-008_1150126.pdf",
    },
    "法扶回報": {
        "keywords": ["回報單", "回報書"],
        "template": "{date} {laf_case_no}({party})_{doc_subtype}",
        "folder": "06_法扶相關",
        "example": "20250124 1131216-J-008(吳榮林)_花分回報單.pdf",
    },
    "閱卷": {
        "keywords": ["閱卷", "規費繳款"],
        "template": "{date} {case_no}《{party}案》{doc_subtype}",
        "folder": "07_閱卷相關",
        "example": "20251002 114年度原上易字第30號《余秋菊案》高等法院花蓮分院規費繳款單（線上聲請閱卷）.pdf",
    },
    "委任相關": {
        "keywords": ["委任證明", "同意書", "告知", "居住同意"],
        "template": "{date} {doc_subtype}({party}){suffix}",
        "folder": "05_委任相關",
        "example": "20250718 無償委任證明書(余秋菊)_已簽名.pdf",
    },
    "債清_書狀": {
        "keywords": ["消費者債務清理", "債權人清冊", "債務人清冊", "更生方案", "財產及收入"],
        "template": "{seq}_{doc_subtype}（{party}）{suffix}",
        "folder": "02_我方歷次書狀",
        "example": "01_消費者債務清理聲請狀（劉亞箖）.pdf",
    },
    "證據": {
        "keywords": ["證據", "鑑定", "函詢", "陳報", "意見書"],
        "template": "{date} {doc_subtype}",
        "folder": "07_證據資料",
        "example": "20230922 李俊億教授書面意見.pdf",
    },
    "法院通知": {
        "keywords": ["傳票", "通知", "命補正"],
        "template": "{date} {court}{case_no}{doc_subtype}",
        "folder": "09_法院通知或程序裁定",
        "example": "20250812 花蓮地方法院通知.pdf",
    },
}


# ─────────────────────────────── 正則提取器 ───────────────────────────────

# 台灣法院案號：如 113年度原易字第179號
CASE_NUMBER_RE = re.compile(
    r"(\d{2,3})\s*年度?\s*"
    r"([\u4e00-\u9fff]+字)\s*"
    r"第\s*(\d+)\s*(?:號|号)+",
)

# 日期前綴 YYYYMMDD
DATE_PREFIX_RE = re.compile(r"^(20\d{6})")

# 舊式日期 YYY.MM.DD
OLD_DATE_RE = re.compile(r"^(\d{2,3})\.(\d{1,2})\.(\d{1,2})")

# 括號內容 （...） 或 (...)
PAREN_CONTENT_RE = re.compile(r"[（(](.+?)[）)]")

# 法扶案號
LAF_CASE_RE = re.compile(r"(\d{7}-[A-Z]-\d{3})")


def extract_date(text: str) -> Optional[str]:
    """
    Extract YYYYMMDD date from text.
    Priority: 收文日期 > 收狀日期 > 發文日期 > first 中華民國日期 > filename prefix.
    
    In legal practice, the filename date = 收文日 (document receipt date),
    NOT 發文日 (issue date). This function prioritizes accordingly.
    """
    # Priority 1: Check filename prefix YYYYMMDD (already manually assigned)
    m = DATE_PREFIX_RE.search(text)
    if m:
        return m.group(1)
    
    # Priority 2: Old format YYY.MM.DD in filename
    m = OLD_DATE_RE.search(text)
    if m:
        y = int(m.group(1)) + 1911
        return f"{y}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    
    # Priority 3: 收文日 (receipt date) — this is the correct date for filenames
    receipt_date = _find_receipt_date(text)
    if receipt_date:
        return receipt_date
    
    # Priority 4: 發文日期 label
    issue_date = _find_labeled_date(text, ["發文日期", "發文日"])
    
    # Priority 5: First 中華民國 date in document (fallback)
    m = re.search(r"中華民國\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    first_roc = None
    if m:
        y = int(m.group(1)) + 1911
        first_roc = f"{y}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    
    # Return: prefer issue_date (closer to receipt) over random first date
    return issue_date or first_roc


def _find_receipt_date(text: str) -> Optional[str]:
    """
    Look for explicit receipt date stamps in legal documents.
    Common patterns: 收文日期, 收狀日期, 收受日期, 到達日期
    """
    receipt_labels = [
        r"收文日期[：:\s]*",
        r"收狀日期[：:\s]*",
        r"收受日期[：:\s]*",
        r"到達日期[：:\s]*",
        r"收文[：:\s]*",
    ]
    
    for label_re in receipt_labels:
        # Try ROC format after label: 114年2月15日 or 114.02.15
        pattern = label_re + r"(\d{2,3})\s*[年.]\s*(\d{1,2})\s*[月.]\s*(\d{1,2})\s*日?"
        m = re.search(pattern, text)
        if m:
            y = int(m.group(1)) + 1911
            return f"{y}{int(m.group(2)):02d}{int(m.group(3)):02d}"
        
        # Try AD format: 2025/02/15 or 2025-02-15
        pattern_ad = label_re + r"(20\d{2})[/\-.](\d{1,2})[/\-.](\d{1,2})"
        m2 = re.search(pattern_ad, text)
        if m2:
            return f"{m2.group(1)}{int(m2.group(2)):02d}{int(m2.group(3)):02d}"
    
    return None


def _find_labeled_date(text: str, labels: list) -> Optional[str]:
    """Find a date that follows a specific label."""
    for label in labels:
        pattern = label + r"[：:\s]*(\d{2,3})\s*[年.]\s*(\d{1,2})\s*[月.]\s*(\d{1,2})\s*日?"
        m = re.search(pattern, text)
        if m:
            y = int(m.group(1)) + 1911
            return f"{y}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    return None


def extract_case_number(text: str) -> Optional[str]:
    """Extract court case number."""
    m = CASE_NUMBER_RE.search(text)
    if m:
        # m.group(2) already ends with 字 (e.g. 原易字)
        return f"{m.group(1)}年度{m.group(2)}第{m.group(3)}號"
    return None


def extract_court_name(text: str) -> Optional[str]:
    """Extract court name from text."""
    courts = [
        "最高法院", "最高行政法院",
        "臺灣高等法院花蓮分院", "臺灣高等法院臺中分院", "臺灣高等法院臺南分院",
        "臺灣高等法院高雄分院", "臺灣高等法院",
        "高等法院花蓮分院", "高等法院臺中分院", "高等法院臺南分院",
        "高等法院高雄分院", "高等法院",
        "高雄高等行政法院", "臺中高等行政法院", "臺北高等行政法院",
    ]
    # 地方法院（含簡稱）
    local_courts_re = re.compile(r"(臺灣)?([\u4e00-\u9fff]{2,4})(地方法院|地院)")
    
    for c in courts:
        if c in text:
            return c
    
    m = local_courts_re.search(text)
    if m:
        area = m.group(2)
        return f"{area}地方法院"
    return None


def extract_party_name(text: str) -> Optional[str]:
    """Extract party name from parentheses or common patterns."""
    for m in PAREN_CONTENT_RE.finditer(text):
        content = m.group(1)
        # 排除非人名的括號內容
        non_name_patterns = ["主文", "主旨", "上午", "下午", "訂", "法顧", "費用",
                            "正本", "繕", "含證據", "印", "份"]
        if any(p in content for p in non_name_patterns):
            continue
        # 取分號前的部分（分號後通常是摘要）
        name = content.split("；")[0].split(";")[0].strip()
        # Reject Chinese numerals (一/二/三...) from legal notation like 準備(一)狀
        if re.match(r'^[一二三四五六七八九十]+$', name):
            continue
        # Reject single characters
        if len(name) <= 1:
            continue
        # Check it looks like a name (2-5 Chinese characters)
        if re.match(r'^[\u4e00-\u9fff]{2,5}$', name):
            return name
        # Or a known format with brackets
        if len(name) <= 20:
            return name
    return None


def classify_document(text: str, filename: str = "") -> Tuple[str, float]:
    """
    Classify document type based on text content and filename.
    Returns (category_name, confidence).
    """
    combined = f"{filename} {text[:2000]}"
    
    scores: Dict[str, float] = {}
    for cat_name, cat_info in DOC_CATEGORIES.items():
        score = 0.0
        for kw in cat_info["keywords"]:
            count = combined.count(kw)
            if count > 0:
                score += min(count * 0.3, 1.0)
        scores[cat_name] = score
    
    if not scores or max(scores.values()) == 0:
        return ("未分類", 0.0)
    
    best = max(scores, key=scores.get)
    return (best, min(scores[best], 1.0))



def build_few_shot_examples(max_per_category: int = 3) -> str:
    """Build few-shot examples string for AI prompt from training data."""
    import os, json
    
    # Try to load from generated few_shot_prompt.md first
    prompt_path = os.path.join(os.path.dirname(__file__), "few_shot_prompt.md")
    if os.path.exists(prompt_path):
        with open(prompt_path, "r", encoding="utf-8") as f:
            content = f.read()
        # Skip header lines
        lines = content.split("\n")
        start = 0
        for i, line in enumerate(lines):
            if line.startswith("### "):
                start = i
                break
        return "\n".join(lines[start:])
    
    # Fallback: build from DOC_CATEGORIES examples
    lines = []
    for cat_name, cat_info in DOC_CATEGORIES.items():
        example = cat_info.get("example", "")
        if example:
            lines.append(f"- 類型「{cat_name}」→ 檔名：{example}")
    return "\n".join(lines)


def load_training_data() -> List[dict]:
    """Load training data from JSON file."""
    import os, json
    path = os.path.join(os.path.dirname(__file__), "training_data.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


# ─────────────────────────────── AI Prompt ────────────────────────────────

SYSTEM_PROMPT = """你是專業的法律事務所文件管理助手。你的任務是根據 PDF 首頁的文字內容，判斷文件類型並產生標準檔名。

## 命名規範

1. **日期前綴**：YYYYMMDD（西元年）。日期 = **收文日期**（事務所收到文件的日期），而非發文日期。如為民國年，需 +1911 轉換。若文件上明確標示「收文日期」就用該日期；若無，則用「發文日期」作為替代。
2. **文件類型**：日期後緊接空格再寫類型
3. **法院+案號**：判決/裁定/庭通知/函文 需含法院全稱和案號
4. **括號補充**：
   - 判決/裁定：（當事人；主文摘要）
   - 庭通知書：（當事人；訂X月X日X時整審理）
   - 函文：（當事人；主旨：xxx）
   - 書狀：(當事人)
5. **後綴**：_已簽名、存底、_繕本、_已用印（如適用）
6. **法扶表單**：保持法扶系統原格式（如 委任狀_案號_日期.pdf）

## 案件資料夾結構
```
01_法院通知公函/    ← 庭通知書、函文
02_我方歷次書狀/    ← 我方書狀、債清聲請
03_對造歷次書狀/    ← 對造書狀、判決、裁定
04_往來信函/        ← 信件
05_委任相關/        ← 契約、收據、委任證明
06_法扶相關/        ← 法扶表單、回報單
07_判決書/          ← 判決
```

## 分類範例
""" + build_few_shot_examples() + """

## 輸出格式
只輸出 JSON，不要任何其他文字：
{
    "doc_type": "文件類型名稱（判決/裁定/庭通知書/函文/書狀_我方/書狀_對造/信件/契約/收據/法扶表單/法扶回報/閱卷/委任相關/債清_書狀）",
    "suggested_filename": "建議的完整檔名.pdf",
    "date": "YYYYMMDD 或 null",
    "party": "當事人姓名 或 null",
    "case_number": "法院案號 或 null",
    "confidence": 0.0-1.0,
    "reasoning": "判斷理由（簡述）"
}
"""
