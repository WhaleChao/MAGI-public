from __future__ import annotations

import json
import os
import re
import zipfile
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request, send_file
from flask_login import login_required

from api.osc.tw_legal_rag import (
    citation_check_against_tlr_bundle,
    search_practical_judgments_via_tlr,
    tlr_health,
    tw_legal_rag_base_url,
    tw_legal_rag_enabled,
)

raziel_bp = Blueprint("raziel", __name__)

DEFAULT_RAZIEL_ROOT = Path.home() / "Desktop" / "interpreter-judgment-classifier"
LEGACY_RAZIEL_ROOT = Path.home() / "Desktop" / "最高法院_通譯_TXT"
RAZIEL_LOCK = threading.Lock()
DELIVERY_ROOT_NAME = "判決捕捉與分類_交付資料"


def _has_classifier_script(path: Path) -> bool:
    return (path / "scripts" / "complete_interpreter_dataset.py").exists()


def _candidate_roots() -> list[Path]:
    candidates: list[Path] = []

    def add(path: Any) -> None:
        if not path:
            return
        candidate = Path(path).expanduser()
        if candidate not in candidates:
            candidates.append(candidate)

    add(os.environ.get("MAGI_RAZIEL_ROOT"))
    add(os.environ.get("INTERPRETER_JUDGMENT_BASE_DIR"))
    add(DEFAULT_RAZIEL_ROOT)
    for base in (Path.home() / "Desktop", Path.home() / "Downloads"):
        for name in (
            "interpreter-judgment-classifier",
            "interpreter-judgment-classifier-main",
            "interpreter-judgment-classifier-fresh",
            "interpreter-judgment-classifier-work",
        ):
            add(base / name)
            add(base / name / name)
        for candidate in sorted(base.glob("interpreter-judgment-classifier*")):
            add(candidate)
            if candidate.is_dir():
                for nested in sorted(candidate.glob("interpreter-judgment-classifier*")):
                    add(nested)
    add(LEGACY_RAZIEL_ROOT)
    return candidates


def _raziel_root() -> Path:
    configured = os.environ.get("MAGI_RAZIEL_ROOT")
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.exists() or _has_classifier_script(configured_path):
            return configured_path.resolve()
    for candidate in _candidate_roots():
        if _has_classifier_script(candidate):
            return candidate.expanduser().resolve()
    if configured:
        return Path(configured).expanduser().resolve()
    return DEFAULT_RAZIEL_ROOT.expanduser().resolve()


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


def _keyword_query_from_config(config: dict[str, Any]) -> str:
    query = str(config.get("keyword_query") or "").strip()
    if query:
        return query
    keywords = config.get("keywords")
    if isinstance(keywords, list):
        query = " ".join(str(x).strip() for x in keywords if str(x).strip())
    else:
        query = str(keywords or "").strip()
    return query or "通譯"


def _effective_rule_query(config: dict[str, Any]) -> str:
    keyword_query = _keyword_query_from_config(config)
    rule_query = str(config.get("rule_query") or "").strip()
    if rule_query == "通譯" and keyword_query != "通譯":
        rule_query = ""
    return rule_query or keyword_query


def _public_config(config: dict[str, Any]) -> dict[str, Any]:
    keyword_query = _keyword_query_from_config(config)
    effective_rule = _effective_rule_query({**config, "keyword_query": keyword_query})
    return {
        "keyword_query": keyword_query,
        "rule_query": "" if effective_rule == keyword_query else effective_rule,
        "effective_rule_query": effective_rule,
        "court_scopes": config.get("court_scopes") or config.get("courts") or ["最高法院"],
        "max_results": config.get("max_results") or config.get("max_api") or 2000,
        "keyword_text_dir_name": config.get("keyword_text_dir_name") or "依關鍵字原文",
        "keyword_pdf_dir_name": config.get("keyword_pdf_dir_name") or "依關鍵字PDF",
        "ai_provider": config.get("ai_provider") or "nvidia",
        "nvidia_model": config.get("nvidia_model") or "nvidia/nemotron-3-super-120b-a12b",
        "nvidia_large_fallback_model": config.get("nvidia_large_fallback_model") or "nvidia/nemotron-3-super-120b-a12b",
        "nvidia_fallback_model": config.get("nvidia_fallback_model") or "meta/llama-3.3-70b-instruct",
        "has_nvidia_api_key": bool(config.get("nvidia_api_key")),
        "tlr_enabled": tw_legal_rag_enabled(),
        "tlr_base_url": tw_legal_rag_base_url(),
    }


