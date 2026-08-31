from __future__ import annotations

import importlib.util
from types import SimpleNamespace
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "tailscale_funnel_healthcheck.py"
SPEC = importlib.util.spec_from_file_location("tailscale_funnel_healthcheck_under_test", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_app_binary_is_forced_into_documented_cli_mode(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout="1.98.9\n", stderr="")

    monkeypatch.setattr(MODULE.subprocess, "run", fake_run)
    MODULE._run([MODULE.TAILSCALE_APP_BIN, "version"])

    assert captured["kwargs"]["env"]["TAILSCALE_BE_CLI"] == "1"


def test_official_app_cli_is_preferred_when_capability_probe_passes(monkeypatch):
    monkeypatch.delenv("MAGI_TAILSCALE_BIN", raising=False)
    monkeypatch.setattr(MODULE.Path, "is_file", lambda self: str(self) in {MODULE.TAILSCALE_APP_BIN, MODULE.TAILSCALE_CLI_BIN})
    monkeypatch.setattr(MODULE.os, "access", lambda *_args: True)
    monkeypatch.setattr(MODULE, "_tailscale_cli_usable", lambda path: path == MODULE.TAILSCALE_APP_BIN)
    assert MODULE._tailscale_bin() == MODULE.TAILSCALE_APP_BIN


def test_unusable_official_app_falls_back_to_homebrew(monkeypatch):
    monkeypatch.delenv("MAGI_TAILSCALE_BIN", raising=False)
    monkeypatch.setattr(MODULE.Path, "is_file", lambda self: str(self) in {MODULE.TAILSCALE_APP_BIN, MODULE.TAILSCALE_CLI_BIN})
    monkeypatch.setattr(MODULE.os, "access", lambda *_args: True)
    monkeypatch.setattr(MODULE, "_tailscale_cli_usable", lambda path: path == MODULE.TAILSCALE_CLI_BIN)
    assert MODULE._tailscale_bin() == MODULE.TAILSCALE_CLI_BIN


def test_configured_audited_cli_has_priority_but_arbitrary_path_is_rejected(monkeypatch):
    monkeypatch.setenv("MAGI_TAILSCALE_BIN", MODULE.TAILSCALE_CLI_BIN)
    monkeypatch.setattr(MODULE.Path, "is_file", lambda self: str(self) in {MODULE.TAILSCALE_APP_BIN, MODULE.TAILSCALE_CLI_BIN})
    monkeypatch.setattr(MODULE.os, "access", lambda *_args: True)
    monkeypatch.setattr(MODULE, "_tailscale_cli_usable", lambda _path: True)
    assert MODULE._tailscale_bin() == MODULE.TAILSCALE_CLI_BIN
    monkeypatch.setenv("MAGI_TAILSCALE_BIN", "/tmp/untrusted-tailscale")
    assert MODULE._tailscale_bin() == MODULE.TAILSCALE_APP_BIN


def test_capability_probe_rejects_version_mismatch(monkeypatch):
    outputs = iter((
        {"ok": True, "stdout": "1.98.9", "stderr": ""},
        {"ok": True, "stdout": '{"Version":"1.94.1"}', "stderr": ""},
        {"ok": True, "stdout": "{}", "stderr": ""},
    ))
    monkeypatch.setattr(MODULE, "_run", lambda *_args, **_kwargs: next(outputs))
    assert MODULE._tailscale_cli_usable(MODULE.TAILSCALE_APP_BIN) is False


def test_local_dns_resolution_fails_closed_without_addresses(monkeypatch):
    monkeypatch.setattr(MODULE.shutil, "which", lambda name: "/usr/bin/dscacheutil" if name == "dscacheutil" else None)
    monkeypatch.setattr(
        MODULE,
        "_run",
        lambda *args, **kwargs: {"ok": True, "returncode": 0, "stdout": "", "stderr": ""},
    )

    result = MODULE._local_dns_resolution("magi.example.test")

    assert result == {
        "ok": False,
        "host": "magi.example.test",
        "address_count": 0,
        "reason_code": "local_dns_unresolved",
    }


def test_edge_coverage_requires_every_advertised_public_address():
    result = MODULE._edge_probe_coverage(
        [
            {"ip": "203.0.113.8", "ok": True},
            {"ip": "203.0.113.9", "ok": False},
            {"ip": "2001:db8::8", "ok": True},
        ]
    )

    assert result["ok"] is False
    assert result["partial"] is True
    assert result["advertised"] == 3
    assert result["passed"] == 2
    assert result["failed"] == 1
    assert result["by_family"]["ipv4"] == {"advertised": 2, "passed": 1, "failed": 1}
    assert result["vantage"] == "host_to_public_edge_pinned"
    assert result["off_host"] is False


def test_mobile_entry_requires_every_advertised_edge(monkeypatch):
    monkeypatch.setattr(
        MODULE,
        "_probe_mobile_entry_url",
        lambda _url, *, host, ip: {"host": host, "ip": ip, "ok": ip.endswith(".8")},
    )

    result = MODULE._probe_mobile_entry_targets(
        [{"host": "magi.example.test", "path": "/", "proxy": "http://127.0.0.1:5002"}],
        {"magi.example.test": ["203.0.113.8", "203.0.113.9"]},
    )

    assert result["ok"] is False
    assert [probe["ok"] for probe in result["probes"]] == [True, False]


def test_public_success_with_local_nxdomain_is_degraded_not_terminal(monkeypatch):
    monkeypatch.setattr(MODULE, "_load_dotenv", lambda: None)
    monkeypatch.setattr(MODULE, "_load_funnel_status", lambda: {"ok": False, "error": "daemon unavailable"})
    monkeypatch.setattr(MODULE, "_public_health_url", lambda: "https://magi.example.test/health")
    monkeypatch.setattr(MODULE, "_configured_public_probes", lambda url: [{"ok": True, "http_code": 200}])
    monkeypatch.setattr(MODULE, "_probe_configured_mobile_entry", lambda: {"ok": True, "probes": []})
    monkeypatch.setattr(
        MODULE,
        "_local_dns_resolution",
        lambda host: {"ok": False, "host": host, "address_count": 0, "reason_code": "local_dns_unresolved"},
    )

    # This is the host-vantage contract.  A sealed release separately requires
    # the signed off-host canary exercised at the end of this module.
    result = MODULE._check_host_vantage(apply=False)

    assert result["status"] == "degraded"
    assert result["local_dns"]["ok"] is False
    assert result["local_access_degraded"] is True
    assert "public Funnel is reachable" in result["reason"]
    assert any("official Tailscale app" in action for action in result["next_actions"])


def test_public_and_local_dns_success_without_scope_is_amber(monkeypatch):
    monkeypatch.setattr(MODULE, "_load_dotenv", lambda: None)
    monkeypatch.setattr(MODULE, "_load_funnel_status", lambda: {"ok": False, "error": "daemon unavailable"})
    monkeypatch.setattr(MODULE, "_public_health_url", lambda: "https://magi.example.test/health")
    monkeypatch.setattr(MODULE, "_configured_public_probes", lambda url: [{"ok": True, "http_code": 200}])
    monkeypatch.setattr(MODULE, "_probe_configured_mobile_entry", lambda: {"ok": True, "probes": []})
    monkeypatch.setattr(
        MODULE,
        "_local_dns_resolution",
        lambda host: {"ok": True, "host": host, "address_count": 2, "reason_code": "resolved"},
    )

    result = MODULE.check(apply=False)

    assert result["status"] == "degraded"
    assert result["local_dns"]["ok"] is True
    assert result["scope_unattested"] is True
    assert result.get("local_access_degraded") is None


def test_schedule_fixture_omits_external_probes_and_requires_bounded_reassert(monkeypatch, tmp_path):
    monkeypatch.setenv("MAGI_V3_REALISM_SANDBOX", "1")
    monkeypatch.setenv("MAGI_V3_SCHEDULE_ADAPTER", "real_entrypoint_fixture_v1")
    monkeypatch.setenv("MAGI_TAILSCALE_FIXTURE_LOCAL_BACKEND", str(tmp_path / "health.json"))
    monkeypatch.setenv("MAGI_PUBLIC_BASE_URL", "https://fixture.tailnet.example")
    monkeypatch.setattr(MODULE, "_load_dotenv", lambda: None)
    monkeypatch.setattr(
        MODULE,
        "_load_funnel_status",
        lambda: {
            "ok": True,
            "data": {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {
                    "fixture.tailnet.example:443": {
                        "Handlers": {"/": {"Proxy": "http://127.0.0.1:5002"}}
                    }
                },
                "AllowFunnel": {"fixture.tailnet.example:443": True},
            },
        },
    )
    monkeypatch.setattr(MODULE, "_local_funnel_backend_ready", lambda: {"ok": True, "fixture": True})
    monkeypatch.setattr(MODULE, "_reassert_approved_funnel", lambda _scope: {"status": "applied"})
    monkeypatch.setattr(MODULE, "_public_ips", lambda _host: pytest.fail("external DNS probe must be omitted"))

    result = MODULE.check(apply=True)

    assert result["status"] == "recovered"
    assert result["external_network_probes"] == "omitted"
    assert result["public_dns"]["skipped"] is True
    assert result["probes"][0]["ok"] is False
    assert result["reprobes"][0]["ok"] is True
    assert result["mobile_entry_after_repair"]["ok"] is True


def test_public_dns_matrix_detects_browser_visible_tcp_nxdomain(monkeypatch):
    monkeypatch.setattr(MODULE.shutil, "which", lambda name: "/usr/bin/dig" if name == "dig" else None)

    def fake_run(args, **_kwargs):
        cloudflare_tcp = "+tcp" in args and "@1.1.1.1" in args
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "" if cloudflare_tcp else "203.0.113.8",
            "stderr": "",
        }

    monkeypatch.setattr(MODULE, "_run", fake_run)
    monkeypatch.setattr(MODULE, "_is_public_ip", lambda value: value == "203.0.113.8")
    monkeypatch.setattr(
        MODULE,
        "_doh_check",
        lambda endpoint, _host: {
            "resolver": endpoint,
            "transport": "doh",
            "ok": False,
            "answer_count": 0,
            "reason_code": "public_dns_unresolved",
        },
    )

    result = MODULE._public_dns_matrix("magi.example.test")

    assert result["ok"] is False
    assert result["partial"] is True
    assert result["reason_code"] == "converging"
    assert sum(1 for check in result["checks"] if check["ok"]) == 3


