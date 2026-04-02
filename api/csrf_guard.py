"""
MAGI CSRF Protection Module
============================

Implements double-submit cookie pattern for CSRF protection:
  - Generates CSRF tokens for forms
  - Validates tokens on POST/PUT/DELETE requests
  - Exempts webhook endpoints (LINE, Discord, Telegram)
  - Exempts API key-authenticated endpoints
  - No session dependency

The double-submit cookie pattern:
  1. Client receives CSRF token (in form or cookie)
  2. Client sends token in request header or form field
  3. Server validates: token from cookie == token from request
  4. Since attacker's site cannot read cookies, they cannot forge valid tokens
"""

import os
import logging
import secrets
from functools import wraps
from flask import request, jsonify, Response, make_response

logger = logging.getLogger(__name__)

# Configuration
CSRF_COOKIE_NAME = "X-CSRF-Token"
CSRF_TOKEN_HEADER = "X-CSRF-Token"
CSRF_TOKEN_FORM_FIELD = "csrf_token"
CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_TOKEN_LENGTH = 32  # bytes for secrets.token_hex()


# ── Webhook endpoints that should be exempt from CSRF ────────────────
CSRF_EXEMPT_PATTERNS = (
    "/line/webhook",
    "/callback",
    "/telegram/webhook",
    "/discord/webhook",
    "/remember",
    "/health",
    "/laf/",
    "/iron-dome/",
    "/mcp/",
    "/login",
    "/register",
)

# ── API endpoints exempt from CSRF (rely on API key auth) ────────────
CSRF_EXEMPT_API_PATTERNS = (
    "/osc/external/",
    "/api/",
    "/collab/",
    "/search",
    "/research",
    "/fetch",
    "/vision",
    "/summarize",
    "/sages",
    "/skills",
    "/static/",
    "/judgment",
    "/transcript",
)


def _generate_csrf_token() -> str:
    """Generate a cryptographically secure CSRF token."""
    return secrets.token_hex(CSRF_TOKEN_LENGTH)


def _is_webhook_endpoint() -> bool:
    """Check if current request is to a webhook endpoint (CSRF exempt)."""
    path = request.path.lower()
    for pattern in CSRF_EXEMPT_PATTERNS:
        if path.startswith(pattern):
            return True
    return False


def _is_api_endpoint() -> bool:
    """Check if current request is to an API endpoint (CSRF exempt if API key auth)."""
    path = request.path.lower()
    for pattern in CSRF_EXEMPT_API_PATTERNS:
        if path.startswith(pattern):
            return True
    return False


def _has_valid_api_key() -> bool:
    """Check if request has valid API key (exempts from CSRF)."""
    from api.authz import _extract_api_key, _check_api_key
    key = _extract_api_key()
    return _check_api_key(key)


def _should_check_csrf() -> bool:
    """Determine if this request should be checked for CSRF token."""
    # Safe methods (GET, HEAD, OPTIONS) don't need CSRF protection
    if request.method in CSRF_SAFE_METHODS:
        return False

    # Webhook endpoints are exempt (they use webhook signatures for auth)
    if _is_webhook_endpoint():
        return False

    # API endpoints with valid API key are exempt
    if _is_api_endpoint() and _has_valid_api_key():
        return False

    # Localhost internal calls are exempt from CSRF (service-to-service)
    remote = request.remote_addr or ""
    if _is_api_endpoint() and remote in ("127.0.0.1", "::1", "localhost"):
        return False

    return True


def _get_csrf_token_from_request() -> str:
    """
    Extract CSRF token from request.

    Priority:
      1. X-CSRF-Token header
      2. csrf_token form field
    """
    # Header: X-CSRF-Token
    token = (request.headers.get(CSRF_TOKEN_HEADER) or "").strip()
    if token:
        return token

    # Form field: csrf_token
    token = (request.form.get(CSRF_TOKEN_FORM_FIELD) or "").strip()
    if token:
        return token

    return ""


