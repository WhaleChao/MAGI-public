from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _write_skill(root: Path, name: str, description: str) -> Path:
    skill = root / "skills" / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"# {name}\n",
        encoding="utf-8",
    )
    (skill / "action.py").write_text("print('base-action')\n", encoding="utf-8")
    return skill


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def test_overlay_precedence_and_copy_on_write_keep_release_immutable(tmp_path, monkeypatch):
    from skills import overlay

    release = tmp_path / "release"
    base = _write_skill(release, "demo-skill", "base description")
    overlay_root = tmp_path / "shared" / "skill-overlays"
    monkeypatch.setattr(overlay, "_ROOT", release)
    monkeypatch.setenv("MAGI_SKILL_OVERLAY_DIR", str(overlay_root))
    before = _tree_digest(release)

    assert overlay.effective_skill_file("demo-skill", "SKILL.md") == base / "SKILL.md"
    target = overlay.mutable_skill_file("demo-skill", "SKILL.md")
    target.write_text(
        "---\nname: demo-skill\ndescription: overlay description\n---\n",
        encoding="utf-8",
    )

    assert (overlay_root / "demo-skill" / "action.py").read_text(encoding="utf-8") == "print('base-action')\n"
    assert overlay.effective_skill_file("demo-skill", "SKILL.md") == target
    assert _tree_digest(release) == before

    from skills.plugin import SkillRegistry

    registry = SkillRegistry(skills_dirs=[str(overlay_root), str(release / "skills")])
    assert registry.discover(force=True) == 1
    listed = registry.list_skills()
    assert listed[0]["description"] == "overlay description"
    assert listed[0]["source"] == "overlay"
    assert registry._resolve_action_path("demo-skill") == str(base / "action.py")
    (overlay_root / "demo-skill" / "action.py").write_text(
        "print('overlay-action')\n", encoding="utf-8"
    )
    assert registry._resolve_action_path("demo-skill") == str(
        overlay_root / "demo-skill" / "action.py"
    )


def test_docs_only_overlay_rebases_release_a_to_b_but_preserves_user_code(tmp_path, monkeypatch):
    from skills import overlay

    release = tmp_path / "release"
    base = _write_skill(release, "docs-only", "release A")
    (base / "support").mkdir()
    (base / "support" / "helper.py").write_text("VALUE = 'A'\n", encoding="utf-8")
    user_base = _write_skill(release, "user-code", "release A")
    (user_base / "support").mkdir()
    (user_base / "support" / "helper.py").write_text("VALUE = 'A'\n", encoding="utf-8")
    overlay_root = tmp_path / "shared" / "skill-overlays"
    monkeypatch.setattr(overlay, "_ROOT", release)
    monkeypatch.setenv("MAGI_SKILL_OVERLAY_DIR", str(overlay_root))

    docs = overlay.mutable_skill_file("docs-only", "SKILL.md")
    docs.write_text(
        "---\nname: docs-only\ndescription: user documentation\n---\n",
        encoding="utf-8",
    )
    user_support = overlay.ensure_overlay_skill("user-code") / "support" / "helper.py"
    user_support.write_text("VALUE = 'USER'\n", encoding="utf-8")

    (base / "action.py").write_text("print('release-B')\n", encoding="utf-8")
    (base / "support" / "helper.py").write_text("VALUE = 'B'\n", encoding="utf-8")
    (base / "support" / "new.py").write_text("NEW = 'B'\n", encoding="utf-8")
    (user_base / "action.py").write_text("print('release-B')\n", encoding="utf-8")
    (user_base / "support" / "helper.py").write_text("VALUE = 'B'\n", encoding="utf-8")
    release_b_digest = _tree_digest(release)

    assert overlay.runtime_skill_dir("docs-only") == base
    assert (overlay_root / "docs-only" / "action.py").read_text(encoding="utf-8") == "print('release-B')\n"
    assert (overlay_root / "docs-only" / "support" / "helper.py").read_text(encoding="utf-8") == "VALUE = 'B'\n"
    assert (overlay_root / "docs-only" / "support" / "new.py").read_text(encoding="utf-8") == "NEW = 'B'\n"
    assert "user documentation" in docs.read_text(encoding="utf-8")

    assert overlay.runtime_skill_dir("user-code") == overlay_root / "user-code"
    assert (overlay_root / "user-code" / "action.py").read_text(encoding="utf-8") == "print('release-B')\n"
    assert user_support.read_text(encoding="utf-8") == "VALUE = 'USER'\n"
    assert _tree_digest(release) == release_b_digest


