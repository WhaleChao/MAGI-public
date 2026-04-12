from __future__ import annotations


def test_skill_preexec_disabled_by_default():
    from skills.evolution import skill_genesis

    assert skill_genesis.SKILL_ENABLE_PREEXEC is False
    assert skill_genesis._skill_preexec() is None


def test_skill_preexec_disabled_in_multithreaded_runtime(monkeypatch):
    from skills.evolution import skill_genesis

    monkeypatch.setattr(skill_genesis, "SKILL_ENABLE_PREEXEC", True)
    monkeypatch.setattr("skills.evolution.skill_genesis.threading.active_count", lambda: 2)

    assert skill_genesis._skill_preexec() is None


def test_skill_cmd_prefers_magi_skill_python(monkeypatch):
    from skills.evolution import skill_genesis

    monkeypatch.setattr(skill_genesis, "SKILL_PYTHON", "/tmp/magi-skill-python")
    monkeypatch.setattr(skill_genesis.os.path, "exists", lambda path: path == "/tmp/magi-skill-python")

    assert skill_genesis._skill_cmd("--help") == ["/tmp/magi-skill-python", "action.py", "--help"]
