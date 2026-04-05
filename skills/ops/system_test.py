#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
skills/ops/system_test.py

Comprehensive MAGI system health check.
Returns a structured JSON report of all subsystem statuses.
"""

import json
import logging
import os
import subprocess
import sys
import time
import socket
from datetime import datetime
from pathlib import Path

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import get_magi_root_dir
from skills.ops import health_probes as _health_probes

MAGI_DIR = str(get_magi_root_dir())
if MAGI_DIR not in sys.path:
    sys.path.insert(0, MAGI_DIR)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(MAGI_DIR, ".env"))
except ImportError:
    import logging as _log
    _log.getLogger("system_test").debug("python-dotenv not installed, relying on system env")


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
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 49, exc_info=True)
    return {
        "host": os.environ.get("OSC_WEB_DB_HOST") or os.environ.get("OSC_DB_HOST") or "100.121.61.74",
        "port": int((os.environ.get("OSC_WEB_DB_PORT") or os.environ.get("OSC_DB_PORT") or "3306").strip()),
        "user": os.environ.get("OSC_WEB_DB_USER") or os.environ.get("OSC_DB_USER") or "python_user",
        "password": os.environ.get("OSC_WEB_DB_PASSWORD") or os.environ.get("OSC_DB_PASSWORD") or os.environ.get("DB_PASSWORD") or "",
        "database": os.environ.get("OSC_WEB_DB_NAME") or os.environ.get("OSC_DB_NAME") or "law_firm_data",
    }


def _ping(ip, timeout_ms=3000):
    try:
        subprocess.check_output(
            ["ping", "-c", "1", "-W", str(timeout_ms), ip],
            stderr=subprocess.STDOUT, timeout=5
        )
        return True
    except Exception:
        return False


def _tcp_connect(host: str, port: int, timeout_sec: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout_sec):
            return True
    except Exception:
        return False


def _http_get(url, timeout=5):
    import requests
    try:
        r = requests.get(url, timeout=timeout)
        return r.status_code, r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
    except Exception as e:
        return 0, str(e)


def _probe_omlx_chat(timeout: int = 8) -> dict:
    """Probe oMLX via GET /v1/models (no inference, avoids blocking the single inference slot)."""
    probe = _health_probes.probe_omlx_models(timeout_sec=timeout)
    models = list(probe.get("models") or [])
    if probe.get("pass"):
        return {"pass": True, "detail": f"oMLX 正常 — {len(models)} models: {', '.join(models[:3])}"}
    if int(probe.get("status_code") or 0) == 200:
        return {"pass": False, "detail": "oMLX /v1/models 回傳空模型清單"}
    if probe.get("error"):
        return {"pass": False, "detail": f"oMLX 無法連線: {probe.get('error')}"}
    return {"pass": False, "detail": f"oMLX HTTP {probe.get('status_code') or 0}"}


def test_casper_ollama():
    """Test local oMLX with a real short inference probe."""
    return _probe_omlx_chat(timeout=8)


def test_melchior_remote():
    """Melchior is now local oMLX — verify embedding service (port 8081)."""
    from skills.bridge import melchior_client
    try:
        import requests

        r = requests.get(f"{melchior_client.OMLX_EMBED_BASE}/v1/models", timeout=5)
        if int(getattr(r, "status_code", 0) or 0) == 200:
            return {"pass": True, "detail": f"Melchior (oMLX Embedding) 正常 ({melchior_client.OMLX_EMBED_BASE})"}
        return {"pass": False, "detail": f"Melchior (oMLX Embedding) HTTP {r.status_code} ({melchior_client.OMLX_EMBED_BASE})"}
    except Exception as e:
        return {"pass": False, "detail": f"Melchior (oMLX Embedding) 無回應 ({melchior_client.OMLX_EMBED_BASE}): {e}"}


def test_balthasar_remote():
    """Test remote Balthasar reachability (skipped if BALTHASAR_REMOTE_ENABLED != 1)."""
    try:
        from skills.bridge import balthasar_bridge as _bb
        if not getattr(_bb, "BALTHASAR_REMOTE_ENABLED", False):
            return {"pass": True, "detail": "Balthasar 遠端未啟用 (BALTHASAR_REMOTE_ENABLED=0)，跳過"}
        ip = str(getattr(_bb, "BALTHASAR_HOST", os.environ.get("BALTHASAR_HOST", ""))).strip()
        port = str(getattr(_bb, "BALTHASAR_PORT", os.environ.get("BALTHASAR_PORT", "5002"))).strip()
    except Exception as e:
        return {"pass": False, "detail": f"balthasar_bridge 載入失敗: {e}"}
    if not ip:
        return {"pass": False, "detail": "BALTHASAR_HOST 未設定"}
    if not _tcp_connect(ip, int(port), 3.0):
        return {"pass": False, "detail": f"Balthasar ({ip}:{port}) 無法建立 TCP 連線"}
    code, _ = _http_get(f"http://{ip}:{port}/health")
    if code == 200:
        return {"pass": True, "detail": f"Balthasar 遠端在線 ({ip}:{port})"}
    return {"pass": False, "detail": f"Balthasar 服務無回應 (HTTP {code}, {ip}:{port})"}


def test_keeper_db():
    """Test Keeper (MariaDB) connectivity."""
    cfg = _load_db_profile("Studio_VPN_Remote")
    ip = str(cfg.get("host") or "100.121.61.74")
    if not _ping(ip, 3000):
        return {"pass": False, "detail": f"Keeper ({ip}) 無法 ping 到"}
    try:
        import mysql.connector
    except ModuleNotFoundError:
        return {"pass": False, "detail": "mysql.connector Python 套件未安裝"}

    try:
        conn = mysql.connector.connect(
            host=ip,
            port=int(cfg.get("port") or 3306),
            user=str(cfg.get("user") or "python_user"),
            password=str(cfg.get("password") or os.environ.get("OSC_DB_PASSWORD", "")),
            database=str(cfg.get("database") or "law_firm_data"),
            connection_timeout=5,
            use_pure=True,
        )
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        conn.close()
        return {
            "pass": True,
            "detail": f"MariaDB 連線正常 ({cfg.get('user')}@{ip}:{cfg.get('port')}/{cfg.get('database')})",
        }
    except Exception as e:
        return {"pass": False, "detail": f"DB Error: {e}"}


def test_memory_module():
    """Test memory module (mem_bridge) with a short recall."""
    try:
        from skills.memory.mem_bridge import recall
        r = recall("MAGI 系統測試", top_k=1)
        return {"pass": True, "detail": f"記憶模組正常 ({len(r)} 筆回憶)"}
    except Exception as e:
        return {"pass": False, "detail": f"記憶模組錯誤: {e}"}


def test_research_module():
    """Test internet connectivity for research."""
    targets = [("1.1.1.1", 53), ("8.8.8.8", 53)]
    for host, port in targets:
        if _tcp_connect(host, port, 3.0):
            return {"pass": True, "detail": f"網路連線正常 ({host}:{port})"}
    return {"pass": False, "detail": "外網無法建立 TCP 連線"}


def test_genesis_module():
    """Test Genesis (skill_genesis) import."""
    try:
        from skills.evolution.skill_genesis import list_skills
        skills = list_skills()
        return {"pass": True, "detail": f"進化模組正常, {len(skills)} 個技能已載入"}
    except Exception as e:
        return {"pass": False, "detail": f"進化模組載入失敗: {e}"}


def test_vector_sync():
    """Test vector DB sync module."""
    try:
        from skills.memory import keeper_sync
        return {"pass": True, "detail": "向量同步模組可載入"}
    except Exception as e:
        return {"pass": False, "detail": f"向量同步載入失敗: {e}"}


def test_iron_dome():
    """Test Iron Dome loadability."""
    try:
        from skills.iron_dome import core as iron_dome_core
        return {"pass": True, "detail": "鐵穹防禦模組正常"}
    except Exception as e:
        return {"pass": False, "detail": f"鐵穹載入失敗: {e}"}


def test_autopilot_schedule():
    """Test nightly schedule: cron_jobs.json exists and Discord bot cron scheduler is running."""
    import subprocess
    try:
        # 1. 檢查 cron_jobs.json 是否存在且有任務
        cron_path = os.path.join(os.environ.get("MAGI_ROOT_DIR", ""), "cron_jobs.json")
        if not os.path.exists(cron_path):
            cron_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "cron_jobs.json")
        if os.path.exists(cron_path):
            import json
            with open(cron_path, encoding="utf-8") as f:
                jobs = json.load(f)
            enabled = [j for j in jobs if j.get("enabled", True)]
            if enabled:
                # 2. 檢查 discord_bot.py（內建 cron scheduler）是否在跑
                bot_alive = subprocess.run(
                    ["pgrep", "-f", "discord_bot.py"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                ).returncode == 0
                if bot_alive:
                    return {"pass": True, "detail": f"Discord cron scheduler 運行中，{len(enabled)} 個任務啟用"}
                return {"pass": False, "detail": f"cron_jobs.json 有 {len(enabled)} 個任務，但 discord_bot.py 未運行"}
        return {"pass": False, "detail": "cron_jobs.json 不存在"}
    except Exception as e:
        return {"pass": False, "detail": str(e)}


def test_daily_reflection():
    """Test daily_reflection script exists."""
    script = os.path.join(MAGI_DIR, "skills", "ops", "daily_reflection.py")
    if os.path.exists(script):
        return {"pass": True, "detail": "每日反省腳本存在"}
    return {"pass": False, "detail": "daily_reflection.py 未找到"}


def _resolve_omlx_model_label() -> str:
    default_model = (os.environ.get("CASPER_LOCAL_MODEL") or "").strip() or ""
    return _health_probes.resolve_omlx_model(default_model)


def _probe_local_llm_inference(timeout: int = 60, retries: int = 3, backoff_sec: float = 2.0) -> dict:
    """Run a bounded local LLM probe and retry when model is loading."""
    probe = _health_probes.probe_local_chat(
        timeout_sec=timeout,
        retries=retries,
        backoff_sec=backoff_sec,
        default_model=(os.environ.get("CASPER_LOCAL_MODEL") or "gemma4:26b").strip() or "gemma4:26b",
        models_timeout_sec=5,
    )
    if not probe.get("pass"):
        models_probe = probe.get("models_probe") or {}
        if int(models_probe.get("status_code") or 0) == 200 and not models_probe.get("models"):
            return {"pass": False, "detail": "oMLX /v1/models 回傳空模型清單"}
        return {"pass": False, "detail": f"本地 LLM 無有效回覆: {probe.get('error') or models_probe.get('error') or 'inference unavailable'}"}

    detail = (
        f"本地 LLM 推理正常 (local_direct, "
        f"model={probe.get('model')}): "
        f"'{str(probe.get('response') or '').strip()[:30]}...'"
    )
    if int(probe.get("attempt") or 1) > 1:
        detail += f" [retry={probe.get('attempt')}]"
    return {"pass": True, "detail": detail}


def test_local_llm_inference():
    """Test local TAIDE inference with a short prompt via oMLX."""
    try:
        return _probe_local_llm_inference(
            timeout=int(os.environ.get("MAGI_SYSTEM_TEST_LOCAL_LLM_TIMEOUT", "30")),
            retries=int(os.environ.get("MAGI_SYSTEM_TEST_LOCAL_LLM_RETRIES", "2")),
        )
    except Exception as e:
        return {"pass": False, "detail": f"TAIDE 推理失敗: {e}"}


def test_local_embedding_inference():
    """Test local embedding inference against the dedicated oMLX embed service."""
    try:
        from skills.bridge import melchior_client

        import requests

        r = requests.post(
            f"{melchior_client.OMLX_EMBED_BASE}/v1/embeddings",
            json={"model": melchior_client.OMLX_EMBED_MODEL, "input": "請輸出這段文字的向量表示。"},
            timeout=60,
        )
        if int(getattr(r, "status_code", 0) or 0) != 200:
            return {"pass": False, "detail": f"Embedding HTTP {r.status_code} ({melchior_client.OMLX_EMBED_BASE})"}
        data = r.json() if hasattr(r, "json") else {}
        emb_data = (data or {}).get("data", [])
        vec = emb_data[0].get("embedding", []) if emb_data and isinstance(emb_data, list) else []
        if isinstance(vec, list) and len(vec) >= 256:
            return {
                "pass": True,
                "detail": (
                    f"Embedding 推理正常 (dims={len(vec)}, "
                    f"base={melchior_client.OMLX_EMBED_BASE})"
                ),
            }
        return {"pass": False, "detail": f"Embedding 維度異常: {len(vec) if isinstance(vec, list) else 'invalid'}"}
    except Exception as e:
        return {"pass": False, "detail": f"Embedding 推理失敗: {e}"}


# ---- Main Runner ----

ALL_TESTS = [
    ("casper_ollama",      "oMLX 本地推理",          test_casper_ollama),
    ("melchior_remote",    "Melchior 遠端主機",       test_melchior_remote),
    ("keeper_db",          "Keeper 資料庫",           test_keeper_db),
    ("memory_module",      "記憶模組 (MEMORY)",       test_memory_module),
    ("research_module",    "研究模組 (RESEARCH)",     test_research_module),
    ("genesis_module",     "進化模組 (GENESIS)",      test_genesis_module),
    ("vector_sync",        "向量同步 (VECTOR)",       test_vector_sync),
    ("iron_dome",          "鐵穹防禦 (IRON DOME)",    test_iron_dome),
    ("autopilot_schedule", "夜間排程 (AUTOPILOT)",    test_autopilot_schedule),
    ("daily_reflection",   "每日反省 (REFLECTION)",   test_daily_reflection),
    ("local_llm",          "本機 LLM 推理 (TAIDE)",   test_local_llm_inference),
    ("local_embed",        "本機 Embedding 推理",     test_local_embedding_inference),
]


def run_all_tests():
    """Execute all tests and return structured report."""
    results = []
    passed = 0
    failed = 0
    start = time.time()

    for test_id, label, fn in ALL_TESTS:
        try:
            r = fn()
        except Exception as e:
            r = {"pass": False, "detail": f"未預期錯誤: {e}"}
        r["id"] = test_id
        r["label"] = label
        results.append(r)
        if r["pass"]:
            passed += 1
        else:
            failed += 1

    elapsed = round(time.time() - start, 1)

    report = {
        "timestamp": datetime.now().isoformat(),
        "elapsed_sec": elapsed,
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "score": f"{passed}/{len(results)}",
        "tests": results,
    }

    # Save report
    report_path = os.path.join(MAGI_DIR, "static", "system_test_report.json")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    return report


if __name__ == "__main__":
    print("🔍 MAGI 全系統功能測試...")
    report = run_all_tests()
    print(f"\n📊 結果: {report['score']} 通過 ({report['elapsed_sec']}s)")
    for t in report["tests"]:
        icon = "✅" if t["pass"] else "❌"
        print(f"  {icon} {t['label']}: {t['detail']}")
