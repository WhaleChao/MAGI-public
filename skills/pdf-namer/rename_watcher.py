#!/usr/bin/env python3
"""
PDF 命名反饋學習器：監控案件資料夾的檔案更名事件。
當使用者手動修正 PDF 檔名時，自動擷取修正模式並寫入訓練資料。

運作方式：
- 定期掃描案件資料夾，建立 filename→inode 對照表
- 比較前後兩次掃描，找出 inode 相同但 filename 不同的 → rename
- 從更名前後的差異中提取 date/party/doc_type 修正
- 寫入 _corrections.json 和 training_data.json
"""
import os
import sys
import json
import re
import time
import logging
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Tuple

sys.stdout.reconfigure(line_buffering=True)

logger = logging.getLogger("RenameWatcher")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# 路徑
MAGI_ROOT = os.environ.get("MAGI_ROOT_DIR") or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SKILL_DIR = os.path.join(MAGI_ROOT, "skills", "pdf-namer")
CORRECTIONS_PATH = os.path.join(SKILL_DIR, "_corrections.json")
TRAINING_DATA_PATH = os.path.join(SKILL_DIR, "training_data.json")
SNAPSHOT_PATH = os.path.join(SKILL_DIR, "_rename_snapshot.json")
RENAME_LOG_PATH = os.path.join(SKILL_DIR, "_rename_log.json")

# 案件資料夾根目錄（動態解析，含 stale mount 防護）
def _init_case_roots() -> list:
    try:
        from api.case_path_mapper import preferred_case_roots
        roots = preferred_case_roots(include_closed=False)
        if roots:
            return roots
    except Exception:
        pass
    return [
        "/Users/ai/Library/CloudStorage/SynologyDrive-homes/01_案件",
        "/Volumes/homes/MAGI_NAS_SHARE/01_案件",
    ]

CASE_ROOTS = _init_case_roots()

# PDF 命名格式
DATE_PREFIX_RE = re.compile(r"^(20\d{6})\s")
PARTY_RE = re.compile(r"[（(]([^）)]+)[）)]")
COURT_RE = re.compile(r"([\u4e00-\u9fff]+(?:地方|高等|最高)(?:行政)?法院)")
CASE_NO_RE = re.compile(r"(\d{2,3}年度[\u4e00-\u9fff]+字第\d+號)")

SCAN_INTERVAL = 300  # 5 分鐘掃描一次


def _get_case_root() -> Optional[str]:
    """找到可用的案件資料夾（含 stale mount 檢查）"""
    for root in CASE_ROOTS:
        try:
            if os.path.isdir(root):
                os.listdir(root)
                return root
        except OSError:
            continue
    return None


def _parse_filename(fn: str) -> dict:
    """從檔名中解析 date, party, court, case_number, doc_type"""
    result = {"date": "", "party": "", "court": "", "case_number": "", "raw": fn}

    # 日期
    m = DATE_PREFIX_RE.match(fn)
    if m:
        result["date"] = m.group(1)

    # 當事人
    m = PARTY_RE.search(fn)
    if m:
        party_str = m.group(1)
        # 分割多個當事人或摘要
        if "；" in party_str:
            result["party"] = party_str.split("；")[0].strip()
        else:
            result["party"] = party_str.strip()

    # 法院
    m = COURT_RE.search(fn)
    if m:
        result["court"] = m.group(1)

    # 案號
    m = CASE_NO_RE.search(fn)
    if m:
        result["case_number"] = m.group(1)

    # 文件類型（日期和案號/法院之間的文字）
    base = os.path.splitext(fn)[0]
    # 去掉日期前綴
    if result["date"]:
        base = base[9:].strip()  # "20260401 " = 9 chars
    # 去掉括號部分
    base = re.sub(r"[（(][^）)]*[）)]", "", base).strip()
    # 去掉法院+案號
    if result["court"]:
        base = base.replace(result["court"], "").strip()
    if result["case_number"]:
        base = base.replace(result["case_number"], "").strip()
    result["doc_type"] = base.strip()

    return result


