"""Flask endpoints for the identity-bound MAGI Agent Gateway.

This blueprint is intentionally narrower than ``api/tools_api.py``.  It is
the server-side half of :mod:`api.agentic.mcp_gateway` and keeps external
agents behind one authentication boundary, a fixed tool surface, and the
existing controlled-autonomy plan store.
"""

from __future__ import annotations

import json
import logging
import re
from functools import partial
from typing import Any, Callable

from flask import Blueprint, g, jsonify, request

from api.agentic.contracts import SideEffectLevel
from api.agentic.control import ControlledAutonomyService
from api.agentic.mcp_gateway import GATEWAY_SCHEMA, mcp_tool_definitions
from api.csrf_guard import csrf_exempt
from api.routing.office_cognition import assess_office_request
from magi_v3.telemetry import TraceContext, Tracer


logger = logging.getLogger(__name__)
agent_gateway_bp = Blueprint("magi_agent_gateway", __name__, url_prefix="/agent/v1")
_PLAN_ID_RE = re.compile(r"\Aca-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}\Z")
_MAX_BODY_BYTES = 512 * 1024
_TRACER = Tracer("magi.agent_gateway.server")


@agent_gateway_bp.before_request
def _start_agent_gateway_trace() -> None:
    route = str(getattr(request.url_rule, "rule", None) or request.path)
    route = re.sub(r"<[^>]+>", "{parameter}", route)
    parent = TraceContext.parse(request.headers.get("traceparent"))
    span = _TRACER.start_span(
        "magi.agent.gateway.server",
        parent=parent,
        attributes={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": "MAGI",
            "magi.component": "agent_gateway",
            "http.request.method": request.method,
            "http.route": route,
        },
    )
    span.__enter__()
    g.magi_agent_gateway_span = span


@agent_gateway_bp.after_request
def _finish_agent_gateway_trace(response):
    span = getattr(g, "magi_agent_gateway_span", None)
    if span is not None:
        span.set_attribute("http.response.status_code", int(response.status_code))
        span.set_attribute("magi.outcome", "passed" if response.status_code < 400 else "failed")
        if response.status_code >= 400:
            span.record_error("AgentGatewayHttpError")
        response.headers["traceparent"] = span.context.traceparent
        span.end()
    return response


@agent_gateway_bp.teardown_request
def _abort_agent_gateway_trace(error) -> None:
    span = getattr(g, "magi_agent_gateway_span", None)
    if span is not None and not span.ended:
        if error is not None:
            span.record_error(type(error).__name__)
        span.end()


def _auth_and_identity() -> tuple[str, str] | tuple[None, Any]:
    """Authenticate an MCP client and require its explicit identity binding."""

    from api.tools_api import _check_external_api_key

    ok, error = _check_external_api_key()
    if not ok:
        status = 503 if str(error).startswith("server_misconfigured:") else 401
        return None, (jsonify({"success": False, "error": str(error)}), status)
    if request.content_length and int(request.content_length) > _MAX_BODY_BYTES:
        return None, (jsonify({"success": False, "error": "request_body_too_large"}), 413)

    user_id = str(request.headers.get("X-MAGI-Agent-User-ID") or "").strip()
    platform = str(request.headers.get("X-MAGI-Agent-Platform") or "").strip().upper()
    if not user_id or len(user_id) > 128 or any(ord(char) < 32 for char in user_id):
        return None, (jsonify({"success": False, "error": "X-MAGI-Agent-User-ID is required"}), 400)
    if not platform or len(platform) > 128 or any(ord(char) < 32 for char in platform):
        return None, (jsonify({"success": False, "error": "X-MAGI-Agent-Platform is required"}), 400)
    return user_id, platform


def _body() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def _text(value: Any, field: str, *, max_length: int, required: bool = True) -> str:
    if value in (None, "") and not required:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    result = value.strip()
    if required and not result:
        raise ValueError(f"{field} must not be empty")
    if len(result) > max_length:
        raise ValueError(f"{field} exceeds the {max_length}-character limit")
    if any(ord(char) < 32 and char not in "\t\n\r" for char in result):
        raise ValueError(f"{field} contains a control character")
    return result


def _int(value: Any, field: str, *, default: int, minimum: int, maximum: int) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc


