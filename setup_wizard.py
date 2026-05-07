#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MAGI Setup Wizard — 首次啟動設定引導
=====================================
偵測硬體、推薦模型、協助安裝、收集設定、產生 .env。

Usage:
    python3 setup_wizard.py          # 啟動 GUI 設定精靈（瀏覽器）
    python3 setup_wizard.py --check  # 僅檢查是否需要設定
    python3 setup_wizard.py --port 8888  # 指定埠號
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from api.model_config import DEFAULT_TEXT_MODEL, DEFAULT_VISION_MODEL

# ---------------------------------------------------------------------------
# Flask (bundled with MAGI dependencies)
# ---------------------------------------------------------------------------
try:
    from flask import (
        Flask,
        jsonify,
        redirect,
        render_template,
        request,
        session,
        url_for,
    )
except ImportError:
    print("Flask is required. Install with: pip install flask")
    sys.exit(1)

try:
    from skills.ops.platform_utils import (
        IS_MACOS, IS_WINDOWS, IS_LINUX, IS_APPLE_SILICON,
        find_executable, get_temp_dir,
    )
except ImportError:
    IS_MACOS = platform.system() == "Darwin"
    IS_WINDOWS = platform.system() == "Windows"
    IS_LINUX = platform.system() == "Linux"
    IS_APPLE_SILICON = IS_MACOS and platform.machine() == "arm64"
    def find_executable(name):
        return shutil.which(name)
    def get_temp_dir():
        return Path(tempfile.gettempdir())

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAGI_ROOT = Path(__file__).resolve().parent
ENV_PATH = MAGI_ROOT / ".env"
ENV_EXAMPLE = MAGI_ROOT / ".env.example"
OMLX_MODEL_DIR = Path.home() / ".omlx" / "models"
WIZARD_VERSION = "1.0.0"

logger = logging.getLogger("SetupWizard")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

# ---------------------------------------------------------------------------
# Hardware Detection
# ---------------------------------------------------------------------------

@dataclass
class HardwareInfo:
    """Detected hardware profile."""
    os_name: str = ""            # macOS / Windows / Linux
    os_version: str = ""
    arch: str = ""               # arm64 / x86_64
    cpu_name: str = ""
    cpu_cores: int = 0
    ram_gb: float = 0.0
    gpu_type: str = ""           # apple_silicon / nvidia / amd / none
    gpu_name: str = ""
    gpu_vram_gb: float = 0.0
    metal_support: bool = False
    cuda_support: bool = False
    disk_free_gb: float = 0.0
    python_version: str = ""
    has_omlx: bool = False
    has_ollama: bool = False
    has_mariadb: bool = False
    has_cloudflared: bool = False


