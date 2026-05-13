#!/usr/bin/env python3
"""
judicial_jlist_crawl.py — 司法院 JList 批量判決抓取
===================================================
在開放時間（00:00-06:00）用 Auth → JList → JDoc 流程批量下載判決全文。
比 daily_crawl 的搜尋路徑更穩定（避免 500 錯誤）。

用法：
    python3 scripts/judicial_jlist_crawl.py
    python3 scripts/judicial_jlist_crawl.py --budget 10800  # 3 小時

排程：cron_jobs.json 中以 00:15 觸發（開放後 15 分鐘開始）
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

MAGI_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAGI_ROOT))

# Load .env
for line in (MAGI_ROOT / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(MAGI_ROOT / ".agent" / "judicial_jlist_crawl.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger("JListCrawl")

# ── 從 judgment-collector 借用 API 工具 ──
sys.path.insert(0, str(MAGI_ROOT / "skills" / "judgment-collector"))
from action import _jdg_post_json, _get_jdg_credentials  # noqa: E402

DEFAULT_BUDGET_SEC = int(os.environ.get("JUDICIAL_JLIST_BUDGET_SEC", "18000"))  # 5 小時
SAVE_DIR = MAGI_ROOT / ".cache" / "judgment_collector"
TOKEN_REFRESH_INTERVAL = 80  # 每 80 筆重新認證
REQUEST_DELAY = 0.3  # 每筆間隔秒數


def _is_open_hours() -> bool:
    """檢查是否在司法院 API 開放時間 (00:00-06:00)。"""
    h = datetime.now().hour
    return 0 <= h < 6


def _authenticate() -> str:
    """認證取得 token。"""
    user, pwd, src = _get_jdg_credentials()
    if not user or not pwd:
        raise RuntimeError(f"無法取得司法院 API 認證資訊 (source={src})")
    auth = _jdg_post_json("Auth", {"user": user, "password": pwd}, timeout_sec=20)
    token = auth.get("Token", "")
    if not token:
        raise RuntimeError(f"Auth 失敗: {auth}")
    return token


def _save_judgment(jid: str, content: str) -> str:
    """存判決全文到檔案。"""
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = jid.replace(",", "_").replace("/", "_")
    fpath = SAVE_DIR / f"{safe_name}.txt"
    fpath.write_text(content, encoding="utf-8")
    return str(fpath)


def _mark_dedup(jid: str, metadata: dict | None = None):
    """標記到 DB 去重。"""
    try:
        from skills.ops.dedup_db import mark_done
        mark_done("judgment_jlist", jid, metadata=metadata)
    except Exception:
        pass


def _is_already_fetched(jid: str) -> bool:
    """檢查是否已抓過。"""
    try:
        from skills.ops.dedup_db import is_done
        return is_done("judgment_jlist", jid)
    except Exception:
        # DB 不可用時用檔案檢查
        safe_name = jid.replace(",", "_").replace("/", "_")
        return (SAVE_DIR / f"{safe_name}.txt").exists()


def run(budget_sec: int = DEFAULT_BUDGET_SEC):
    if not _is_open_hours():
        logger.info("⏰ 不在開放時間 (00:00-06:00)，跳過")
        return {"success": False, "reason": "not_open_hours"}

    logger.info("=== 司法院 JList 批量抓取 ===")
    logger.info("Budget: %ds (%.1f hr)", budget_sec, budget_sec / 3600)

    # 1. Auth
    try:
        token = _authenticate()
        user, pwd, _ = _get_jdg_credentials()
    except Exception as e:
        logger.error("認證失敗: %s", e)
        return {"success": False, "error": str(e)}
    logger.info("✓ Token 取得成功")

    # 2. JList
    jlist = _jdg_post_json("JList", {"token": token}, timeout_sec=30)
    if not isinstance(jlist, list):
        logger.error("JList 失敗: %s", str(jlist)[:200])
        return {"success": False, "error": f"JList: {jlist}"}

    total_items = sum(len(c.get("list", [])) for c in jlist if isinstance(c, dict))
    logger.info("JList: %d 個法院更新, 共 %d 筆判決", len(jlist), total_items)

    # 3. 批量下載
    downloaded = 0
    skipped = 0
    deduped = 0
    errors = 0
    t0 = time.time()
    deadline = t0 + budget_sec

    for court_data in jlist:
        if not isinstance(court_data, dict):
            continue
        items = court_data.get("list", [])

        for jid in items:
            if time.time() > deadline:
                logger.warning("⏰ Time budget 用完")
                break
            if not _is_open_hours():
                logger.warning("⏰ 已超過開放時間 06:00")
                break

            # 去重
            if _is_already_fetched(jid):
                deduped += 1
                continue

            # Token 刷新
            if downloaded > 0 and downloaded % TOKEN_REFRESH_INTERVAL == 0:
                try:
                    token = _authenticate()
                    logger.info("🔄 Token 更新 (downloaded=%d)", downloaded)
                except Exception as e:
                    logger.warning("Token 更新失敗: %s", e)

            # 下載
            try:
                resp = _jdg_post_json("JDoc", {"token": token, "j": jid}, timeout_sec=30)
                if not isinstance(resp, dict):
                    errors += 1
                    continue

                content = ""
                jfullx = resp.get("JFULLX", {})
                if isinstance(jfullx, dict):
                    content = jfullx.get("JFULLCONTENT", "")
                if not content:
                    content = resp.get("JFULL", resp.get("jfull", ""))

                if not content:
                    skipped += 1
                    continue

                _save_judgment(jid, content)
                _mark_dedup(jid, {"chars": len(content), "date": datetime.now().isoformat()})
                downloaded += 1

                if downloaded % 50 == 0:
                    elapsed = time.time() - t0
                    rate = downloaded / elapsed * 60
                    logger.info(
                        "📥 %d 筆 (skip=%d, dedup=%d, err=%d, %.0f筆/分, %.0fs)",
                        downloaded, skipped, deduped, errors, rate, elapsed,
                    )

                time.sleep(REQUEST_DELAY)

            except Exception as e:
                errors += 1
                if errors % 20 == 0:
                    logger.warning("❌ Error #%d: %s", errors, str(e)[:100])

        if time.time() > deadline or not _is_open_hours():
            break

    elapsed = time.time() - t0
    result = {
        "success": True,
        "downloaded": downloaded,
        "skipped": skipped,
        "deduped": deduped,
        "errors": errors,
        "total_available": total_items,
        "elapsed_sec": round(elapsed),
        "rate_per_min": round(downloaded / max(1, elapsed) * 60, 1),
    }
    logger.info("=== 完成 ===")
    logger.info("下載: %d, 跳過: %d, 去重: %d, 錯誤: %d", downloaded, skipped, deduped, errors)
    logger.info("耗時: %.0fs (%.1f 筆/分)", elapsed, result["rate_per_min"])

    # 通知
    try:
        from skills.ops.red_phone import send_alert
        send_alert(
            f"📚 司法院判決抓取完成\n"
            f"下載: {downloaded} 筆 / 可用: {total_items} 筆\n"
            f"去重: {deduped}, 跳過: {skipped}, 錯誤: {errors}\n"
            f"耗時: {round(elapsed/60)}分鐘 ({result['rate_per_min']}筆/分)",
            source="judicial_jlist_crawl",
            severity="info",
            topic_key="judicial_api",
        )
    except Exception:
        pass

    return result


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="司法院 JList 批量判決抓取")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET_SEC, help="Time budget (seconds)")
    args = ap.parse_args()
    result = run(budget_sec=args.budget)
    print(json.dumps(result, ensure_ascii=False, indent=2))
