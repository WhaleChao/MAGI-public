"""Fail-closed controller for MAGI's source evolution loop.

The controller turns de-identified health or quality signals into durable
repair proposals, stages an externally generated unified diff in an isolated
Git worktree, and certifies that candidate with an allow-listed test suite.

It deliberately has no deploy operation.  A certified candidate is only
``ready_for_human_review``; the existing immutable release and atomic cutover
pipeline remains the sole production path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
MAX_PATCH_BYTES = 160_000
MAX_PATCH_FILES = 12
MAX_TEST_SELECTORS = 16
_SECRET_WORDS = re.compile(
    r"(?i)(api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|password)\s*[:=]\s*['\"][^'\"]+"
)
_DANGEROUS_PATCH_TEXT = re.compile(
    r"(?i)(shell\s*=\s*True|os\.system\s*\(|subprocess\.(?:call|run|Popen)\s*\([^\n]*['\"](?:rm|sudo|launchctl)\b)"
)
_FORBIDDEN_PREFIXES = (
    ".git/",
    ".agent/",
    ".runtime/",
    "runtime/",
    "static/exports/",
    "secrets/",
    "credentials/",
    "venv/",
)
_FORBIDDEN_NAMES = {".env", "active-release.json", "external-env.json"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _digest(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:length]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ComponentRule:
    component: str
    keywords: tuple[str, ...]
    source_prefixes: tuple[str, ...]
    tests: tuple[str, ...]
    base_risk: str = "medium"


COMPONENT_RULES: tuple[ComponentRule, ...] = (
    ComponentRule(
        "security_privacy",
        ("security", "privacy", "pii", "csrf", "auth", "credential", "個資", "權限", "登入"),
        ("api/auth", "api/security", "api/tools", "skills/iron_dome", "magi_v3"),
        ("tests/test_message_intent_boundaries.py", "tests/v3/test_observability.py"),
        "critical",
    ),
    ComponentRule(
        "conversation_routing",
        ("intent", "routing", "clarification", "chat", "tool", "意圖", "追問", "對話", "工具"),
        ("api/routing", "api/pipelines", "api/agentic", "api/tools", "skills/engine"),
        ("tests/test_clarification_gate.py", "tests/test_intent_tool_adversarial_rc556.py"),
        "high",
    ),
    ComponentRule(
        "legal_aid",
        ("laf", "legal_aid", "法扶", "附件", "報結", "派案"),
        ("casper_ecosystem/law_firm_orchestrators", "skills"),
        ("tests/test_laf_portal_retry_heartbeat.py",),
        "high",
    ),
    ComponentRule(
        "judicial_download",
        ("judicial", "file_review", "court_portal", "閱卷", "卷宗", "法院入口", "繳費"),
        ("casper_ecosystem/law_firm_orchestrators", "skills/file-review-orchestrator", "skills"),
        ("tests/test_transcript_portal_empty_failclosed_rc223.py",),
        "high",
    ),
    ComponentRule(
        "transcript",
        ("transcript", "筆錄", "逐字稿", "錄音"),
        ("casper_ecosystem/law_firm_orchestrators", "skills/transcript-downloader", "skills"),
        ("tests/test_transcript_partial_retry_rc239.py", "tests/test_transcript_filename_repair.py"),
        "high",
    ),
    ComponentRule(
        "calendar_todos",
        ("calendar", "todo", "deadline", "日曆", "行程", "庭期", "期限", "待辦"),
        ("api/blueprints", "scripts", "skills/osc-orchestrator", "skills"),
        ("tests/test_reconcile_overdue_todos.py",),
        "high",
    ),
    ComponentRule(
        "document_quality",
        ("summary", "draft", "pdf", "bookmark", "translation", "摘要", "書狀", "翻譯", "書籤", "命名"),
        ("api/blueprints", "skills", "scripts"),
        ("tests/test_judgment_summary_quality_rc170.py", "tests/test_ai_draft_dispatch_quality.py"),
    ),
    ComponentRule(
        "storage_sync",
        ("drive", "nas", "smb", "storage", "sync", "同步", "網路硬碟", "路徑"),
        ("scripts", "skills", "magi_v3"),
        ("tests/test_drive_case_sync_hash_timeout.py",),
        "high",
    ),
    ComponentRule(
        "model_runtime",
        ("model", "omlx", "nvidia", "inference", "模型", "推理"),
        ("api", "magi_v3", "scripts", "skills/bridge"),
        ("tests/test_install_omlx_text.py",),
        "critical",
    ),
    ComponentRule(
        "web_mobile",
        ("web", "mobile", "osc", "menubar", "網頁", "手機", "介面"),
        ("api/blueprints", "static", "templates"),
        ("tests/test_osc_web_smoke.py", "tests/test_mobile_auth_routes.py"),
    ),
    ComponentRule(
        "health_scheduling",
        ("cron", "health", "doctor", "scheduler", "排程", "健康", "紅燈", "自我修復"),
        ("magi_v3", "scripts/ops", "skills/ops", "skills/magi-self-repair"),
        ("tests/v3/test_function_health_operational_semantics.py", "tests/v3/test_magi_doctor_v3_runtime.py"),
        "high",
    ),
)

DEFAULT_RULE = ComponentRule(
    "cross_module",
    (),
    ("api", "magi_v3", "skills", "scripts", "casper_ecosystem"),
    ("tests/test_message_intent_boundaries.py",),
    "high",
)


def _safe_signal(signal: Mapping[str, Any]) -> dict[str, Any]:
    """Return only aggregate, privacy-safe fields from a diagnostic signal."""
    category = str(signal.get("category") or "unknown")[:80]
    source = str(signal.get("source") or "unknown")[:80]
    severity = str(signal.get("severity") or "warning").lower()[:16]
    status = str(signal.get("status") or "open").lower()[:32]
    reason_code = str(signal.get("reason_code") or "").strip()[:80]
    issue_id = str(signal.get("id") or "")
    # Never persist summary, recommendation, evidence, paths, user content or
    # exception strings in the evolution ledger.
    identity = {
        "category": category,
        "source": source,
        "severity": severity,
        "reason_code": reason_code,
        "issue_id_hash": _digest(issue_id, 20) if issue_id else "",
    }
    return {
        "signal_ref": f"sig-{_digest(_canonical_json(identity), 20)}",
        "category": re.sub(r"[^A-Za-z0-9_.:-]", "_", category),
        "source": re.sub(r"[^A-Za-z0-9_.:-]", "_", source),
        "severity": severity if severity in {"info", "warning", "error", "critical"} else "warning",
        "status": status,
        "reason_code": re.sub(r"[^A-Za-z0-9_.:-]", "_", reason_code),
        "issue_id_hash": _digest(issue_id, 20) if issue_id else "",
    }


def classify_component(signal: Mapping[str, Any]) -> ComponentRule:
    primary = " ".join(
        str(signal.get(key) or "") for key in ("summary", "recommendation", "reason_code")
    ).lower()
    secondary = " ".join(
        str(signal.get(key) or "") for key in ("id", "category", "source")
    ).lower()
    scored = [
        (
            3 * sum(1 for keyword in rule.keywords if keyword.lower() in primary)
            + sum(1 for keyword in rule.keywords if keyword.lower() in secondary),
            index,
            rule,
        )
        for index, rule in enumerate(COMPONENT_RULES)
    ]
    score, _index, rule = max(scored, key=lambda item: (item[0], -item[1]))
    return rule if score else DEFAULT_RULE


def _risk_for(rule: ComponentRule, signal: Mapping[str, Any]) -> str:
    severity = str(signal.get("severity") or "warning").lower()
    text = " ".join(str(signal.get(key) or "") for key in ("category", "source", "summary")).lower()
    if any(word in text for word in ("database schema", "migration", "credential", "auth", "pii", "個資", "刪除")):
        return "critical"
    if severity in {"critical", "error"} and rule.base_risk in {"high", "critical"}:
        return "critical" if rule.base_risk == "critical" else "high"
    return rule.base_risk


def build_structure_inventory(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    components: list[dict[str, Any]] = []
    for rule in (*COMPONENT_RULES, DEFAULT_RULE):
        files: list[str] = []
        for prefix in rule.source_prefixes:
            target = root / prefix
            if target.is_file() and not target.is_symlink():
                files.append(prefix)
            elif target.is_dir() and not target.is_symlink():
                for path in target.rglob("*"):
                    if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts:
                        files.append(path.relative_to(root).as_posix())
        unique = sorted(set(files))
        components.append(
            {
                "component": rule.component,
                "source_prefixes": list(rule.source_prefixes),
                "acceptance_tests": list(rule.tests),
                "file_count": len(unique),
                "structure_digest": hashlib.sha256("\n".join(unique).encode("utf-8")).hexdigest(),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso_now(),
        "components": components,
        "component_count": len(components),
    }


def build_proposal(signal: Mapping[str, Any], *, release_id: str, root: Path) -> dict[str, Any]:
    rule = classify_component(signal)
    safe = _safe_signal(signal)
    risk = _risk_for(rule, signal)
    identity = _canonical_json({"release_id": release_id, "signal": safe, "component": rule.component})
    proposal_id = f"ce-{_digest(identity, 20)}"
    return {
        "schema_version": SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "created_at": _iso_now(),
        "release_id": str(release_id or "unknown")[:120],
        "component": rule.component,
        "risk": risk,
        "signal": safe,
        "structure_scope": {
            "source_prefixes": list(rule.source_prefixes),
            "acceptance_tests": [test for test in rule.tests if (root / test).exists()],
        },
        "status": "planned",
        "candidate_only": True,
        "auto_deploy": False,
        "requires_human_before_deploy": True,
        "next_action": "generate_deidentified_patch_then_stage_in_isolated_worktree",
    }


class EvolutionStore:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS evolution_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    release_id TEXT NOT NULL,
                    component TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    status TEXT NOT NULL,
                    proposal_json TEXT NOT NULL,
                    certification_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def upsert(self, proposal: Mapping[str, Any]) -> dict[str, Any]:
        proposal_id = str(proposal.get("proposal_id") or "")
        if not re.fullmatch(r"ce-[a-f0-9]{20}", proposal_id):
            raise ValueError("invalid proposal id")
        now = _iso_now()
        payload = dict(proposal)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT status, certification_json, created_at FROM evolution_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if existing:
                payload["status"] = str(existing["status"])
                created_at = str(existing["created_at"])
                certification = str(existing["certification_json"] or "{}")
            else:
                created_at = str(payload.get("created_at") or now)
                certification = "{}"
            connection.execute(
                """
                INSERT INTO evolution_proposals
                  (proposal_id, release_id, component, risk, status, proposal_json,
                   certification_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(proposal_id) DO UPDATE SET
                  proposal_json=excluded.proposal_json, updated_at=excluded.updated_at
                """,
                (
                    proposal_id,
                    str(payload.get("release_id") or "unknown"),
                    str(payload.get("component") or "cross_module"),
                    str(payload.get("risk") or "high"),
                    str(payload.get("status") or "planned"),
                    _canonical_json(payload),
                    certification,
                    created_at,
                    now,
                ),
            )
        return self.get(proposal_id)

    def get(self, proposal_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM evolution_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        if row is None:
            raise KeyError("proposal not found")
        proposal = json.loads(str(row["proposal_json"]))
        proposal["status"] = str(row["status"])
        proposal["certification"] = json.loads(str(row["certification_json"] or "{}"))
        return proposal

    def transition(self, proposal_id: str, *, status: str, certification: Mapping[str, Any] | None = None) -> dict[str, Any]:
        allowed = {"planned", "staged", "verification_failed", "ready_for_human_review", "cancelled"}
        if status not in allowed:
            raise ValueError("invalid evolution status")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM evolution_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise KeyError("proposal not found")
            current = str(row["status"])
            transitions = {
                "planned": {"staged", "cancelled"},
                "staged": {"verification_failed", "ready_for_human_review", "cancelled"},
                "verification_failed": {"staged", "cancelled"},
                "ready_for_human_review": {"cancelled"},
                "cancelled": set(),
            }
            if status != current and status not in transitions[current]:
                raise ValueError(f"invalid transition: {current}->{status}")
            connection.execute(
                "UPDATE evolution_proposals SET status=?, certification_json=?, updated_at=? WHERE proposal_id=?",
                (status, _canonical_json(dict(certification or {})), _iso_now(), proposal_id),
            )
        return self.get(proposal_id)

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(500, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT proposal_id FROM evolution_proposals ORDER BY updated_at DESC LIMIT ?", (bounded,)
            ).fetchall()
        return [self.get(str(row["proposal_id"])) for row in rows]


def ingest_signals(
    signals: Iterable[Mapping[str, Any]], *, root: Path, release_id: str, store: EvolutionStore
) -> list[dict[str, Any]]:
    proposals: dict[str, dict[str, Any]] = {}
    for signal in signals:
        if str(signal.get("severity") or "").lower() == "info":
            continue
        if signal.get("auto_repair"):
            # Known low-risk actions belong to the guardian, not source evolution.
            continue
        proposal = store.upsert(build_proposal(signal, release_id=release_id, root=root))
        proposals[str(proposal["proposal_id"])] = proposal
    return list(proposals.values())


def ingest_quality_outcomes(
    outcomes: Iterable[Mapping[str, Any]], *, root: Path, release_id: str, store: EvolutionStore
) -> list[dict[str, Any]]:
    """Accept only canonical, de-identified business-quality ledger entries.

    The quality ledger has already removed all raw evidence.  This adapter
    keeps that boundary intact while making unresolved quality work visible to
    controlled source evolution.  It cannot stage, deploy, or invoke tools.
    """
    from .quality_ledger import canonical_quality_signal

    signals: list[dict[str, Any]] = []
    for outcome in outcomes:
        safe = canonical_quality_signal(outcome)
        if safe["state"] in {"resolved", "cancelled"} or safe["actionability"] == "observe":
            continue
        signals.append({
            "id": safe["outcome_id"], "source": "quality_outcome_ledger",
            "category": safe["kind"], "severity": "error" if safe["human_required"] else "warning",
            "status": safe["state"], "reason_code": safe["kind"],
        })
    return ingest_signals(signals, root=root, release_id=release_id, store=store)


def _patch_paths(patch_text: str) -> list[str]:
    paths: list[str] = []
    for line in patch_text.splitlines():
        if not (line.startswith("+++ ") or line.startswith("--- ")):
            continue
        raw = line[4:].split("\t", 1)[0].strip()
        if raw == "/dev/null":
            continue
        if raw.startswith(("a/", "b/")):
            raw = raw[2:]
        paths.append(raw)
    return sorted(set(paths))


def validate_patch(patch_text: str, proposal: Mapping[str, Any]) -> dict[str, Any]:
    encoded = patch_text.encode("utf-8", errors="replace")
    errors: list[str] = []
    if not patch_text.strip() or not patch_text.lstrip().startswith("diff --git "):
        errors.append("not_unified_git_diff")
    if len(encoded) > MAX_PATCH_BYTES:
        errors.append("patch_too_large")
    if "GIT binary patch" in patch_text or "Binary files " in patch_text:
        errors.append("binary_patch_forbidden")
    if _SECRET_WORDS.search(patch_text):
        errors.append("possible_secret_literal")
    if _DANGEROUS_PATCH_TEXT.search(patch_text):
        errors.append("dangerous_process_invocation")
    paths = _patch_paths(patch_text)
    if not paths:
        errors.append("no_changed_paths")
    if len(paths) > MAX_PATCH_FILES:
        errors.append("too_many_changed_files")
    allowed = tuple(str(item).rstrip("/") for item in ((proposal.get("structure_scope") or {}).get("source_prefixes") or ()))
    for raw in paths:
        path = PurePosixPath(raw)
        if path.is_absolute() or ".." in path.parts or raw.startswith("/"):
            errors.append(f"unsafe_path:{raw}")
            continue
        if raw in _FORBIDDEN_NAMES or raw.startswith(_FORBIDDEN_PREFIXES):
            errors.append(f"forbidden_path:{raw}")
            continue
        if not any(raw == prefix or raw.startswith(prefix + "/") for prefix in allowed):
            errors.append(f"outside_component_scope:{raw}")
    return {
        "ok": not errors,
        "errors": sorted(set(errors)),
        "paths": paths,
        "patch_sha256": hashlib.sha256(encoded).hexdigest(),
        "size_bytes": len(encoded),
    }


def _run(
    args: Sequence[str],
    *,
    cwd: Path,
    timeout: int = 120,
    stdin: str | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=str(cwd),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=dict(
            env
            or {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPYCACHEPREFIX": "/dev/null",
            }
        ),
    )


def resource_admission(
    *,
    root: Path,
    min_disk_gb: float = 8.0,
    max_normalized_load: float = 1.5,
    min_memory_free_percent: int = 20,
) -> dict[str, Any]:
    load = float(os.getloadavg()[0])
    cpus = max(1, int(os.cpu_count() or 1))
    normalized = load / cpus
    try:
        disk_gb = shutil.disk_usage(root).free / (1024**3)
    except OSError:
        disk_gb = 0.0
    reasons: list[str] = []
    if normalized > max_normalized_load:
        reasons.append("cpu_pressure_high")
    if disk_gb < min_disk_gb:
        reasons.append("disk_headroom_low")
    free_percent: int | None = None
    memory_pressure = Path("/usr/bin/memory_pressure")
    if memory_pressure.is_file():
        probe = _run([str(memory_pressure), "-Q"], cwd=root, timeout=8)
        match = re.search(r"free percentage:\s*(\d+)%", probe.stdout, re.I)
        if probe.returncode == 0 and match:
            free_percent = int(match.group(1))
    if free_percent is None:
        reasons.append("memory_probe_unavailable")
    elif free_percent < min_memory_free_percent:
        reasons.append("memory_headroom_low")
    return {
        "safe": not reasons,
        "normalized_load": round(normalized, 3),
        "max_normalized_load": max_normalized_load,
        "disk_free_gb": round(disk_gb, 1),
        "min_disk_free_gb": min_disk_gb,
        "memory_free_percent": free_percent,
        "min_memory_free_percent": min_memory_free_percent,
        "reasons": reasons,
    }


def _quoted(path: Path) -> str:
    return json.dumps(str(path.resolve()), ensure_ascii=False)


def _candidate_test_command(
    *,
    candidate: Path,
    python: str,
    tests: Sequence[str],
    sandbox_dir: Path,
    network_probe_port: int,
) -> tuple[list[str], dict[str, Any]]:
    """Build a mandatory network-denied candidate test command.

    macOS uses Seatbelt.  Other hosts must provide an independently attested
    outer isolation layer before this controller may certify a candidate.
    """
    executable = Path("/usr/bin/sandbox-exec")
    if sys.platform != "darwin" or not executable.is_file():
        return [], {"ok": False, "kind": "unavailable", "reason": "candidate_isolation_unavailable"}
    home = Path.home().resolve()
    magi_support = home / "Library" / "Application Support" / "MAGI"
    protected = (
        # Keep all mutable LIVE state, secrets and immutable production source
        # unreadable.  The sibling ``runtimes`` tree is deliberately not
        # denied: it contains the manifest-bound Python interpreter and
        # already-installed pytest needed to start the isolated child.  A
        # blanket deny on ``MAGI`` would make every LIVE candidate fail before
        # the active denial probes can even run.
        magi_support / "runtime",
        magi_support / "releases",
        home / "Library" / "CloudStorage",
        home / "Library" / "Keychains",
        home / "Library" / "Mail",
        home / "Library" / "Messages",
        home / "Library" / "Safari",
        home / "Desktop",
        home / "Documents",
        home / "Downloads",
        home / ".ssh",
        Path("/Volumes"),
        Path("/opt/homebrew/var/mysql"),
    )
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        "(deny file-write*)",
        '(allow file-write* (literal "/dev/null"))',
        f"(allow file-write* (subpath {_quoted(candidate)}))",
    ]
    for path in protected:
        rules.append(f"(deny file-read* (literal {_quoted(path)}))")
        rules.append(f"(deny file-read* (subpath {_quoted(path)}))")
        rules.append(f"(deny file-write* (literal {_quoted(path)}))")
        rules.append(f"(deny file-write* (subpath {_quoted(path)}))")
    profile = "".join(rules)
    live_probe = home / "Library" / "Application Support" / "MAGI" / "runtime" / "active-release.json"
    escape_probe = Path("/private/tmp") / f"magi-evolution-escape-{os.getpid()}"
    probe_code = (
        "import os,socket,sys;"
        "live,escape,port,*tests=sys.argv[1:];"
        "\ntry:\n open(live,'rb').read(1)\nexcept OSError: pass\nelse: raise SystemExit(91);"
        "\ntry:\n open(escape,'w').write('x')\nexcept OSError: pass\nelse:\n os.unlink(escape)\n raise SystemExit(92);"
        "\ns=socket.socket();s.settimeout(2);"
        "\ntry:\n s.connect(('127.0.0.1',int(port)))\nexcept OSError: pass\nelse:\n s.close()\n raise SystemExit(93);"
        "\nimport pytest;raise SystemExit(pytest.main(['-q','-p','no:cacheprovider',*tests]))"
    )
    command = [
        str(executable),
        "-p",
        profile,
        "--",
        python,
        "-c",
        probe_code,
        str(live_probe),
        str(escape_probe),
        str(network_probe_port),
        *tests,
    ]
    return command, {
        "ok": True,
        "kind": "macos_seatbelt",
        "network_denied": True,
        "live_state_read_denied": True,
        "writes_confined_to_candidate": True,
        "active_network_denial_probe": True,
        "active_live_read_denial_probe": live_probe.is_file(),
        "active_external_write_denial_probe": True,
        "sandbox_dir": sandbox_dir.relative_to(candidate).as_posix(),
    }


def _pytest_site_root(python: str, *, cwd: Path) -> Path | None:
    """Resolve pytest's already-installed package root before HOME isolation."""
    try:
        resolved_python = Path(python).expanduser().resolve(strict=True)
        current_python = Path(sys.executable).resolve(strict=True)
    except OSError:
        return None
    configured = os.environ.get("MAGI_CONTROLLED_EVOLUTION_PYTHON", "").strip()
    allowed = {current_python}
    if configured:
        try:
            allowed.add(Path(configured).expanduser().resolve(strict=True))
        except OSError:
            return None
    if resolved_python not in allowed:
        return None
    probe = _run(
        [
            str(resolved_python),
            "-c",
            "import pathlib,pytest; print(pathlib.Path(pytest.__file__).resolve().parents[1])",
        ],
        cwd=cwd,
        timeout=15,
        env=os.environ,
    )
    if probe.returncode != 0 or len(probe.stdout.splitlines()) != 1:
        return None
    try:
        site_root = Path(probe.stdout.strip()).resolve(strict=True)
    except OSError:
        return None
    if not site_root.is_dir() or site_root.is_symlink() or "site-packages" not in site_root.parts:
        return None
    return site_root


def _pytest_outcome(text: str) -> dict[str, Any]:
    """Extract aggregate pytest counts without retaining test output."""
    outcome: dict[str, Any] = {}
    for key in ("passed", "failed", "error", "errors", "skipped", "xfailed", "xpassed"):
        match = re.search(rf"\b(\d+)\s+{key}\b", text, re.I)
        if match:
            normalized = "errors" if key in {"error", "errors"} else key
            outcome[normalized] = int(match.group(1))
    if "Operation not permitted" in text or "sandbox_apply" in text:
        outcome["isolation_denial_observed"] = True
    if "ModuleNotFoundError" in text or "ImportError" in text:
        outcome["import_error_observed"] = True
    return outcome


def _open_network_probe_listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    return listener


def stage_candidate(
    *,
    proposal: Mapping[str, Any],
    store: EvolutionStore,
    source_root: Path,
    workspace_root: Path,
    patch_text: str,
    commit: str = "HEAD",
) -> dict[str, Any]:
    validation = validate_patch(patch_text, proposal)
    if not validation["ok"]:
        return {"ok": False, "status": "blocked", "validation": validation}
    source_root = source_root.expanduser().resolve()
    workspace_root = workspace_root.expanduser().resolve()
    admission = resource_admission(root=workspace_root.parent if workspace_root.parent.exists() else source_root)
    if not admission["safe"]:
        return {"ok": False, "status": "deferred", "reason": "resource_guard", "resources": admission}
    probe = _run(["git", "rev-parse", "--is-inside-work-tree"], cwd=source_root)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return {"ok": False, "status": "blocked", "reason": "source_not_versioned"}
    proposal_id = str(proposal.get("proposal_id") or "")
    candidate = (workspace_root / f"{proposal_id}-{validation['patch_sha256'][:12]}").resolve()
    if candidate.parent != workspace_root:
        return {"ok": False, "status": "blocked", "reason": "unsafe_candidate_path"}
    workspace_root.mkdir(parents=True, exist_ok=True)
    if candidate.exists():
        return {"ok": False, "status": "blocked", "reason": "candidate_already_exists"}
    clean = _run(["git", "diff", "--quiet"], cwd=source_root)
    if clean.returncode != 0:
        return {"ok": False, "status": "blocked", "reason": "source_worktree_not_clean"}
    precheck = _run(["git", "apply", "--check", "-"], cwd=source_root, stdin=patch_text)
    if precheck.returncode != 0:
        return {"ok": False, "status": "blocked", "reason": "patch_check_failed", "detail_hash": _digest(precheck.stderr)}
    add = _run(["git", "worktree", "add", "--detach", str(candidate), commit], cwd=source_root, timeout=180)
    if add.returncode != 0:
        return {"ok": False, "status": "blocked", "reason": "worktree_create_failed", "detail_hash": _digest(add.stderr)}
    check = _run(["git", "apply", "--check", "-"], cwd=candidate, stdin=patch_text)
    if check.returncode != 0:
        return {"ok": False, "status": "blocked", "reason": "patch_check_failed", "detail_hash": _digest(check.stderr)}
    apply_result = _run(["git", "apply", "-"], cwd=candidate, stdin=patch_text)
    if apply_result.returncode != 0:
        return {"ok": False, "status": "blocked", "reason": "patch_apply_failed", "detail_hash": _digest(apply_result.stderr)}
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "source_commit": commit,
        "patch_sha256": validation["patch_sha256"],
        "paths": validation["paths"],
        "created_at": _iso_now(),
        "live_mutation_allowed": False,
        "deploy_operation_available": False,
    }
    marker = candidate / ".magi-controlled-evolution.json"
    marker.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    store.transition(proposal_id, status="staged", certification={"candidate_marker_sha256": hashlib.sha256(marker.read_bytes()).hexdigest()})
    return {"ok": True, "status": "staged", "candidate": str(candidate), "metadata": metadata, "resources": admission}


