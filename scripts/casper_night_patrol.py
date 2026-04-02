"""
Casper 夜間巡邏 (Night Patrol)
==============================
單機模式的輕量夜間自治腳本，取代需要三哲人 + Watcher 的聯邦議會。

每日 03:00 AM 由 cron 觸發，執行：
1. 日誌審查 — 掃描 daemon.log 的 ERROR/WARNING
2. 記憶歸檔 — 呼叫 memory_consolidation
3. 系統健檢 — 呼叫 magi-doctor diagnose
4. 本地自省 — 用 oMLX 分析當日異常，以白話文提出改善建議
5. 報告發送 — 透過 red_phone 推送至 LINE/Discord
6. 留檔 — 寫入 reports/night_patrol_YYYYMMDD.md

安全閥：Casper 可以提案但不能自行執行程式碼變更。
        需要改動的項目寫入 pending_proposals.json，等管理員早上審批。
"""

import json
import logging
import os
import sys
import time
import socket
from datetime import datetime, timedelta

# Project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 載入 .env — cron 環境不會自動帶入環境變數
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
except ImportError:
    pass  # 沒有 python-dotenv 就靠系統環境

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("NightPatrol")

# Paths
DAEMON_LOG = os.path.join(PROJECT_ROOT, "daemon.log")
REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")
PROPOSALS_FILE = os.path.join(PROJECT_ROOT, ".agent", "pending_proposals.json")
OMLX_URL = os.environ.get("OMLX_URL", os.environ.get("OLLAMA_URL", "http://127.0.0.1:8080"))
OMLX_MODEL = os.environ.get("CASPER_LOCAL_MODEL", "TAIDE-12b-Chat-mlx-4bit")


# ─── 1. 日誌審查 ───────────────────────────────────────────────

def scan_logs() -> dict:
    """掃描 daemon.log 最近 24 小時的異常。"""
    errors = []
    warnings = []
    total_lines = 0

    if not os.path.exists(DAEMON_LOG):
        return {"errors": [], "warnings": [], "total_lines": 0, "note": "daemon.log 不存在"}

    cutoff = datetime.now() - timedelta(hours=24)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    try:
        with open(DAEMON_LOG, "r", errors="replace") as f:
            for line in f:
                # 只看最近 24 小時（粗略比對日期前綴）
                if line[:10] < cutoff_str:
                    continue
                total_lines += 1
                if "ERROR" in line:
                    errors.append(line.strip()[:200])
                elif "WARNING" in line:
                    warnings.append(line.strip()[:200])
    except Exception as e:
        return {"errors": [], "warnings": [], "total_lines": 0, "note": f"讀取失敗: {e}"}

    # 只保留最後 20 筆避免過長
    return {
        "errors": errors[-20:],
        "warnings": warnings[-20:],
        "error_count": len(errors),
        "warning_count": len(warnings),
        "total_lines": total_lines,
    }


# ─── 2. 記憶歸檔 ───────────────────────────────────────────────

def run_memory_consolidation() -> str:
    try:
        from scripts.memory_consolidation import run_consolidation
        return run_consolidation() or "記憶歸檔完成（無新內容）"
    except Exception as e:
        return f"記憶歸檔失敗: {e}"


# ─── 3. 系統健檢 ───────────────────────────────────────────────

def run_health_check() -> dict:
    try:
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "skills", "magi-doctor"))
        from action import diagnose
        return diagnose()
    except Exception as e:
        return {"error": str(e)}


# ─── 4. 本地自省 ───────────────────────────────────────────────