def _tlr_preview_for_config(config: dict[str, Any], *, limit: int = 3) -> dict[str, Any]:
    query = _keyword_query_from_config(config)
    if not tw_legal_rag_enabled():
        return {"ok": False, "enabled": False, "query": query, "error": "tw_legal_rag_disabled"}
    result = search_practical_judgments_via_tlr(
        query,
        limit=max(1, min(int(limit), 10)),
        fulltext_limit=1,
    )
    if not result.get("success"):
        return {
            "ok": False,
            "enabled": True,
            "query": result.get("query") or query,
            "error": result.get("error") or "no_tlr_matches",
            "source": result.get("source"),
        }
    bundle = result.get("bundle") if isinstance(result.get("bundle"), dict) else {}
    items = result.get("items") if isinstance(result.get("items"), list) else []
    citation_text = "\n".join(str(item.get("citation_text") or item.get("title") or "") for item in items if isinstance(item, dict))
    check = citation_check_against_tlr_bundle(citation_text, bundle) if bundle else {"overall": "needs_review"}
    return {
        "ok": True,
        "enabled": True,
        "query": result.get("query") or query,
        "source_label": result.get("source_label"),
        "count": len(items),
        "items": items,
        "bundle": bundle,
        "citation_check": check,
        "privacy_note": "TLR 只接收已去識別化的法律關鍵字；不要把當事人個資或完整案情放進搜尋式。",
    }


def _write_tlr_preview_file(preview: dict[str, Any]) -> str:
    try:
        target = _complete_dir() / "全判決語義檢索預覽.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(preview, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(target)
    except Exception:
        return ""


def _output_root() -> Path:
    root = _raziel_root()
    return root / "判決抓取與分類結果"


def _latest_project_payload() -> dict[str, Any]:
    pointer = _output_root() / "目前使用的搜尋專案.json"
    if not pointer.exists():
        return {}
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _complete_dir() -> Path:
    payload = _latest_project_payload()
    project_dir = payload.get("project_dir")
    if project_dir:
        path = Path(str(project_dir)).expanduser()
        if path.exists():
            return path.resolve()
    legacy = _raziel_root() / "完整812"
    if legacy.exists():
        return legacy
    return _output_root()


def _first_result_path(base: Path, *names: str) -> Path:
    paths = [base / name for name in names]
    for path in paths:
        if path.exists():
            return path
    return paths[0]


def _result_paths() -> dict[str, str]:
    complete = _complete_dir()
    return {
        "xlsx": str(_first_result_path(complete, "判決分類表.xlsx", "最高法院_通譯_分類表.xlsx")),
        "csv": str(_first_result_path(complete, "判決分類表.csv", "最高法院_通譯_分類表.csv")),
        "md": str(_first_result_path(complete, "判決分類表.md", "最高法院_通譯_分類表.md")),
        "preview": str(_first_result_path(complete, "規則前後文預覽.json")),
        "report": str(_first_result_path(complete, "判決補抓與分類報告.json", "通譯812補抓分析報告.json")),
        "tlr": str(_first_result_path(complete, "全判決語義檢索預覽.json")),
    }


def _delivery_dir() -> Path:
    return _complete_dir() / "交付壓縮檔"


def _delivery_split_bytes(value: Any = None) -> int:
    raw = value
    if raw is None or str(raw).strip() == "":
        raw = os.environ.get("MAGI_RAZIEL_DELIVERY_SPLIT_MB", "1900")
    try:
        mb = float(raw)
    except (TypeError, ValueError):
        mb = 1900.0
    return max(1, int(mb * 1024 * 1024))


def _delivery_source_specs(config: dict[str, Any]) -> list[tuple[Path, Path]]:
    complete = _complete_dir()
    result_names = {
        "xlsx": "分類表.xlsx",
        "csv": "分類表.csv",
        "md": "分類表.md",
        "preview": "前後文預覽.json",
        "report": "補抓分析報告.json",
        "tlr": "全判決語義檢索預覽.json",
    }
    specs: list[tuple[Path, Path]] = []
    for key, value in _result_paths().items():
        path = Path(value)
        if path.exists():
            specs.append((path, Path(result_names.get(key, path.name))))

    dir_specs = [
        ("TXT", "判決原文_TXT"),
        ("PDF", "判決PDF"),
        (str(config.get("keyword_text_dir_name") or "依關鍵字原文").strip() or "依關鍵字原文", "依關鍵字原文"),
        (str(config.get("keyword_pdf_dir_name") or "依關鍵字PDF").strip() or "依關鍵字PDF", "依關鍵字PDF"),
    ]
    for name, archive_name in dir_specs:
        path = complete / name
        if path.exists():
            specs.append((path, Path(archive_name)))
    return specs