def test_nerv_edit_snapshot_rollback_and_definitions_are_external(tmp_path, monkeypatch):
    from skills import overlay
    from skills.evolution import skill_genesis as genesis

    release = tmp_path / "release"
    base = _write_skill(release, "demo-skill", "version A")
    definitions = {
        "_meta": {"version": "1"},
        "tools": [{"name": "base_tool", "description": "base"}],
    }
    (release / "skills" / "definitions.json").write_text(
        json.dumps(definitions, ensure_ascii=False), encoding="utf-8"
    )
    overlay_root = tmp_path / "shared" / "skill-overlays"
    monkeypatch.setattr(overlay, "_ROOT", release)
    monkeypatch.setenv("MAGI_SKILL_OVERLAY_DIR", str(overlay_root))
    monkeypatch.setattr(genesis, "BASE_SKILLS_DIR", str(release / "skills"))
    monkeypatch.setattr(genesis, "SKILLS_DIR", str(overlay_root))
    monkeypatch.setattr(genesis, "SKILL_VERSIONS_DIR", str(overlay_root / ".versions"))
    monkeypatch.setattr(genesis, "SKILL_EVENTS_FILE", str(overlay_root / ".logs" / "events.jsonl"))
    monkeypatch.setattr(genesis, "SKILL_USAGE_TRACKER_FILE", str(overlay_root / ".logs" / "usage.jsonl"))
    monkeypatch.setattr(
        genesis.dome_override,
        "request_override",
        lambda **kwargs: {"blocked": False, "message": ""},
    )
    before = _tree_digest(release)
    from skills.plugin import SkillRegistry

    registry = SkillRegistry(skills_dirs=[str(overlay_root), str(release / "skills")])
    registry.discover(force=True)
    assert registry.list_skills()[0]["description"] == "version A"

    version_b = "---\nname: demo-skill\ndescription: version B\n---\n"
    first = genesis.update_skill_document("demo-skill", version_b)
    assert first["success"] is True
    assert first["snapshot"]["files"] == ["SKILL.md", "action.py"]
    registry.discover(force=True)
    assert registry.list_skills()[0]["description"] == "version B"
    assert genesis._resolve_run_target("demo-skill")["skill_dir"] == str(base)

    version_c = "---\nname: demo-skill\ndescription: version C\n---\n"
    second = genesis.update_skill_document("demo-skill", version_c)
    assert second["success"] is True
    rollback = genesis.rollback_skill_version(
        "demo-skill", second["snapshot"]["version_id"]
    )
    assert rollback["success"] is True
    assert (overlay_root / "demo-skill" / "SKILL.md").read_text(encoding="utf-8") == version_b
    assert (overlay_root / "demo-skill" / "action.py").read_text(encoding="utf-8") == "print('base-action')\n"
    registry.discover(force=True)
    assert registry.list_skills()[0]["description"] == "version B"

    registered = genesis._register_skill_tool_definition("demo-skill", "overlay tool")
    assert registered["success"] is True
    overlay_definitions = json.loads((overlay_root / "definitions.json").read_text(encoding="utf-8"))
    assert {tool["name"] for tool in overlay_definitions["tools"]} == {
        "base_tool",
        "run_demo_skill",
    }
    import skills.bridge.semantic_router as semantic_router
    from skills.bridge.embedding_router import EmbeddingRouter

    semantic_router._SKILLS_CACHE = None
    semantic_router._SKILLS_CACHE_TS = 0.0
    assert {item["name"] for item in semantic_router._load_skills()} == {
        "base_tool",
        "run_demo_skill",
    }
    assert set(EmbeddingRouter()._load_skills()) == {"base_tool", "run_demo_skill"}
    assert json.loads((release / "skills" / "definitions.json").read_text(encoding="utf-8")) == definitions
    assert base.joinpath("SKILL.md").read_text(encoding="utf-8").find("version A") >= 0
    assert _tree_digest(release) == before


