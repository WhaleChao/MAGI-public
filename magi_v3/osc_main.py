"""Lazy production composition for the native OSC cases route.

Importing this module reads no environment, file, database, compatibility
inventory, or V2 application module.  ``create_main_app`` is the explicit
composition boundary; connections remain deferred until a native request.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
import uuid
from datetime import timedelta
from http.cookies import SimpleCookie
from types import SimpleNamespace
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs

from .osc_cases import MariaDBCaseStore, OscCasesApplication, OscCasesService, WSGIApp


ConnectionFactory = Callable[[], Any]
UserLoader = Callable[[str], Any]


def _env_truthy(environ: Mapping[str, str], name: str) -> bool:
    return str(environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _https_enforced(environ: Mapping[str, str]) -> bool:
    if _env_truthy(environ, "MAGI_FORCE_HTTPS"):
        return True
    public_https = any(
        str(environ.get(name) or "").strip().lower().startswith("https://")
        for name in ("MAGI_PUBLIC_BASE_URL", "MAGI_BASE_URL", "MAGI_EXTERNAL_BASE_URL")
    )
    if _env_truthy(environ, "MAGI_SECURE_COOKIES") and public_https:
        return True
    deployment = str(environ.get("MAGI_DEPLOYMENT_MODE") or "").strip().lower()
    return deployment in {
        "production",
        "prod",
        "saas",
        "formal_saas",
        "managed_saas",
        "multi_tenant_saas",
    } and public_https


class V2SecurityHeaderPolicy:
    """Pure WSGI equivalent of ``api.app_factory.install_security_headers``."""

    _CORE = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "SAMEORIGIN",
        "X-XSS-Protection": "1; mode=block",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "media-src 'self' blob:; frame-src 'self' blob:; object-src 'self' blob:;"
        ),
    }

    def __init__(self, environ: Mapping[str, str]) -> None:
        self.environ = dict(environ)
        self.hsts = _https_enforced(self.environ) or _env_truthy(
            self.environ, "MAGI_ENABLE_HSTS"
        )

    def __call__(self, request_environ: Mapping[str, Any]) -> dict[str, str]:
        headers = dict(self._CORE)
        if not str(request_environ.get("PATH_INFO") or "").startswith("/static/"):
            headers["Cache-Control"] = "no-store"
        if self.hsts:
            headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return headers


def _cookie_value(environ: Mapping[str, Any], name: str) -> str:
    cookie = SimpleCookie()
    try:
        cookie.load(str(environ.get("HTTP_COOKIE") or ""))
    except Exception:
        return ""
    morsel = cookie.get(name)
    return str(morsel.value if morsel is not None else "").strip()


class FlaskSessionAuthorizer:
    """Validate the same signed ``session`` cookie used by Flask-Login."""

    def __init__(
        self,
        secret_key: str,
        user_loader: UserLoader,
        *,
        cookie_name: str = "session",
        secret_key_fallbacks: tuple[str, ...] = (),
        max_age_seconds: int = int(timedelta(days=31).total_seconds()),
    ) -> None:
        from flask.sessions import SecureCookieSessionInterface
        from itsdangerous import BadSignature

        if not secret_key:
            raise ValueError("Flask session secret is required")
        app = SimpleNamespace(
            secret_key=secret_key,
            config={"SECRET_KEY_FALLBACKS": list(secret_key_fallbacks)},
        )
        serializer = SecureCookieSessionInterface().get_signing_serializer(app)  # type: ignore[arg-type]
        if serializer is None:
            raise ValueError("Flask session serializer is unavailable")
        self.serializer = serializer
        self.user_loader = user_loader
        self.cookie_name = cookie_name
        self.max_age_seconds = max_age_seconds
        self.bad_signature = BadSignature

    def __call__(self, environ: Mapping[str, Any]) -> bool:
        encoded = _cookie_value(environ, self.cookie_name)
        if not encoded:
            return False
        try:
            session = self.serializer.loads(encoded, max_age=self.max_age_seconds)
        except self.bad_signature:
            return False
        if not isinstance(session, dict):
            return False
        user_id = str(session.get("_user_id") or "").strip()
        if not user_id:
            return False
        try:
            user = self.user_loader(user_id)
        except Exception:
            return False
        if isinstance(user, Mapping):
            return bool(user.get("id")) and bool(user.get("is_active", True))
        return bool(user is not None and getattr(user, "is_authenticated", True))


class MariaDBUserLoader:
    """Equivalent of the V2 Flask-Login ``users WHERE id`` lookup."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self.connection_factory = connection_factory

    def __call__(self, user_id: str) -> dict[str, Any] | None:
        supplied = self.connection_factory()
        connection = supplied[0] if isinstance(supplied, tuple) else supplied
        try:
            try:
                cursor = connection.cursor(dictionary=True)
            except TypeError:
                cursor = connection.cursor()
            try:
                cursor.execute(
                    "SELECT id, username, role FROM users WHERE id = %s LIMIT 1",
                    (user_id,),
                )
                row = cursor.fetchone()
                return dict(row) if row is not None else None
            finally:
                cursor.close()
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()


