from __future__ import annotations

from skills.ops.database import backup_restore


def test_local_profile_can_use_loopback_remote_fallback(monkeypatch):
    remote = backup_restore.DBProfile(
        name="Studio_VPN_Remote",
        host="127.0.0.1",
        port=3306,
        user="casper_service",
        password="secret",
        database="law_firm_data",
    )
    local = backup_restore.DBProfile(
        name="Studio_Local",
        host="127.0.0.1",
        port=3306,
        user="python_user",
        password="",
        database="law_firm_data",
    )

    def fake_ping(profile):
        return bool(profile.password)

    monkeypatch.setattr(backup_restore, "_ping_db", fake_ping)
    chosen = backup_restore._choose_local_profile(
        {"Studio_Local": local, "Studio_VPN_Remote": remote}
    )

    assert chosen.name == "Studio_VPN_Remote_as_local"
    assert chosen.host == "127.0.0.1"
    assert chosen.user == "casper_service"
    assert chosen.password == "secret"
