from __future__ import annotations

import hashlib
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from api.blueprints import video_studio as blueprint
from magi_v3 import video_autopilot_adapter as adapter
from magi_v3.video_autopilot_adapter import (
    AssetInput,
    ENGINE_CONTRACT,
    UPSTREAM_COMMIT,
    VideoAutopilotError,
    interpret_edit_instructions,
    render_asset_storyboard,
    render_storyboard,
    validate_storyboard_request,
)


def _app(*, csrf: bool = False) -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.update(TESTING=True, SECRET_KEY="video-studio-test")
    app.register_blueprint(blueprint.video_studio_bp)
    if csrf:
        from api.csrf_guard import middleware_apply_csrf

        middleware_apply_csrf(app)
    return app


def _payload() -> dict:
    return {
        "title": "MAGI 自動剪輯",
        "subtitle": "本機生成・不外送",
        "scenes": ["第一幕說清楚問題", "第二幕提出方法", "第三幕整理結論"],
        "palette": "ocean",
        "duration_seconds": 6,
    }


def test_public_tool_directory_and_video_page_require_no_login() -> None:
    client = _app().test_client()
    tools = client.get("/tools")
    video = client.get("/video-studio")
    assert tools.status_code == 200
    assert video.status_code == 200
    assert tools.headers["Cache-Control"] == "no-store"
    directory = tools.get_data(as_text=True)
    assert all(path in directory for path in ("/video-studio", "/cookie-cutter", "/lottery", "/exam-tutor"))
    assert all(label in directory for label in ("創作與製造", "學習與活動", "法律實務與系統維護"))
    assert "免登入" in video.get_data(as_text=True)
    assert "影片內容／逐幕字幕" in video.get_data(as_text=True)
    assert "確認 MAGI 理解" in video.get_data(as_text=True)
    assert "先預覽，再決定是否下載" in video.get_data(as_text=True)


@pytest.mark.parametrize("template_name", ["public_tools.html", "video_studio.html"])
def test_public_video_navigation_uses_shared_magi_theme_contract(template_name: str) -> None:
    source = (Path(__file__).resolve().parents[1] / "templates" / template_name).read_text(encoding="utf-8")

    assert "magi-site.css" in source
    assert "magi-theme.css" in source
    assert "magi-theme.js" in source
    assert "data-magi-theme-toggle" in source
    assert 'aria-label="切換日夜主題"' in source


def test_video_studio_defines_matching_day_and_night_palette() -> None:
    source = (Path(__file__).resolve().parents[1] / "templates" / "video_studio.html").read_text(encoding="utf-8")

    assert ":root{" in source
    assert ':root[data-magi-theme="cyber"]' in source
    for token in ("--vs-bg", "--vs-panel", "--vs-ink", "--vs-muted", "--vs-line", "--vs-accent", "--vs-soft"):
        assert source.count(token) >= 2


def test_health_is_public_safe_and_exact() -> None:
    payload = _app().test_client().get("/api/video-studio/health").get_json()
    assert payload == {
        "ok": True,
        "engine_contract": ENGINE_CONTRACT,
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_version": "0.21.1",
        "programmatic_path": True,
        "storyboard_input_supported": True,
        "media_upload_supported": True,
        "command_interpretation_required": True,
        "visual_quality_gate": True,
        "capcut_required": False,
        "network_used": False,
        "external_publish_enabled": False,
        "pii_included": False,
    }


def test_edit_command_is_interpreted_and_unknown_or_conflicting_meanings_fail_closed() -> None:
    client = _app().test_client()
    response = client.post(
        "/api/video-studio/interpret",
        json={"instructions": "倒序，柔和轉場，平滑運鏡，靜音"},
    )

    assert response.status_code == 200
    assert response.get_json()["plan"] == {
        "order": "reverse",
        "order_label": "倒序排列",
        "transition": "fade",
        "transition_label": "淡化轉場",
        "motion": "gentle_zoom",
        "motion_label": "平滑運鏡",
        "audio": "silent",
        "audio_label": "靜音 AAC",
        "plan_sha256": interpret_edit_instructions("倒序，柔和轉場，平滑運鏡，靜音").sha256,
        "pii_included": False,
    }
    assert client.post(
        "/api/video-studio/interpret", json={"instructions": "請幫我做得很厲害"}
    ).get_json()["error"] == "unsupported_edit_instruction"
    assert client.post(
        "/api/video-studio/interpret", json={"instructions": "依序，倒序"}
    ).get_json()["error"] == "conflicting_edit_instruction"