def test_full_tree_snapshot_rollback_deletes_extras_and_reloads_registry(tmp_path, monkeypatch):
    from skills import overlay
    from skills.evolution import skill_genesis as genesis
    from skills.plugin import skill_registry

    release = tmp_path / "release"
    base = _write_skill(release, "tree-skill", "version A")
    (base / "support").mkdir()
    (base / "support" / "helper.py").write_text("VALUE = 'A'\n", encoding="utf-8")
    (base / "config.json").write_text('{"version":"A"}\n', encoding="utf-8")
    overlay_root = tmp_path / "shared" / "skill-overlays"
    versions = overlay_root / ".versions"
    monkeypatch.setattr(overlay, "_ROOT", release)
    monkeypatch.setenv("MAGI_SKILL_OVERLAY_DIR", str(overlay_root))
    monkeypatch.setattr(genesis, "BASE_SKILLS_DIR", str(release / "skills"))
    monkeypatch.setattr(genesis, "SKILLS_DIR", str(overlay_root))
    monkeypatch.setattr(genesis, "SKILL_VERSIONS_DIR", str(versions))
    monkeypatch.setattr(genesis, "SKILL_EVENTS_FILE", str(overlay_root / ".logs" / "events.jsonl"))
    monkeypatch.setattr(
        genesis.dome_override,
        "request_override",
        lambda **kwargs: {"blocked": False, "message": ""},
    )
    monkeypatch.setattr(skill_registry, "_skills_dirs", [str(overlay_root), str(release / "skills")])
    skill_registry.discover(force=True)

    edited = genesis.update_skill_document(
        "tree-skill",
        "---\nname: tree-skill\ndescription: version B\n---\n",
    )
    snapshot = edited["snapshot"]
    assert snapshot["files"] == [
        "SKILL.md",
        "action.py",
        "config.json",
        "support/helper.py",
    ]

    mutable = overlay_root / "tree-skill"
    (mutable / "action.py").write_text("print('changed')\n", encoding="utf-8")
    (mutable / "support" / "helper.py").write_text("VALUE = 'CHANGED'\n", encoding="utf-8")
    (mutable / "config.json").unlink()
    (mutable / "support" / "extra.py").write_text("EXTRA = True\n", encoding="utf-8")
    (mutable / "new-top.txt").write_text("must disappear\n", encoding="utf-8")

    result = genesis.rollback_skill_version("tree-skill", snapshot["version_id"])

    assert result["success"] is True
    assert result["restored_files"] == snapshot["files"]
    assert (mutable / "action.py").read_text(encoding="utf-8") == "print('base-action')\n"
    assert (mutable / "support" / "helper.py").read_text(encoding="utf-8") == "VALUE = 'A'\n"
    assert (mutable / "config.json").read_text(encoding="utf-8") == '{"version":"A"}\n'
    assert not (mutable / "support" / "extra.py").exists()
    assert not (mutable / "new-top.txt").exists()
    listed = {item["folder"]: item for item in skill_registry.list_skills()}
    assert listed["tree-skill"]["description"] == "version A"


def test_runtime_mutation_paths_live_under_overlay(tmp_path, monkeypatch):
    from skills import overlay

    shared = tmp_path / "shared"
    monkeypatch.setenv("MAGI_V3_SHARED_STATE_DIR", str(shared))
    monkeypatch.delenv("MAGI_SKILL_OVERLAY_DIR", raising=False)
    monkeypatch.delenv("MAGI_SKILL_RUNTIME_SITE_PACKAGES", raising=False)
    monkeypatch.delenv("MAGI_SKILL_EVENTS_FILE", raising=False)
    monkeypatch.delenv("MAGI_SKILL_USAGE_TRACKER_FILE", raising=False)

    root = shared / "skill-overlays"
    assert overlay.skill_overlay_dir() == root
    assert overlay.skill_versions_dir() == root / ".versions"
    assert overlay.skill_runtime_site_packages_dir() == root / ".runtime-site-packages"
    assert overlay.skill_events_file() == root / ".logs" / "skill_runtime_events.jsonl"
    assert overlay.skill_usage_tracker_file() == root / ".logs" / "skill_usage_events.jsonl"


