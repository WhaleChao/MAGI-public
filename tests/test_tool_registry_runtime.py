from __future__ import annotations

from api.tools import ToolContext, ToolRegistry, get_global_tool_registry


def test_tool_registry_register_list_and_execute():
    registry = ToolRegistry()
    registry.register_callable(
        "greet",
        lambda name="world": f"hello {name}",
        description="Greeting tool",
        permission_tag="tool:greet",
    )

    tools = registry.list_tools()
    assert tools[0]["name"] == "greet"
    assert tools[0]["permission_tag"] == "tool:greet"

    result = registry.execute("greet", {"name": "magi"}, ToolContext(user_id="u1", platform="LINE"))
    assert result.success is True
    assert result.output == "hello magi"


def test_global_tool_registry_is_importable_and_listable():
    import api.tools_api  # noqa: F401

    registry = get_global_tool_registry()
    tools = {tool["name"] for tool in registry.list_tools()}

    assert {"search", "research", "fetch"}.issubset(tools)


def test_tools_api_registered_search_uses_existing_callable(monkeypatch):
    import api.tools_api as tools_api

    monkeypatch.setattr(
        tools_api,
        "search_web",
        lambda query, num_results: {"query": query, "num_results": num_results, "results": [{"title": "ok"}]},
    )

    registry = get_global_tool_registry()
    result = registry.execute("search", {"query": "MAGI", "num_results": 2})

    assert result.success is True
    assert result.output["query"] == "MAGI"
    assert result.output["num_results"] == 2
