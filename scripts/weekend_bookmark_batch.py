#!/usr/bin/env python3
"""
weekend_bookmark_batch.py — 週六批次自動書籤（兩階段）

Stage 1: regex (pdf-bookmarker) 快速掃描，建立基本導覽書籤
Stage 2: vision (oMLX gemma-4) 逐頁補漏，修正 regex 辨識不到的頁面

已完成的 PDF 記錄在 state file 中，下次跑自動跳過。
首次全量可能需要 2-3 個週末，之後增量每週 ~1 小時。

排程：每週六 03:00
"""
from __future__ import annotations

import importlib.util
import errno
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

MAGI_ROOT = Path(__file__).resolve().parents[1]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("weekend-bookmark")


class FileScanTimeout(TimeoutError):
    pass


class VisionSourceChanged(RuntimeError):
    pass


_TRANSIENT_STORAGE_ERROR_TOKENS = (
    "device not configured",
    "socket is not connected",
    "input/output error",
    "transport endpoint is not connected",
    "network is down",
    "network is unreachable",
    "stale file handle",
)
_TRANSIENT_STORAGE_ERRNOS = {
    errno.EIO,
    errno.ENXIO,
    errno.ENODEV,
    errno.ENOTCONN,
    errno.ENETDOWN,
    errno.ENETUNREACH,
    errno.ESTALE,
}


def _is_transient_storage_error(exc: BaseException) -> bool:
    """Identify an SMB/File Provider outage without hiding corrupt PDFs."""

    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        err_no = getattr(current, "errno", None)
        if isinstance(err_no, int) and err_no in _TRANSIENT_STORAGE_ERRNOS:
            return True
        message = str(current).strip().lower()
        if any(token in message for token in _TRANSIENT_STORAGE_ERROR_TOKENS):
            return True
        current = current.__cause__ or current.__context__
    return False


def _source_unavailable_is_transient(pdf: Path, exc: BaseException) -> bool:
    """Treat a just-discovered network source disappearing as a retryable outage."""
    if _is_transient_storage_error(exc):
        return True
    if not isinstance(exc, FileNotFoundError):
        return False
    path = str(pdf)
    return (
        path.startswith("/Volumes/")
        or path.startswith("/Network/")
        or "/Library/CloudStorage/" in path
    )


def _failed_result_is_transient_network_source(pdf: Path, result: Any) -> bool:
    """Recognize scanners that return an ENOENT message instead of raising it.

    Some bookmark backends catch ``FileNotFoundError`` internally and return a
    ``success=false`` payload.  Restrict this classification to a network
    source whose returned message names that source, so a corrupt local PDF or
    an unrelated missing dependency remains a genuine error.
    """

    if not isinstance(result, dict) or result.get("success") is True:
        return False
    source = str(pdf)
    if not (
        source.startswith("/Volumes/")
        or source.startswith("/Network/")
        or "/Library/CloudStorage/" in source
    ):
        return False
    message = str(result.get("message") or result.get("error") or "").strip()
    lowered = message.lower()
    missing_source = any(
        token in lowered
        for token in (
            "no such file",
            "file not found",
            "cannot find the file",
        )
    )
    return missing_source and (source in message or pdf.name in message)


class _file_timeout:
    def __init__(self, seconds: int):
        self.seconds = max(0, int(seconds or 0))
        self._old_handler = None

    def __enter__(self):
        if self.seconds <= 0 or not hasattr(signal, "SIGALRM"):
            return self
        self._old_handler = signal.getsignal(signal.SIGALRM)

        def _raise_timeout(_signum, _frame):
            raise FileScanTimeout(f"single PDF scan exceeded {self.seconds}s")

        signal.signal(signal.SIGALRM, _raise_timeout)
        signal.alarm(self.seconds)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.seconds > 0 and hasattr(signal, "SIGALRM"):
            signal.alarm(0)
            if self._old_handler is not None:
                signal.signal(signal.SIGALRM, self._old_handler)
        return False

# ── State persistence ─────────────────────────────────────────────────────────
_LEGACY_STATE_FILE = MAGI_ROOT / ".agent" / "bookmark_batch_state.json"
_AGENT_DIR = Path(os.environ.get("MAGI_AGENT_DIR") or str(MAGI_ROOT / ".agent")).expanduser()
_RUNTIME_DIR = Path(os.environ.get("MAGI_RUNTIME_DIR") or str(MAGI_ROOT / ".runtime")).expanduser()
STATE_FILE = Path(
    os.environ.get("MAGI_BOOKMARK_BATCH_STATE_PATH") or _AGENT_DIR / "bookmark_batch_state.json"
).expanduser()

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_WALLCLOCK_BUDGET_SECONDS = int(
    os.environ.get("BOOKMARK_WALLCLOCK_BUDGET_SEC", "18000")
)  # 5 hours; scheduler timeout is 6 hours
VISION_BUDGET_SECONDS = int(
    os.environ.get("BOOKMARK_VISION_BUDGET_SEC", str(DEFAULT_WALLCLOCK_BUDGET_SECONDS))
)
VISION_MAX_PAGES = int(os.environ.get("BOOKMARK_VISION_MAX_PAGES", "350"))
VISION_PER_PAGE_TIMEOUT = 30  # seconds per vision call
MAX_FILE_SCAN_TIMEOUT_SECONDS = 45
OMLX_STARTUP_BUDGET_SECONDS = 45
BOOKMARK_WORKER_MARKER = "__MAGI_BOOKMARK_SCAN_JSON__"
BOOKMARK_WORKER_POLL_SECONDS = 0.5
BOOKMARK_WORKER_STOP_GRACE_SECONDS = 3.0
RESOURCE_CHECK_INTERVAL_SECONDS = float(
    os.environ.get("BOOKMARK_RESOURCE_CHECK_INTERVAL_SEC", "30")
)
TARGET_SUBDIRS = ["05_閱卷資料", "06_閱卷資料", "06_證據資料"]
BACKFILL_PLAN_PATH = Path(
    os.environ.get("MAGI_BOOKMARK_BACKFILL_PLAN_PATH")
    or _RUNTIME_DIR / "bookmark_backfill_plan_latest.json"
).expanduser()
FOLLOWUP_PLAN_PATH = Path(
    os.environ.get("MAGI_BOOKMARK_FOLLOWUP_PLAN_PATH")
    or _RUNTIME_DIR / "bookmark_followup_plan_latest.json"
).expanduser()
RECEIPT_PATH = Path(
    os.environ.get("MAGI_BOOKMARK_BATCH_RECEIPT_PATH")
    or _RUNTIME_DIR / "bookmark_batch_receipt_latest.json"
).expanduser()
SINGLE_DOC_FASTPATH_RE = re.compile(
    r"(?:判決|裁定|聲請書|申請書|申冤表格|調查報告|不起訴處分書|起訴書|答辯狀|陳報狀|抗告狀|上訴狀)"
)
MERGED_RECORD_HINT_RE = re.compile(
    r"(?:全案卷宗|調查卷|卷\d+|卷[一二三四五六七八九十]|DOC_|_OCR|P\d+|P\d+-\d+)"
)
FILENAME_BOOKMARK_FALLBACK_RE = re.compile(
    r"(?:"
    r"P\d+\s*[-~－—]\s*\d+|"
    r"(?:卷|卷宗)\s*\d+|卷[一二三四五六七八九十]+|"
    r"DOC_\d+|"
    r"手機|群組|對話|聯絡人|LINE|mission|"
    r"限閱|遮隱|調查卷|閱卷資料|光碟|錄音|錄影|監視器|照片|相片"
    r")",
    re.IGNORECASE,
)
WATERMARK_ONLY_RE = re.compile(
    r"(?:司法院線上閱卷系統作業平台|[\u4e00-\u9fff]{2,4}律師|\d{2,3}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})"
)

_STOP_REQUESTED = False
_STOP_SIGNAL = 0
_RESOURCE_LAST_CHECK_AT = 0.0
_RESOURCE_LAST_RESULT: tuple[bool, str] = (False, "")

# ── Imports ───────────────────────────────────────────────────────────────────
try:
    from api.case_path_mapper import preferred_case_roots
except ImportError:
    logger.error("Cannot import case_path_mapper — aborting")
    sys.exit(1)

from scripts.ops.pdf_mutation_lock import pdf_in_place_mutation_lock
from magi_v3.ocr_queue import resolve_nas_ocr_queue_db_path

_BOOKMARK_VALIDATOR_DIR = MAGI_ROOT / "skills" / "pdf-bookmarker"
if str(_BOOKMARK_VALIDATOR_DIR) not in sys.path:
    sys.path.insert(0, str(_BOOKMARK_VALIDATOR_DIR))
from bookmark_validator import normalize_bookmark