def test_generate_install_and_autoskill_write_only_overlay(tmp_path, monkeypatch):
    from skills import overlay
    from skills.evolution import skill_genesis as genesis
    from skills.management import auto_skill

    release = tmp_path / "release"
    (release / "skills").mkdir(parents=True)
    overlay_root = tmp_path / "shared" / "skill-overlays"
    monkeypatch.setattr(overlay, "_ROOT", release)
    monkeypatch.setenv("MAGI_SKILL_OVERLAY_DIR", str(overlay_root))
    monkeypatch.setattr(genesis, "BASE_SKILLS_DIR", str(release / "skills"))
    monkeypatch.setattr(genesis, "SKILLS_DIR", str(overlay_root))
    monkeypatch.setattr(genesis, "SKILL_VERSIONS_DIR", str(overlay_root / ".versions"))
    monkeypatch.setattr(genesis, "validate_skill_safety", lambda content: (True, []))
    before = _tree_digest(release)

    generated = genesis.generate_skill("generated-demo", "generated", "do the task")
    assert generated["success"] is True
    assert Path(generated["path"]).is_relative_to(overlay_root)
    assert genesis._resolve_run_target("generated-demo")["skill_dir"] == str(
        overlay_root / "generated-demo"
    )

    monkeypatch.setattr(genesis, "MAGI_ALLOW_INTERNET", True)
    monkeypatch.setattr(
        genesis,
        "fetch_skill_from_url",
        lambda url: {
            "success": True,
            "content": "---\nname: installed-demo\ndescription: installed\n---\n",
            "error": None,
        },
    )
    installed = genesis.install_skill_from_url(
        "https://example.invalid/SKILL.md", require_hitl=False
    )
    assert installed["success"] is True
    assert Path(installed["path"]).is_relative_to(overlay_root)

    monkeypatch.setattr(auto_skill, "SKILLS_ROOT", str(overlay_root))
    learned = auto_skill.AutoSkill.__new__(auto_skill.AutoSkill)
    learned.knowledge = [
        {
            "tip": "Use an isolated overlay.",
            "keywords": ["overlay"],
            "timestamp": "2026-07-16T00:00:00",
        }
    ]
    internalized = learned.internalize_as_skill(
        skill_name="learned-demo", auto_activate=False
    )
    assert internalized["success"] is True
    assert Path(internalized["skill_path"]).is_relative_to(overlay_root)
    assert _tree_digest(release) == before


def test_overlay_rejects_symlink_escape(tmp_path, monkeypatch):
    from skills import overlay

    release = tmp_path / "release"
    _write_skill(release, "demo-skill", "base")
    overlay_root = tmp_path / "shared" / "skill-overlays"
    outside = tmp_path / "outside"
    outside.mkdir()
    overlay_root.parent.mkdir(parents=True)
    overlay_root.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(overlay, "_ROOT", release)
    monkeypatch.setenv("MAGI_SKILL_OVERLAY_DIR", str(overlay_root))

    try:
        overlay.ensure_overlay_skill("demo-skill")
    except ValueError as exc:
        assert "unsafe_skill_overlay_root" in str(exc)
    else:
        raise AssertionError("symlinked overlay root must fail closed")


