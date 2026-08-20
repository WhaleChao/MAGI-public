from __future__ import annotations

import os
import sys
import json
from pathlib import Path
from typing import Mapping, Optional


_MAGI_ROOT_DEFAULT = Path(__file__).resolve().parent.parent
_LEGACY_CODE_ROOT_SENTINEL = _MAGI_ROOT_DEFAULT / ".legacy_code_disabled"
_ORCH_RELATIVE = Path("casper_ecosystem") / "law_firm_orchestrators"


def _env_path(*names: str) -> Optional[Path]:
    for name in names:
        raw = (os.environ.get(name) or "").strip()
        if raw:
            return Path(raw).expanduser()
    return None


def _env_flag(name: str, default: str = "0") -> bool:
    return (os.environ.get(name) or default).strip().lower() in {"1", "true", "yes", "on"}


def dotenv_override_allowed(environ: Mapping[str, str] | None = None) -> bool:
    """Keep V2's edited-env behavior while protecting V3 launch bindings."""
    source = os.environ if environ is None else environ
    return not bool((source.get("MAGI_V3_RELEASE_ID") or "").strip())


def _unique_paths(items: list[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for item in items:
        try:
            key = str(item.resolve())
        except Exception:
            key = str(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def ensure_path_on_sys_path(path: Path | str) -> Path:
    p = Path(path).expanduser()
    try:
        s = str(p.resolve())
    except Exception:
        s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)
    return Path(s)


def ensure_magi_root_on_sys_path() -> Path:
    return ensure_path_on_sys_path(get_magi_root_dir())


def ensure_orch_on_sys_path() -> Path:
    return ensure_path_on_sys_path(get_orch_dir())


def get_magi_root_dir() -> Path:
    env = _env_path("MAGI_ROOT_DIR", "MAGI_ROOT")
    if env:
        return env.resolve()
    return _MAGI_ROOT_DEFAULT


def get_runtime_dir() -> Path:
    env = _env_path("MAGI_RUNTIME_DIR")
    if env:
        return env.resolve()
    shared = _env_path("MAGI_SHARED_STATE_DIR", "MAGI_V3_SHARED_STATE_DIR")
    if shared:
        return (shared / "runtime").resolve()
    return (get_magi_root_dir() / ".runtime").resolve()


def get_agent_dir() -> Path:
    env = _env_path("MAGI_AGENT_DIR", "MAGI_DATA_DIR")
    if env:
        return env.resolve()
    shared = _env_path("MAGI_SHARED_STATE_DIR", "MAGI_V3_SHARED_STATE_DIR")
    if shared:
        return (shared / "agent").resolve()
    runtime = _env_path("MAGI_RUNTIME_DIR")
    if os.environ.get("MAGI_V3_RELEASE_ID", "").strip() and runtime:
        return (runtime.parent / "agent").resolve()
    return (get_magi_root_dir() / ".agent").resolve()


def get_transcript_download_dir() -> Path:
    """Return the writable staging directory for judicial transcripts.

    Sealed V3 releases must never fall back to ``./筆錄下載`` because their
    working directory is immutable. V2 keeps the historical in-tree default
    unless an explicit or shared runtime binding is present.
    """

    explicit = _env_path("MAGI_TRANSCRIPT_DOWNLOAD_DIR")
    if explicit:
        return explicit.resolve()
    shared = _env_path("MAGI_SHARED_STATE_DIR", "MAGI_V3_SHARED_STATE_DIR")
    if shared:
        return (shared / "transcript-downloads").resolve()
    runtime = _env_path("MAGI_RUNTIME_DIR")
    if os.environ.get("MAGI_V3_RELEASE_ID", "").strip() and runtime:
        return (runtime.parent / "transcript-downloads").resolve()
    return (get_magi_root_dir() / "筆錄下載").resolve()


def get_laf_orchestrator_state_dir() -> Path:
    """Return the mutable LAF/orchestrator handoff directory.

    V3 launchers bind ``MAGI_AGENT_DIR`` outside the immutable release.  V2
    keeps its historical in-tree location unless an explicit handoff path is
    configured, so this resolver can be deployed before cutover without
    splitting producer/consumer state.
    """

    explicit = _env_path("MAGI_LAF_ORCHESTRATOR_STATE_DIR")
    if explicit:
        return explicit.resolve()
    agent = _env_path("MAGI_AGENT_DIR", "MAGI_DATA_DIR")
    if agent:
        return (agent / "laf-orchestrator").resolve()
    return get_orch_dir()


def get_laf_orchestrator_state_path(filename: str) -> Path:
    """Resolve one simple mutable orchestrator filename outside V3 code."""

    name = Path(filename)
    if name.name != filename or filename in {"", ".", ".."}:
        raise ValueError("orchestrator state filename must be a simple basename")
    return (get_laf_orchestrator_state_dir() / name).resolve()


def get_hearing_leave_template_path() -> Path:
    """Return the external, de-identified hearing-leave template location."""

    explicit = _env_path("MAGI_HEARING_LEAVE_TEMPLATE_PATH")
    if explicit:
        return explicit.resolve()
    shared = _env_path("MAGI_SHARED_STATE_DIR", "MAGI_V3_SHARED_STATE_DIR")
    if shared:
        return (shared / "templates" / "hearing-leave-public.docx").resolve()
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "MAGI"
        / "shared"
        / "templates"
        / "hearing-leave-public.docx"
    ).resolve()


def get_mutable_static_dir() -> Path:
    env = _env_path("MAGI_MUTABLE_STATIC_DIR")
    return env.resolve() if env else (get_magi_root_dir() / "static").resolve()


def get_exports_dir() -> Path:
    env = _env_path("MAGI_EXPORTS_DIR")
    return env.resolve() if env else (get_magi_root_dir() / "exports").resolve()


def get_logs_dir() -> Path:
    env = _env_path("MAGI_LOG_DIR", "MAGI_V3_LOG_DIR")
    return env.resolve() if env else (get_magi_root_dir() / "logs").resolve()


def get_file_review_pending_path(root: Path | str | None = None) -> Path:
    """Return the shared file-review confirmation store without changing V2 defaults."""
    explicit = _env_path("MAGI_FILE_REVIEW_PENDING_PATH")
    if explicit:
        return explicit.resolve()
    agent = _env_path("MAGI_AGENT_DIR", "MAGI_DATA_DIR")
    if agent:
        return (agent / "file-review" / "review_submit_pending.json").resolve()
    base = Path(root).expanduser() if root is not None else get_magi_root_dir()
    return (base / "skills" / "file-review-orchestrator" / ".review_submit_pending.json").resolve()


def _bound_named_shared_file(
    env_name: str,
    shared_relative: str,
    source_fallback: Path | str,
) -> Path:
    """Use the V3 fail-closed resolver without imposing it on V2 callers."""

    from magi_v3.external_inputs import bound_shared_file

    return bound_shared_file(
        get_magi_root_dir(),
        env_name=env_name,
        shared_relative=shared_relative,
        source_fallback=source_fallback,
    )


def get_laf_processed_emails_path() -> Path:
    """Return the one LAF Gmail dedup file shared across restarts/cutover."""

    return _bound_named_shared_file(
        "MAGI_LAF_PROCESSED_EMAILS_PATH",
        "agent/laf-orchestrator/processed_laf_emails.json",
        get_laf_orchestrator_state_path("processed_laf_emails.json"),
    )


def get_payment_registry_path(download_folder: Path | str | None = None) -> Path:
    """Return the canonical file-review payment registry."""

    fallback_root = (
        Path(download_folder).expanduser()
        if download_folder is not None
        else get_magi_root_dir() / "閱卷下載"
    )
    return _bound_named_shared_file(
        "MAGI_PAYMENT_REGISTRY_PATH",
        "file-review/downloads/payment_registry.json",
        fallback_root / "payment_registry.json",
    )


def get_payment_proof_registry_path(download_folder: Path | str | None = None) -> Path:
    """Return the canonical file-review payment-proof registry."""

    fallback_root = (
        Path(download_folder).expanduser()
        if download_folder is not None
        else get_magi_root_dir() / "閱卷下載"
    )
    return _bound_named_shared_file(
        "MAGI_PAYMENT_PROOF_REGISTRY_PATH",
        "file-review/downloads/payment_proof_registry.json",
        fallback_root / "payment_proof_registry.json",
    )


def get_payment_proof_upload_queue_path() -> Path:
    """Return the durable queue used when the single court portal is busy."""

    # Production already binds the whole file-review state domain.  Derive the
    # queue from that declared boundary so a sealed V3 release does not invent
    # a new mutable-state authority at import time.
    state_dir = _env_path("MAGI_FILE_REVIEW_STATE_DIR")
    if state_dir:
        return (state_dir / "payment-proof-upload-queue.json").resolve()
    return _bound_named_shared_file(
        "MAGI_PAYMENT_PROOF_UPLOAD_QUEUE_PATH",
        "file-review/payment-proof-upload-queue.json",
        get_runtime_dir() / "file-review" / "payment-proof-upload-queue.json",
    )


def get_payment_proof_upload_store_dir() -> Path:
    """Return persistent storage for screenshots waiting in the upload queue."""

    explicit = _env_path("MAGI_PAYMENT_PROOF_UPLOAD_STORE_DIR")
    if explicit:
        return explicit.resolve()
    state_dir = _env_path("MAGI_FILE_REVIEW_STATE_DIR")
    if state_dir:
        return (state_dir / "payment-proof-pending-files").resolve()
    shared = _env_path("MAGI_SHARED_STATE_DIR", "MAGI_V3_SHARED_STATE_DIR")
    if shared:
        return (shared / "file-review" / "payment-proof-pending-files").resolve()
    return (get_runtime_dir() / "file-review" / "payment-proof-pending-files").resolve()


def get_judgments_json_path() -> Path:
    """Return the mutable judgment export; sealed V3 requires an exact binding."""

    return _bound_named_shared_file(
        "MAGI_JUDGMENTS_JSON_PATH",
        "agent/judgment-collector/judgments.json",
        get_magi_root_dir() / "skills" / "judgment-collector" / "judgments.json",
    )


def get_judicial_archive_dir() -> Path:
    """Return the writable archive used by the judicial search pipeline.

    Sealed V3 releases are read-only, so their archive must live in the bound
    shared state. Legacy/source runs retain the historical in-tree fallback.
    """

    explicit = _env_path("JUDICIAL_ARCHIVE_ROOT")
    if explicit:
        return explicit.resolve()
    shared = _env_path("MAGI_SHARED_STATE_DIR", "MAGI_V3_SHARED_STATE_DIR")
    if shared:
        return (shared / "archive" / "judicial_search").resolve()
    runtime = _env_path("MAGI_RUNTIME_DIR")
    if os.environ.get("MAGI_V3_RELEASE_ID", "").strip() and runtime:
        return (runtime.parent / "archive" / "judicial_search").resolve()
    return (get_magi_root_dir() / "archive" / "judicial_search").resolve()


def get_database_backup_dir() -> Path:
    """Return the writable directory for managed database backups."""

    explicit = _env_path("MAGI_DB_BACKUP_DIR")
    if explicit:
        return explicit.resolve()
    shared = _env_path("MAGI_SHARED_STATE_DIR", "MAGI_V3_SHARED_STATE_DIR")
    if shared:
        return (shared / "db-backups" / "law_firm_data").resolve()
    runtime = _env_path("MAGI_RUNTIME_DIR")
    if os.environ.get("MAGI_V3_RELEASE_ID", "").strip() and runtime:
        return (runtime.parent / "db-backups" / "law_firm_data").resolve()
    return (get_magi_root_dir() / "_db_backups" / "law_firm_data").resolve()


def get_pdf_namer_case_index_path() -> Path:
    """Return the pdf-namer case index shared by its producer and consumers."""

    state_dir = _env_path("MAGI_PDF_NAMER_STATE_DIR")
    if state_dir is None:
        v3_state = _env_path("MAGI_V3_STATE_DIR")
        state_dir = v3_state / "pdf-namer" if v3_state else None
    fallback = (
        state_dir / "_case_index.json"
        if state_dir
        else get_magi_root_dir() / "skills" / "pdf-namer" / "_case_index.json"
    )
    return _bound_named_shared_file(
        "MAGI_PDF_NAMER_CASE_INDEX",
        "pdf-namer/_case_index.json",
        fallback,
    )


def get_cortex_sync_state_path() -> Path:
    """Return the durable Cortex cursor outside a sealed release."""

    runtime = _env_path("MAGI_RUNTIME_DIR")
    fallback = (
        runtime / "cortex_sync_state.json"
        if runtime
        else get_magi_root_dir() / "cortex_sync_state.json"
    )
    return _bound_named_shared_file(
        "MAGI_CORTEX_SYNC_STATE_PATH",
        "runtime/cortex_sync_state.json",
        fallback,
    )


def get_faiss_index_dir(
    root: Path | str | None = None, *, honor_environment: bool = True
) -> Path:
    """Return the FAISS producer/consumer directory with the legacy V2 fallback."""
    explicit = _env_path("FAISS_INDEX_DIR") if honor_environment else None
    if explicit:
        return explicit.resolve()
    shared = _env_path("MAGI_SHARED_STATE_DIR") if honor_environment else None
    if shared:
        return (shared / "memory" / "index_cache").resolve()
    base = Path(root).expanduser() if root is not None else get_magi_root_dir()
    return (base / "skills" / "memory" / "index_cache").resolve()


def get_faiss_active_artifact(
    name: str,
    root: Path | str | None = None,
    *,
    honor_environment: bool = True,
) -> Path:
    """Resolve one committed FAISS artifact with a V2 flat-layout fallback."""

    if name not in {"mem_index.faiss", "mem_idmap.npy", "meta.json"}:
        raise ValueError("unsupported FAISS artifact name")
    index_dir = get_faiss_index_dir(root, honor_environment=honor_environment)
    active_path = index_dir / "active_generation.json"
    try:
        active = json.loads(active_path.read_text(encoding="utf-8"))
        generation = str(active.get("active_generation") or "")
        if (
            active.get("schema_version") == 1
            and generation
            and "/" not in generation
            and not generation.startswith(".")
        ):
            candidate = index_dir / "generations" / generation / name
            if candidate.is_file():
                return candidate
    except Exception:
        pass
    return index_dir / name


def get_env_file() -> Path:
    """Return the external runtime secret file without reading it."""

    configured = _env_path("MAGI_ENV_FILE")
    if configured:
        return configured.resolve()
    return (get_magi_root_dir() / ".env").resolve()


def get_legacy_code_root() -> Path:
    env = _env_path("MAGI_LEGACY_CODE_DIR")
    if env:
        return env.resolve()
    # Keep old call sites self-contained: when legacy mode is not enabled,
    # treat the MAGI root itself as the effective compatibility root.
    if not _env_flag("MAGI_ENABLE_LEGACY_CODE_ROOT", "0"):
        return get_magi_root_dir()
    return _LEGACY_CODE_ROOT_SENTINEL


def legacy_code_enabled() -> bool:
    if _env_path("MAGI_LEGACY_CODE_DIR") is not None:
        return True
    return _env_flag("MAGI_ENABLE_LEGACY_CODE_ROOT", "0")


def get_orch_dir() -> Path:
    env = _env_path("MAGI_ORCH_DIR")
    if env and env.is_dir():
        return env.resolve()

    compat = _env_path("MAGI_CODE_DIR")
    if compat and (compat / "laf_orchestrator.py").exists():
        return compat.resolve()

    magi_default = get_magi_root_dir() / _ORCH_RELATIVE
    if magi_default.is_dir():
        return magi_default.resolve()

    legacy = get_legacy_code_root()
    if legacy_code_enabled() and legacy.is_dir():
        return legacy.resolve()
    return magi_default.resolve()


def get_json_dir() -> Path:
    env = _env_path("MAGI_JSON_DIR")
    if env:
        return env.resolve()

    magi_json = get_magi_root_dir() / "json"
    orch_json = get_orch_dir() / "json"
    paths = [magi_json, orch_json]
    if legacy_code_enabled():
        paths.append(get_legacy_code_root() / "json")
    for path in paths:
        if path.is_dir():
            return path.resolve()
    return magi_json.resolve()


def get_metrics_dir() -> Path:
    env = _env_path("MAGI_METRICS_DIR")
    if env:
        return env.resolve()
    return (get_magi_root_dir() / "_metrics").resolve()


def get_autopilot_runs_dir() -> Path:
    env = _env_path("MAGI_AUTOPILOT_RUNS_DIR")
    if env:
        return env.resolve()
    return (get_magi_root_dir() / "_autopilot_runs").resolve()


def config_candidates(name: str = "config.json") -> list[Path]:
    env_specific = _env_path("MAGI_CONFIG_PATH") if name == "config.json" else None
    items: list[Optional[Path]] = [
        env_specific,
        get_json_dir() / name,
        get_orch_dir() / "json" / name,
        get_orch_dir() / name,
    ]
    if legacy_code_enabled():
        items.extend(
            [
                get_legacy_code_root() / "json" / name,
                get_legacy_code_root() / name,
            ]
        )
    return _unique_paths([p for p in items if p is not None])


def get_config_path(name: str = "config.json") -> Path:
    for path in config_candidates(name):
        if path.exists():
            return path.resolve()
    return config_candidates(name)[0]


def get_module_path(filename: str) -> Path:
    candidates = [
        get_orch_dir() / filename,
    ]
    if legacy_code_enabled():
        candidates.append(get_legacy_code_root() / filename)
    for path in _unique_paths(candidates):
        if path.exists():
            return path.resolve()
    return candidates[0]


def get_laf_script() -> Path:
    return get_module_path("laf_orchestrator.py")


def get_skill_python() -> Path:
    env = _env_path("MAGI_SKILL_PYTHON")
    if env and env.exists():
        return env

    root = get_magi_root_dir()
    _IS_WIN = sys.platform == "win32"

    # Check venv candidates (cross-platform)
    if _IS_WIN:
        candidates = [
            root / "venv" / "Scripts" / "python.exe",
            root / ".venv" / "Scripts" / "python.exe",
            get_orch_dir() / ".venv" / "Scripts" / "python.exe",
        ]
    else:
        candidates = [
            root / "venv" / "bin" / "python3",
            root / "venv" / "bin" / "python",
            root / ".venv" / "bin" / "python3",
            get_orch_dir() / ".venv" / "bin" / "python",
        ]

    if legacy_code_enabled():
        if _IS_WIN:
            candidates.append(get_legacy_code_root() / ".venv" / "Scripts" / "python.exe")
        else:
            candidates.append(get_legacy_code_root() / ".venv" / "bin" / "python")

    for p in candidates:
        if p.exists():
            return p

    return Path(sys.executable or "python3")
