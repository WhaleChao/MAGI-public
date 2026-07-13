from scripts.ops import obsidian_acceptance_gate as gate


def test_yellow_acceptance_is_operational_with_visible_warnings(monkeypatch):
    import scripts
    import skills.obsidian

    class FakeObsidian:
        AGENT_DIR = "/tmp/agent"

        @staticmethod
        def task_status():
            return {"vault_configured": True, "vault_path": "/tmp/vault"}

        @staticmethod
        def task_cleanup_duplicate_notes(dry_run=True):
            return {"success": True, "planned_moves": 0, "duplicate_groups": 0}

    class FakeLint:
        @staticmethod
        def check_obsidian_summary_quality():
            return {"status": "warn", "bad_notes": 1, "total_notes": 10, "issue_count": 1}

        @staticmethod
        def check_wiki_staleness():
            return {"status": "ok", "stale_cases": 0}

        @staticmethod
        def check_orphan_notes():
            return {"status": "ok", "unindexed": 0, "orphaned_index_entries": 0, "zero_chunk_notes": 0}

    monkeypatch.setitem(__import__("sys").modules, "skills.obsidian.action", FakeObsidian)
    monkeypatch.setitem(__import__("sys").modules, "scripts.knowledge_lint", FakeLint)
    monkeypatch.setattr(skills.obsidian, "action", FakeObsidian, raising=False)
    monkeypatch.setattr(scripts, "knowledge_lint", FakeLint, raising=False)

    report = gate.run_gate()

    assert report["ok"] is True
    assert report["status"] == "YELLOW"
    assert report["summary"] == {"pass": 4, "warn": 1, "fail": 0}
