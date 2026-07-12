from __future__ import annotations

from types import SimpleNamespace

from tests import conftest as magi_conftest


class _Hook:
    def __init__(self):
        self.deselected = []

    def pytest_deselected(self, *, items):
        self.deselected.extend(items)


class _Item:
    def __init__(self, name: str, *, live: bool):
        self.name = name
        self.live = live

    def get_closest_marker(self, name: str):
        if name == "live" and self.live:
            return object()
        return None


def _config(markexpr: str = "", magi_live: bool = False):
    hook = _Hook()
    config = SimpleNamespace(
        option=SimpleNamespace(markexpr=markexpr),
        hook=hook,
        getoption=lambda name: magi_live if name == "--magi-live" else False,
    )
    return config, hook


def test_live_marked_items_are_deselected_by_default(monkeypatch):
    monkeypatch.setenv("MAGI_ENABLE_LIVE_TESTS", "0")
    normal = _Item("normal", live=False)
    live = _Item("live", live=True)
    items = [normal, live]
    config, hook = _config()

    magi_conftest.pytest_collection_modifyitems(config, items)

    assert items == [normal]
    assert hook.deselected == [live]


def test_live_marked_files_are_ignored_before_import_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("MAGI_ENABLE_LIVE_TESTS", "0")
    live_file = tmp_path / "test_external_live.py"
    live_file.write_text("import pytest\npytestmark = pytest.mark.live\n", encoding="utf-8")
    config, _hook = _config()

    assert magi_conftest.pytest_ignore_collect(live_file, config) is True


def test_live_marked_files_are_collected_when_env_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("MAGI_ENABLE_LIVE_TESTS", "1")
    live_file = tmp_path / "test_external_live.py"
    live_file.write_text("import pytest\npytestmark = pytest.mark.live\n", encoding="utf-8")
    config, _hook = _config()

    assert magi_conftest.pytest_ignore_collect(live_file, config) is False


def test_live_marked_items_run_when_env_enabled(monkeypatch):
    monkeypatch.setenv("MAGI_ENABLE_LIVE_TESTS", "1")
    normal = _Item("normal", live=False)
    live = _Item("live", live=True)
    items = [normal, live]
    config, hook = _config()

    magi_conftest.pytest_collection_modifyitems(config, items)

    assert items == [normal, live]
    assert hook.deselected == []


def test_live_marked_items_run_for_explicit_marker_expression(monkeypatch):
    monkeypatch.setenv("MAGI_ENABLE_LIVE_TESTS", "0")
    live = _Item("live", live=True)
    items = [live]
    config, hook = _config(markexpr="live")

    magi_conftest.pytest_collection_modifyitems(config, items)

    assert items == [live]
    assert hook.deselected == []