def _get_csrf_token_from_cookie() -> str:
    """Extract CSRF token from cookie."""
    return (request.cookies.get(CSRF_COOKIE_NAME) or "").strip()


def validate_csrf_token() -> tuple[bool, str]:
    """
    Validate CSRF token for current request.

    Returns:
        (valid: bool, reason: str) tuple
    """
    if not _should_check_csrf():
        return True, "exempt"

    cookie_token = _get_csrf_token_from_cookie()
    request_token = _get_csrf_token_from_request()

    if not cookie_token:
        return False, "csrf_token_missing_in_cookie"

    if not request_token:
        return False, "csrf_token_missing_in_request"

    # Constant-time comparison to prevent timing attacks
    import hmac
    if not hmac.compare_digest(cookie_token, request_token):
        return False, "csrf_token_mismatch"

    return True, "valid"


def csrf_exempt(f):
    """
    Decorator: Exempt endpoint from CSRF validation.

    Use for webhook endpoints or endpoints with their own auth (API key).

    Usage:
        @app.route('/webhook/external', methods=['POST'])
        @csrf_exempt
        def external_webhook():
            ...
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Store flag in request context so middleware can skip this endpoint
        request.environ['csrf_exempt'] = True
        return f(*args, **kwargs)

    return decorated_function


def inject_csrf_token() -> None:
    """
    Inject CSRF token into response cookie.

    Call in application setup or before rendering forms.
    Typically used in a before_request hook.
    """
    # Only inject for methods that might display forms
    if request.method not in {"GET", "HEAD"}:
        return

    # Check if cookie already exists (reuse if present)
    existing_token = request.cookies.get(CSRF_COOKIE_NAME)
    token = existing_token or _generate_csrf_token()

    # Store token for later use in before_request hook
    request.csrf_token = token


def get_csrf_token() -> str:
    """
    Get current CSRF token for this request.

    Use in templates: {{ get_csrf_token() }}

    Returns:
        CSRF token string
    """
    if not hasattr(request, 'csrf_token'):
        inject_csrf_token()
    return getattr(request, 'csrf_token', '')


def middleware_apply_csrf(app):
    """
    Apply CSRF protection middleware to Flask app.

    Call after app creation:
        app = Flask(__name__)
        middleware_apply_csrf(app)

    This middleware:
      1. Generates/injects CSRF token cookie on GET requests
      2. Validates CSRF token on POST/PUT/DELETE requests
    """

    @app.before_request
    def _before_request():
        # Inject CSRF token for all requests
        inject_csrf_token()

    @app.after_request
    def _after_request(response):
        """Add CSRF token to response cookies."""
        token = getattr(request, 'csrf_token', '')
        if token and request.method in {"GET", "HEAD"}:
            response.set_cookie(
                CSRF_COOKIE_NAME,
                token,
                httponly=False,  # Must be readable by JS (double-submit pattern)
                samesite="Lax",
                max_age=3600 * 24,  # 24 hours
            )
        return response

    @app.before_request
    def _check_csrf():
        """Validate CSRF token on state-changing requests."""
        if request.method in CSRF_SAFE_METHODS:
            return

        # Skip if explicitly exempted
        if request.environ.get('csrf_exempt'):
            return

        valid, reason = validate_csrf_token()
        if not valid:
            logger.warning(
                f"csrf_check_failed: path={request.path} method={request.method} "
                f"reason={reason} remote_addr={request.remote_addr}"
            )
            return jsonify({
                "error": "forbidden: invalid CSRF token",
                "code": "csrf_validation_failed",
                "reason": reason,
            }), 403


# ── Template helper for generating hidden form field ────────────────
def render_csrf_field() -> str:
    """
    Render hidden form field for CSRF token.

    Use in Jinja2 templates:
        <form method="POST">
            {{ render_csrf_field() | safe }}
            ... other fields ...
        </form>
    """
    token = get_csrf_token()
    return f'<input type="hidden" name="{CSRF_TOKEN_FORM_FIELD}" value="{token}">'