def _bool(value: Any, field: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"0", "1", "true", "false", "yes", "no", "on", "off"}:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    raise ValueError(f"{field} must be a boolean")


def _plan_id(value: Any) -> str:
    result = _text(value, "plan_id", max_length=64).lower()
    if not _PLAN_ID_RE.fullmatch(result):
        raise ValueError("plan_id has an invalid format")
    return result


def _error(exc: Exception, status: int = 400):
    return jsonify({"success": False, "error": str(exc)[:240]}), status


def _plan_payload(proposal: Any, *, include_token: bool) -> dict[str, Any]:
    payload = {
        "success": True,
        "schema": GATEWAY_SCHEMA,
        "requires_confirmation": True,
        "plan": proposal.plan.to_dict(),
        "expires_at": proposal.expires_at,
        "message": proposal.user_message(),
    }
    if include_token:
        # The token is returned only at proposal time. GET/list endpoints never
        # expose it, so a stale plan read cannot be used as an authorization
        # oracle.
        payload["confirmation_token"] = proposal.confirmation_token
    return payload


def _tool_call(
    tool_name: str,
    function: Callable[..., Any],
    *args: Any,
    timeout_sec: int,
    input_data: dict[str, Any],
):
    """Run one read-only tool through MAGI's existing permission/telemetry path."""

    from api.tools_api import (
        _check_tool_access,
        _finish_tool_event,
        _run_with_timeout,
        _start_tool_event,
        _tool_denied_response,
        _tool_exception_response,
        _tool_preview,
    )

    started = _start_tool_event(tool_name, input_data, {"route": "agent_gateway"})
    allowed, decision = _check_tool_access(tool_name, command_subject=f"tool:{tool_name}")
    if not allowed:
        return _tool_denied_response(tool_name, started, decision, {"route": "agent_gateway"})
    ok, result = _run_with_timeout(function, timeout_sec, *args)
    if not ok:
        error = str(result.get("error") if isinstance(result, dict) else result or "tool_timeout")
        return _tool_exception_response(
            tool_name,
            started,
            f"{tool_name}_failed: {error[:180]}",
            metadata={"route": "agent_gateway"},
            status_code=504,
        )
    _finish_tool_event(
        tool_name,
        started,
        ok=True,
        status="handled",
        output_data=_tool_preview(result),
        metadata={"route": "agent_gateway"},
    )
    return jsonify({"success": True, "schema": GATEWAY_SCHEMA, "result": result}), 200


@agent_gateway_bp.route("/health", methods=["GET"])
def agent_health():
    identity = _auth_and_identity()
    if identity[0] is None:
        return identity[1]
    try:
        from api.tools_api import _tools_health_snapshot

        runtime = _tools_health_snapshot(fresh=False)
        runtime_public = {
            "ok": bool(runtime.get("ok")),
            "status": str(runtime.get("status") or "unknown"),
            "service": str(runtime.get("service") or "MAGI Tools API"),
            "cached": bool(runtime.get("cached", False)),
        }
    except Exception as exc:
        logger.warning("agent gateway health probe failed: %s", type(exc).__name__)
        runtime_public = {"ok": False, "status": "probe_failed", "service": "MAGI Tools API", "cached": False}
    controlled = ControlledAutonomyService().store.health_snapshot()
    return jsonify(
        {
            "success": True,
            "schema": GATEWAY_SCHEMA,
            "service": "MAGI Agent Gateway",
            "gateway": {"ok": True, "status": "ready"},
            "magi_runtime": runtime_public,
            "controlled_autonomy": controlled,
            "identity_bound": True,
        }
    ), 200


@agent_gateway_bp.route("/capabilities", methods=["GET"])
def agent_capabilities():
    identity = _auth_and_identity()
    if identity[0] is None:
        return identity[1]
    return jsonify(
        {
            "success": True,
            "schema": GATEWAY_SCHEMA,
            "server": "magi-agent-gateway",
            "tools": mcp_tool_definitions(),
            "security": {
                "identity_bound": True,
                "read_default": True,
                "mutable_actions_require_plan_and_one_time_token": True,
                "raw_shell_and_raw_database_access": False,
            },
        }
    ), 200


