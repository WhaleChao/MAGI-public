"""
Runtime-facing Web/API routes extracted from server.py.

This module owns lightweight dashboard-supporting routes that depend on
runtime objects injected from the main server bootstrap:
  - process monitor page + APIs
  - vector memory dashboard APIs
  - OSC chat/poll helper APIs
  - legacy judgments JSON compatibility API
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required


def _parse_etime_to_sec(raw: str) -> int:
    text = (raw or "").strip()
    if not text:
        return 0
    match = re.match(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$", text)
    if not match:
        return 0
    dd = int(match.group(1) or 0)
    hh = int(match.group(2) or 0)
    mm = int(match.group(3) or 0)
    ss = int(match.group(4) or 0)
    return (dd * 86400) + (hh * 3600) + (mm * 60) + ss


def _process_monitor_markers(magi_root: Path) -> tuple[list[str], list[str], dict[str, str]]:
    worker_markers = [
        "skills/judgment-collector/action.py",
        "skills/file-review-orchestrator/action.py",
        "skills/transcript-downloader/action.py",
        "skills/laf-portal-automation/action.py",
        "skills/laf-orchestrator/action.py",
        "skills/laf-withdrawal-report/action.py",
        "skills/laf-refine-case/action.py",
        "skills/osc-orchestrator/action.py",
        "skills/osc-scan-folder/action.py",
        "skills/pdf-namer/action.py",
        "skills/crawler-targets/action.py",
        "skills/statutes-vdb/action.py",
        "skills/magi-autopilot/action.py",
    ]
    try:
        from daemon import REAPER_NEVER_KILL as daemon_never_kill

        core_markers = list(daemon_never_kill)
    except Exception:
        core_markers = [
            f"{magi_root}/daemon.py",
            "api/server.py",
            "api/discord_bot.py",
            "skills/ops/openclaw_cron_runner.py",
            "openclaw-gateway",
            "rpc-server",
        ]
    core_labels = {
        f"{magi_root}/daemon.py": "Daemon",
        "api/server.py": "API/LINE Webhook",
        "api/discord_bot.py": "Discord Bot",
        "skills/ops/openclaw_cron_runner.py": "OpenClaw Cron Bridge",
        "openclaw-gateway": "OpenClaw Gateway",
        "rpc-server": "RPC Worker",
    }
    return worker_markers, core_markers, core_labels


def _collect_process_monitor(
    *,
    process_monitor_state_path: Path,
    magi_root: Path,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,etime=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=8,
        ).stdout or ""
        for raw in out.splitlines():
            line = (raw or "").strip()
            if not line:
                continue
            parts = line.split(None, 3)
            if len(parts) < 4:
                continue
            try:
                pid = int(parts[0])
                ppid = int(parts[1])
            except Exception:
                continue
            rows.append(
                {
                    "pid": pid,
                    "ppid": ppid,
                    "age_sec": _parse_etime_to_sec(parts[2]),
                    "age": parts[2],
                    "cmd": parts[3],
                }
            )
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "summary": {},
            "core": [],
            "workers": [],
            "orphans": [],
            "duplicates": [],
        }

    worker_markers, core_markers, core_labels = _process_monitor_markers(magi_root)
    core = []
    workers = []
    orphans = []
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in rows:
        cmd = str(row.get("cmd") or "")
        label = None
        for marker in core_markers:
            if marker in cmd:
                label = core_labels.get(marker, marker)
                break
        if label:
            entry = dict(row)
            entry["label"] = label
            core.append(entry)
        is_worker = any(marker in cmd for marker in worker_markers)
        if is_worker:
            workers.append(row)
            grouped[cmd].append(row)
            if int(row.get("ppid") or 0) == 1:
                orphans.append(row)

    duplicates = []
    for cmd, items in grouped.items():
        if len(items) <= 1:
            continue
        duplicates.append(
            {
                "count": len(items),
                "pids": [int(item["pid"]) for item in items],
                "cmd": cmd[:320],
            }
        )

    guardian_state: dict[str, Any] = {}
    try:
        if process_monitor_state_path.exists():
            guardian_state = json.loads(process_monitor_state_path.read_text(encoding="utf-8")) or {}
    except Exception:
        guardian_state = {}

    return {
        "ok": True,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "core_count": len(core),
            "worker_count": len(workers),
            "orphan_count": len(orphans),
            "duplicate_groups": len(duplicates),
        },
        "core": sorted(core, key=lambda x: (x.get("label", ""), x.get("pid", 0))),
        "workers": sorted(workers, key=lambda x: (x.get("age_sec", 0), x.get("pid", 0)), reverse=True),
        "orphans": sorted(orphans, key=lambda x: (x.get("age_sec", 0), x.get("pid", 0)), reverse=True),
        "duplicates": sorted(duplicates, key=lambda x: x.get("count", 0), reverse=True),
        "guardian_state": guardian_state,
    }


def create_web_runtime_blueprint(
    *,
    orchestrator: Any,
    logger: Any,
    web_notifications: dict[str, list[Any]],
    normalize_output_text=None,
    magi_root: str | Path | None = None,
) -> Blueprint:
    bp = Blueprint("web_runtime", __name__)
    root = Path(magi_root) if magi_root else Path(__file__).resolve().parents[2]
    agent_dir = root / ".agent"
    process_monitor_state_path = root / "static" / "process_guardian_state.json"
    guardian_control_path = root / "static" / "guardian_control.json"

    @bp.route("/ops/process-monitor")
    @login_required
    def process_monitor_page():
        return render_template("process_monitor.html", user=current_user)

    @bp.route("/api/memory/stats", methods=["GET"])
    @login_required
    def api_memory_stats():
        stats: dict[str, Any] = {"doc_count": 0, "last_ingest": None, "obsidian": {}, "faiss_size": 0}
        try:
            idx_path = agent_dir / "doc_vector_index.json"
            if idx_path.exists():
                idx = json.loads(idx_path.read_text(encoding="utf-8"))
                entries = idx if isinstance(idx, list) else list(idx.values()) if isinstance(idx, dict) else []
                stats["doc_count"] = len(entries)
                dates = [entry.get("updated_at") or entry.get("created_at", "") for entry in entries if isinstance(entry, dict)]
                dates = sorted([item for item in dates if item], reverse=True)
                if dates:
                    stats["last_ingest"] = dates[0]
        except Exception as exc:
            stats["doc_index_error"] = str(exc)
        try:
            obs_cfg = agent_dir / "obsidian_vault_config.json"
            obs_idx = agent_dir / "obsidian_index.json"
            if obs_cfg.exists():
                cfg = json.loads(obs_cfg.read_text(encoding="utf-8"))
                stats["obsidian"]["vault_path"] = cfg.get("vault_path", "")
                stats["obsidian"]["vault_name"] = cfg.get("vault_name", "")
            if obs_idx.exists():
                oidx = json.loads(obs_idx.read_text(encoding="utf-8"))
                stats["obsidian"]["notes_indexed"] = len((oidx.get("notes") or {}))
                stats["obsidian"]["last_update"] = oidx.get("updated_at", "")
        except Exception as exc:
            stats["obsidian_error"] = str(exc)
        try:
            faiss_path = root / "skills" / "memory" / "index_cache" / "mem_index.faiss"
            if faiss_path.exists():
                stats["faiss_size"] = faiss_path.stat().st_size
            meta_path = root / "skills" / "memory" / "index_cache" / "meta.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                stats["faiss_vector_count"] = meta.get("total", 0)
                stats["faiss_index_type"] = meta.get("index_type", "unknown")
                stats["faiss_last_updated"] = meta.get("updated", "")
        except Exception:
            logger.debug("silent-catch in api_memory_stats", exc_info=True)
        try:
            from skills.memory.faiss_index import FAISSMemoryIndex
            idx = FAISSMemoryIndex.get_instance()
            stats["faiss_vector_count"] = idx.total
            stats["faiss_index_type"] = idx.index_type
        except Exception:
            pass
        return jsonify(stats)

    @bp.route("/api/memory/recall", methods=["POST"])
    @login_required
    def api_memory_recall():
        data = request.get_json() or {}
        query = str(data.get("query", "")).strip()
        top_k = min(20, max(1, int(data.get("top_k", 5))))
        source_filter = str(data.get("source", "")).strip() or None
        if not query:
            return jsonify({"error": "請輸入搜尋關鍵字"}), 400
        try:
            from skills.memory.mem_bridge import recall

            results = recall(query, top_k=top_k, source_contains=source_filter)
            return jsonify({"memories": results or [], "query": query})
        except Exception as exc:
            logger.error("Memory recall error: %s", exc)
            return jsonify({"error": str(exc)}), 500

    @bp.route("/api/memory/remember", methods=["POST"])
    @login_required
    def api_memory_remember():
        data = request.get_json() or {}
        content = str(data.get("content", "")).strip()
        source = str(data.get("source", "dashboard-manual")).strip() or "dashboard-manual"
        if not content:
            return jsonify({"error": "請輸入要記憶的內容"}), 400
        if len(content) > 50000:
            return jsonify({"error": "內容過長（上限 50,000 字元）"}), 400
        try:
            from skills.memory.mem_bridge import remember

            remember(content, source)
            return jsonify({"success": True, "message": f"已儲存 {len(content)} 字元至向量記憶庫"})
        except Exception as exc:
            logger.error("Memory remember error: %s", exc)
            return jsonify({"error": str(exc)}), 500

    @bp.route("/api/memory/obsidian-sync", methods=["POST"])
    @login_required
    def api_memory_obsidian_sync():
        def _run_ingest():
            try:
                from skills.obsidian.action import task_ingest

                task_ingest({})
            except Exception as exc:
                logger.error("Obsidian ingest error: %s", exc)

        thread = threading.Thread(target=_run_ingest, daemon=True)
        thread.start()
        return jsonify({"success": True, "message": "Obsidian 重新索引已啟動（背景執行中）"})

    @bp.route("/api/ops/process-monitor", methods=["GET"])
    @login_required
    def process_monitor_api():
        data = _collect_process_monitor(
            process_monitor_state_path=process_monitor_state_path,
            magi_root=root,
        )
        ctrl_enabled = True
        if guardian_control_path.exists():
            try:
                ctrl_enabled = json.loads(guardian_control_path.read_text(encoding="utf-8")).get("enabled", True)
            except Exception:
                logger.debug("silent-catch in process_monitor_api", exc_info=True)
        data["guardian_control_enabled"] = ctrl_enabled
        return jsonify(data), 200 if data.get("ok") else 500

    @bp.route("/api/ops/process-guardian/toggle", methods=["POST"])
    @login_required
    def process_guardian_toggle_api():
        try:
            ctrl = {"enabled": True}
            if guardian_control_path.exists():
                ctrl = json.loads(guardian_control_path.read_text(encoding="utf-8"))
            ctrl["enabled"] = not ctrl.get("enabled", True)
            guardian_control_path.write_text(json.dumps(ctrl, ensure_ascii=False, indent=2), encoding="utf-8")
            return jsonify({"ok": True, "enabled": ctrl["enabled"]})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/osc/chat", methods=["POST"])
    @login_required
    def osc_chat_api():
        data = request.get_json() or {}
        msg = (data.get("message") or "").strip()
        if not msg:
            return jsonify({"error": "Empty message"}), 400
        reply = orchestrator.process_message(
            user_id=str(current_user.id),
            message=msg,
            platform="WEB",
            role=current_user.role,
        )
        try:
            if normalize_output_text:
                reply = normalize_output_text(str(reply or ""), platform="WEB")
        except Exception:
            logger.debug("silent-catch in osc_chat_api", exc_info=True)
        return jsonify({"reply": reply})

    @bp.route("/api/osc/poll", methods=["GET"])
    @login_required
    def osc_poll_api():
        uid = str(current_user.id)
        messages = []
        if uid in web_notifications:
            messages = list(web_notifications[uid])
            web_notifications[uid].clear()
        return jsonify({"messages": messages})

    @bp.route("/api/osc/judgments_legacy", methods=["GET"])
    @login_required
    def osc_judgments_api():
        try:
            json_path = root / "skills" / "judgment-collector" / "judgments.json"
            if json_path.exists():
                return jsonify(json.loads(json_path.read_text(encoding="utf-8")))
            return jsonify([])
        except Exception as exc:
            logger.error("Error serving judgments: %s", exc)
            return jsonify([])

    return bp
