from __future__ import annotations

import json
import sys
import types

import pytest


@pytest.fixture()
def tools_api_module(monkeypatch):
    web_research = types.ModuleType("skills.research.web_research")
    web_research.search_web = lambda query, num_results=5: {"results": []}
    web_research.research_topic = lambda *args, **kwargs: {"success": True, "results": []}
    web_research.fetch_url_content = lambda *args, **kwargs: {"success": True, "content": ""}
    monkeypatch.setitem(sys.modules, "skills.research.web_research", web_research)

    balthasar_bridge = types.ModuleType("skills.bridge.balthasar_bridge")
    balthasar_bridge.summarize_text = lambda *args, **kwargs: {"success": True, "text": ""}
    balthasar_bridge.check_health = lambda *args, **kwargs: (False, "stubbed")
    monkeypatch.setitem(sys.modules, "skills.bridge.balthasar_bridge", balthasar_bridge)

    import api.tools_api as tools_api

    return tools_api


def _write_skill(root, name: str, description: str = "demo skill") -> None:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n",
        encoding="utf-8",
    )


def test_skill_list_hides_generated_dirs_and_collapses_shims(tmp_path):
    from api.pipelines.skill_listing import build_skill_list_response

    for name in (
        "code-generated-noise",
        "iron_dome",
        "iron-dome",
        "osc_orchestrator",
        "osc-orchestrator",
        "pdf-namer",
    ):
        _write_skill(tmp_path, name)

    response = build_skill_list_response(str(tmp_path))

    assert "code-generated-noise" not in response
    assert "**iron_dome**" not in response
    assert "**osc_orchestrator**" not in response
    assert "**iron-dome**" in response
    assert "**osc-orchestrator**" in response
    assert "**pdf-namer**" in response


def test_skill_genesis_list_skills_uses_public_catalog_filter(tmp_path, monkeypatch):
    from skills.evolution import skill_genesis

    for name in ("code-noise", "iron_dome", "iron-dome", "osc_orchestrator", "osc-orchestrator"):
        _write_skill(tmp_path, name)

    monkeypatch.setattr(skill_genesis, "SKILLS_DIR", str(tmp_path / "skills"))
    names = {item["folder"] for item in skill_genesis.list_skills()}

    assert "code-noise" not in names
    assert "iron_dome" not in names
    assert "osc_orchestrator" not in names
    assert {"iron-dome", "osc-orchestrator"} <= names


def test_tools_api_definitions_hide_generated_and_shim_tools(tools_api_module, monkeypatch):
    tools_api = tools_api_module
    monkeypatch.setattr(tools_api, "_discover_runnable_skill_dirs", lambda: {"translator", "iron-dome"})

    payload = {
        "tools": [
            {"name": "run_code_tests_noise", "endpoint": "/skills/run"},
            {
                "name": "run_iron_dome",
                "endpoint": "/skills/run",
                "parameters": {"properties": {"skill": {"default": "iron-dome"}}},
            },
            {
                "name": "run_translator",
                "endpoint": "/skills/run",
                "parameters": {"properties": {"skill": {"default": "translator"}}},
            },
        ]
    }

    sanitized = tools_api._sanitize_definitions_payload(json.loads(json.dumps(payload)))
    names = [tool["name"] for tool in sanitized["tools"]]

    assert names == ["run_translator"]
    assert sanitized["_meta"]["runtime_filter"]["dropped_hidden_tools"] == 2


def test_semantic_router_skips_deprecated_pdf_annotate(tmp_path, monkeypatch):
    from skills.bridge import semantic_router

    definitions = {
        "tools": [
            {
                "name": "pdf_annotate",
                "description": "Automatically annotate PDFs with labels",
                "endpoint": "/skills/run",
            },
            {
                "name": "web_search",
                "description": "Search the web",
                "endpoint": "/search",
            },
        ]
    }
    defs_path = tmp_path / "definitions.json"
    defs_path.write_text(json.dumps(definitions), encoding="utf-8")

    monkeypatch.setattr(semantic_router, "_DEFINITIONS_PATH", str(defs_path))
    monkeypatch.setattr(semantic_router, "_SKILLS_CACHE", None)
    monkeypatch.setattr(semantic_router, "_SKILLS_CACHE_TS", 0.0)
    monkeypatch.setattr(semantic_router, "_LLM_ENABLED", False)

    assert all(item["name"] != "pdf_annotate" for item in semantic_router._load_skills())
    assert semantic_router.route("PDF標籤") is None
    assert "pdf-bookmarker" in semantic_router.deprecated_route_hint("請幫 PDF 標籤")


def test_semantic_router_respects_explicit_no_tool_request(tmp_path, monkeypatch):
    from skills.bridge import semantic_router

    definitions = {
        "tools": [
            {
                "name": "web_search",
                "description": "Search the web for current information",
                "endpoint": "/search",
            },
            {
                "name": "query_clients",
                "description": "查詢案件與客戶資料",
                "endpoint": "/skills/run",
                "parameters": {"properties": {"skill": {"default": "case-query"}}},
            },
        ]
    }
    defs_path = tmp_path / "definitions.json"
    defs_path.write_text(json.dumps(definitions), encoding="utf-8")

    monkeypatch.setattr(semantic_router, "_DEFINITIONS_PATH", str(defs_path))
    monkeypatch.setattr(semantic_router, "_SKILLS_CACHE", None)
    monkeypatch.setattr(semantic_router, "_SKILLS_CACHE_TS", 0.0)
    monkeypatch.setattr(semantic_router, "_LLM_ENABLED", False)

    assert semantic_router.route("只是聊天，不要查案件，也不要調工具") is None