@agent_gateway_bp.route("/read", methods=["POST"])
@csrf_exempt
def agent_read():
    identity = _auth_and_identity()
    if identity[0] is None:
        return identity[1]
    user_id, platform = identity
    data = _body()
    try:
        message = _text(data.get("message"), "message", max_length=4_000)
        has_attachment = _bool(data.get("has_attachment"), "has_attachment")
        timeout_sec = _int(data.get("timeout_sec"), "timeout_sec", default=60, minimum=10, maximum=300)
        office = assess_office_request(message, has_attachment=has_attachment)
    except (TypeError, ValueError) as exc:
        return _error(exc)
    if office.envelope.side_effect in {SideEffectLevel.WRITE, SideEffectLevel.DESTRUCTIVE}:
        return jsonify(
            {
                "success": False,
                "safe": False,
                "requires_confirmation": True,
                "side_effect": office.envelope.side_effect.value,
                "next_tool": "magi_prepare_action",
                "error": "mutable request must be prepared as a controlled-autonomy plan",
            }
        ), 409
    if office.needs_clarification:
        return jsonify(
            {
                "success": False,
                "safe": False,
                "requires_input": True,
                "missing_fields": [item.to_dict() for item in office.envelope.missing_fields],
                "error": "request needs clarification before read execution",
            }
        ), 422

    def _run() -> str:
        from api.tools_api import _get_osc_orchestrator, _guard_text

        result = _get_osc_orchestrator().process_message(
            user_id=user_id,
            message=message,
            platform=platform,
            role="user",
            correlation_id=str(request.headers.get("X-Request-ID") or ""),
            channel_context={"agent_gateway": True, "read_only_preflight": True},
        )
        return _guard_text(str(result or ""), platform=platform)

    try:
        from api.tools_api import _get_osc_orchestrator, _run_with_timeout

        del _get_osc_orchestrator  # Imported above inside _run for lazy loading.
        ok, result = _run_with_timeout(_run, timeout_sec)
    except Exception as exc:
        logger.warning("agent gateway read failed: %s", type(exc).__name__)
        return _error(RuntimeError("read_request_failed"), 502)
    if not ok:
        return jsonify({"success": False, "safe": True, "degraded": True, "error": "read_request_timeout"}), 504
    return jsonify(
        {
            "success": True,
            "safe": True,
            "schema": GATEWAY_SCHEMA,
            "reply": str(result or ""),
            "side_effect": office.envelope.side_effect.value,
        }
    ), 200


@agent_gateway_bp.route("/case-status", methods=["POST"])
@csrf_exempt
def agent_case_status():
    identity = _auth_and_identity()
    if identity[0] is None:
        return identity[1]
    data = _body()
    try:
        query = _text(data.get("query"), "query", max_length=256, required=False)
        case_number = _text(data.get("case_number"), "case_number", max_length=128, required=False)
        row_id = _text(data.get("row_id"), "row_id", max_length=128, required=False)
        max_cases = _int(data.get("max_cases"), "max_cases", default=6, minimum=1, maximum=20)
        max_files = _int(data.get("max_files_per_case"), "max_files_per_case", default=20, minimum=1, maximum=50)
        full_scan = _bool(data.get("full_scan"), "full_scan")
        query = query or case_number or row_id
        if not query:
            raise ValueError("one of query, case_number, or row_id is required")
    except (TypeError, ValueError) as exc:
        return _error(exc)

    from skills.evolution.skill_genesis import run_skill_action

    payload = {
        "query": query,
        "case_number": case_number,
        "row_id": row_id,
        "max_cases": max_cases,
        "max_files_per_case": max_files,
        "full_scan": full_scan,
        "summary_only": True,
    }
    task = "status " + json.dumps(payload, ensure_ascii=False)
    guarded_status = partial(
        run_skill_action,
        "osc-flow-case-status",
        task,
        timeout_sec=180,
        auto_repair=False,
        rollback_on_fail=False,
        auto_install_deps=False,
        route_key="osc:agent_gateway:case_status",
    )
    return _tool_call(
        "agent:case_status",
        guarded_status,
        timeout_sec=180,
        input_data={"query": query, "max_cases": max_cases, "full_scan": full_scan},
    )


