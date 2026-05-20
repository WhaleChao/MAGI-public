from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request, send_file
from flask_login import login_required

raziel_bp = Blueprint("raziel", __name__)

DEFAULT_RAZIEL_ROOT = Path(os.environ.get("MAGI_RAZIEL_ROOT", "/Users/ai/Desktop/最高法院_通譯_TXT")).expanduser()
RAZIEL_LOCK = threading.Lock()


def _raziel_root() -> Path:
    return Path(os.environ.get("MAGI_RAZIEL_ROOT", str(DEFAULT_RAZIEL_ROOT))).expanduser().resolve()


def _config_path() -> Path:
    return _raziel_root() / "config" / "app_config.json"


def _script_path() -> Path:
    return _raziel_root() / "scripts" / "complete_interpreter_dataset.py"


def _load_config() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_config(config: dict[str, Any]) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _split_lines_or_commas(value: Any) -> list[str]:
    text = str(value or "")
    out: list[str] = []
    for part in re.split(r"[\n,，;；]+", text):
        item = part.strip()
        if item and item not in out:
            out.append(item)
    return out


def _terms_from_query(query: str) -> list[str]:
    text = str(query or "")
    token_re = re.compile(r'"([^"]+)"|「([^」]+)」|『([^』]+)』|([()（）])|(\S+)')
    terms: list[str] = []
    skip_next = False
    for match in token_re.finditer(text):
        item = next((group for group in match.groups() if group), "").strip()
        if not item:
            continue
        upper = item.upper()
        if upper in {"AND", "OR"} or item in {"且", "或", "+", "(", ")", "（", "）"}:
            continue
        if upper == "NOT" or item in {"非", "-"}:
            skip_next = True
            continue
        if item.startswith("-"):
            continue
        item = item.lstrip("+").strip()
        if skip_next:
            skip_next = False
            continue
        if item and item not in terms:
            terms.append(item)
    return terms


def _public_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "keyword_query": config.get("keyword_query") or config.get("keywords") or "通譯",
        "rule_query": config.get("rule_query") or config.get("keyword_query") or "通譯",
        "court_scopes": config.get("court_scopes") or config.get("courts") or ["最高法院"],
        "max_results": config.get("max_results") or config.get("max_api") or 812,
        "keyword_text_dir_name": config.get("keyword_text_dir_name") or "依關鍵字原文",
        "keyword_pdf_dir_name": config.get("keyword_pdf_dir_name") or "依關鍵字PDF",
        "ai_provider": config.get("ai_provider") or "nvidia",
        "nvidia_model": config.get("nvidia_model") or "meta/llama-3.1-405b-instruct",
        "nvidia_large_fallback_model": config.get("nvidia_large_fallback_model") or "nvidia/nemotron-3-super-120b-a12b",
        "nvidia_fallback_model": config.get("nvidia_fallback_model") or "meta/llama-3.3-70b-instruct",
        "has_nvidia_api_key": bool(config.get("nvidia_api_key")),
    }


def _result_paths() -> dict[str, str]:
    root = _raziel_root()
    return {
        "xlsx": str(root / "完整812" / "最高法院_通譯_分類表.xlsx"),
        "csv": str(root / "完整812" / "最高法院_通譯_分類表.csv"),
        "md": str(root / "完整812" / "最高法院_通譯_分類表.md"),
        "preview": str(root / "完整812" / "規則前後文預覽.json"),
        "report": str(root / "完整812" / "通譯812補抓分析報告.json"),
    }


