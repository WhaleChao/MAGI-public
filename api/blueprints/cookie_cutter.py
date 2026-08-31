"""Public, bounded, memory-only cookie cutter/stamp conversion."""
from __future__ import annotations

import io
import json
import math
import multiprocessing
import threading
import time
import zipfile
from pathlib import PurePath

from flask import Blueprint, jsonify, make_response, render_template, request, send_file
from api.durable_rate_limit import DurableRateLimiter, check_rate_limit
from skills.cookie_stl import (
    CookieParameters,
    CookieSTLError,
    generate_zip_bytes,
    inspect_line_art_bytes,
)


cookie_cutter_bp = Blueprint("cookie_cutter", __name__)

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 4096 * 4096
MAX_REQUESTS_PER_MINUTE = 6
MAX_CONCURRENT_PREVIEWS = 2
MAX_GENERATION_SECONDS = 35
MAX_MULTIPART_OVERHEAD_BYTES = 64 * 1024
MAX_GENERATION_RSS_BYTES = 384 * 1024 * 1024
_preview_slots = threading.BoundedSemaphore(MAX_CONCURRENT_PREVIEWS)
_rate_limiter = DurableRateLimiter(limits={"cookie_cutter": MAX_REQUESTS_PER_MINUTE})
_COOKIE_ARCHIVE_COMMON_MEMBERS = {
    "cutter.stl",
    "cutter.obj",
    "cutter.3mf",
    "segmentation_preview.png",
    "parameters.json",
    "README.txt",
}
_COOKIE_ARCHIVE_STAMP_MEMBERS = _COOKIE_ARCHIVE_COMMON_MEMBERS | {
    "stamp_mirrored.stl",
    "stamp_mirrored.obj",
    "stamp_mirrored.3mf",
}
_RESOURCE_ATTESTATION_KEYS = {
    "generation_seconds",
    "peak_rss_bytes",
    "child_reaped",
    "child_leaks",
}
_RESOURCE_LIMIT_SETUP_FAILURE = "generation_resource_limit_setup_failed"
_COOKIE_ERROR_MESSAGES = {
    "no_usable_line_art": "圖片中找不到可用的黑白線稿。",
    "open_or_missing_outer_contour": "找不到完整封閉的最外框，請先補齊斷線。",
    "outer_contour_touches_image_edge": "最外框碰到圖片邊緣或底色無法判讀，請在四周保留白邊。",
    "line_art_geometry_incomplete": "線稿無法建立完整封閉模型，請簡化細節後重試。",
    "vector_geometry_unavailable": "切模向量引擎尚未就緒，請稍後再試。",
    "contour_quality_failed": "線稿曲線誤差超過 0.15 mm，請提高原圖解析度後重試。",
    "cutter_wall_not_continuous": "切模外壁無法形成單一連續封閉環，請加粗過窄處或移除相交線段後重試。",
    "feature_too_small": "線稿細節小於可安全列印的最小寬度，請加粗後重試。",
    "resource_limit_exceeded": "線稿過於複雜或處理逾時，請簡化細節後重試。",
    "finished_envelope_exceeded": "成品尺寸超出設定值，請調整握邊或寬度。",
    "invalid_dimensions": "建模尺寸不合理，請依畫面範圍調整。",
    "generation_resource_limit": "模型產生超過資源限制，請簡化線稿後重試。",
    "generation_resource_limit_setup_failed": "模型資源限制無法安全啟用，請稍後再試。",
    "generation_resource_cleanup_failed": "模型工作程序未能安全回收，請稍後再試。",
    "generation_resource_attestation_failed": "模型資源證據無法安全驗證，請稍後再試。",
    "generation_archive_schema_failed": "模型壓縮檔未通過完整性檢查，請稍後再試。",
    "resource_attestation_unavailable": "模型資源監測證據不足，請稍後再試。",
}