def _load_bookmarker_impl():
    """Import the in-process implementation inside the isolated worker only."""
    bm_path = MAGI_ROOT / "skills" / "pdf-bookmarker" / "action.py"
    if not bm_path.exists():
        logger.error(f"pdf-bookmarker not found at {bm_path}")
        sys.exit(1)
    spec = importlib.util.spec_from_file_location("pdf_bookmarker_action", str(bm_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.scan_and_bookmark


def _terminate_bookmark_worker(
    proc: subprocess.Popen,
    *,
    grace_sec: float = BOOKMARK_WORKER_STOP_GRACE_SECONDS,
) -> None:
    """Terminate and reap a bookmark worker and every OCR descendant."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.communicate(timeout=max(0.1, float(grace_sec)))
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.communicate(timeout=2.0)
    except Exception:
        pass


def _isolated_scan_and_bookmark(
    pdf_path: str,
    output_path: str | None = None,
    dry_run: bool = False,
    default_name: str = "",
    min_text_len: int = 30,
    rebuild_existing: bool = False,
) -> dict:
    """Run one native PDF/OCR scan in a bounded, reaped subprocess.

    PyMuPDF and ONNX Runtime execute native code which cannot be safely
    interrupted by ``SIGALRM`` inside a long-lived owner. A wedged or crashed
    file must therefore end only its worker, never the whole weekend batch.
    """
    try:
        timeout_sec = min(
            MAX_FILE_SCAN_TIMEOUT_SECONDS,
            max(
                1,
                int(
                    os.environ.get(
                        "BOOKMARK_FILE_TIMEOUT_SEC",
                        str(MAX_FILE_SCAN_TIMEOUT_SECONDS),
                    )
                ),
            ),
        )
    except (TypeError, ValueError):
        timeout_sec = MAX_FILE_SCAN_TIMEOUT_SECONDS
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--_scan-file-worker",
        str(pdf_path),
        "--_scan-min-text-len",
        str(max(0, int(min_text_len or 0))),
    ]
    if output_path:
        command.extend(["--_scan-output", str(output_path)])
    if default_name:
        command.extend(["--_scan-default-name", str(default_name)])
    if dry_run:
        command.append("--_scan-dry-run")
    if rebuild_existing:
        command.append("--_scan-rebuild-existing")

    env = os.environ.copy()
    try:
        threads = max(1, min(2, int(env.get("MAGI_BOOKMARK_OCR_THREADS", "2"))))
    except (TypeError, ValueError):
        threads = 2
    for name in (
        "OMP_NUM_THREADS",
        "OMP_THREAD_LIMIT",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        env[name] = str(threads)
    env["MAGI_BOOKMARK_WORKER_CHILD"] = "1"

    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=(os.name == "posix"),
    )
    deadline = time.monotonic() + timeout_sec
    stdout = ""
    stderr = ""
    while True:
        if _STOP_REQUESTED:
            _terminate_bookmark_worker(proc)
            raise FileScanTimeout("single PDF scan stopped at safe checkpoint")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_bookmark_worker(proc)
            raise FileScanTimeout(f"single PDF scan exceeded {timeout_sec}s")
        try:
            stdout, stderr = proc.communicate(
                timeout=min(BOOKMARK_WORKER_POLL_SECONDS, remaining)
            )
            break
        except subprocess.TimeoutExpired:
            continue

    if proc.returncode is not None and proc.returncode < 0:
        try:
            signal_name = signal.Signals(-proc.returncode).name
        except Exception:
            signal_name = str(-proc.returncode)
        raise RuntimeError(f"bookmark_pdf_worker_crashed:{signal_name}")
    marker_at = stdout.rfind(BOOKMARK_WORKER_MARKER)
    if marker_at < 0:
        raise RuntimeError(
            f"bookmark_pdf_worker_invalid_result:rc={proc.returncode}:"
            f"{(stderr or stdout)[-240:]}"
        )
    raw = stdout[marker_at + len(BOOKMARK_WORKER_MARKER) :].strip()
    try:
        payload = json.loads(raw)
    except Exception as exc:
        raise RuntimeError("bookmark_pdf_worker_invalid_json") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        worker_error = payload.get("error") if isinstance(payload, dict) else payload
        raise RuntimeError(
            f"bookmark_pdf_worker_failed:rc={proc.returncode}:"
            f"{str(worker_error)[:240]}"
        )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("bookmark_pdf_worker_invalid_payload")
    return result


def _load_bookmarker():
    """Return the crash-contained per-file scanner used by batch owners."""
    return _isolated_scan_and_bookmark


def _bookmark_worker_main(argv: list[str]) -> int:
    """Hidden entrypoint for exactly one PDF scan."""
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("pdf_path")
    parser.add_argument("--_scan-output", default="")
    parser.add_argument("--_scan-default-name", default="")
    parser.add_argument("--_scan-min-text-len", type=int, default=30)
    parser.add_argument("--_scan-dry-run", action="store_true")
    parser.add_argument("--_scan-rebuild-existing", action="store_true")
    args = parser.parse_args(argv)
    try:
        if os.name == "posix":
            nice = max(0, min(15, int(os.environ.get("MAGI_BOOKMARK_WORKER_NICE", "10"))))
            os.nice(nice)
    except Exception:
        pass
    try:
        scan = _load_bookmarker_impl()
        result = scan(
            args.pdf_path,
            output_path=args._scan_output or None,
            dry_run=bool(args._scan_dry_run),
            default_name=args._scan_default_name,
            min_text_len=max(0, int(args._scan_min_text_len)),
            rebuild_existing=bool(args._scan_rebuild_existing),
        )
        payload = {"ok": True, "result": result}
        returncode = 0
    except BaseException as exc:
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        returncode = 2
    print(BOOKMARK_WORKER_MARKER + json.dumps(payload, ensure_ascii=False, default=str))
    return returncode


def _load_vision_gateway():
    """Import InferenceGateway for vision calls."""
    try:
        from skills.bridge.inference_gateway import InferenceGateway
        return InferenceGateway()
    except Exception as e:
        logger.warning(f"Cannot load InferenceGateway: {e}")
        return None


def _load_state() -> dict:
    try:
        candidates = [STATE_FILE]
        if STATE_FILE.resolve(strict=False) != _LEGACY_STATE_FILE.resolve(strict=False):
            candidates.append(_LEGACY_STATE_FILE)
        for candidate in candidates:
            if candidate.exists():
                return json.loads(candidate.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"completed": {}, "vision_done": {}, "vision_progress": {}, "last_run": None}


def _set_state_file(path: str | None) -> None:
    """Allow priority/example runs to keep their progress outside the global batch state."""
    if not path:
        return
    global STATE_FILE
    state_path = Path(path).expanduser()
    if not state_path.is_absolute():
        state_path = (_RUNTIME_DIR if os.environ.get("MAGI_RUNTIME_DIR") else MAGI_ROOT) / state_path
    STATE_FILE = state_path


def _save_state(state: dict):
    """Atomically persist resumable progress without exposing a partial JSON file."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["last_run"] = datetime.now().isoformat()
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{STATE_FILE.name}.",
        suffix=".tmp",
        dir=str(STATE_FILE.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, STATE_FILE)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _request_stop(signum, _frame) -> None:
    """Signal-safe handler: defer all writes and cleanup to the next safe point."""
    global _STOP_REQUESTED, _STOP_SIGNAL
    _STOP_REQUESTED = True
    _STOP_SIGNAL = int(signum or 0)


def _install_stop_handlers() -> dict[int, Any]:
    global _STOP_REQUESTED, _STOP_SIGNAL
    _STOP_REQUESTED = False
    _STOP_SIGNAL = 0
    previous: dict[int, Any] = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, _request_stop)
    except Exception:
        _restore_stop_handlers(previous)
        raise
    return previous


def _restore_stop_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except Exception:
            pass


def _resource_pressure() -> tuple[bool, str]:
    """Return whether work must defer; sampling is cached but checked at every boundary."""
    global _RESOURCE_LAST_CHECK_AT, _RESOURCE_LAST_RESULT
    now = time.monotonic()
    if (
        _RESOURCE_LAST_CHECK_AT
        and now - _RESOURCE_LAST_CHECK_AT < max(0.0, RESOURCE_CHECK_INTERVAL_SECONDS)
    ):
        return _RESOURCE_LAST_RESULT
    try:
        from scripts.ops import resource_governor

        decision = resource_governor.classify(resource_governor.collect_snapshot())
        blocked = decision.level in {"throttle", "core_only", "critical"}
        reason = "resource_pressure:" + decision.level if blocked else ""
        result = (blocked, reason)
    except Exception as exc:
        logger.warning("Resource pressure check failed; deferring safely: %s", exc)
        result = (True, f"resource_check_failed:{type(exc).__name__}")
    _RESOURCE_LAST_CHECK_AT = now
    _RESOURCE_LAST_RESULT = result
    return result


def _defer_reason(deadline: float | None, *, check_resources: bool = True) -> str:
    if _STOP_REQUESTED:
        return f"signal:{_STOP_SIGNAL or 'stop'}"
    if deadline is not None and time.monotonic() >= deadline:
        return "wallclock_budget_exhausted"
    if check_resources:
        blocked, reason = _resource_pressure()
        if blocked:
            return reason
    return ""


def _pdf_state_snapshot(pdf: Path) -> tuple[str, tuple[int, int, int, int]]:
    """Capture the state fields used to bind stage results to one PDF version."""
    stat = pdf.stat()
    identity = (
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
    )
    return str(stat.st_mtime), identity


def _state_identity(value: Any) -> tuple[int, int, int, int]:
    try:
        identity = tuple(int(item) for item in (value or []))
    except Exception:
        return ()
    return identity if len(identity) == 4 else ()


def _save_loop_progress(
    state: dict,
    key: str,
    index: int,
    total: int,
    stats: dict | None = None,
) -> None:
    info = (state.get("completed") or {}).get(key)
    if isinstance(info, dict) and info.get("stage1"):
        try:
            stored_mtime, stored_identity = _pdf_state_snapshot(Path(key))
            info["mtime"] = stored_mtime
            info["identity"] = list(stored_identity)
            info.pop("identity_untrusted", None)
        except Exception as exc:
            # Never allow a stage1 result with an unverifiable source identity
            # to flow into vision.  The next run must re-evaluate the PDF.
            info["stage1"] = False
            info.pop("identity", None)
            info["identity_untrusted"] = True
            info["message"] = f"PDF identity unavailable after stage1: {type(exc).__name__}: {exc}"[:500]
            if stats is not None:
                if _source_unavailable_is_transient(Path(key), exc):
                    stats["deferred"] = True
                    stats["defer_reason"] = "storage_unavailable"
                else:
                    stats["errors"] = int(stats.get("errors") or 0) + 1
    state["last_file"] = key
    # This is a durable cursor, not a claim that the whole corpus completed.
    # A bounded run resumes after this file so an early alphabetical volume
    # cannot monopolise every scheduled window.
    state["continuation_cursor"] = key
    state["last_index"] = index
    state["last_total"] = total
    _save_state(state)


def _resume_pdf_order(pdfs: list[Path], state: dict) -> list[Path]:
    """Rotate a stable corpus after the last safe checkpoint.

    Discovery remains deterministic; only the starting point changes.  This
    prevents a long backlog from repeatedly starving later matters while
    preserving the per-file identity checks that make re-entry safe.
    """
    ordered = sorted(pdfs, key=lambda item: str(item))
    cursor = str(state.get("continuation_cursor") or state.get("last_file") or "")
    if not ordered or not cursor:
        return ordered
    for index, pdf in enumerate(ordered):
        if str(pdf) == cursor:
            return ordered[index + 1 :] + ordered[: index + 1]
    return ordered


def _write_receipt(*, state: dict, pdf_count: int, status: str, reason: str, stats: dict) -> None:
    """Emit a compact, source-free receipt for the next operator/run."""
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(),
        "status": status,
        "reason": reason,
        "pdf_count": int(pdf_count),
        "continuation_cursor": str(state.get("continuation_cursor") or ""),
        "last_file": str(state.get("last_file") or ""),
        "resource_gate": str(
            stats.get("defer_reason") if isinstance(stats, dict) else ""
        ),
        "processed": int((stats or {}).get("processed") or 0),
        "deferred_large_ocr": int((stats or {}).get("deferred_large_ocr") or 0),
        "file_timeout": int((stats or {}).get("file_timeout") or 0),
    }
    RECEIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = RECEIPT_PATH.with_suffix(RECEIPT_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, RECEIPT_PATH)


# ── Path discovery ────────────────────────────────────────────────────────────

def find_all_pdfs(roots: list[str], target_subdirs: list[str] | None = None) -> list[Path]:
    """Find PDFs in target evidence/review folders under case roots."""
    target_subdirs = target_subdirs or TARGET_SUBDIRS
    pdfs = []
    for root in roots:
        root_path = Path(root)
        if not root_path.is_dir():
            logger.warning(f"Case root not mounted: {root}")
            continue
        logger.info(f"Scanning root: {root}")
        for case_type_dir in sorted(root_path.iterdir()):
            if not case_type_dir.is_dir() or case_type_dir.name.startswith("."):
                continue
            for case_dir in sorted(case_type_dir.iterdir()):
                if not case_dir.is_dir() or case_dir.name.startswith("."):
                    continue
                _collect_pdfs_from(case_dir, pdfs, target_subdirs)
                # One level deeper (e.g. 法扶案件/刑事/case_name)
                for sub_dir in sorted(case_dir.iterdir()):
                    if not sub_dir.is_dir() or sub_dir.name.startswith("."):
                        continue
                    _collect_pdfs_from(sub_dir, pdfs, target_subdirs)
    return pdfs


