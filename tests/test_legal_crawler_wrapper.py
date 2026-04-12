from __future__ import annotations

from skills.law_firm import legal_crawler_wrapper


def test_post_tools_skill_success(monkeypatch):
    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = '{"ok": true}'

        @staticmethod
        def json():
            return {"output": '{"message":"done"}'}

    monkeypatch.setattr(legal_crawler_wrapper.requests, "post", lambda *args, **kwargs: _Resp())
    ok, payload = legal_crawler_wrapper._post_tools_skill(
        "osc-orchestrator",
        "index_cases {}",
        timeout_sec=30,
        request_timeout_sec=35,
    )
    assert ok is True
    assert payload["status_code"] == 200
    assert payload["data"]["output"] == '{"message":"done"}'


def test_run_file_review_check_formats_http_errors(monkeypatch):
    monkeypatch.setattr(
        legal_crawler_wrapper,
        "_post_tools_skill",
        lambda *args, **kwargs: (False, {"status_code": 503, "data": {}}),
    )
    text = legal_crawler_wrapper._run_file_review_check()
    assert "HTTP 503" in text
