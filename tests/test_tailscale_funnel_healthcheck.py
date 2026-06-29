from scripts.ops import tailscale_funnel_healthcheck as healthcheck


def _funnel_status():
    return {
        "ok": True,
        "data": {
            "Web": {
                "magi.example.com:443": {
                    "Handlers": {
                        "/": {"Proxy": "http://127.0.0.1:5002"},
                    }
                }
            }
        },
    }


def _curl_result(stdout: str, *, ok: bool = True):
    return {"ok": ok, "returncode": 0 if ok else 7, "stdout": stdout, "stderr": "", "args": []}


def test_tailscale_healthcheck_json_out_dash_prints_without_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        healthcheck,
        "check",
        lambda apply=False: {
            "checked_at": "2026-06-28T00:00:00+0800",
            "status": "ok",
            "reason": "test",
            "targets": [],
            "probes": [],
            "actions": [],
        },
    )

    assert healthcheck.main(["--json-out", "-"]) == 0
    assert '"status": "ok"' in capsys.readouterr().out
    assert not (tmp_path / "-").exists()


def test_tailscale_healthcheck_print_json_does_not_touch_default_state(monkeypatch, capsys):
    wrote = []

    class FakePath:
        def __init__(self, value):
            self.value = value
            self.parent = self

        def mkdir(self, *args, **kwargs):
            wrote.append(("mkdir", self.value))

        def write_text(self, *args, **kwargs):
            wrote.append(("write", self.value))

    monkeypatch.setattr(healthcheck, "Path", FakePath)
    monkeypatch.setattr(
        healthcheck,
        "check",
        lambda apply=False: {
            "checked_at": "2026-06-28T00:00:00+0800",
            "status": "ok",
            "reason": "test",
            "targets": [],
            "probes": [],
            "actions": [],
        },
    )

    assert healthcheck.main(["--print-json"]) == 0
    assert '"status": "ok"' in capsys.readouterr().out
    assert wrote == []


def test_tailscale_healthcheck_mobile_entry_redirect_is_explicit(monkeypatch):
    monkeypatch.setattr(healthcheck, "_load_dotenv", lambda: None)
    monkeypatch.setattr(healthcheck, "_load_funnel_status", _funnel_status)
    monkeypatch.setattr(healthcheck, "_public_ips", lambda host: ["93.184.216.34"])

    def fake_run(args, timeout=20):
        url = args[-1]
        if url.endswith("/mobile-app"):
            return _curl_result("HTTP/2 302\nlocation: /login?next=%2Fmobile&mobile_app=1\n\n302")
        return _curl_result("200")

    monkeypatch.setattr(healthcheck, "_run", fake_run)

    payload = healthcheck.check(apply=False)

    assert payload["status"] == "ok"
    assert payload["reason"] == "public Funnel and mobile entry probes succeeded"
    assert payload["mobile_entry"]["ok"] is True
    probe = payload["mobile_entry"]["probes"][0]
    assert probe["kind"] == "mobile_entry"
    assert probe["http_code"] == 302
    assert probe["location"] == "/login?next=%2Fmobile&mobile_app=1"
    assert probe["expected"] == healthcheck.MOBILE_ENTRY_EXPECTED
    assert payload["next_actions"] == []


def test_tailscale_healthcheck_mobile_entry_failure_has_login_probe_and_actions(monkeypatch):
    monkeypatch.setattr(healthcheck, "_load_dotenv", lambda: None)
    monkeypatch.setattr(healthcheck, "_load_funnel_status", _funnel_status)
    monkeypatch.setattr(healthcheck, "_public_ips", lambda host: ["93.184.216.34"])

    def fake_run(args, timeout=20):
        url = args[-1]
        if url.endswith("/mobile-app"):
            return _curl_result("HTTP/2 404\n\n404")
        if "/login?next=/mobile&mobile_app=1" in url:
            return _curl_result("HTTP/2 200\n\n200")
        return _curl_result("200")

    monkeypatch.setattr(healthcheck, "_run", fake_run)

    payload = healthcheck.check(apply=False)

    assert payload["status"] == "failed"
    assert payload["reason"] == "public Funnel probe succeeded, but mobile entry/login probe failed"
    assert payload["mobile_entry"]["ok"] is False
    probe = payload["mobile_entry"]["probes"][0]
    assert probe["http_code"] == 404
    assert probe["login_probe"]["ok"] is True
    assert any("/mobile-app" in item for item in payload["next_actions"])
    assert "restart Tailscale/Funnel first" in payload["restart_hint"]
