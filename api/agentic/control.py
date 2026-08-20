"""Durable, confirmation-bound handoff for MAGI's controlled autonomy.

The language model is intentionally not an executor for mutable office work.
It may autonomously complete read-only research through the existing ReAct
route.  A mutable request that reaches the broad agent route is converted into
a durable plan and, after an exact one-time confirmation, replayed through
MAGI's registered domain workflow handlers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any, Mapping

from api.agentic.contracts import (
    ConfirmationRequirement,
    PlanStatus,
    SideEffectLevel,
    StepStatus,
    WorkflowPlan,
    WorkflowStep,
)
from api.agentic.planner import build_plan, cancel_plan, confirm_step, transition_step
from api.routing.office_cognition import OfficeUnderstanding, assess_office_request
from api.runtime_paths import get_agent_dir


_SCHEMA = "magi.controlled-autonomy/v1"
_PLAN_ID_RE = re.compile(r"\Aca-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}\Z")
_TOKEN_RE = re.compile(r"\A[a-f0-9]{12}\Z")
_COMMAND_RE = re.compile(
    r"\A\s*(?P<verb>確認|核准|取消|狀態|查詢|列出)\s*"
    r"(?:受控自主計畫|自主計畫)"
    r"(?:\s+(?P<plan>ca-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}))?"
    r"(?:\s+(?P<token>[a-f0-9]{12}))?\s*\Z",
    re.IGNORECASE,
)
_COMMAND_ALT_RE = re.compile(
    r"\A\s*(?:受控自主計畫|自主計畫)\s*"
    r"(?P<verb>狀態|查詢|列出|取消)"
    r"(?:\s+(?P<plan>ca-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}))?\s*\Z",
    re.IGNORECASE,
)
_STATUS_LABELS = {
    "draft": "草稿",
    "awaiting_input": "等待補充資料",
    "awaiting_confirmation": "等待核准",
    "ready": "可執行",
    "running": "執行中",
    "succeeded": "已完成安全交接",
    "failed": "交接失敗",
    "blocked": "受阻",
    "cancelled": "已取消",
    "pending": "待處理",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _owner_hash(user_id: str, platform: str) -> str:
    normalized_user = str(user_id or "").strip()
    normalized_platform = str(platform or "").strip().upper()
    if not normalized_user or not normalized_platform:
        raise ValueError("controlled autonomy requires a bound user and platform")
    payload = f"{normalized_platform}\0{normalized_user}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _token_hash(plan_id: str, token: str) -> str:
    return hashlib.sha256(f"{plan_id}\0{token}".encode("ascii")).hexdigest()


def _reply_hash(reply: str) -> str:
    return hashlib.sha256(str(reply or "").encode("utf-8")).hexdigest()


def _safe_summary(text: str, limit: int = 120) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    return compact[:limit]


def _default_db_path() -> Path:
    explicit = str(os.environ.get("MAGI_CONTROLLED_AUTONOMY_DB", "") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    return (get_agent_dir() / "controlled-autonomy" / "plans.sqlite3").resolve()


@dataclass(frozen=True)
class PlanProposal:
    plan: WorkflowPlan
    confirmation_token: str
    expires_at: str
    domain: str
    operation: str

    def user_message(self) -> str:
        effect = "刪除或不可逆操作" if self.plan.max_side_effect is SideEffectLevel.DESTRUCTIVE else "寫入或對外操作"
        return (
            "🧭 已建立受控自主計畫\n"
            f"計畫：{self.plan.plan_id}\n"
            f"目標：{_safe_summary(self.plan.intent.utterance)}\n"
            f"判斷：{self.domain} · {self.operation} · {effect}\n"
            "處理方式：核准後只會交給 MAGI 既有的專用業務流程，並保留該流程原有的驗證與安全閥門。\n"
            f"若要開始，請回覆：「確認自主計畫 {self.plan.plan_id} {self.confirmation_token}」\n"
            f"核准碼有效至：{self.expires_at}"
        )


@dataclass(frozen=True)
class DispatchLease:
    plan_id: str
    original_request: str
    plan: WorkflowPlan


@dataclass(frozen=True)
class ControlledCommand:
    verb: str
    plan_id: str = ""
    token: str = ""


def parse_controlled_command(message: str) -> ControlledCommand | None:
    text = str(message or "")
    match = _COMMAND_RE.fullmatch(text) or _COMMAND_ALT_RE.fullmatch(text)
    if not match:
        return None
    verb = str(match.group("verb") or "").strip()
    if verb == "核准":
        verb = "確認"
    if verb == "查詢":
        verb = "狀態"
    groups = match.groupdict()
    return ControlledCommand(
        verb=verb,
        plan_id=str(groups.get("plan") or "").lower(),
        token=str(groups.get("token") or "").lower(),
    )


class ControlledAutonomyStore:
    """SQLite-backed, process-safe plan store shared by web and messaging."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path).expanduser().resolve() if path else _default_db_path()
        self._prepare_path()
        self._init_schema()

    def _prepare_path(self) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            parent.chmod(0o700)
        except OSError:
            pass
        if self.path.is_symlink():
            raise ValueError("controlled autonomy database must not be a symlink")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS controlled_plans (
                    plan_id TEXT PRIMARY KEY,
                    schema_name TEXT NOT NULL,
                    owner_hash TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    replay_count INTEGER NOT NULL DEFAULT 0,
                    receipt_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_controlled_plans_owner_updated "
                "ON controlled_plans(owner_hash, updated_at DESC)"
            )
            terminal_cutoff = _iso(_utcnow() - timedelta(days=30))
            expired_cutoff = _iso(_utcnow() - timedelta(days=7))
            connection.execute(
                "DELETE FROM controlled_plans WHERE "
                "(status IN ('succeeded','failed','cancelled') AND updated_at < ?) OR "
                "(status='awaiting_confirmation' AND expires_at < ?)",
                (terminal_cutoff, expired_cutoff),
            )
            interrupted_cutoff = _iso(_utcnow() - timedelta(minutes=15))
            interrupted = connection.execute(
                "SELECT plan_id, plan_json FROM controlled_plans "
                "WHERE status='running' AND updated_at < ?",
                (interrupted_cutoff,),
            ).fetchall()
            for row in interrupted:
                try:
                    plan = WorkflowPlan.from_json(str(row["plan_json"]))
                    if plan.get_step("dispatch").status is not StepStatus.RUNNING:
                        continue
                    plan = transition_step(
                        plan,
                        "dispatch",
                        StepStatus.FAILED,
                        error="interrupted_handoff_requires_reconciliation",
                    )
                    receipt = {
                        "schema": _SCHEMA,
                        "observed_at": _iso(_utcnow()),
                        "handoff_success": False,
                        "business_completion_attested": False,
                        "recovery_action": "verify_registered_workflow_state_before_retry",
                    }
                    connection.execute(
                        "UPDATE controlled_plans SET plan_json=?, status=?, receipt_json=?, updated_at=? "
                        "WHERE plan_id=?",
                        (
                            plan.to_json(),
                            plan.status.value,
                            json.dumps(receipt, sort_keys=True),
                            _iso(_utcnow()),
                            str(row["plan_id"]),
                        ),
                    )
                except Exception:
                    # A malformed stored plan must never be silently rewritten;
                    # callers will fail closed when they attempt to restore it.
                    continue
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _validate_plan_id(plan_id: str) -> str:
        normalized = str(plan_id or "").strip().lower()
        if not _PLAN_ID_RE.fullmatch(normalized):
            raise ValueError("invalid controlled autonomy plan id")
        return normalized

    def create(
        self,
        understanding: OfficeUnderstanding,
        *,
        user_id: str,
        platform: str,
        ttl_minutes: int = 30,
    ) -> PlanProposal:
        if understanding.envelope.side_effect not in {SideEffectLevel.WRITE, SideEffectLevel.DESTRUCTIVE}:
            raise ValueError("a confirmation plan is only valid for mutable work")
        now = _utcnow()
        plan_id = f"ca-{now.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}"
        token = secrets.token_hex(6)
        expires = now + timedelta(minutes=max(5, min(120, int(ttl_minutes))))
        requirement = ConfirmationRequirement(
            required=True,
            reason="mutable office workflow handoff",
            prompt="Confirm exact plan and one-time token before workflow dispatch.",
        )
        assessment = WorkflowStep(
            step_id="assess",
            action="assess_goal_and_boundary",
            description="Determine domain, operation, evidence requirement and side-effect boundary.",
            status=StepStatus.SUCCEEDED,
            side_effect=SideEffectLevel.NONE,
            output={
                "domain": understanding.primary_domain,
                "operation": understanding.operation,
                "tool_hints": list(understanding.tool_hints),
                "tool_requirement": understanding.tool_requirement.level,
            },
        )
        dispatch = WorkflowStep(
            step_id="dispatch",
            action="handoff_to_registered_workflow",
            description="Replay the approved request through MAGI's dedicated domain handlers.",
            depends_on=("assess",),
            side_effect=understanding.envelope.side_effect,
            confirmation=requirement,
            inputs={"domain": understanding.primary_domain, "operation": understanding.operation},
        )
        observe = WorkflowStep(
            step_id="observe",
            action="record_handoff_receipt",
            description="Record a de-identified workflow handoff receipt; do not infer business completion.",
            depends_on=("dispatch",),
            side_effect=SideEffectLevel.READ,
        )
        plan = build_plan(
            understanding.envelope,
            (assessment, dispatch, observe),
            plan_id=plan_id,
            metadata={
                "schema": _SCHEMA,
                "domain": understanding.primary_domain,
                "operation": understanding.operation,
                "completion_scope": "workflow_handoff_only",
            },
        )
        platform_name = str(platform or "UNKNOWN").strip().upper()
        owner = _owner_hash(user_id, platform_name)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT plan_id, plan_json FROM controlled_plans "
                "WHERE owner_hash=? AND goal=? AND status='awaiting_confirmation' AND expires_at>? "
                "ORDER BY updated_at DESC LIMIT 1",
                (owner, understanding.envelope.utterance, _iso(now)),
            ).fetchone()
            if existing is not None:
                existing_plan = WorkflowPlan.from_json(str(existing["plan_json"]))
                connection.execute(
                    "UPDATE controlled_plans SET token_hash=?, expires_at=?, updated_at=? WHERE plan_id=?",
                    (
                        _token_hash(existing_plan.plan_id, token),
                        _iso(expires),
                        _iso(now),
                        existing_plan.plan_id,
                    ),
                )
                connection.commit()
                return PlanProposal(
                    plan=existing_plan,
                    confirmation_token=token,
                    expires_at=_iso(expires),
                    domain=understanding.primary_domain,
                    operation=understanding.operation,
                )
            active_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM controlled_plans WHERE owner_hash=? "
                    "AND status IN ('awaiting_input','awaiting_confirmation','ready','running','blocked')",
                    (owner,),
                ).fetchone()[0]
            )
            if active_count >= 50:
                connection.rollback()
                raise RuntimeError("controlled autonomy active plan limit reached")
            connection.execute(
                """
                INSERT INTO controlled_plans
                (plan_id, schema_name, owner_hash, platform, goal, plan_json,
                 token_hash, status, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    _SCHEMA,
                    owner,
                    platform_name,
                    understanding.envelope.utterance,
                    plan.to_json(),
                    _token_hash(plan_id, token),
                    plan.status.value,
                    _iso(expires),
                    _iso(now),
                    _iso(now),
                ),
            )
            connection.commit()
        return PlanProposal(
            plan=plan,
            confirmation_token=token,
            expires_at=_iso(expires),
            domain=understanding.primary_domain,
            operation=understanding.operation,
        )

    def _bound_row(self, connection: sqlite3.Connection, plan_id: str, user_id: str, platform: str) -> sqlite3.Row:
        normalized = self._validate_plan_id(plan_id)
        row = connection.execute(
            "SELECT * FROM controlled_plans WHERE plan_id = ?",
            (normalized,),
        ).fetchone()
        if row is None or not hmac.compare_digest(
            str(row["owner_hash"]), _owner_hash(user_id, platform)
        ):
            raise LookupError("controlled autonomy plan not found")
        return row

    def get(self, plan_id: str, *, user_id: str, platform: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._bound_row(connection, plan_id, user_id, str(platform or "").upper())
        plan = WorkflowPlan.from_json(str(row["plan_json"]))
        return {
            "plan_id": plan.plan_id,
            "goal": str(row["goal"]),
            "status": plan.status.value,
            "expires_at": str(row["expires_at"]),
            "updated_at": str(row["updated_at"]),
            "replay_count": int(row["replay_count"]),
            "steps": [
                {"action": step.action, "status": step.status.value}
                for step in plan.steps
            ],
            "receipt": json.loads(str(row["receipt_json"] or "{}")),
            "confirmation_expired": _parse_iso(str(row["expires_at"])) < _utcnow(),
        }

    def list_for_owner(self, *, user_id: str, platform: str, limit: int = 5) -> list[dict[str, Any]]:
        owner = _owner_hash(user_id, str(platform or "").upper())
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT plan_id, goal, plan_json, expires_at, updated_at FROM controlled_plans "
                "WHERE owner_hash = ? ORDER BY updated_at DESC LIMIT ?",
                (owner, max(1, min(20, int(limit)))),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            plan = WorkflowPlan.from_json(str(row["plan_json"]))
            result.append(
                {
                    "plan_id": plan.plan_id,
                    "goal": str(row["goal"]),
                    "status": plan.status.value,
                    "expires_at": str(row["expires_at"]),
                    "confirmation_expired": _parse_iso(str(row["expires_at"])) < _utcnow(),
                }
            )
        return result

    def health_snapshot(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM controlled_plans GROUP BY status"
            ).fetchall()
            stale_running = connection.execute(
                "SELECT COUNT(*) FROM controlled_plans WHERE status='running' AND updated_at < ?",
                (_iso(_utcnow() - timedelta(minutes=15)),),
            ).fetchone()[0]
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "schema": _SCHEMA,
            "ok": int(stale_running) == 0,
            "counts": counts,
            "stale_running": int(stale_running),
        }

    def begin_dispatch(
        self,
        plan_id: str,
        token: str,
        *,
        user_id: str,
        platform: str,
    ) -> DispatchLease:
        normalized_token = str(token or "").strip().lower()
        if not _TOKEN_RE.fullmatch(normalized_token):
            raise PermissionError("confirmation token is invalid")
        now = _utcnow()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._bound_row(connection, plan_id, user_id, str(platform or "").upper())
            plan = WorkflowPlan.from_json(str(row["plan_json"]))
            if int(row["replay_count"]) != 0:
                raise RuntimeError("controlled autonomy plan was already dispatched")
            if plan.status is not PlanStatus.AWAITING_CONFIRMATION:
                raise RuntimeError("controlled autonomy plan is not awaiting confirmation")
            if _parse_iso(str(row["expires_at"])) < now:
                raise TimeoutError("controlled autonomy confirmation expired")
            if not hmac.compare_digest(str(row["token_hash"]), _token_hash(plan.plan_id, normalized_token)):
                raise PermissionError("confirmation token is invalid")
            plan = confirm_step(plan, "dispatch", confirmation_id=plan.plan_id)
            plan = transition_step(plan, "dispatch", StepStatus.RUNNING)
            connection.execute(
                "UPDATE controlled_plans SET plan_json=?, status=?, replay_count=1, updated_at=? WHERE plan_id=?",
                (plan.to_json(), plan.status.value, _iso(now), plan.plan_id),
            )
            connection.commit()
        return DispatchLease(plan_id=plan.plan_id, original_request=str(row["goal"]), plan=plan)

    def finish_dispatch(self, lease: DispatchLease, *, success: bool, reply: str) -> WorkflowPlan:
        now = _utcnow()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM controlled_plans WHERE plan_id = ?",
                (self._validate_plan_id(lease.plan_id),),
            ).fetchone()
            if row is None:
                raise LookupError("controlled autonomy plan not found")
            plan = WorkflowPlan.from_json(str(row["plan_json"]))
            if plan.get_step("dispatch").status is not StepStatus.RUNNING:
                raise RuntimeError("controlled autonomy dispatch is not running")
            receipt = {
                "schema": _SCHEMA,
                "observed_at": _iso(now),
                "reply_sha256": _reply_hash(reply),
                "reply_present": bool(str(reply or "").strip()),
                "handoff_success": bool(success),
                "business_completion_attested": False,
            }
            if success:
                plan = transition_step(
                    plan,
                    "dispatch",
                    StepStatus.SUCCEEDED,
                    output={"registered_workflow_reply": True},
                )
                plan = transition_step(plan, "observe", StepStatus.RUNNING)
                plan = transition_step(plan, "observe", StepStatus.SUCCEEDED, output=receipt)
            else:
                plan = transition_step(plan, "dispatch", StepStatus.FAILED, error="registered_workflow_handoff_failed")
            connection.execute(
                "UPDATE controlled_plans SET plan_json=?, status=?, receipt_json=?, updated_at=? WHERE plan_id=?",
                (plan.to_json(), plan.status.value, json.dumps(receipt, sort_keys=True), _iso(now), plan.plan_id),
            )
            connection.commit()
        return plan

    def cancel(self, plan_id: str, *, user_id: str, platform: str) -> WorkflowPlan:
        now = _utcnow()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._bound_row(connection, plan_id, user_id, str(platform or "").upper())
            plan = WorkflowPlan.from_json(str(row["plan_json"]))
            if plan.status is PlanStatus.CANCELLED:
                connection.commit()
                return plan
            if plan.status.terminal:
                raise RuntimeError("completed controlled autonomy plan cannot be cancelled")
            plan = cancel_plan(plan)
            connection.execute(
                "UPDATE controlled_plans SET plan_json=?, status=?, updated_at=? WHERE plan_id=?",
                (plan.to_json(), plan.status.value, _iso(now), plan.plan_id),
            )
            connection.commit()
        return plan


class ControlledAutonomyService:
    def __init__(self, store: ControlledAutonomyStore | None = None) -> None:
        self.store = store or ControlledAutonomyStore()

    def propose(self, message: str, *, user_id: str, platform: str, has_attachment: bool = False) -> PlanProposal | None:
        understanding = assess_office_request(message, has_attachment=has_attachment)
        if understanding.envelope.side_effect not in {SideEffectLevel.WRITE, SideEffectLevel.DESTRUCTIVE}:
            return None
        if understanding.needs_clarification:
            return None
        return self.store.create(understanding, user_id=user_id, platform=platform)

    def command(self, message: str, *, user_id: str, platform: str) -> tuple[str, DispatchLease | None] | None:
        command = parse_controlled_command(message)
        if command is None:
            return None
        if command.verb == "列出":
            rows = self.store.list_for_owner(user_id=user_id, platform=platform)
            if not rows:
                return ("目前沒有受控自主計畫。", None)
            lines = ["🧭 最近的受控自主計畫："]
            for row in rows:
                status = (
                    "核准已過期"
                    if row.get("confirmation_expired") and row["status"] == "awaiting_confirmation"
                    else _STATUS_LABELS.get(str(row["status"]), str(row["status"]))
                )
                lines.append(f"- {row['plan_id']} · {status} · {_safe_summary(row['goal'], 60)}")
            return ("\n".join(lines), None)
        if not command.plan_id:
            return ("請提供計畫編號。例如：「狀態自主計畫 ca-...」", None)
        if command.verb == "狀態":
            record = self.store.get(command.plan_id, user_id=user_id, platform=platform)
            status = (
                "核准已過期"
                if record["confirmation_expired"] and record["status"] == "awaiting_confirmation"
                else _STATUS_LABELS.get(str(record["status"]), str(record["status"]))
            )
            lines = [
                f"🧭 {record['plan_id']} · {status}",
                f"目標：{_safe_summary(record['goal'])}",
            ]
            lines.extend(
                f"- {item['action']}: {_STATUS_LABELS.get(str(item['status']), str(item['status']))}"
                for item in record["steps"]
            )
            return ("\n".join(lines), None)
        if command.verb == "取消":
            plan = self.store.cancel(command.plan_id, user_id=user_id, platform=platform)
            return (f"已取消受控自主計畫 {plan.plan_id}。", None)
        if command.verb == "確認":
            if not command.token:
                return ("核准缺少一次性核准碼；未啟動任何寫入流程。", None)
            lease = self.store.begin_dispatch(
                command.plan_id,
                command.token,
                user_id=user_id,
                platform=platform,
            )
            return ("", lease)
        return None


def handoff_reply_succeeded(reply: str) -> bool:
    text = str(reply or "").strip()
    if not text:
        return False
    if text.startswith("❌") or text.startswith("系統暫時"):
        return False
    blocked_markers = (
        "未找到可安全執行的專用流程",
        "系統暫時忙碌",
        "registered_workflow_handoff_failed",
    )
    return not any(marker in text for marker in blocked_markers)


__all__ = [
    "ControlledAutonomyService",
    "ControlledAutonomyStore",
    "ControlledCommand",
    "DispatchLease",
    "PlanProposal",
    "handoff_reply_succeeded",
    "parse_controlled_command",
]