def verify_candidate(
    *, proposal: Mapping[str, Any], store: EvolutionStore, candidate: Path, python: str | None = None, timeout: int = 900
) -> dict[str, Any]:
    candidate = candidate.expanduser().resolve()
    marker = candidate / ".magi-controlled-evolution.json"
    try:
        metadata = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"ok": False, "status": "blocked", "reason": "candidate_marker_missing_or_invalid"}
    if metadata.get("proposal_id") != proposal.get("proposal_id"):
        return {"ok": False, "status": "blocked", "reason": "proposal_binding_mismatch"}
    diff = _run(["git", "diff", "--binary", "--no-ext-diff"], cwd=candidate)
    if diff.returncode != 0:
        return {"ok": False, "status": "blocked", "reason": "candidate_diff_unavailable"}
    validation = validate_patch(diff.stdout, proposal)
    if not validation["ok"] or validation["patch_sha256"] != metadata.get("patch_sha256"):
        return {"ok": False, "status": "blocked", "reason": "candidate_integrity_mismatch", "validation": validation}
    diff_check = _run(["git", "diff", "--check"], cwd=candidate)
    tests = list((proposal.get("structure_scope") or {}).get("acceptance_tests") or ())[:MAX_TEST_SELECTORS]
    if not tests:
        certification = {
            "verified_at": _iso_now(),
            "ok": False,
            "reason": "no_acceptance_tests",
            "patch_sha256": validation["patch_sha256"],
        }
        store.transition(str(proposal["proposal_id"]), status="verification_failed", certification=certification)
        return {"ok": False, "status": "verification_failed", "certification": certification}
    executable = python or sys.executable
    pytest_site_root = _pytest_site_root(executable, cwd=candidate)
    if pytest_site_root is None:
        certification = {
            "verified_at": _iso_now(),
            "ok": False,
            "reason": "trusted_pytest_runtime_unavailable",
            "patch_sha256": validation["patch_sha256"],
        }
        store.transition(str(proposal["proposal_id"]), status="verification_failed", certification=certification)
        return {"ok": False, "status": "verification_failed", "certification": certification}
    sandbox_dir = candidate / ".magi-evolution-sandbox"
    sandbox_dir.mkdir(mode=0o700)
    listener: socket.socket | None = None
    try:
        listener = _open_network_probe_listener()
        network_probe_port = int(listener.getsockname()[1])
        command, isolation = _candidate_test_command(
            candidate=candidate,
            python=executable,
            tests=tests,
            sandbox_dir=sandbox_dir,
            network_probe_port=network_probe_port,
        )
    except OSError:
        if listener is not None:
            listener.close()
        certification = {
            "verified_at": _iso_now(),
            "ok": False,
            "reason": "isolation_probe_listener_unavailable",
            "patch_sha256": validation["patch_sha256"],
        }
        store.transition(str(proposal["proposal_id"]), status="verification_failed", certification=certification)
        return {"ok": False, "status": "verification_failed", "certification": certification}
    if not isolation.get("ok"):
        certification = {
            "verified_at": _iso_now(),
            "ok": False,
            "reason": isolation.get("reason"),
            "isolation": isolation,
            "patch_sha256": validation["patch_sha256"],
        }
        store.transition(str(proposal["proposal_id"]), status="verification_failed", certification=certification)
        return {"ok": False, "status": "verification_failed", "certification": certification}
    candidate_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(sandbox_dir),
        "TMPDIR": str(sandbox_dir),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": "/dev/null",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": str(pytest_site_root),
        "MAGI_ALLOW_INTERNET": "0",
        "MAGI_ENABLE_LIVE_TESTS": "0",
        "MAGI_V3_EXTERNAL_WRITES_ENABLED": "0",
        "MAGI_V3_NOTIFICATIONS_ENABLED": "0",
        "MAGI_V3_SCHEDULER_ENABLED": "0",
    }
    try:
        result = _run(
            command,
            cwd=candidate,
            timeout=max(60, min(3600, int(timeout))),
            env=candidate_env,
        )
    finally:
        if listener is not None:
            listener.close()
    post_diff = _run(["git", "diff", "--binary", "--no-ext-diff"], cwd=candidate)
    post_validation = validate_patch(post_diff.stdout, proposal) if post_diff.returncode == 0 else {"ok": False}
    post_integrity_ok = bool(
        post_validation.get("ok")
        and post_validation.get("patch_sha256") == metadata.get("patch_sha256")
    )
    active_isolation_probes_ok = result.returncode not in {91, 92, 93}
    isolation["active_probes_ok"] = active_isolation_probes_ok
    ok = (
        diff_check.returncode == 0
        and result.returncode == 0
        and post_integrity_ok
        and active_isolation_probes_ok
    )
    certification = {
        "schema_version": SCHEMA_VERSION,
        "proposal_id": proposal["proposal_id"],
        "verified_at": _iso_now(),
        "ok": ok,
        "risk": proposal.get("risk"),
        "patch_sha256": validation["patch_sha256"],
        "changed_paths": validation["paths"],
        "tests": tests,
        "pytest_returncode": result.returncode,
        "pytest_outcome": _pytest_outcome(result.stdout + result.stderr),
        "pytest_output_sha256": hashlib.sha256((result.stdout + result.stderr).encode("utf-8")).hexdigest(),
        "diff_check_ok": diff_check.returncode == 0,
        "candidate_integrity_after_tests": post_integrity_ok,
        "isolation": isolation,
        "candidate_only": True,
        "auto_deploy": False,
        "requires_human_before_deploy": True,
    }
    status = "ready_for_human_review" if ok else "verification_failed"
    store.transition(str(proposal["proposal_id"]), status=status, certification=certification)
    return {"ok": ok, "status": status, "certification": certification}
