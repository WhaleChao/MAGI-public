from api.routing.route_decision import build_route_decision
from api.routing.route_explanations import RouteExplanation, RouteExplanationCollector
from api.routing.route_policy import (
    get_skill_min_confidence,
    is_generic_word_only,
    should_cache_intent,
    should_dispatch_skill,
)

# Phase 1 registries
from api.routing.service_registry import get_service, get_service_url, get_service_host_port
from api.routing.model_registry import get_role_model, resolve_model, is_alias
from api.routing.node_registry import get_node, get_node_ip, get_node_url
from api.routing.datastore_registry import get_datastore, get_connection_params

# Phase 4: unified routing system
from api.routing.context import RoutingContext
from api.routing.models import RoutingDecision, FallbackPlan, ServiceTarget
from api.routing.policy_engine import PolicyEngine
from api.routing.request_router import RequestRouter
from api.routing.inference_router import InferenceRouter
from api.routing.telemetry import RoutingTelemetry

__all__ = [
    "RouteExplanation",
    "RouteExplanationCollector",
    "build_route_decision",
    "get_skill_min_confidence",
    "is_generic_word_only",
    "should_cache_intent",
    "should_dispatch_skill",
    # Service registry
    "get_service",
    "get_service_url",
    "get_service_host_port",
    # Model registry
    "get_role_model",
    "resolve_model",
    "is_alias",
    # Node registry
    "get_node",
    "get_node_ip",
    "get_node_url",
    # Datastore registry
    "get_datastore",
    "get_connection_params",
    # Phase 4: unified routing
    "RoutingContext",
    "RoutingDecision",
    "FallbackPlan",
    "ServiceTarget",
    "PolicyEngine",
    "RequestRouter",
    "InferenceRouter",
    "RoutingTelemetry",
]