def test_doh_two_independent_resolvers_recover_when_raw_53_blocked(monkeypatch):
    monkeypatch.setattr(MODULE.shutil, "which", lambda _name: None)
    monkeypatch.setattr(MODULE, "_doh_check", lambda endpoint, _host: {"resolver": endpoint, "transport": "doh", "ok": True, "answer_count": 1, "reason_code": "resolved"})
    result = MODULE._public_dns_matrix("magi.example.test")
    assert result["ok"] is True and result["partial"] is False
    assert all("raw" not in str(check).lower() for check in result["checks"])


def test_one_doh_answer_is_only_partial(monkeypatch):
    monkeypatch.setattr(MODULE.shutil, "which", lambda _name: None)
    responses = iter(({"transport":"doh","ok":True,"answer_count":1,"reason_code":"resolved"},{"transport":"doh","ok":False,"answer_count":0,"reason_code":"public_dns_unresolved"}))
    monkeypatch.setattr(MODULE, "_doh_check", lambda *_args: next(responses))
    result = MODULE._public_dns_matrix("magi.example.test")
    assert result["ok"] is False and result["partial"] is True


def test_doh_nxdomain_is_unresolved_without_body_leak(monkeypatch):
    monkeypatch.setattr(MODULE.shutil, "which", lambda _name: None)
    monkeypatch.setattr(MODULE, "_doh_check", lambda endpoint, _host: {"resolver":endpoint,"transport":"doh","ok":False,"answer_count":0,"reason_code":"public_dns_unresolved"})
    result = MODULE._public_dns_matrix("magi.example.test")
    assert result["reason_code"] == "unresolved"
    assert "Answer" not in str(result) and "body" not in str(result).lower()


