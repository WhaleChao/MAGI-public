"""Controlled, local-only admission for deep model work.

This module deliberately does not start models itself.  It serializes work
around the reviewed oMLX switch script and model-live gate, which makes it
safe to use from a request worker or a scheduled worker without bypassing the
existing watchdog.  ``@heavy`` remains the only route that may select a cloud
provider; a deep task here is always a local 20B/26B candidate.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
import json
import hashlib
import subprocess
import threading
import time
import re
import urllib.request
from pathlib import Path
from typing import Any


DEEP_TASK_TYPES = frozenset({"legal_analysis", "draft", "long_document", "multi_step"})
_DEEP_LANGUAGE_RE = re.compile(
    r"(?:深入|完整|全面|詳細|逐步|交叉核對|系統性|建立方案|利弊比較|多階段)",
    re.I,
)


@dataclass(frozen=True)
class DeepAdmission:
    required: bool
    reason: str


class DeepTaskDeferred(RuntimeError):
    """Safe retry signal emitted only by controller admission/topology gates."""


def assess_deep_admission(*, task_type: str, prompt: str = "", disagreement: bool = False) -> DeepAdmission:
    """Use deterministic escalation only; it never authorizes an external API."""
    task = str(task_type or "").strip().lower()
    if disagreement:
        return DeepAdmission(True, "local verifier disagreement")
    if task in DEEP_TASK_TYPES:
        return DeepAdmission(True, f"deep local task type: {task}")
    if len(str(prompt or "")) >= int(os.environ.get("MAGI_DEEP_PROMPT_CHARS", "12000")):
        return DeepAdmission(True, "long prompt requires local deep review")
    return DeepAdmission(False, "routine local route")


def infer_local_deep_task_type(prompt: str) -> str:
    """Classify only work that merits a topology switch.

    Realtime facts and ordinary lookups stay on deterministic tools/small
    models.  Mutable work is deliberately excluded: the dedicated controlled
    autonomy workflow must confirm it before any write or export.
    """
    text = str(prompt or "").strip()
    if not text:
        return ""
    try:
        from api.agentic.contracts import SideEffectLevel
        from api.routing.office_cognition import assess_office_request

        office = assess_office_request(text)
        if office.envelope.side_effect.rank >= SideEffectLevel.WRITE.rank:
            return ""
        domains = {item.name for item in office.candidates}
        if office.operation == "analyze" and "legal_research" in domains:
            return "legal_analysis"
        if "summary" in domains and len(text) >= 2_000:
            return "long_document"
        if office.operation == "analyze" and len(domains) >= 2:
            return "multi_step"
        if office.operation == "analyze" and len(text) >= 80 and _DEEP_LANGUAGE_RE.search(text):
            return "multi_step"
    except Exception:
        return "long_document" if len(text) >= 12_000 else ""
    return "long_document" if len(text) >= 12_000 else ""


class DeepTaskController:
    """Single-concurrency local deep task executor with topology rollback.

    The submitted callable is retained while a failed switch is rolled back;
    therefore the task is never dropped.  A caller can inspect the returned
    exception and retry after the existing watchdog reports recovery.
    """

    def __init__(
        self,
        *,
        switch: Callable[[str], None],
        health: Callable[[str], bool],
        resource_ok: Callable[[], bool] | None = None,
        transaction_free: Callable[[], bool] | None = None,
    ) -> None:
        self._switch = switch
        self._health = health
        # A controller without evidence providers is intentionally unusable.
        # Tests and production must explicitly bind both gates.
        self._resource_ok = resource_ok or (lambda: False)
        self._transaction_free = transaction_free or (lambda: False)
        self._lock = threading.Lock()

    def run(self, work: Callable[[], Any]) -> Any:
        """Run one deep task, restoring the day topology after completion.

        The controller fails closed before a switch if a transactional writer
        or resource governor is active.  It never performs cloud fallback.
        """
        with self._lock:
            if not self._transaction_free():
                raise DeepTaskDeferred("deep task deferred: transaction lock is active")
            if not self._resource_ok():
                raise DeepTaskDeferred("deep task deferred: resource gate rejected admission")
            if not self._health("day"):
                raise DeepTaskDeferred("deep task deferred: day topology is unhealthy")
            switched = False
            try:
                self._switch("night")
                switched = True
                if not self._health("night"):
                    raise DeepTaskDeferred("deep task aborted: night topology is unhealthy")
                return work()
            finally:
                if switched:
                    self._switch("day")
                    if not self._health("day"):
                        raise RuntimeError("deep task rollback failed: day topology is unhealthy")


def _resource_governor_allows_deep() -> bool:
    """Deep switching requires the existing governor's best (normal) state."""
    try:
        from scripts.ops import resource_governor
        decision = resource_governor.classify(resource_governor.collect_snapshot())
        return bool(decision.ok and decision.level == "normal")
    except Exception:
        return False


