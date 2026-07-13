from __future__ import annotations

from dataclasses import dataclass, field
import inspect
from typing import Any, Callable, Mapping, Optional

from api.tools.base import ToolExecutor
from api.tools.contracts import ToolContext, ToolResult, ToolSideEffect, ToolSpec, normalize_tool_side_effect
from api.tools.executors import CallableToolExecutor


@dataclass()
class RegisteredTool:
    spec: ToolSpec
    executor: ToolExecutor
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        payload = self.spec.as_dict()
        if self.aliases:
            payload["aliases"] = list(self.aliases)
        return payload


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        spec: ToolSpec,
        executor: ToolExecutor,
        *,
        aliases: tuple[str, ...] | list[str] = (),
    ) -> RegisteredTool:
        entry = RegisteredTool(spec=spec, executor=executor, aliases=tuple(aliases))
        self._tools[spec.name] = entry
        for alias in entry.aliases:
            self._tools[alias] = entry
        return entry

    def register_callable(
        self,
        name: str,
        fn,
        *,
        description: str = "",
        permission_tag: str = "",
        timeout_sec: int = 60,
        input_schema: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        aliases: tuple[str, ...] | list[str] = (),
        side_effect: str = ToolSideEffect.READ_ONLY,
        requires_confirmation: bool = False,
        idempotent: bool = True,
        verification: Callable[..., Any] | Mapping[str, Any] | bool | None = None,
        rollback: Callable[..., Any] | Mapping[str, Any] | bool | None = None,
        retry: Mapping[str, Any] | None = None,
        health: Mapping[str, Any] | None = None,
        degraded: Mapping[str, Any] | None = None,
    ) -> RegisteredTool:
        spec = ToolSpec(
            name=name,
            description=description,
            permission_tag=permission_tag,
            timeout_sec=timeout_sec,
            input_schema=dict(input_schema or {}),
            metadata=dict(metadata or {}),
            side_effect=side_effect,
            requires_confirmation=requires_confirmation,
            idempotent=idempotent,
            verification=verification,
            rollback=rollback,
            retry=dict(retry or {}),
            health=dict(health or {}),
            degraded=dict(degraded or {}),
        )
        return self.register(spec, CallableToolExecutor(fn), aliases=aliases)

    def get(self, name: str) -> Optional[RegisteredTool]:
        return self._tools.get(name)

    def list_tools(self) -> list[dict[str, Any]]:
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        for key, entry in sorted(self._tools.items(), key=lambda item: item[0]):
            if entry.spec.name in seen:
                continue
            seen.add(entry.spec.name)
            items.append(entry.as_dict())
        return items

    def execute(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        context: Optional[ToolContext] = None,
    ) -> ToolResult:
        entry = self.get(name)
        if entry is None:
            return ToolResult(tool_name=name, success=False, error=f"Unknown tool: {name}")
        arguments = dict(arguments or {})
        contract = self._contract_metadata(entry.spec)
        max_attempts = self._max_attempts(entry.spec)
        can_retry = entry.spec.idempotent and max_attempts > 1
        contract["retry"] = {
            "configured_max_attempts": max_attempts,
            "allowed": can_retry,
            "attempts": 0,
        }

        permission_error = self._permission_error(entry.spec, context)
        if permission_error:
            contract["status"] = "blocked"
            contract["permission"] = {"status": "denied", "reason": permission_error}
            if contract["verification"]["status"] == "pending":
                contract["verification"] = {"status": "not_run"}
            return self._result(entry.spec, success=False, error=permission_error, contract=contract)

        confirmation_error = self._confirmation_error(entry.spec, context)
        if confirmation_error:
            contract["status"] = "blocked"
            contract["confirmation"] = {"status": "required", "reason": confirmation_error}
            if contract["verification"]["status"] == "pending":
                contract["verification"] = {"status": "not_run"}
            return self._result(entry.spec, success=False, error=confirmation_error, contract=contract)

        contract["permission"] = {"status": "granted"}
        contract["confirmation"] = {
            "status": "confirmed" if entry.spec.requires_confirmation else "not_required"
        }

        last_result: ToolResult | None = None
        verification_failed = False
        for attempt in range(1, max_attempts + 1 if can_retry else 2):
            result = self._execute_once(entry, arguments, context)
            contract["retry"]["attempts"] = attempt
            result = self._with_contract(result, entry.spec, contract)
            last_result = result

            if not result.success:
                if can_retry and attempt < max_attempts:
                    continue
                contract["status"] = "failed"
                return self._with_contract(result, entry.spec, contract)

            verification = self._verify(entry.spec, arguments, result, context, attempt)
            contract["verification"] = verification
            if verification["status"] in {"not_required", "passed"}:
                contract["status"] = "succeeded"
                return self._with_contract(result, entry.spec, contract)

            verification_failed = True
            if can_retry and attempt < max_attempts:
                continue
            break

        if last_result is None:
            return self._result(entry.spec, success=False, error="tool_execution_unavailable", contract=contract)

        if verification_failed:
            contract["rollback"] = self._rollback(entry.spec, arguments, last_result, context)
            contract["status"] = "verification_failed"
            return self._result(
                entry.spec,
                success=False,
                output=last_result.output,
                error="verification_failed",
                contract=contract,
            )

        contract["status"] = "failed"
        return self._with_contract(last_result, entry.spec, contract)

    @staticmethod
    def _contract_metadata(spec: ToolSpec) -> dict[str, Any]:
        return {
            "side_effect": normalize_tool_side_effect(spec.side_effect),
            "requires_confirmation": spec.requires_confirmation,
            "idempotent": spec.idempotent,
            "health": dict(spec.health),
            "degraded": dict(spec.degraded),
            "verification": {
                "status": "not_required" if spec.verification in (None, False) else "pending"
            },
            "rollback": {
                "status": "not_available" if spec.rollback in (None, False) else "not_attempted"
            },
            "status": "pending",
        }

    @staticmethod
    def _context_metadata(context: ToolContext | None) -> Mapping[str, Any]:
        return context.metadata if context is not None and isinstance(context.metadata, Mapping) else {}

    def _permission_error(self, spec: ToolSpec, context: ToolContext | None) -> str:
        if not spec.permission_tag:
            return ""
        metadata = self._context_metadata(context)
        permission_keys = ("permissions", "permission_tags")
        has_explicit_policy = any(key in metadata for key in permission_keys)
        if context is not None and context.permissions is not None:
            has_explicit_policy = True
        has_explicit_policy = has_explicit_policy or bool(metadata.get("enforce_tool_permissions"))
        if not has_explicit_policy:
            return ""

        permissions = self._values(
            getattr(context, "permissions", ()),
            metadata.get("permissions", ()),
            metadata.get("permission_tags", ()),
        )
        if "*" in permissions or spec.permission_tag in permissions:
            return ""
        return "permission_denied"

    def _confirmation_error(self, spec: ToolSpec, context: ToolContext | None) -> str:
        if not spec.requires_confirmation:
            return ""
        metadata = self._context_metadata(context)
        expected = self._values(
            spec.metadata.get("confirmation_token"),
            spec.metadata.get("confirmation_tokens"),
        )
        confirmations = metadata.get("confirmations", {})
        if isinstance(confirmations, Mapping):
            supplied_from_map = (confirmations.get(spec.name), confirmations.get("*"))
        else:
            supplied_from_map = ()
        supplied = self._values(
            getattr(context, "confirmation_token", ""),
            getattr(context, "confirmation_tokens", ()),
            metadata.get("confirmation_token"),
            metadata.get("confirmation_tokens"),
            supplied_from_map,
        )
        if not supplied:
            return "confirmation_required"
        if expected and not expected.intersection(supplied):
            return "confirmation_invalid"
        return ""

    @staticmethod
    def _values(*values: Any) -> set[str]:
        normalized: set[str] = set()
        for value in values:
            if value is None:
                continue
            if isinstance(value, str):
                if value:
                    normalized.add(value)
                continue
            if isinstance(value, Mapping):
                normalized.update(str(item) for item in value.values() if item)
                continue
            try:
                normalized.update(str(item) for item in value if item)
            except TypeError:
                normalized.add(str(value))
        return normalized

    @staticmethod
    def _max_attempts(spec: ToolSpec) -> int:
        value = spec.retry.get("max_attempts", spec.retry.get("attempts", 1))
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 1

    def _execute_once(
        self,
        entry: RegisteredTool,
        arguments: Mapping[str, Any],
        context: ToolContext | None,
    ) -> ToolResult:
        try:
            output = entry.executor.execute(dict(arguments), context=context)
            if isinstance(output, ToolResult):
                return output
            return ToolResult(tool_name=entry.spec.name, success=True, output=output)
        except Exception as exc:
            return ToolResult(tool_name=entry.spec.name, success=False, error=str(exc))

    def _verify(
        self,
        spec: ToolSpec,
        arguments: Mapping[str, Any],
        result: ToolResult,
        context: ToolContext | None,
        attempt: int,
    ) -> dict[str, Any]:
        handler = self._contract_handler(spec.verification)
        if spec.verification in (None, False):
            return {"status": "not_required"}
        if handler is None:
            return {"status": "unavailable", "error": "verification_handler_missing"}
        try:
            verified = self._invoke_handler(
                handler,
                arguments=arguments,
                output=result.output,
                result=result,
                context=context,
                spec=spec,
                attempt=attempt,
            )
        except Exception as exc:
            return {"status": "failed", "error": str(exc)}
        if isinstance(verified, ToolResult):
            return {"status": "passed" if verified.success else "failed", "result": verified.as_dict()}
        if isinstance(verified, Mapping):
            passed = bool(verified.get("ok", verified.get("success", False)))
            return {"status": "passed" if passed else "failed", "result": dict(verified)}
        return {"status": "passed" if bool(verified) else "failed"}

    def _rollback(
        self,
        spec: ToolSpec,
        arguments: Mapping[str, Any],
        result: ToolResult,
        context: ToolContext | None,
    ) -> dict[str, Any]:
        handler = self._contract_handler(spec.rollback)
        if spec.rollback in (None, False):
            return {"status": "not_available"}
        if handler is None:
            return {"status": "not_available", "error": "rollback_handler_missing"}
        try:
            rollback_result = self._invoke_handler(
                handler,
                arguments=arguments,
                output=result.output,
                result=result,
                context=context,
                spec=spec,
            )
        except Exception as exc:
            return {"status": "failed", "error": str(exc)}
        if isinstance(rollback_result, ToolResult):
            return {
                "status": "succeeded" if rollback_result.success else "failed",
                "result": rollback_result.as_dict(),
            }
        if isinstance(rollback_result, Mapping):
            succeeded = bool(rollback_result.get("ok", rollback_result.get("success", True)))
            return {"status": "succeeded" if succeeded else "failed", "result": dict(rollback_result)}
        return {"status": "succeeded" if rollback_result is not False else "failed"}

    @staticmethod
    def _contract_handler(contract: Callable[..., Any] | Mapping[str, Any] | bool | None) -> Callable[..., Any] | None:
        if callable(contract):
            return contract
        if isinstance(contract, Mapping) and callable(contract.get("handler")):
            return contract["handler"]
        return None

    @staticmethod
    def _invoke_handler(handler: Callable[..., Any], **available: Any) -> Any:
        try:
            signature = inspect.signature(handler)
        except (TypeError, ValueError):
            return handler(**available)
        if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
            return handler(**available)
        aliases = {
            "tool_result": available.get("result"),
            "value": available.get("output"),
            "response": available.get("output"),
        }
        kwargs = {
            name: available[name] if name in available else aliases[name]
            for name in signature.parameters
            if name in available or name in aliases
        }
        return handler(**kwargs)

    @staticmethod
    def _result(
        spec: ToolSpec,
        *,
        success: bool,
        output: Any = None,
        error: str = "",
        contract: Mapping[str, Any],
    ) -> ToolResult:
        return ToolResult(
            tool_name=spec.name,
            success=success,
            output=output,
            error=error,
            metadata={**dict(spec.metadata), "contract": dict(contract)},
        )

    def _with_contract(self, result: ToolResult, spec: ToolSpec, contract: Mapping[str, Any]) -> ToolResult:
        return ToolResult(
            tool_name=result.tool_name or spec.name,
            success=result.success,
            output=result.output,
            error=result.error,
            metadata={**dict(spec.metadata), **dict(result.metadata), "contract": dict(contract)},
        )


GLOBAL_TOOL_REGISTRY = ToolRegistry()


def get_global_tool_registry() -> ToolRegistry:
    return GLOBAL_TOOL_REGISTRY
