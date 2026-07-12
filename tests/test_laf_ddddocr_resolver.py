from __future__ import annotations


def test_laf_ddddocr_resolver_uses_legacy_fallback_without_false_warning(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators import laf_automation_v2 as laf

    package_dir = tmp_path / "ddddocr"
    compat_dir = package_dir / "compat"
    compat_dir.mkdir(parents=True)
    (compat_dir / "legacy.py").write_text(
        "class DdddOcr:\n"
        "    pass\n",
        encoding="utf-8",
    )

    class FakeSpec:
        submodule_search_locations = [str(package_dir)]

    monkeypatch.setattr(laf, "DDDDOCR_AVAILABLE", True)
    monkeypatch.setattr(laf.importlib, "import_module", lambda _name: (_ for _ in ()).throw(ImportError("broken __init__")))
    monkeypatch.setattr(laf.importlib.util, "find_spec", lambda name: FakeSpec() if name == "ddddocr" else None)
    messages: list[str] = []

    cls = laf._resolve_ddddocr_class(log=messages.append)

    assert cls is not None
    assert cls.__name__ == "DdddOcr"
    assert any("compat/legacy fallback" in message for message in messages)
    assert not any("import 失敗" in message for message in messages)
    assert not any(message.startswith("⚠️") for message in messages)