def detect_hardware() -> HardwareInfo:
    """Detect system hardware and available tools."""
    info = HardwareInfo()
    info.python_version = platform.python_version()

    # OS
    system = platform.system()
    if system == "Darwin":
        info.os_name = "macOS"
        try:
            info.os_version = subprocess.check_output(
                ["sw_vers", "-productVersion"], text=True
            ).strip()
        except Exception:
            info.os_version = platform.mac_ver()[0]
    elif system == "Windows":
        info.os_name = "Windows"
        info.os_version = platform.version()
    else:
        info.os_name = "Linux"
        info.os_version = platform.release()

    info.arch = platform.machine()

    # CPU
    try:
        if system == "Darwin":
            info.cpu_name = subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
            ).strip()
            if not info.cpu_name:
                # Apple Silicon doesn't have brand_string
                chip = subprocess.check_output(
                    ["sysctl", "-n", "hw.chip"], text=True, stderr=subprocess.DEVNULL
                ).strip()
                info.cpu_name = chip or "Apple Silicon"
        elif system == "Windows":
            info.cpu_name = platform.processor()
        else:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if "model name" in line:
                        info.cpu_name = line.split(":")[1].strip()
                        break
    except Exception:
        info.cpu_name = platform.processor() or "Unknown"

    # Apple Silicon detection
    if info.arch == "arm64" and system == "Darwin":
        if not info.cpu_name or info.cpu_name == "Unknown":
            info.cpu_name = "Apple Silicon"
        info.gpu_type = "apple_silicon"
        info.metal_support = True
        # Unified memory — GPU shares RAM
        info.gpu_name = info.cpu_name

    info.cpu_cores = os.cpu_count() or 1

    # RAM
    try:
        import psutil
        mem = psutil.virtual_memory()
        info.ram_gb = round(mem.total / (1024 ** 3), 1)
    except ImportError:
        if system == "Darwin":
            try:
                raw = subprocess.check_output(
                    ["sysctl", "-n", "hw.memsize"], text=True
                ).strip()
                info.ram_gb = round(int(raw) / (1024 ** 3), 1)
            except Exception:
                pass
        elif system == "Windows":
            try:
                raw = subprocess.check_output(
                    ["wmic", "computersystem", "get", "TotalPhysicalMemory"],
                    text=True,
                )
                for line in raw.strip().split("\n"):
                    line = line.strip()
                    if line.isdigit():
                        info.ram_gb = round(int(line) / (1024 ** 3), 1)
            except Exception:
                pass

    # GPU (Apple Silicon unified memory)
    if info.gpu_type == "apple_silicon":
        info.gpu_vram_gb = info.ram_gb  # unified memory

    # NVIDIA GPU detection
    if not info.gpu_type:
        try:
            nv = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,memory.total",
                 "--format=csv,noheader,nounits"],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
            if nv:
                parts = nv.split(",")
                info.gpu_type = "nvidia"
                info.gpu_name = parts[0].strip()
                info.gpu_vram_gb = round(float(parts[1].strip()) / 1024, 1)
                info.cuda_support = True
        except Exception:
            pass

    # Disk
    try:
        usage = shutil.disk_usage(str(MAGI_ROOT))
        info.disk_free_gb = round(usage.free / (1024 ** 3), 1)
    except Exception:
        pass

    # Tool availability
    info.has_omlx = shutil.which("omlx") is not None
    info.has_ollama = shutil.which("ollama") is not None
    info.has_mariadb = (
        shutil.which("mariadb") is not None or shutil.which("mysql") is not None
    )
    info.has_cloudflared = shutil.which("cloudflared") is not None

    return info


# ---------------------------------------------------------------------------
# Model Catalog & Recommendation
# ---------------------------------------------------------------------------

@dataclass
class ModelSpec:
    """Model specification for recommendation."""
    name: str
    display_name: str
    size_gb: float          # disk size
    min_ram_gb: float       # minimum RAM to run
    category: str           # chat / embed / vision / code
    platform: str           # mlx / gguf / both
    description: str = ""
    recommended_for: str = ""
    omlx_id: str = ""       # oMLX model identifier
    ollama_id: str = ""     # Ollama model identifier


MODEL_CATALOG: list[ModelSpec] = [
    # ── MLX Models (Apple Silicon) ──
    ModelSpec(
        name="gemma-3-12b-it-4bit",
        display_name="Gemma 3 12B（多語言通用 + 視覺）",
        size_gb=7.0, min_ram_gb=12, category="chat", platform="mlx",
        description="Google Gemma 3 多模態模型，支援文字與視覺任務",
        recommended_for="分類器、快速推理、多語言任務、視覺辨識",
        omlx_id="gemma-3-12b-it-4bit",
    ),
    ModelSpec(
        name="Qwen2.5-Coder-14B-Instruct-4bit",
        display_name="Qwen 2.5 Coder 14B（程式碼）",
        size_gb=8.5, min_ram_gb=16, category="code", platform="mlx",
        description="程式碼生成與分析專用模型",
        recommended_for="程式碼生成、自動修復、evolution skill",
        omlx_id="Qwen2.5-Coder-14B-Instruct-4bit",
    ),
    ModelSpec(
        name="modernbert-embed-4bit",
        display_name="ModernBERT Embed（向量嵌入）",
        size_gb=0.5, min_ram_gb=8, category="embed", platform="mlx",
        description="高效向量嵌入模型，用於技能路由和語義搜索",
        recommended_for="Embedding Router、語義搜索、記憶向量化",
        omlx_id="modernbert-embed-4bit",
    ),
    # ── GGUF Models (Cross-platform / llama.cpp) ──
    ModelSpec(
        name="nomic-embed-text",
        display_name="Nomic Embed（向量嵌入）",
        size_gb=0.3, min_ram_gb=4, category="embed", platform="gguf",
        description="輕量向量嵌入模型，跨平台可用",
        recommended_for="Windows/Linux Embedding Router",
        ollama_id="nomic-embed-text",
    ),
]


