"""Portrait normalizer derived from video-autopilot-kit shorts_vertical.py.

Upstream commit: 6dc9ad8b3dc9b2ef158e4eac835f5f721e5b3bed
Upstream source SHA-256: 67a84cf455aadd14924978b9147ec406bfdad73e0f83b4ce2d691c034d627942

The MAGI adapter supplies a fixed ffmpeg executable and a private environment;
the geometric filter and output contract are kept from the upstream function.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable


PORTRAIT_FILTER = (
    "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
    "fps=30,setsar=1,format=yuv420p"
)


def normalize_to_portrait(
    clip_in: Path,
    clip_out: Path,
    *,
    ffmpeg: Path,
    cwd: Path,
    runner: Callable[[list[str]], int],
    crf: int = 22,
) -> Path:
    """Normalize one clip to upright 1080x1920/30fps with no audio."""
    returncode = runner(
        [
            str(ffmpeg),
            "-v",
            "error",
            "-y",
            "-i",
            str(clip_in),
            "-vf",
            PORTRAIT_FILTER,
            "-an",
            "-c:v",
            "libx264",
            "-crf",
            str(crf),
            "-preset",
            "medium",
            str(clip_out),
        ]
    )
    if returncode != 0 or not clip_out.is_file() or clip_out.is_symlink():
        raise RuntimeError("normalize_to_portrait_failed")
    return clip_out