def _delivery_sources(config: dict[str, Any]) -> list[Path]:
    return [source for source, _archive_name in _delivery_source_specs(config)]


def _safe_delivery_name(name: str) -> str:
    clean = Path(str(name or "")).name
    if not clean or clean in {".", ".."}:
        return ""
    return clean


def _write_delivery_zip(config: dict[str, Any], split_bytes: int) -> dict[str, Any]:
    delivery = _delivery_dir()
    delivery.mkdir(parents=True, exist_ok=True)
    for old in delivery.glob("判決捕捉與分類_交付_*.zip*"):
        if old.is_file():
            old.unlink()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = delivery / f"判決捕捉與分類_交付_{stamp}.zip"
    source_specs = _delivery_source_specs(config)
    seen: set[Path] = set()
    keyword_aliases: dict[tuple[str, str], str] = {}
    keyword_alias_rows: list[dict[str, str]] = []
    file_count = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for source, archive_name in source_specs:
            if source.is_file():
                files = [(source, archive_name)]
            else:
                files = []
                for path in source.rglob("*"):
                    if not path.is_file():
                        continue
                    rel = path.relative_to(source)
                    archive_rel = archive_name / rel
                    if str(archive_name) in {"依關鍵字原文", "依關鍵字PDF"} and len(rel.parts) > 1:
                        original_keyword = rel.parts[0]
                        alias_key = (str(archive_name), original_keyword)
                        if alias_key not in keyword_aliases:
                            keyword_aliases[alias_key] = f"關鍵字{len(keyword_aliases) + 1:02d}"
                            keyword_alias_rows.append(
                                {
                                    "資料夾": str(archive_name / keyword_aliases[alias_key]),
                                    "原關鍵字": original_keyword,
                                }
                            )
                        archive_rel = archive_name / keyword_aliases[alias_key] / Path(*rel.parts[1:])
                    files.append((path, archive_rel))
            for path, relative_archive_name in files:
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                zf.write(path, Path(DELIVERY_ROOT_NAME) / relative_archive_name)
                file_count += 1
        if keyword_alias_rows:
            zf.writestr(
                str(Path(DELIVERY_ROOT_NAME) / "關鍵字資料夾對照表.json"),
                json.dumps(keyword_alias_rows, ensure_ascii=False, indent=2),
            )
            file_count += 1
    size = zip_path.stat().st_size
    parts: list[dict[str, Any]] = []
    if size > split_bytes:
        with zip_path.open("rb") as fh:
            idx = 1
            while True:
                chunk = fh.read(split_bytes)
                if not chunk:
                    break
                part_path = delivery / f"{zip_path.name}.part{idx:03d}"
                part_path.write_bytes(chunk)
                parts.append(
                    {
                        "name": part_path.name,
                        "path": str(part_path),
                        "size": part_path.stat().st_size,
                        "url": f"/api/osc/raziel/delivery/{part_path.name}",
                    }
                )
                idx += 1
        zip_path.unlink(missing_ok=True)
        split = True
    else:
        parts.append(
            {
                "name": zip_path.name,
                "path": str(zip_path),
                "size": size,
                "url": f"/api/osc/raziel/delivery/{zip_path.name}",
            }
        )
        split = False
    manifest = {
        "ok": True,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "delivery_dir": str(delivery),
        "file_count": file_count,
        "split": split,
        "split_bytes": split_bytes,
        "parts": parts,
        "folder_name": DELIVERY_ROOT_NAME,
        "sources": [str(path) for path, _archive_name in source_specs],
    }
    manifest_path = delivery / "交付清單.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def _apply_payload_to_config(payload: dict[str, Any]) -> dict[str, Any]:
    config = _load_config()
    keyword_query = str(payload.get("keyword_query") or config.get("keyword_query") or "通譯").strip()
    if "rule_query" in payload:
        rule_query = str(payload.get("rule_query") or "").strip()
    else:
        rule_query = str(config.get("rule_query") or "").strip()
    if rule_query == "通譯" and keyword_query != "通譯":
        rule_query = ""
    effective_rule_query = rule_query or keyword_query or "通譯"
    stored_rule_query = "" if effective_rule_query == keyword_query else effective_rule_query
    courts = _split_lines_or_commas(payload.get("court_scopes")) or list(config.get("court_scopes") or ["最高法院"])
    try:
        max_results = int(payload.get("max_results") or config.get("max_results") or config.get("max_api") or 2000)
    except (TypeError, ValueError):
        max_results = 2000
    config.update(
        {
            "keyword_query": keyword_query,
            "keywords": _terms_from_query(keyword_query) or [keyword_query],
            "rule_query": stored_rule_query,
            "rule_keywords": _terms_from_query(effective_rule_query) or [effective_rule_query],
            "court_scopes": courts,
            "courts": courts,
            "max_results": max(1, max_results),
            "max_api": max(1, int(payload.get("max_api") or max_results)),
            "keyword_text_dir_name": str(payload.get("keyword_text_dir_name") or config.get("keyword_text_dir_name") or "依關鍵字原文").strip(),
            "keyword_pdf_dir_name": str(payload.get("keyword_pdf_dir_name") or config.get("keyword_pdf_dir_name") or "依關鍵字PDF").strip(),
            "ai_provider": str(payload.get("ai_provider") or config.get("ai_provider") or "nvidia").strip(),
            "nvidia_model": str(payload.get("nvidia_model") or config.get("nvidia_model") or "nvidia/nemotron-3-super-120b-a12b").strip(),
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
    env = os.environ.copy()
    env["INTERPRETER_JUDGMENT_BASE_DIR"] = str(root)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            env=env,
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
    if mode in {"status", "search", "preview", "table"}:
        tlr_preview = _tlr_preview_for_config(config, limit=int(os.environ.get("MAGI_RAZIEL_TLR_PREVIEW_LIMIT", "3") or "3"))
        parsed["tlr_semantic_preview"] = tlr_preview
        tlr_path = _write_tlr_preview_file(tlr_preview)
        if tlr_path:
            parsed["tlr_semantic_preview_path"] = tlr_path
    return {"ok": True, "mode": mode, "config": _public_config(config), "paths": _result_paths(), "result": parsed}


@raziel_bp.route("/api/osc/raziel/status", methods=["GET"])
@login_required
def raziel_status_api():
    root = _raziel_root()
    config = _load_config()
    paths = _result_paths()
    files = {key: {"path": value, "exists": Path(value).exists()} for key, value in paths.items()}
    script_path = _script_path()
    script_exists = script_path.exists()
    return jsonify(
        {
            "ok": True,
            "root": str(root),
            "script_path": str(script_path),
            "script_exists": script_exists,
            "configured_root": os.environ.get("MAGI_RAZIEL_ROOT") or "",
            "status_message": (
                "判決捕捉與分類器已連線。"
                if script_exists
                else "找不到判決捕捉與分類器的程式資料夾，請把下載的分類器資料夾放在桌面或下載資料夾。"
            ),
            "config": _public_config(config),
            "tlr": tlr_health(),
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


@raziel_bp.route("/api/osc/raziel/tlr-preview", methods=["POST"])
@login_required
def raziel_tlr_preview_api():
    payload = request.get_json() or {}
    config = _apply_payload_to_config(payload)
    preview = _tlr_preview_for_config(config, limit=int(payload.get("limit") or os.environ.get("MAGI_RAZIEL_TLR_PREVIEW_LIMIT", "3") or "3"))
    path = _write_tlr_preview_file(preview)
    if path:
        preview["path"] = path
    status = 200 if preview.get("ok") else 502
    return jsonify(preview), status


@raziel_bp.route("/api/osc/raziel/delivery", methods=["POST"])
@login_required
def raziel_delivery_api():
    payload = request.get_json() or {}
    with RAZIEL_LOCK:
        config = _apply_payload_to_config(payload)
        manifest = _write_delivery_zip(config, _delivery_split_bytes(payload.get("split_mb")))
    return jsonify(manifest)


@raziel_bp.route("/api/osc/raziel/delivery/<path:name>", methods=["GET"])
@login_required
def raziel_delivery_file_api(name: str):
    clean = _safe_delivery_name(name)
    if not clean:
        return jsonify({"ok": False, "error": "檔案不存在"}), 404
    path = (_delivery_dir() / clean).resolve()
    if not path.exists() or path.parent != _delivery_dir().resolve():
        return jsonify({"ok": False, "error": "檔案不存在"}), 404
    return send_file(str(path), as_attachment=True)


@raziel_bp.route("/api/osc/raziel/file/<kind>", methods=["GET"])
@login_required
def raziel_file_api(kind: str):
    paths = _result_paths()
    path = Path(paths.get(kind, "")).resolve()
    if kind not in paths or not path.exists():
        return jsonify({"ok": False, "error": "檔案不存在"}), 404
    return send_file(str(path), as_attachment=True)
