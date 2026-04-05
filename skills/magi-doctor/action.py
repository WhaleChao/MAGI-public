#!/usr/bin/env python3
import logging
# -*- coding: utf-8 -*-
"""
skills/magi-doctor/action.py

MAGI Doctor — 三哲人全系統自我排查與檢修技能。
涵蓋：Skill Import 檢查、依賴套件檢查、基礎設施健康檢查、自動修復。
"""

import argparse
import importlib
import json
import os
import py_compile
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

MAGI_DIR = str(Path(__file__).resolve().parents[2])
if MAGI_DIR not in sys.path:
    sys.path.insert(0, MAGI_DIR)

from skills.ops import health_probes as _health_probes

REPORT_PATH = os.path.join(MAGI_DIR, "static", "doctor_report.json")
SKILLS_DIR = os.path.join(MAGI_DIR, "skills")


# ============================================================
# 1. Skill Import 檢查 — 掃描所有 action.py 是否可 compile
# ============================================================

def check_skill_imports():
    """Scan all skills/*/action.py files for compile errors."""
    results = []
    skills_root = SKILLS_DIR

    for entry in sorted(os.listdir(skills_root)):
        skill_dir = os.path.join(skills_root, entry)
        if not os.path.isdir(skill_dir):
            continue
        action_py = os.path.join(skill_dir, "action.py")
        if not os.path.exists(action_py):
            continue

        try:
            py_compile.compile(action_py, doraise=True)
            results.append({
                "skill": entry,
                "file": action_py,
                "pass": True,
                "detail": "語法正常",
            })
        except py_compile.PyCompileError as e:
            results.append({
                "skill": entry,
                "file": action_py,
                "pass": False,
                "detail": f"編譯錯誤: {e}",
            })

    passed = sum(1 for r in results if r["pass"])
    return {
        "category": "skill_imports",
        "label": "Skill Import 檢查",
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "items": results,
    }


# ============================================================
# 2. 依賴套件檢查
# ============================================================

CORE_DEPENDENCIES = [
    ("requests", "requests"),
    ("flask", "Flask"),
    ("dotenv", "python-dotenv"),
    ("yaml", "PyYAML"),
    ("bs4", "beautifulsoup4"),
    ("mariadb", "mariadb"),
    ("numpy", "numpy"),
]


def check_dependencies():
    """Check that core Python packages are importable."""
    results = []
    for module_name, pip_name in CORE_DEPENDENCIES:
        try:
            importlib.import_module(module_name)
            results.append({
                "module": module_name,
                "pip_name": pip_name,
                "pass": True,
                "detail": "已安裝",
            })
        except ImportError:
            results.append({
                "module": module_name,
                "pip_name": pip_name,
                "pass": False,
                "detail": f"未安裝 — pip install {pip_name}",
            })

    passed = sum(1 for r in results if r["pass"])
    return {
        "category": "dependencies",
        "label": "依賴套件檢查",
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "items": results,
    }


# ============================================================
# 3. 基礎設施健康檢查 (整合 system_test.py)
# ============================================================