def find_direct_pdfs(roots: list[str]) -> list[Path]:
    """Find PDFs directly below explicit roots for priority case/folder processing."""
    pdfs: list[Path] = []
    for root in roots:
        root_path = Path(root).expanduser()
        if not root_path.is_dir():
            logger.warning(f"Explicit PDF root not mounted: {root}")
            continue
        logger.info(f"Scanning explicit PDF root: {root_path}")
        for pdf in sorted(root_path.rglob("*.pdf")):
            if not pdf.name.startswith(".") and not _is_temporary_pdf_artifact(pdf):
                pdfs.append(pdf)
    return pdfs


def _is_temporary_pdf_artifact(pdf: Path) -> bool:
    """Exclude incomplete atomic-write artifacts from the source corpus."""
    return bool(
        re.search(
            r"(?:\.tmp|\.partial|\.part|\.download)\.pdf$",
            pdf.name,
            re.IGNORECASE,
        )
    )


def _is_pdf_structure_error(exc: object) -> bool:
    """Identify malformed legacy PDFs that need a separate repair workflow."""
    text = str(exc or "")
    return bool(
        re.search(
            r"(?:page\s+\d+\s+not in document|"
            r"not a dict\s*\(null\)|"
            r"invalid xref|"
            r"broken xref|"
            r"object is not a stream|"
            r"malformed pdf)",
            text,
            re.IGNORECASE,
        )
    )


def _quarantine_pdf_structure(
    completed: dict,
    stage1_errors: dict,
    stats: dict,
    *,
    key: str,
    mtime: str,
    page_count: int,
    error: object,
) -> None:
    """Persist an actionable data-quality item without failing the whole batch."""
    completed[key] = {
        "mtime": mtime,
        "stage1": True,
        "stage1_bookmarks": 0,
        "pages": page_count,
        "quarantined_pdf_structure": True,
        "message": f"PDF 結構異常，已隔離等待單檔修復: {error}"[:500],
        "processed_at": datetime.now().isoformat(),
    }
    stage1_errors.pop(key, None)
    stats["quarantined"] = int(stats.get("quarantined") or 0) + 1


def _collect_pdfs_from(case_dir: Path, out: list[Path], target_subdirs: list[str] | None = None):
    target_subdirs = target_subdirs or TARGET_SUBDIRS
    for sub in target_subdirs:
        target = case_dir / sub
        if not target.is_dir():
            continue
        for pdf in sorted(target.rglob("*.pdf")):
            if not pdf.name.startswith(".") and not _is_temporary_pdf_artifact(pdf):
                out.append(pdf)


def _looks_like_obvious_single_doc(pdf: Path, page_count: int) -> bool:
    """Fast-path clearly single legal documents so large example batches focus on merged records."""
    max_pages = int(os.environ.get("BOOKMARK_SINGLE_DOC_FASTPATH_MAX_PAGES", "80"))
    if page_count > max_pages:
        return False
    text = str(pdf)
    name = pdf.name
    if MERGED_RECORD_HINT_RE.search(text):
        return False
    if page_count <= 3:
        return True
    return bool(SINGLE_DOC_FASTPATH_RE.search(name))


def _looks_like_filename_bookmark_fallback(pdf: Path) -> bool:
    """A no-boundary court/evidence volume whose filename is already the useful bookmark label."""
    return bool(FILENAME_BOOKMARK_FALLBACK_RE.search(pdf.name))


def _single_doc_bookmark_title(pdf: Path) -> str:
    stem = pdf.stem
    stem = re.sub(r"[_\-\s]*OCR$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\s+", " ", stem).strip(" -_")
    # Keep this fast path under the same quality contract as the primary
    # bookmarker; otherwise case/file suffixes leak into generated labels.
    return normalize_bookmark(stem) or "文件"


def _write_single_doc_bookmark(pdf: Path, title: str | None = None) -> int:
    """Write a page-1 bookmark for a confirmed single-document PDF."""
    import fitz

    with pdf_in_place_mutation_lock(
        owner="weekend_bookmark_batch.single_doc",
        pdf_path=pdf,
        blocking=True,
    ):
        temp = pdf.with_name(f".{pdf.name}.bookmark-{os.getpid()}.tmp.pdf")
        doc = fitz.open(str(pdf))
        try:
            existing = doc.get_toc() or []
            if existing:
                return len(existing)
            toc = [[1, title or _single_doc_bookmark_title(pdf), 1]]
            doc.set_toc(toc)
            doc.save(str(temp), garbage=4, deflate=True)
            doc.close()
            os.replace(temp, pdf)
            return 1
        finally:
            try:
                doc.close()
            except Exception:
                pass
            try:
                temp.unlink(missing_ok=True)
            except Exception:
                pass


def _meaningful_boundary_chars(text: str) -> int:
    """Count useful text after removing repetitive court/watermark noise."""
    clean = WATERMARK_ONLY_RE.sub("", text or "")
    clean = re.sub(r"\s+", "", clean)
    clean = re.sub(r"[^\w\u4e00-\u9fff]", "", clean)
    return len(clean)


def _text_profile(pdf: Path, page_count: int, max_samples: int = 5) -> dict:
    """Sample native text to decide if a PDF needs OCR before bookmark detection."""
    import fitz

    if page_count <= 0:
        return {"sampled_pages": 0, "useful_chars": 0, "max_useful_chars": 0}
    indexes = {0, min(1, page_count - 1), page_count // 2, max(0, page_count - 2), page_count - 1}
    indexes = sorted(i for i in indexes if 0 <= i < page_count)[:max_samples]
    useful_counts: list[int] = []
    doc = fitz.open(str(pdf))
    try:
        for idx in indexes:
            useful_counts.append(_meaningful_boundary_chars(doc[idx].get_text() or ""))
    finally:
        doc.close()
    return {
        "sampled_pages": len(useful_counts),
        "useful_chars": sum(useful_counts),
        "max_useful_chars": max(useful_counts or [0]),
    }


def _needs_full_ocr(pdf: Path, page_count: int) -> tuple[bool, str]:
    if "_OCR" in pdf.stem.upper():
        return False, "already_ocr_named"
    try:
        profile = _text_profile(pdf, page_count)
    except Exception as exc:
        return True, f"text_profile_failed:{type(exc).__name__}"
    max_useful = int(profile.get("max_useful_chars") or 0)
    useful_total = int(profile.get("useful_chars") or 0)
    if max_useful < 80 and useful_total < 250:
        return True, f"text_poor_sample:max={max_useful},total={useful_total}"
    return False, f"text_sample_ok:max={max_useful},total={useful_total}"


# ── oMLX management ──────────────────────────────────────────────────────────

def _stop_omlx():
    """Compatibility no-op: model unloading belongs to oMLX's configured TTL."""
    logger.info("oMLX lifecycle unchanged; waiting for configured model TTL")


def _start_omlx(*, deadline: float | None = None):
    """Start oMLX for vision stage (or restore after completion)."""
    startup_deadline = time.monotonic() + OMLX_STARTUP_BUDGET_SECONDS
    if deadline is not None:
        startup_deadline = min(startup_deadline, deadline)
    try:
        reason = _defer_reason(startup_deadline)
        if reason:
            logger.info("oMLX startup deferred before launch: %s", reason)
            return False
        launch_timeout = max(1.0, min(10.0, startup_deadline - time.monotonic()))
        subprocess.run(
            ["launchctl", "start", "com.magi.omlx"],
            capture_output=True,
            timeout=launch_timeout,
        )
        logger.info("▶️ Starting oMLX text inference...")
        while time.monotonic() < startup_deadline:
            reason = _defer_reason(startup_deadline)
            if reason:
                logger.info("oMLX startup deferred safely: %s", reason)
                return False
            time.sleep(min(2.0, max(0.0, startup_deadline - time.monotonic())))
            reason = _defer_reason(startup_deadline)
            if reason:
                logger.info("oMLX startup deferred safely: %s", reason)
                return False
            try:
                import urllib.request
                port = os.environ.get("MAGI_OMLX_PORT", "8080")
                request_timeout = max(
                    0.1,
                    min(3.0, startup_deadline - time.monotonic()),
                )
                resp = urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/v1/models",
                    timeout=request_timeout,
                )
                if resp.status == 200:
                    logger.info("✅ oMLX ready")
                    return True
            except Exception:
                pass
        logger.warning("oMLX did not become ready in %ss", OMLX_STARTUP_BUDGET_SECONDS)
        return False
    except Exception as e:
        logger.warning(f"Could not start oMLX: {e}")
        return False


# ── Stage 1: Regex bookmarks ─────────────────────────────────────────────────