def runtime_owner_worker_transaction_free(*, root: str) -> bool:
    """Require live owner, zero inflight work and no other running cron job.

    An explicitly configured receipt remains supported for isolated tests.
    Production uses the existing release-bound ready/health endpoints and
    canonical cron state instead of relying on a new receipt with no owner.
    Any unavailable, stale or malformed source fails closed.
    """
    configured = os.environ.get("MAGI_DEEP_RUNTIME_EVIDENCE_PATH", "").strip()
    if configured:
        try:
            payload = json.loads(Path(configured).read_text(encoding="utf-8"))
            age = time.time() - float(payload["observed_at_epoch"])
            return bool(
                payload.get("schema") == "magi.v3.runtime-owner-worker-transaction/v1"
                and payload.get("owner_verified") is True
                and payload.get("transaction_active") is False
                and int(payload.get("active_workers")) == 0
                and 0 <= age <= 60
            )
        except Exception:
            return False

    def _json_url(url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=3.0) as response:
            if int(getattr(response, "status", 0) or 0) != 200:
                raise RuntimeError("health endpoint unavailable")
            value = json.loads(response.read(262_144).decode("utf-8"))
            if not isinstance(value, dict):
                raise RuntimeError("health endpoint returned invalid payload")
            return value

    try:
        gateway = _json_url(os.environ.get("MAGI_DEEP_GATEWAY_READYZ", "http://127.0.0.1:5002/readyz"))
        components = gateway.get("components") if isinstance(gateway.get("components"), dict) else {}
        resources = components.get("active_resources") if isinstance(components.get("active_resources"), dict) else {}
        if not (
            gateway.get("ready") is True
            and components.get("release_ownership") == "verified"
            and int(resources.get("total", -1)) == 0
        ):
            return False
        tools = _json_url(os.environ.get("MAGI_DEEP_TOOLS_HEALTH", "http://127.0.0.1:5003/health"))
        checks = tools.get("checks") if isinstance(tools.get("checks"), dict) else {}
        capacity = checks.get("capacity") if isinstance(checks.get("capacity"), dict) else {}
        if not (
            tools.get("ok") is True
            and int(capacity.get("tool_inflight", -1)) == 0
            and int(capacity.get("inference_inflight", -1)) == 0
        ):
            return False

        from api.platforms import runtime_dir

        cron_path = Path(os.environ.get("MAGI_CRON_STATE_PATH", "").strip() or runtime_dir.cron_state())
        cron = json.loads(cron_path.read_text(encoding="utf-8"))
        if not isinstance(cron, dict):
            return False
        current_job = os.environ.get("MAGI_CRON_JOB_ID", "job_local_deep_queue_worker").strip()
        active_states = {"running", "claimed", "started", "executing"}
        for job_id, row in cron.items():
            if str(job_id) == current_job or not isinstance(row, dict):
                continue
            if str(row.get("last_status") or "").strip().lower() in active_states:
                return False
            occurrence = row.get("v3_pending_occurrence")
            if isinstance(occurrence, dict) and str(occurrence.get("status") or occurrence.get("state") or "").strip().lower() in active_states:
                return False
        return True
    except Exception:
        return False


def enqueue_local_deep_admission(
    *,
    task_type: str,
    prompt: str,
    secure_task_ref: str = "",
    task_id: str = "",
    user_id: str = "",
    platform: str = "",
) -> str:
    """Durably record an admission for the owned deep worker, never a cloud job."""
    from api.platforms import runtime_dir
    task_id = str(task_id or "").strip() or hashlib.sha256(f"{time.time_ns()}:{task_type}:{prompt}".encode()).hexdigest()[:24]
    runtime_dir.atomic_append_jsonl(
        runtime_dir.metrics("local_deep_queue"),
        {"task_id": task_id, "task_type": str(task_type), "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "secure_task_ref": str(secure_task_ref or ""), "user_id": str(user_id or ""), "platform": str(platform or ""), "provider": "omlx", "state": "admitted_waiting_for_owned_worker", "created_at_epoch": time.time()},
        rotate_at=500,
        keep_tail=300,
    )
    return task_id


def enqueue_local_deep_job(
    *,
    task_type: str,
    prompt: str,
    platform: str,
    user_id: str,
    role: str = "user",
    chat_id: str = "",
    reply_to_message_id: int | None = None,
) -> str:
    """Persist the private prompt locally and expose only an opaque queue ref."""
    task = str(task_type or "").strip().lower()
    if task not in DEEP_TASK_TYPES:
        raise ValueError("unsupported local deep task type")
    text = str(prompt or "").strip()
    if not text:
        raise ValueError("local deep prompt is required")
    from skills.memory import job_queue

    job_id = job_queue.enqueue(
        job_type="local_deep",
        platform=str(platform or "WEB"),
        user_id=str(user_id or ""),
        role=str(role or "user"),
        user_text=text,
        chat_id=str(chat_id or ""),
        reply_to_message_id=reply_to_message_id,
        payload={
            "task_type": task,
            "prompt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "provider": "omlx",
        },
    )
    try:
        enqueue_local_deep_admission(
            task_type=task,
            prompt=text,
            secure_task_ref=f"job_queue:{job_id}",
            task_id=job_id,
            user_id=str(user_id or ""),
            platform=str(platform or "WEB"),
        )
    except Exception:
        job_queue.fail(job_id, "local deep admission persistence failed")
        raise
    return job_id


def reviewed_local_controller(*, root: str | None = None) -> DeepTaskController:
    """Production adapter for the existing switch/watchdog contract.

    This adapter is intentionally opt-in: importing it cannot switch a model.
    """
    project = root or str(__import__("pathlib").Path(__file__).resolve().parents[1])
    switch_script = os.path.join(project, "config", "bin", "omlx_switch_model.sh")

    def switch(profile: str) -> None:
        subprocess.run(["/bin/bash", switch_script, profile], check=True, timeout=900)

    def health(profile: str) -> bool:
        from scripts.ops.model_live_gate import build_report
        return bool(build_report(profile).ok)

    return DeepTaskController(
        switch=switch,
        health=health,
        resource_ok=_resource_governor_allows_deep,
        transaction_free=lambda: runtime_owner_worker_transaction_free(root=project),
    )