def _run(cmd, timeout=30):
    """Run a shell command safely."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return False, "", str(e)


def _http_get(url, timeout=5):
    """HTTP GET with JSON parsing."""
    import requests
    try:
        r = requests.get(url, timeout=timeout)
        ct = r.headers.get("content-type", "")
        body = r.json() if "json" in ct else r.text
        return r.status_code, body
    except Exception as e:
        return 0, str(e)


def _probe_omlx_chat(timeout_sec: int = 8) -> dict:
    """Probe oMLX via GET /v1/models (no inference, avoids blocking the single inference slot)."""
    probe = _health_probes.probe_omlx_models(timeout_sec=timeout_sec)
    models = list(probe.get("models") or [])
    if probe.get("pass"):
        return {"pass": True, "detail": f"推理引擎正常 — {len(models)} models: {', '.join(models[:3])}"}
    if int(probe.get("status_code") or 0) == 200:
        return {"pass": False, "detail": "oMLX /v1/models 回傳空模型清單"}
    if probe.get("error"):
        return {"pass": False, "detail": str(probe.get("error"))}
    return {"pass": False, "detail": f"oMLX HTTP {probe.get('status_code') or 0}"}


def _probe_local_llm_inference(timeout_sec: int = 30, retries: int = 2, backoff_sec: float = 1.5) -> dict:
    """Probe local TAIDE inference with one bounded retry when oMLX is briefly saturated."""
    probe = _health_probes.probe_local_chat(
        timeout_sec=timeout_sec,
        retries=retries,
        backoff_sec=backoff_sec,
        default_model=os.environ.get("MAGI_TEXT_PRIMARY_MODEL", ""),
        models_timeout_sec=8,
    )
    if not probe.get("pass"):
        models_probe = probe.get("models_probe") or {}
        if int(models_probe.get("status_code") or 0) == 200 and not models_probe.get("models"):
            return {"pass": False, "detail": "oMLX /v1/models 回傳空模型清單"}
        return {"pass": False, "detail": str(probe.get("error") or models_probe.get("error") or "oMLX unavailable")}

    detail = f"推理正常 (model={probe.get('model')})"
    if int(probe.get("attempt") or 1) > 1:
        detail += f" [retry={probe.get('attempt')}]"
    return {"pass": True, "detail": detail}


def _tcp_connect(host: str, port: int, timeout_sec: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout_sec):
            return True
    except Exception:
        return False


def _load_db_profile(profile_name: str = "Studio_VPN_Remote") -> dict:
    try:
        from api.runtime_paths import get_config_path

        cfg_path = str(get_config_path("config.json"))
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            for item in data.get("mariadb_profiles", []):
                if str(item.get("profile_name") or "").strip() != profile_name:
                    continue
                cfg = item.get("config") or {}
                return {
                    "host": str(cfg.get("host") or "100.121.61.74"),
                    "port": int(cfg.get("port") or 3306),
                    "user": str(cfg.get("user") or os.environ.get("OSC_DB_USER", "python_user")),
                    "password": str(cfg.get("password") or os.environ.get("OSC_DB_PASSWORD", "")),
                    "database": str(cfg.get("database") or "law_firm_data"),
                }
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 190, exc_info=True)
    return {
        "host": os.environ.get("OSC_DB_HOST") or "100.121.61.74",
        "port": int((os.environ.get("OSC_DB_PORT") or "3306").strip()),
        "user": os.environ.get("OSC_DB_USER") or "python_user",
        "password": os.environ.get("OSC_DB_PASSWORD") or "",
        "database": os.environ.get("OSC_DB_NAME") or "law_firm_data",
    }


def _resolve_omlx_model() -> str:
    return _health_probes.resolve_omlx_model(os.environ.get("MAGI_TEXT_PRIMARY_MODEL", ""))


def _ping(ip, timeout_ms=3000):
    """Ping an IP address."""
    try:
        subprocess.check_output(
            ["ping", "-c", "1", "-W", str(timeout_ms), ip],
            stderr=subprocess.STDOUT, timeout=5
        )
        return True
    except Exception:
        return False


def check_infrastructure():
    """Run infrastructure health checks."""
    checks = []

    # -- oMLX --
    omlx_probe = _probe_omlx_chat(timeout_sec=8)
    checks.append({
        "id": "omlx_local",
        "label": "oMLX 本地推理",
        "pass": bool(omlx_probe.get("pass")),
        "detail": str(omlx_probe.get("detail") or "unknown"),
    })

    # -- Melchior → oMLX Embedding (port 8081 / ModernBERT) --
    code2, _ = _http_get("http://127.0.0.1:8081/v1/models", timeout=4)
    emb_ok = (code2 == 200)
    checks.append({"id": "melchior_omlx_embed", "label": "Melchior (oMLX Embedding)",
                   "pass": emb_ok,
                   "detail": "Embedding 服務正常 (port 8081)" if emb_ok else "Embedding 服務無回應 (port 8081)"})



    # -- Keeper DB --
    keeper_cfg = _load_db_profile("Studio_VPN_Remote")
    keeper_ip = str(keeper_cfg.get("host") or "100.121.61.74")
    keeper_port = int(keeper_cfg.get("port") or 3306)
    keeper_user = str(keeper_cfg.get("user") or "python_user")
    keeper_password = str(keeper_cfg.get("password") or os.environ.get("OSC_DB_PASSWORD", ""))
    keeper_db = str(keeper_cfg.get("database") or "law_firm_data")
    if _tcp_connect(keeper_ip, keeper_port, 3.0):
        try:
            conn = None
            driver = ""
            try:
                import mariadb  # type: ignore
                conn = mariadb.connect(
                    host=keeper_ip,
                    port=keeper_port,
                    user=keeper_user,
                    password=keeper_password,
                    database=keeper_db,
                    connect_timeout=5,
                )
                driver = "mariadb"
            except ModuleNotFoundError:
                try:
                    import mysql.connector  # type: ignore
                    conn = mysql.connector.connect(
                        host=keeper_ip,
                        port=keeper_port,
                        user=keeper_user,
                        password=keeper_password,
                        database=keeper_db,
                        connection_timeout=5,
                        use_pure=True,
                    )
                    driver = "mysql.connector"
                except ModuleNotFoundError:
                    import pymysql  # type: ignore
                    conn = pymysql.connect(
                        host=keeper_ip,
                        port=keeper_port,
                        user=keeper_user,
                        password=keeper_password,
                        database=keeper_db,
                        connect_timeout=5,
                    )
                    driver = "pymysql"
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            conn.close()
            checks.append({"id": "keeper_db", "label": "Keeper 資料庫", "pass": True,
                            "detail": f"MariaDB 連線正常 ({driver})"})
        except Exception as e:
            checks.append({"id": "keeper_db", "label": "Keeper 資料庫", "pass": False,
                            "detail": f"DB Error: {e}"})
    else:
        checks.append({"id": "keeper_db", "label": "Keeper 資料庫", "pass": False,
                        "detail": f"無法建立 TCP 連線 ({keeper_ip}:{keeper_port})"})

    # -- Network --
    if _tcp_connect("1.1.1.1", 53, 3.0) or _tcp_connect("8.8.8.8", 53, 3.0):
        checks.append({"id": "network", "label": "外網連線", "pass": True, "detail": "TCP 連線正常"})
    else:
        checks.append({"id": "network", "label": "外網連線", "pass": False, "detail": "無法建立外網 TCP 連線"})

    # -- Memory module --
    try:
        from skills.memory.mem_bridge import recall
        r = recall("doctor ping", top_k=1)
        checks.append({"id": "memory_module", "label": "記憶模組", "pass": True,
                        "detail": f"正常 ({len(r)} 筆)"})
    except Exception as e:
        checks.append({"id": "memory_module", "label": "記憶模組", "pass": False,
                        "detail": str(e)})

    # -- Iron Dome --
    try:
        from skills.iron_dome import core as iron_dome_core  # noqa: F401
        checks.append({"id": "iron_dome", "label": "鐵穹防禦", "pass": True, "detail": "正常"})
    except Exception as e:
        checks.append({"id": "iron_dome", "label": "鐵穹防禦", "pass": False, "detail": str(e)})

    # -- Genesis / Evolution --
    try:
        from skills.evolution.skill_genesis import list_skills
        skills = list_skills()
        checks.append({"id": "genesis_module", "label": "進化模組", "pass": True,
                        "detail": f"{len(skills)} 個技能已載入"})
    except Exception as e:
        checks.append({"id": "genesis_module", "label": "進化模組", "pass": False,
                        "detail": str(e)})

    # -- Autopilot schedule --
    # v2 architecture: cron_jobs.json + Discord bot cron scheduler (no OpenClaw dependency)
    try:
        _magi_root = os.environ.get("MAGI_ROOT_DIR", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        cron_path = os.path.join(_magi_root, "cron_jobs.json")
        has_jobs = False
        job_count = 0
        if os.path.exists(cron_path):
            data = json.load(open(cron_path, "r", encoding="utf-8"))
            job_count = sum(1 for j in data if j.get("enabled", True))
            has_jobs = job_count > 0
        bot_alive = subprocess.run(
            ["pgrep", "-f", "discord_bot.py"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).returncode == 0
        if has_jobs and bot_alive:
            checks.append(
                {
                    "id": "autopilot_schedule",
                    "label": "夜間排程",
                    "pass": True,
                    "detail": f"Discord cron scheduler 運行中，{job_count} 個任務啟用",
                }
            )
        elif has_jobs:
            checks.append(
                {
                    "id": "autopilot_schedule",
                    "label": "夜間排程",
                    "pass": False,
                    "detail": f"cron_jobs.json 有 {job_count} 個任務，但 discord_bot.py 未運行",
                }
            )
        else:
            checks.append(
                {
                    "id": "autopilot_schedule",
                    "label": "夜間排程",
                    "pass": False,
                    "detail": "cron_jobs.json 不存在或無啟用任務",
                }
            )
    except Exception as e:
        checks.append({"id": "autopilot_schedule", "label": "夜間排程", "pass": False, "detail": str(e)})

    # -- TAIDE LLM (oMLX) --
    llm_probe = _probe_local_llm_inference(
        timeout_sec=int(os.environ.get("MAGI_DOCTOR_OMLX_TIMEOUT", "30")),
        retries=int(os.environ.get("MAGI_DOCTOR_OMLX_RETRIES", "2")),
    )
    checks.append({"id": "local_llm", "label": "本機 TAIDE (oMLX)", "pass": bool(llm_probe.get("pass")),
                    "detail": str(llm_probe.get("detail") or "unknown")})

    passed = sum(1 for c in checks if c["pass"])
    return {
        "category": "infrastructure",
        "label": "基礎設施健康檢查",
        "total": len(checks),
        "passed": passed,
        "failed": len(checks) - passed,
        "items": checks,
    }


# ============================================================
# 4. 自動修復 (整合 magi-self-repair)
# ============================================================

def heal(failed_infra_ids):
    """Attempt to repair failed infrastructure items."""
    repairs = []

    repair_strategies = {
        "omlx_local": _repair_ollama,
        "melchior_omlx_embed": _repair_ollama,
        "keeper_db": _repair_keeper_db,
        "memory_module": _repair_ollama,  # memory depends on embedding → oMLX
        "iron_dome": _repair_iron_dome,
        "autopilot_schedule": _repair_plist,
        "local_llm": _repair_local_llm,
        "network": _repair_network,
    }

    for fid in failed_infra_ids:
        fn = repair_strategies.get(fid)
        if callable(fn):
            try:
                result = fn()
            except Exception as e:
                result = {"repaired": False, "action": "未知", "detail": str(e)}
        elif fn is not None:
            result = fn  # pre-built dict from _repair_tailscale_ping
        else:
            result = {"repaired": False, "action": "無對應策略", "detail": fid}
        result["id"] = fid
        repairs.append(result)

    return repairs


def _repair_ollama():
    """Ollama 已退役 (2026-03-11)，改為實際探測 oMLX 推理狀態。"""
    probe = _probe_omlx_chat(timeout_sec=8)
    ok = bool(probe.get("pass"))
    return {
        "repaired": ok,
        "action": "檢查 oMLX",
        "detail": "oMLX 推理正常" if ok else f"oMLX 推理異常：{probe.get('detail') or 'unknown'}",
    }


def _repair_tailscale_ping(ip, name):
    def _fn():
        ok, _, _ = _run(["tailscale", "ping", "--c", "3", ip], timeout=10)
        return {"repaired": ok, "action": f"Tailscale ping {name}",
                "detail": f"{name} 已回應" if ok else f"{name} 仍無回應，可能需手動開機"}
    return _fn


def _repair_keeper_db():
    os.environ["MAGI_PREFER_LOCAL_DB"] = "1"
    env_path = os.path.join(MAGI_DIR, ".env")
    try:
        lines = open(env_path).readlines() if os.path.exists(env_path) else []
        found = False
        for i, line in enumerate(lines):
            if line.startswith("MAGI_PREFER_LOCAL_DB="):
                lines[i] = "MAGI_PREFER_LOCAL_DB=1\n"
                found = True
                break
        if not found:
            lines.append("MAGI_PREFER_LOCAL_DB=1\n")
        with open(env_path, "w") as f:
            f.writelines(lines)
        return {"repaired": True, "action": "切換至本地 DB", "detail": "MAGI_PREFER_LOCAL_DB=1"}
    except Exception as e:
        return {"repaired": False, "action": "切換至本地 DB", "detail": str(e)}


def _repair_iron_dome():
    script = os.path.join(MAGI_DIR, "skills", "ops", "iron_dome_sync.py")
    if os.path.exists(script):
        ok, _, err = _run([sys.executable, script], timeout=30)
        return {"repaired": ok, "action": "重新同步 Iron Dome",
                "detail": "鐵穹已重載" if ok else f"同步錯誤: {err[:200]}"}
    return {"repaired": False, "action": "Iron Dome", "detail": "iron_dome_sync.py 不存在"}


def _repair_plist():
    """v2: 排程由 Discord bot cron scheduler 管理，不再依賴 legacy plist。"""
    # 檢查 discord_bot.py 是否在跑
    import subprocess
    bot_alive = subprocess.run(
        ["pgrep", "-f", "discord_bot.py"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
    ).returncode == 0
    if bot_alive:
        return {"repaired": True, "action": "排程檢查", "detail": "Discord cron scheduler 正在運行"}
    # 嘗試重啟 daemon（會帶起 discord_bot）
    daemon_plist = os.path.expanduser("~/Library/LaunchAgents/com.magi.daemon.plist")
    if os.path.exists(daemon_plist):
        _run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.magi.daemon"])
        time.sleep(3)
        return {"repaired": True, "action": "重啟 daemon", "detail": "已嘗試重啟 daemon（含 cron scheduler）"}
    return {"repaired": False, "action": "排程修復", "detail": "daemon plist 不存在，無法自動修復"}


def _repair_local_llm():
    try:
        import requests
        r = requests.post("http://127.0.0.1:8080/v1/chat/completions",
                          json={"model": os.environ.get("MAGI_MAIN_MODEL", ""),
                                "messages": [{"role": "user", "content": "test"}],
                                "max_tokens": 5, "stream": False}, timeout=60)
        if r.status_code == 200:
            choices = r.json().get("choices") or []
            resp = (choices[0].get("message", {}).get("content", "") if choices else "")
            if resp.strip():
                return {"repaired": True, "action": "模型暖機", "detail": "TAIDE (oMLX) 推理已恢復"}
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 543, exc_info=True)
    return {"repaired": False, "action": "oMLX 修復",
            "detail": "請檢查 oMLX 服務狀態 (port 8080)"}


def _repair_network():
    """網路無法自動修復，提示人工處理。"""
    return {"repaired": False, "action": "外網連線修復",
            "detail": "無法自動修復網路，請檢查路由器、DNS 或 ISP 連線狀態"}


# ============================================================
# 5. 主流程
# ============================================================

def diagnose():
    """Run full diagnosis and return structured report."""
    start = time.time()

    skill_report = check_skill_imports()
    dep_report = check_dependencies()
    infra_report = check_infrastructure()

    elapsed = round(time.time() - start, 1)

    total_pass = skill_report["passed"] + dep_report["passed"] + infra_report["passed"]
    total_all = skill_report["total"] + dep_report["total"] + infra_report["total"]

    report = {
        "timestamp": datetime.now().isoformat(),
        "elapsed_sec": elapsed,
        "overall_score": f"{total_pass}/{total_all}",
        "overall_pass": total_pass,
        "overall_total": total_all,
        "sections": [skill_report, dep_report, infra_report],
    }

    # Ensure static dir exists
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    return report


def heal_from_report(report):
    """Run auto-repair for all failed infrastructure items."""
    # Collect failed infra IDs
    failed_ids = []
    for section in report.get("sections", []):
        if section.get("category") == "infrastructure":
            for item in section.get("items", []):
                if not item.get("pass"):
                    failed_ids.append(item["id"])

    if not failed_ids:
        return {"message": "基礎設施全部正常，無需修復", "repairs": []}

    repairs = heal(failed_ids)
    return {
        "timestamp": datetime.now().isoformat(),
        "total_targets": len(failed_ids),
        "repaired": sum(1 for r in repairs if r.get("repaired")),
        "failed": sum(1 for r in repairs if not r.get("repaired")),
        "repairs": repairs,
    }


def print_report(report):
    """Pretty-print report to terminal."""
    print(f"\n{'='*60}")
    print(f"🏥 MAGI Doctor 報告  {report['timestamp']}")
    print(f"{'='*60}")
    print(f"📊 總分: {report['overall_score']}  ({report['elapsed_sec']}s)\n")

    for section in report["sections"]:
        icon = "✅" if section["failed"] == 0 else "⚠️"
        print(f"{icon} {section['label']}  ({section['passed']}/{section['total']})")
        for item in section.get("items", []):
            status = "  ✅" if item.get("pass") else "  ❌"
            name = item.get("label") or item.get("skill") or item.get("module") or "?"
            print(f"  {status} {name}: {item.get('detail', '')}")
        print()


def print_heal_report(heal_report):
    """Pretty-print heal report."""
    if heal_report.get("message"):
        print(f"\n🎉 {heal_report['message']}")
        return
    print(f"\n{'='*60}")
    print(f"🔧 MAGI Doctor 修復報告  {heal_report['timestamp']}")
    print(f"{'='*60}")
    print(f"目標: {heal_report['total_targets']}  成功: {heal_report['repaired']}  失敗: {heal_report['failed']}\n")
    for r in heal_report.get("repairs", []):
        icon = "✅" if r.get("repaired") else "❌"
        print(f"  {icon} [{r['id']}] {r.get('action', '?')}: {r.get('detail', '')}")


# ============================================================
# CLI
# ============================================================

def main():
    # Load MAGI env for DB credentials / tokens in standalone runs.
    try:
        if load_dotenv:
            load_dotenv(os.path.join(MAGI_DIR, ".env"), override=False)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 652, exc_info=True)

    parser = argparse.ArgumentParser(description="MAGI Doctor — 全系統自我排查與檢修")
    parser.add_argument("--task", required=True, choices=["diagnose", "heal", "report", "help"],
                        help="diagnose=排查, heal=排查+修復, report=讀取上次報告, help=顯示說明")
    args = parser.parse_args()

    if args.task == "help":
        print(json.dumps({"skill": "magi-doctor", "tasks": ["diagnose", "heal", "report"], "description": "MAGI Doctor — 全系統自我排查與檢修"}, ensure_ascii=False, indent=2))
        return

    if args.task == "diagnose":
        report = diagnose()
        print_report(report)

    elif args.task == "heal":
        report = diagnose()
        print_report(report)
        heal_report = heal_from_report(report)
        print_heal_report(heal_report)
        # Re-run diagnosis to show updated state
        if heal_report.get("repairs"):
            print("\n🔄 修復後重新檢查...")
            time.sleep(2)
            report2 = diagnose()
            print_report(report2)

    elif args.task == "report":
        if os.path.exists(REPORT_PATH):
            with open(REPORT_PATH, "r") as f:
                report = json.load(f)
            print_report(report)
        else:
            print("❌ 找不到報告，請先執行 --task diagnose")

    # Always output JSON to stdout for programmatic use
    if args.task != "report":
        print(f"\n📁 報告已存至: {REPORT_PATH}")


if __name__ == "__main__":
    main()