def test_funnel_scope_accepts_only_approved_root_to_5002(monkeypatch):
    monkeypatch.setenv("MAGI_PUBLIC_BASE_URL", "https://magi.example.test")

    approved = MODULE._funnel_scope(
        [{"host": "magi.example.test", "path": "/", "proxy": "http://127.0.0.1:5002"}]
    )
    extra_path = MODULE._funnel_scope(
        [
            {"host": "magi.example.test", "path": "/", "proxy": "http://127.0.0.1:5002"},
            {"host": "magi.example.test", "path": "/tools", "proxy": "http://127.0.0.1:5003"},
        ]
    )

    assert approved["ok"] is True
    assert approved["repair_allowed"] is True
    assert extra_path["ok"] is False
    assert extra_path["repair_allowed"] is False
    assert extra_path["reason_code"] == "funnel_scope_violation"
    extra_port = MODULE._funnel_scope(
        [{"host": "magi.example.test", "path": "/", "proxy": "http://127.0.0.1:5002"}],
        {
            "TCP": {"443": {"HTTPS": True}, "8443": {"HTTPS": True}},
            "AllowFunnel": {"magi.example.test:443": True, "magi.example.test:8443": True},
        },
    )
    assert extra_port["ok"] is False
    assert extra_port["repair_allowed"] is False