@agent_gateway_bp.route("/search", methods=["POST"])
@csrf_exempt
def agent_search():
    identity = _auth_and_identity()
    if identity[0] is None:
        return identity[1]
    data = _body()
    try:
        query = _text(data.get("query"), "query", max_length=1_000)
        num_results = _int(data.get("num_results"), "num_results", default=5, minimum=1, maximum=10)
    except (TypeError, ValueError) as exc:
        return _error(exc)
    from skills.research.web_research import search_web

    return _tool_call("agent:search", search_web, query, num_results, timeout_sec=30, input_data={"query": query})


@agent_gateway_bp.route("/research", methods=["POST"])
@csrf_exempt
def agent_research():
    identity = _auth_and_identity()
    if identity[0] is None:
        return identity[1]
    data = _body()
    try:
        topic = _text(data.get("topic"), "topic", max_length=2_000)
        depth = _int(data.get("depth"), "depth", default=3, minimum=1, maximum=5)
    except (TypeError, ValueError) as exc:
        return _error(exc)
    from skills.research.web_research import research_topic

    return _tool_call("agent:research", research_topic, topic, depth, timeout_sec=60, input_data={"topic": topic})


@agent_gateway_bp.route("/fetch", methods=["POST"])
@csrf_exempt
def agent_fetch():
    identity = _auth_and_identity()
    if identity[0] is None:
        return identity[1]
    data = _body()
    try:
        url = _text(data.get("url"), "url", max_length=2_048)
        from api.tools_api import _validate_fetch_url

        valid, reason = _validate_fetch_url(url, data)
        if not valid:
            return jsonify({"success": False, "error": reason}), 403
    except (TypeError, ValueError) as exc:
        return _error(exc)
    from skills.research.web_research import fetch_url_content

    return _tool_call("agent:fetch", fetch_url_content, url, timeout_sec=30, input_data={"url": url})


@agent_gateway_bp.route("/summarize", methods=["POST"])
@csrf_exempt
def agent_summarize():
    identity = _auth_and_identity()
    if identity[0] is None:
        return identity[1]
    data = _body()
    try:
        text = _text(data.get("text"), "text", max_length=50_000)
    except (TypeError, ValueError) as exc:
        return _error(exc)
    from api.handlers.summary_handler import summarize_text_resilient

    return _tool_call("agent:summarize", summarize_text_resilient, text, timeout_sec=120, input_data={"text_length": len(text)})


@agent_gateway_bp.route("/plans", methods=["POST"])
@csrf_exempt
def agent_prepare_action():
    identity = _auth_and_identity()
    if identity[0] is None:
        return identity[1]
    user_id, platform = identity
    data = _body()
    try:
        message = _text(data.get("message"), "message", max_length=4_000)
        has_attachment = _bool(data.get("has_attachment"), "has_attachment")
        ttl_minutes = _int(data.get("ttl_minutes"), "ttl_minutes", default=30, minimum=5, maximum=120)
        understanding = assess_office_request(message, has_attachment=has_attachment)
    except (TypeError, ValueError) as exc:
        return _error(exc)
    if understanding.envelope.side_effect not in {SideEffectLevel.WRITE, SideEffectLevel.DESTRUCTIVE}:
        return jsonify({"success": False, "error": "request does not require a mutable action plan", "next_tool": "magi_read"}), 409
    if understanding.needs_clarification:
        return jsonify(
            {
                "success": False,
                "requires_input": True,
                "missing_fields": [item.to_dict() for item in understanding.envelope.missing_fields],
                "error": "request needs clarification before a mutable plan can be created",
            }
        ), 422
    try:
        proposal = ControlledAutonomyService().propose(
            message,
            user_id=user_id,
            platform=platform,
            has_attachment=has_attachment,
            ttl_minutes=ttl_minutes,
        )
    except Exception as exc:
        logger.warning("agent gateway plan creation failed: %s", type(exc).__name__)
        return _error(RuntimeError("plan_creation_failed"), 503)
    if proposal is None:
        return jsonify({"success": False, "error": "MAGI could not create a safe mutable plan"}), 409
    logger.info("agent_gateway_plan_proposed plan_id=%s platform=%s", proposal.plan.plan_id, platform)
    return jsonify(_plan_payload(proposal, include_token=True)), 201


