"""Unified inference router.

Decides which model, provider, and endpoint to use for inference requests.
Uses :mod:`api.routing.model_registry` and :mod:`api.routing.service_registry`
to resolve logical roles into concrete targets, and produces a
:class:`~api.routing.models.FallbackPlan` so callers can transparently
retry across providers.

Usage::

    from api.routing.inference_router import InferenceRouter
    from api.routing.context import RoutingContext

    router = InferenceRouter()
    ctx = RoutingContext(message="翻譯這段話", intent="QUERY")
    plan = router.resolve(ctx, model_role="text_primary")
"""

from __future__ import annotations

import logging
from typing import Any

from api.routing.context import RoutingContext
from api.routing.models import FallbackPlan, ServiceTarget

_log = logging.getLogger(__name__)


class InferenceRouter:
    """Resolve inference requests to concrete service targets.

    The router reads from the model and service registries at call time
    (not at init time) so that hot-reloads are automatically picked up.

    Parameters:
        default_provider:   Default provider tag when none is specified.
        default_timeout:    Default per-target timeout in seconds.
        max_retries:        Default max retries across all targets.
    """

    def __init__(
        self,
        *,
        default_provider: str = "omlx",
        default_timeout: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        self._default_provider = default_provider
        self._default_timeout = default_timeout
        self._max_retries = max_retries

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(
        self,
        ctx: RoutingContext,
        *,
        model_role: str = "text_primary",
        provider: Optional[str] = None,
        service_name: Optional[str] = None,
    ) -> FallbackPlan:
        """Resolve *model_role* to a :class:`FallbackPlan`.

        Steps:
        1. Look up the concrete model ID from the model registry.
        2. Look up the service endpoint from the service registry.
        3. Build the primary target.
        4. If the model role has a ``fallback_role``, build a fallback target.
        5. Return a :class:`FallbackPlan` with both targets.

        Parameters:
            ctx:            Routing context (used for trace metadata).
            model_role:     Logical model role to resolve.
            provider:       Provider override.  Falls back to default.
            service_name:   Service name override.  Falls back to provider
                            mapping.
        """
        from api.routing.model_registry import get_role_model, get_role
        from api.routing.service_registry import get_service

        effective_provider = provider or self._default_provider
        effective_service = service_name or self._provider_to_service(effective_provider)

        targets: list[ServiceTarget] = []
        reason_parts: list[str] = []

        # --- Primary target ---
        model_id = get_role_model(model_role)
        endpoint = self._resolve_endpoint(effective_service)

        targets.append(ServiceTarget(
            service_name=effective_service,
            model_role=model_role,
            provider=effective_provider,
            endpoint=endpoint,
            model_id=model_id,
            priority=0,
        ))
        reason_parts.append(f"primary={model_role}({model_id})")

        # --- Fallback target (if role has fallback_role) ---
        role_entry = get_role(model_role)
        if role_entry and role_entry.fallback_role:
            fb_role = role_entry.fallback_role
            fb_model_id = get_role_model(fb_role)
            if fb_model_id != model_id:
                targets.append(ServiceTarget(
                    service_name=effective_service,
                    model_role=fb_role,
                    provider=effective_provider,
                    endpoint=endpoint,
                    model_id=fb_model_id,
                    priority=1,
                ))
                reason_parts.append(f"fallback={fb_role}({fb_model_id})")

        # --- Vision attachment override ---
        if ctx.has_attachment and ctx.attachment_type in {"image", "image/*"}:
            vision_model = get_role_model("vision")
            if vision_model and vision_model != model_id:
                # Prepend vision target as highest priority
                vision_target = ServiceTarget(
                    service_name=effective_service,
                    model_role="vision",
                    provider=effective_provider,
                    endpoint=endpoint,
                    model_id=vision_model,
                    priority=-1,  # higher than primary
                )
                targets.insert(0, vision_target)
                reason_parts.insert(0, f"vision={vision_model}")

        return FallbackPlan.from_targets(
            targets,
            max_retries=self._max_retries,
            timeout_sec=self._default_timeout,
            reason="; ".join(reason_parts),
        )

    def resolve_embedding(
        self,
        ctx: RoutingContext,
        *,
        provider: Optional[str] = None,
    ) -> ServiceTarget:
        """Resolve the embedding model to a single target.

        Embedding requests do not need a fallback plan because they are
        typically synchronous and short-lived.
        """
        from api.routing.model_registry import get_role_model
        from api.routing.service_registry import get_service

        effective_provider = provider or self._default_provider
        service_name = self._provider_to_service(effective_provider)
        model_id = get_role_model("embedding")
        endpoint = self._resolve_endpoint(service_name)

        return ServiceTarget(
            service_name=service_name,
            model_role="embedding",
            provider=effective_provider,
            endpoint=endpoint,
            model_id=model_id,
            priority=0,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _provider_to_service(provider: str) -> str:
        """Map a provider tag to the canonical service name."""
        mapping: dict[str, str] = {
            "omlx": "omlx_inference",
            "openai": "openai_api",
            "anthropic": "anthropic_api",
        }
        return mapping.get(provider, f"{provider}_inference")

    @staticmethod
    def _resolve_endpoint(service_name: str) -> str:
        """Best-effort endpoint resolution.  Returns empty string on failure."""
        try:
            from api.routing.service_registry import get_service_url
            return get_service_url(service_name)
        except (KeyError, Exception):
            _log.debug("Could not resolve endpoint for service %s", service_name)
            return ""