def test_public_tool_pages_are_part_of_the_funnel_security_boundary(monkeypatch):
    calls = []
    monkeypatch.setattr(
        MODULE,
        "_probe_boundary_url",
        lambda host, ip, path, kind: calls.append((host, ip, path, kind))
        or {"path": path, "kind": kind, "ok": True, "http_code": 200},
    )

    result = MODULE._probe_security_boundaries(
        [{"host": "magi.example.test", "path": "/", "proxy": "http://127.0.0.1:5002"}],
        {"magi.example.test": ["203.0.113.8"]},
        use_dns_route=True,
    )

    assert result["ok"] is True
    assert ("magi.example.test", "", "/lottery", "public") in calls
    assert ("magi.example.test", "", "/exam-tutor", "public") in calls
    assert ("magi.example.test", "", "/cookie-cutter", "public") in calls


def test_approved_target_reads_bound_runtime_public_base_file(tmp_path, monkeypatch):
    base_file = tmp_path / "public-base.txt"
    base_file.write_text("https://magi.example.test\n", encoding="utf-8")
    for key in (
        "MAGI_PUBLIC_BASE_URL",
        "MAGI_MOBILE_BASE_URL",
        "MAGI_TAILSCALE_URL",
        "MAGI_TAILSCALE_FUNNEL_HEALTH_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MAGI_OSC_FILE_SHARE_PUBLIC_BASE_FILE", str(base_file))

    assert MODULE._approved_funnel_target() == {
        "host": "magi.example.test",
        "path": "/",
        "proxy": "http://127.0.0.1:5002",
    }


def test_bounded_repair_never_resets_or_exposes_another_port(monkeypatch):
    calls = []
    monkeypatch.setattr(MODULE, "_tailscale_bin", lambda: "/fixture/tailscale")
    monkeypatch.setattr(
        MODULE,
        "_run",
        lambda args, **_kwargs: calls.append(args) or {"ok": True, "returncode": 0, "stdout": "", "stderr": ""},
    )
    scope = {
        "ok": True,
        "repair_allowed": True,
        "reason_code": "approved_scope",
        "approved": {"host": "magi.example.test", "path": "/", "proxy": "http://127.0.0.1:5002"},
    }

    result = MODULE._reassert_approved_funnel(scope)

    assert result["status"] == "applied"
    assert calls == [["/fixture/tailscale", "funnel", "--bg", "--yes", "http://127.0.0.1:5002"]]
    assert all("reset" not in call for call in calls)


def test_public_ingress_refresh_is_bounded_and_ordered(monkeypatch):
    calls = []
    monkeypatch.setattr(MODULE, "_tailscale_bin", lambda: "/fixture/tailscale")
    monkeypatch.setattr(
        MODULE,
        "_local_funnel_backend_ready",
        lambda: {"ok": True, "reason_code": "local_backend_ready"},
    )
    monkeypatch.setattr(
        MODULE,
        "_run",
        lambda args, **_kwargs: calls.append(args) or {"ok": True, "returncode": 0, "stdout": "", "stderr": ""},
    )
    scope = {
        "ok": True,
        "repair_allowed": True,
        "reason_code": "approved_scope",
        "approved": {"host": "magi.example.test", "path": "/", "proxy": "http://127.0.0.1:5002"},
    }

    result = MODULE._refresh_public_ingress(scope)

    assert result["status"] == "applied"
    assert calls == [
        ["/fixture/tailscale", "funnel", "--bg", "--yes", "http://127.0.0.1:5002"],
    ]
    assert all("reset" not in call and "down" not in call and "up" not in call for call in calls)
    assert result["reason_code"] == "non_disruptive_root_reassert"
    assert result["disruption_policy"] == "no_disruptive_funnel_mutation"


def test_public_ingress_refresh_blocks_without_healthy_local_backend(monkeypatch):
    calls = []
    monkeypatch.setattr(MODULE, "_tailscale_bin", lambda: "/fixture/tailscale")
    monkeypatch.setattr(
        MODULE,
        "_local_funnel_backend_ready",
        lambda: {"ok": False, "reason_code": "local_backend_unreachable"},
    )
    monkeypatch.setattr(
        MODULE,
        "_run",
        lambda args, **_kwargs: calls.append(args) or {"ok": True, "returncode": 0, "stdout": "", "stderr": ""},
    )
    scope = {
        "ok": True,
        "repair_allowed": True,
        "reason_code": "approved_scope",
        "approved": {"host": "magi.example.test", "path": "/", "proxy": "http://127.0.0.1:5002"},
    }

    result = MODULE._refresh_public_ingress(scope)

    assert result["status"] == "blocked"
    assert result["reason_code"] == "local_backend_unreachable"
    assert calls == []