@pytest.mark.parametrize(
    "change",
    [
        {"extra": "x"},
        {"title": ""},
        {"subtitle": "x" * 65},
        {"palette": "unknown"},
        {"duration_seconds": True},
        {"duration_seconds": 8},
        {"scenes": ["只有一幕"]},
        {"scenes": ["重複", "重複"]},
        {"scenes": ["a", "b", "c", "d", "e", "f"]},
    ],
)
def test_request_contract_rejects_unknown_and_coerced_values(change: dict) -> None:
    payload = _payload()
    payload.update(change)
    with pytest.raises(VideoAutopilotError):
        validate_storyboard_request(payload)


def test_public_render_returns_attested_mp4_without_login(monkeypatch) -> None:
    media = b"\x00\x00\x00\x18ftypmp42synthetic"
    monkeypatch.setattr(
        blueprint,
        "check_rate_limit",
        lambda *_args, **_kwargs: SimpleNamespace(rejected=False),
    )
    monkeypatch.setattr(
        blueprint,
        "_render_bounded",
        lambda _payload, _instructions=None: (
            media,
            {
                "output_sha256": hashlib.sha256(media).hexdigest(),
                "output_bytes": len(media),
                "render_seconds": 1.25,
                "storyboard_sha256": "b" * 64,
                "edit_plan_sha256": interpret_edit_instructions("").sha256,
                "scene_count": 3,
                "visual_sample_count": 3,
            },
        ),
    )
    response = _app().test_client().post("/api/video-studio/render", json=_payload())
    assert response.status_code == 200
    assert response.mimetype == "video/mp4"
    assert response.data == media
    assert response.headers["X-MAGI-Engine-Contract"] == ENGINE_CONTRACT
    assert response.headers["X-MAGI-Output-SHA256"] == hashlib.sha256(media).hexdigest()
    assert response.headers["X-MAGI-Storyboard-SHA256"] == "b" * 64
    assert response.headers["X-MAGI-Scene-Count"] == "3"
    assert response.headers["X-MAGI-Visual-Samples"] == "3"
    assert response.headers["Cache-Control"] == "no-store"


def test_public_text_render_uses_the_confirmed_edit_plan(monkeypatch) -> None:
    instructions = "倒序，直接切換，固定畫面，靜音"
    plan = interpret_edit_instructions(instructions)
    observed: dict[str, object] = {}

    def fake_render(payload, received_instructions):
        observed.update(payload=payload, instructions=received_instructions)
        return b"mp4", {
            "output_sha256": "a" * 64,
            "output_bytes": 3,
            "render_seconds": 0.1,
            "storyboard_sha256": "b" * 64,
            "edit_plan_sha256": plan.sha256,
            "scene_count": 3,
            "visual_sample_count": 3,
        }

    monkeypatch.setattr(
        blueprint, "check_rate_limit",
        lambda *_args, **_kwargs: SimpleNamespace(rejected=False),
    )
    monkeypatch.setattr(blueprint, "_render_bounded", fake_render)
    envelope = {
        "request": _payload(),
        "instructions": instructions,
        "edit_plan_sha256": plan.sha256,
    }

    response = _app().test_client().post("/api/video-studio/render", json=envelope)

    assert response.status_code == 200
    assert observed == {"payload": _payload(), "instructions": instructions}
    assert response.headers["X-MAGI-Edit-Plan-SHA256"] == plan.sha256
    envelope["edit_plan_sha256"] = "0" * 64
    assert _app().test_client().post(
        "/api/video-studio/render", json=envelope,
    ).get_json()["error"] == "edit_plan_mismatch"


