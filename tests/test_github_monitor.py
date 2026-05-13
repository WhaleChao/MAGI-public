from __future__ import annotations


def test_search_repos_uses_scrapling_json_adapter(monkeypatch):
    from skills.research import github_monitor

    monkeypatch.setenv("MAGI_ALLOW_INTERNET", "1")
    monkeypatch.setattr(
        github_monitor,
        "fetch_json",
        lambda url, headers=None, timeout=10: {
            "use_fallback": False,
            "success": True,
            "status_code": 200,
            "engine": "scrapling",
            "data": {
                "items": [
                    {
                        "full_name": "openai/example",
                        "description": "Example repo",
                        "stargazers_count": 42,
                        "html_url": "https://github.com/openai/example",
                    }
                ]
            },
        },
    )

    result = github_monitor.search_repos("openai")

    assert "openai/example" in result
    assert "42" in result


def test_search_repos_falls_back_to_requests(monkeypatch):
    from skills.research import github_monitor

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "items": [
                    {
                        "full_name": "openai/fallback",
                        "description": "Fallback repo",
                        "stargazers_count": 7,
                        "html_url": "https://github.com/openai/fallback",
                    }
                ]
            }

    monkeypatch.setenv("MAGI_ALLOW_INTERNET", "1")
    monkeypatch.setattr(github_monitor, "fetch_json", lambda *args, **kwargs: {"use_fallback": True})
    monkeypatch.setattr(github_monitor.requests, "get", lambda *args, **kwargs: _Response())

    result = github_monitor.search_repos("openai")

    assert "openai/fallback" in result