def test_partial_public_dns_is_amber_and_never_reasserts_healthy_funnel(monkeypatch):
    monkeypatch.setenv("MAGI_PUBLIC_BASE_URL", "https://magi.example.test")
    monkeypatch.setattr(MODULE, "_load_dotenv", lambda: None)
    monkeypatch.setattr(
        MODULE,
        "_load_funnel_status",
        lambda: {
            "ok": True,
            "data": {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {"magi.example.test:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:5002"}}}},
                "AllowFunnel": {"magi.example.test:443": True},
            },
        },
    )
    monkeypatch.setattr(MODULE, "_public_ips", lambda _host: ["203.0.113.8"])
    monkeypatch.setattr(
        MODULE,
        "_probe",
        lambda host, ip, path: {"host": host, "ip": ip, "path": path, "ok": True, "http_code": 200},
    )
    monkeypatch.setattr(MODULE, "_probe_mobile_entry_targets", lambda *_args: {"ok": True, "probes": []})
    monkeypatch.setattr(
        MODULE,
        "_public_dns_matrix",
        lambda _host: {"ok": False, "partial": True, "checks": [], "reason_code": "converging"},
    )
    monkeypatch.setattr(MODULE, "_probe_security_boundaries", lambda *_args, **_kwargs: {"ok": True, "checks": []})
    monkeypatch.setattr(
        MODULE,
        "_local_dns_resolution",
        lambda host: {"ok": True, "host": host, "address_count": 1, "reason_code": "resolved"},
    )
    repairs = []
    monkeypatch.setattr(
        MODULE,
        "_reassert_approved_funnel",
        lambda scope: repairs.append(scope) or {"action": "reassert_approved_funnel", "status": "applied"},
    )

    result = MODULE.check(apply=True)

    assert result["status"] == "degraded"
    assert result["dns_convergence_pending"] is True
    assert result["ingress_mutation_suppressed"] == "public_route_verified"
    assert result["actions"] == []
    assert repairs == []
    assert "healthy Funnel was left unchanged" in result["next_actions"][0]


def test_scope_violation_is_red_and_never_auto_repaired(monkeypatch):
    monkeypatch.setenv("MAGI_PUBLIC_BASE_URL", "https://magi.example.test")
    monkeypatch.setattr(MODULE, "_load_dotenv", lambda: None)
    monkeypatch.setattr(
        MODULE,
        "_load_funnel_status",
        lambda: {
            "ok": True,
            "data": {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {
                    "magi.example.test:443": {
                        "Handlers": {
                            "/": {"Proxy": "http://127.0.0.1:5002"},
                            "/tools": {"Proxy": "http://127.0.0.1:5003"},
                        }
                    }
                },
                "AllowFunnel": {"magi.example.test:443": True},
            },
        },
    )
    monkeypatch.setattr(MODULE, "_reassert_approved_funnel", lambda _scope: (_ for _ in ()).throw(AssertionError("must not repair")))

    result = MODULE.check(apply=True)

    assert result["status"] == "failed"
    assert result["scope"]["reason_code"] == "funnel_scope_violation"
    assert result["action_required"] is True


def test_authentication_boundary_failure_never_changes_funnel(monkeypatch):
    monkeypatch.setenv("MAGI_PUBLIC_BASE_URL", "https://magi.example.test")
    monkeypatch.setattr(MODULE, "_load_dotenv", lambda: None)
    monkeypatch.setattr(
        MODULE,
        "_load_funnel_status",
        lambda: {
            "ok": True,
            "data": {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {"magi.example.test:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:5002"}}}},
                "AllowFunnel": {"magi.example.test:443": True},
            },
        },
    )
    monkeypatch.setattr(MODULE, "_public_ips", lambda _host: ["203.0.113.8"])
    monkeypatch.setattr(MODULE, "_probe", lambda *_args: {"ok": True, "http_code": 200})
    monkeypatch.setattr(MODULE, "_probe_mobile_entry_targets", lambda *_args: {"ok": True, "probes": []})
    monkeypatch.setattr(MODULE, "_public_dns_matrix", lambda _host: {"ok": True, "partial": False, "checks": []})
    monkeypatch.setattr(
        MODULE,
        "_probe_security_boundaries",
        lambda *_args, **_kwargs: {"ok": False, "checks": [{"path": "/dashboard", "ok": False}]},
    )
    monkeypatch.setattr(MODULE, "_reassert_approved_funnel", lambda _scope: (_ for _ in ()).throw(AssertionError("must not repair")))

    result = MODULE.check(apply=True)

    assert result["status"] == "failed"
    assert result["action_required"] is True
    assert result["actions"] == []