def recommend_models(hw: HardwareInfo) -> dict[str, list[ModelSpec]]:
    """Recommend models based on hardware.

    Returns dict with keys: essential, recommended, optional
    """
    result: dict[str, list[ModelSpec]] = {
        "essential": [],
        "recommended": [],
        "optional": [],
    }

    # Determine platform preference
    if hw.gpu_type == "apple_silicon" and hw.has_omlx:
        plat = "mlx"
    elif hw.cuda_support or hw.os_name == "Windows":
        plat = "gguf"
    else:
        plat = "gguf"

    available_ram = hw.ram_gb

    for m in MODEL_CATALOG:
        # Filter by platform compatibility
        if m.platform != "both" and m.platform != plat:
            continue
        # Check RAM
        if m.min_ram_gb > available_ram:
            continue

        if m.category == "chat":
            result["essential"].append(m)
        elif m.category == "embed":
            result["essential"].append(m)
        elif m.category == "vision":
            result["recommended"].append(m)
        else:
            result["optional"].append(m)

    return result


def get_installed_models() -> list[str]:
    """Get list of installed oMLX model names."""
    models = []
    if OMLX_MODEL_DIR.is_dir():
        for p in OMLX_MODEL_DIR.iterdir():
            if p.is_dir():
                models.append(p.name)
    return models


# ---------------------------------------------------------------------------
# .env Generator
# ---------------------------------------------------------------------------

