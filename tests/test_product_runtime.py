from __future__ import annotations

from api import product_runtime


def test_get_product_profile_merges_defaults_and_config(monkeypatch):
    monkeypatch.setattr(product_runtime, "load_product_runtime", lambda: {"file_review": {"codex_mode": "local"}})
    config = {
        "product_runtime": {
            "file_review": {
                "codex_mode": "codex",
            }
        }
    }

    profile = product_runtime.get_product_profile("file_review", config=config)

    assert profile["codex_mode"] == "local"


def test_resolve_laf_portal_targets_compare_mode():
    profile = {
        "codex_mode": "auto",
        "portal_env": "compare",
        "prod_base_url": "https://lawyer.laf.org.tw",
        "test_base_url": "http://127.0.0.1:17002",
        "compare_base_url": "",
    }

    targets = product_runtime.resolve_laf_portal_targets(config={}, profile=profile)

    assert targets["execute_env"] == "test"
    assert targets["execute_base_url"] == "http://127.0.0.1:17002"
    assert targets["execute_mock_mode"] is True
    assert targets["compare_enabled"] is True
    assert targets["compare_base_url"] == "https://lawyer.laf.org.tw"


def test_apply_product_runtime_env_sets_context_and_laf_portal(monkeypatch):
    monkeypatch.setattr(
        product_runtime,
        "get_product_profile",
        lambda product, config=None: {
            "codex_mode": "codex",
            "portal_env": "test",
            "prod_base_url": "https://lawyer.laf.org.tw",
            "test_base_url": "http://127.0.0.1:17002",
            "compare_base_url": "",
        } if product == "laf" else {"codex_mode": "codex"},
    )

    env = {}
    info = product_runtime.apply_product_runtime_env("laf", env=env, config={})

    assert env["MAGI_CODEX_CONTEXT"] == "laf"
    assert env["MAGI_CODEX_CONTEXT_MODE"] == "codex"
    assert env["MAGI_LAF_PORTAL_ENV"] == "test"
    assert env["LAF_BASE_URL"] == "http://127.0.0.1:17002"
    assert env["LAF_MOCK_MODE"] == "1"
    assert info["portal"]["execute_env"] == "test"
