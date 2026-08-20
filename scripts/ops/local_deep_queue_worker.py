#!/usr/bin/env python3
"""Scheduled, local-only worker for admitted deep requests.

The public queue contains only hashes and opaque references.  The worker may
resolve those references solely through MAGI's private SQLite job store.
"""
from __future__ import annotations

import json
import os
import time
import hashlib
from contextlib import contextmanager
from collections.abc import Callable
from pathlib import Path
from typing import Any

from api.deep_task_control import DeepTaskDeferred, reviewed_local_controller
from api.platforms import runtime_dir


def _queue_path() -> Path:
    return runtime_dir.metrics("local_deep_queue")


def _records() -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in _queue_path().read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []


def _receipt_rows() -> list[dict[str, Any]]:
    try:
        return [
            row
            for row in (json.loads(line) for line in runtime_dir.metrics("local_deep_queue_receipts").read_text(encoding="utf-8").splitlines() if line.strip())
            if isinstance(row, dict)
        ]
    except (OSError, json.JSONDecodeError):
        return []


def _terminal_task_ids() -> set[str]:
    # Deferred means "retry after the runtime gate changes", not completion.
    return {
        str(row.get("task_id") or "")
        for row in _receipt_rows()
        if row.get("state") in {"succeeded", "failed", "rejected"}
    }


def _deferred_until() -> dict[str, float]:
    delay = max(30, int(os.environ.get("MAGI_LOCAL_DEEP_RETRY_SEC", "300") or "300"))
    latest: dict[str, float] = {}
    for row in _receipt_rows():
        if row.get("state") == "deferred":
            latest[str(row.get("task_id") or "")] = float(row.get("at_epoch") or 0.0) + delay
    return latest


def _receipt(task_id: str, state: str, **detail: Any) -> None:
    runtime_dir.atomic_append_jsonl(
        runtime_dir.metrics("local_deep_queue_receipts"),
        {"task_id": task_id, "state": state, "provider": "omlx", "at_epoch": time.time(), **detail},
        rotate_at=500,
        keep_tail=300,
    )


@contextmanager
def _single_worker_lock():
    """Cross-process owner lock; a competing scheduler tick simply defers."""
    path = runtime_dir.root() / "local_deep_queue_worker.lock"
    with path.open("a+") as handle:
        try:
            from magi_v3 import fcntl_compat as fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            yield False
            return
        try:
            yield True
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


def _drain_once_unlocked(
    *,
    retrieve: Callable[[str], str | None],
    execute: Callable[[str, dict[str, Any]], Any],
    controller_factory: Callable[[], Any] = reviewed_local_controller,
) -> dict[str, Any]:
    """Execute at most one outstanding reference through the controlled switch."""
    completed = _terminal_task_ids()
    retry_after = _deferred_until()
    now = time.time()
    for row in _records():
        task_id = str(row.get("task_id") or "")
        if (
            not task_id
            or task_id in completed
            or retry_after.get(task_id, 0.0) > now
            or row.get("state") != "admitted_waiting_for_owned_worker"
        ):
            continue
        reference = str(row.get("secure_task_ref") or "")
        if not reference:
            _receipt(task_id, "deferred", reason="secure_task_ref_missing")
            return {"state": "deferred", "task_id": task_id}
        prompt = retrieve(reference)
        if not isinstance(prompt, str) or not prompt:
            _receipt(task_id, "deferred", reason="secure_task_unavailable")
            return {"state": "deferred", "task_id": task_id}
        try:
            result = controller_factory().run(lambda: execute(prompt, row))
        except DeepTaskDeferred as exc:
            _receipt(task_id, "deferred", reason=str(exc))
            return {"state": "deferred", "task_id": task_id}
        except Exception as exc:
            _receipt(task_id, "failed", reason=type(exc).__name__)
            _deliver(row, "本機深度工作未能完成，請稍後重新提交。", task_id)
            return {"state": "failed", "task_id": task_id}
        _receipt(task_id, "succeeded", result_type=type(result).__name__)
        _deliver(row, f"本機深度工作已完成：{str(result)[:10000]}", task_id)
        return {"state": "succeeded", "task_id": task_id}
    return {"state": "idle"}


