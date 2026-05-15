from __future__ import annotations


def test_paperclip_share_tunnel_uses_launchd_when_installed(monkeypatch):
    from api import startup

    kicked = []

    monkeypatch.setattr(startup, "_paperclip_share_gateway_port", lambda: "5014")
    monkeypatch.setattr(startup, "_paperclip_share_gateway_health_ok", lambda port: False)
    monkeypatch.setattr(startup, "_paperclip_share_tunnel_pids_for_port", lambda port: [])
    monkeypatch.setattr(startup, "_paperclip_share_public_health_ok", lambda: False)
    monkeypatch.setattr(startup, "_paperclip_share_launchd_managed", lambda: True)
    monkeypatch.setattr(startup, "_kickstart_paperclip_share_launchd", lambda: kicked.append(True))

    def fail_legacy_script(*args, **kwargs):
        raise AssertionError("legacy background tunnel script should not run when launchd owns the tunnel")

    monkeypatch.setattr(startup.subprocess, "run", fail_legacy_script)

    startup._ensure_paperclip_share_tunnel()

    assert kicked == [True]


def test_paperclip_share_tunnel_does_nothing_when_healthy(monkeypatch):
    from api import startup

    monkeypatch.setattr(startup, "_paperclip_share_gateway_port", lambda: "5014")
    monkeypatch.setattr(startup, "_paperclip_share_gateway_health_ok", lambda port: True)
    monkeypatch.setattr(startup, "_paperclip_share_tunnel_pids_for_port", lambda port: ["123"])
    monkeypatch.setattr(startup, "_paperclip_share_public_health_ok", lambda: True)
    monkeypatch.setattr(startup, "_paperclip_share_launchd_managed", lambda: (_ for _ in ()).throw(AssertionError("launchd check should not run")))
    monkeypatch.setattr(startup, "_kickstart_paperclip_share_launchd", lambda: (_ for _ in ()).throw(AssertionError("kickstart should not run")))

    startup._ensure_paperclip_share_tunnel()
