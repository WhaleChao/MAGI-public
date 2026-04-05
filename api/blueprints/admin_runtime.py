"""
Administrative / runtime status routes extracted from server.py.

This module keeps Web dashboard support, NERV APIs, system health probes,
and audio transcription wiring, while receiving runtime dependencies from the
main server bootstrap.
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user, login_required


def create_admin_runtime_blueprint(
    *,
    logger: Any,
    orchestrator: Any,
    require_json_auth,
    list_skill_docs,
    nerv_skill_interview_user_id,
    extract_interview_skill_name,
    skill_doc_path,
    skill_action_path,
    skill_summary,
    nerv_product_runtime_payload,
    nerv_product_names,
    update_product_runtime,
    cloudflared_alive,
    server_start_time: float,
    attachment_job_queue,
    list_attachment_job_ids,
    read_attachment_job,
    expected_magi_api_key: str,
    db_config: dict[str, Any],
    mysql_connector: Any,
    safe_remove_tmp,
    magi_root: str | Path | None = None,
) -> Blueprint:
    bp = Blueprint("admin_runtime", __name__)
    root = Path(magi_root) if magi_root else Path(__file__).resolve().parents[2]
    static_dir = root / "static"
    agent_dir = root / ".agent"
    status_file = static_dir / "magi_status.json"
    server_log_path = agent_dir / "server.log"

    def _load_status_payload() -> dict[str, Any]:
        return json.loads(status_file.read_text(encoding="utf-8"))

    @bp.route("/dashboard/nerv/api/health")
    @login_required
    def nerv_api_health():
        import requests as _rq

        results: dict[str, Any] = {}

        def _check(name, fn):
            try:
                results[name] = fn()
            except Exception as exc:
                results[name] = {"status": "error", "detail": str(exc)[:120]}

        def _omlx():
            try:
                _omlx_url = os.environ.get("MAGI_OMLX_CHAT_URL", "http://127.0.0.1:11434")
                response = _rq.get(f"{_omlx_url}/v1/models", timeout=3)
                if response.status_code == 200:
                    models = [item.get("id", "?") for item in (response.json().get("data") or [])]
                    return {"status": "online", "models": models, "count": len(models)}
            except Exception:
                logger.debug("silent-catch in nerv_api_health omlx", exc_info=True)
            return {"status": "error", "detail": "unreachable"}

        def _ollama():
            try:
                response = _rq.get("http://127.0.0.1:11434/api/tags", timeout=2)
                if response.status_code == 200:
                    models = [item.get("name", "?") for item in (response.json().get("models") or [])]
                    return {"status": "online", "models": models, "count": len(models)}
            except Exception:
                logger.debug("silent-catch in nerv_api_health ollama", exc_info=True)
            return {"status": "retired", "detail": "已退役，推理走 oMLX"}

        def _melchior():
            return {"status": "local", "detail": "oMLX 本地推理"}

        def _balthasar():
            return {"status": "local", "detail": "oMLX 本地摘要"}

        def _watcher():
            return {"status": "retired", "detail": "由 Worldmonitor 取代"}

        def _mysql():
            try:
                conn = mysql_connector.connect(
                    host=os.environ.get("DB_HOST", "127.0.0.1"),
                    port=int(os.environ.get("DB_PORT", "3306")),
                    user=os.environ.get("DB_USER", "casper_service"),
                    password=os.environ.get("DB_PASSWORD") or os.environ.get("MAGI_REMOTE_DB_PASSWORD", ""),
                    connection_timeout=4,
                    use_pure=True,
                )
                conn.close()
                return {"status": "online"}
            except Exception as exc:
                return {"status": "error", "detail": str(exc)[:80]}

        def _cloudflared():
            try:
                if cloudflared_alive():
                    return {"status": "online"}
                return {"status": "offline"}
            except Exception:
                return {"status": "error", "detail": "check failed"}

        def _line_webhook():
            try:
                webhook = os.environ.get("MAGI_LINE_WEBHOOK_ENDPOINT", "")
                if not webhook:
                    return {"status": "offline", "detail": "no endpoint configured"}
                response = _rq.get(webhook.replace("/line/webhook", "/health"), timeout=5)
                return {"status": "online" if response.status_code == 200 else "error"}
            except Exception:
                return {"status": "error", "detail": "unreachable"}

        def _worldmonitor():
            try:
                import subprocess as _sp

                result = _sp.run(["pgrep", "-f", "worldmonitor"], capture_output=True, timeout=3)
                return {"status": "online" if result.returncode == 0 else "offline"}
            except Exception:
                return {"status": "error"}

        def _office_app():
            try:
                response = _rq.get("http://127.0.0.1:4200/office", timeout=4)
                return {"status": "online" if response.status_code == 200 else "error", "detail": f"HTTP {response.status_code}"}
            except Exception as exc:
                return {"status": "error", "detail": str(exc)[:80]}

        def _caddy_proxy():
            return {"status": "skipped", "detail": "removed (direct cloudflared→5002)"}

        def _skills():
            docs = list_skill_docs()
            found = [
                item["name"]
                for item in docs
                if not item["name"].startswith(("_", "."))
                and item["name"] not in {"bridge", "ops", "memory", "evolution", "brain_manager"}
            ]
            return {"status": "online", "skills": found, "count": len(found)}

        checks = {
            "omlx": _omlx,
            "ollama": _ollama,
            "melchior": _melchior,
            "balthasar": _balthasar,
            "watcher": _watcher,
            "mysql": _mysql,
            "cloudflared": _cloudflared,
            "line_webhook": _line_webhook,
            "worldmonitor": _worldmonitor,
            "office_app": _office_app,
            "caddy_proxy": _caddy_proxy,
            "skills": _skills,
        }
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {name: pool.submit(fn) for name, fn in checks.items()}
            for name, future in futures.items():
                try:
                    results[name] = future.result(timeout=8)
                except Exception as exc:
                    results[name] = {"status": "error", "detail": str(exc)[:80]}

        results["magi_server"] = {"status": "online", "pid": os.getpid()}
        results["timestamp"] = datetime.now().isoformat()
        return jsonify(results)

    @bp.route("/api/system-test", methods=["POST"])
    @login_required
    def api_system_test():
        try:
            from skills.ops.system_test import run_all_tests

            return jsonify(run_all_tests())
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @bp.route("/api/self-repair", methods=["POST"])
    @login_required
    def api_self_repair():
        try:
            data = request.get_json() or {}
            targets = data.get("targets")
            base_dir = root / "skills"
            candidates = [
                base_dir / "magi-self-repair" / "action.py",
                base_dir / "magi-doctor" / "action.py",
            ]
            repair_mod = None
            for action_path in candidates:
                if not action_path.exists():
                    continue
                spec = importlib.util.spec_from_file_location("magi_self_repair", action_path)
                if spec is None or spec.loader is None:
                    continue
                repair_mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(repair_mod)
                break
            if repair_mod is None:
                raise FileNotFoundError("No self-repair module found. Tried: " + ", ".join(str(item) for item in candidates))
            if not hasattr(repair_mod, "repair_targets"):
                raise AttributeError("self-repair module missing repair_targets()")
            return jsonify(repair_mod.repair_targets(targets))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @bp.route("/api/nerv/skill-interview", methods=["GET"])
    def api_nerv_skill_interview_status():
        auth_error = require_json_auth()
        if auth_error:
            return auth_error
        try:
            state = orchestrator.get_skill_interview_state(nerv_skill_interview_user_id(), "NERV")
            return jsonify(
                {
                    "ok": True,
                    "can_edit": bool(getattr(current_user, "is_admin", False)),
                    "interview": state,
                }
            )
        except Exception as exc:
            logger.error("NERV skill interview status failed: %s", exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/nerv/skill-interview/start", methods=["POST"])
    def api_nerv_skill_interview_start():
        auth_error = require_json_auth(admin=True)
        if auth_error:
            return auth_error
        payload = request.get_json(silent=True) or {}
        initial_request = str(payload.get("request") or "").strip()
        if not initial_request:
            return jsonify({"ok": False, "error": "empty_request"}), 400
        try:
            message = orchestrator.start_skill_interview(
                nerv_skill_interview_user_id(),
                "NERV",
                getattr(current_user, "role", "user"),
                initial_request,
                trigger_reason="manual",
            )
            state = orchestrator.get_skill_interview_state(nerv_skill_interview_user_id(), "NERV")
            return jsonify({"ok": True, "message": message, "interview": state})
        except Exception as exc:
            logger.error("NERV skill interview start failed: %s", exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/nerv/skill-interview/reply", methods=["POST"])
    def api_nerv_skill_interview_reply():
        auth_error = require_json_auth(admin=True)
        if auth_error:
            return auth_error
        payload = request.get_json(silent=True) or {}
        reply_text = str(payload.get("message") or "").strip()
        if not reply_text:
            return jsonify({"ok": False, "error": "empty_message"}), 400
        try:
            handled, message = orchestrator.reply_skill_interview(
                nerv_skill_interview_user_id(),
                "NERV",
                getattr(current_user, "role", "user"),
                reply_text,
            )
            if not handled:
                return jsonify({"ok": False, "error": "no_active_interview"}), 400
            state = orchestrator.get_skill_interview_state(nerv_skill_interview_user_id(), "NERV")
            finalized = (not state.get("active")) and ("新 SKILL 已建立並啟用" in str(message or ""))
            cancelled = (not state.get("active")) and ("已取消這次 SKILL 訪談" in str(message or ""))
            return jsonify(
                {
                    "ok": True,
                    "message": message,
                    "interview": state,
                    "finalized": finalized,
                    "cancelled": cancelled,
                    "skill_name": extract_interview_skill_name(message),
                }
            )
        except Exception as exc:
            logger.error("NERV skill interview reply failed: %s", exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/skills/interview-history", methods=["GET"])
    def api_skill_interview_history():
        auth_error = require_json_auth()
        if auth_error:
            return auth_error
        limit = request.args.get("limit", default=10, type=int) or 10
        limit = max(1, min(limit, 50))
        try:
            from skills.management.skill_interview import list_interview_history

            return jsonify({"ok": True, "history": list_interview_history(limit=limit)})
        except Exception as exc:
            logger.error("Skill interview history failed: %s", exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/skills/<skill_name>/versions", methods=["GET"])
    def api_skill_versions(skill_name):
        auth_error = require_json_auth()
        if auth_error:
            return auth_error
        try:
            skill_doc_path(skill_name)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        try:
            from skills.evolution.skill_genesis import list_skill_versions

            result = list_skill_versions(str(skill_name).strip())
            if not result.get("success"):
                return jsonify({"ok": False, "error": result.get("error") or "versions_unavailable"}), 404
            return jsonify({"ok": True, "versions": result.get("versions") or []})
        except Exception as exc:
            logger.error("Skill versions failed for %s: %s", skill_name, exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/skills/<skill_name>/rollback", methods=["POST"])
    def api_skill_rollback(skill_name):
        auth_error = require_json_auth(admin=True)
        if auth_error:
            return auth_error
        try:
            skill_doc_path(skill_name)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        payload = request.get_json(silent=True) or {}
        version_id = str(payload.get("version_id") or "").strip()
        try:
            from skills.evolution.skill_genesis import rollback_skill_version
            from skills.bridge.embedding_router import get_router
            import skills.bridge.semantic_router as semantic_router

            result = rollback_skill_version(str(skill_name).strip(), version_id=version_id)
            if not result.get("success"):
                return jsonify({"ok": False, "error": result.get("error") or "rollback_failed"}), 400
            try:
                router = get_router()
                if router.is_ready:
                    router.rebuild_cache()
                else:
                    router.initialize()
            except Exception:
                logger.debug("silent-catch in api_skill_rollback router", exc_info=True)
            try:
                semantic_router._SKILLS_CACHE = None
                semantic_router._SKILLS_CACHE_TS = 0.0
            except Exception:
                logger.debug("silent-catch in api_skill_rollback semantic cache", exc_info=True)
            return jsonify({"ok": True, "result": result})
        except Exception as exc:
            logger.error("Skill rollback failed for %s: %s", skill_name, exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/nerv/skills", methods=["GET"])
    def api_nerv_skills():
        auth_error = require_json_auth()
        if auth_error:
            return auth_error
        try:
            return jsonify({"ok": True, "skills": list_skill_docs()})
        except Exception as exc:
            logger.error("NERV skill list failed: %s", exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/nerv/product-runtime", methods=["GET", "POST"])
    def api_nerv_product_runtime():
        auth_error = require_json_auth(admin=request.method == "POST")
        if auth_error:
            return auth_error
        if request.method == "GET":
            try:
                return jsonify(nerv_product_runtime_payload())
            except Exception as exc:
                logger.error("NERV product runtime load failed: %s", exc, exc_info=True)
                return jsonify({"ok": False, "error": str(exc)}), 500

        payload = request.get_json(silent=True) or {}
        product = str(payload.get("product") or "").strip().lower()
        if product not in nerv_product_names:
            return jsonify({"ok": False, "error": "unsupported_product"}), 400
        allowed_keys = {"codex_mode"}
        if product == "laf":
            allowed_keys |= {"portal_env", "prod_base_url", "test_base_url", "compare_base_url"}
        updates = {}
        for key in allowed_keys:
            value = payload.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            updates[key] = text
        if not updates:
            return jsonify({"ok": False, "error": "empty_updates"}), 400
        try:
            updated = update_product_runtime(product, **updates)
            response = nerv_product_runtime_payload()
            response["updated_product"] = product
            response["updated_profile"] = updated
            return jsonify(response)
        except Exception as exc:
            logger.error("NERV product runtime save failed: %s", exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/nerv/skills/<skill_name>", methods=["GET", "POST"])
    def api_nerv_skill_detail(skill_name):
        auth_error = require_json_auth(admin=request.method != "GET")
        if auth_error:
            return auth_error
        try:
            skill_doc = skill_doc_path(skill_name)
            action_file = skill_action_path(skill_name)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        if request.method == "GET":
            exists = skill_doc.exists()
            content = ""
            if exists:
                try:
                    content = skill_doc.read_text(encoding="utf-8")
                except Exception as exc:
                    return jsonify({"ok": False, "error": f"read_failed: {exc}"}), 500
            updated_at = ""
            stat_target = skill_doc if exists else action_file
            if stat_target.exists():
                try:
                    updated_at = datetime.fromtimestamp(stat_target.stat().st_mtime).isoformat()
                except Exception:
                    updated_at = ""
            return jsonify(
                {
                    "ok": True,
                    "skill": {
                        "name": str(skill_name).strip(),
                        "content": content,
                        "has_skill_doc": exists,
                        "has_action": action_file.exists(),
                        "updated_at": updated_at,
                        "summary": skill_summary(content),
                    },
                }
            )

        payload = request.get_json(silent=True) or {}
        content = str(payload.get("content") or "")
        if not content.strip():
            return jsonify({"ok": False, "error": "empty_skill_content"}), 400
        try:
            skill_doc.parent.mkdir(parents=True, exist_ok=True)
            normalized = content.replace("\r\n", "\n")
            if not normalized.endswith("\n"):
                normalized += "\n"
            skill_doc.write_text(normalized, encoding="utf-8")
        except Exception as exc:
            logger.error("NERV skill save failed for %s: %s", skill_name, exc, exc_info=True)
            return jsonify({"ok": False, "error": f"save_failed: {exc}"}), 500

        return jsonify(
            {
                "ok": True,
                "saved": True,
                "skill": {
                    "name": str(skill_name).strip(),
                    "content": normalized,
                    "has_skill_doc": True,
                    "has_action": action_file.exists(),
                    "updated_at": datetime.now().isoformat(),
                    "summary": skill_summary(normalized),
                },
            }
        )

    @bp.route("/api/codex-distributed/status", methods=["GET"])
    def api_codex_distributed_status():
        auth_error = require_json_auth()
        if auth_error:
            return auth_error
        try:
            from skills.bridge.llm_direct import public_status_report

            return jsonify({"status": public_status_report(), "can_toggle": current_user.is_admin()})
        except Exception as exc:
            logger.error("Codex distributed status failed: %s", exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/codex-distributed/toggle", methods=["POST"])
    def api_codex_distributed_toggle():
        auth_error = require_json_auth(admin=True)
        if auth_error:
            return auth_error
        try:
            from skills.bridge.llm_direct import apply_manual_command, public_status_report

            payload = request.get_json(silent=True) or {}
            command = str(payload.get("command") or "").strip().lower()
            features = payload.get("features")
            apply_manual_command(command, features=features)
            return jsonify({"status": public_status_report(), "can_toggle": True})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            logger.error("Codex distributed toggle failed: %s", exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/status")
    def api_status():
        try:
            return _load_status_payload()
        except Exception as exc:
            return {"error": str(exc)}, 500

    @bp.route("/api/live-log")
    @login_required
    def api_live_log():
        limit = min(int(request.args.get("limit", 40)), 100)
        lines = []
        try:
            with server_log_path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                read_size = min(size, 32768)
                handle.seek(size - read_size)
                raw = handle.read().decode("utf-8", errors="replace")
                lines = raw.strip().splitlines()[-limit:]
        except Exception as exc:
            lines = [f"[LOG READ ERROR] {exc}"]
        return jsonify({"lines": lines})

    @bp.route("/health", methods=["GET"])
    def health():
        import time as _time
        from skills.bridge.http_pool import get_session as _get_session

        sess = _get_session()
        checks: dict[str, Any] = {"status": "operational", "timestamp": _time.time()}

        try:
            _chat_url = os.environ.get("MAGI_OMLX_CHAT_URL", "http://127.0.0.1:11434")
            response = sess.get(f"{_chat_url}/v1/models", timeout=3)
            models = [item.get("id", "") for item in (response.json() or {}).get("data", [])]
            checks["omlx"] = {"ok": response.status_code == 200, "models": models}
        except Exception:
            checks["omlx"] = {"ok": False}

        conn = None
        try:
            conn = mysql_connector.connect(**db_config, connection_timeout=3, use_pure=True)
            checks["db"] = {"ok": conn.is_connected()}
        except Exception as exc:
            checks["db"] = {"ok": False, "detail": str(exc)[:80]}
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    logger.debug("silent-catch in health db close", exc_info=True)

        try:
            import psutil

            vm = psutil.virtual_memory()
            du = psutil.disk_usage("/")
            checks["system"] = {
                "cpu_percent": psutil.cpu_percent(interval=0.1),
                "memory_percent": vm.percent,
                "memory_available_gb": round(vm.available / (1024**3), 1),
                "disk_percent": du.percent,
                "disk_free_gb": round(du.free / (1024**3), 1),
            }
        except Exception:
            logger.debug("silent-catch in health system", exc_info=True)

        uptime = _time.time() - server_start_time
        if uptime < 60:
            checks["faiss"] = {"ok": True, "deferred": True, "reason": "startup_grace_period"}
        else:
            try:
                from skills.memory.faiss_index import FAISSMemoryIndex

                idx = FAISSMemoryIndex.get_instance()
                checks["faiss"] = {"ok": True, "vectors": getattr(idx, "total", getattr(idx, "ntotal", 0))}
            except Exception:
                checks["faiss"] = {"ok": False}

        try:
            if attachment_job_queue:
                checks["attachment_jobs"] = attachment_job_queue.stats()
            else:
                job_ids = list_attachment_job_ids()
                pending = sum(1 for job_id in job_ids if read_attachment_job(job_id).get("status") in ("queued", "running"))
                checks["attachment_jobs"] = {"total": len(job_ids), "active": pending}
        except Exception:
            logger.debug("silent-catch in health attachment_jobs", exc_info=True)

        try:
            from api.nas_mount_guard import _SHARES, _is_mounted

            checks["nas"] = {vol.split("/")[-1]: _is_mounted(vol) for _, vol in _SHARES}
        except Exception:
            logger.debug("silent-catch in health nas", exc_info=True)

        try:
            checks["uptime_seconds"] = round(_time.time() - server_start_time, 0)
        except Exception:
            logger.debug("silent-catch in health uptime", exc_info=True)

        try:
            from skills.bridge.tier_router import get_status as _tier_st
            checks["tier"] = _tier_st()
        except Exception:
            pass

        checks["status"] = "operational" if checks.get("omlx", {}).get("ok") else "degraded"
        return jsonify(checks), 200

    @bp.route("/api/transcribe", methods=["POST"])
    def transcribe_audio():
        import hmac

        api_key = (request.headers.get("X-MAGI-API-KEY") or "").strip()
        api_key_ok = bool(expected_magi_api_key) and hmac.compare_digest(api_key, expected_magi_api_key)
        if not api_key_ok and not current_user.is_authenticated:
            return jsonify({"error": "Unauthorized"}), 401
        if "file" not in request.files:
            return jsonify({"error": "No file part"}), 400
        file = request.files["file"]
        if file.filename == "":
            return jsonify({"error": "No selected file"}), 400
        try:
            safe_filename = "".join([char for char in file.filename if char.isalnum() or char in "._-"]) or "audio.wav"
            filename = f"audio_{int(time.time())}_{safe_filename}"
            filepath = os.path.join("/tmp", filename)
            file.save(filepath)
            logger.info("🎤 Received audio for transcription: %s", filepath)

            from skills.bridge.balthasar_bridge import transcribe

            language = str(request.form.get("language") or "").strip() or None
            taigi_hint_raw = str(request.form.get("taigi_hint") or "").strip().lower()
            taigi_hint = taigi_hint_raw in {"1", "true", "yes", "on"}
            result = transcribe(filepath, language=language, taigi_hint=taigi_hint)
            if os.path.exists(filepath):
                safe_remove_tmp(filepath)
            return jsonify(result)
        except Exception as exc:
            logger.error("❌ Transcription endpoint error: %s", exc)
            return jsonify({"error": str(exc)}), 500

    return bp
