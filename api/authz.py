"""
MAGI Unified Authorization Module
===================================

Provides:
  - Role-based decorators (@require_role, @require_api_key)
  - Audit logging for access control
  - Centralized policy enforcement
  - Replaces scattered role checks throughout codebase

Roles:
  - admin: Full access to all endpoints and operations
  - operator: Access to operational endpoints (status, logs, etc.)
  - viewer: Read-only access to non-sensitive data
"""

import os
import logging
import hmac
from functools import wraps
from flask import request, jsonify, current_app
from flask_login import current_user

logger = logging.getLogger(__name__)

# ── Environment Configuration ────────────────────────────────────────
MAGI_API_KEY = os.environ.get("MAGI_API_KEY", "").strip()
MAGI_EXTERNAL_API_KEY = os.environ.get("MAGI_EXTERNAL_API_KEY", "").strip()
REQUIRE_API_KEY = (
    os.environ.get("MAGI_API_KEY_REQUIRED", "1").strip().lower()
    not in {"0", "false", "no", "off"}
)


def _get_calling_user_id() -> str:
    """Extract user ID from Flask request context."""
    if current_user and current_user.is_authenticated:
        return str(getattr(current_user, "id", "unknown"))
    return "anonymous"


def _log_access(endpoint: str, user_id: str, role: str, allowed: bool, reason: str = "") -> None:
    """Log access attempts for audit trail."""
    status = "ALLOWED" if allowed else "DENIED"
    reason_str = f" ({reason})" if reason else ""
    logger.info(
        f"authz_event: endpoint={endpoint} user_id={user_id} role={role} {status}{reason_str}"
    )


def _check_api_key(provided: str) -> bool:
    """Validate provided API key against configured key(s)."""
    if not provided:
        return False
    if not MAGI_API_KEY:
        return False
    return hmac.compare_digest(provided, MAGI_API_KEY)


def _extract_api_key() -> str:
    """
    Extract API key from request.

    Priority:
      1. X-API-Key header
      2. api_key query parameter
      3. Authorization: Bearer <key> header
    """
    # Header: X-API-Key
    key = (request.headers.get("X-API-Key") or "").strip()
    if key:
        return key

    # Query parameter: api_key
    key = (request.args.get("api_key") or "").strip()
    if key:
        return key

    # Header: Authorization: Bearer <key>
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()

    return ""


def require_api_key(f):
    """
    Decorator: Requires valid API key (X-API-Key header or api_key query param).

    Usage:
        @app.route('/admin/reset', methods=['POST'])
        @require_api_key
        def admin_reset():
            ...
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        provided = _extract_api_key()
        user_id = _get_calling_user_id()
        endpoint = request.endpoint or "unknown"

        if not provided:
            _log_access(endpoint, user_id, "none", False, "no_api_key_provided")
            return jsonify({"error": "unauthorized: missing API key"}), 401

        if not _check_api_key(provided):
            _log_access(endpoint, user_id, "none", False, "invalid_api_key")
            return jsonify({"error": "unauthorized: invalid API key"}), 401

        _log_access(endpoint, user_id, "api_key", True, "api_key_verified")
        return f(*args, **kwargs)

    return decorated_function


def require_role(role: str):
    """
    Decorator: Requires user to have specified role or higher.

    Roles hierarchy: admin > operator > viewer

    Usage:
        @app.route('/admin/settings', methods=['POST'])
        @require_role("admin")
        def admin_settings():
            ...
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            endpoint = request.endpoint or "unknown"
            user_id = _get_calling_user_id()

            # Check if user is authenticated
            if not (current_user and current_user.is_authenticated):
                _log_access(endpoint, user_id, "anonymous", False, "not_authenticated")
                return jsonify({"error": "unauthorized: not authenticated"}), 401

            # Get user role (default: viewer if not set)
            user_role = getattr(current_user, "role", "viewer") or "viewer"

            # Role hierarchy: admin > operator > viewer
            role_hierarchy = {"admin": 3, "operator": 2, "viewer": 1}
            required_level = role_hierarchy.get(role, 0)
            user_level = role_hierarchy.get(user_role, 0)

            if user_level < required_level:
                _log_access(
                    endpoint, user_id, user_role, False,
                    f"insufficient_role: has {user_role}, needs {role}"
                )
                return jsonify({
                    "error": f"forbidden: role '{user_role}' insufficient (requires '{role}')"
                }), 403

            _log_access(endpoint, user_id, user_role, True)
            return f(*args, **kwargs)

        return decorated_function

    return decorator


def is_admin(user=None) -> bool:
    """
    Check if user has admin role.

    Args:
        user: Flask-Login user object (defaults to current_user if None)

    Returns:
        True if user is admin, False otherwise
    """
    if user is None:
        user = current_user
    if not user or not user.is_authenticated:
        return False
    return getattr(user, "role", "viewer") == "admin"


def is_operator_or_admin(user=None) -> bool:
    """Check if user has operator or admin role."""
    if user is None:
        user = current_user
    if not user or not user.is_authenticated:
        return False
    role = getattr(user, "role", "viewer")
    return role in {"operator", "admin"}


def check_authorization(required_role: str) -> tuple[bool, str]:
    """
    Low-level authorization check (returns status instead of raising).

    Returns:
        (allowed: bool, reason: str) tuple
    """
    if not (current_user and current_user.is_authenticated):
        return False, "not_authenticated"

    user_role = getattr(current_user, "role", "viewer") or "viewer"
    role_hierarchy = {"admin": 3, "operator": 2, "viewer": 1}
    required_level = role_hierarchy.get(required_role, 0)
    user_level = role_hierarchy.get(user_role, 0)

    if user_level < required_level:
        return False, f"insufficient_role: has {user_role}, needs {required_role}"

    return True, "authorized"