def test_public_asset_render_requires_confirmed_plan_and_returns_anonymous_proofs(monkeypatch) -> None:
    media = b"\x00\x00\x00\x18ftypmp42asset"
    instructions = "依序，淡入淡出，慢慢拉近，背景音樂"
    plan = interpret_edit_instructions(instructions)
    request_payload = _payload()
    request_payload["scenes"] = ["第一份素材", "第二份素材"]
    spec = {
        "request": request_payload,
        "instructions": instructions,
        "edit_plan_sha256": plan.sha256,
    }
    monkeypatch.setattr(
        blueprint,
        "check_rate_limit",
        lambda *_args, **_kwargs: SimpleNamespace(rejected=False),
    )
    monkeypatch.setattr(
        blueprint,
        "_render_assets_bounded",
        lambda _payload, _uploads, _instructions: (
            media,
            {
                "output_sha256": hashlib.sha256(media).hexdigest(),
                "storyboard_sha256": "a" * 64,
                "asset_set_sha256": "b" * 64,
                "edit_plan_sha256": plan.sha256,
                "scene_count": 2,
                "visual_sample_count": 2,
                "render_seconds": 1.0,
            },
        ),
    )
    files = [
        (io.BytesIO(b"\x89PNG\r\n\x1a\nfirst"), "private-first.png"),
        (io.BytesIO(b"\x89PNG\r\n\x1a\nsecond"), "private-second.png"),
    ]

    response = _app().test_client().post(
        "/api/video-studio/render-assets",
        data={"spec": json.dumps(spec, ensure_ascii=False), "assets": files},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.data == media
    assert response.headers["X-MAGI-Edit-Plan-SHA256"] == plan.sha256
    assert response.headers["X-MAGI-Asset-Set-SHA256"] == "b" * 64
    assert "private" not in str(response.headers).lower()

    bad = dict(spec)
    bad["edit_plan_sha256"] = "0" * 64
    rejected = _app().test_client().post(
        "/api/video-studio/render-assets",
        data={
            "spec": json.dumps(bad, ensure_ascii=False),
            "assets": [
                (io.BytesIO(b"\x89PNG\r\n\x1a\none"), "one.png"),
                (io.BytesIO(b"\x89PNG\r\n\x1a\ntwo"), "two.png"),
            ],
        },
        content_type="multipart/form-data",
    )
    assert rejected.status_code == 400
    assert rejected.get_json()["error"] == "edit_plan_mismatch"


def test_public_render_keeps_double_submit_csrf(monkeypatch) -> None:
    monkeypatch.setattr(
        blueprint,
        "check_rate_limit",
        lambda *_args, **_kwargs: SimpleNamespace(rejected=False),
    )
    monkeypatch.setattr(
        blueprint,
        "_render_bounded",
        lambda _payload, _instructions=None: (
            b"mp4",
            {
                "output_sha256": "a" * 64,
                "output_bytes": 3,
                "render_seconds": 0.1,
                "storyboard_sha256": "b" * 64,
                "edit_plan_sha256": interpret_edit_instructions("").sha256,
                "scene_count": 3,
                "visual_sample_count": 3,
            },
        ),
    )
    client = _app(csrf=True).test_client()
    page = client.get("/video-studio")
    token_cookie = next(cookie for cookie in page.headers.getlist("Set-Cookie") if cookie.startswith("X-CSRF-Token="))
    token = token_cookie.split(";", 1)[0].split("=", 1)[1]
    assert client.post("/api/video-studio/render", json=_payload()).status_code == 403
    assert client.post(
        "/api/video-studio/render",
        json=_payload(),
        headers={"X-CSRF-Token": token},
    ).status_code == 200


def test_public_render_enforces_request_limit_without_trusting_content_length() -> None:
    client = _app().test_client()
    response = client.post(
        "/api/video-studio/render",
        data=b"{" + (b"x" * blueprint.MAX_JSON_BYTES) + b"}",
        content_type="application/json",
    )
    assert response.status_code == 413
    assert response.get_json()["error"] == "invalid_request_schema"


def test_engine_failure_is_service_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        blueprint,
        "check_rate_limit",
        lambda *_args, **_kwargs: SimpleNamespace(rejected=False),
    )
    monkeypatch.setattr(
        blueprint,
        "_render_bounded",
        lambda _payload, _instructions=None: (
            _ for _ in ()
        ).throw(VideoAutopilotError("video_resource_limit")),
    )
    response = _app().test_client().post("/api/video-studio/render", json=_payload())
    assert response.status_code == 503
    assert response.get_json()["error"] == "video_resource_limit"


