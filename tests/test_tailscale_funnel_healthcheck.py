from scripts.ops import tailscale_funnel_healthcheck as healthcheck


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
