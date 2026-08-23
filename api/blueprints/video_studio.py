"""Public, bounded, memory-only adapter for video-autopilot-kit."""
from __future__ import annotations

import hashlib
import io
import json
import multiprocessing
import os
import signal
import tempfile
import threading
import time
from pathlib import Path

from flask import Blueprint, jsonify, make_response, render_template, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge

from api.durable_rate_limit import DurableRateLimiter, check_rate_limit
from magi_v3.video_autopilot_adapter import (
    AssetInput,
    ENGINE_CONTRACT,
    UPSTREAM_COMMIT,
    UPSTREAM_VERSION,
    VideoAutopilotError,
    interpret_edit_instructions,
    public_edit_plan,
    render_asset_storyboard,
    render_storyboard,
    validate_asset_storyboard_request,
    validate_storyboard_request,
)


video_studio_bp = Blueprint("video_studio", __name__)

MAX_JSON_BYTES = 16 * 1024
MAX_ASSET_BYTES = 24 * 1024 * 1024
MAX_ASSET_REQUEST_BYTES = 64 * 1024 * 1024
MAX_ASSET_COUNT = 5
MAX_REQUESTS_PER_MINUTE = 2
MAX_RENDER_SECONDS = 70
MAX_RENDER_RSS_BYTES = 1536 * 1024 * 1024
_render_slot = threading.BoundedSemaphore(1)
_rate_limiter = DurableRateLimiter(limits={"video_studio": MAX_REQUESTS_PER_MINUTE})
_ERROR_MESSAGES = {
    "invalid_request_schema": "請使用畫面提供的欄位建立短影音。",
    "invalid_title": "主標題需為 1 至 36 個字。",
    "invalid_subtitle": "副標題最多 64 個字。",
    "invalid_scenes": "請填寫 2 至 5 段不同的影片內容，每行一幕、每幕最多 88 字。",
    "invalid_edit_instruction": "剪輯指令格式不正確。",
    "unsupported_edit_instruction": "有一段剪輯指令無法理解；請依畫面列出的用語重新描述。",
    "conflicting_edit_instruction": (
        "剪輯指令互相矛盾，請只保留一種順序、轉場、運鏡與音訊設定。"
    ),
    "edit_plan_mismatch": "剪輯理解結果已改變，請重新確認後再生成。",
    "invalid_media_asset": (
        "素材只接受有效的 JPG、PNG、WebP、MP4 或 MOV，且必須符合大小與解析度限制。"
    ),
    "asset_scene_count_mismatch": "素材數量必須與分鏡行數相同。",
    "invalid_palette": "請選擇有效的影片配色。",
    "invalid_duration": "影片長度只接受 6、9 或 12 秒。",
    "video_resource_limit": "影片處理超過本機資源限制，請稍後再試。",
    "video_cleanup_failed": "影片工作程序未能安全回收，請稍後再試。",
}


def _plain_error(code: str, status: int):
    response = jsonify({
        "ok": False,
        "error": code,
        "message": _ERROR_MESSAGES.get(code, "影片暫時無法產生，請稍後再試。"),
    })
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _client_key() -> str:
    return str(request.remote_addr or "anonymous")


def _render_child(
    connection, work_dir: str, payload: dict, instructions: str | None,
) -> None:
    try:
        # Keep every ffmpeg descendant in one private process group so the
        # parent can terminate and prove cleanup after a timeout or crash.
        os.setsid()
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_CPU, (MAX_RENDER_SECONDS, MAX_RENDER_SECONDS + 2))
            resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        except (ImportError, OSError, ValueError):
            connection.send(("error", "video_resource_limit"))
            return
        plan = interpret_edit_instructions(instructions or "")
        media, attestation = render_storyboard(
            Path(work_dir), validate_storyboard_request(payload), plan,
        )
        connection.send(("ok", media, attestation))
    except VideoAutopilotError as exc:
        connection.send(("error", str(exc)))
    except BaseException:
        connection.send(("error", "video_engine_failed"))
    finally:
        connection.close()