def test_real_programmatic_adapter_builds_verified_storyboard_mp4(tmp_path: Path) -> None:
    request = validate_storyboard_request(_payload())
    media, attestation = render_storyboard(tmp_path, request)
    assert media.startswith(b"\x00\x00")
    assert attestation["engine_contract"] == ENGINE_CONTRACT
    assert attestation["upstream_commit"] == UPSTREAM_COMMIT
    assert attestation["width"] == 1080 and attestation["height"] == 1920
    assert attestation["video_codec"] == "h264" and attestation["audio_codec"] == "aac"
    assert 5.85 <= attestation["duration_seconds"] <= 6.25
    assert attestation["frame_rate"] == 30.0
    assert 174 <= attestation["frame_count"] <= 186
    assert attestation["scene_count"] == len(request.scenes)
    assert attestation["visual_sample_count"] == len(request.scenes)
    assert attestation["distinct_visual_sample_count"] == len(request.scenes)
    assert attestation["minimum_sample_luma_stddev"] >= 16
    assert attestation["storyboard_sha256"] == hashlib.sha256(json.dumps(
        {
            "duration_seconds": request.duration_seconds,
            "palette": request.palette,
            "scenes": list(request.scenes),
            "subtitle": request.subtitle,
            "title": request.title,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    assert attestation["output_sha256"] == hashlib.sha256(media).hexdigest()
    assert 0 < attestation["renderer_peak_rss_bytes"] < 1536 * 1024 * 1024
    assert attestation["renderer_process_count"] == len(request.scenes) + 3
    assert attestation["upload_persisted"] is False
    assert attestation["network_used"] is False
    assert attestation["external_publish_used"] is False


def test_real_text_storyboard_obeys_reverse_cut_still_and_silent_plan(tmp_path: Path) -> None:
    payload = _payload()
    payload["scenes"] = ["第一幕", "第二幕"]
    request_contract = validate_storyboard_request(payload)
    plan = interpret_edit_instructions("倒序，直接切換，固定畫面，靜音")

    media, attestation = render_storyboard(tmp_path, request_contract, plan)

    reversed_request = adapter.StoryboardRequest(
        request_contract.title,
        request_contract.subtitle,
        tuple(reversed(request_contract.scenes)),
        request_contract.palette,
        request_contract.duration_seconds,
    )
    assert media.startswith(b"\x00\x00")
    assert attestation["edit_plan_sha256"] == plan.sha256
    assert attestation["storyboard_sha256"] == adapter._storyboard_sha256(reversed_request)
    assert attestation["distinct_visual_sample_count"] == 2
    assert attestation["minimum_sample_luma_stddev"] >= 16


def test_real_public_endpoint_runs_bounded_storyboard_and_returns_content_proof(monkeypatch) -> None:
    monkeypatch.setattr(
        blueprint,
        "check_rate_limit",
        lambda *_args, **_kwargs: SimpleNamespace(rejected=False),
    )

    response = _app().test_client().post("/api/video-studio/render", json=_payload())

    assert response.status_code == 200
    assert response.mimetype == "video/mp4"
    assert response.data.startswith(b"\x00\x00")
    assert response.headers["X-MAGI-Engine-Contract"] == ENGINE_CONTRACT
    assert response.headers["X-MAGI-Scene-Count"] == "3"
    assert response.headers["X-MAGI-Visual-Samples"] == "3"
    assert len(response.headers["X-MAGI-Storyboard-SHA256"]) == 64
    assert response.headers["X-MAGI-Output-SHA256"] == hashlib.sha256(response.data).hexdigest()


def test_real_asset_renderer_combines_uploaded_images_under_confirmed_plan(tmp_path: Path) -> None:
    from PIL import Image, ImageDraw

    assets: list[AssetInput] = []
    for index, color in enumerate(((30, 96, 150), (160, 70, 45))):
        path = tmp_path / f"input-{index}.png"
        image = Image.new("RGB", (720, 960), color)
        draw = ImageDraw.Draw(image)
        draw.ellipse((100 + index * 40, 120, 620, 640), fill=(230, 220 - index * 20, 120))
        image.save(path, format="PNG")
        assets.append(AssetInput(path, "image", hashlib.sha256(path.read_bytes()).hexdigest()))
    payload = _payload()
    payload["scenes"] = ["第一張素材的重點", "第二張素材的結論"]
    request_contract = adapter.validate_asset_storyboard_request(payload)
    plan = interpret_edit_instructions("倒序，柔和轉場，平滑運鏡，靜音")

    media, attestation = render_asset_storyboard(tmp_path, request_contract, tuple(assets), plan)

    assert media.startswith(b"\x00\x00")
    assert attestation["scene_count"] == 2
    assert attestation["edit_plan_sha256"] == plan.sha256
    assert attestation["visual_sample_count"] == 2
    assert attestation["distinct_visual_sample_count"] == 2
    assert attestation["upload_persisted"] is False
    assert attestation["network_used"] is False


def test_real_public_asset_endpoint_reaps_worker_and_removes_private_uploads(monkeypatch, tmp_path: Path) -> None:
    from PIL import Image

    buffers: list[bytes] = []
    for color in ((25, 90, 140), (145, 55, 35)):
        stream = io.BytesIO()
        Image.new("RGB", (640, 840), color).save(stream, format="PNG")
        buffers.append(stream.getvalue())
    instructions = "依上傳順序，淡化轉場，平滑運鏡，靜音"
    plan = interpret_edit_instructions(instructions)
    request_payload = _payload()
    request_payload["scenes"] = ["素材一的說明", "素材二的結論"]
    spec = json.dumps(
        {
            "request": request_payload,
            "instructions": instructions,
            "edit_plan_sha256": plan.sha256,
        },
        ensure_ascii=False,
    )
    monkeypatch.setattr(
        blueprint,
        "check_rate_limit",
        lambda *_args, **_kwargs: SimpleNamespace(rejected=False),
    )
    monkeypatch.setattr(blueprint.tempfile, "tempdir", str(tmp_path))

    response = _app().test_client().post(
        "/api/video-studio/render-assets",
        data={
            "spec": spec,
            "assets": [
                (io.BytesIO(buffers[0]), "first-private-name.png"),
                (io.BytesIO(buffers[1]), "second-private-name.png"),
            ],
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.data.startswith(b"\x00\x00")
    assert response.headers["X-MAGI-Edit-Plan-SHA256"] == plan.sha256
    assert response.headers["X-MAGI-Scene-Count"] == "2"
    assert response.headers["X-MAGI-Output-SHA256"] == hashlib.sha256(response.data).hexdigest()
    assert list(tmp_path.glob("magi-video-assets-*")) == []


def test_real_asset_renderer_accepts_a_bounded_local_video_and_image_mix(tmp_path: Path) -> None:
    from PIL import Image

    image_path = tmp_path / "still.png"
    video_path = tmp_path / "clip.mp4"
    Image.new("RGB", (640, 840), (36, 110, 82)).save(image_path, format="PNG")
    generated = subprocess.run(
        [
            "/opt/homebrew/bin/ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x7040a0:size=640x360:duration=1:rate=30",
            "-vf",
            "drawbox=x=80:y=70:w=260:h=180:color=white@0.7:t=fill",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video_path),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
    )
    assert generated.returncode == 0
    assets = tuple(
        AssetInput(path, kind, hashlib.sha256(path.read_bytes()).hexdigest())
        for path, kind in ((image_path, "image"), (video_path, "video"))
    )
    payload = _payload()
    payload["scenes"] = ["靜態素材", "動態素材"]

    media, attestation = render_asset_storyboard(
        tmp_path,
        adapter.validate_asset_storyboard_request(payload),
        assets,
        interpret_edit_instructions("依序，直接切換，固定畫面，背景音樂"),
    )

    assert media.startswith(b"\x00\x00")
    assert attestation["scene_count"] == 2
    assert attestation["video_codec"] == "h264"
    assert attestation["audio_codec"] == "aac"
    assert attestation["distinct_visual_sample_count"] == 2


def test_storyboard_content_changes_rendered_scene_bytes_and_long_text_fits(tmp_path: Path) -> None:
    first = validate_storyboard_request(_payload())
    changed_payload = _payload()
    changed_payload["scenes"] = ["這是完全不同的第一幕", "x" * 88, "最後一幕"]
    second = validate_storyboard_request(changed_payload)
    first_path = tmp_path / "first.png"
    second_path = tmp_path / "second.png"

    adapter._scene_png(first_path, first, 0, first.scenes[0])
    adapter._scene_png(second_path, second, 1, second.scenes[1])

    assert hashlib.sha256(first_path.read_bytes()).hexdigest() != hashlib.sha256(second_path.read_bytes()).hexdigest()
    assert adapter._storyboard_sha256(first) != adapter._storyboard_sha256(second)


def test_visual_quality_gate_rejects_repeated_scene_frames(monkeypatch, tmp_path: Path) -> None:
    repeated = (b"\x00\x00\x00" + b"\xff\xff\xff") * (180 * 320 // 2)
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=repeated, stderr=b""),
    )

    with pytest.raises(VideoAutopilotError, match="video_visual_quality_failed"):
        adapter._visual_quality(tmp_path / "synthetic.mp4", validate_storyboard_request(_payload()))


def test_integration_manifest_disables_updater_publish_and_remote_fetch() -> None:
    root = Path(__file__).resolve().parents[1]
    contract = (root / "third_party/video_autopilot_kit/MAGI_INTEGRATION.json").read_text(encoding="utf-8")
    adapter = (root / "magi_v3/video_autopilot_adapter.py").read_text(encoding="utf-8")
    assert UPSTREAM_COMMIT in contract
    assert all(value in contract for value in ("automatic_update", "remote_asset_fetch", "external_publish"))
    assert "startup_update" not in adapter
    assert "publish_hub" not in adapter
    assert "testsrc2" not in adapter
    assert "mandelbrot" not in adapter
