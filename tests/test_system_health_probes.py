from __future__ import annotations

import importlib.util
import sys
import types
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest


MAGI_ROOT = Path(__file__).resolve().parent.parent
MAGI_DOCTOR_PATH = MAGI_ROOT / "skills" / "magi-doctor" / "action.py"
MAGI_WORLDMONITOR_PATH = MAGI_ROOT / "skills" / "worldmonitor-intel" / "action.py"
MAGI_MARKET_BRIEFING_PATH = MAGI_ROOT / "skills" / "market-briefing" / "action.py"
MAGI_SYSTEM_TEST_PATH = MAGI_ROOT / "skills" / "ops" / "system_test.py"


def _load_magi_doctor_module():
    spec = importlib.util.spec_from_file_location("magi_doctor_test", MAGI_DOCTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_worldmonitor_module():
    spec = importlib.util.spec_from_file_location("worldmonitor_intel_test", MAGI_WORLDMONITOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_market_briefing_module():
    spec = importlib.util.spec_from_file_location("market_briefing_test", MAGI_MARKET_BRIEFING_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_system_test_module():
    spec = importlib.util.spec_from_file_location("system_test_module_test", MAGI_SYSTEM_TEST_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _mock_models_response(status_code=200, models=None):
    """Create a mock requests.get response for /v1/models."""
    if models is None:
        models = [{"id": "gemma-4-e4b-it-4bit"}]
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"data": models}
    return resp


@pytest.mark.parametrize(
    "payload, expected",
    [
        (
            {"object": "list", "data": [{"id": "gemma-4-e4b-it-4bit"}, {"id": "Qwen2.5-Coder-14B"}]},
            ["gemma-4-e4b-it-4bit", "Qwen2.5-Coder-14B"],
        ),
        (
            {"object": "list", "models": [{"name": "gemma-4-e4b-it-4bit"}, {"model": "Qwen2.5-Coder-14B"}]},
            ["gemma-4-e4b-it-4bit", "Qwen2.5-Coder-14B"],
        ),
        (
            [{"id": "gemma-4-e4b-it-4bit"}, "Qwen2.5-Coder-14B"],
            ["gemma-4-e4b-it-4bit", "Qwen2.5-Coder-14B"],
        ),
    ],
)
def test_shared_health_probe_extract_model_labels_normalizes_payloads(payload, expected):
    from skills.ops import health_probes

    assert health_probes.extract_model_labels(payload) == expected


def test_shared_health_probe_local_chat_retries_after_timeout(monkeypatch):
    from skills.ops import health_probes

    mock_models = _mock_models_response(200, [{"id": "gemma-4-e4b-it-4bit"}])
    success_resp = MagicMock()
    success_resp.status_code = 200
    success_resp.json.return_value = {"choices": [{"message": {"content": "OK"}}]}
    post_calls = []

    def fake_post(url, json=None, timeout=30):
        post_calls.append(timeout)
        if len(post_calls) == 1:
            raise TimeoutError("Read timed out")
        return success_resp

    fake_requests = types.ModuleType("requests")
    fake_requests.get = MagicMock(return_value=mock_models)
    fake_requests.post = fake_post
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    monkeypatch.setattr(health_probes.time, "sleep", lambda *_: None)

    result = health_probes.probe_local_chat(timeout_sec=30, retries=2)

    assert result["pass"] is True
    assert result["model"] == "gemma-4-e4b-it-4bit"
    assert len(post_calls) == 2


def test_system_test_omlx_uses_models_endpoint(monkeypatch):
    from skills.ops import system_test

    mock_resp = _mock_models_response(200, [{"id": "gemma-4-e4b-it-4bit"}, {"id": "Qwen2.5-Coder-14B"}])
    fake_requests = types.ModuleType("requests")
    fake_requests.get = MagicMock(return_value=mock_resp)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    result = system_test.test_casper_ollama()

    assert result["pass"] is True
    assert "2 models" in result["detail"]


def test_system_test_omlx_unreachable(monkeypatch):
    from skills.ops import system_test

    def raise_err(url, timeout=5):
        raise ConnectionError("refused")

    fake_requests = types.ModuleType("requests")
    fake_requests.get = raise_err
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    result = system_test.test_casper_ollama()

    assert result["pass"] is False


@pytest.mark.parametrize(
    "payload, expected",
    [
        (
            {"object": "list", "data": [{"id": "gemma-4-e4b-it-4bit"}, {"id": "Qwen2.5-Coder-14B"}]},
            ["gemma-4-e4b-it-4bit", "Qwen2.5-Coder-14B"],
        ),
        (
            {"object": "list", "models": [{"name": "gemma-4-e4b-it-4bit"}, {"model": "Qwen2.5-Coder-14B"}]},
            ["gemma-4-e4b-it-4bit", "Qwen2.5-Coder-14B"],
        ),
        (
            [{"id": "gemma-4-e4b-it-4bit"}, "Qwen2.5-Coder-14B"],
            ["gemma-4-e4b-it-4bit", "Qwen2.5-Coder-14B"],
        ),
    ],
)
def test_worldmonitor_extract_model_labels_normalizes_omlx_payloads(payload, expected):
    module = _load_worldmonitor_module()

    assert module._extract_model_labels(payload) == expected


def test_magi_doctor_runtime_paths_resolve_to_repo_root():
    module = _load_magi_doctor_module()

    assert module.MAGI_DIR == str(MAGI_ROOT)
    assert Path(module.REPORT_PATH) == MAGI_ROOT / "static" / "doctor_report.json"
    assert Path(module.SKILLS_DIR) == MAGI_ROOT / "skills"


def test_system_test_runtime_paths_resolve_to_repo_root():
    module = _load_system_test_module()

    assert module.MAGI_DIR == str(MAGI_ROOT)


def test_system_test_run_all_tests_writes_report_under_static(tmp_path, monkeypatch):
    module = _load_system_test_module()

    monkeypatch.setattr(module, "MAGI_DIR", str(tmp_path))
    monkeypatch.setattr(module, "ALL_TESTS", [("ok", "Smoke", lambda: {"pass": True, "detail": "ok"})])

    report = module.run_all_tests()

    report_path = tmp_path / "static" / "system_test_report.json"
    assert report["score"] == "1/1"
    assert report_path.exists()
    assert '"score": "1/1"' in report_path.read_text(encoding="utf-8")


def test_magi_doctor_probe_imports_requests_lazily_and_uses_models_schema(monkeypatch):
    module = _load_magi_doctor_module()

    mock_resp = _mock_models_response(200, [{"id": "gemma-4-e4b-it-4bit"}, {"id": "Qwen2.5-Coder-14B"}])
    fake_requests = types.ModuleType("requests")
    fake_requests.get = MagicMock(return_value=mock_resp)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    result = module._probe_omlx_chat(timeout_sec=8)

    assert result["pass"] is True
    assert "2 models" in result["detail"]
    assert "gemma-4-e4b-it-4bit" in result["detail"]
    assert "Qwen2.5-Coder-14B" in result["detail"]


def test_magi_doctor_repair_ollama_uses_models_probe(monkeypatch):
    module = _load_magi_doctor_module()

    mock_resp = _mock_models_response(200, [])
    fake_requests = types.ModuleType("requests")
    fake_requests.get = MagicMock(return_value=mock_resp)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    result = module._repair_ollama()

    assert result["repaired"] is False
    assert "oMLX" in result["detail"] or "空模型" in result["detail"]


def test_magi_doctor_local_llm_probe_retries_after_timeout(monkeypatch):
    module = _load_magi_doctor_module()

    mock_models = _mock_models_response(200, [{"id": "gemma-4-e4b-it-4bit"}])
    success_resp = MagicMock()
    success_resp.status_code = 200
    success_resp.json.return_value = {"choices": [{"message": {"content": "OK"}}]}
    post_calls = []

    def fake_post(url, json=None, timeout=30):
        post_calls.append(timeout)
        if len(post_calls) == 1:
            raise TimeoutError("Read timed out")
        return success_resp

    fake_requests = types.ModuleType("requests")
    fake_requests.get = MagicMock(return_value=mock_models)
    fake_requests.post = fake_post
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    monkeypatch.setattr(module.time, "sleep", lambda *_: None)

    result = module._probe_local_llm_inference(timeout_sec=30, retries=2)

    assert result["pass"] is True
    assert "[retry=2]" in result["detail"]
    assert len(post_calls) == 2


def test_worldmonitor_store_to_memory_uses_source_signature(monkeypatch, tmp_path):
    module = _load_worldmonitor_module()
    calls = []

    def remember(content, source="manual"):
        calls.append((content, source))
        return True

    fake_mem_bridge = types.ModuleType("skills.memory.mem_bridge")
    fake_mem_bridge.remember = remember

    import skills.memory as memory_pkg

    monkeypatch.setitem(sys.modules, "skills.memory.mem_bridge", fake_mem_bridge)
    monkeypatch.setattr(memory_pkg, "mem_bridge", fake_mem_bridge, raising=False)
    monkeypatch.setattr(module, "MAGI_DIR", str(tmp_path))

    assert module._store_to_memory(
        "payload",
        metadata={"news_count": 3, "market_symbols": ["AAPL", "NVDA"]},
    ) is True

    assert calls == [("payload", "worldmonitor-intel|news=3|markets=2")]


def test_worldmonitor_store_to_memory_falls_back_to_file(monkeypatch, tmp_path):
    module = _load_worldmonitor_module()

    def remember(content, source="manual"):
        raise ModuleNotFoundError("mysql")

    fake_mem_bridge = types.ModuleType("skills.memory.mem_bridge")
    fake_mem_bridge.remember = remember

    import skills.memory as memory_pkg

    monkeypatch.setitem(sys.modules, "skills.memory.mem_bridge", fake_mem_bridge)
    monkeypatch.setattr(memory_pkg, "mem_bridge", fake_mem_bridge, raising=False)
    monkeypatch.setattr(module, "MAGI_DIR", str(tmp_path))

    assert module._store_to_memory("fallback payload") is True

    report_dir = tmp_path / "static" / "worldmonitor_reports"
    reports = list(report_dir.glob("intel_*.md"))
    assert reports
    assert reports[0].read_text(encoding="utf-8") == "fallback payload"


def test_worldmonitor_collect_and_analyze_emits_degraded_report(monkeypatch):
    module = _load_worldmonitor_module()

    monkeypatch.setattr(
        module,
        "collect_news",
        lambda: ([], [{"source": "BBC World", "ok": False, "count": 0, "error": "fetch failed"}]),
    )
    monkeypatch.setattr(
        module,
        "collect_markets",
        lambda: ({}, {"ok": False, "detail": "FINNHUB_API_KEY 未設定，市場行情已停用"}),
    )

    stored = {}

    def store(content, metadata=None):
        stored["content"] = content
        stored["metadata"] = metadata
        return True

    monkeypatch.setattr(module, "_store_to_memory", store)

    report = module.collect_and_analyze(use_melchior=False)

    assert "降級模式" in report
    assert "來源健康狀態" in report
    assert "BBC World" in report
    assert stored["content"] == report


def test_worldmonitor_collect_and_analyze_uses_structured_fallback_when_melchior_fails(monkeypatch):
    module = _load_worldmonitor_module()

    monkeypatch.setattr(
        module,
        "collect_news",
        lambda: ([
            {"source": "BBC World", "title": "Hormuz tensions rise", "summary": "Shipping routes face new pressure."},
            {"source": "NHK Asia", "title": "Japan and Asia monitor regional risk", "summary": "Officials discuss energy security."},
        ], [{"source": "BBC World", "ok": True, "count": 1, "error": ""}]),
    )
    monkeypatch.setattr(
        module,
        "collect_markets",
        lambda: ({}, {"ok": False, "detail": "FINNHUB_API_KEY 未設定，市場行情已停用"}),
    )
    monkeypatch.setattr(module, "_reason_with_melchior", lambda prompt: "[推理失敗] HTTP Error 404: Not Found")
    monkeypatch.setattr(module, "_store_to_memory", lambda content, metadata=None: True)

    report = module.collect_and_analyze(use_melchior=True)

    assert "[推理失敗]" not in report
    assert "重大事件概述" in report
    assert "對台灣與亞太的潛在影響" in report


def test_worldmonitor_collect_without_reasoning_still_emits_readable_summary(monkeypatch):
    module = _load_worldmonitor_module()

    monkeypatch.setattr(
        module,
        "collect_news",
        lambda: ([
            {"source": "BBC World", "title": "Asia supply chains face pressure", "summary": "Ports and energy routes remain under scrutiny."},
        ], [{"source": "BBC World", "ok": True, "count": 1, "error": ""}]),
    )
    monkeypatch.setattr(
        module,
        "collect_markets",
        lambda: ({"SPY": {"price": 500.0, "change_pct": 0.5}}, {"ok": True, "detail": "1/1 quotes"}),
    )
    monkeypatch.setattr(module, "_reason_with_melchior", lambda prompt: "SHOULD NOT RUN")
    monkeypatch.setattr(module, "_store_to_memory", lambda content, metadata=None: True)

    report = module.collect_and_analyze(use_melchior=False)

    assert "重大事件概述" in report
    assert "對台灣與亞太的潛在影響" in report
    assert "**分析**: 來源整理" in report
    assert "SHOULD NOT RUN" not in report


def test_worldmonitor_rejects_chatty_melchior_output(monkeypatch):
    module = _load_worldmonitor_module()

    monkeypatch.setattr(
        module,
        "collect_news",
        lambda: ([
            {"source": "BBC World", "title": "Asia officials monitor trade risk", "summary": "Regional officials discuss resilience."},
        ], [{"source": "BBC World", "ok": True, "count": 1, "error": ""}]),
    )
    monkeypatch.setattr(module, "collect_markets", lambda: ({}, {"ok": True, "detail": "no symbols"}))
    monkeypatch.setattr(module, "_reason_with_melchior", lambda prompt: "好的，我是 MAGI 系統的情報分析員 Melchior。我已接收並審閱。")
    monkeypatch.setattr(module, "_store_to_memory", lambda content, metadata=None: True)

    report = module.collect_and_analyze(use_melchior=True)

    assert "我是 MAGI" not in report
    assert "我已接收" not in report
    assert "**分析**: 來源整理" in report


def test_worldmonitor_plain_text_output_strips_markdown_shell():
    module = _load_worldmonitor_module()

    plain = module.render_plain_text_report(
        """# 🌐 MAGI 全球情報摘要
**時間**: 2026-05-07 08:00:00
**分析**: 來源整理

---

## 重大事件概述
- [BBC World](https://example.test)：市場關注供應鏈。

<details><summary>原始資料</summary>
## 📰 全球新聞
- raw markdown
</details>"""
    )

    assert "# " not in plain
    assert "**" not in plain
    assert "<details>" not in plain
    assert "BBC World：市場關注供應鏈。" in plain


def test_dashboard_openclaw_button_targets_local_route():
    dashboard_path = MAGI_ROOT / "templates" / "dashboard.html"
    text = dashboard_path.read_text(encoding="utf-8")

    assert "localhost:18789" not in text
    assert "window.open('/openclaw'" in text


def test_openclaw_alias_redirects_to_magi_adjust():
    from api.server import app

    client = app.test_client()
    response = client.get("/openclaw", base_url="http://localhost", follow_redirects=False)

    assert response.status_code in {301, 302, 303, 307, 308}
    assert response.headers["Location"].endswith("/magi-adjust")


def test_worldmonitor_alias_redirects_to_intel_panel():
    from api.server import app

    client = app.test_client()
    response = client.get("/worldmonitor", base_url="http://localhost", follow_redirects=False)

    assert response.status_code in {301, 302, 303, 307, 308}
    assert response.headers["Location"].endswith("/intel")


def test_market_briefing_uses_current_python_for_skill_invocation(monkeypatch):
    module = _load_market_briefing_module()

    recorded = {}

    def fake_run(cmd, capture_output=True, timeout=None, text=None, cwd=None):
        recorded["cmd"] = cmd
        recorded["cwd"] = cwd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("MAGI_SKILL_PYTHON", "")
    monkeypatch.setattr(sys, "executable", "/opt/venv/bin/python")

    module._register_financial_crawl_targets([
        module.WatchItem(symbol="TSLA", label="TSLA", market="US"),
    ])

    assert recorded["cmd"][0] == "/opt/venv/bin/python"
    assert recorded["cmd"][1].endswith("skills/crawler-targets/action.py")
    assert recorded["cwd"] == str(MAGI_ROOT)


def test_system_test_local_llm_probe_retries_after_timeout(monkeypatch):
    from skills.ops import system_test

    mock_models = _mock_models_response(200, [{"id": "gemma-4-e4b-it-4bit"}])
    success_resp = MagicMock()
    success_resp.status_code = 200
    success_resp.json.return_value = {"choices": [{"message": {"content": "OK"}}]}
    post_calls = []

    def fake_post(url, json=None, timeout=20):
        post_calls.append(timeout)
        if len(post_calls) == 1:
            raise TimeoutError("Read timed out")
        return success_resp

    fake_requests = types.ModuleType("requests")
    fake_requests.get = MagicMock(return_value=mock_models)
    fake_requests.post = fake_post
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    monkeypatch.setattr(system_test.time, "sleep", lambda *_: None)

    result = system_test.test_local_llm_inference()

    assert result["pass"] is True
    assert "[retry=2]" in result["detail"]
    assert len(post_calls) == 2