def stage1_regex(
    pdfs: list[Path],
    state: dict,
    scan_fn,
    *,
    deadline: float | None = None,
) -> dict:
    """Fast regex-based bookmark pass. Returns stats dict."""
    import fitz

    stats = {
        "processed": 0,
        "bookmarks": 0,
        "skipped": 0,
        "no_boundary": 0,
        "single_doc": 0,
        "needs_ocr": 0,
        "deferred_large_ocr": 0,
        "file_timeout": 0,
        "quarantined": 0,
        "errors": 0,
        "deferred": False,
        "defer_reason": "",
        "visited": 0,
        "scan_complete": False,
    }
    completed = state.setdefault("completed", {})
    stage1_errors = state.setdefault("stage1_errors", {})

    # Optional soft budget (seconds) — nightly caller sets this to ~1800 to bound wall-clock.
    _budget_raw = os.environ.get("BOOKMARK_REGEX_BUDGET_SEC", "").strip()
    try:
        budget_sec = int(_budget_raw) if _budget_raw else 0
    except ValueError:
        budget_sec = 0
    single_doc_fastpath = os.environ.get("BOOKMARK_SINGLE_DOC_FASTPATH", "").strip() == "1"
    skip_large_non_ocr = os.environ.get("BOOKMARK_SKIP_LARGE_NON_OCR", "").strip() == "1"
    if os.environ.get("BOOKMARK_STAGE1_ALLOW_VISION", "0").strip() not in ("1", "true", "yes"):
        # Stage 1 must stay deterministic and bounded. Vision fallback belongs to
        # stage2_vision; leaving it on here can make every regex miss call the
        # inference service and stall large court-record PDFs.
        os.environ["MAGI_BOOKMARKER_VISION_FALLBACK"] = "0"
    retry_no_boundary = os.environ.get("BOOKMARK_RETRY_NO_BOUNDARY", "").strip() == "1"
    retry_deferred = os.environ.get("BOOKMARK_RETRY_DEFERRED", "").strip() == "1"
    retry_needs_ocr = os.environ.get("BOOKMARK_RETRY_NEEDS_OCR", "").strip() == "1"
    try:
        large_non_ocr_pages = int(os.environ.get("BOOKMARK_LARGE_NON_OCR_MIN_PAGES", "100"))
    except ValueError:
        large_non_ocr_pages = 100
    try:
        defer_large_ocr_pages = int(os.environ.get("BOOKMARK_DEFER_LARGE_OCR_PAGES", "0"))
    except ValueError:
        defer_large_ocr_pages = 0
    try:
        file_timeout_sec = min(
            MAX_FILE_SCAN_TIMEOUT_SECONDS,
            max(0, int(os.environ.get("BOOKMARK_FILE_TIMEOUT_SEC", str(MAX_FILE_SCAN_TIMEOUT_SECONDS)))),
        )
    except ValueError:
        file_timeout_sec = MAX_FILE_SCAN_TIMEOUT_SECONDS
    stage_start = time.time()

    for i, pdf in enumerate(pdfs, 1):
        if stats["deferred"]:
            break
        stats["visited"] += 1
        reason = _defer_reason(deadline)
        if reason:
            stats["deferred"] = True
            stats["defer_reason"] = reason
            logger.info("Stage 1 deferred safely at %d/%d: %s", i, len(pdfs), reason)
            _save_state(state)
            break
        if budget_sec and (time.time() - stage_start) > budget_sec:
            logger.info(
                f"⏰ Stage 1 regex budget exhausted ({budget_sec}s) at {i}/{len(pdfs)}; remaining PDFs will be picked up next run"
            )
            stats["deferred"] = True
            stats["defer_reason"] = "regex_budget_exhausted"
            _save_state(state)
            break
        key = str(pdf)
        try:
            mtime, current_identity = _pdf_state_snapshot(pdf)
        except Exception as exc:
            if _source_unavailable_is_transient(pdf, exc):
                stats["deferred"] = True
                stats["defer_reason"] = "storage_unavailable"
                logger.warning(
                    "Stage 1 deferred because case storage became unavailable"
                )
                _save_state(state)
                break
            stage1_errors[key] = {
                "stage1": False,
                "message": f"PDF source metadata unavailable: {type(exc).__name__}: {exc}"[:500],
                "processed_at": datetime.now().isoformat(),
            }
            stats["errors"] += 1
            _save_loop_progress(state, key, i, len(pdfs), stats)
            continue

        # Skip only when stage1 is bound to the exact same PDF identity.  Legacy
        # mtime-only state is intentionally reprocessed once so a same-mtime NAS
        # replacement cannot inherit another file's stage result.
        prev = completed.get(key, {})
        previous_identity = _state_identity(prev.get("identity"))
        force_retry = (
            (retry_no_boundary and prev.get("no_boundary"))
            or (retry_deferred and prev.get("deferred_large_ocr"))
            or (retry_needs_ocr and prev.get("needs_ocr"))
        )
        if (
            prev.get("mtime") == mtime
            and prev.get("stage1")
            and previous_identity == current_identity
            and not force_retry
        ):
            stats["skipped"] += 1
            stage1_errors.pop(key, None)
            _save_loop_progress(state, key, i, len(pdfs), stats)
            continue
        if prev.get("stage1") and previous_identity != current_identity:
            prev["stage1"] = False
            prev.pop("identity", None)
            prev["identity_untrusted"] = True
            state.setdefault("vision_done", {}).pop(key, None)
            state.setdefault("vision_progress", {}).pop(key, None)

        # Skip if already has enough bookmarks
        try:
            doc = fitz.open(str(pdf))
            existing = doc.get_toc() or []
            page_count = doc.page_count
            doc.close()
            if len(existing) >= max(3, page_count // 15):
                completed[key] = {
                    "mtime": mtime, "stage1": True,
                    "stage1_bookmarks": len(existing),
                    "pages": page_count,
                    "processed_at": datetime.now().isoformat(),
                }
                stats["skipped"] += 1
                stage1_errors.pop(key, None)
                _save_loop_progress(state, key, i, len(pdfs), stats)
                continue
        except Exception as exc:
            if _is_transient_storage_error(exc):
                stats["deferred"] = True
                stats["defer_reason"] = "storage_unavailable"
                logger.warning(
                    "Stage 1 deferred because case storage became unavailable"
                )
                _save_state(state)
                break
            if _is_pdf_structure_error(exc):
                _quarantine_pdf_structure(
                    completed,
                    stage1_errors,
                    stats,
                    key=key,
                    mtime=mtime,
                    page_count=0,
                    error=exc,
                )
                _save_loop_progress(state, key, i, len(pdfs), stats)
                continue
            stage1_errors[key] = {
                "mtime": mtime,
                "stage1": False,
                "message": "PDF 開啟或讀取既有書籤失敗",
                "processed_at": datetime.now().isoformat(),
            }
            stats["errors"] += 1
            _save_loop_progress(state, key, i, len(pdfs), stats)
            continue

        if skip_large_non_ocr and page_count >= large_non_ocr_pages:
            needs_ocr, ocr_reason = _needs_full_ocr(pdf, page_count)
            if not needs_ocr:
                logger.info("  large non-OCR text sample OK: %s (%s)", pdf.name, ocr_reason)
            elif not retry_needs_ocr:
                completed[key] = {
                    "mtime": mtime,
                    "stage1": True,
                    "stage1_bookmarks": 0,
                    "pages": page_count,
                    "needs_ocr": True,
                    "ocr_reason": ocr_reason,
                    "message": "大型 PDF 文字層不足，需 OCR 後再編標籤",
                    "processed_at": datetime.now().isoformat(),
                }
                stats["needs_ocr"] += 1
                stage1_errors.pop(key, None)
                _save_loop_progress(state, key, i, len(pdfs), stats)
                continue

        if (
            defer_large_ocr_pages
            and page_count >= defer_large_ocr_pages
            and "_OCR" in pdf.stem.upper()
            and not retry_deferred
        ):
            completed[key] = {
                "mtime": mtime,
                "stage1": True,
                "stage1_bookmarks": 0,
                "pages": page_count,
                "deferred_large_ocr": True,
                "message": f"大型 OCR PDF（{page_count} 頁）先排入分割/離峰重跑，避免專案批次被單檔阻塞",
                "processed_at": datetime.now().isoformat(),
            }
            stats["deferred_large_ocr"] += 1
            stage1_errors.pop(key, None)
            _save_loop_progress(state, key, i, len(pdfs), stats)
            continue

        if single_doc_fastpath and _looks_like_obvious_single_doc(pdf, page_count):
            try:
                bm_count = _write_single_doc_bookmark(pdf)
            except Exception as exc:
                if _is_transient_storage_error(exc):
                    stats["deferred"] = True
                    stats["defer_reason"] = "storage_unavailable"
                    logger.warning(
                        "Stage 1 deferred because case storage became unavailable"
                    )
                    _save_state(state)
                    break
                if _is_pdf_structure_error(exc):
                    _quarantine_pdf_structure(
                        completed,
                        stage1_errors,
                        stats,
                        key=key,
                        mtime=mtime,
                        page_count=page_count,
                        error=exc,
                    )
                    _save_loop_progress(state, key, i, len(pdfs), stats)
                    continue
                stage1_errors[key] = {
                    "mtime": mtime,
                    "stage1": False,
                    "pages": page_count,
                    "message": f"單一文件書籤寫入失敗: {exc}"[:500],
                    "processed_at": datetime.now().isoformat(),
                }
                stats["errors"] += 1
                _save_loop_progress(state, key, i, len(pdfs), stats)
                continue
            try:
                stored_mtime = str(pdf.stat().st_mtime)
            except Exception:
                stored_mtime = mtime
            completed[key] = {
                "mtime": stored_mtime,
                "stage1": True,
                "stage1_bookmarks": bm_count,
                "pages": page_count,
                "classification": "single_doc_bookmark",
                "message": "明確單一文件，已補 page-1 檔名書籤",
                "processed_at": datetime.now().isoformat(),
            }
            stats["single_doc"] += 1
            stage1_errors.pop(key, None)
            _save_loop_progress(state, key, i, len(pdfs), stats)
            continue

        if single_doc_fastpath and prev.get("no_boundary") and _looks_like_filename_bookmark_fallback(pdf):
            try:
                bm_count = _write_single_doc_bookmark(pdf)
            except Exception as exc:
                if _is_transient_storage_error(exc):
                    stats["deferred"] = True
                    stats["defer_reason"] = "storage_unavailable"
                    logger.warning(
                        "Stage 1 deferred because case storage became unavailable"
                    )
                    _save_state(state)
                    break
                if _is_pdf_structure_error(exc):
                    _quarantine_pdf_structure(
                        completed,
                        stage1_errors,
                        stats,
                        key=key,
                        mtime=mtime,
                        page_count=page_count,
                        error=exc,
                    )
                    _save_loop_progress(state, key, i, len(pdfs), stats)
                    continue
                stage1_errors[key] = {
                    "mtime": mtime,
                    "stage1": False,
                    "pages": page_count,
                    "message": f"檔名回退書籤寫入失敗: {exc}"[:500],
                    "processed_at": datetime.now().isoformat(),
                }
                stats["errors"] += 1
                _save_loop_progress(state, key, i, len(pdfs), stats)
                continue
            try:
                stored_mtime = str(pdf.stat().st_mtime)
            except Exception:
                stored_mtime = mtime
            completed[key] = {
                "mtime": stored_mtime,
                "stage1": True,
                "stage1_bookmarks": bm_count,
                "pages": page_count,
                "classification": "single_doc_bookmark",
                "classification_reason": "filename_volume_or_evidence_chunk_after_no_boundary",
                "message": "舊無邊界卷宗/證據分段檔，已補 page-1 檔名書籤",
                "processed_at": datetime.now().isoformat(),
            }
            stats["single_doc"] += 1
            stage1_errors.pop(key, None)
            _save_loop_progress(state, key, i, len(pdfs), stats)
            continue

        if i % 50 == 0:
            logger.info(f"  Stage 1 progress: {i}/{len(pdfs)}")

        try:
            # The scanner owns its timeout in an isolated subprocess. Never
            # inject SIGALRM into PyMuPDF/RapidOCR native code in this owner.
            result = scan_fn(str(pdf), output_path=None, dry_run=False)
            if result.get("success"):
                bm_count = result.get("bookmarks", 0)
                try:
                    stored_mtime = str(pdf.stat().st_mtime)
                except Exception:
                    stored_mtime = mtime
                stats["processed"] += 1
                stats["bookmarks"] += bm_count
                completed[key] = {
                    "mtime": stored_mtime, "stage1": True,
                    "stage1_bookmarks": bm_count,
                    "pages": page_count,
                    "processed_at": datetime.now().isoformat(),
                }
                stage1_errors.pop(key, None)
            elif _is_stage1_no_hit_result(result):
                message = str(result.get("message") or "")
                classification = str(result.get("classification") or "")
                if (
                    classification in {"legitimate_single_doc", "filename_bookmark_fallback"}
                    or _looks_like_obvious_single_doc(pdf, page_count)
                    or _looks_like_filename_bookmark_fallback(pdf)
                ):
                    bm_count = _write_single_doc_bookmark(pdf)
                    try:
                        stored_mtime = str(pdf.stat().st_mtime)
                    except Exception:
                        stored_mtime = mtime
                    completed[key] = {
                        "mtime": stored_mtime,
                        "stage1": True,
                        "stage1_bookmarks": bm_count,
                        "pages": page_count,
                        "classification": "single_doc_bookmark",
                        "classification_reason": str(result.get("classification_reason") or ""),
                        "message": "無多文件邊界但判定可用檔名建立索引，已補 page-1 檔名書籤",
                        "processed_at": datetime.now().isoformat(),
                    }
                    stats["single_doc"] += 1
                    stage1_errors.pop(key, None)
                    _save_loop_progress(state, key, i, len(pdfs), stats)
                    continue
                completed[key] = {
                    "mtime": mtime,
                    "stage1": True,
                    "stage1_bookmarks": 0,
                    "pages": page_count,
                    "no_boundary": True,
                    "message": message,
                    "processed_at": datetime.now().isoformat(),
                }
                stats["no_boundary"] += 1
                stage1_errors.pop(key, None)
            else:
                result_message = (
                    (result or {}).get("message")
                    if isinstance(result, dict)
                    else result
                )
                if _failed_result_is_transient_network_source(pdf, result):
                    stats["deferred"] = True
                    stats["defer_reason"] = "storage_unavailable"
                    stage1_errors.pop(key, None)
                    logger.warning(
                        "Stage 1 deferred because case storage became unavailable"
                    )
                    _save_state(state)
                    break
                if _is_pdf_structure_error(result_message):
                    _quarantine_pdf_structure(
                        completed,
                        stage1_errors,
                        stats,
                        key=key,
                        mtime=mtime,
                        page_count=page_count,
                        error=result_message,
                    )
                else:
                    stage1_errors[key] = {
                        "mtime": mtime,
                        "stage1": False,
                        "pages": page_count,
                        "message": str(result_message)[:500],
                        "processed_at": datetime.now().isoformat(),
                    }
                    stats["errors"] += 1
        except FileScanTimeout as e:
            logger.warning("  Stage 1 timeout %s: %s", pdf.name, e)
            completed[key] = {
                "mtime": mtime,
                "stage1": True,
                "stage1_bookmarks": 0,
                "pages": page_count,
                "deferred_large_ocr": "_OCR" in pdf.stem.upper(),
                "file_timeout": True,
                "message": str(e),
                "processed_at": datetime.now().isoformat(),
            }
            stats["file_timeout"] += 1
        except Exception as e:
            logger.debug(f"  Stage 1 error {pdf.name}: {e}")
            # scan_and_bookmark performs an atomic in-place replacement. If
            # SMB/File Provider disappears between opening the source and
            # creating its hidden temporary file, Python reports ENOENT for
            # the temp path even though the network source disappeared.
            # Preserve the checkpoint and retry later instead of reporting a
            # red batch failure.
            if _source_unavailable_is_transient(pdf, e):
                stats["deferred"] = True
                stats["defer_reason"] = "storage_unavailable"
                logger.warning(
                    "Stage 1 deferred because case storage became unavailable"
                )
                _save_state(state)
                break
            if _is_pdf_structure_error(e):
                _quarantine_pdf_structure(
                    completed,
                    stage1_errors,
                    stats,
                    key=key,
                    mtime=mtime,
                    page_count=page_count,
                    error=e,
                )
            else:
                stage1_errors[key] = {
                    "mtime": mtime,
                    "stage1": False,
                    "pages": page_count,
                    "message": str(e)[:500],
                    "processed_at": datetime.now().isoformat(),
                }
                stats["errors"] += 1

        _save_loop_progress(state, key, i, len(pdfs), stats)
    else:
        stats["scan_complete"] = True

    if not stats["scan_complete"] and not stats["deferred"]:
        stats["deferred"] = True
        stats["defer_reason"] = "incomplete_iteration"

    _save_state(state)
    return stats


def _is_stage1_no_hit_result(result: Any) -> bool:
    """Classify expected regex misses (no boundary / empty toc) as non-errors."""
    if not isinstance(result, dict):
        return False
    if result.get("success") is True:
        return False

    msg = str(result.get("message") or "")
    if re.search(r"(未偵測到文件邊界|無法產生書籤|no\s*boundary|empty\s*toc|toc\s*empty)", msg, re.IGNORECASE):
        return True

    bookmarks = result.get("bookmarks")
    toc = result.get("toc")
    zero_bookmarks = bookmarks == 0
    toc_empty = toc is None or (isinstance(toc, list) and len(toc) == 0)
    return zero_bookmarks and toc_empty


def build_backfill_plan(pdfs: list[Path], state: dict, sample_limit: int = 20) -> dict:
    """Build a non-destructive backfill plan from current state + file mtimes."""
    completed = state.get("completed", {}) or {}
    vision_done = state.get("vision_done", {}) or {}

    stage1_pending = []
    stage1_done = 0
    no_boundary_backlog = []
    vision_pending = []
    stage1_skipped_with_bookmarks = 0

    for pdf in pdfs:
        key = str(pdf)
        info = completed.get(key, {}) or {}
        try:
            mtime = str(pdf.stat().st_mtime)
        except Exception:
            continue

        stage1_done_same_version = bool(info.get("stage1") and info.get("mtime") == mtime)
        if stage1_done_same_version:
            stage1_done += 1
            if int(info.get("stage1_bookmarks", 0) or 0) >= 3:
                stage1_skipped_with_bookmarks += 1
        else:
            stage1_pending.append(key)

        if info.get("no_boundary") and info.get("mtime") == mtime:
            no_boundary_backlog.append(key)

        if (
            stage1_done_same_version
            and not info.get("quarantined_pdf_structure")
            and int(info.get("pages", 0) or 0) >= 5
        ):
            vision_info = vision_done.get(key, {}) or {}
            if vision_info.get("mtime") != mtime:
                vision_pending.append(key)

    plan = {
        "generated_at": datetime.now().isoformat(),
        "total_pdfs": len(pdfs),
        "stage1_pending_count": len(stage1_pending),
        "stage1_done_count": stage1_done,
        "stage1_existing_bookmark_skip_estimate": stage1_skipped_with_bookmarks,
        "no_boundary_backlog_count": len(no_boundary_backlog),
        "vision_pending_count": len(vision_pending),
        "samples": {
            "stage1_pending": stage1_pending[:sample_limit],
            "no_boundary_backlog": no_boundary_backlog[:sample_limit],
            "vision_pending": vision_pending[:sample_limit],
        },
    }
    return plan


def _followup_action_for(
    pdf: Path,
    info: dict,
    mtime: str,
    vision_progress_info: dict | None = None,
) -> dict:
    """Return the next concrete action MAGI should take for a bookmarked PDF state."""
    key = str(pdf)
    if not pdf.exists():
        return {"path": key, "action": "missing", "reason": "file_not_found"}

    pages = int(info.get("pages") or 0)
    bookmarks = int(info.get("stage1_bookmarks") or 0)
    if info.get("quarantined_pdf_structure"):
        return {
            "path": key,
            "action": "repair_pdf_structure",
            "reason": str(info.get("message") or "malformed_pdf"),
            "pages": pages,
        }
    if info.get("vision_source_changed") or not info.get("stage1"):
        return {
            "path": key,
            "action": "retry_stage1",
            "reason": str(info.get("message") or "vision_source_changed"),
            "pages": pages,
        }
    if info.get("vision_page_limit_deferred"):
        return {
            "path": key,
            "action": "split_or_offpeak_vision",
            "reason": str(info.get("message") or "vision_page_limit_deferred"),
            "pages": pages,
        }
    progress = vision_progress_info if isinstance(vision_progress_info, dict) else {}
    progress_matches = bool(progress and str(progress.get("mtime") or "") == mtime)
    failed_pages = [
        int(page)
        for page in (progress.get("failed_pages") or [])
        if str(page).isdigit() and int(page) > 0
    ]
    if progress_matches and (
        failed_pages
        or progress.get("commit_failed")
        or str(progress.get("defer_reason") or "").strip()
    ):
        if progress.get("commit_failed"):
            reason = "vision_bookmark_commit_failed"
        elif failed_pages:
            reason = "vision_failed_pages:" + ",".join(str(page) for page in failed_pages[:20])
        else:
            reason = str(progress.get("defer_reason") or "vision_deferred")
        return {
            "path": key,
            "action": "retry_vision",
            "reason": reason,
            "pages": pages,
            "failed_pages": failed_pages,
        }
    if bookmarks > 0:
        return {"path": key, "action": "complete", "reason": f"bookmarks={bookmarks}", "pages": pages}

    if info.get("needs_ocr"):
        return {
            "path": key,
            "action": "ocr_then_bookmark",
            "reason": str(info.get("ocr_reason") or info.get("message") or "needs_ocr"),
            "pages": pages,
        }

    if info.get("deferred_large_ocr"):
        return {
            "path": key,
            "action": "split_large_ocr" if info.get("file_timeout") else "offpeak_retry_stage1",
            "reason": "file_timeout" if info.get("file_timeout") else "large_ocr_pdf_deferred",
            "pages": pages,
        }

    if info.get("file_timeout"):
        return {
            "path": key,
            "action": "split_or_manual_boundary",
            "reason": "file_timeout",
            "pages": pages,
        }

    if info.get("no_boundary"):
        try:
            page_count = pages
            if page_count <= 0:
                import fitz
                doc = fitz.open(str(pdf))
                page_count = doc.page_count
                doc.close()
        except Exception:
            page_count = pages
        if _looks_like_filename_bookmark_fallback(pdf):
            return {
                "path": key,
                "action": "single_doc_page1_bookmark",
                "reason": "filename_volume_or_evidence_chunk",
                "pages": page_count,
            }
        if page_count >= 100:
            needs_ocr, reason = _needs_full_ocr(pdf, page_count)
            if needs_ocr:
                return {
                    "path": key,
                    "action": "ocr_then_bookmark",
                    "reason": reason,
                    "pages": page_count,
                }
            return {
                "path": key,
                "action": "offpeak_retry_stage1",
                "reason": "large_pdf_no_boundary_with_text",
                "pages": page_count,
            }
        if _looks_like_obvious_single_doc(pdf, page_count) or info.get("classification") in {"legitimate_single_doc", "filename_bookmark_fallback"}:
            return {
                "path": key,
                "action": "single_doc_page1_bookmark",
                "reason": str(info.get("classification_reason") or "single_doc"),
                "pages": page_count,
            }
        return {
            "path": key,
            "action": "vision_boundary_review",
            "reason": "no_boundary_without_single_doc_signal",
            "pages": page_count,
        }

    if not info.get("stage1") or info.get("mtime") != mtime:
        return {"path": key, "action": "retry_stage1", "reason": "stage1_missing_or_stale", "pages": pages}

    return {"path": key, "action": "manual_review", "reason": "zero_bookmarks_without_known_class", "pages": pages}


def build_followup_plan(pdfs: list[Path], state: dict, sample_limit: int = 50) -> dict:
    """Build an actionable plan for OCR/no-boundary/deferred PDFs.

    This is the key guard against silent drift: every zero-bookmark PDF must be
    either resolved as a single document, queued for OCR, retried off-peak, or
    explicitly assigned to vision/manual review.
    """
    completed = state.get("completed", {}) or {}
    vision_progress = state.get("vision_progress", {}) or {}
    actions: list[dict] = []
    counts: dict[str, int] = {}
    for pdf in pdfs:
        key = str(pdf)
        info = completed.get(key, {}) or {}
        try:
            mtime = str(pdf.stat().st_mtime)
        except Exception:
            mtime = ""
        action = _followup_action_for(
            pdf,
            info,
            mtime,
            vision_progress.get(key, {}) or {},
        )
        counts[action["action"]] = counts.get(action["action"], 0) + 1
        if action["action"] not in {"complete"}:
            actions.append(action)

    priority_order = {
        "retry_vision": 0,
        "ocr_then_bookmark": 0,
        "offpeak_retry_stage1": 1,
        "split_large_ocr": 1,
        "split_or_manual_boundary": 2,
        "split_or_offpeak_vision": 2,
        "single_doc_page1_bookmark": 3,
        "vision_boundary_review": 4,
        "retry_stage1": 5,
        "manual_review": 6,
        "missing": 7,
    }
    actions.sort(key=lambda item: (priority_order.get(item["action"], 99), -int(item.get("pages") or 0), item["path"]))
    return {
        "generated_at": datetime.now().isoformat(),
        "total_pdfs": len(pdfs),
        "counts": counts,
        "pending_count": len(actions),
        "actions": actions[:sample_limit],
        "truncated": len(actions) > sample_limit,
    }


def write_followup_plan(plan: dict, path: Path = FOLLOWUP_PLAN_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")


def enqueue_ocr_followups(plan: dict) -> int:
    """Insert OCR follow-up items into the NAS OCR worker queue."""
    import sqlite3

    db_path = resolve_nas_ocr_queue_db_path(release_root=MAGI_ROOT)
    rows = [
        item["path"]
        for item in plan.get("actions", [])
        if item.get("action") == "ocr_then_bookmark"
    ]
    if not rows:
        return 0
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS ocr_queue (
                file_path TEXT PRIMARY KEY,
                status TEXT DEFAULT 'pending',
                last_attempt TIMESTAMP,
                attempt_count INTEGER DEFAULT 0,
                error_msg TEXT
            )
            """
        )
        added = 0
        for file_path in rows:
            try:
                c.execute("INSERT INTO ocr_queue (file_path, status, error_msg) VALUES (?, 'pending', ?)", (
                    file_path,
                    "queued_by_bookmark_followup",
                ))
                added += 1
            except sqlite3.IntegrityError:
                c.execute(
                    "UPDATE ocr_queue SET status='pending', error_msg=? WHERE file_path=? AND status IN ('failed','missing')",
                    ("requeued_by_bookmark_followup", file_path),
                )
        conn.commit()
        return added
    finally:
        conn.close()


# ── Stage 2: Vision refinement ───────────────────────────────────────────────

_VISION_SAFETY_FLAGS = ("deferred_large_ocr", "needs_ocr", "file_timeout")


def _pdf_identity(pdf: Path) -> tuple[int, int, int, int]:
    return _pdf_state_snapshot(pdf)[1]


def _write_vision_bookmarks(
    pdf: Path,
    bookmarks: list[list[Any]],
    *,
    expected_identity: tuple[int, int, int, int],
) -> tuple[int, str, tuple[int, int, int, int]]:
    """Revalidate and commit completed page results under one mutation lock."""
    import fitz

    added = 0
    with pdf_in_place_mutation_lock(
        owner="weekend_bookmark_batch.vision",
        pdf_path=pdf,
        blocking=True,
    ):
        current_identity = _pdf_identity(pdf)
        if current_identity != expected_identity:
            raise VisionSourceChanged(
                f"PDF identity changed before vision commit: expected={expected_identity}, "
                f"current={current_identity}"
            )
        if not bookmarks:
            return 0, str(pdf.stat().st_mtime), current_identity
        write_doc = fitz.open(str(pdf))
        try:
            current_toc = write_doc.get_toc() or []
            current_pages = {pg for _, _, pg in current_toc}
            additions = [item for item in bookmarks if item[2] not in current_pages]
            if additions:
                merged_toc = list(current_toc) + additions
                merged_toc.sort(key=lambda item: (item[2], item[0]))
                write_doc.set_toc(merged_toc)
                write_doc.saveIncr()
                added = len(additions)
        finally:
            write_doc.close()
        stored_mtime = str(pdf.stat().st_mtime)
        stored_identity = _pdf_identity(pdf)
    return added, stored_mtime, stored_identity


def _invalidate_changed_vision_source(
    pdf: Path,
    key: str,
    completed: dict,
    vision_progress: dict,
    vision_done: dict,
    reason: str,
) -> None:
    """Discard stale vision output and force deterministic stage1 re-evaluation."""
    info = completed.setdefault(key, {})
    try:
        info["mtime"] = str(pdf.stat().st_mtime)
    except Exception:
        pass
    info["stage1"] = False
    info.pop("identity", None)
    info["vision_source_changed"] = True
    info["message"] = reason[:500]
    info["processed_at"] = datetime.now().isoformat()
    vision_progress.pop(key, None)
    vision_done.pop(key, None)


def stage2_vision(
    pdfs: list[Path],
    state: dict,
    gw,
    *,
    deadline: float | None = None,
    max_pages: int | None = None,
) -> dict:
    """Vision refinement with resumable, fail-closed per-page progress."""
    import fitz

    stats = {
        "pages_checked": 0,
        "bookmarks_added": 0,
        "files_refined": 0,
        "errors": 0,
        "failed_pages": 0,
        "skipped_safety": 0,
        "skipped_page_limit": 0,
        "deferred": False,
        "defer_reason": "",
        "partial": False,
        "error_samples": [],
    }
    completed = state.setdefault("completed", {})
    vision_done = state.setdefault("vision_done", {})
    vision_progress = state.setdefault("vision_progress", {})
    page_limit = VISION_MAX_PAGES if max_pages is None else max(0, int(max_pages))
    vision_deadline = time.monotonic() + max(1, int(VISION_BUDGET_SECONDS))
    if deadline is not None:
        vision_deadline = min(vision_deadline, deadline)

    candidates = []
    for pdf in pdfs:
        key = str(pdf)
        info = completed.get(key, {}) or {}
        if not info.get("stage1"):
            continue
        mtime = str(info.get("mtime") or "")
        pages = int(info.get("pages") or 0)
        done_info = vision_done.get(key, {}) or {}
        if done_info.get("mtime") == mtime:
            try:
                actual_identity = _pdf_identity(pdf)
                completed_identity = _state_identity(info.get("identity"))
                done_identity = _state_identity(done_info.get("identity"))
            except Exception:
                actual_identity = ()
                completed_identity = ()
                done_identity = ()
            if (
                actual_identity
                and completed_identity == actual_identity
                and done_identity == actual_identity
            ):
                continue
            stats["errors"] += 1
            stats["partial"] = True
            reason = "PDF identity changed or missing before vision_done skip"
            _invalidate_changed_vision_source(
                pdf,
                key,
                completed,
                vision_progress,
                vision_done,
                reason,
            )
            if len(stats["error_samples"]) < 20:
                stats["error_samples"].append(
                    {"path": key, "error": f"VisionSourceChanged: {reason}"}
                )
            continue
        if any(info.get(flag) for flag in _VISION_SAFETY_FLAGS):
            stats["skipped_safety"] += 1
            continue
        if page_limit and pages > page_limit:
            stats["skipped_page_limit"] += 1
            stats["deferred"] = True
            if not stats["defer_reason"]:
                stats["defer_reason"] = "vision_page_limit"
            info["vision_page_limit_deferred"] = True
            info["vision_page_limit"] = page_limit
            info["message"] = (
                f"PDF 共 {pages} 頁，超過 vision 上限 {page_limit}；"
                "需先分割或另排離峰 vision。"
            )
            continue
        if info.get("vision_page_limit_deferred"):
            info.pop("vision_page_limit_deferred", None)
            info.pop("vision_page_limit", None)
        if pages < 5:
            continue
        try:
            actual_identity = _pdf_identity(pdf)
        except Exception:
            actual_identity = ()
        completed_identity = _state_identity(info.get("identity"))
        if not actual_identity or completed_identity != actual_identity:
            stats["errors"] += 1
            stats["partial"] = True
            reason = "PDF stage1 identity missing or changed before vision"
            _invalidate_changed_vision_source(
                pdf,
                key,
                completed,
                vision_progress,
                vision_done,
                reason,
            )
            if len(stats["error_samples"]) < 20:
                stats["error_samples"].append(
                    {"path": key, "error": f"VisionSourceChanged: {reason}"}
                )
            continue
        ratio = int(info.get("stage1_bookmarks") or 0) / max(pages, 1)
        candidates.append((ratio, str(pdf), pdf, pages, mtime, actual_identity))
    candidates.sort()

    if not candidates:
        logger.info("Stage 2: No eligible files need vision refinement")
        _save_state(state)
        return stats

    logger.info("Stage 2: %d eligible files to refine with vision", len(candidates))
    prompt_template = (
        "這是台灣法院卷宗的第 {page} 頁。\n"
        "請判斷此頁的文件類型。\n"
        "回傳 JSON：{{\"type\": \"文件類型\", \"date\": \"民國年月日\", \"title\": \"書籤標題(20字內)\"}}\n"
        "文件類型包括：起訴書、判決、裁定、聲請狀、答辯狀、筆錄、鑑定報告、\n"
        "搜索票、通訊監察、診斷證明、財產資料、戶籍謄本、送達證書、債權人清冊、\n"
        "陳報狀、委任狀、報到單、照片截圖、票據契約、收發文函等。\n"
        "書籤標題格式：「日期 文件類型 當事人」，如「114.09.18 調查筆錄 陳OO」\n"
        "若此頁不值得加書籤（空白頁、浮水印頁、前一文件的續頁），回傳 {{\"type\": null}}\n"
        "只輸出 JSON。"
    )

    for _, _, pdf, page_count, source_mtime, expected_identity in candidates:
        key = str(pdf)
        reason = _defer_reason(vision_deadline)
        if reason:
            stats["deferred"] = True
            stats["defer_reason"] = reason
            _save_state(state)
            break

        previous = vision_progress.get(key, {}) or {}
        if str(previous.get("mtime") or "") != source_mtime:
            previous = {}
        next_page = max(1, int(previous.get("next_page") or 1))
        failed_pages = {
            int(page)
            for page in (previous.get("failed_pages") or [])
            if str(page).isdigit() and 1 <= int(page) <= page_count
        }
        retry_count = int(previous.get("retry_count") or 0)
        new_bookmarks: list[list[Any]] = []
        doc = None
        file_deferred_reason = ""
        source_identity: tuple[int, int, int, int] | None = None
        try:
            current_mtime, source_identity = _pdf_state_snapshot(pdf)
            if source_identity != expected_identity or current_mtime != source_mtime:
                raise VisionSourceChanged(
                    "PDF identity changed before vision read: "
                    f"expected={expected_identity}, current={source_identity}"
                )
            doc = fitz.open(str(pdf))
            actual_pages = int(doc.page_count)
            if page_limit and actual_pages > page_limit:
                completed[key]["pages"] = actual_pages
                completed[key]["vision_page_limit_deferred"] = True
                completed[key]["vision_page_limit"] = page_limit
                completed[key]["message"] = (
                    f"PDF 實際共 {actual_pages} 頁，超過 vision 上限 {page_limit}；"
                    "需先分割或另排離峰 vision。"
                )
                stats["skipped_page_limit"] += 1
                stats["deferred"] = True
                if not stats["defer_reason"]:
                    stats["defer_reason"] = "vision_page_limit"
                vision_progress.pop(key, None)
                vision_done.pop(key, None)
                _save_state(state)
                continue
            if actual_pages != page_count:
                raise VisionSourceChanged(
                    f"PDF page count changed before vision read: state={page_count}, "
                    f"actual={actual_pages}"
                )
            bookmarked_pages = {pg for _, _, pg in (doc.get_toc() or [])}
            pending_pages = sorted(failed_pages) + list(range(next_page, page_count + 1))
            pending_pages = list(dict.fromkeys(pending_pages))

            for pg_num in pending_pages:
                reason = _defer_reason(vision_deadline)
                if reason:
                    file_deferred_reason = reason
                    break
                if pg_num in bookmarked_pages:
                    failed_pages.discard(pg_num)
                    next_page = max(next_page, pg_num + 1)
                    continue

                img_path = ""
                try:
                    page = doc[pg_num - 1]
                    page_text = page.get_text().strip()
                    if len(page_text) < 20:
                        failed_pages.discard(pg_num)
                        next_page = max(next_page, pg_num + 1)
                        continue
                    pix = page.get_pixmap(dpi=150)
                    img_fd, img_path = tempfile.mkstemp(suffix=".png")
                    os.close(img_fd)
                    pix.save(img_path)
                    prompt = prompt_template.format(page=pg_num)
                    result = gw.vision(
                        img_path,
                        prompt,
                        timeout=VISION_PER_PAGE_TIMEOUT,
                        task_type="vision",
                    )
                    stats["pages_checked"] += 1
                    if not isinstance(result, dict) or not result.get("success"):
                        raise RuntimeError(str((result or {}).get("error") or "vision_call_failed"))
                    raw = str(result.get("text") or result.get("content") or "")
                    parsed = _parse_vision_response(raw)
                    if parsed is None:
                        raise ValueError("vision_response_invalid_json")
                    if parsed.get("type") and parsed.get("title"):
                        title = str(parsed["title"])[:30].strip()
                        if len(title) >= 3:
                            new_bookmarks.append([1, title, pg_num])
                    failed_pages.discard(pg_num)
                except Exception as exc:
                    failed_pages.add(pg_num)
                    stats["errors"] += 1
                    if len(stats["error_samples"]) < 20:
                        stats["error_samples"].append(
                            {"path": key, "page": pg_num, "error": f"{type(exc).__name__}: {exc}"[:300]}
                        )
                finally:
                    next_page = max(next_page, pg_num + 1)
                    if img_path:
                        try:
                            os.unlink(img_path)
                        except Exception:
                            pass
        except VisionSourceChanged as exc:
            stats["errors"] += 1
            stats["partial"] = True
            _invalidate_changed_vision_source(
                pdf,
                key,
                completed,
                vision_progress,
                vision_done,
                str(exc),
            )
            if len(stats["error_samples"]) < 20:
                stats["error_samples"].append(
                    {"path": key, "error": f"VisionSourceChanged: {exc}"[:300]}
                )
            logger.warning("  Vision source changed %s: %s", pdf.name, exc)
            _save_state(state)
            continue
        except Exception as exc:
            stats["errors"] += 1
            stats["partial"] = True
            failed_pages.update(range(next_page, page_count + 1))
            if len(stats["error_samples"]) < 20:
                stats["error_samples"].append(
                    {"path": key, "error": f"{type(exc).__name__}: {exc}"[:300]}
                )
            logger.warning("  Vision error %s: %s", pdf.name, exc)
        finally:
            if doc is not None:
                doc.close()

        added = 0
        commit_failed = False
        try:
            if source_identity is None:
                raise RuntimeError("vision_source_identity_unavailable")
            added, stored_mtime, stored_identity = _write_vision_bookmarks(
                pdf,
                new_bookmarks,
                expected_identity=source_identity,
            )
            stats["bookmarks_added"] += added
            if added:
                stats["files_refined"] += 1
                logger.info("  📑 Vision: +%d bookmarks → %s", added, pdf.name)
            completed[key]["mtime"] = stored_mtime
            completed[key]["identity"] = list(stored_identity)
        except VisionSourceChanged as exc:
            stats["errors"] += 1
            stats["partial"] = True
            _invalidate_changed_vision_source(
                pdf,
                key,
                completed,
                vision_progress,
                vision_done,
                str(exc),
            )
            if len(stats["error_samples"]) < 20:
                stats["error_samples"].append(
                    {"path": key, "error": f"VisionSourceChanged: {exc}"[:300]}
                )
            logger.warning("  Vision source changed before commit %s: %s", pdf.name, exc)
            _save_state(state)
            continue
        except Exception as exc:
            stored_mtime = str(completed[key].get("mtime") or source_mtime)
            stored_identity = source_identity or ()
            stats["errors"] += 1
            stats["partial"] = True
            commit_failed = True
            failed_pages.update(item[2] for item in new_bookmarks)
            if not failed_pages:
                # A lock/stat/validation failure with only type:null results
                # still requires a real retry before this file can be done.
                failed_pages.add(max(1, min(page_count, next_page - 1)))
            if len(stats["error_samples"]) < 20:
                stats["error_samples"].append(
                    {"path": key, "error": f"bookmark_commit:{type(exc).__name__}: {exc}"[:300]}
                )

        progress = {
            "mtime": stored_mtime,
            "next_page": min(next_page, page_count + 1),
            "failed_pages": sorted(failed_pages),
            "retry_count": retry_count + (1 if failed_pages else 0),
            "updated_at": datetime.now().isoformat(),
        }
        if file_deferred_reason:
            progress["defer_reason"] = file_deferred_reason
        if commit_failed:
            progress["commit_failed"] = True
        vision_progress[key] = progress
        stats["failed_pages"] += len(failed_pages)

        if file_deferred_reason:
            stats["deferred"] = True
            stats["defer_reason"] = file_deferred_reason
        elif not commit_failed and not failed_pages and next_page > page_count:
            vision_done[key] = {
                "mtime": stored_mtime,
                "identity": list(stored_identity),
                "added": int((vision_done.get(key) or {}).get("added") or 0)
                + added,
                "completed_at": datetime.now().isoformat(),
            }
            vision_progress.pop(key, None)
        else:
            stats["partial"] = True
            vision_done.pop(key, None)

        _save_state(state)
        if file_deferred_reason:
            break

    if stats["errors"] or stats["failed_pages"]:
        stats["partial"] = True
    _save_state(state)
    return stats


def _parse_vision_response(raw: str) -> dict | None:
    """Parse JSON from vision model response."""
    try:
        # Find JSON in response
        m = re.search(r"\{[^{}]*\}", raw)
        if m:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def _main_impl():
    import argparse

    if len(sys.argv) >= 3 and sys.argv[1] == "--_scan-file-worker":
        return _bookmark_worker_main(sys.argv[2:])

    parser = argparse.ArgumentParser(
        description="Two-stage PDF bookmark batch (regex + vision)"
    )
    parser.add_argument(
        "--stage",
        choices=["regex", "vision", "all"],
        default="all",
        help="regex=fast nightly pass (no oMLX restart), vision=refinement only, all=full weekend pass (default)",
    )
    parser.add_argument(
        "--max-minutes",
        type=int,
        default=0,
        help="Soft time budget for regex stage in minutes (0=no limit). Nightly runs should cap ~30.",
    )
    parser.add_argument(
        "--max-runtime-minutes",
        type=int,
        default=max(1, DEFAULT_WALLCLOCK_BUDGET_SECONDS // 60),
        help="Unified wall-clock budget for all stages (default: 300 minutes).",
    )
    parser.add_argument(
        "--vision-max-pages",
        type=int,
        default=VISION_MAX_PAGES,
        help="Never send a PDF above this page count to stage2 vision (default: 350).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only output a backfill plan (no PDF writes, no oMLX restart).",
    )
    parser.add_argument(
        "--plan-limit",
        type=int,
        default=20,
        help="Number of sample paths per plan category.",
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="Explicit folder to process recursively. Use for a priority case/evidence folder; can be repeated.",
    )
    parser.add_argument(
        "--target-subdir",
        action="append",
        default=[],
        help="Case subfolder name to scan during normal root discovery. Defaults include evidence and review folders.",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help="Progress JSON path. Defaults to MAGI_AGENT_DIR/bookmark_batch_state.json; priority/example runs should use a separate file.",
    )
    parser.add_argument(
        "--report-path",
        default=None,
        help="Optional JSON report path for this run.",
    )
    parser.add_argument(
        "--single-doc-fastpath",
        action="store_true",
        help="Skip obvious single-document filenames in priority/example runs and record them as legitimate single docs.",
    )
    parser.add_argument(
        "--skip-large-non-ocr",
        action="store_true",
        help="In priority/example runs, mark large non-OCR PDFs as needs_ocr instead of spending the batch on OCR.",
    )
    parser.add_argument(
        "--defer-large-ocr-pages",
        type=int,
        default=0,
        help="In priority/example runs, defer OCR PDFs at or above this page count to a split/off-peak pass.",
    )
    parser.add_argument(
        "--file-timeout-sec",
        type=int,
        default=MAX_FILE_SCAN_TIMEOUT_SECONDS,
        help="Hard timeout per PDF for stage1 scan (capped at 45s). Timed-out PDFs are recorded for split/off-peak follow-up.",
    )
    parser.add_argument(
        "--retry-no-boundary",
        action="store_true",
        help="Reprocess same-mtime PDFs previously marked no_boundary; used after boundary/OCR rules improve.",
    )
    parser.add_argument(
        "--retry-deferred",
        action="store_true",
        help="Reprocess same-mtime PDFs previously deferred as large OCR PDFs.",
    )
    parser.add_argument(
        "--retry-needs-ocr",
        action="store_true",
        help="Re-evaluate same-mtime PDFs previously marked needs_ocr.",
    )
    parser.add_argument(
        "--write-followup-plan",
        action="store_true",
        help="Write an actionable OCR/no-boundary/deferred follow-up plan JSON after discovery/run.",
    )
    parser.add_argument(
        "--enqueue-ocr-followups",
        action="store_true",
        help=(
            "Add ocr_then_bookmark follow-up items to the shared NAS OCR queue "
            "selected by MAGI_NAS_OCR_QUEUE_DB_PATH (V2 fallback: "
            "~/.magi_nas_ocr_queue.db)."
        ),
    )
    args = parser.parse_args()
    _set_state_file(args.state_file)
    if args.single_doc_fastpath:
        os.environ["BOOKMARK_SINGLE_DOC_FASTPATH"] = "1"
    if args.skip_large_non_ocr:
        os.environ["BOOKMARK_SKIP_LARGE_NON_OCR"] = "1"
    if args.defer_large_ocr_pages > 0:
        os.environ["BOOKMARK_DEFER_LARGE_OCR_PAGES"] = str(args.defer_large_ocr_pages)
    if args.file_timeout_sec > 0:
        os.environ["BOOKMARK_FILE_TIMEOUT_SEC"] = str(args.file_timeout_sec)
    if args.retry_no_boundary:
        os.environ["BOOKMARK_RETRY_NO_BOUNDARY"] = "1"
    if args.retry_deferred:
        os.environ["BOOKMARK_RETRY_DEFERRED"] = "1"
    if args.retry_needs_ocr:
        os.environ["BOOKMARK_RETRY_NEEDS_OCR"] = "1"
    started = time.time()
    started_monotonic = time.monotonic()
    deadline = (
        started_monotonic + max(1, int(args.max_runtime_minutes)) * 60
        if args.max_runtime_minutes > 0
        else None
    )
    state = _load_state()

    target_subdirs = args.target_subdir or TARGET_SUBDIRS
    # preferred_case_roots already prefers NAS SMB over Synology Drive
    roots = [str(Path(r).expanduser()) for r in args.root] if args.root else preferred_case_roots(include_closed=False)
    label = "Nightly Regex" if args.stage == "regex" else ("Vision Only" if args.stage == "vision" else "Weekend")
    logger.info(f"📑 Bookmark Batch [{label}] — roots: {roots}")
    logger.info(f"Progress state: {STATE_FILE}")
    if not args.root:
        logger.info(f"Target subfolders: {target_subdirs}")

    pdfs = find_direct_pdfs(roots) if args.root else find_all_pdfs(roots, target_subdirs)
    pdfs = _resume_pdf_order(pdfs, state)
    logger.info(f"Found {len(pdfs)} PDFs across all case folders")

    if not pdfs:
        logger.info("No PDFs to process — done")
        print(json.dumps({"ok": True, "success": True, "status": "success", "pdf_count": 0}))
        return 0

    if args.dry_run:
        plan = build_backfill_plan(pdfs, state, sample_limit=max(1, args.plan_limit))
        BACKFILL_PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
        BACKFILL_PLAN_PATH.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.write_followup_plan or args.enqueue_ocr_followups:
            followup = build_followup_plan(pdfs, state, sample_limit=max(1, args.plan_limit))
            write_followup_plan(followup)
            if args.enqueue_ocr_followups:
                added = enqueue_ocr_followups(followup)
                logger.info("Queued %d OCR follow-up PDFs", added)
        logger.info("Dry-run backfill plan written: %s", BACKFILL_PLAN_PATH)
        logger.info(
            "Plan summary: total=%d stage1_pending=%d no_boundary=%d vision_pending=%d",
            plan["total_pdfs"],
            plan["stage1_pending_count"],
            plan["no_boundary_backlog_count"],
            plan["vision_pending_count"],
        )
        print(json.dumps({"ok": True, "success": True, "status": "success", "dry_run": True}))
        return 0

    s1 = {
        "processed": 0,
        "bookmarks": 0,
        "skipped": 0,
        "no_boundary": 0,
        "single_doc": 0,
        "needs_ocr": 0,
        "deferred_large_ocr": 0,
        "file_timeout": 0,
        "quarantined": 0,
        "errors": 0,
        "deferred": False,
        "defer_reason": "",
        "visited": 0,
        "scan_complete": False,
    }
    s2 = {
        "pages_checked": 0,
        "bookmarks_added": 0,
        "files_refined": 0,
        "errors": 0,
        "failed_pages": 0,
        "deferred": False,
        "defer_reason": "",
        "partial": False,
    }

    do_regex = args.stage in ("regex", "all")
    do_vision = args.stage in ("vision", "all")

    if do_regex:
        # ── Stage 1: Regex (fast, no LLM needed) ──
        logger.info("═══ Stage 1: Regex bookmarks ═══")
        # Keep oMLX lifecycle under its configured TTL.  This job never kills a
        # loaded model directly; admission and in-run pressure checks defer work.

        scan_fn = _load_bookmarker()
        # Pass soft budget through env so stage1_regex can honor it without signature churn
        if args.max_minutes > 0:
            os.environ["BOOKMARK_REGEX_BUDGET_SEC"] = str(args.max_minutes * 60)
        s1 = stage1_regex(pdfs, state, scan_fn, deadline=deadline)
        logger.info(
            f"Stage 1 done: {s1['processed']} processed, "
            f"{s1['bookmarks']} bookmarks, {s1['skipped']} skipped, "
            f"{s1.get('single_doc', 0)} single-doc, "
            f"{s1['no_boundary']} no-boundary, {s1['errors']} errors ({time.time() - started:.0f}s)"
        )

    if do_vision:
        # ── Stage 2: Vision refinement (needs oMLX) ──
        logger.info("═══ Stage 2: Vision refinement ═══")
        if s1.get("deferred"):
            s2["deferred"] = True
            s2["defer_reason"] = str(s1.get("defer_reason") or "stage1_deferred")
            logger.info("Stage 2 deferred because Stage 1 reached a safe checkpoint")
            omlx_ok = False
        # For --stage vision we assume oMLX is already up; only --stage all had to restart it.
        else:
            omlx_ok = _start_omlx(deadline=deadline) if args.stage == "all" else True
        if not omlx_ok and not s2.get("deferred"):
            logger.warning("oMLX not available — deferring vision stage")
            s2["deferred"] = True
            s2["defer_reason"] = _defer_reason(deadline) or "omlx_unavailable"
        elif omlx_ok:
            gw = _load_vision_gateway()
            if gw:
                s2 = stage2_vision(
                    pdfs,
                    state,
                    gw,
                    deadline=deadline,
                    max_pages=args.vision_max_pages,
                )
            else:
                s2["deferred"] = True
                s2["defer_reason"] = "vision_gateway_unavailable"

    elapsed = time.time() - started
    total_errors = int(s1.get("errors") or 0) + int(s2.get("errors") or 0)
    is_deferred = bool(s1.get("deferred") or s2.get("deferred"))
    is_partial = bool(total_errors or s2.get("partial") or s2.get("failed_pages"))
    # Genuine errors/failed pages must remain actionable even when the same run
    # later reaches a safe resource checkpoint.
    status = "partial" if is_partial else ("deferred" if is_deferred else "success")
    if status == "partial":
        partial_parts = [
            f"errors={total_errors}",
            f"failed_pages={int(s2.get('failed_pages') or 0)}",
        ]
        error_samples = s2.get("error_samples") or []
        if error_samples:
            partial_parts.append(str((error_samples[0] or {}).get("error") or "")[:160])
        status_reason = "partial:" + ",".join(partial_parts)
    elif status == "deferred":
        status_reason = str(s2.get("defer_reason") or s1.get("defer_reason") or "deferred")
    else:
        status_reason = ""
    status_label = {"success": "完成", "deferred": "已安全延期", "partial": "部分完成"}[status]
    lines = [f"📑 Bookmark Batch [{label}] {status_label}"]
    lines.append(f"  PDF 數量：{len(pdfs)} 個")
    if do_regex:
        lines.append("  ── Stage 1 (regex) ──")
        lines.append(f"  處理：{s1['processed']} 份 / {s1['bookmarks']} 個書籤")
        lines.append(f"  跳過：{s1['skipped']} 份")
        if s1.get("single_doc"):
            lines.append(f"  單一文件：{s1['single_doc']} 份（已補 page-1 書籤）")
        lines.append(f"  無邊界（待 vision 補漏）：{s1['no_boundary']} 份")
        if s1.get("needs_ocr"):
            lines.append(f"  大型非 OCR（待 OCR 後再編）：{s1['needs_ocr']} 份")
        if s1.get("deferred_large_ocr"):
            lines.append(f"  大型 OCR（待分割/離峰重跑）：{s1['deferred_large_ocr']} 份")
        if s1.get("file_timeout"):
            lines.append(f"  單檔逾時（已排入分割流程）：{s1['file_timeout']} 份")
        if s1.get("quarantined"):
            lines.append(
                f"  結構異常（已隔離待單檔修復）：{s1['quarantined']} 份"
            )
    if do_vision:
        lines.append("  ── Stage 2 (vision) ──")
        lines.append(f"  視覺檢查：{s2['pages_checked']} 頁")
        lines.append(f"  補充書籤：{s2['bookmarks_added']} 個 / {s2['files_refined']} 份")
    lines.append(f"  錯誤：{total_errors} 筆")
    if status != "success":
        lines.append(f"  狀態：{status}（{status_reason}）")
    lines.append(f"  耗時：{elapsed:.0f} 秒（{elapsed / 3600:.1f} 小時）")
    summary = "\n".join(lines)
    logger.info(summary)

    followup = None
    if args.write_followup_plan or args.enqueue_ocr_followups:
        storage_unavailable = any(
            str(stage.get("defer_reason") or "") == "storage_unavailable"
            for stage in (s1, s2)
        )
        if storage_unavailable:
            # The previously discovered Path objects become false negatives
            # when smbfs disconnects mid-run.  Do not overwrite the follow-up
            # plan with thousands of fictitious "file_not_found" records.
            followup = {
                "generated_at": datetime.now().isoformat(),
                "status": status,
                "reason": "storage_unavailable",
                "total_pdfs": len(pdfs),
                "counts": {"storage_unavailable": len(pdfs)},
                "pending_count": len(pdfs),
                "actions": [],
                "truncated": bool(pdfs),
            }
        else:
            followup = build_followup_plan(
                pdfs,
                state,
                sample_limit=max(1, args.plan_limit),
            )
        write_followup_plan(followup)
        logger.info("Follow-up plan written: %s", FOLLOWUP_PLAN_PATH)
        if args.enqueue_ocr_followups:
            added = enqueue_ocr_followups(followup)
            logger.info("Queued %d OCR follow-up PDFs", added)

    if args.report_path:
        report_path = Path(args.report_path).expanduser()
        if not report_path.is_absolute():
            report_path = (_RUNTIME_DIR if os.environ.get("MAGI_RUNTIME_DIR") else MAGI_ROOT) / report_path
        completed = state.get("completed", {}) or {}
        stage1_errors = state.get("stage1_errors", {}) or {}
        relevant_completed = [
            key for key in completed
            if any(str(Path(root).expanduser()) in key for root in roots)
        ] if args.root else list(completed.keys())
        relevant_errors = [
            key for key in stage1_errors
            if any(str(Path(root).expanduser()) in key for root in roots)
        ] if args.root else list(stage1_errors.keys())
        report = {
            "timestamp": datetime.now().isoformat(),
            "label": label,
            "stage": args.stage,
            "status": status,
            "success": status == "success",
            "roots": roots,
            "target_subdirs": target_subdirs if not args.root else None,
            "state_file": str(STATE_FILE),
            "pdf_count": len(pdfs),
            "stats": {"regex": s1, "vision": s2},
            "completed_count_for_roots": len(relevant_completed),
            "bookmarked_count_for_roots": sum(
                1 for key in relevant_completed
                if int((completed.get(key) or {}).get("stage1_bookmarks") or 0) > 0
            ),
            "no_boundary_count_for_roots": sum(
                1 for key in relevant_completed
                if (completed.get(key) or {}).get("no_boundary")
            ),
            "stage1_error_count_for_roots": len(relevant_errors),
            "stage1_error_samples": [
                {
                    "path": key,
                    "message": str((stage1_errors.get(key) or {}).get("message") or ""),
                }
                for key in relevant_errors[:20]
            ],
            "elapsed_sec": round(elapsed, 3),
        }
        if followup is not None:
            report["followup_counts"] = followup.get("counts", {})
            report["followup_pending_count"] = followup.get("pending_count", 0)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Run report written: %s", report_path)

    # Notify (weekend full pass gets the system channel; nightly is quieter — log only)
    if args.stage == "all":
        try:
            from api.red_phone import notify
            notify(summary, channel="system")
        except Exception:
            pass

    result = {
        "ok": status == "success",
        "success": status == "success",
        "status": status,
        "deferred": status == "deferred",
        "partial": is_partial,
        "reason": status_reason,
        "pdf_count": len(pdfs),
        "elapsed_sec": round(elapsed, 3),
        "stats": {"regex": s1, "vision": s2},
    }
    _write_receipt(
        state=state,
        pdf_count=len(pdfs),
        status=status,
        reason=status_reason,
        stats=s1,
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if status == "success" else 75


def main():
    previous_signal_handlers = _install_stop_handlers()
    try:
        return _main_impl()
    finally:
        _restore_stop_handlers(previous_signal_handlers)


if __name__ == "__main__":
    raise SystemExit(main())
