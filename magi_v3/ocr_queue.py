"""One authoritative path contract for the NAS PDF OCR SQLite queue."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


QUEUE_DB_ENV = "MAGI_NAS_OCR_QUEUE_DB_PATH"
LEGACY_QUEUE_DB_NAME = ".magi_nas_ocr_queue.db"


class OCRQueuePathError(RuntimeError):
    """The OCR queue path is missing or unsafe for the active runtime."""


def _sealed_v3_context(release_root: Path, environ: Mapping[str, str]) -> bool:
    return bool(
        str(environ.get("MAGI_V3_RELEASE_ID") or "").strip()
        or str(environ.get("MAGI_V3_DEPLOYMENT_MODE") or "").strip()
        or str(environ.get("MAGI_V3_RELEASE_MANIFEST") or "").strip()
        or (release_root / "release-manifest.json").is_file()
        or (release_root / "RELEASE_COMPLETE.json").is_file()
    )


def resolve_nas_ocr_queue_db_path(
    *,
    environ: Mapping[str, str] | None = None,
    release_root: Path | str | None = None,
) -> Path:
    """Resolve the shared OCR queue without allowing a sealed V3 fallback.

    V2/source execution retains the historical home-directory database when
    no explicit binding is present.  A sealed V3 release must declare the
    same external database explicitly so producers, workers, and diagnostics
    cannot silently create independent queues under different runtime roots.
    """

    env = os.environ if environ is None else environ
    root = Path(release_root or Path(__file__).resolve().parents[1]).expanduser().resolve()
    sealed = _sealed_v3_context(root, env)
    declared = str(env.get(QUEUE_DB_ENV) or "").strip()
    if not declared:
        if sealed:
            raise OCRQueuePathError(
                f"sealed V3 release requires {QUEUE_DB_ENV}"
            )
        return Path.home() / LEGACY_QUEUE_DB_NAME

    raw = Path(declared).expanduser()
    if not raw.is_absolute():
        raise OCRQueuePathError(f"{QUEUE_DB_ENV} must be an absolute path")
    if raw.is_symlink():
        raise OCRQueuePathError(f"{QUEUE_DB_ENV} must not be a symlink")
    resolved = raw.resolve(strict=False)
    if raw != resolved:
        raise OCRQueuePathError(f"{QUEUE_DB_ENV} must be canonical")
    if sealed and (resolved == root or resolved.is_relative_to(root)):
        raise OCRQueuePathError(
            f"{QUEUE_DB_ENV} must stay outside the sealed V3 release"
        )
    return resolved