def test_tailnet_dns_success_cannot_mask_public_edge_failure(monkeypatch):
    monkeypatch.setenv("MAGI_PUBLIC_BASE_URL", "https://magi.example.test")
    monkeypatch.setattr(MODULE, "_load_dotenv", lambda: None)
    monkeypatch.setattr(
        MODULE,
        "_load_funnel_status",
        lambda: {
            "ok": True,
            "data": {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {"magi.example.test:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:5002"}}}},
                "AllowFunnel": {"magi.example.test:443": True},
            },
        },
    )
    monkeypatch.setattr(MODULE, "_public_ips", lambda _host: ["203.0.113.8", "2001:db8::8"])
    monkeypatch.setattr(MODULE, "_probe", lambda *_args: {"ok": False, "http_code": 0, "stderr": "tls edge rejected"})
    monkeypatch.setattr(
        MODULE,
        "_probe_mobile_entry_targets",
        lambda *_args: {"ok": False, "probes": [{"ok": False, "http_code": 0}]},
    )
    monkeypatch.setattr(MODULE, "_probe_dns_route", lambda *_args: {"ok": True, "http_code": 302, "route": "public_dns"})
    monkeypatch.setattr(
        MODULE,
        "_probe_mobile_entry_targets_dns",
        lambda *_args: {"ok": True, "probes": [{"ok": True, "http_code": 302, "route": "public_dns"}]},
    )
    monkeypatch.setattr(MODULE, "_public_dns_matrix", lambda _host: {"ok": True, "partial": False, "checks": []})
    boundary_calls = []
    monkeypatch.setattr(
        MODULE,
        "_probe_security_boundaries",
        lambda *_args, **kwargs: boundary_calls.append(kwargs) or {"ok": True, "checks": [], "route": "public_dns"},
    )
    monkeypatch.setattr(
        MODULE,
        "_local_dns_resolution",
        lambda host: {"ok": True, "host": host, "address_count": 2, "reason_code": "resolved"},
    )

    result = MODULE.check(apply=False)

    assert result["status"] == "failed"
    assert result["canonical_dns_probes"][0]["ok"] is True
    assert result["canonical_dns_is_tailnet_only"] is True
    assert boundary_calls == []
    assert result["reason"] == "one or more advertised public Funnel edges failed"


def test_canonical_tailnet_route_is_degraded_without_disruptive_refresh(monkeypatch):
    monkeypatch.setenv("MAGI_PUBLIC_BASE_URL", "https://magi.example.test")
    monkeypatch.setattr(MODULE, "_load_dotenv", lambda: None)
    monkeypatch.setattr(
        MODULE,
        "_load_funnel_status",
        lambda: {
            "ok": True,
            "data": {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {"magi.example.test:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:5002"}}}},
                "AllowFunnel": {"magi.example.test:443": True},
            },
        },
    )
    monkeypatch.setattr(MODULE, "_public_ips", lambda _host: [])
    monkeypatch.setattr(
        MODULE,
        "_probe_dns_route",
        lambda *_args: {"ok": True, "http_code": 302, "route": "public_dns"},
    )
    monkeypatch.setattr(
        MODULE,
        "_probe_mobile_entry_targets_dns",
        lambda _targets: {"ok": True, "probes": [{"ok": True, "http_code": 302, "route": "public_dns"}]},
    )
    monkeypatch.setattr(
        MODULE,
        "_probe_security_boundaries",
        lambda *_args, **_kwargs: {"ok": True, "checks": [], "route": "public_dns"},
    )
    monkeypatch.setattr(
        MODULE,
        "_public_dns_matrix",
        lambda _host: {"ok": False, "partial": False, "checks": [], "reason_code": "unresolved"},
    )
    monkeypatch.setattr(
        MODULE,
        "_refresh_public_ingress",
        lambda *_args: (_ for _ in ()).throw(AssertionError("canonical route must not refresh Funnel")),
    )
    monkeypatch.setattr(
        MODULE,
        "_local_dns_resolution",
        lambda host: {"ok": True, "host": host, "address_count": 1, "reason_code": "resolved"},
    )

    result = MODULE.check(apply=True)

    assert result["status"] == "degraded"
    assert result["public_edge_unattested"] is True
    assert result["canonical_dns_is_tailnet_only"] is True
    assert result["security_boundary"]["route"] == "public_dns"
    assert result["actions"] == []
    assert "no Funnel refresh" in result["next_actions"][0]


