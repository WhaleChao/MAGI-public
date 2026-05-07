"""
attachment_matcher.py — 消債補件模組 M4/M11：檔名 keyword 匹配 + 首頁 OCR 驗證

v1（M4）：純檔名 keyword 比對（不做 OCR）。
v2（M11）：加入首頁 OCR 驗證（並行 + 低信心才驗 + cache）。
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import time
import unicodedata
from pathlib import Path
from typing import Callable, Optional

# 支援的副檔名（小寫）
_SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {".pdf", ".docx", ".doc", ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff"}
)

# 遞迴深度上限（防止符號連結迴圈或超深巢狀）
_MAX_DEPTH = 5

# 來源資料夾名稱對應 case_meta key
_SOURCE_FOLDERS: list[tuple[str, str]] = [
    ("subfolder_open_case", "02_開辦資料"),
    ("subfolder_archive", "06_閱卷資料"),
    ("subfolder_evidence", "07_證據資料"),
]

# OCR category → 用於識別文件類型的關鍵字
_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "綜所稅清單": ["綜合所得稅", "綜所稅", "各類所得"],
    "勞保異動": ["勞保異動", "勞工保險", "投保資料"],
    "戶籍謄本": ["戶籍謄本", "全戶戶籍"],
    "財產清冊": ["財產清冊", "財產查詢"],
    "存摺影本": ["存摺", "交易明細", "對帳單"],
    "健保資料": ["健保", "健康保險"],
    "租賃契約": ["租賃契約", "房屋租賃"],
    "水電帳單": ["電費", "水費", "瓦斯", "電信"],
    "身分證影本": ["身分證", "國民身分證"],
    "債權人清冊": ["債權人清冊", "債權人名冊"],
}


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _collect_files(root_dir: str, max_depth: int = _MAX_DEPTH) -> list[tuple[str, str]]:
    """遞迴掃描 root_dir 下所有符合副檔名的檔案，深度限制 max_depth。

    Returns:
        list of (abs_path, filename_nfc)
    """
    results: list[tuple[str, str]] = []
    root_depth = root_dir.rstrip(os.sep).count(os.sep)

    for dirpath, dirnames, filenames in os.walk(root_dir, followlinks=False):
        current_depth = dirpath.rstrip(os.sep).count(os.sep) - root_depth
        if current_depth >= max_depth:
            dirnames.clear()  # 不再往下遞迴
            continue

        for fname in filenames:
            fname_nfc = _nfc(fname)
            # 過濾隱藏檔
            if fname_nfc.startswith("."):
                continue
            # 過濾副檔名
            ext = os.path.splitext(fname_nfc)[1].lower()
            if ext not in _SUPPORTED_EXTENSIONS:
                continue
            abs_path = os.path.join(dirpath, fname)
            results.append((abs_path, fname_nfc))

    return results


# ---------------------------------------------------------------------------
# OCR helpers（M11）
# ---------------------------------------------------------------------------

def _magi_root() -> str:
    """解析 MAGI_ROOT。"""
    env = os.environ.get("MAGI_ROOT", "").strip()
    if env:
        return env
    # __file__ = .../src/supplement_core/attachment_matcher.py → 上溯 3 層
    return str(Path(__file__).parent.parent.parent)


def _ocr_cache_path(pdf_path: str) -> Optional[str]:
    """計算 OCR 首頁 cache 檔的絕對路徑（使用 mtime + size 作為 key 的一部份）。

    回 None 表示無法計算（檔案不存在等）。
    """
    try:
        abs_path = os.path.abspath(pdf_path)
        mtime = os.path.getmtime(pdf_path)
        size = os.path.getsize(pdf_path)
        key = hashlib.sha1(f"{abs_path}|{mtime}|{size}".encode()).hexdigest()
    except Exception:
        return None

    cache_dir = os.path.join(_magi_root(), "runtime", "supplement_cache")
    return os.path.join(cache_dir, f"ocrhead_{key}.json")


def _guess_category_from_text(text: str) -> str:
    """從 OCR 首頁文字猜 category 名稱。"""
    if not text:
        return ""
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return category
    return ""


def _run_ocr_first_page_only(pdf_path: str, timeout_sec: int) -> str:
    """只 OCR PDF 第一頁，回傳文字。失敗回 ""，不拋例外。"""
    try:
        from pdf2image import convert_from_path  # type: ignore
        from skills.engine.ocr.consensus import run_consensus  # type: ignore
    except ImportError:
        return ""
    import tempfile

    try:
        with tempfile.TemporaryDirectory() as tmp:
            # pdf2image first_page/last_page 為 1-indexed
            images = convert_from_path(pdf_path, dpi=200, first_page=1, last_page=1)
            if not images:
                return ""
            png = os.path.join(tmp, "p1.png")
            images[0].save(png)
            res = run_consensus(png, task_type="legal", timeout_sec=timeout_sec)
            if res.success:
                return (res.selected_text or res.corrected_text or "").strip()
    except Exception:
        pass
    return ""


def _ocr_first_page(pdf_path: str, timeout_sec: int = 15) -> tuple[str, str]:
    """OCR PDF 首頁，回 (category_guess, first_page_text)。

    cache 命中先回；OCR 失敗回 ("", "")，不拋例外。
    """
    cache_path = _ocr_cache_path(pdf_path)
    if cache_path and os.path.isfile(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 驗證 mtime/size 防 stale
            cached_mtime = data.get("pdf_mtime", -1)
            cached_size = data.get("pdf_size", -1)
            try:
                cur_mtime = os.path.getmtime(pdf_path)
                cur_size = os.path.getsize(pdf_path)
            except Exception:
                cur_mtime = cur_size = -1
            if abs(cached_mtime - cur_mtime) <= 1e-3 and cached_size == cur_size:
                return data.get("category_guess", ""), data.get("first_page_text", "")
        except Exception:
            pass

    text = _run_ocr_first_page_only(pdf_path, timeout_sec)
    category = _guess_category_from_text(text)

    # 寫 cache（失敗不影響主流程）
    if cache_path:
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "category_guess": category,
                        "first_page_text": text[:2000],  # 截斷省空間
                        "cached_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "pdf_mtime": os.path.getmtime(pdf_path) if os.path.exists(pdf_path) else -1,
                        "pdf_size": os.path.getsize(pdf_path) if os.path.exists(pdf_path) else -1,
                    },
                    f,
                    ensure_ascii=False,
                )
        except Exception:
            pass

    return category, text


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def find_candidates(
    case_meta: dict,
    items: list[dict],
    *,
    enable_ocr_verify: bool = False,
    ocr_max_workers: int = 4,
    ocr_timeout_per_page: int = 15,
    progress_callback: Optional[Callable] = None,
) -> list[dict]:
    """為每個補件項目，從案件附件來源資料夾找出候選檔。

    輸入：
        case_meta: parse_case_meta() 輸出
        items: supplement_extractor.extract() 輸出的 items list
        enable_ocr_verify: True 時啟用 M11 OCR 驗證階段（預設 False，維持 M4 行為）
        ocr_max_workers: OCR 並行執行緒數（預設 4）
        ocr_timeout_per_page: 每頁 OCR timeout 秒數（預設 15）
        progress_callback: 可選，回報進度；呼叫簽名為 callback(event_name: str, data: dict)

    Returns:
        list[dict]，每筆對應一個 item，順序與輸入 items 相同：
        {
            "item_id": int,
            "category": str,
            "candidates": [{
                "path": str,
                "filename": str,
                "size_bytes": int,
                "mtime": float,
                "source_folder": str,
                "matched_keywords": list[str],
                # M11 新增（有跑 OCR 的才有）：
                "verified_by_ocr": bool,
                "ocr_category": str,
            }],
            "selected": str | None,
            "status": "have" | "missing",
            "warnings": list[str],
        }
    """
    if not items:
        return []

    # ── M4 stage 1：純檔名比對 ────────────────────────────────────────────
    results = _stage1_filename_match(case_meta, items)

    if not enable_ocr_verify:
        return results

    # ── M11 stage 2：OCR 驗證 ─────────────────────────────────────────────
    # 1. 收集低信心候選（matched_keywords ≤ 1 個）
    targets: list[tuple[int, int, str]] = []  # (item_idx, cand_idx, path)
    for i, r in enumerate(results):
        for j, c in enumerate(r["candidates"]):
            if len(c.get("matched_keywords", [])) <= 1:
                targets.append((i, j, c["path"]))

    # 2. status=missing 的 item，掃漏網檔
    all_known_paths: set[str] = set()
    for r in results:
        for c in r["candidates"]:
            all_known_paths.add(c["path"])

    missing_extras: list[tuple[int, str]] = []  # (item_idx, path)
    for i, r in enumerate(results):
        if r["status"] != "missing":
            continue
        for src_key in ("subfolder_open_case", "subfolder_archive", "subfolder_evidence"):
            src = case_meta.get(src_key)
            if not src:
                continue
            src_dir = os.path.join(case_meta.get("case_dir", ""), src)
            if not os.path.isdir(src_dir):
                continue
            for root, dirs, files in os.walk(src_dir):
                for fname in files:
                    if fname.lower().endswith(".pdf"):
                        full = os.path.join(root, fname)
                        if full not in all_known_paths:
                            missing_extras.append((i, full))

    # 3. 合併 OCR jobs（去重同一路徑在同 item 中的重複）
    all_jobs: list[tuple[int, Optional[int], str]] = (
        [(i, j, p) for (i, j, p) in targets]
        + [(i, None, p) for (i, p) in missing_extras]
    )

    if not all_jobs:
        if progress_callback:
            progress_callback("ocr_start", {"total": 0})
            progress_callback("ocr_end", {})
        return results

    if progress_callback:
        progress_callback("ocr_start", {"total": len(all_jobs)})

    # 4. 並行 OCR
    with concurrent.futures.ThreadPoolExecutor(max_workers=ocr_max_workers) as ex:
        future_to_job: dict[concurrent.futures.Future, tuple[int, Optional[int], str]] = {
            ex.submit(_ocr_first_page, p, ocr_timeout_per_page): (i, j, p)
            for (i, j, p) in all_jobs
        }
        completed = 0
        for fut in concurrent.futures.as_completed(future_to_job):
            i, j, p = future_to_job[fut]
            try:
                category_guess, _text = fut.result()
            except Exception:
                category_guess = ""
            completed += 1
            if progress_callback:
                progress_callback("ocr_progress", {"done": completed, "total": len(all_jobs)})

            if not category_guess:
                continue

            # 比對目標 item 的 category
            target_category = items[i].get("category", "")
            matched = (
                category_guess in target_category
                or target_category in category_guess
            )
            if not matched:
                continue

            if j is not None:
                # 已存在候選 → 標 verified
                results[i]["candidates"][j]["verified_by_ocr"] = True
                results[i]["candidates"][j]["ocr_category"] = category_guess
            else:
                # 漏網檔 → 加入候選
                try:
                    st = os.stat(p)
                    results[i]["candidates"].append(
                        {
                            "path": p,
                            "filename": os.path.basename(p),
                            "size_bytes": st.st_size,
                            "mtime": st.st_mtime,
                            "source_folder": "OCR_RESCUE",
                            "matched_keywords": [],
                            "verified_by_ocr": True,
                            "ocr_category": category_guess,
                        }
                    )
                    results[i]["status"] = "have"
                except OSError:
                    pass

    if progress_callback:
        progress_callback("ocr_end", {})

    return results


def _stage1_filename_match(case_meta: dict, items: list[dict]) -> list[dict]:
    """M4 純檔名 keyword 比對邏輯（內部函式，供 find_candidates 呼叫）。"""
    case_dir: str = case_meta.get("case_dir", "")

    # ── 1. 解析三個來源資料夾 ──────────────────────────────────────────────
    folder_info: list[tuple[str, str, str]] = []
    global_warnings: list[str] = []

    for meta_key, display_name in _SOURCE_FOLDERS:
        subfolder_name: str = case_meta.get(meta_key, "")
        if subfolder_name:
            abs_folder = os.path.join(case_dir, subfolder_name)
        else:
            # case_meta 裡沒有這個 key 或值為空：用 display_name 猜測
            abs_folder = os.path.join(case_dir, display_name)

        if not os.path.isdir(abs_folder):
            global_warnings.append(f"{display_name} not found")
            continue

        folder_info.append((abs_folder, display_name, meta_key))

    # ── 2. 一次掃描所有來源資料夾 ──────────────────────────────────────────
    all_files: list[tuple[str, str, str, int, float]] = []

    for abs_folder, display_name, _ in folder_info:
        for abs_path, fname_nfc in _collect_files(abs_folder):
            try:
                stat = os.stat(abs_path)
                size_bytes = stat.st_size
                mtime = stat.st_mtime
            except OSError:
                size_bytes = 0
                mtime = 0.0
            all_files.append((abs_path, fname_nfc, display_name, size_bytes, mtime))

    # ── 3. 對每個 item 執行 keyword 比對 ──────────────────────────────────
    results: list[dict] = []

    for item in items:
        item_id: int = item.get("item_id", 0)
        category: str = item.get("category", "")
        raw_keywords: list[str] = list(item.get("keywords", []))

        # category 本身也當 keyword
        effective_keywords: list[str] = list(raw_keywords)
        if category and category not in effective_keywords:
            effective_keywords.append(category)

        item_warnings: list[str] = list(global_warnings)  # 繼承全域 warning

        candidates: list[dict] = []

        for abs_path, fname_nfc, source_folder, size_bytes, mtime in all_files:
            matched: list[str] = []
            for kw in effective_keywords:
                if kw and kw in fname_nfc:
                    if kw not in matched:
                        matched.append(kw)
            if matched:
                candidates.append(
                    {
                        "path": abs_path,
                        "filename": fname_nfc,
                        "size_bytes": size_bytes,
                        "mtime": mtime,
                        "source_folder": source_folder,
                        "matched_keywords": matched,
                    }
                )

        # 按 mtime 倒序（最新優先）
        candidates.sort(key=lambda c: c["mtime"], reverse=True)

        results.append(
            {
                "item_id": item_id,
                "category": category,
                "candidates": candidates,
                "selected": None,
                "status": "have" if candidates else "missing",
                "warnings": item_warnings,
            }
        )

    return results
