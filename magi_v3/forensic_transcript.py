"""MAGI V3 adapter for the shared forensic transcript verification skill."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import sys
import time
from urllib.parse import urlparse
from pathlib import Path
from typing import Any, Mapping

from .dispatcher import VerifiedCompletion
from .state import JobStatus
from .supervisor import WorkerResult, WorkerSpec


CAPABILITY = "audio_transcription_translation"
ALLOWED_OPERATIONS = frozenset(
    {"inspect", "audit", "validate-docx", "full-check", "autonomous-plan", "autonomous"}
)
ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / "skills" / "forensic-transcript-verifier" / "action.py"
TRANSCRIPTION_BACKENDS = ROOT / "config" / "v3_transcription_backends.json"
COMPLETION_BINDING = "v3-completion-binding.json"
V3_PRODUCTION_ROOT = (
    Path.home() / "Library" / "Application Support" / "MAGI" / "runtime" / "MAGI_v3"
).resolve()
_PATH_FIELDS = ("video", "transcript", "baseline", "asr_json", "secondary_asr_json")
_RESOURCE_MINIMUMS = {
    "inspect": (512.0, 0.0, 100, "light", "none"),
    "audit": (512.0, 0.0, 100, "light", "none"),
    "validate-docx": (1024.0, 0.0, 150, "light", "none"),
    "full-check": (1536.0, 0.0, 200, "heavy", "none"),
    "autonomous-plan": (768.0, 0.0, 150, "light", "none"),
    # Local oMLX is reached only through a Seatbelt-allowlisted loopback port.
    "autonomous": (6144.0, 3072.0, 400, "heavy", "light"),
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lease_token(lease: Any) -> str:
    token = str(getattr(lease, "token", None) or getattr(lease, "lease_token", None) or "")
    if not token:
        raise ValueError("forensic transcript lease token is required")
    return token


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _mutable_root() -> Path:
    root = Path(
        os.environ.get("MAGI_V3_FORENSIC_MUTABLE_ROOT")
        or Path.home()
        / "Library"
        / "Application Support"
        / "MAGI"
        / "v3"
        / "forensic-transcript"
    ).expanduser().resolve()
    if _within(root, ROOT) or _within(root, V3_PRODUCTION_ROOT):
        raise ValueError("V3 forensic mutable root overlaps release/production runtime")
    return root


def _workspace(job: Any, lease: Any) -> Path:
    safe_job = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(job.job_id)).strip("-.") or "job"
    token_hash = hashlib.sha256(_lease_token(lease).encode("utf-8")).hexdigest()[:16]
    attempt = int(getattr(lease, "attempt_number", 0))
    return (_mutable_root() / f"{safe_job}-{attempt}-{token_hash}").resolve()


def _input_evidence(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for field in _PATH_FIELDS:
        raw = str(payload.get(field) or "").strip()
        if not raw:
            continue
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"forensic transcript input is missing: {field}={path}")
        if _within(path, ROOT) or _within(path, V3_PRODUCTION_ROOT):
            raise ValueError(f"forensic transcript input overlaps release/V3 production: {field}")
        evidence[field] = {
            "path": str(path),
            "size": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
    return evidence


def _task_payload(
    job: Any,
    lease: Any,
    *,
    asr_runtime: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if job.capability != CAPABILITY:
        raise ValueError(f"unsupported capability for forensic transcript worker: {job.capability}")
    operation = str(job.operation or "").strip().lower().replace("_", "-")
    if operation not in ALLOWED_OPERATIONS:
        raise ValueError(f"unsupported forensic transcript operation: {operation}")
    if not isinstance(job.input, Mapping):
        raise ValueError("forensic transcript job input must be an object")
    payload = dict(job.input)
    payload["operation"] = operation
    required = {
        "inspect": ("video", "transcript", "baseline"),
        "audit": ("transcript", "baseline"),
        "validate-docx": ("transcript",),
        "full-check": ("video", "transcript", "baseline"),
        "autonomous-plan": ("video", "transcript", "baseline", "asr_json"),
        "autonomous": ("video", "transcript", "baseline"),
    }[operation]
    missing = [name for name in required if not str(payload.get(name) or "").strip()]
    if missing:
        raise ValueError(f"forensic transcript job missing required input: {', '.join(missing)}")
    if not ACTION.is_file():
        raise ValueError(f"forensic transcript action is missing: {ACTION}")
    workspace = _workspace(job, lease)
    payload["output_dir"] = str(workspace)
    if operation == "autonomous":
        output_name = Path(str(payload.get("output_docx") or "court-transcript.docx")).name
        payload["output_docx"] = str(workspace / output_name)
        payload["require_generated_dual_asr"] = True
        payload["dual_asr_execution"] = "serialized"
    evidence = _input_evidence(payload)
    issued_at_ns = time.time_ns()
    payload["_v3_contract"] = {
        "schema_version": 1,
        "job_id": str(job.job_id),
        "attempt_number": int(getattr(lease, "attempt_number", 0)),
        "lease_token_sha256": hashlib.sha256(_lease_token(lease).encode("utf-8")).hexdigest(),
        "operation": operation,
        "output_dir": str(workspace),
        "input_evidence": evidence,
        "asr_runtime": dict(asr_runtime or {}),
        "issued_at_ns": issued_at_ns,
    }
    return payload


def _seatbelt_profile(workspace: Path, *, allow_local_model: bool) -> str:
    quoted = json.dumps(str(workspace))
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        "(deny file-write*)",
        f"(allow file-write* (subpath {quoted}))",
    ]
    if allow_local_model:
        port = int(os.environ.get("MAGI_OMLX_LOCAL_PORT", "8080") or "8080")
        if not 1 <= port <= 65535:
            raise ValueError("MAGI_OMLX_LOCAL_PORT is invalid")
        rules.append(f'(allow network-outbound (remote ip "localhost:{port}"))')
    return "".join(rules)


def _transcription_policy() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    try:
        manifest = json.loads(TRANSCRIPTION_BACKENDS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"transcription backend manifest is unavailable: {exc}") from exc
    policy = manifest.get("policy") if isinstance(manifest, Mapping) else None
    rows = manifest.get("backends") if isinstance(manifest, Mapping) else None
    if not isinstance(policy, Mapping) or not isinstance(rows, list):
        raise ValueError("transcription backend manifest is invalid")
    backends = {
        str(row.get("id")): dict(row) for row in rows if isinstance(row, Mapping)
    }
    return dict(policy), backends


def _required_env(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required for forensic dual ASR")
    return value


def _expected_sha256(name: str) -> str:
    value = _required_env(name).lower()
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _absolute_file(name: str, *, executable: bool = False) -> Path:
    path = Path(_required_env(name)).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f"{name} must reference an existing absolute file")
    if executable and not os.access(path, os.X_OK):
        raise ValueError(f"{name} must reference an executable file")
    return path.resolve()


def _content_evidence(
    path: Path,
    *,
    expected_env: str,
) -> dict[str, Any]:
    expected = _expected_sha256(expected_env)
    actual = _sha256_file(path)
    if actual != expected:
        raise ValueError(f"{expected_env} mismatch for {path}")
    return {"path": str(path), "size": path.stat().st_size, "sha256": actual}


def _validated_dual_asr_runtime() -> tuple[dict[str, str], dict[str, Any]]:
    """Content-bind the two explicitly configured offline ASR installations."""

    policy, backends = _transcription_policy()
    primary_id = _required_env("MAGI_FORENSIC_PRIMARY_ASR_BACKEND").lower()
    secondary_id = _required_env("MAGI_FORENSIC_SECONDARY_ASR_BACKEND").lower()
    if primary_id != policy.get("primary_backend_id") or secondary_id != policy.get(
        "secondary_backend_id"
    ):
        raise ValueError("forensic ASR backend pair does not match the release manifest")
    if primary_id == secondary_id:
        raise ValueError("forensic ASR backends must be different")
    primary_row = backends.get(primary_id) or {}
    secondary_row = backends.get(secondary_id) or {}
    if any(
        not row.get("enabled") or not row.get("selectable")
        for row in (primary_row, secondary_row)
    ):
        raise ValueError("forensic ASR backend is not enabled/selectable")

    allowed_licenses = set(policy.get("allowed_model_license_ids") or [])
    primary_license = _required_env("MAGI_FORENSIC_PRIMARY_MODEL_LICENSE")
    secondary_license = _required_env("MAGI_FORENSIC_SECONDARY_MODEL_LICENSE")
    if primary_license not in allowed_licenses or secondary_license not in allowed_licenses:
        raise ValueError("forensic ASR model license is missing or not approved")

    primary_model = Path(_required_env("MAGI_FORENSIC_PRIMARY_ASR_MODEL")).expanduser()
    mlx_model = Path(_required_env("MAGI_FORENSIC_MLX_MODEL_PATH")).expanduser()
    if (
        not primary_model.is_absolute()
        or not mlx_model.is_absolute()
        or not primary_model.is_dir()
        or primary_model.resolve() != mlx_model.resolve()
    ):
        raise ValueError("primary MLX model must be one explicit preinstalled absolute directory")
    primary_model = primary_model.resolve()
    config_path = (primary_model / "config.json").resolve()
    weight_candidates = [
        path.resolve()
        for path in (primary_model / "weights.safetensors", primary_model / "weights.npz")
        if path.is_file()
    ]
    if not config_path.is_file() or len(weight_candidates) != 1:
        raise ValueError("primary MLX snapshot must contain one weights file and config.json")
    primary_binary = _absolute_file("MAGI_FORENSIC_PRIMARY_BACKEND_BINARY")

    secondary_model = _absolute_file("MAGI_FORENSIC_SECONDARY_ASR_MODEL")
    model_dir = Path(_required_env("MAGI_WHISPER_MODEL_DIR")).expanduser()
    if not model_dir.is_absolute() or not model_dir.is_dir() or model_dir.resolve() not in (
        secondary_model.parent,
        *secondary_model.parents,
    ):
        raise ValueError("secondary Whisper model must reside in the explicit absolute model dir")
    model_dir = model_dir.resolve()
    secondary_binary = _absolute_file("MAGI_WHISPER_BIN", executable=True)
    if primary_model == secondary_model or weight_candidates[0] == secondary_model:
        raise ValueError("forensic ASR models must be different")

    primary = {
        "backend_id": primary_id,
        "model_id": str(primary_model),
        "license_id": primary_license,
        "weights": _content_evidence(
            weight_candidates[0], expected_env="MAGI_FORENSIC_PRIMARY_MODEL_WEIGHTS_SHA256"
        ),
        "config": _content_evidence(
            config_path, expected_env="MAGI_FORENSIC_PRIMARY_MODEL_CONFIG_SHA256"
        ),
        "backend_binary": _content_evidence(
            primary_binary, expected_env="MAGI_FORENSIC_PRIMARY_BACKEND_BINARY_SHA256"
        ),
    }
    secondary = {
        "backend_id": secondary_id,
        "model_id": str(secondary_model),
        "model_dir": str(model_dir),
        "license_id": secondary_license,
        "weights": _content_evidence(
            secondary_model, expected_env="MAGI_FORENSIC_SECONDARY_MODEL_WEIGHTS_SHA256"
        ),
        "config": {"mode": "embedded_in_checkpoint"},
        "backend_binary": _content_evidence(
            secondary_binary, expected_env="MAGI_FORENSIC_SECONDARY_BACKEND_BINARY_SHA256"
        ),
    }
    if primary["weights"]["sha256"] == secondary["weights"]["sha256"]:
        raise ValueError("forensic ASR model weight content must be different")
    evidence = {
        "schema_version": 1,
        "execution": "serialized",
        "maximum_concurrent_heavy_workers": 1,
        "auto_download_allowed": False,
        "primary": primary,
        "secondary": secondary,
    }
    env = {
        "MAGI_FORENSIC_PRIMARY_ASR_BACKEND": primary_id,
        "MAGI_FORENSIC_PRIMARY_ASR_MODEL": str(primary_model),
        "MAGI_FORENSIC_MLX_MODEL_PATH": str(primary_model),
        "MAGI_FORENSIC_PRIMARY_BACKEND_BINARY": str(primary_binary),
        "MAGI_FORENSIC_PRIMARY_MODEL_LICENSE": primary_license,
        "MAGI_FORENSIC_PRIMARY_MODEL_WEIGHTS_SHA256": primary["weights"]["sha256"],
        "MAGI_FORENSIC_PRIMARY_MODEL_CONFIG_SHA256": primary["config"]["sha256"],
        "MAGI_FORENSIC_PRIMARY_BACKEND_BINARY_SHA256": primary["backend_binary"]["sha256"],
        "MAGI_FORENSIC_SECONDARY_ASR_BACKEND": secondary_id,
        "MAGI_FORENSIC_SECONDARY_ASR_MODEL": str(secondary_model),
        "MAGI_WHISPER_MODEL_DIR": str(model_dir),
        "MAGI_WHISPER_BIN": str(secondary_binary),
        "MAGI_FORENSIC_SECONDARY_MODEL_LICENSE": secondary_license,
        "MAGI_FORENSIC_SECONDARY_MODEL_WEIGHTS_SHA256": secondary["weights"]["sha256"],
        "MAGI_FORENSIC_SECONDARY_BACKEND_BINARY_SHA256": secondary["backend_binary"]["sha256"],
        "MAGI_FORENSIC_DUAL_ASR_SERIALIZED": "1",
    }
    return env, evidence


def _validated_local_runtime_environment(
    dual_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Pass only validated local model settings into the clean worker env."""

    output: dict[str, str] = {}
    path_fields = {
        "MAGI_FORENSIC_MLX_MODEL_PATH": "exists",
        "MAGI_WHISPER_MODEL_DIR": "dir",
        "MAGI_WHISPER_BIN": "executable",
    }
    for name, kind in path_fields.items():
        raw = str(os.environ.get(name) or "").strip()
        if not raw:
            continue
        path = Path(raw).expanduser()
        valid = path.exists()
        if kind == "dir":
            valid = path.is_dir()
        elif kind == "executable":
            valid = path.is_file() and os.access(path, os.X_OK)
        if not path.is_absolute() or not valid:
            raise ValueError(f"{name} must reference a preinstalled local artifact")
        output[name] = str(path.resolve())
    for role in ("PRIMARY", "SECONDARY"):
        backend_name = f"MAGI_FORENSIC_{role}_ASR_BACKEND"
        model_name = f"MAGI_FORENSIC_{role}_ASR_MODEL"
        backend = str(os.environ.get(backend_name) or "").strip().lower()
        model = str(os.environ.get(model_name) or "").strip()
        if backend:
            if backend not in {"mlx_whisper", "whisper_cli"}:
                raise ValueError(f"{backend_name} is not an approved local backend")
            output[backend_name] = backend
        if model:
            candidate = Path(model).expanduser()
            if candidate.is_absolute() and not candidate.exists():
                raise ValueError(f"{model_name} local artifact is missing")
            if backend == "mlx_whisper" and not candidate.is_absolute():
                raise ValueError(f"{model_name} must be an absolute preinstalled MLX path")
            if backend == "whisper_cli" and not candidate.is_absolute():
                model_dir = Path(output.get("MAGI_WHISPER_MODEL_DIR", ""))
                if not model_dir.is_dir() or not (model_dir / f"{candidate.name}.pt").is_file():
                    raise ValueError(f"{model_name} is not installed in MAGI_WHISPER_MODEL_DIR")
            output[model_name] = str(candidate.resolve()) if candidate.is_absolute() else candidate.name
    local_url = str(os.environ.get("INFERENCE_LOCAL_OLLAMA_BASE") or "").strip()
    if local_url:
        parsed = urlparse(local_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("INFERENCE_LOCAL_OLLAMA_BASE must be loopback-only")
        output["INFERENCE_LOCAL_OLLAMA_BASE"] = local_url.rstrip("/")
    for name in ("INFERENCE_LOCAL_CHAT_MODELS", "INFERENCE_LOCAL_VISION_MODELS"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            if any(marker in value.lower() for marker in ("openai", "codex", "anthropic")):
                raise ValueError(f"{name} contains a forbidden external model")
            output[name] = value
    output.update({str(key): str(value) for key, value in (dual_env or {}).items()})
    return output


def _worker_environment(
    workspace: Path,
    *,
    dual_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env = {
        "HOME": str(workspace / "home"),
        "TMPDIR": str(workspace / "tmp"),
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "MAGI_WHISPER_OFFLINE_ONLY": "1",
        "INFERENCE_FORCE_LOCAL": "1",
        "MELCHIOR_FORCE_LOCAL": "1",
        "MAGI_AVOID_DISTRIBUTED": "1",
        "MAGI_CODEX_CHAT_FALLBACK": "0",
        "MAGI_CODEX_DIRECT_VISION_ENABLE": "0",
        "NVIDIA_NIM_ENABLE": "0",
        "BALTHASAR_REMOTE_ENABLED": "0",
        "NO_PROXY": "*",
    }
    env.update(_validated_local_runtime_environment(dual_env))
    return env


def build_worker_spec(job: Any, lease: Any) -> WorkerSpec:
    """Build the fenced V3 transcription worker command for one verification job."""

    operation = str(job.operation or "").strip().lower().replace("_", "-")
    dual_env: dict[str, str] = {}
    dual_evidence: dict[str, Any] = {}
    if operation == "autonomous":
        dual_env, dual_evidence = _validated_dual_asr_runtime()
    payload = _task_payload(job, lease, asr_runtime=dual_evidence)
    claim = job.resource_claim if isinstance(job.resource_claim, Mapping) else {}
    memory, metal, cpu, disk, network = _RESOURCE_MINIMUMS[payload["operation"]]
    if float(claim.get("memory_mb", 0)) < memory:
        raise ValueError("forensic transcript memory claim is below the operation minimum")
    if float(claim.get("metal_mb", 0)) < metal:
        raise ValueError("forensic transcript Metal claim is below the operation minimum")
    if int(claim.get("cpu_percent", 0)) < cpu:
        raise ValueError("forensic transcript CPU claim is below the operation minimum")
    if str(claim.get("disk_io", "none")) != disk:
        raise ValueError("forensic transcript disk claim does not match the operation")
    if str(claim.get("network", "none")) != network:
        raise ValueError("forensic transcript network claim does not match the sandbox contract")
    if int(claim.get("browser_tokens", 0)) != 0:
        raise ValueError("forensic transcript worker must not reserve browser tokens")
    workspace = Path(payload["output_dir"])
    sandbox_exec = shutil.which("sandbox-exec")
    if sys.platform == "darwin" and not sandbox_exec:
        raise ValueError("macOS sandbox-exec is required for forensic transcript workers")
    worker_env = _worker_environment(workspace, dual_env=dual_env)
    if workspace.exists():
        raise ValueError("forensic transcript lease workspace already exists")
    (workspace / "home").mkdir(parents=True, exist_ok=False)
    (workspace / "tmp").mkdir(exist_ok=False)
    command = (
        sys.executable,
        str(ACTION),
        "--task",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )
    if sandbox_exec:
        command = (
            sandbox_exec,
            "-p",
            _seatbelt_profile(workspace, allow_local_model=network == "light"),
            *command,
        )
    return WorkerSpec(
        job_id=job.job_id,
        worker_class="transcription",
        argv=command,
        cwd=ROOT,
        estimated_footprint_mb=memory,
        estimated_metal_mb=metal,
        cpu_percent=cpu,
        disk_io=disk,
        nas_io=str(claim.get("nas_io", "none")),
        network=network,
        browser_tokens=0,
        priority_class=job.priority_class,
        timeout_sec=float(job.timeout_sec),
        attempt_number=lease.attempt_number,
        lease_token=_lease_token(lease),
        env=worker_env,
        inherit_environment=False,
    )


def verify_completion(job: Any, lease: Any, result: WorkerResult) -> VerifiedCompletion:
    """Accept completion only when the expected reports prove the requested checks ran."""

    operation = str(job.operation or "").strip().lower().replace("_", "-")
    dual_evidence: dict[str, Any] = {}
    if operation == "autonomous":
        _dual_env, dual_evidence = _validated_dual_asr_runtime()
    payload = _task_payload(job, lease, asr_runtime=dual_evidence)
    output_dir = Path(str(payload["output_dir"])).expanduser().resolve()
    operation = payload["operation"]
    report_names = {
        "inspect": ("inspection.json",),
        "audit": ("audit.json",),
        "validate-docx": ("docx-validation.json",),
        "full-check": ("inspection.json", "audit.json", "docx-validation.json", "full-check.json"),
        "autonomous-plan": ("autonomous-plan.json",),
        "autonomous": ("autonomous.json", "audit.json", "docx-validation.json"),
    }[operation]
    reports: list[tuple[Path, dict[str, Any]]] = []
    for name in report_names:
        path = output_dir / name
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return VerifiedCompletion(
                target=JobStatus.FAILED,
                business_completed=False,
                error={"code": "forensic_report_missing", "message": f"{path}: {exc}"},
            )
        reports.append((path, report))

    binding_path = output_dir / COMPLETION_BINDING
    try:
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return VerifiedCompletion(
            target=JobStatus.FAILED,
            business_completed=False,
            error={"code": "forensic_binding_missing", "message": str(exc)},
        )
    expected = payload["_v3_contract"]
    bound_contract = binding.get("contract") if isinstance(binding, Mapping) else None
    stable_fields = (
        "schema_version",
        "job_id",
        "attempt_number",
        "lease_token_sha256",
        "operation",
        "output_dir",
        "input_evidence",
        "asr_runtime",
    )
    if not isinstance(bound_contract, Mapping) or any(
        bound_contract.get(field) != expected.get(field) for field in stable_fields
    ):
        return VerifiedCompletion(
            target=JobStatus.FAILED,
            business_completed=False,
            error={"code": "forensic_binding_mismatch"},
        )
    completed_at_ns = int(binding.get("completed_at_ns", 0) or 0)
    issued_at_ns = int(bound_contract.get("issued_at_ns", 0) or 0)
    now_ns = time.time_ns()
    duration_ns = int(max(0.0, float(getattr(result, "duration_sec", 0) or 0)) * 1_000_000_000)
    if not (issued_at_ns <= completed_at_ns <= now_ns + 5_000_000_000) or issued_at_ns < (
        now_ns - duration_ns - 60_000_000_000
    ):
        return VerifiedCompletion(
            target=JobStatus.FAILED,
            business_completed=False,
            error={"code": "forensic_binding_stale"},
        )
    report_hashes = binding.get("report_sha256") or {}
    if any(report_hashes.get(path.name) != _sha256_file(path) for path, _ in reports):
        return VerifiedCompletion(
            target=JobStatus.FAILED,
            business_completed=False,
            error={"code": "forensic_report_hash_mismatch"},
        )

    failed_reason = ""
    if result.returncode != 0 or result.timed_out or result.killed:
        failed_reason = f"worker exited with returncode={result.returncode}"
    audit = next((body for path, body in reports if path.name == "audit.json"), None)
    validation = next((body for path, body in reports if path.name == "docx-validation.json"), None)
    full_check = next((body for path, body in reports if path.name == "full-check.json"), None)
    autonomous = next((body for path, body in reports if path.name == "autonomous.json"), None)
    if audit is not None and not audit.get("passed_deterministic_gates"):
        failed_reason = "forensic transcript deterministic gates did not pass"
    if validation is not None and not validation.get("passed"):
        failed_reason = "forensic transcript DOCX validation did not pass"
    if full_check is not None and not full_check.get("passed"):
        failed_reason = "forensic transcript full-check did not pass"
    if autonomous is not None and not autonomous.get("passed"):
        failed_reason = "forensic transcript autonomous video review did not pass"
    if autonomous is not None and not autonomous.get("court_grade_contract_satisfied"):
        failed_reason = "forensic transcript court-grade contract was not satisfied"
    if autonomous is not None:
        expected_asr = expected.get("asr_runtime") or {}
        reported_asr = autonomous.get("asr_evidence") or {}
        for role, ordinal in (("primary", 1), ("secondary", 2)):
            expected_role = expected_asr.get(role) if isinstance(expected_asr, Mapping) else None
            reported_role = reported_asr.get(role) if isinstance(reported_asr, Mapping) else None
            provenance = (
                reported_role.get("provenance")
                if isinstance(reported_role, Mapping)
                else None
            )
            model_evidence = (
                provenance.get("model_evidence") if isinstance(provenance, Mapping) else None
            )
            if (
                not isinstance(expected_role, Mapping)
                or not isinstance(model_evidence, Mapping)
                or dict(model_evidence) != dict(expected_role)
                or provenance.get("execution_ordinal") != ordinal
                or provenance.get("execution_mode") != "serialized"
            ):
                failed_reason = f"forensic transcript {role} ASR provenance mismatch"
                break
        if autonomous.get("dual_asr_execution") != "serialized":
            failed_reason = "forensic transcript dual ASR was not serialized"
    if failed_reason:
        return VerifiedCompletion(
            target=JobStatus.FAILED,
            business_completed=False,
            error={"code": "forensic_verification_failed", "message": failed_reason},
            artifacts=tuple(
                {"kind": "forensic_transcript_report", "uri": str(path)} for path, _ in reports
            ),
        )

    artifacts = [
        {"kind": "forensic_transcript_report", "uri": str(path)} for path, _ in reports
    ]
    autonomous_output = str((autonomous or {}).get("output_docx") or "")
    transcript = Path(autonomous_output or str(payload.get("transcript") or "")).expanduser().resolve()
    if autonomous is not None:
        artifact_hashes = binding.get("artifact_sha256") or {}
        if (
            output_dir not in transcript.parents
            or not transcript.is_file()
            or artifact_hashes.get(transcript.name) != _sha256_file(transcript)
        ):
            return VerifiedCompletion(
                target=JobStatus.FAILED,
                business_completed=False,
                error={"code": "forensic_output_artifact_mismatch"},
            )
    if transcript.is_file():
        artifacts.insert(0, {"kind": "forensic_transcript_docx", "uri": str(transcript)})
    return VerifiedCompletion(
        target=JobStatus.SUCCEEDED,
        business_completed=True,
        result={
            "operation": operation,
            "manual_second_pass_required": operation in {"audit", "full-check"},
            "human_final_confirmation_required": operation == "autonomous",
        },
        artifacts=tuple(artifacts),
    )
