from __future__ import annotations

from skills.ops import process_guardian


class _Proc:
    def __init__(self, pid: int, name: str, cmdline: list[str], create_time: float = 1.0):
        self.info = {
            "pid": pid,
            "name": name,
            "cmdline": cmdline,
            "create_time": create_time,
        }


def test_get_running_processes_does_not_count_guardian_invocation(monkeypatch):
    monkeypatch.setattr(process_guardian.os, "getpid", lambda: 100)
    monkeypatch.setattr(
        process_guardian.psutil,
        "process_iter",
        lambda _fields: [
            _Proc(100, "python3", ["python3", "skills/ops/process_guardian.py", "api/discord_bot.py"]),
            _Proc(222, "python3", ["python3", "api/discord_bot.py"], create_time=2.0),
        ],
    )

    matches = process_guardian.get_running_processes("api/discord_bot.py")

    assert [match["pid"] for match in matches] == [222]