def ask_local_llm(prompt: str, timeout: int = 180) -> str:
    """用本地 oMLX (OpenAI-compatible API) 做簡短分析。"""
    import requests

    # Resolve actual model name via melchior_client (canonical source)
    resolved_model = OMLX_MODEL
    try:
        from skills.bridge import melchior_client as _mc
        models = _mc.list_omlx_models()
        if models:
            req_low = OMLX_MODEL.lower()
            resolved_model = next(
                (m for m in models if req_low and (req_low == m.lower() or req_low in m.lower() or m.lower().startswith(req_low))),
                next((m for m in models if "taide" in m.lower()), models[0]),
            )
    except Exception as _e:
        logger.debug("melchior_client.list_omlx_models skipped: %s", _e)
    try:
        resp = requests.post(
            f"{OMLX_URL}/v1/chat/completions",
            json={
                "model": resolved_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 600,
                "stream": False,
            },
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            choices = data.get("choices") or []
            if choices:
                return (choices[0].get("message") or {}).get("content", "").strip()
            return "(oMLX 回應無內容)"
        return f"(oMLX 回應異常: {resp.status_code}, model={resolved_model})"
    except Exception as e:
        return f"(oMLX 無法連線: {e})"


def generate_proposals(log_result: dict, health_result: dict) -> list:
    """讓 Casper 用白話文分析問題並提出建議。"""

    error_sample = "\n".join(log_result.get("errors", [])[:10]) or "無"
    warning_sample = "\n".join(log_result.get("warnings", [])[:10]) or "無"

    health_summary = ""
    if isinstance(health_result, dict) and "error" not in health_result:
        failed = health_result.get("failed_items", [])
        if failed:
            health_summary = "健檢失敗項目:\n" + "\n".join(
                f"- {item.get('name', '?')}: {item.get('error', '?')}" for item in failed[:10]
            )
        else:
            health_summary = "所有健檢項目通過"
    else:
        health_summary = f"健檢執行失敗: {health_result.get('error', '?')}"

    prompt = f"""你是 MAGI 系統的管理 AI「Casper」，正在進行夜間自我巡檢。
請根據以下資訊，用台灣繁體中文白話文寫出：
1. 今天系統運作的簡短總結（2-3 句話）
2. 如果有需要改善的地方，列出具體建議（每項包含「問題」和「建議做法」）
   - 用一般人看得懂的白話文，不要用技術術語
   - 如果沒有問題就說「今天一切正常」

錯誤數量：{log_result.get('error_count', 0)}
警告數量：{log_result.get('warning_count', 0)}
總活動量：{log_result.get('total_lines', 0)} 行

最近的錯誤訊息：
{error_sample}

最近的警告訊息：
{warning_sample}

系統健檢結果：
{health_summary}

請用以下格式回覆：

## 今日總結
（寫在這裡）

## 改善建議
（如果有的話，每項用「- 問題：... / 建議：...」格式；沒有就寫「今天一切正常，沒有需要改動的地方。」）
"""

    analysis = ask_local_llm(prompt)
    return analysis


# ─── 5. 提案寫入 ──────────────────────────────────────────────

def save_proposals(analysis: str, log_result: dict, health_result: dict):
    """如果分析中有改善建議，寫入 pending_proposals.json。"""
    os.makedirs(os.path.dirname(PROPOSALS_FILE), exist_ok=True)

    # 讀取現有提案
    existing = []
    if os.path.exists(PROPOSALS_FILE):
        try:
            with open(PROPOSALS_FILE, "r") as f:
                existing = json.load(f)
        except Exception:
            existing = []

    # 只有分析中包含「建議」關鍵字才新增提案
    has_suggestions = "建議" in analysis and "一切正常" not in analysis

    if has_suggestions:
        proposal = {
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "source": "casper_night_patrol",
            "status": "pending",  # pending → approved / rejected
            "summary": analysis,
            "error_count": log_result.get("error_count", 0),
            "warning_count": log_result.get("warning_count", 0),
        }
        existing.append(proposal)
        with open(PROPOSALS_FILE, "w") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        logger.info(f"已寫入 1 筆新提案至 {PROPOSALS_FILE}")
        return True

    return False


# ─── 6. 報告產出與發送 ─────────────────────────────────────────

def build_report(log_result: dict, memory_result: str, health_result: dict, analysis: str, has_proposals: bool) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    time_str = datetime.now().strftime("%H:%M")

    report = f"""# Casper 夜間巡邏報告
**日期**：{today}　**時間**：{time_str}

---

## 日誌審查
- 總活動量：{log_result.get('total_lines', 0)} 行
- 錯誤：{log_result.get('error_count', 0)} 筆
- 警告：{log_result.get('warning_count', 0)} 筆

## 記憶歸檔
{memory_result}

## 系統健檢
"""
    if isinstance(health_result, dict) and "error" not in health_result:
        passed = health_result.get("overall_pass", health_result.get("passed", "?"))
        total = health_result.get("overall_total", health_result.get("total", "?"))
        report += f"通過 {passed}/{total} 項檢查（耗時 {health_result.get('elapsed_sec', '?')} 秒）\n"
        # 列出失敗項目
        for section in health_result.get("sections", []):
            for item in section.get("items", []):
                if not item.get("pass", True):
                    name = item.get("label") or item.get("skill") or item.get("id") or item.get("module") or "?"
                    detail = item.get("detail") or item.get("error") or "未知"
                    report += f"  - 失敗：{name} — {detail}\n"
    else:
        report += f"健檢異常：{health_result.get('error', '未知')}\n"

    report += f"""
## Casper 自省分析
{analysis}

## 待審提案
{"有新提案等待你早上審批，請查看 .agent/pending_proposals.json" if has_proposals else "今晚無新提案。"}

---
*此報告由 Casper 夜間巡邏自動產生*
"""
    return report


def save_report(report: str):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    filename = f"night_patrol_{datetime.now().strftime('%Y%m%d')}.md"
    filepath = os.path.join(REPORTS_DIR, filename)
    with open(filepath, "w") as f:
        f.write(report)
    logger.info(f"報告已存至 {filepath}")
    return filepath


def send_report(report: str):
    """透過 red_phone 推送摘要 + 外網完整報告連結給管理員。"""
    # 嘗試匯出完整報告為外網可讀連結
    report_url = ""
    report_path = ""
    try:
        from skills.ops.export_text import export_txt
        exported = export_txt(report, prefix="night_patrol")
        if exported.get("success"):
            report_url = exported.get("url") or ""
            report_path = exported.get("path") or ""
            logger.info("報告已匯出至外網: %s", report_url or report_path)
    except Exception as e:
        logger.warning(f"匯出外網連結失敗: {e}")

    # 推送精簡版 + 外網連結
    short = report[:800]
    if report_url:
        short += f"\n\n📄 完整報告：{report_url}"
    elif report_path:
        short += f"\n\n📄 完整報告檔案：{report_path}"
    elif len(report) > 800:
        short += "\n\n…（完整報告請見 reports/ 目錄）"

    try:
        from skills.ops.red_phone import alert_admin
        alert_admin(short, severity="info", source="casper_night_patrol", topic_key="check")
        logger.info("報告已透過 red_phone 發送")
    except Exception as e:
        logger.warning(f"red_phone 發送失敗: {e}，報告僅存檔")


# ─── 主流程 ────────────────────────────────────────────────────

def run_laf_nightly_audit():
    """步驟 6：法扶夜間巡檢 — 呼叫 laf_nightly_audit.run_audit()"""
    try:
        from scripts.laf_nightly_audit import run_audit
        result = run_audit(notify=True, dry_run=False)
        logger.info(f"  法扶巡檢完成：{result}")
        return result
    except Exception as e:
        logger.warning(f"  法扶巡檢失敗：{e}")
        return {"error": str(e)}


def main():
    start = time.time()
    logger.info("🌙 Casper 夜間巡邏開始")

    # 1. 日誌審查
    logger.info("步驟 1/6：掃描日誌")
    log_result = scan_logs()
    logger.info(f"  錯誤 {log_result.get('error_count', 0)} / 警告 {log_result.get('warning_count', 0)}")

    # 2. 記憶歸檔
    logger.info("步驟 2/6：記憶歸檔")
    memory_result = run_memory_consolidation()

    # 3. 系統健檢
    logger.info("步驟 3/6：系統健檢")
    health_result = run_health_check()

    # 4. 本地自省
    logger.info("步驟 4/6：Casper 自省分析")
    analysis = generate_proposals(log_result, health_result)

    # 5. 提案寫入
    has_proposals = save_proposals(analysis, log_result, health_result)

    # 6. 法扶夜間巡檢
    logger.info("步驟 5/6：法扶夜間巡檢")
    laf_result = run_laf_nightly_audit()

    # 7. 報告
    logger.info("步驟 6/6：產出報告")
    report = build_report(log_result, memory_result, health_result, analysis, has_proposals)
    filepath = save_report(report)
    send_report(report)

    elapsed = round(time.time() - start, 1)
    logger.info(f"🌙 夜間巡邏完成，耗時 {elapsed} 秒，報告: {filepath}")


if __name__ == "__main__":
    main()
