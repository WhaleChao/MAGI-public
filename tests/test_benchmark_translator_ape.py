from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path


MAGI_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = MAGI_ROOT / "scripts" / "ops" / "benchmark_translator_ape.py"


def _load_module(monkeypatch):
    skills_mod = types.ModuleType("skills")
    engine_mod = types.ModuleType("skills.engine")
    apple_mod = types.ModuleType("skills.engine.apple_translation")
    translator_mod = types.ModuleType("skills.translator")
    ape_mod = types.ModuleType("skills.translator._apple_post_edit")
    action_mod = types.ModuleType("skills.translator.action")

    apple_mod.is_available = lambda: (True, "")
    apple_mod.translate = lambda *a, **k: {"success": True, "provider": "apple_translation", "text": "ok"}
    ape_mod.translate_with_ape = lambda *a, **k: {"provider": "apple_translation_ape", "degraded": False, "text": "ok", "validator": {"reasons": []}}
    action_mod.translate = lambda *a, **k: {"provider": "apple_translation_ape", "degraded": False, "text": "ok"}

    monkeypatch.setitem(sys.modules, "skills", skills_mod)
    monkeypatch.setitem(sys.modules, "skills.engine", engine_mod)
    monkeypatch.setitem(sys.modules, "skills.engine.apple_translation", apple_mod)
    monkeypatch.setitem(sys.modules, "skills.translator", translator_mod)
    monkeypatch.setitem(sys.modules, "skills.translator._apple_post_edit", ape_mod)
    monkeypatch.setitem(sys.modules, "skills.translator.action", action_mod)

    spec = importlib.util.spec_from_file_location("benchmark_translator_ape_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_main_fails_when_ape_output_empty_or_case_numbers_missing(monkeypatch, capsys):
    mod = _load_module(monkeypatch)
    mod.SUITE = [
        {"id": "prayer_for_relief", "zh": "x", "expect_terms_en": []},
        {"id": "case_number", "zh": "y", "expect_terms_en": []},
    ]
    mod._warmup_omlx = lambda timeout_sec=60: True
    mod._apple_avail = lambda: (True, "")
    mod._write_static_result = lambda summary: None
    mod._send_dc_alert = lambda summary: None

    def _bench_gtx(item):
        return {
            "id": item["id"],
            "stage": "gtx_primary",
            "provider": "apple_translation_ape",
            "degraded": False,
            "elapsed_ms": 1,
            "text": "ok",
            "term_hit_rate": 1.0,
        }

    def _bench_baseline(item):
        return {
            "id": item["id"],
            "stage": "apple_baseline",
            "provider": "apple_translation",
            "success": True,
            "elapsed_ms": 1,
            "text": "ok",
            "term_hit_rate": 1.0,
        }

    def _bench_ape(item):
        if item["id"] == "prayer_for_relief":
            return {
                "id": item["id"],
                "stage": "apple_ape",
                "provider": "apple_translation_failed",
                "degraded": False,
                "elapsed_ms": 1,
                "text": "",
                "validator_reasons": None,
                "term_hit_rate": 0.0,
            }
        return {
            "id": item["id"],
            "stage": "apple_ape",
            "provider": "apple_translation_baseline",
            "degraded": True,
            "elapsed_ms": 1,
            "text": "missing case id",
            "validator_reasons": ["numbers_missing", "case_numbers_missing"],
            "term_hit_rate": 0.0,
        }

    mod._bench_gtx = _bench_gtx
    mod._bench_apple_baseline = _bench_baseline
    mod._bench_ape = _bench_ape

    rc = mod.main()
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)

    assert rc == 1
    assert payload["success"] is False
    assert payload["has_failures"] is True
    assert payload["case_fail_count"] == 2