class EnvironmentApiKeyAuthorizer:
    """Match V2's X-API-Key/Bearer extraction without importing Flask globals."""

    def __init__(
        self,
        keys: tuple[str, ...],
        *,
        allow_query_key: bool = False,
    ) -> None:
        self.keys = tuple(dict.fromkeys(key for key in keys if key))
        self.allow_query_key = allow_query_key

    def __call__(self, environ: Mapping[str, Any]) -> bool:
        provided = str(environ.get("HTTP_X_API_KEY") or "").strip()
        if not provided:
            authorization = str(environ.get("HTTP_AUTHORIZATION") or "").strip()
            if authorization.lower().startswith("bearer "):
                provided = authorization[7:].strip()
        if not provided and self.allow_query_key:
            provided = str(
                (parse_qs(str(environ.get("QUERY_STRING") or "")).get("api_key") or [""])[0]
            ).strip()
        return bool(provided) and any(
            hmac.compare_digest(provided, expected) for expected in self.keys
        )


class DoubleSubmitCsrfProtection:
    """V2-equivalent CSRF rules for the native route."""

    cookie_name = "X-CSRF-Token"

    def __init__(
        self,
        *,
        api_key_authorizer: Callable[[Mapping[str, Any]], bool],
        token_factory: Callable[[], str] = lambda: secrets.token_hex(32),
    ) -> None:
        self.api_key_authorizer = api_key_authorizer
        self.token_factory = token_factory

    def validate(self, environ: Mapping[str, Any]) -> tuple[bool, str]:
        if self.api_key_authorizer(environ):
            return True, "exempt"
        cookie_token = _cookie_value(environ, self.cookie_name)
        request_token = str(environ.get("HTTP_X_CSRF_TOKEN") or "").strip()
        if not cookie_token:
            return False, "csrf_token_missing_in_cookie"
        if not request_token:
            return False, "csrf_token_missing_in_request"
        if not hmac.compare_digest(cookie_token, request_token):
            return False, "csrf_token_mismatch"
        return True, "valid"

    def safe_response_cookie(self, environ: Mapping[str, Any]) -> str | None:
        from werkzeug.http import dump_cookie

        token = _cookie_value(environ, self.cookie_name) or str(self.token_factory()).strip()
        if not token:
            return None
        return dump_cookie(
            self.cookie_name,
            token,
            max_age=24 * 60 * 60,
            httponly=False,
            samesite="Lax",
        )