@agent_gateway_bp.route("/plans", methods=["GET"])
def agent_list_plans():
    identity = _auth_and_identity()
    if identity[0] is None:
        return identity[1]
    user_id, platform = identity
    try:
        limit = _int(request.args.get("limit"), "limit", default=5, minimum=1, maximum=20)
        rows = ControlledAutonomyService().store.list_for_owner(user_id=user_id, platform=platform, limit=limit)
    except (TypeError, ValueError) as exc:
        return _error(exc)
    return jsonify({"success": True, "schema": GATEWAY_SCHEMA, "plans": rows}), 200


@agent_gateway_bp.route("/plans/<plan_id>", methods=["GET"])
def agent_get_plan(plan_id: str):
    identity = _auth_and_identity()
    if identity[0] is None:
        return identity[1]
    try:
        normalized = _plan_id(plan_id)
        user_id, platform = identity
        row = ControlledAutonomyService().store.get(normalized, user_id=user_id, platform=platform)
    except LookupError:
        return jsonify({"success": False, "error": "plan_not_found"}), 404
    except (TypeError, ValueError) as exc:
        return _error(exc)
    return jsonify({"success": True, "schema": GATEWAY_SCHEMA, "plan": row}), 200


@agent_gateway_bp.route("/plans/<plan_id>/cancel", methods=["POST"])
@csrf_exempt
def agent_cancel_plan(plan_id: str):
    identity = _auth_and_identity()
    if identity[0] is None:
        return identity[1]
    try:
        normalized = _plan_id(plan_id)
        user_id, platform = identity
        plan = ControlledAutonomyService().store.cancel(normalized, user_id=user_id, platform=platform)
    except LookupError:
        return jsonify({"success": False, "error": "plan_not_found"}), 404
    except (TypeError, ValueError, RuntimeError) as exc:
        return _error(exc, 409)
    return jsonify({"success": True, "schema": GATEWAY_SCHEMA, "plan_id": plan.plan_id, "status": plan.status.value}), 200


@agent_gateway_bp.route("/plans/<plan_id>/confirm", methods=["POST"])
@csrf_exempt
def agent_confirm_plan(plan_id: str):
    identity = _auth_and_identity()
    if identity[0] is None:
        return identity[1]
    data = _body()
    try:
        normalized = _plan_id(plan_id)
        token = _text(data.get("confirmation_token"), "confirmation_token", max_length=32).lower()
        if not re.fullmatch(r"[a-f0-9]{12}", token):
            raise ValueError("confirmation_token has an invalid format")
        user_id, platform = identity
        service = ControlledAutonomyService()
        # Route the exact confirmation through the same deterministic pipeline
        # used by LINE, Telegram, Discord, and the existing external chat API.
        from api.tools_api import _get_osc_orchestrator

        reply = _get_osc_orchestrator().process_message(
            user_id=user_id,
            message=f"確認自主計畫 {normalized} {token}",
            platform=platform,
            role="user",
            correlation_id=str(request.headers.get("X-Request-ID") or ""),
            channel_context={"agent_gateway": True, "controlled_autonomy": True},
        )
        status = service.store.get(normalized, user_id=user_id, platform=platform)
    except LookupError:
        return jsonify({"success": False, "error": "plan_not_found_or_not_owned"}), 404
    except PermissionError:
        return jsonify({"success": False, "error": "confirmation_token_invalid"}), 403
    except TimeoutError:
        return jsonify({"success": False, "error": "confirmation_token_expired"}), 410
    except (TypeError, ValueError, RuntimeError) as exc:
        return _error(exc, 409)
    except Exception as exc:
        logger.warning("agent gateway plan confirmation failed: %s", type(exc).__name__)
        return _error(RuntimeError("plan_confirmation_failed"), 502)
    final_status = str(status.get("status") or "")
    if final_status == "awaiting_confirmation":
        return jsonify(
            {
                "success": False,
                "schema": GATEWAY_SCHEMA,
                "error": "confirmation_not_accepted",
                "reply": str(reply or ""),
                "plan": status,
            }
        ), 403
    return jsonify(
        {
            "success": final_status in {"succeeded", "running"},
            "schema": GATEWAY_SCHEMA,
            "reply": str(reply or ""),
            "plan": status,
            "business_completion_attested": False,
        }
    ), 200


__all__ = ["agent_gateway_bp"]