def _deliver(row: dict[str, Any], text: str, task_id: str) -> None:
    """Persist only fully addressed results; an empty recipient is never broadcast."""
    user_id = str(row.get("user_id") or "").strip()
    platform = str(row.get("platform") or "").strip()
    if not user_id or not platform:
        return
    from api.durable_notifications import enqueue

    enqueue(
        user_id=user_id,
        platform=platform,
        text=text,
        dedupe_key=f"local-deep:{task_id}",
    )


def _job_id_from_reference(reference: str) -> str:
    prefix = "job_queue:"
    value = str(reference or "")
    job_id = value[len(prefix):].strip() if value.startswith(prefix) else ""
    if not job_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in job_id):
        return ""
    return job_id


def retrieve_local_job(reference: str) -> str | None:
    """Resolve only this process's private SQLite job reference."""
    job_id = _job_id_from_reference(reference)
    if not job_id:
        return None
    from skills.memory import job_queue

    job = job_queue.read(job_id)
    if job.get("job_type") != "local_deep" or job.get("status") != "queued":
        return None
    prompt = str(job.get("user_text") or "")
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    expected = str(payload.get("prompt_sha256") or "")
    if not prompt or expected != hashlib.sha256(prompt.encode("utf-8")).hexdigest():
        return None
    return prompt


def execute_local_job(prompt: str, row: dict[str, Any]) -> str:
    """Claim, execute on the now-active 26B topology, persist and deliver."""
    job_id = _job_id_from_reference(str(row.get("secure_task_ref") or ""))
    if not job_id:
        raise RuntimeError("secure local task reference is invalid")
    from skills.memory import job_queue

    job = job_queue.read(job_id)
    if job.get("job_type") != "local_deep" or not job_queue.claim(job_id):
        raise RuntimeError("secure local task is unavailable")
    try:
        from api.orchestrator import Orchestrator
        from api.pipelines.message_pipeline import _try_agentic_route
        from api.routing.intent_contract import classify_intent_contract

        orchestrator = Orchestrator()
        reply = _try_agentic_route(
            orchestrator,
            prompt,
            user_id=str(job.get("user_id") or ""),
            role=str(job.get("role") or "user"),
            platform=str(job.get("platform") or "WEB"),
            decision=classify_intent_contract(prompt),
            heavy=False,
        )
        if not str(reply or "").strip():
            from skills.bridge.inference_gateway import InferenceGateway

            result = InferenceGateway().chat(
                prompt,
                task_type=str(row.get("task_type") or "legal_analysis"),
                timeout=max(120, int(os.environ.get("MAGI_LOCAL_DEEP_INFERENCE_TIMEOUT_SEC", "300") or "300")),
                force_quality=True,
                allow_synthetic_fallback=False,
            )
            if not result.get("success") or not str(result.get("response") or "").strip():
                raise RuntimeError("local deep model did not produce a verified response")
            reply = str(result["response"]).strip()
        try:
            orchestrator.record_assistant_reply(str(job.get("user_id") or ""), str(reply))
        except Exception:
            pass
        job_queue.complete(
            job_id,
            json.dumps(
                {"success": True, "output": str(reply), "provider": "omlx", "task_type": row.get("task_type")},
                ensure_ascii=False,
            ),
        )
        if str(job.get("platform") or "").upper() in {"LINE", "TELEGRAM"}:
            try:
                from api.webhooks.line import _deliver_attachment_job_response

                _deliver_attachment_job_response(job, str(reply))
            except Exception:
                # The durable result remains available even when a push
                # channel is temporarily unavailable.
                pass
        return str(reply)
    except Exception as exc:
        job_queue.fail(job_id, type(exc).__name__)
        raise


def drain_once(
    *,
    retrieve: Callable[[str], str | None] = retrieve_local_job,
    execute: Callable[[str, dict[str, Any]], Any] = execute_local_job,
    controller_factory: Callable[[], Any] = reviewed_local_controller,
) -> dict[str, Any]:
    with _single_worker_lock() as acquired:
        if not acquired:
            return {"state": "deferred", "reason": "single_worker_lock_active"}
        return _drain_once_unlocked(retrieve=retrieve, execute=execute, controller_factory=controller_factory)


def main() -> int:
    result = drain_once()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result.get("state") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