def _cookie_generation_child(
    connection,
    monitor_ready,
    content: bytes,
    values: dict[str, float],
) -> None:
    """Spawn-safe, importable child target. Never writes uploads to disk."""
    try:
        # Import in the child so macOS spawn does not inherit server state.
        from skills.cookie_stl import CookieParameters, CookieSTLError, generate_zip_bytes
        try:
            import resource
            resource.setrlimit(
                resource.RLIMIT_CPU,
                (MAX_GENERATION_SECONDS, MAX_GENERATION_SECONDS + 1),
            )
        except (ImportError, OSError, ValueError):
            # A child without its CPU ceiling is unsafe to run.  Send only a
            # fixed protocol value: no platform exception details cross IPC.
            connection.send(("resource_error", _RESOURCE_LIMIT_SETUP_FAILURE))
            return
        # Do not start the expensive engine until the parent has attached its
        # OS-level RSS monitor and recorded the first positive sample.  Without
        # this handshake, a simple frame can legitimately finish before psutil
        # observes it and is then rejected as an unattested fast child.
        if not monitor_ready.wait(timeout=MAX_GENERATION_SECONDS):
            connection.send(("cookie_error", "resource_attestation_unavailable"))
            return
        bundle, summary = generate_zip_bytes(content, CookieParameters(**values))
        connection.send(("ok", bundle, summary))
    except CookieSTLError as exc:
        connection.send(("cookie_error", str(exc)))
    except Exception:
        connection.send(("error", "generation_failed"))
    finally:
        connection.close()


def _plain_error(message: str, status: int):
    response = jsonify({"ok": False, "message": message})
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _cookie_error_response(exc: CookieSTLError):
    return _plain_error(
        _COOKIE_ERROR_MESSAGES.get(
            str(exc),
            "線稿未通過封閉網格檢查，請修正後重試。",
        ),
        400,
    )


@cookie_cutter_bp.before_request
def _reject_oversized_multipart_before_parse():
    """Reject a declared oversized body before Flask accesses request.files."""
    if request.method != "POST":
        return None
    declared = request.content_length
    if declared is not None and declared > MAX_UPLOAD_BYTES + MAX_MULTIPART_OVERHEAD_BYTES:
        return _plain_error("圖片過大，請控制在 8MB 以內", 413)
    return None


def _client_key() -> str:
    # Do not trust a forwarded header directly.  The limiter stores only a
    # SHA-256 of this normalized identity.
    return str(request.remote_addr or "anonymous")


def _within_rate_limit() -> bool:
    return not check_rate_limit("cookie_cutter", _client_key(), limiter=_rate_limiter).rejected