def _subfolder_to_category(subfolder: str) -> str:
    """資料夾名稱 → 分類"""
    mappings = {
        "00_委任狀": "委任相關",
        "01_": "委任相關",
        "02_起訴": "書狀",
        "03_": "書狀",
        "04_我方": "書狀",
        "05_對方": "對方歷次書狀",
        # 06_閱卷 — 由閱卷模組管理，不納入
        "07_判決": "判決",
        "08_裁定": "裁定",
        "09_法院通知": "法院通知",
        "10_執行": "執行",
    }
    for prefix, cat in mappings.items():
        if subfolder.startswith(prefix):
            return cat
    if "證據" in subfolder:
        return "證據資料"
    if "收據" in subfolder or "繳費" in subfolder:
        return "收據"
    return ""


# 排除閱卷和筆錄資料夾（由其他模組自動管理）
_SKIP_SUBFOLDERS = {"06_閱卷資料", "閱卷資料", "筆錄", "庭期筆錄", "11_筆錄"}

def scan_pdfs(case_root: str) -> Dict[int, dict]:
    """掃描所有 PDF，建立 inode → 檔案資訊 的對照表"""
    result = {}
    for root, dirs, files in os.walk(case_root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        subfolder = os.path.basename(root)
        if subfolder in _SKIP_SUBFOLDERS or "閱卷" in subfolder or "筆錄" in subfolder:
            continue
        for fn in files:
            if not fn.lower().endswith(".pdf") or fn.startswith("."):
                continue
            fp = os.path.join(root, fn)
            try:
                stat = os.stat(fp)
                inode = stat.st_ino
                result[inode] = {
                    "path": fp,
                    "filename": fn,
                    "subfolder": subfolder,
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                }
            except (OSError, PermissionError):
                continue
    return result


def load_snapshot() -> Dict[int, dict]:
    """載入上次的 snapshot"""
    try:
        if os.path.exists(SNAPSHOT_PATH):
            with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # JSON key 是 string，轉回 int
            return {int(k): v for k, v in data.items()}
    except Exception:
        pass
    return {}


def save_snapshot(snapshot: Dict[int, dict]):
    """儲存 snapshot"""
    # 只存最近的（避免檔案太大）
    data = {str(k): v for k, v in snapshot.items()}
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def detect_renames(old_snap: Dict[int, dict], new_snap: Dict[int, dict]) -> List[dict]:
    """比較兩次 snapshot，找出更名事件"""
    renames = []
    for inode, new_info in new_snap.items():
        if inode in old_snap:
            old_info = old_snap[inode]
            old_fn = old_info["filename"]
            new_fn = new_info["filename"]
            if old_fn != new_fn and new_fn.lower().endswith(".pdf"):
                renames.append({
                    "inode": inode,
                    "old_filename": old_fn,
                    "new_filename": new_fn,
                    "old_path": old_info["path"],
                    "new_path": new_info["path"],
                    "subfolder": new_info["subfolder"],
                    "detected_at": datetime.now().isoformat(),
                })
    return renames


def extract_learning(rename: dict) -> Optional[dict]:
    """從更名事件中提取學習資料"""
    old_fn = rename["old_filename"]
    new_fn = rename["new_filename"]

    # 只學習正確命名格式的新檔名
    if not DATE_PREFIX_RE.match(new_fn):
        return None

    old_parsed = _parse_filename(old_fn)
    new_parsed = _parse_filename(new_fn)

    corrections = {}
    if old_parsed["date"] != new_parsed["date"]:
        corrections["date"] = {"from": old_parsed["date"], "to": new_parsed["date"]}
    if old_parsed["party"] != new_parsed["party"]:
        corrections["party"] = {"from": old_parsed["party"], "to": new_parsed["party"]}
    if old_parsed["court"] != new_parsed["court"]:
        corrections["court"] = {"from": old_parsed["court"], "to": new_parsed["court"]}
    if old_parsed["doc_type"] != new_parsed["doc_type"]:
        corrections["doc_type"] = {"from": old_parsed["doc_type"], "to": new_parsed["doc_type"]}

    if not corrections:
        return None

    # 從路徑推斷案件
    case_match = re.search(r"(\d{4}-\d{4}-[^/]+)", rename["new_path"])
    case_name = case_match.group(1) if case_match else ""

    # 提取當事人列表
    parties = []
    if new_parsed["party"]:
        parties = [p.strip() for p in re.split(r"[、,]", new_parsed["party"]) if p.strip()]

    return {
        "old_filename": old_fn,
        "new_filename": new_fn,
        "old_parsed": old_parsed,
        "new_parsed": new_parsed,
        "corrections": corrections,
        "case": case_name,
        "subfolder": rename["subfolder"],
        "parties": parties,
        "detected_at": rename["detected_at"],
    }


def append_correction(learning: dict):
    """寫入 _corrections.json"""
    corrections = []
    try:
        if os.path.exists(CORRECTIONS_PATH):
            with open(CORRECTIONS_PATH, "r", encoding="utf-8") as f:
                corrections = json.load(f) or []
    except Exception:
        corrections = []

    entry = {
        "timestamp": learning["detected_at"],
        "filename": learning["new_filename"],
        "old_filename": learning["old_filename"],
        "case": learning["case"],
        "subfolder": learning["subfolder"],
        "parties": learning["parties"],
        "corrections": learning["corrections"],
        "source": "rename_watcher",
    }
    corrections.append(entry)

    # 保留最近 500 筆
    corrections = corrections[-500:]
    with open(CORRECTIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(corrections, f, ensure_ascii=False, indent=2)


def append_training_data(learning: dict):
    """寫入 training_data.json"""
    training = []
    try:
        if os.path.exists(TRAINING_DATA_PATH):
            with open(TRAINING_DATA_PATH, "r", encoding="utf-8") as f:
                training = json.load(f) or []
    except Exception:
        training = []

    new_parsed = learning["new_parsed"]
    category = _subfolder_to_category(learning["subfolder"])

    entry = {
        "filename": learning["new_filename"],
        "category": category or "其他",
        "confidence": 1.0,  # 人工修正 = 100% 信心
        "date": new_parsed["date"],
        "party": new_parsed["party"],
        "case_number": new_parsed["case_number"],
        "court": new_parsed["court"],
        "text_preview": "",
        "text_method": "human_correction",
        "relative_path": learning["case"] + "/" + learning["subfolder"] + "/" + learning["new_filename"],
    }

    # 避免重複
    existing_fns = {t.get("filename") for t in training}
    if entry["filename"] not in existing_fns:
        training.append(entry)
        with open(TRAINING_DATA_PATH, "w", encoding="utf-8") as f:
            json.dump(training, f, ensure_ascii=False, indent=2)
        return True
    return False


def log_rename(learning: dict):
    """記錄更名歷史"""
    log = []
    try:
        if os.path.exists(RENAME_LOG_PATH):
            with open(RENAME_LOG_PATH, "r", encoding="utf-8") as f:
                log = json.load(f) or []
    except Exception:
        log = []

    log.append({
        "ts": learning["detected_at"],
        "old": learning["old_filename"],
        "new": learning["new_filename"],
        "case": learning["case"],
        "corrections": learning["corrections"],
    })
    log = log[-200:]
    with open(RENAME_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def run_once() -> dict:
    """執行一次掃描比對"""
    case_root = _get_case_root()
    if not case_root:
        return {"ok": False, "error": "案件資料夾不存在"}

    old_snap = load_snapshot()
    new_snap = scan_pdfs(case_root)

    if not old_snap:
        # 第一次跑，只建立 snapshot
        save_snapshot(new_snap)
        return {"ok": True, "first_run": True, "files_scanned": len(new_snap)}

    renames = detect_renames(old_snap, new_snap)
    save_snapshot(new_snap)

    learned = 0
    for rename in renames:
        learning = extract_learning(rename)
        if learning:
            append_correction(learning)
            added = append_training_data(learning)
            log_rename(learning)
            learned += 1
            logger.info(
                "📝 學習更名: %s → %s (修正: %s)",
                rename["old_filename"][:40],
                rename["new_filename"][:40],
                ", ".join(learning["corrections"].keys()),
            )

    return {
        "ok": True,
        "files_scanned": len(new_snap),
        "renames_detected": len(renames),
        "learned": learned,
    }


def main():
    logger.info("=== PDF Rename Watcher 啟動 ===")
    logger.info("掃描間隔: %d 秒", SCAN_INTERVAL)

    while True:
        try:
            result = run_once()
            if result.get("first_run"):
                logger.info("首次掃描，建立快照: %d 檔案", result.get("files_scanned", 0))
            elif result.get("learned", 0) > 0:
                logger.info(
                    "掃描完成: %d 檔案, %d 更名, %d 學習",
                    result.get("files_scanned", 0),
                    result.get("renames_detected", 0),
                    result.get("learned", 0),
                )
        except Exception as e:
            logger.error("掃描失敗: %s", e)

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