def test_iron_dome_approval_commits_to_overlay_not_release(tmp_path, monkeypatch):
    from skills import overlay
    from skills.iron_dome import protocol_override

    release = tmp_path / "release"
    base = _write_skill(release, "demo-skill", "base")
    overlay_root = tmp_path / "shared" / "skill-overlays"
    pending = tmp_path / "shared" / "agent" / "pending.json"
    pending.parent.mkdir(parents=True)
    pending.write_text(
        json.dumps(
            {
                "skill_name": "demo-skill",
                "files": {"SKILL.md": "---\nname: demo-skill\ndescription: approved\n---\n"},
                "reason": "test",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(overlay, "_ROOT", release)
    monkeypatch.setenv("MAGI_SKILL_OVERLAY_DIR", str(overlay_root))
    function_globals = protocol_override.approve_override.__globals__
    monkeypatch.setitem(function_globals, "PENDING_FILE", str(pending))

    class _Notifier:
        def notify_admin(self, *args, **kwargs):
            return True

    monkeypatch.setitem(function_globals, "LAFNotifier", _Notifier)
    before = _tree_digest(release)

    result = protocol_override.approve_override()

    assert result["success"] is True
    assert "approved" in (overlay_root / "demo-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "base" in (base / "SKILL.md").read_text(encoding="utf-8")
    assert _tree_digest(release) == before
    assert not pending.exists()


def test_iron_dome_partial_approval_failure_keeps_proposal_and_old_tree(tmp_path, monkeypatch):
    from skills import overlay
    from skills.iron_dome import protocol_override

    release = tmp_path / "release"
    base = _write_skill(release, "demo-skill", "base")
    overlay_root = tmp_path / "shared" / "skill-overlays"
    pending = tmp_path / "shared" / "agent" / "pending.json"
    pending.parent.mkdir(parents=True)
    pending.write_text(
        json.dumps(
            {
                "skill_name": "demo-skill",
                "files": {
                    "SKILL.md": "---\nname: demo-skill\ndescription: approved\n---\n",
                    "action.py": "print('approved')\n",
                },
                "reason": "test",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(overlay, "_ROOT", release)
    monkeypatch.setenv("MAGI_SKILL_OVERLAY_DIR", str(overlay_root))
    function_globals = protocol_override.approve_override.__globals__
    monkeypatch.setitem(function_globals, "PENDING_FILE", str(pending))
    writes = []

    def _fail_second(path, content):
        writes.append(path.name)
        if len(writes) == 2:
            raise OSError("simulated second-file failure")
        path.write_text(content, encoding="utf-8")

    monkeypatch.setitem(function_globals, "_write_override_file", _fail_second)

    class _Notifier:
        calls = []

        def notify_admin(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return True

    monkeypatch.setitem(function_globals, "LAFNotifier", _Notifier)
    before = _tree_digest(release)

    result = protocol_override.approve_override()

    assert result["success"] is False
    assert pending.exists()
    assert _Notifier.calls == []
    assert (overlay_root / "demo-skill" / "SKILL.md").read_text(encoding="utf-8") == base.joinpath("SKILL.md").read_text(encoding="utf-8")
    assert (overlay_root / "demo-skill" / "action.py").read_text(encoding="utf-8") == "print('base-action')\n"
    assert _tree_digest(release) == before


def test_iron_dome_reads_v2_seed_but_writes_only_external_state(tmp_path, monkeypatch):
    from skills.iron_dome import core, sync

    release = tmp_path / "release"
    legacy_dynamic = release / "skills" / "evolution" / "iron_dome_dynamic_rules.json"
    legacy_cache = release / "static" / "iron_dome_patterns.json"
    legacy_dynamic.parent.mkdir(parents=True)
    legacy_cache.parent.mkdir(parents=True)
    legacy_state = {
        "patterns": [
            {
                "id": "v2-seed",
                "pattern": "legacy-only-pattern",
                "enabled": True,
            }
        ]
    }
    legacy_dynamic.write_text(
        json.dumps(legacy_state, ensure_ascii=False), encoding="utf-8"
    )
    legacy_cache.write_text(
        json.dumps({"patterns": {"dynamic": ["legacy-cache-pattern"]}}),
        encoding="utf-8",
    )
    external = tmp_path / "shared" / "skill-overlays" / ".iron-dome"
    external_dynamic = external / "dynamic_rules.json"
    external_cache = external / "patterns_cache.json"
    before = _tree_digest(release)

    core_globals = core._load_dynamic_state.__globals__
    monkeypatch.setitem(core_globals, "IRON_DOME_DYNAMIC_RULES_PATH", str(external_dynamic))
    monkeypatch.setitem(core_globals, "_LEGACY_DYNAMIC_RULES_PATH", str(legacy_dynamic))
    monkeypatch.setitem(core_globals, "PATTERNS_CACHE_FILE", str(external_cache))
    monkeypatch.setitem(core_globals, "_LEGACY_PATTERNS_CACHE_FILE", str(legacy_cache))

    # A V3 overlay that has not been initialized may read the V2 release seed.
    assert core._load_dynamic_state() == legacy_state

    # The first mutation forks state externally and never changes the seed.
    monkeypatch.setitem(core_globals, "_reload_patterns", lambda force=False: True)
    updated = {
        "patterns": [
            {"id": "v3-active", "pattern": "overlay-pattern", "enabled": True}
        ]
    }
    assert core._save_dynamic_state(updated)["success"] is True
    assert json.loads(external_dynamic.read_text(encoding="utf-8"))["patterns"] == updated["patterns"]

    sync_globals = sync.export_patterns.__globals__
    monkeypatch.setitem(sync_globals, "PATTERNS_CACHE_FILE", str(external_cache))
    monkeypatch.setitem(sync_globals, "get_all_patterns", lambda: [])
    monkeypatch.setitem(
        sync_globals,
        "STATIC_RULE_SETS",
        {"PROMPT_INJECTION": ["safe"], "DESTRUCTIVE_COMMAND": [], "SENSITIVE_DATA": []},
    )
    exported = sync.export_patterns()
    assert exported["patterns"]["prompt_injection"] == ["safe"]
    assert json.loads(external_cache.read_text(encoding="utf-8"))["source_node"]

    assert json.loads(legacy_dynamic.read_text(encoding="utf-8")) == legacy_state
    assert _tree_digest(release) == before
