"""Local-only, content-driven adapter for the pinned video-autopilot-kit path.

MAGI excludes the upstream updater, publishers, remote fetchers, and CapCut
automation. Each input line becomes a separate motion-graphics scene; input
text and intermediate files remain in a per-request temporary directory.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import subprocess
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any


UPSTREAM_COMMIT = "6dc9ad8b3dc9b2ef158e4eac835f5f721e5b3bed"
UPSTREAM_VERSION = "0.21.1"
ENGINE_CONTRACT = "magi.video-autopilot-storyboard/v3"
_ROOT = Path(__file__).resolve().parents[1]
_UPSTREAM = _ROOT / "third_party" / "video_autopilot_kit"
_ALLOWED_PALETTES = {
    "ocean": ("#071a2f", "#0d7f91", "#d9fbff"),
    "sunset": ("#36152a", "#d85b42", "#fff1da"),
    "forest": ("#10271d", "#2b8764", "#e8fff4"),
}
_ALLOWED_DURATIONS = {6, 9, 12}
_TRANSITION_SECONDS = 0.35


class VideoAutopilotError(RuntimeError):
    """Fixed public error family; callers must not expose subprocess stderr."""


@dataclass(frozen=True)
class StoryboardRequest:
    title: str
    subtitle: str
    scenes: tuple[str, ...]
    palette: str
    duration_seconds: int


@dataclass(frozen=True)
class EditPlan:
    order: str
    transition: str
    motion: str
    audio: str
    sha256: str


@dataclass(frozen=True)
class AssetInput:
    path: Path
    kind: str
    sha256: str


def _clean_text(value: Any, *, minimum: int, maximum: int, code: str) -> str:
    if not isinstance(value, str):
        raise VideoAutopilotError(code)
    cleaned = " ".join(value.strip().split())
    if not minimum <= len(cleaned) <= maximum or any(ord(character) < 32 for character in cleaned):
        raise VideoAutopilotError(code)
    return cleaned


def _validate_storyboard_request(payload: Any, *, minimum_scenes: int) -> StoryboardRequest:
    if not isinstance(payload, dict) or set(payload) != {
        "title", "subtitle", "scenes", "palette", "duration_seconds",
    }:
        raise VideoAutopilotError("invalid_request_schema")
    title = _clean_text(payload.get("title"), minimum=1, maximum=36, code="invalid_title")
    subtitle_raw = payload.get("subtitle")
    subtitle = "" if subtitle_raw == "" else _clean_text(
        subtitle_raw, minimum=1, maximum=64, code="invalid_subtitle"
    )
    raw_scenes = payload.get("scenes")
    if not isinstance(raw_scenes, list) or not minimum_scenes <= len(raw_scenes) <= 5:
        raise VideoAutopilotError("invalid_scenes")
    scenes = tuple(
        _clean_text(value, minimum=1, maximum=88, code="invalid_scenes")
        for value in raw_scenes
    )
    if len({scene.casefold() for scene in scenes}) != len(scenes) or sum(map(len, scenes)) > 320:
        raise VideoAutopilotError("invalid_scenes")
    palette = payload.get("palette")
    if palette not in _ALLOWED_PALETTES:
        raise VideoAutopilotError("invalid_palette")
    duration = payload.get("duration_seconds")
    if isinstance(duration, bool) or type(duration) is not int or duration not in _ALLOWED_DURATIONS:
        raise VideoAutopilotError("invalid_duration")
    return StoryboardRequest(title, subtitle, scenes, palette, duration)


def validate_storyboard_request(payload: Any) -> StoryboardRequest:
    return _validate_storyboard_request(payload, minimum_scenes=2)


def validate_asset_storyboard_request(payload: Any) -> StoryboardRequest:
    return _validate_storyboard_request(payload, minimum_scenes=1)


_EDIT_PHRASES = {
    "order": {
        "forward": ("依上傳順序", "照上傳順序", "依序排列", "依序"),
        "reverse": ("倒序", "反向排列", "反過來"),
    },
    "transition": {
        "fade": ("淡化轉場", "淡入淡出", "柔和轉場"),
        "cut": ("直接切換", "不要轉場", "硬切"),
    },
    "motion": {
        "gentle_zoom": ("慢慢拉近", "平滑運鏡", "輕微推進"),
        "still": ("不要運鏡", "固定畫面"),
    },
    "audio": {
        "ambient": ("背景音樂", "柔和配樂", "保留配樂"),
        "silent": ("靜音", "不要音樂", "無配樂"),
    },
}
_EDIT_DEFAULTS = {
    "order": "forward",
    "transition": "fade",
    "motion": "gentle_zoom",
    "audio": "ambient",
}
_EDIT_LABELS = {
    "forward": "依上傳順序",
    "reverse": "倒序排列",
    "fade": "淡化轉場",
    "cut": "直接切換",
    "gentle_zoom": "平滑運鏡",
    "still": "固定畫面",
    "ambient": "柔和背景音樂",
    "silent": "靜音 AAC",
}


def interpret_edit_instructions(value: Any) -> EditPlan:
    if (
        not isinstance(value, str)
        or len(value) > 240
        or any(ord(character) < 32 and character not in "\r\n\t" for character in value)
    ):
        raise VideoAutopilotError("invalid_edit_instruction")
    clauses = [clause.strip() for clause in re.split(r"[，,、；;。\n]+", value) if clause.strip()]
    selected = dict(_EDIT_DEFAULTS)
    seen: dict[str, str] = {}
    for clause in clauses:
        matched = False
        for category, options in _EDIT_PHRASES.items():
            for option, phrases in options.items():
                if any(phrase in clause for phrase in phrases):
                    if category in seen and seen[category] != option:
                        raise VideoAutopilotError("conflicting_edit_instruction")
                    selected[category] = option
                    seen[category] = option
                    matched = True
        if not matched:
            raise VideoAutopilotError("unsupported_edit_instruction")
    encoded = json.dumps(selected, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return EditPlan(**selected, sha256=hashlib.sha256(encoded).hexdigest())


def public_edit_plan(plan: EditPlan) -> dict[str, Any]:
    return {
        "order": plan.order,
        "order_label": _EDIT_LABELS[plan.order],
        "transition": plan.transition,
        "transition_label": _EDIT_LABELS[plan.transition],
        "motion": plan.motion,
        "motion_label": _EDIT_LABELS[plan.motion],
        "audio": plan.audio,
        "audio_label": _EDIT_LABELS[plan.audio],
        "plan_sha256": plan.sha256,
        "pii_included": False,
    }


def _run(
    command: list[str], *, cwd: Path, timeout: float = 35.0,
    rss_samples: list[int] | None = None,
) -> int:
    try:
        process = subprocess.Popen(
            command, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=_environment(cwd),
        )
        import psutil
        monitor = psutil.Process(process.pid)
        started = time.monotonic()
        peak = 0
        while process.poll() is None:
            try:
                observed = int(monitor.memory_info().rss)
            except psutil.NoSuchProcess:
                observed = 0
            peak = max(peak, observed)
            if peak > 1536 * 1024 * 1024 or time.monotonic() - started >= timeout:
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2.0)
                raise VideoAutopilotError("video_resource_limit")
            time.sleep(0.04)
        returncode = int(process.wait(timeout=2.0))
        if rss_samples is not None:
            rss_samples.append(peak)
    except VideoAutopilotError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise VideoAutopilotError("video_engine_failed") from exc
    if returncode != 0 or peak <= 0:
        raise VideoAutopilotError("video_engine_failed")
    return returncode


def _load_normalizer():
    contract_path = _UPSTREAM / "MAGI_INTEGRATION.json"
    if not (_UPSTREAM / "LICENSE").is_file() or not contract_path.is_file():
        raise VideoAutopilotError("upstream_snapshot_missing")
    manifest = json.loads(contract_path.read_text(encoding="utf-8"))
    if (
        manifest.get("version") != UPSTREAM_VERSION
        or manifest.get("commit") != UPSTREAM_COMMIT
        or manifest.get("license") != "MIT"
        or manifest.get("runtime_network_allowed") is not False
        or manifest.get("user_media_persisted") is not False
    ):
        raise VideoAutopilotError("upstream_snapshot_mismatch")
    from third_party.video_autopilot_kit.runtime.portrait_normalizer import normalize_to_portrait
    return normalize_to_portrait


def _environment(cwd: Path) -> dict[str, str]:
    return {
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(cwd), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
    }


def _font_path() -> str:
    for candidate in (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/Library/Fonts/NotoSansCJKtc-Regular.otf",
    ):
        if Path(candidate).is_file():
            return candidate
    raise VideoAutopilotError("traditional_chinese_font_missing")


def _rgb(value: str) -> tuple[int, int, int]:
    return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))


def _blend(
    first: tuple[int, int, int], second: tuple[int, int, int], ratio: float,
) -> tuple[int, int, int]:
    return tuple(round(a + (b - a) * ratio) for a, b in zip(first, second, strict=True))


def _wrap_by_width(draw, text: str, font, *, width: int, maximum_lines: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for character in text:
        candidate = current + character
        bounds = draw.textbbox((0, 0), candidate, font=font)
        if current and bounds[2] - bounds[0] > width:
            lines.append(current.rstrip())
            current = character.lstrip()
        else:
            current = candidate
    if current:
        lines.append(current.rstrip())
    if not lines or len(lines) > maximum_lines:
        raise VideoAutopilotError("invalid_scenes")
    return lines


def _single_line_font(draw, text: str, font_path: str, *, width: int, largest: int, smallest: int):
    from PIL import ImageFont
    for size in range(largest, smallest - 1, -2):
        font = ImageFont.truetype(font_path, size)
        bounds = draw.textbbox((0, 0), text, font=font)
        if bounds[2] - bounds[0] <= width:
            return font
    raise VideoAutopilotError("invalid_scenes")


def _content_font_and_lines(draw, text: str, font_path: str):
    from PIL import ImageFont
    for size in range(70, 43, -3):
        font = ImageFont.truetype(font_path, size)
        try:
            return font, _wrap_by_width(draw, text, font, width=790, maximum_lines=4)
        except VideoAutopilotError:
            continue
    raise VideoAutopilotError("invalid_scenes")


def _scene_png(path: Path, request: StoryboardRequest, index: int, content: str) -> None:
    from PIL import Image, ImageDraw, ImageFont
    background, accent, text_color = _ALLOWED_PALETTES[request.palette]
    dark, bright = _rgb(background), _rgb(accent)
    top = _blend(dark, bright, 0.44 + 0.07 * (index % 3))
    bottom = _blend(dark, (0, 0, 0), 0.52)
    gradient = Image.new("RGB", (1, 1920))
    gradient.putdata([_blend(top, bottom, row / 1919) for row in range(1920)])
    canvas = gradient.resize((1080, 1920)).convert("RGBA")
    decoration = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shapes = ImageDraw.Draw(decoration)
    for ring in range(5):
        radius = 260 + ring * 82
        cx, cy = 930 - index * 45, 260 + index * 75
        shapes.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            outline=bright + (max(18, 72 - ring * 10),), width=18,
        )
    shapes.rounded_rectangle(
        (68, 1080, 1012, 1665), radius=58,
        fill=dark + (222,), outline=bright + (225,), width=5,
    )
    shapes.line((102, 1740, 978, 1740), fill=bright + (150,), width=3)
    canvas = Image.alpha_composite(canvas, decoration)
    draw = ImageDraw.Draw(canvas)
    font_path = _font_path()
    label_font = ImageFont.truetype(font_path, 29)
    title_font = _single_line_font(draw, request.title, font_path, width=928, largest=38, smallest=24)
    content_font, lines = _content_font_and_lines(draw, content, font_path)
    subtitle_font = _single_line_font(draw, request.subtitle or " ", font_path, width=730, largest=31, smallest=20)
    draw.rounded_rectangle((76, 88, 258, 145), radius=28, fill=bright + (245,))
    draw.text((108, 99), f"SCENE {index + 1:02d}", font=label_font, fill="#ffffff")
    draw.text((76, 184), request.title, font=title_font, fill=text_color)
    y = 1265 - len(lines) * 98 / 2
    for line in lines:
        bounds = draw.textbbox((0, 0), line, font=content_font, stroke_width=2)
        x = (1080 - (bounds[2] - bounds[0])) / 2
        draw.text(
            (x, y), line, font=content_font, fill=text_color,
            stroke_width=2, stroke_fill="#000000",
        )
        y += 98
    if request.subtitle:
        draw.text((96, 1780), request.subtitle, font=subtitle_font, fill="#ffffff")
    draw.text((886, 1780), f"{index + 1} / {len(request.scenes)}", font=subtitle_font, fill=text_color)
    canvas.save(path, format="PNG", optimize=True)


def _storyboard_sha256(request: StoryboardRequest) -> str:
    encoded = json.dumps(
        {
            "duration_seconds": request.duration_seconds, "palette": request.palette,
            "scenes": list(request.scenes), "subtitle": request.subtitle, "title": request.title,
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _probe(path: Path, *, expected_duration: int) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "/opt/homebrew/bin/ffprobe", "-v", "error", "-count_frames",
                "-show_entries",
                "format=duration:stream=codec_name,codec_type,width,height,avg_frame_rate,nb_read_frames",
                "-of", "json", str(path),
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=10, check=False,
        )
        report = json.loads(result.stdout.decode("utf-8")) if result.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VideoAutopilotError("video_attestation_failed") from exc
    streams = report.get("streams")
    if not isinstance(streams, list) or len(streams) != 2:
        raise VideoAutopilotError("video_attestation_failed")
    video = [item for item in streams if item.get("codec_type") == "video"]
    audio = [item for item in streams if item.get("codec_type") == "audio"]
    try:
        duration = float(report["format"]["duration"])
        frame_rate = float(Fraction(video[0]["avg_frame_rate"]))
        frame_count = int(video[0]["nb_read_frames"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise VideoAutopilotError("video_attestation_failed") from exc
    if (
        len(video) != 1 or len(audio) != 1
        or video[0].get("codec_name") != "h264"
        or video[0].get("width") != 1080 or video[0].get("height") != 1920
        or audio[0].get("codec_name") != "aac" or not math.isfinite(duration)
        or not expected_duration - 0.15 <= duration <= expected_duration + 0.25
        or not 29.9 <= frame_rate <= 30.1
        or not expected_duration * 29 <= frame_count <= expected_duration * 31
    ):
        raise VideoAutopilotError("video_attestation_failed")
    return {
        "width": 1080, "height": 1920, "duration_seconds": round(duration, 3),
        "frame_rate": round(frame_rate, 3), "frame_count": frame_count,
        "video_codec": "h264", "audio_codec": "aac",
    }


def _visual_quality(path: Path, request: StoryboardRequest) -> dict[str, Any]:
    frame_hashes: list[str] = []
    deviations: list[float] = []
    scene_duration = request.duration_seconds / len(request.scenes)
    for index in range(len(request.scenes)):
        timestamp = min(request.duration_seconds - 0.15, scene_duration * (index + 0.5))
        try:
            result = subprocess.run(
                [
                    "/opt/homebrew/bin/ffmpeg", "-v", "error", "-ss", f"{timestamp:.3f}",
                    "-i", str(path), "-frames:v", "1", "-vf", "scale=180:320",
                    "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
                ],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=12, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise VideoAutopilotError("video_visual_quality_failed") from exc
        raw = result.stdout
        if result.returncode != 0 or len(raw) != 180 * 320 * 3:
            raise VideoAutopilotError("video_visual_quality_failed")
        luminance = [
            (raw[offset] * 299 + raw[offset + 1] * 587 + raw[offset + 2] * 114) / 1000
            for offset in range(0, len(raw), 3)
        ]
        deviation, mean = statistics.pstdev(luminance), statistics.fmean(luminance)
        if not 12 <= mean <= 243 or deviation < 16:
            raise VideoAutopilotError("video_visual_quality_failed")
        deviations.append(deviation)
        frame_hashes.append(hashlib.sha256(raw).hexdigest())
    if len(set(frame_hashes)) != len(request.scenes):
        raise VideoAutopilotError("video_visual_quality_failed")
    return {
        "visual_sample_count": len(frame_hashes),
        "distinct_visual_sample_count": len(set(frame_hashes)),
        "minimum_sample_luma_stddev": round(min(deviations), 3),
    }


def render_storyboard(
    work_dir: Path, request: StoryboardRequest, plan: EditPlan | None = None,
) -> tuple[bytes, dict[str, Any]]:
    work_dir = work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    plan = plan or interpret_edit_instructions("")
    if plan.order == "reverse":
        request = StoryboardRequest(
            request.title, request.subtitle, tuple(reversed(request.scenes)),
            request.palette, request.duration_seconds,
        )
    normalizer = _load_normalizer()
    renderer_rss_samples: list[int] = []
    scene_count = len(request.scenes)
    transition_seconds = _TRANSITION_SECONDS if plan.transition == "fade" else 0
    clip_duration = (
        request.duration_seconds + transition_seconds * (scene_count - 1)
    ) / scene_count
    frame_count = math.ceil(clip_duration * 30) + 2
    clips: list[Path] = []
    for index, content in enumerate(request.scenes):
        image_path = work_dir / f"scene-{index:02d}.png"
        clip_path = work_dir / f"scene-{index:02d}.mp4"
        _scene_png(image_path, request, index, content)
        if plan.motion == "gentle_zoom":
            direction = "0.00045" if index % 2 == 0 else "0.00035"
            video_filter = (
                f"zoompan=z='min(zoom+{direction},1.055)':"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"d={frame_count}:s=1080x1920:fps=30,format=yuv420p"
            )
        else:
            video_filter = "fps=30,format=yuv420p"
        _run(
            [
                "/opt/homebrew/bin/ffmpeg", "-v", "error", "-y", "-loop", "1",
                "-i", str(image_path), "-vf", video_filter,
                "-t", f"{clip_duration:.4f}", "-an", "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "20", str(clip_path),
            ],
            cwd=work_dir, rss_samples=renderer_rss_samples,
        )
        clips.append(clip_path)

    assembled = work_dir / "storyboard-assembled.mp4"
    command = ["/opt/homebrew/bin/ffmpeg", "-v", "error", "-y"]
    for clip in clips:
        command.extend(["-i", str(clip)])
    if plan.transition == "cut":
        filters = [
            "".join(f"[{index}:v]" for index in range(scene_count))
            + f"concat=n={scene_count}:v=1:a=0[v]"
        ]
        output_label = "v"
    else:
        previous = "0:v"
        filters = []
        for index in range(1, scene_count):
            output_label = f"xf{index}"
            offset = index * clip_duration - index * _TRANSITION_SECONDS
            filters.append(
                f"[{previous}][{index}:v]xfade=transition=fade:"
                f"duration={_TRANSITION_SECONDS}:offset={offset:.4f}[{output_label}]"
            )
            previous = output_label
    command.extend(
        [
            "-filter_complex", ";".join(filters), "-map", f"[{output_label}]",
            "-t", str(request.duration_seconds), "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-r", "30", str(assembled),
        ]
    )
    _run(command, cwd=work_dir, rss_samples=renderer_rss_samples)

    normalized = work_dir / "storyboard-normalized.mp4"
    try:
        normalizer(
            assembled, normalized, ffmpeg=Path("/opt/homebrew/bin/ffmpeg"), cwd=work_dir,
            runner=lambda item: _run(item, cwd=work_dir, rss_samples=renderer_rss_samples),
            crf=20,
        )
    except Exception as exc:
        raise VideoAutopilotError("upstream_normalizer_failed") from exc

    output = work_dir / "magi-storyboard.mp4"
    _mux_verified_audio(
        work_dir, normalized, request, plan, renderer_rss_samples, output,
    )
    media = output.read_bytes()
    if not media or len(media) > 32 * 1024 * 1024:
        raise VideoAutopilotError("video_attestation_failed")
    attestation = _probe(output, expected_duration=request.duration_seconds)
    attestation.update(_visual_quality(output, request))
    attestation.update(
        {
            "engine_contract": ENGINE_CONTRACT, "upstream_commit": UPSTREAM_COMMIT,
            "upstream_version": UPSTREAM_VERSION,
            "storyboard_sha256": _storyboard_sha256(request),
            "edit_plan_sha256": plan.sha256,
            "scene_count": scene_count,
            "output_sha256": hashlib.sha256(media).hexdigest(), "output_bytes": len(media),
            "upload_persisted": False, "network_used": False,
            "external_publish_used": False,
            "renderer_peak_rss_bytes": max(renderer_rss_samples),
            "renderer_process_count": len(renderer_rss_samples),
        }
    )
    return media, attestation


def _validate_asset(asset: AssetInput) -> None:
    path = asset.path
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise VideoAutopilotError("invalid_media_asset")
    data_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    if data_sha256 != asset.sha256:
        raise VideoAutopilotError("invalid_media_asset")
    if asset.kind == "image":
        try:
            from PIL import Image
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
                image_format = str(image.format or "").upper()
        except Exception as exc:
            raise VideoAutopilotError("invalid_media_asset") from exc
        if image_format not in {"JPEG", "PNG", "WEBP"} or width < 160 or height < 160 or width * height > 40_000_000:
            raise VideoAutopilotError("invalid_media_asset")
        return
    if asset.kind != "video":
        raise VideoAutopilotError("invalid_media_asset")
    try:
        result = subprocess.run(
            [
                "/opt/homebrew/bin/ffprobe", "-v", "error", "-show_entries",
                "format=duration:stream=codec_type,width,height", "-of", "json", str(path),
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=12, check=False,
        )
        report = json.loads(result.stdout.decode("utf-8")) if result.returncode == 0 else {}
        videos = [row for row in report.get("streams", []) if row.get("codec_type") == "video"]
        duration = float(report["format"]["duration"])
        width, height = int(videos[0]["width"]), int(videos[0]["height"])
    except (
        OSError,
        subprocess.SubprocessError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        ValueError,
    ) as exc:
        raise VideoAutopilotError("invalid_media_asset") from exc
    if len(videos) != 1 or not math.isfinite(duration) or not 0.3 <= duration <= 120 or max(width, height) > 4096:
        raise VideoAutopilotError("invalid_media_asset")


def _scene_overlay_png(path: Path, request: StoryboardRequest, index: int, content: str) -> None:
    from PIL import Image, ImageDraw, ImageFont
    background, accent, text_color = _ALLOWED_PALETTES[request.palette]
    dark, bright = _rgb(background), _rgb(accent)
    canvas = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((54, 76, 280, 142), radius=32, fill=dark + (220,), outline=bright + (230,), width=3)
    draw.rounded_rectangle((54, 1100, 1026, 1680), radius=58, fill=dark + (224,), outline=bright + (235,), width=5)
    font_path = _font_path()
    label_font = ImageFont.truetype(font_path, 28)
    title_font = _single_line_font(draw, request.title, font_path, width=900, largest=36, smallest=22)
    content_font, lines = _content_font_and_lines(draw, content, font_path)
    subtitle_font = _single_line_font(draw, request.subtitle or " ", font_path, width=720, largest=30, smallest=19)
    draw.text((86, 91), f"素材 {index + 1:02d} / {len(request.scenes):02d}", font=label_font, fill="#ffffff")
    draw.text((74, 1016), request.title, font=title_font, fill=text_color, stroke_width=2, stroke_fill="#000000")
    y = 1320 - len(lines) * 94 / 2
    for line in lines:
        bounds = draw.textbbox((0, 0), line, font=content_font, stroke_width=2)
        x = (1080 - (bounds[2] - bounds[0])) / 2
        draw.text((x, y), line, font=content_font, fill=text_color, stroke_width=2, stroke_fill="#000000")
        y += 94
    if request.subtitle:
        draw.text(
            (82, 1748), request.subtitle, font=subtitle_font,
            fill="#ffffff", stroke_width=1, stroke_fill="#000000",
        )
    canvas.save(path, format="PNG", optimize=True)


def _assemble_asset_clips(
    work_dir: Path, clips: list[Path], request: StoryboardRequest, plan: EditPlan,
    renderer_rss_samples: list[int],
) -> Path:
    assembled = work_dir / "assets-assembled.mp4"
    command = ["/opt/homebrew/bin/ffmpeg", "-v", "error", "-y"]
    for clip in clips:
        command.extend(["-i", str(clip)])
    if len(clips) == 1:
        filters = "[0:v]null[v]"
        output_label = "v"
    elif plan.transition == "cut":
        filters = "".join(f"[{index}:v]" for index in range(len(clips)))
        filters += f"concat=n={len(clips)}:v=1:a=0[v]"
        output_label = "v"
    else:
        clip_duration = (request.duration_seconds + _TRANSITION_SECONDS * (len(clips) - 1)) / len(clips)
        previous = "0:v"
        rows: list[str] = []
        for index in range(1, len(clips)):
            output_label = f"xf{index}"
            offset = index * clip_duration - index * _TRANSITION_SECONDS
            rows.append(
                f"[{previous}][{index}:v]xfade=transition=fade:duration={_TRANSITION_SECONDS}:"
                f"offset={offset:.4f}[{output_label}]"
            )
            previous = output_label
        filters = ";".join(rows)
    command.extend(
        [
            "-filter_complex", filters, "-map", f"[{output_label}]",
            "-t", str(request.duration_seconds), "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
            str(assembled),
        ]
    )
    _run(command, cwd=work_dir, rss_samples=renderer_rss_samples)
    return assembled


def _mux_verified_audio(
    work_dir: Path, video: Path, request: StoryboardRequest, plan: EditPlan,
    renderer_rss_samples: list[int], output: Path,
) -> None:
    command = ["/opt/homebrew/bin/ffmpeg", "-v", "error", "-y", "-i", str(video)]
    if plan.audio == "silent":
        command.extend(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
        audio_filter = "[1:a]atrim=duration=%s[a]" % request.duration_seconds
    else:
        frequencies = {
            "ocean": (196, 247, 330), "sunset": (220, 277, 370), "forest": (174, 220, 294),
        }[request.palette]
        for frequency in frequencies:
            command.extend([
                "-f", "lavfi", "-i",
                f"sine=frequency={frequency}:duration={request.duration_seconds}",
            ])
        audio_start = max(0.0, request.duration_seconds - 0.8)
        audio_filter = (
            "[1:a][2:a][3:a]amix=inputs=3:duration=longest:normalize=0,volume=0.025,"
            f"afade=t=in:st=0:d=0.45,afade=t=out:st={audio_start:.3f}:d=0.8[a]"
        )
    command.extend(
        [
            "-filter_complex", audio_filter, "-map", "0:v:0", "-map", "[a]",
            "-t", str(request.duration_seconds), "-c:v", "copy", "-c:a", "aac",
            "-b:a", "128k", "-movflags", "+faststart", str(output),
        ]
    )
    _run(command, cwd=work_dir, rss_samples=renderer_rss_samples)


def render_asset_storyboard(
    work_dir: Path, request: StoryboardRequest, assets: tuple[AssetInput, ...], plan: EditPlan,
) -> tuple[bytes, dict[str, Any]]:
    work_dir = work_dir.resolve()
    if len(assets) != len(request.scenes) or not 1 <= len(assets) <= 5:
        raise VideoAutopilotError("asset_scene_count_mismatch")
    for asset in assets:
        _validate_asset(asset)
    pairs = list(zip(assets, request.scenes, strict=True))
    if plan.order == "reverse":
        pairs.reverse()
    renderer_rss_samples: list[int] = []
    clip_duration = request.duration_seconds / len(pairs)
    if plan.transition == "fade" and len(pairs) > 1:
        clip_duration = (request.duration_seconds + _TRANSITION_SECONDS * (len(pairs) - 1)) / len(pairs)
    frame_count = math.ceil(clip_duration * 30) + 2
    clips: list[Path] = []
    for index, (asset, content) in enumerate(pairs):
        overlay = work_dir / f"asset-overlay-{index:02d}.png"
        clip = work_dir / f"asset-scene-{index:02d}.mp4"
        _scene_overlay_png(overlay, request, index, content)
        command = ["/opt/homebrew/bin/ffmpeg", "-v", "error", "-y"]
        if asset.kind == "image":
            command.extend(["-loop", "1", "-i", str(asset.path)])
            if plan.motion == "gentle_zoom":
                base_filter = (
                    "scale=1200:2134:force_original_aspect_ratio=increase,crop=1200:2134,"
                    f"zoompan=z='min(zoom+0.0004,1.05)':x='iw/2-(iw/zoom/2)':"
                    f"y='ih/2-(ih/zoom/2)':d={frame_count}:s=1080x1920:fps=30"
                )
            else:
                base_filter = (
                    "scale=1080:1920:force_original_aspect_ratio=increase,"
                    "crop=1080:1920,fps=30,setsar=1"
                )
        else:
            command.extend(["-stream_loop", "-1", "-i", str(asset.path)])
            base_filter = (
                "scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,fps=30,setsar=1"
            )
        command.extend([
            "-loop", "1", "-i", str(overlay), "-filter_complex",
            f"[0:v]{base_filter},format=yuv420p[base];[base][1:v]overlay=0:0:format=auto,format=yuv420p[v]",
            "-map", "[v]", "-t", f"{clip_duration:.4f}", "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "20", "-r", "30", str(clip),
        ])
        _run(command, cwd=work_dir, rss_samples=renderer_rss_samples)
        clips.append(clip)
    assembled = _assemble_asset_clips(work_dir, clips, request, plan, renderer_rss_samples)
    output = work_dir / "magi-asset-storyboard.mp4"
    _mux_verified_audio(work_dir, assembled, request, plan, renderer_rss_samples, output)
    media = output.read_bytes()
    if not media or len(media) > 64 * 1024 * 1024:
        raise VideoAutopilotError("video_attestation_failed")
    attestation = _probe(output, expected_duration=request.duration_seconds)
    ordered_request = StoryboardRequest(
        request.title, request.subtitle, tuple(content for _asset, content in pairs),
        request.palette, request.duration_seconds,
    )
    attestation.update(_visual_quality(output, ordered_request))
    asset_digest = hashlib.sha256("\n".join(asset.sha256 for asset, _content in pairs).encode("ascii")).hexdigest()
    attestation.update({
        "engine_contract": ENGINE_CONTRACT,
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_version": UPSTREAM_VERSION,
        "storyboard_sha256": _storyboard_sha256(ordered_request),
        "asset_set_sha256": asset_digest,
        "edit_plan_sha256": plan.sha256,
        "scene_count": len(pairs),
        "output_sha256": hashlib.sha256(media).hexdigest(),
        "output_bytes": len(media),
        "upload_persisted": False,
        "network_used": False,
        "external_publish_used": False,
        "renderer_peak_rss_bytes": max(renderer_rss_samples),
        "renderer_process_count": len(renderer_rss_samples),
    })
    return media, attestation
