from api.startup import (
    _inprocess_laf_gmail_monitor_enabled,
    _startup_feature_enabled,
)


def test_startup_prefetch_is_lazy_by_default(monkeypatch):
    monkeypatch.delenv("MAGI_WARMUP_OMLX_ON_START", raising=False)
    assert _startup_feature_enabled("MAGI_WARMUP_OMLX_ON_START") is False


def test_startup_prefetch_accepts_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("MAGI_WARMUP_OMLX_ON_START", "yes")
    assert _startup_feature_enabled("MAGI_WARMUP_OMLX_ON_START") is True


def test_startup_prefetch_rejects_false_like_values(monkeypatch):
    monkeypatch.setenv("MAGI_PRELOAD_FAISS_ON_START", "false")
    assert _startup_feature_enabled("MAGI_PRELOAD_FAISS_ON_START", default=True) is False


def test_inprocess_laf_gmail_monitor_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MAGI_ENABLE_INPROCESS_LAF_GMAIL_MONITOR", raising=False)
    assert _inprocess_laf_gmail_monitor_enabled() is False


def test_inprocess_laf_gmail_monitor_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("MAGI_ENABLE_INPROCESS_LAF_GMAIL_MONITOR", "true")
    assert _inprocess_laf_gmail_monitor_enabled() is True