def test_apply_suppresses_refresh_when_confirmation_probe_recovers(monkeypatch):
    monkeypatch.setenv("MAGI_PUBLIC_BASE_URL", "https://magi.example.test")
    monkeypatch.setattr(MODULE, "_load_dotenv", lambda: None)
    monkeypatch.setattr(
        MODULE,
        "_load_funnel_status",
        lambda: {
            "ok": True,
            "data": {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {"magi.example.test:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:5002"}}}},
                "AllowFunnel": {"magi.example.test:443": True},
            },
        },
    )
    monkeypatch.setattr(MODULE, "_public_ips", lambda _host: ["203.0.113.8"])
    edge_results = iter(
        [
            {"ip": "203.0.113.8", "ok": False, "http_code": 0, "stderr": "tls closed"},
            {"ip": "203.0.113.8", "ok": True, "http_code": 200, "stderr": ""},
        ]
    )
    monkeypatch.setattr(MODULE, "_probe", lambda *_args: next(edge_results))
    mobile_results = iter(
        [
            {"ok": False, "probes": [{"ok": False, "http_code": 0}]},
            {"ok": True, "probes": [{"ok": True, "http_code": 302}]},
        ]
    )
    monkeypatch.setattr(MODULE, "_probe_mobile_entry_targets", lambda *_args: next(mobile_results))
    monkeypatch.setattr(MODULE, "_probe_dns_route", lambda *_args: {"ok": True, "http_code": 200, "route": "public_dns"})
    monkeypatch.setattr(
        MODULE,
        "_probe_mobile_entry_targets_dns",
        lambda *_args: {"ok": True, "probes": [{"ok": True, "http_code": 302, "route": "public_dns"}]},
    )
    monkeypatch.setattr(MODULE, "_public_dns_matrix", lambda _host: {"ok": True, "partial": False, "checks": []})
    repairs = []
    monkeypatch.setattr(
        MODULE,
        "_refresh_public_ingress",
        lambda scope: repairs.append(scope) or {"action": "refresh_public_ingress", "status": "applied"},
    )
    monkeypatch.setattr(MODULE.time, "sleep", lambda _seconds: None)
    boundary_calls = []
    monkeypatch.setattr(
        MODULE,
        "_probe_security_boundaries",
        lambda *_args, **kwargs: boundary_calls.append(kwargs) or {"ok": True, "checks": [], "route": "edge_pinned"},
    )
    monkeypatch.setattr(
        MODULE,
        "_local_dns_resolution",
        lambda host: {"ok": True, "host": host, "address_count": 1, "reason_code": "resolved"},
    )

    # A recovered host probe must not be rewritten by the separate release
    # canary wrapper while this non-disruptive refresh policy is under test.
    result = MODULE._check_host_vantage(apply=True)

    assert result["status"] == "recovered"
    assert repairs == []
    assert result["canonical_dns_is_tailnet_only"] is True
    assert result["confirmation_probes"][0]["ok"] is True
    assert result["ingress_mutation_suppressed"] == "transient_public_probe_recovered"
    assert boundary_calls == [{"use_dns_route": False}]


def test_apply_never_replays_verified_scope_after_two_public_failures(monkeypatch):
    monkeypatch.setenv("MAGI_PUBLIC_BASE_URL", "https://magi.example.test")
    monkeypatch.setattr(MODULE, "_load_dotenv", lambda: None)
    monkeypatch.setattr(
        MODULE,
        "_load_funnel_status",
        lambda: {
            "ok": True,
            "data": {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {"magi.example.test:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:5002"}}}},
                "AllowFunnel": {"magi.example.test:443": True},
            },
        },
    )
    monkeypatch.setattr(MODULE, "_public_ips", lambda _host: ["203.0.113.8"])
    edge_results = iter(
        [
            {"ok": False, "http_code": 0, "stderr": "tls closed"},
            {"ok": False, "http_code": 0, "stderr": "tls still closed"},
        ]
    )
    monkeypatch.setattr(MODULE, "_probe", lambda *_args: next(edge_results))
    mobile_results = iter(
        [
            {"ok": False, "probes": [{"ok": False, "http_code": 0}]},
            {"ok": False, "probes": [{"ok": False, "http_code": 0}]},
        ]
    )
    monkeypatch.setattr(MODULE, "_probe_mobile_entry_targets", lambda *_args: next(mobile_results))
    monkeypatch.setattr(MODULE, "_probe_dns_route", lambda *_args: {"ok": False, "http_code": 0, "route": "public_dns"})
    monkeypatch.setattr(MODULE, "_probe_mobile_entry_targets_dns", lambda *_args: {"ok": False, "probes": []})
    monkeypatch.setattr(MODULE, "_public_dns_matrix", lambda _host: {"ok": True, "partial": False, "checks": []})
    repairs = []
    monkeypatch.setattr(
        MODULE,
        "_refresh_public_ingress",
        lambda scope: repairs.append(scope) or {"action": "refresh_public_ingress", "status": "applied"},
    )
    monkeypatch.setattr(MODULE.time, "sleep", lambda _seconds: None)
    boundary_calls = []
    monkeypatch.setattr(
        MODULE,
        "_probe_security_boundaries",
        lambda *_args, **kwargs: boundary_calls.append(kwargs) or {"ok": True, "checks": [], "route": "edge_pinned"},
    )

    result = MODULE.check(apply=True)

    assert result["status"] == "failed"
    assert result["action_required"] is True
    assert repairs == []
    assert result["actions"] == []
    assert result["confirmation_probes"][0]["ok"] is False
    assert result["ingress_mutation_suppressed"] == "verified_scope_public_failure"
    assert boundary_calls == []


