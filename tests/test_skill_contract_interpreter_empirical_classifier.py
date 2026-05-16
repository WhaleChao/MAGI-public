import importlib.util
import json
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path


ROOT = Path("/Users/ai/Desktop/MAGI_v2")
ACTION_PATH = ROOT / "skills" / "interpreter-empirical-classifier" / "action.py"


def _load_action():
    spec = importlib.util.spec_from_file_location("interpreter_empirical_classifier_action", ACTION_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_interpreter_empirical_classifier_self_test():
    action = _load_action()
    result = action.self_test()
    assert result["success"] is True
    assert result["outputs"]["csv"].endswith(".csv")


def test_interpreter_empirical_classifier_cli_outputs_json():
    proc = subprocess.run(
        [sys.executable, str(ACTION_PATH), "--task", "self_test"],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["success"] is True


def test_interpreter_fetch_and_classify_uses_keyword_search(tmp_path):
    action = _load_action()
    cache = tmp_path / "cache"
    cache.mkdir()
    calls = {"search": [], "fetch": []}

    def fake_search(**kwargs):
        calls["search"].append(kwargs)
        return {
            "success": True,
            "count": 2,
            "total_count": 2,
            "engine": "fake",
            "results": [
                {"title": "最高法院 114 年度台抗字第 1 號刑事裁定", "url": "https://example.test/1"},
                {"title": "最高法院 114 年度台上字第 2 號刑事判決", "url": "https://example.test/2"},
            ],
        }

    def fake_fetch(url, **_kwargs):
        calls["fetch"].append(url)
        idx = "1" if url.endswith("/1") else "2"
        path = cache / f"{idx}.txt"
        if idx == "1":
            path.write_text(
                "裁判日期：民國 114 年 1 月 1 日\n裁判案由：再審\n\n主文\n抗告駁回。\n理由\n"
                "原判決所憑之證言、鑑定或通譯已證明其為虛偽者，得聲請再審。\n",
                encoding="utf-8",
            )
        else:
            path.write_text(
                "裁判日期：民國 114 年 1 月 2 日\n裁判案由：違反毒品危害防制條例\n\n主文\n上訴駁回。\n理由\n"
                "上訴人主張警詢時未經通譯傳譯，且通譯並未如實翻譯，譯文與真意不符。\n",
                encoding="utf-8",
            )
        return {"success": True, "text_path": str(path), "engine": "fake"}

    fake_jws = SimpleNamespace(_search_impl=fake_search, _fetch_text_impl=fake_fetch)
    result = action.fetch_and_classify(
        keyword="最高法院 通譯",
        output_dir=str(tmp_path / "out"),
        max_results=2,
        delay_sec=0,
        jws_module=fake_jws,
    )

    assert result["success"] is True
    assert calls["search"][0]["keywords"] == "通譯"
    assert calls["search"][0]["courts"] == ["最高法院"]
    assert len(calls["fetch"]) == 2
    assert result["fetch"]["fetched_count"] == 2
    csv_path = Path(result["outputs"]["csv"])
    assert csv_path.exists()
    csv_text = csv_path.read_text(encoding="utf-8-sig")
    assert "僅條文引用" in csv_text
    assert "實質通譯爭點" in csv_text
    assert (tmp_path / "out" / "fetch_report.json").exists()


def test_fetch_and_classify_without_keyword_falls_back_to_local_classify(monkeypatch, tmp_path):
    action = _load_action()
    called = {}

    def fake_classify(input_dir="", output_prefix=""):
        called["input_dir"] = input_dir
        called["output_prefix"] = output_prefix
        return {"success": True, "task": "classify", "outputs": {"csv": str(tmp_path / "out.csv")}}

    monkeypatch.setattr(action, "classify", fake_classify)
    result = action.fetch_and_classify(output_prefix=str(tmp_path / "out"))

    assert result["success"] is True
    assert result["task"] == "classify"
    assert called["input_dir"] == ""
    assert called["output_prefix"].endswith("out")


def test_interpreter_tool_definition_is_exposed():
    data = json.loads((ROOT / "skills" / "definitions.json").read_text(encoding="utf-8"))
    tool = next((t for t in data["tools"] if t.get("name") == "run_interpreter_empirical_classifier"), None)
    assert tool is not None
    assert tool["endpoint"] == "/skills/run"
    skill = tool["parameters"]["properties"]["skill"]
    assert skill["default"] == "interpreter-empirical-classifier"
    assert skill["enum"] == ["interpreter-empirical-classifier"]


def test_interpreter_skill_is_in_react_run_skill_allowlist():
    from skills.engine.tool_registry import _ALLOWED_SKILLS

    assert "interpreter-empirical-classifier" in _ALLOWED_SKILLS


def test_run_skill_preserves_params_in_task_payload(monkeypatch):
    from skills.bridge import http_pool
    from skills.engine import tool_registry

    posted = {}

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"success": True, "result": "ok"}

    class FakeSession:
        def post(self, url, json=None, timeout=None):
            posted["url"] = url
            posted["json"] = json
            posted["timeout"] = timeout
            return FakeResponse()

    monkeypatch.setattr(http_pool, "get_session", lambda: FakeSession())
    monkeypatch.setattr(tool_registry, "_tools_api_url", lambda: "http://tools.test")
    out = tool_registry._run_skill(
        skill_name="interpreter-empirical-classifier",
        task="fetch_and_classify",
        params='{"keywords":"最高法院 通譯","max_results":1,"timeout_sec":120}',
    )

    assert out == "ok"
    assert posted["json"]["skill"] == "interpreter-empirical-classifier"
    assert posted["json"]["timeout_sec"] == 120
    assert posted["timeout"] == 130
    assert posted["json"]["task"].startswith("fetch_and_classify ")
    assert '"keywords": "最高法院 通譯"' in posted["json"]["task"]