def generate_env(config: dict[str, Any]) -> str:
    """Generate .env file content from wizard config."""
    flask_secret = secrets.token_hex(32)

    lines = [
        "# ============================================================",
        "# MAGI — Multi-Agent Governance Infrastructure",
        f"# Generated by Setup Wizard v{WIZARD_VERSION}",
        f"# Date: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "# ============================================================",
        "",
        "# ── Node Identity ─────────────────────────────────────────────",
        "MAGI_ROLE=CASPER",
        "",
        "# ── LINE Messaging API ───────────────────────────────────────",
        f"MAGI_LINE_CHANNEL_ACCESS_TOKEN={config.get('line_token', '')}",
        f"MAGI_LINE_CHANNEL_SECRET={config.get('line_secret', '')}",
        f"MAGI_PUBLIC_BASE_URL={config.get('public_url', '')}",
        "MAGI_LINE_HEALTH_CHECK_EVERY_LOOPS=15",
        "MAGI_LINE_HEALTH_AUTO_HEAL=1",
        "",
        "# ── Admin Identity ────────────────────────────────────────────",
        f"MAGI_ADMIN_DISPLAY_NAME={config.get('admin_name', '')}",
        f"MAGI_ADMIN_LINE_IDS={config.get('admin_line_id', '')}",
        "MAGI_LINE_AUTO_ADMIN_LAST_SENDER=0",
        "",
    ]

    # Discord (optional)
    if config.get("discord_token") or config.get("discord_admin_id"):
        lines += [
            "# ── Discord ──────────────────────────────────────────────────",
            f"DISCORD_BOT_TOKEN={config.get('discord_token', '')}",
            f"DISCORD_ADMIN_IDS={config.get('discord_admin_id', '')}",
            "MAGI_INTERNAL_CRON_ENABLED=1",
            "",
        ]

    # Telegram (optional)
    if config.get("telegram_token"):
        lines += [
            "# ── Telegram ─────────────────────────────────────────────────",
            # Legacy var name kept for backward compat with existing deployments
            # that still read OPENCLAW_TELEGRAM_BOT_TOKEN. OpenClaw itself is
            # removed (2026-04-20); prefer MAGI_TELEGRAM_BOT_TOKEN going forward.
            f"OPENCLAW_TELEGRAM_BOT_TOKEN={config.get('telegram_token', '')}",
            f"MAGI_TELEGRAM_BOT_TOKEN={config.get('telegram_token', '')}",
            f"MAGI_ADMIN_TELEGRAM_IDS={config.get('telegram_admin_id', '')}",
            "",
        ]

    # Database
    db_host = config.get("db_host", "127.0.0.1")
    lines += [
        "# ── Database ──────────────────────────────────────────────────",
        f"DB_HOST={db_host}",
        f"DB_USER={config.get('db_user', 'magi')}",
        f"DB_PASSWORD={config.get('db_password', '')}",
        f"MAGI_PREFER_LOCAL_DB=1",
        "",
    ]

    # Remote DB (optional)
    if config.get("remote_db_host"):
        lines += [
            f"MAGI_REMOTE_DB_HOST={config.get('remote_db_host', '')}",
            f"MAGI_REMOTE_DB_PORT={config.get('remote_db_port', '3306')}",
            f"MAGI_REMOTE_DB_USER={config.get('remote_db_user', '')}",
            f"MAGI_REMOTE_DB_PASSWORD={config.get('remote_db_password', '')}",
            "MAGI_REMOTE_DB_NAME=law_firm_data",
            f"OSC_DB_HOST={config.get('remote_db_host', '')}",
            f"OSC_DB_PORT={config.get('remote_db_port', '3306')}",
            f"OSC_DB_USER={config.get('remote_db_user', '')}",
            f"OSC_DB_PASSWORD={config.get('remote_db_password', '')}",
            "OSC_DB_NAME=law_firm_data",
            "MAGI_ENABLE_DB_BIDIR_SYNC=1",
            "MAGI_DB_BACKUP_TARGET=both",
            "",
        ]

    # Safety
    lines += [
        "# ── Safety Policy ─────────────────────────────────────────────",
        "MAGI_ALLOW_INTERNET=1",
        "MAGI_ALLOW_CLOUD_MODELS=0",
        "MAGI_NO_DELETE=1",
        "MAGI_DB_NO_DELETE=1",
        "MAGI_MYSQL_USE_PURE=1",
        "MYSQL_USE_PURE=1",
        "MAGI_AVOID_DISTRIBUTED=1",
        "MAGI_LAF_DRAFT_ONLY=1",
        "",
    ]

    # LLM Model config
    main_model = config.get("main_model", "")
    inference = config.get("inference_engine", "omlx")
    lines += [
        "# ── LLM Configuration ─────────────────────────────────────────",
        f"MAGI_MAIN_MODEL={main_model}",
        f"CASPER_DISTRIBUTED_MODEL={main_model}",
        f"CASPER_LOCAL_MODEL={main_model}",
        f"CASPER_CLASSIFIER_MODEL={config.get('classifier_model', main_model)}",
        f"CASPER_CLASSIFIER_FALLBACK_MODELS={config.get('fallback_model', 'llama3.1:8b')}",
        "",
    ]

    if inference == "omlx":
        lines += [
            "# ── oMLX (Apple Silicon) ──────────────────────────────────────",
            "MAGI_OMLX_ENABLED=1",
            "MAGI_OMLX_HOST=127.0.0.1",
            "MAGI_OMLX_PORT=8080",
            f"MAGI_OMLX_SUMMARY_MODEL={config.get('summary_model', DEFAULT_TEXT_MODEL)}",
            f"MAGI_OMLX_VISION_MODEL={config.get('vision_model', DEFAULT_VISION_MODEL)}",
            f"MAGI_OMLX_OCR_MODEL={config.get('ocr_model', DEFAULT_VISION_MODEL)}",
            # MAGI_OPENCLAW_PRIMARY_MODEL removed 2026-04-20: OpenClaw chain deleted.
            "",
        ]
    else:
        lines += [
            "# ── Ollama / llama.cpp ────────────────────────────────────────",
            "MAGI_OMLX_ENABLED=0",
            # MAGI_OPENCLAW_PRIMARY_MODEL removed 2026-04-20: OpenClaw chain deleted.
            "",
        ]

    # Timeouts
    lines += [
        "# ── Timeouts ──────────────────────────────────────────────────",
        "MAGI_CHAT_TIMEOUT_SEC=90",
        "MAGI_QUERY_TIMEOUT_SEC=120",
        "MAGI_NL_ROUTE_EXEC_TIMEOUT_SEC=300",
        "MAGI_NL_ROUTE_ASYNC_TIMEOUT_SEC=3600",
        "MAGI_CHAT_ASYNC=1",
        "MAGI_QUERY_ASYNC=1",
        "",
    ]

    # Paths
    lines += [
        "# ── Paths ─────────────────────────────────────────────────────",
        f"MAGI_ROOT_DIR={MAGI_ROOT}",
        f"MAGI_ORCH_DIR={MAGI_ROOT / 'casper_ecosystem' / 'law_firm_orchestrators'}",
        f"MAGI_JSON_DIR={MAGI_ROOT / 'json'}",
        f"MAGI_CONFIG_PATH={MAGI_ROOT / 'json' / 'config.json'}",
        "",
    ]

    # Flask
    lines += [
        "# ── Flask ─────────────────────────────────────────────────────",
        f"FLASK_SECRET_KEY={flask_secret}",
        f"MAGI_API_KEY={secrets.token_hex(16)}",
        "",
    ]

    # Taiwan review
    lines += [
        "# ── Taiwan Output Review ──────────────────────────────────────",
        "MAGI_TW_REVIEW_ENABLED=0",
        "",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# First-run Detection
# ---------------------------------------------------------------------------

def needs_setup() -> bool:
    """Check if MAGI needs first-time setup."""
    if not ENV_PATH.exists():
        return True
    # Check if required vars are actually filled in
    from dotenv import dotenv_values
    vals = dotenv_values(ENV_PATH)
    required = [
        "MAGI_LINE_CHANNEL_ACCESS_TOKEN",
        "MAGI_LINE_CHANNEL_SECRET",
        "DB_HOST",
        "DB_USER",
        "DB_PASSWORD",
        "FLASK_SECRET_KEY",
    ]
    for key in required:
        v = vals.get(key, "")
        if not v or v.startswith("your_"):
            return True
    return False


# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------

app = Flask(
    __name__,
    template_folder=str(MAGI_ROOT / "templates" / "wizard"),
)
app.secret_key = secrets.token_hex(16)

# Global hardware info (cached)
_hw_cache: HardwareInfo | None = None


def _get_hw() -> HardwareInfo:
    global _hw_cache
    if _hw_cache is None:
        _hw_cache = detect_hardware()
    return _hw_cache


# ── Routes ──

@app.route("/")
def index():
    """Redirect to first step."""
    return redirect(url_for("step_welcome"))


@app.route("/welcome")
def step_welcome():
    return render_template("welcome.html", version=WIZARD_VERSION)


@app.route("/hardware")
def step_hardware():
    hw = _get_hw()
    return render_template("hardware.html", hw=hw)


@app.route("/api/hardware")
def api_hardware():
    hw = _get_hw()
    return jsonify(asdict(hw))


@app.route("/models")
def step_models():
    hw = _get_hw()
    recs = recommend_models(hw)
    installed = get_installed_models()
    return render_template(
        "models.html", hw=hw, recs=recs, installed=installed,
    )


@app.route("/api/install-model", methods=["POST"])
def api_install_model():
    """Install a model via oMLX or Ollama."""
    data = request.get_json(force=True)
    model_id = data.get("model_id", "")
    engine = data.get("engine", "omlx")

    if not model_id:
        return jsonify({"ok": False, "error": "No model_id provided"}), 400

    try:
        if engine == "omlx":
            # oMLX models are downloaded to ~/.omlx/models/ via huggingface
            # For now, provide instructions
            return jsonify({
                "ok": True,
                "message": f"請手動下載模型至 ~/.omlx/models/{model_id}",
                "instructions": (
                    f"git lfs install && "
                    f"git clone https://huggingface.co/mlx-community/{model_id} "
                    f"~/.omlx/models/{model_id}"
                ),
            })
        elif engine == "ollama":
            proc = subprocess.Popen(
                ["ollama", "pull", model_id],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            output = proc.communicate(timeout=600)[0]
            if proc.returncode == 0:
                return jsonify({"ok": True, "message": f"{model_id} 安裝完成"})
            else:
                return jsonify({"ok": False, "error": output}), 500
        else:
            return jsonify({"ok": False, "error": f"Unknown engine: {engine}"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/config")
def step_config():
    hw = _get_hw()
    return render_template("config.html", hw=hw)


@app.route("/review", methods=["POST"])
def step_review():
    """Review all collected configuration before generating .env."""
    config = request.form.to_dict()
    session["wizard_config"] = config
    env_preview = generate_env(config)
    return render_template("review.html", config=config, env_preview=env_preview)


@app.route("/apply", methods=["POST"])
def apply_config():
    """Write .env and mark setup as complete."""
    config = session.get("wizard_config", {})
    if not config:
        return redirect(url_for("step_config"))

    env_content = generate_env(config)

    # Backup existing .env if present
    if ENV_PATH.exists():
        backup = ENV_PATH.with_suffix(f".env.bak.{int(time.time())}")
        shutil.copy2(ENV_PATH, backup)
        logger.info("Backed up existing .env to %s", backup)

    ENV_PATH.write_text(env_content, encoding="utf-8")
    logger.info("Generated .env at %s", ENV_PATH)

    return render_template("complete.html", env_path=str(ENV_PATH))


@app.route("/api/test-db", methods=["POST"])
def api_test_db():
    """Test database connection."""
    data = request.get_json(force=True)
    host = data.get("host", "127.0.0.1")
    port = int(data.get("port", 3306))
    user = data.get("user", "")
    password = data.get("password", "")

    try:
        import mysql.connector
        conn = mysql.connector.connect(
            host=host, port=port, user=user, password=password,
            connect_timeout=5, use_pure=True,
        )
        ver = conn.get_server_info()
        conn.close()
        return jsonify({"ok": True, "message": f"連線成功 — MariaDB {ver}"})
    except ImportError:
        return jsonify({"ok": False, "error": "mysql-connector-python 未安裝"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/test-line", methods=["POST"])
def api_test_line():
    """Test LINE channel token validity."""
    data = request.get_json(force=True)
    token = data.get("token", "")
    if not token:
        return jsonify({"ok": False, "error": "未提供 token"}), 400
    try:
        import requests as req
        resp = req.get(
            "https://api.line.me/v2/bot/info",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            info = resp.json()
            return jsonify({
                "ok": True,
                "message": f"✓ Bot: {info.get('displayName', 'OK')}",
            })
        else:
            return jsonify({
                "ok": False,
                "error": f"LINE API 回應 {resp.status_code}",
            }), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="MAGI Setup Wizard")
    parser.add_argument("--check", action="store_true",
                        help="Check if setup is needed, exit 0=needed 1=done")
    parser.add_argument("--port", type=int, default=8199,
                        help="Port for wizard web UI (default: 8199)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't auto-open browser")
    args = parser.parse_args()

    if args.check:
        if needs_setup():
            print("MAGI needs first-time setup. Run: python3 setup_wizard.py")
            sys.exit(0)
        else:
            print("MAGI is already configured.")
            sys.exit(1)

    port = args.port
    url = f"http://127.0.0.1:{port}"

    print(f"""
╔══════════════════════════════════════════════╗
║     MAGI Setup Wizard v{WIZARD_VERSION}               ║
║     首次啟動設定引導                           ║
╠══════════════════════════════════════════════╣
║  Opening browser → {url:<25s}║
║  Press Ctrl+C to quit                        ║
╚══════════════════════════════════════════════╝
""")

    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