def _render_asset_child(
    connection, work_dir: str, payload: dict, descriptors: tuple[tuple[str, str, str], ...], instructions: str,
) -> None:
    try:
        os.setsid()
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_CPU, (MAX_RENDER_SECONDS, MAX_RENDER_SECONDS + 2))
            resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        except (ImportError, OSError, ValueError):
            connection.send(("error", "video_resource_limit"))
            return
        request_contract = validate_asset_storyboard_request(payload)
        plan = interpret_edit_instructions(instructions)
        assets = tuple(
            AssetInput(Path(work_dir) / relative, kind, digest)
            for relative, kind, digest in descriptors
        )
        media, attestation = render_asset_storyboard(Path(work_dir), request_contract, assets, plan)
        connection.send(("ok", media, attestation))
    except VideoAutopilotError as exc:
        connection.send(("error", str(exc)))
    except BaseException:
        connection.send(("error", "video_engine_failed"))
    finally:
        connection.close()


def _controller_rss(process) -> int:
    return int(process.memory_info().rss)


def _process_group_absent(pgid: int) -> bool:
    for _attempt in range(20):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _render_bounded(
    payload: dict, instructions: str | None = None,
) -> tuple[bytes, dict]:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    worker_started = False
    peak_rss = 0
    sample_count = 0
    with tempfile.TemporaryDirectory(prefix="magi-video-studio-") as work_dir:
        worker = context.Process(
            target=_render_child,
            args=(child, work_dir, dict(payload), instructions),
            name="magi-video-studio-render",
        )
        process = None
        started = time.monotonic()
        try:
            worker.start()
            worker_started = True
            child.close()
            import psutil

            process = psutil.Process(worker.pid)
            deadline = started + MAX_RENDER_SECONDS
            while True:
                if worker.is_alive():
                    observed = _controller_rss(process)
                    if observed <= 0:
                        raise VideoAutopilotError("video_resource_limit")
                    peak_rss = max(peak_rss, observed)
                    sample_count += 1
                    if peak_rss > MAX_RENDER_RSS_BYTES:
                        raise VideoAutopilotError("video_resource_limit")
                if parent.poll(0.05):
                    break
                if time.monotonic() >= deadline:
                    raise VideoAutopilotError("video_resource_limit")
                if not worker.is_alive():
                    raise VideoAutopilotError("video_engine_failed")
            result = parent.recv()
            if (
                isinstance(result, tuple)
                and len(result) == 3
                and result[0] == "ok"
                and isinstance(result[1], bytes)
                and isinstance(result[2], dict)
            ):
                if sample_count <= 0 or peak_rss <= 0:
                    raise VideoAutopilotError("video_resource_limit")
                attestation = dict(result[2])
                attestation.update({
                    "render_seconds": round(time.monotonic() - started, 3),
                    "controller_peak_rss_bytes": peak_rss,
                    "per_process_rss_limit_bytes": MAX_RENDER_RSS_BYTES,
                })
                return result[1], attestation
            if isinstance(result, tuple) and len(result) == 2 and result[0] == "error":
                raise VideoAutopilotError(str(result[1]))
            raise VideoAutopilotError("video_engine_failed")
        except (EOFError, OSError) as exc:
            raise VideoAutopilotError("video_engine_failed") from exc
        finally:
            try:
                parent.close()
            except OSError:
                pass
            try:
                child.close()
            except OSError:
                pass
            if worker_started:
                worker.join(0.3)
                if worker.is_alive():
                    try:
                        os.killpg(worker.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    worker.join(2.0)
                if worker.is_alive():
                    try:
                        os.killpg(worker.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    worker.join(2.0)
                if worker.is_alive() or worker.exitcode is None:
                    raise VideoAutopilotError("video_cleanup_failed")
                if not _process_group_absent(worker.pid):
                    raise VideoAutopilotError("video_cleanup_failed")


def _write_private(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _bounded_uploads() -> tuple[tuple[str, str, bytes, str], ...]:
    uploads = request.files.getlist("assets")
    if not 1 <= len(uploads) <= MAX_ASSET_COUNT:
        raise VideoAutopilotError("asset_scene_count_mismatch")
    rows: list[tuple[str, str, bytes, str]] = []
    total = 0
    for upload in uploads:
        data = upload.stream.read(MAX_ASSET_BYTES + 1)
        if not data or len(data) > MAX_ASSET_BYTES:
            raise VideoAutopilotError("invalid_media_asset")
        total += len(data)
        if total > MAX_ASSET_REQUEST_BYTES:
            raise VideoAutopilotError("invalid_media_asset")
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            kind, suffix = "image", ".png"
        elif data.startswith(b"\xff\xd8\xff"):
            kind, suffix = "image", ".jpg"
        elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind, suffix = "image", ".webp"
        elif len(data) >= 12 and data[4:8] == b"ftyp":
            kind, suffix = "video", ".mp4"
        else:
            raise VideoAutopilotError("invalid_media_asset")
        rows.append((kind, suffix, data, hashlib.sha256(data).hexdigest()))
    return tuple(rows)


def _render_assets_bounded(
    payload: dict, uploads: tuple[tuple[str, str, bytes, str], ...], instructions: str,
) -> tuple[bytes, dict]:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    worker_started = False
    peak_rss = 0
    sample_count = 0
    with tempfile.TemporaryDirectory(prefix="magi-video-assets-") as work_dir_value:
        work_dir = Path(work_dir_value)
        descriptors: list[tuple[str, str, str]] = []
        for index, (kind, suffix, data, digest) in enumerate(uploads):
            relative = f"asset-{index:02d}{suffix}"
            _write_private(work_dir / relative, data)
            descriptors.append((relative, kind, digest))
        worker = context.Process(
            target=_render_asset_child,
            args=(child, work_dir_value, dict(payload), tuple(descriptors), instructions),
            name="magi-video-assets-render",
        )
        process = None
        started = time.monotonic()
        try:
            worker.start()
            worker_started = True
            child.close()
            import psutil

            process = psutil.Process(worker.pid)
            deadline = started + MAX_RENDER_SECONDS
            while True:
                if worker.is_alive():
                    observed = _controller_rss(process)
                    if observed <= 0:
                        raise VideoAutopilotError("video_resource_limit")
                    peak_rss = max(peak_rss, observed)
                    sample_count += 1
                    if peak_rss > MAX_RENDER_RSS_BYTES:
                        raise VideoAutopilotError("video_resource_limit")
                if parent.poll(0.05):
                    break
                if time.monotonic() >= deadline:
                    raise VideoAutopilotError("video_resource_limit")
                if not worker.is_alive():
                    raise VideoAutopilotError("video_engine_failed")
            result = parent.recv()
            if (
                isinstance(result, tuple) and len(result) == 3 and result[0] == "ok"
                and isinstance(result[1], bytes) and isinstance(result[2], dict)
            ):
                if sample_count <= 0 or peak_rss <= 0:
                    raise VideoAutopilotError("video_resource_limit")
                attestation = dict(result[2])
                attestation.update({
                    "render_seconds": round(time.monotonic() - started, 3),
                    "controller_peak_rss_bytes": peak_rss,
                    "per_process_rss_limit_bytes": MAX_RENDER_RSS_BYTES,
                })
                return result[1], attestation
            if isinstance(result, tuple) and len(result) == 2 and result[0] == "error":
                raise VideoAutopilotError(str(result[1]))
            raise VideoAutopilotError("video_engine_failed")
        except (EOFError, OSError) as exc:
            raise VideoAutopilotError("video_engine_failed") from exc
        finally:
            try:
                parent.close()
            except OSError:
                pass
            try:
                child.close()
            except OSError:
                pass
            if worker_started:
                worker.join(0.3)
                if worker.is_alive():
                    try:
                        os.killpg(worker.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    worker.join(2.0)
                if worker.is_alive():
                    try:
                        os.killpg(worker.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    worker.join(2.0)
                if worker.is_alive() or worker.exitcode is None or not _process_group_absent(worker.pid):
                    raise VideoAutopilotError("video_cleanup_failed")


@video_studio_bp.before_request
def _reject_oversized_request():
    limit = (
        MAX_ASSET_REQUEST_BYTES
        if request.endpoint == "video_studio.video_studio_render_assets"
        else MAX_JSON_BYTES
    )
    request.max_content_length = limit
    if request.method == "POST" and request.content_length is not None and request.content_length > limit:
        return _plain_error("invalid_request_schema", 413)
    return None


@video_studio_bp.app_errorhandler(RequestEntityTooLarge)
def _request_too_large(_error):
    return _plain_error("invalid_request_schema", 413)


@video_studio_bp.get("/tools")
def public_tools_page():
    response = make_response(render_template("public_tools.html"))
    response.headers["Cache-Control"] = "no-store"
    return response


@video_studio_bp.get("/video-studio")
def video_studio_page():
    response = make_response(render_template("video_studio.html"))
    response.headers["Cache-Control"] = "no-store"
    return response


@video_studio_bp.get("/api/video-studio/health")
def video_studio_health():
    return jsonify({
        "ok": True,
        "engine_contract": ENGINE_CONTRACT,
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_version": UPSTREAM_VERSION,
        "programmatic_path": True,
        "storyboard_input_supported": True,
        "media_upload_supported": True,
        "command_interpretation_required": True,
        "visual_quality_gate": True,
        "capcut_required": False,
        "network_used": False,
        "external_publish_enabled": False,
        "pii_included": False,
    })


@video_studio_bp.post("/api/video-studio/interpret")
def video_studio_interpret():
    if not request.is_json:
        return _plain_error("invalid_request_schema", 415)
    try:
        payload = request.get_json(silent=False)
        if not isinstance(payload, dict) or set(payload) != {"instructions"}:
            raise VideoAutopilotError("invalid_request_schema")
        plan = interpret_edit_instructions(payload["instructions"])
    except (VideoAutopilotError, ValueError, TypeError) as exc:
        code = str(exc) if isinstance(exc, VideoAutopilotError) else "invalid_request_schema"
        return _plain_error(code, 400)
    response = jsonify({"ok": True, "plan": public_edit_plan(plan)})
    response.headers["Cache-Control"] = "no-store"
    return response


@video_studio_bp.post("/api/video-studio/render")
def video_studio_render():
    if not request.is_json:
        return _plain_error("invalid_request_schema", 415)
    if check_rate_limit("video_studio", _client_key(), limiter=_rate_limiter).rejected:
        return _plain_error("video_rate_limited", 429)
    try:
        payload = request.get_json(silent=False)
        instructions = None
        if isinstance(payload, dict) and set(payload) == {
            "request", "instructions", "edit_plan_sha256",
        }:
            request_payload = payload["request"]
            plan = interpret_edit_instructions(payload["instructions"])
            if payload["edit_plan_sha256"] != plan.sha256:
                raise VideoAutopilotError("edit_plan_mismatch")
            instructions = payload["instructions"]
        else:
            request_payload = payload
        validate_storyboard_request(request_payload)
    except (VideoAutopilotError, ValueError, TypeError) as exc:
        code = str(exc) if isinstance(exc, VideoAutopilotError) else "invalid_request_schema"
        return _plain_error(code, 400)
    if not _render_slot.acquire(blocking=False):
        return _plain_error("video_busy", 429)
    try:
        media, attestation = _render_bounded(request_payload, instructions)
        response = send_file(
            io.BytesIO(media),
            mimetype="video/mp4",
            as_attachment=True,
            download_name="MAGI_video_studio_short.mp4",
            max_age=0,
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-MAGI-Engine-Contract"] = ENGINE_CONTRACT
        response.headers["X-MAGI-Output-SHA256"] = str(attestation["output_sha256"])
        response.headers["X-MAGI-Output-Bytes"] = str(attestation["output_bytes"])
        response.headers["X-MAGI-Render-Seconds"] = str(attestation["render_seconds"])
        response.headers["X-MAGI-Storyboard-SHA256"] = str(attestation["storyboard_sha256"])
        response.headers["X-MAGI-Edit-Plan-SHA256"] = str(attestation["edit_plan_sha256"])
        response.headers["X-MAGI-Scene-Count"] = str(attestation["scene_count"])
        response.headers["X-MAGI-Visual-Samples"] = str(attestation["visual_sample_count"])
        return response
    except VideoAutopilotError as exc:
        return _plain_error(str(exc), 503)
    finally:
        _render_slot.release()


@video_studio_bp.post("/api/video-studio/render-assets")
def video_studio_render_assets():
    if not request.mimetype or not request.mimetype.startswith("multipart/form-data"):
        return _plain_error("invalid_request_schema", 415)
    if check_rate_limit("video_studio", _client_key(), limiter=_rate_limiter).rejected:
        return _plain_error("video_rate_limited", 429)
    try:
        spec_raw = request.form.get("spec", "")
        if not isinstance(spec_raw, str) or not 1 <= len(spec_raw.encode("utf-8")) <= MAX_JSON_BYTES:
            raise VideoAutopilotError("invalid_request_schema")
        spec = json.loads(spec_raw)
        if not isinstance(spec, dict) or set(spec) != {"request", "instructions", "edit_plan_sha256"}:
            raise VideoAutopilotError("invalid_request_schema")
        request_payload = spec["request"]
        request_contract = validate_asset_storyboard_request(request_payload)
        plan = interpret_edit_instructions(spec["instructions"])
        if spec["edit_plan_sha256"] != plan.sha256:
            raise VideoAutopilotError("edit_plan_mismatch")
        uploads = _bounded_uploads()
        if len(uploads) != len(request_contract.scenes):
            raise VideoAutopilotError("asset_scene_count_mismatch")
    except (VideoAutopilotError, ValueError, TypeError, json.JSONDecodeError) as exc:
        code = str(exc) if isinstance(exc, VideoAutopilotError) else "invalid_request_schema"
        return _plain_error(code, 400)
    if not _render_slot.acquire(blocking=False):
        return _plain_error("video_busy", 429)
    try:
        media, attestation = _render_assets_bounded(request_payload, uploads, spec["instructions"])
        response = send_file(
            io.BytesIO(media), mimetype="video/mp4", as_attachment=True,
            download_name="MAGI_asset_storyboard.mp4", max_age=0,
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-MAGI-Engine-Contract"] = ENGINE_CONTRACT
        response.headers["X-MAGI-Output-SHA256"] = str(attestation["output_sha256"])
        response.headers["X-MAGI-Storyboard-SHA256"] = str(attestation["storyboard_sha256"])
        response.headers["X-MAGI-Asset-Set-SHA256"] = str(attestation["asset_set_sha256"])
        response.headers["X-MAGI-Edit-Plan-SHA256"] = str(attestation["edit_plan_sha256"])
        response.headers["X-MAGI-Scene-Count"] = str(attestation["scene_count"])
        response.headers["X-MAGI-Visual-Samples"] = str(attestation["visual_sample_count"])
        response.headers["X-MAGI-Render-Seconds"] = str(attestation["render_seconds"])
        return response
    except VideoAutopilotError as exc:
        return _plain_error(str(exc), 503)
    finally:
        _render_slot.release()