def _apply_payload_to_config(payload: dict[str, Any]) -> dict[str, Any]:
    config = _load_config()
    keyword_query = str(payload.get("keyword_query") or config.get("keyword_query") or "通譯").strip()
    rule_query = str(payload.get("rule_query") or config.get("rule_query") or keyword_query or "通譯").strip()
    courts = _split_lines_or_commas(payload.get("court_scopes")) or list(config.get("court_scopes") or ["最高法院"])
    try:
        max_results = int(payload.get("max_results") or config.get("max_results") or config.get("max_api") or 812)
    except (TypeError, ValueError):
        max_results = 812
    config.update(
        {
            "keyword_query": keyword_query,
            "keywords": _terms_from_query(keyword_query) or [keyword_query],
            "rule_query": rule_query,
            "rule_keywords": _terms_from_query(rule_query) or [rule_query],
            "court_scopes": courts,
            "courts": courts,
            "max_results": max(1, max_results),
            "max_api": max(1, int(payload.get("max_api") or max_results)),
            "keyword_text_dir_name": str(payload.get("keyword_text_dir_name") or config.get("keyword_text_dir_name") or "依關鍵字原文").strip(),
            "keyword_pdf_dir_name": str(payload.get("keyword_pdf_dir_name") or config.get("keyword_pdf_dir_name") or "依關鍵字PDF").strip(),
            "ai_provider": str(payload.get("ai_provider") or config.get("ai_provider") or "nvidia").strip(),
            "nvidia_model": str(payload.get("nvidia_model") or config.get("nvidia_model") or "meta/llama-3.1-405b-instruct").strip(),
            "nvidia_large_fallback_model": str(
                payload.get("nvidia_large_fallback_model")
                or config.get("nvidia_large_fallback_model")
                or "nvidia/nemotron-3-super-120b-a12b"
            ).strip(),
            "nvidia_fallback_model": str(
                payload.get("nvidia_fallback_model") or config.get("nvidia_fallback_model") or "meta/llama-3.3-70b-instruct"
            ).strip(),
        }
    )
    nvidia_api_key = str(payload.get("nvidia_api_key") or "").strip()
    if nvidia_api_key:
        config["nvidia_api_key"] = nvidia_api_key
    _save_config(config)
    return config


def _run_raziel(mode: str, config: dict[str, Any], max_api: int | None = None) -> dict[str, Any]:
    root = _raziel_root()
    script = _script_path()
    if not script.exists():
        return {"ok": False, "error": f"找不到判決分類核心腳本：{script}"}
    cmd = [sys.executable, str(script), "--mode", mode, "--no-zip"]
    if max_api:
        cmd.extend(["--max-api", str(max_api)])
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            text=True,
            capture_output=True,
            timeout=900 if mode in {"search", "nightly", "table"} else 240,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "判決分類器執行逾時，建議改用夜間補抓或降低筆數。"}
    output = (proc.stdout or "").strip()
    parsed: dict[str, Any] = {}
    if output:
        try:
            parsed = json.loads(output)
        except Exception:
            parsed = {"raw_output": output[-4000:]}
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": "判決分類器執行失敗",
            "stderr": (proc.stderr or "")[-4000:],
            "result": parsed,
        }
    parsed.setdefault("success", True)
    return {"ok": True, "mode": mode, "config": _public_config(config), "paths": _result_paths(), "result": parsed}


@raziel_bp.route("/api/osc/raziel/status", methods=["GET"])
@login_required
def raziel_status_api():
    root = _raziel_root()
    config = _load_config()
    paths = _result_paths()
    files = {key: {"path": value, "exists": Path(value).exists()} for key, value in paths.items()}
    return jsonify(
        {
            "ok": True,
            "root": str(root),
            "script_exists": _script_path().exists(),
            "config": _public_config(config),
            "files": files,
        }
    )


@raziel_bp.route("/api/osc/raziel/run", methods=["POST"])
@login_required
def raziel_run_api():
    payload = request.get_json() or {}
    mode = str(payload.get("mode") or "preview").strip()
    if mode not in {"status", "search", "preview", "nightly", "table"}:
        return jsonify({"ok": False, "error": "mode must be status/search/preview/nightly/table"}), 400
    with RAZIEL_LOCK:
        config = _apply_payload_to_config(payload)
        result = _run_raziel(mode, config, max_api=int(config.get("max_api") or 0) if mode in {"search", "nightly"} else None)
    status = 200 if result.get("ok") else 500
    return jsonify(result), status


@raziel_bp.route("/api/osc/raziel/file/<kind>", methods=["GET"])
@login_required
def raziel_file_api(kind: str):
    paths = _result_paths()
    path = Path(paths.get(kind, "")).resolve()
    if kind not in paths or not path.exists():
        return jsonify({"ok": False, "error": "檔案不存在"}), 404
    return send_file(str(path), as_attachment=True)
