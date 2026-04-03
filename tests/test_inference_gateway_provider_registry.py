from __future__ import annotations

from providers.base import ProviderHealth


def test_inference_gateway_exposes_provider_registry_helpers(monkeypatch):
    from skills.bridge import inference_gateway as gw_mod

    class _DummyAdapter:
        name = "dummy"

        def health_check(self, *, timeout=3):
            return ProviderHealth(provider="dummy", available=True, detail="ok")

    monkeypatch.setattr(gw_mod, "_build_provider_registry", lambda session=None: {"dummy": _DummyAdapter()})

    gw = gw_mod.InferenceGateway()

    assert gw.list_provider_adapters() == ["dummy"]
    assert gw.get_provider_adapter("dummy").name == "dummy"
    snapshot = gw.provider_health_snapshot(timeout=1)
    assert snapshot["dummy"]["available"] is True
    assert snapshot["dummy"]["detail"] == "ok"