def _read_upload_bounded(upload) -> bytes:
    """Reject oversized multipart streams before retaining an unbounded body."""
    declared = request.content_length
    if declared is not None and declared > MAX_UPLOAD_BYTES + MAX_MULTIPART_OVERHEAD_BYTES:
        raise ValueError("圖片過大，請控制在 8MB 以內")
    stream = upload.stream
    remaining = MAX_UPLOAD_BYTES + 1
    chunks: list[bytes] = []
    while remaining:
        chunk = stream.read(min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > MAX_UPLOAD_BYTES or stream.read(1):
        raise ValueError("圖片過大，請控制在 8MB 以內")
    return payload


def _generate_bounded(content: bytes, parameters: CookieParameters):
    """Run conversion in a killable macOS-spawn child with bounded IPC."""
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    monitor_ready = context.Event()
    worker = context.Process(
        target=_cookie_generation_child,
        args=(
            child,
            monitor_ready,
            bytes(content),
            {
                name: float(getattr(parameters, name))
                for name in parameters.__dataclass_fields__
            },
        ),
        name="cookie-cutter-generate",
    )
    peak_rss_bytes = 0
    rss_sample_count = 0
    started = time.monotonic()
    worker_started = False
    bundle: bytes | None = None
    engine_summary: dict[str, object] | None = None
    process = None
    initial_monitor_error: Exception | None = None
    monitor_released = False
    try:
        worker.start()
        worker_started = True
        child.close()
        try:
            import psutil
            process = psutil.Process(worker.pid)
        except Exception as exc:
            raise CookieSTLError("resource_attestation_unavailable") from exc
        # Sample before the first blocking Pipe poll.  Simple, valid line art
        # can finish in under that poll interval; waiting first made fast
        # successful children look unmonitored and fail with a false red light.
        # A setup-error child may already be gone, so defer this sampling error
        # until the exact terminal payload is known below.
        try:
            observed_rss = int(process.memory_info().rss)
            if observed_rss <= 0:
                raise RuntimeError("non_positive_child_rss")
            peak_rss_bytes = observed_rss
            rss_sample_count = 1
            if peak_rss_bytes > MAX_GENERATION_RSS_BYTES:
                raise CookieSTLError("generation_resource_limit")
            monitor_ready.set()
            monitor_released = True
        except Exception as exc:
            if isinstance(exc, CookieSTLError):
                raise
            initial_monitor_error = exc
        deadline = started + MAX_GENERATION_SECONDS
        while True:
            if worker.is_alive():
                try:
                    observed_rss = int(process.memory_info().rss)
                    if observed_rss <= 0:
                        raise RuntimeError("non_positive_child_rss")
                    peak_rss_bytes = max(peak_rss_bytes, observed_rss)
                    rss_sample_count += 1
                    if peak_rss_bytes > MAX_GENERATION_RSS_BYTES:
                        raise CookieSTLError("generation_resource_limit")
                    if not monitor_released:
                        monitor_ready.set()
                        monitor_released = True
                except Exception as exc:
                    if isinstance(exc, CookieSTLError):
                        raise
                    # The child can finish between ``is_alive`` and the RSS
                    # read.  Drain a completed Pipe response before treating
                    # a still-live, unmonitorable process as unsafe.
                    worker.join(0)
                    if parent.poll(0) or not worker.is_alive():
                        if parent.poll(0):
                            break
                        continue
                    raise CookieSTLError("resource_attestation_unavailable") from exc
            if parent.poll(0.01):
                break
            if time.monotonic() >= deadline:
                raise CookieSTLError("generation_resource_limit")
        payload = parent.recv()
        if not isinstance(payload, tuple) or not payload:
            raise CookieSTLError("generation_failed")
        if payload[0] == "ok":
            if (
                len(payload) != 3
                or not isinstance(payload[1], bytes)
                or not isinstance(payload[2], dict)
            ):
                raise CookieSTLError("generation_failed")
            if rss_sample_count <= 0 or peak_rss_bytes <= 0:
                raise CookieSTLError("resource_attestation_unavailable") from initial_monitor_error
            bundle = payload[1]
            engine_summary = dict(payload[2])
        elif payload[0] == "cookie_error":
            raise CookieSTLError(str(payload[1]))
        elif payload == ("resource_error", _RESOURCE_LIMIT_SETUP_FAILURE):
            raise CookieSTLError(_RESOURCE_LIMIT_SETUP_FAILURE)
        else:
            raise CookieSTLError("generation_failed")
    except EOFError as exc:
        raise CookieSTLError("generation_failed") from exc
    finally:
        try:
            parent.close()
        except OSError:
            pass
        try:
            child.close()
        except OSError:
            pass
        child_reaped = not worker_started
        child_leaks = 0
        if worker_started:
            worker.join(0.25)
            if worker.is_alive():
                worker.terminate()
                worker.join(1.0)
            if worker.is_alive():
                worker.kill()
                worker.join(1.0)
            child_leaks = 1 if worker.is_alive() else 0
            child_reaped = bool(
                child_leaks == 0 and getattr(worker, "exitcode", None) is not None
            )
        if worker_started and (not child_reaped or child_leaks):
            raise CookieSTLError("generation_resource_cleanup_failed")

    if bundle is None or engine_summary is None:
        raise CookieSTLError("generation_failed")
    attestation: dict[str, object] = {
        "generation_seconds": round(max(0.0, time.monotonic() - started), 4),
        "peak_rss_bytes": max(0, int(peak_rss_bytes)),
        "child_reaped": bool(child_reaped),
        "child_leaks": int(child_leaks),
    }
    return _attest_generated_bundle(bundle, engine_summary, attestation)


def _attest_generated_bundle(
    bundle: bytes,
    engine_summary: dict[str, object],
    attestation: dict[str, object],
) -> tuple[bytes, dict[str, object]]:
    """Bind parent-observed, PII-free resource evidence into the ZIP receipt."""

    if set(attestation) != _RESOURCE_ATTESTATION_KEYS:
        raise CookieSTLError("generation_resource_attestation_failed")
    generation_seconds = attestation.get("generation_seconds")
    peak_rss_bytes = attestation.get("peak_rss_bytes")
    child_reaped = attestation.get("child_reaped")
    child_leaks = attestation.get("child_leaks")
    if (
        isinstance(generation_seconds, bool)
        or not isinstance(generation_seconds, (int, float))
        or not math.isfinite(float(generation_seconds))
        or float(generation_seconds) < 0
        or isinstance(peak_rss_bytes, bool)
        or not isinstance(peak_rss_bytes, int)
        or peak_rss_bytes <= 0
        or not isinstance(child_reaped, bool)
        or isinstance(child_leaks, bool)
        or not isinstance(child_leaks, int)
        or child_leaks < 0
        or not child_reaped
        or child_leaks != 0
    ):
        raise CookieSTLError("generation_resource_attestation_failed")
    try:
        source = io.BytesIO(bundle)
        output = io.BytesIO()
        with zipfile.ZipFile(source, "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            stored_summary = json.loads(archive.read("parameters.json"))
            if not isinstance(stored_summary, dict) or stored_summary != engine_summary:
                raise CookieSTLError("generation_archive_schema_failed")
            mode = stored_summary.get("mode")
            stamp_generated = stored_summary.get("stamp_generated")
            if mode == "cutter_only" and stamp_generated is False:
                expected_members = _COOKIE_ARCHIVE_COMMON_MEMBERS
            elif mode == "cutter_and_stamp" and stamp_generated is True:
                expected_members = _COOKIE_ARCHIVE_STAMP_MEMBERS
            else:
                raise CookieSTLError("generation_archive_schema_failed")
            if len(names) != len(set(names)) or set(names) != expected_members:
                raise CookieSTLError("generation_archive_schema_failed")
            attested_summary = dict(stored_summary)
            # Reserved keys are overwritten exclusively with parent-observed
            # scalars; child/client values can never attest their own cleanup.
            attested_summary.update(attestation)
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as rebuilt:
                for info in infos:
                    if info.filename == "parameters.json":
                        rebuilt.writestr(
                            "parameters.json",
                            json.dumps(
                                attested_summary,
                                ensure_ascii=False,
                                indent=2,
                                sort_keys=True,
                            ),
                        )
                    else:
                        rebuilt.writestr(info, archive.read(info.filename))
        return output.getvalue(), attested_summary
    except CookieSTLError:
        raise
    except (json.JSONDecodeError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        raise CookieSTLError("generation_archive_schema_failed") from exc


def _image_dimensions(content: bytes) -> tuple[int, int, str]:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(content)) as image:
            width, height = image.size
            image_type = str(image.format or "").lower()
    except Exception as exc:
        raise ValueError("只接受有效的 PNG、JPG、BMP 或 TIF 線稿圖片") from exc
    if image_type not in {"png", "jpeg", "bmp", "tiff"}:
        raise ValueError("只接受有效的 PNG、JPG、BMP 或 TIF 線稿圖片")
    return width, height, image_type


def build_cookie_cutter_request(filename: str, content: bytes) -> dict[str, object]:
    """Validate image bytes with the same topology contract as generation."""
    safe_name = PurePath(str(filename or "")).name
    suffix = PurePath(safe_name).suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}:
        raise ValueError("只支援 PNG、JPG、BMP 或 TIF 圖片")
    if not content:
        raise ValueError("請先選擇一張黑白線稿圖片")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("圖片過大，請控制在 8MB 以內")
    width, height, image_type = _image_dimensions(content)
    if not width or not height or width * height > MAX_IMAGE_PIXELS:
        raise ValueError("圖片尺寸過大，請控制在 4096 × 4096 像素以內")
    line_art = inspect_line_art_bytes(content)
    return {
        "engine_contract": "magi.cookie-cutter-model/v2",
        "image_type": image_type,
        "width": width,
        "height": height,
        "upload_persisted": False,
        **line_art,
        "next_step": (
            "已確認封閉外框與框內獨立圖案，可產生切模與鏡像壓模。"
            if line_art["generation_mode"] == "cutter_and_stamp"
            else "已確認封閉外框；原圖沒有獨立內部圖案，將只產生光滑切模。"
        ),
    }


@cookie_cutter_bp.get("/cookie-cutter")
def cookie_cutter_page():
    response = make_response(render_template("cookie_cutter.html"))
    response.headers["Cache-Control"] = "no-store"
    return response


@cookie_cutter_bp.get("/api/cookie-cutter/health")
def cookie_cutter_health_api():
    response = jsonify({
        "ok": True,
        "engine_contract": "magi.cookie-cutter-model/v2",
        "formats": ["stl", "obj", "3mf"],
        "outline_only_supported": True,
        "internal_relief_supported": True,
        "serialized_mesh_revalidated": True,
        "maximum_contour_error_mm": 0.15,
        "upload_persisted": False,
        "network_used": False,
        "pii_included": False,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@cookie_cutter_bp.post("/api/cookie-cutter/prepare")
def cookie_cutter_prepare_api():
    if not _within_rate_limit():
        return _plain_error("請稍後再試，這個工具每分鐘最多處理 6 次預覽。", 429)
    if not _preview_slots.acquire(blocking=False):
        return _plain_error("目前預覽作業較多，請稍後再試。", 429)
    try:
        upload = request.files.get("image")
        if not upload or not upload.filename:
            return _plain_error("請先選擇一張黑白線稿圖片。", 400)
        return jsonify({"ok": True, **build_cookie_cutter_request(upload.filename, _read_upload_bounded(upload))})
    except CookieSTLError as exc:
        return _cookie_error_response(exc)
    except ValueError as exc:
        return _plain_error(str(exc), 400)
    except Exception:
        return _plain_error("圖片暫時無法辨識，請改用清楚的 PNG 或 JPG 線稿。", 400)
    finally:
        _preview_slots.release()


def _parameters(form) -> dict[str, float]:
    specs = {
        "width_mm": (20, 200, None),
        "blade_height_mm": (5, 30, None),
        "blade_wall_mm": (0.6, 3, None),
        "rim_mm": (0, 15, None),
        "stamp_base_mm": (1, 10, None),
        "relief_mm": (0.4, 12, None),
        "clearance_mm": (0, 3, None),
        # New v2 controls default safely for older bookmarked clients.
        "grip_height_mm": (2, 6, 2.8),
        "relief_width_mm": (0.4, 3, 0.55),
        "smoothing_mm": (0.05, 0.15, 0.12),
    }
    result: dict[str, float] = {}
    for key, (low, high, default) in specs.items():
        try:
            value = float(form.get(key, "" if default is None else default))
        except (TypeError, ValueError) as exc:
            raise ValueError("請填入正確的建模尺寸。") from exc
        if not low <= value <= high:
            raise ValueError("建模尺寸超出安全範圍，請依畫面提示調整。")
        result[key] = value
    return result


@cookie_cutter_bp.post("/api/cookie-cutter/generate")
def cookie_cutter_generate_api():
    if not _within_rate_limit():
        return _plain_error("請稍後再試，這個工具每分鐘最多處理 6 次預覽。", 429)
    if not _preview_slots.acquire(blocking=False):
        return _plain_error("目前預覽作業較多，請稍後再試。", 429)
    try:
        upload = request.files.get("image")
        if not upload or not upload.filename:
            return _plain_error("請先選擇一張黑白線稿圖片。", 400)
        content = _read_upload_bounded(upload)
        build_cookie_cutter_request(upload.filename, content)
        values = _parameters(request.form)
        bundle_bytes, summary = _generate_bounded(content, CookieParameters(**values))
        response = send_file(
            io.BytesIO(bundle_bytes),
            mimetype="application/zip",
            as_attachment=True,
            download_name="MAGI_cookie_cutter_and_stamp.zip",
            max_age=0,
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-MAGI-Mesh-Status"] = "watertight" if summary.get("watertight") else "rejected"
        return response
    except CookieSTLError as exc:
        return _cookie_error_response(exc)
    except ValueError as exc:
        return _plain_error(str(exc), 400)
    except Exception:
        return _plain_error("目前無法產生模型，請稍後再試。", 400)
    finally:
        _preview_slots.release()
