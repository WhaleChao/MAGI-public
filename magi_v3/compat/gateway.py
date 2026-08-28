"""WSGI-preserving bridge from V3 gateway listeners to V2 route handlers.

The bridge deliberately keeps ports 5002 and 5003 as separate compatibility
services.  The pinned inventory contains two conflicting route-method pairs
(``/health`` and ``/livez``), so merging both Flask URL maps would silently
change dispatch behavior.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import threading
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INVENTORY_PATH = REPO_ROOT / "docs" / "architecture" / "v3" / "generated" / "v2_runtime_routes.json"
SUPPORTED_SERVICES = ("5002", "5003")
EXPECTED_COUNTS = {"5002": 280, "5003": 67, "total": 347}
HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
LEGACY_MODULES = {"5002": "api.server", "5003": "api.tools_api"}


class StartResponse(Protocol):
    def __call__(
        self,
        status: str,
        response_headers: list[tuple[str, str]],
        exc_info: Any = None,
    ) -> Callable[[bytes], Any] | None: ...


WSGIApp = Callable[[Mapping[str, Any], StartResponse], Iterable[bytes]]
AppLoader = Callable[[str], WSGIApp]


class CompatibilityLoadError(RuntimeError):
    """The legacy compatibility app could not be safely loaded."""


class CompatibilitySurfaceError(CompatibilityLoadError):
    """The loaded app no longer matches the pinned V2 route surface."""


@dataclass(frozen=True, order=True, slots=True)
class RouteSpec:
    service: str
    rule: str
    methods: tuple[str, ...]
    endpoint: str

    @classmethod
    def from_mapping(cls, service: str, value: Mapping[str, Any]) -> "RouteSpec":
        methods = tuple(sorted({str(item).upper() for item in value.get("methods", ())}))
        rule = value.get("rule")
        endpoint = value.get("endpoint")
        if service not in SUPPORTED_SERVICES:
            raise CompatibilitySurfaceError(f"unsupported compatibility service: {service!r}")
        if not isinstance(rule, str) or not rule.startswith("/"):
            raise CompatibilitySurfaceError("route rule must be absolute")
        if not methods or not set(methods) <= HTTP_METHODS:
            raise CompatibilitySurfaceError(f"route methods are invalid: {methods!r}")
        if not isinstance(endpoint, str) or not endpoint:
            raise CompatibilitySurfaceError("route endpoint is missing")
        if value.get("service", service) != service:
            raise CompatibilitySurfaceError("route service binding is inconsistent")
        return cls(service, rule, methods, endpoint)


# New MAGI-native pages may share the legacy Flask application without
# redefining the frozen V2 compatibility inventory. Keep every extension
# signature explicit so an accidental route remains fail-closed.
NATIVE_EXTENSION_ROUTES = frozenset(
    {
        RouteSpec(
            "5002",
            "/tools",
            ("GET",),
            "video_studio.public_tools_page",
        ),
        RouteSpec(
            "5002",
            "/video-studio",
            ("GET",),
            "video_studio.video_studio_page",
        ),
        RouteSpec(
            "5002",
            "/api/video-studio/health",
            ("GET",),
            "video_studio.video_studio_health",
        ),
        RouteSpec(
            "5002",
            "/api/video-studio/render",
            ("POST",),
            "video_studio.video_studio_render",
        ),
        RouteSpec(
            "5002",
            "/api/video-studio/interpret",
            ("POST",),
            "video_studio.video_studio_interpret",
        ),
        RouteSpec(
            "5002",
            "/api/video-studio/render-assets",
            ("POST",),
            "video_studio.video_studio_render_assets",
        ),
        RouteSpec(
            "5002",
            "/cookie-cutter",
            ("GET",),
            "cookie_cutter.cookie_cutter_page",
        ),
        RouteSpec(
            "5002",
            "/api/cookie-cutter/prepare",
            ("POST",),
            "cookie_cutter.cookie_cutter_prepare_api",
        ),
        RouteSpec(
            "5002",
            "/api/cookie-cutter/generate",
            ("POST",),
            "cookie_cutter.cookie_cutter_generate_api",
        ),
        RouteSpec(
            "5002",
            "/api/cookie-cutter/health",
            ("GET",),
            "cookie_cutter.cookie_cutter_health_api",
        ),
        RouteSpec(
            "5002",
            "/api/exam-tutor/choice-attempt",
            ("POST",),
            "exam_tutor.exam_tutor_choice_attempt_api",
        ),
        RouteSpec(
            "5002",
            "/api/exam-tutor/choice-bank",
            ("GET",),
            "exam_tutor.exam_tutor_choice_bank_api",
        ),
        RouteSpec(
            "5002",
            "/api/exam-tutor/choice-import",
            ("POST",),
            "exam_tutor.exam_tutor_choice_import_api",
        ),
        RouteSpec(
            "5002",
            "/api/exam-tutor/essay-bank",
            ("GET",),
            "exam_tutor.exam_tutor_essay_bank_api",
        ),
        RouteSpec(
            "5002",
            "/api/exam-tutor/review",
            ("POST",),
            "exam_tutor.exam_tutor_review_api",
        ),
        RouteSpec(
            "5002",
            "/api/exam-tutor/trends",
            ("GET",),
            "exam_tutor.exam_tutor_trends_api",
        ),
        RouteSpec(
            "5002",
            "/exam-tutor",
            ("GET",),
            "exam_tutor.exam_tutor_page",
        ),
        RouteSpec(
            "5002",
            "/exam-tutor/archive/<path:relative_path>",
            ("GET",),
            "exam_tutor.exam_tutor_archive_file",
        ),
        RouteSpec(
            "5002",
            "/sentencing-trends",
            ("GET",),
            "sentencing_trends.page",
        ),
        RouteSpec(
            "5002",
            "/api/sentencing-trends/search",
            ("GET",),
            "sentencing_trends.search_api",
        ),
        RouteSpec(
            "5002",
            "/manual",
            ("GET",),
            "dashboard_pages.maintenance_manual",
        ),
        RouteSpec(
            "5002",
            "/manual/pdf",
            ("GET",),
            "dashboard_pages.maintenance_manual_pdf",
        ),
        RouteSpec(
            "5002",
            "/manual/markdown",
            ("GET",),
            "dashboard_pages.maintenance_manual_markdown",
        ),
        RouteSpec(
            "5002",
            "/manual/source-index.json",
            ("GET",),
            "dashboard_pages.maintenance_manual_source_index",
        ),
    }
)


@dataclass(frozen=True, slots=True)
class RouteInventory:
    routes: tuple[RouteSpec, ...]

    @classmethod
    def load(cls, path: str | Path = DEFAULT_INVENTORY_PATH) -> "RouteInventory":
        source = Path(path).expanduser().resolve()
        try:
            document = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CompatibilitySurfaceError(f"route inventory is unreadable: {source}: {exc}") from exc
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise CompatibilitySurfaceError("route inventory schema_version must equal 1")
        services = document.get("services")
        if not isinstance(services, dict) or set(services) != set(SUPPORTED_SERVICES):
            raise CompatibilitySurfaceError("route inventory must contain exactly services 5002 and 5003")
        routes: list[RouteSpec] = []
        for service in SUPPORTED_SERVICES:
            rows = services.get(service)
            if not isinstance(rows, list):
                raise CompatibilitySurfaceError(f"service {service} routes must be a list")
            routes.extend(RouteSpec.from_mapping(service, row) for row in rows if isinstance(row, dict))
            if len(rows) != sum(route.service == service for route in routes):
                raise CompatibilitySurfaceError(f"service {service} contains invalid route metadata")
        inventory = cls(tuple(routes))
        if inventory.counts != EXPECTED_COUNTS:
            raise CompatibilitySurfaceError(
                f"pinned route counts changed: expected {EXPECTED_COUNTS}, got {inventory.counts}"
            )
        declared = document.get("counts")
        if not isinstance(declared, dict) or any(declared.get(key) != value for key, value in EXPECTED_COUNTS.items()):
            raise CompatibilitySurfaceError("declared route counts do not match the pinned compatibility surface")
        if len(inventory.routes) != len(set(inventory.routes)):
            raise CompatibilitySurfaceError("route inventory contains duplicate signatures")
        return inventory

    @property
    def counts(self) -> dict[str, int]:
        return {
            "5002": sum(route.service == "5002" for route in self.routes),
            "5003": sum(route.service == "5003" for route in self.routes),
            "total": len(self.routes),
        }

    def for_service(self, service: str) -> tuple[RouteSpec, ...]:
        _validate_service(service)
        return tuple(route for route in self.routes if route.service == service)


def _validate_service(service: str) -> str:
    normalized = str(service or "").strip()
    if normalized not in SUPPORTED_SERVICES:
        raise ValueError(f"service must be one of {', '.join(SUPPORTED_SERVICES)}")
    return normalized


def _surface_signatures(app: Any, service: str) -> Counter[RouteSpec]:
    url_map = getattr(app, "url_map", None)
    if url_map is None or not callable(getattr(url_map, "iter_rules", None)):
        raise CompatibilitySurfaceError(f"service {service} did not load a Flask-compatible URL map")
    routes: Counter[RouteSpec] = Counter()
    for rule in url_map.iter_rules():
        if str(rule.endpoint) == "static":
            continue
        methods = tuple(sorted(set(getattr(rule, "methods", ())) & HTTP_METHODS))
        if not methods:
            continue
        routes[RouteSpec(service, str(rule.rule), methods, str(rule.endpoint))] += 1
    return routes


def verify_loaded_surface(app: Any, service: str, inventory: RouteInventory) -> None:
    expected = Counter(inventory.for_service(service))
    observed = _surface_signatures(app, service)
    allowed_extensions = Counter(
        route for route in NATIVE_EXTENSION_ROUTES if route.service == service
    )
    missing_counter = expected - observed
    unexpected_counter = observed - expected - allowed_extensions
    if not missing_counter and not unexpected_counter:
        return
    missing = sorted(missing_counter.elements())[:5]
    unexpected = sorted(unexpected_counter.elements())[:5]
    raise CompatibilitySurfaceError(
        f"service {service} route surface mismatch: expected={sum(expected.values())} "
        f"observed={sum(observed.values())} missing={missing!r} unexpected={unexpected!r}"
    )


_IMPORT_LOCK = threading.RLock()


@contextmanager
def _startup_suppressed() -> Iterator[None]:
    names = ("MAGI_DISABLE_SERVER_STARTUP_HOOKS", "MAGI_SKIP_IMPORT_PROBES")
    previous = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ[name] = "1"
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def _suppress_known_model_probe(service: str) -> Iterator[None]:
    if service != "5002":
        yield
        return
    probes = importlib.import_module("skills.ops.health_probes")
    original = getattr(probes, "probe_omlx_models")
    probes.probe_omlx_models = lambda *args, **kwargs: {
        "pass": False,
        "status_code": 0,
        "models": [],
        "error": "disabled_during_compat_import",
    }
    try:
        yield
    finally:
        probes.probe_omlx_models = original


def _legacy_loader(service: str) -> WSGIApp:
    service = _validate_service(service)
    if service == "5003" and importlib.util.find_spec("flask_cors") is None:
        raise CompatibilityLoadError("flask_cors must be installed before loading service 5003")
    with _IMPORT_LOCK, _startup_suppressed(), _suppress_known_model_probe(service):
        module: ModuleType = importlib.import_module(LEGACY_MODULES[service])
    app = getattr(module, "app", None)
    if not callable(app):
        raise CompatibilityLoadError(f"{LEGACY_MODULES[service]} does not expose a callable app")
    return app


class LazyCompatibilityApp:
    """Load and verify one legacy WSGI app on its first request.

    The request environ and ``start_response`` callback are passed through
    without copying or normalization.  Consequently Flask session cookies,
    duplicate headers, streaming iterables, and multipart input streams retain
    their original behavior.
    """

    def __init__(
        self,
        service: str,
        *,
        inventory: RouteInventory,
        loader: AppLoader = _legacy_loader,
        verifier: Callable[[Any, str, RouteInventory], None] = verify_loaded_surface,
    ) -> None:
        self.service = _validate_service(service)
        self.inventory = inventory
        self._loader = loader
        self._verifier = verifier
        self._lock = threading.Lock()
        self._app: WSGIApp | None = None
        self._error: str = ""

    @property
    def loaded(self) -> bool:
        return self._app is not None

    def load(self) -> WSGIApp:
        if self._app is not None:
            return self._app
        with self._lock:
            if self._app is not None:
                return self._app
            try:
                candidate = self._loader(self.service)
                self._verifier(candidate, self.service, self.inventory)
            except Exception as exc:
                self._error = f"{type(exc).__name__}: {exc}"
                raise CompatibilityLoadError(self._error) from exc
            self._app = candidate
            self._error = ""
            return candidate

    def status(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "loaded": self.loaded,
            "route_count": len(self.inventory.for_service(self.service)),
            "error": self._error,
            "startup_hooks_enabled": False,
        }

    def __call__(self, environ: Mapping[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        try:
            app = self.load()
        except CompatibilityLoadError:
            body = json.dumps(
                {"ok": False, "error": "compatibility_surface_unavailable", "service": self.service},
                separators=(",", ":"),
            ).encode("utf-8")
            start_response(
                "503 Service Unavailable",
                [("Content-Type", "application/json"), ("Content-Length", str(len(body)))],
            )
            return [body]
        return app(environ, start_response)


def create_app(
    service: str | None = None,
    *,
    loader: AppLoader = _legacy_loader,
    inventory_path: str | Path = DEFAULT_INVENTORY_PATH,
) -> LazyCompatibilityApp:
    """Create one lazy compatibility app for a V3 gateway listener.

    V3 should instantiate this factory once for port 5002 and once for port
    5003.  No legacy module is imported until the returned WSGI app is called or
    its :meth:`load` method is explicitly invoked during readiness checks.
    """

    selected = service or os.environ.get("MAGI_V3_COMPAT_SERVICE", "")
    return LazyCompatibilityApp(
        _validate_service(selected),
        inventory=RouteInventory.load(inventory_path),
        loader=loader,
    )


def create_main_app() -> LazyCompatibilityApp:
    """Create the fixed port-5002 compatibility surface for production wiring."""

    return create_app("5002")


def create_tools_app() -> LazyCompatibilityApp:
    """Create the fixed port-5003 compatibility surface for production wiring."""

    return create_app("5003")


def inventory_report(path: str | Path = DEFAULT_INVENTORY_PATH) -> dict[str, Any]:
    inventory = RouteInventory.load(path)
    route_methods = sum(len(route.methods) for route in inventory.routes)
    conflicts: dict[tuple[str, str], list[str]] = {}
    by_rule_method: dict[tuple[str, str], set[str]] = {}
    for route in inventory.routes:
        for method in route.methods:
            by_rule_method.setdefault((route.rule, method), set()).add(route.service)
    for key, services in by_rule_method.items():
        if len(services) > 1:
            conflicts[key] = sorted(services)
    return {
        "schema_version": 1,
        "counts": inventory.counts,
        "route_methods": route_methods,
        "services": list(SUPPORTED_SERVICES),
        "cross_service_conflicts": [
            {"rule": rule, "method": method, "services": services}
            for (rule, method), services in sorted(conflicts.items())
        ],
        "adapter": "lazy_wsgi_passthrough",
        "response_transformation": False,
        "import_time_socket_or_process": False,
    }