def test_canonical_dns_failure_remains_red_when_edge_pins_also_fail(monkeypatch):
    monkeypatch.setenv("MAGI_PUBLIC_BASE_URL", "https://magi.example.test")
    monkeypatch.setattr(MODULE, "_load_dotenv", lambda: None)
    monkeypatch.setattr(
        MODULE,
        "_load_funnel_status",
        lambda: {
            "ok": True,
            "data": {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {"magi.example.test:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:5002"}}}},
                "AllowFunnel": {"magi.example.test:443": True},
            },
        },
    )
    monkeypatch.setattr(MODULE, "_public_ips", lambda _host: ["203.0.113.8"])
    monkeypatch.setattr(MODULE, "_probe", lambda *_args: {"ok": False, "http_code": 0})
    monkeypatch.setattr(MODULE, "_probe_mobile_entry_targets", lambda *_args: {"ok": False, "probes": []})
    monkeypatch.setattr(MODULE, "_probe_dns_route", lambda *_args: {"ok": False, "http_code": 0, "route": "public_dns"})
    monkeypatch.setattr(MODULE, "_probe_mobile_entry_targets_dns", lambda *_args: {"ok": False, "probes": []})
    monkeypatch.setattr(MODULE, "_public_dns_matrix", lambda _host: {"ok": True, "partial": False, "checks": []})

    result = MODULE.check(apply=False)

    assert result["status"] == "failed"
    assert result["reason"] == "one or more advertised public Funnel edges failed"


def test_sealed_release_cannot_claim_external_green_without_offhost_receipt(monkeypatch):
    monkeypatch.setenv("MAGI_V3_RELEASE_MANIFEST", "/sealed/release/release-manifest.json")
    monkeypatch.setattr(
        MODULE,
        "_check_host_vantage",
        lambda apply=False: {
            "status": "ok",
            "reason": "public Funnel probe succeeded",
            "targets": [{"host": "magi.example.test", "path": "/", "proxy": "http://127.0.0.1:5002"}],
            "next_actions": [],
        },
    )
    monkeypatch.setattr(
        MODULE,
        "_load_offhost_canary",
        lambda **kwargs: {"ok": False, "off_host": False, "reason_code": "off_host_receipt_unconfigured"},
    )

    result = MODULE.check(apply=False)

    assert result["status"] == "degraded"
    assert result["host_vantage_status"] == "ok"
    assert result["availability_claim"] == "host_to_edge_only"
    assert result["external_canary"]["off_host"] is False


def test_fresh_offhost_receipt_allows_external_availability_claim(monkeypatch):
    monkeypatch.setenv("MAGI_V3_RELEASE_MANIFEST", "/sealed/release/release-manifest.json")
    monkeypatch.setattr(
        MODULE,
        "_check_host_vantage",
        lambda apply=False: {
            "status": "ok",
            "reason": "public Funnel probe succeeded",
            "targets": [{"host": "magi.example.test", "path": "/", "proxy": "http://127.0.0.1:5002"}],
            "next_actions": [],
        },
    )
    monkeypatch.setattr(
        MODULE,
        "_load_offhost_canary",
        lambda **kwargs: {"ok": True, "off_host": True, "checks": {"dns_ipv4": True, "dns_ipv6": True, "tls": True, "http_health": True, "login_redirect": True}},
    )

    result = MODULE.check(apply=False)

    assert result["status"] == "ok"
    assert result["availability_claim"] == "externally_verified_public_availability"
    assert result["external_canary"]["off_host"] is True