class MariaDBLawyerResolver:
    """Resolve the same regular/debt setting keys used by the V2 helper."""

    _DEMO = frozenset(
        {"範例律師", "示範律師", "測試律師", "Sample Lawyer", "Demo Lawyer"}
    )
    _DEBT_MARKERS = ("消費者債務清理", "消債", "更生", "清算")
    _SETTING_KEYS = (
        "default_debt_lawyer",
        "consumer_debt_lawyer",
        "debt_lawyer",
        "default_specialist",
        "default_lawyer",
        "lawyer_name",
    )

    def __init__(
        self,
        connection_factory: ConnectionFactory,
        environ: Mapping[str, str],
        *,
        cache_ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.connection_factory = connection_factory
        self.environ = environ
        self.cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        self.clock = clock
        self._cache: dict[str, str] = {}
        self._cache_expires_at = 0.0
        self._cache_lock = threading.Lock()

    def __call__(self, current: str, case_type: str, reason: str, category: str) -> str:
        current = str(current or "").strip()
        if current and current not in self._DEMO:
            return current
        debt = any(marker in f"{case_type} {reason} {category}" for marker in self._DEBT_MARKERS)
        setting_keys = (
            ("default_debt_lawyer", "consumer_debt_lawyer", "debt_lawyer", "default_specialist")
            if debt
            else ("default_lawyer", "lawyer_name")
        )
        settings = self._settings()
        for key in setting_keys:
            value = settings.get(key, "")
            if value and value not in self._DEMO:
                return value
        environment_keys = (
            ("MAGI_DEFAULT_DEBT_LAWYER", "MAGI_CONSUMER_DEBT_LAWYER")
            if debt
            else ("MAGI_DEFAULT_LAWYER", "MAGI_PUBLIC_LAWYER_NAME", "MAGI_LAWYER_NAME")
        )
        for key in environment_keys:
            value = str(self.environ.get(key) or "").strip()
            if value and value not in self._DEMO:
                return value
        return ""

    def cache_info(self) -> dict[str, Any]:
        with self._cache_lock:
            return {
                "size": len(self._cache),
                "maximum_size": len(self._SETTING_KEYS),
                "expires_at": self._cache_expires_at,
            }

    def _settings(self) -> dict[str, str]:
        now = self.clock()
        if now < self._cache_expires_at:
            return dict(self._cache)
        with self._cache_lock:
            now = self.clock()
            if now < self._cache_expires_at:
                return dict(self._cache)
            loaded = self._load_settings()
            self._cache = {
                key: str(loaded.get(key) or "").strip()
                for key in self._SETTING_KEYS
                if str(loaded.get(key) or "").strip()
            }
            self._cache_expires_at = now + self.cache_ttl_seconds
            return dict(self._cache)

    def _load_settings(self) -> dict[str, str]:
        supplied = self.connection_factory()
        connection = supplied[0] if isinstance(supplied, tuple) else supplied
        try:
            try:
                cursor = connection.cursor(dictionary=True)
            except TypeError:
                cursor = connection.cursor()
            try:
                placeholders = ",".join("%s" for _ in self._SETTING_KEYS)
                cursor.execute(
                    f"SELECT `key` AS setting_key, value FROM settings WHERE `key` IN ({placeholders})",
                    self._SETTING_KEYS,
                )
                rows = cursor.fetchall() or []
                return {
                    str(dict(row).get("setting_key") or "").strip(): str(
                        dict(row).get("value") or ""
                    ).strip()
                    for row in rows
                    if str(dict(row).get("setting_key") or "").strip() in self._SETTING_KEYS
                }
            finally:
                cursor.close()
        except Exception:
            return {}
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()


class ConfiguredPathCanonicalizer:
    """String-only path mapping; never probes a local/NAS filesystem."""

    def __init__(self, mappings: tuple[tuple[str, str], ...] = ()) -> None:
        self.mappings = tuple(
            (source.replace("\\", "/").rstrip("/"), target.replace("\\", "/").rstrip("/"))
            for source, target in mappings
            if source and target
        )

    def __call__(self, value: str) -> str:
        normalized = str(value or "").strip().replace("\\", "/")
        if not normalized:
            return ""
        if len(normalized) >= 2 and normalized[1] == ":":
            return normalized.replace("/", "\\")
        for source, target in self.mappings:
            if normalized.lower() == source.lower() or normalized.lower().startswith(
                (source + "/").lower()
            ):
                relative = normalized[len(source) :].lstrip("/")
                return (target if not relative else f"{target}/{relative}").replace("/", "\\")
        return normalized


class ConfiguredPathLocalizer:
    """Reverse configured canonical mappings without probing the filesystem."""

    def __init__(self, mappings: tuple[tuple[str, str], ...] = ()) -> None:
        self.mappings = tuple(
            (source.replace("\\", "/").rstrip("/"), target.replace("\\", "/").rstrip("/"))
            for source, target in mappings
            if source and target
        )

    def __call__(self, value: str) -> str:
        normalized = str(value or "").strip().replace("\\", "/")
        if not normalized:
            return ""
        for source, target in self.mappings:
            if normalized.lower() == target.lower() or normalized.lower().startswith(
                (target + "/").lower()
            ):
                relative = normalized[len(target) :].lstrip("/")
                return source if not relative else f"{source}/{relative}"
        return normalized


def _legacy_connection_factory() -> Any:
    # Deliberately deferred: importing api.osc.utils reads legacy environment.
    from api.osc.utils import _osc_web_connect

    return _osc_web_connect()


def _compat_main_factory() -> WSGIApp:
    from magi_v3.compat import create_main_app

    return create_main_app()


def create_main_app(
    *,
    connection_factory: ConnectionFactory = _legacy_connection_factory,
    fallback_factory: Callable[[], WSGIApp] = _compat_main_factory,
    environ: Mapping[str, str] | None = None,
    path_mappings: tuple[tuple[str, str], ...] = (),
    token_factory: Callable[[], str] = lambda: secrets.token_hex(32),
) -> OscCasesApplication:
    """Explicitly compose native OSC cases over the remaining lazy V2 surface."""

    env = dict(os.environ if environ is None else environ)
    if not path_mappings:
        raw_mappings = str(env.get("MAGI_V3_PATH_MAPPINGS_JSON") or "").strip()
        if raw_mappings:
            try:
                parsed_mappings = json.loads(raw_mappings)
            except json.JSONDecodeError as exc:
                raise ValueError("MAGI_V3_PATH_MAPPINGS_JSON is invalid") from exc
            if (
                not isinstance(parsed_mappings, list)
                or not parsed_mappings
                or any(
                    not isinstance(row, list)
                    or len(row) != 2
                    or any(not isinstance(item, str) or not item.strip() for item in row)
                    for row in parsed_mappings
                )
            ):
                raise ValueError("MAGI_V3_PATH_MAPPINGS_JSON must contain non-empty path pairs")
            path_mappings = tuple((row[0], row[1]) for row in parsed_mappings)
    secret_key = str(env.get("FLASK_SECRET_KEY") or "")
    user_loader = MariaDBUserLoader(connection_factory)
    authorizer = FlaskSessionAuthorizer(secret_key, user_loader)
    api_key_authorizer = EnvironmentApiKeyAuthorizer(
        (str(env.get("MAGI_API_KEY") or "").strip(),),
        allow_query_key=str(env.get("MAGI_ALLOW_QUERY_API_KEY") or "").lower()
        in {"1", "true", "yes", "on"},
    )

    def csrf_exemption(request_environ: Mapping[str, Any]) -> bool:
        if api_key_authorizer(request_environ):
            return True
        allow_cli = str(env.get("MAGI_CSRF_ALLOW_CLI") or "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        client = str(
            request_environ.get("HTTP_X_MAGI_CLIENT")
            or request_environ.get("HTTP_X_CLIENT")
            or ""
        ).strip().lower()
        marker = str(request_environ.get("HTTP_X_MAGI_CLI") or "").strip().lower()
        return allow_cli and client in {"cli", "magi-cli"} and marker in {
            "1",
            "true",
            "yes",
            "on",
        }

    csrf = DoubleSubmitCsrfProtection(
        api_key_authorizer=csrf_exemption,
        token_factory=token_factory,
    )
    canonicalizer = ConfiguredPathCanonicalizer(path_mappings)
    side_effects = None
    if str(env.get("MAGI_V3_CASE_ROOT") or "").strip() or str(
        env.get("MAGI_V3_ARCHIVE_ROOT") or ""
    ).strip():
        from .case_filesystem import NativeCaseFilesystemEffects

        side_effects = NativeCaseFilesystemEffects.from_environment(
            env,
            canonicalize=canonicalizer,
            localize=ConfiguredPathLocalizer(path_mappings),
        )
    service = OscCasesService(
        MariaDBCaseStore(connection_factory),
        id_factory=lambda: f"web-{uuid.uuid4().hex[:12]}",
        path_canonicalizer=canonicalizer,
        lawyer_resolver=MariaDBLawyerResolver(connection_factory, env),
        post_persist=side_effects or (lambda _tx, _result, _payload: None),
        side_effects_enabled=side_effects is not None,
    )
    return OscCasesApplication(
        service,
        authorize=authorizer,
        csrf=csrf,
        response_security_headers=V2SecurityHeaderPolicy(env),
        fallback=fallback_factory(),
    )
