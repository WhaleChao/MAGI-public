"""test_error_classifier.py"""
import pytest

from skills.engine.error_classifier import classify_error, FailoverReason


def test_omlx_not_ready():
    ce = classify_error("Connection refused", provider="omlx")
    assert ce.reason == FailoverReason.OMLX_NOT_READY
    assert ce.retryable is True
    assert ce.should_fallback is True


def test_omlx_oom():
    ce = classify_error("Model exceeds max-model-memory", provider="omlx")
    assert ce.reason == FailoverReason.OMLX_OOM
    assert ce.retryable is False
    assert ce.should_fallback is True


def test_rate_limit_429():
    ce = classify_error({"error": "rate limited", "status_code": 429})
    assert ce.reason == FailoverReason.RATE_LIMIT
    assert ce.retryable is True


def test_context_overflow():
    ce = classify_error("This model's maximum context length is 8192 tokens")
    assert ce.reason == FailoverReason.CONTEXT_OVERFLOW
    assert ce.retryable is True
    assert ce.should_compress is True


def test_timeout():
    ce = classify_error(TimeoutError("Read timed out"))
    assert ce.reason == FailoverReason.TIMEOUT
    assert ce.retryable is True


def test_auth_failure():
    ce = classify_error({"error": "Invalid API key"}, http_status=401)
    assert ce.reason == FailoverReason.AUTH
    assert ce.should_rotate_credential is True


def test_billing_permanent():
    ce = classify_error({"error": "Insufficient credits"}, http_status=402)
    assert ce.reason == FailoverReason.BILLING
    assert ce.retryable is False
    assert ce.should_fallback is True


def test_billing_transient():
    ce = classify_error({"error": "Usage limit resets in 5 minutes"}, http_status=402)
    assert ce.reason == FailoverReason.RATE_LIMIT
    assert ce.retryable is True


def test_model_not_found():
    ce = classify_error({"error": "model not found"}, http_status=404)
    assert ce.reason == FailoverReason.MODEL_NOT_FOUND
    assert ce.should_fallback is True


def test_server_error_500():
    ce = classify_error({"error": "Internal server error"}, http_status=500)
    assert ce.reason == FailoverReason.SERVER_ERROR
    assert ce.retryable is True


def test_unknown_fallback():
    ce = classify_error("Something weird happened")
    assert ce.reason == FailoverReason.UNKNOWN
    assert ce.retryable is True


def test_format_error():
    ce = classify_error({"error": "Bad request"}, http_status=400)
    assert ce.reason == FailoverReason.FORMAT_ERROR
    assert ce.retryable is False
