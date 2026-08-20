from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace


class _OfflineAutoSkill:
    def _ok(self, **kwargs):
        return {"success": True, "fixture": kwargs}

    def teach(self, lesson, **kwargs):
        return self._ok(lesson=lesson, **kwargs)

    def learn(self, keywords, lesson, **kwargs):
        return self._ok(keywords=keywords, lesson=lesson, **kwargs)

    def learn_from_file(self, file_path, **kwargs):
        return self._ok(file_path=file_path, **kwargs)

    def internalize_as_skill(self, **kwargs):
        return self._ok(**kwargs)

    def internalize_codebase_as_skills(self, **kwargs):
        return self._ok(**kwargs)

    def import_toolsai_auto_skill(self, **kwargs):
        return self._ok(**kwargs)


def test_tools_success_paths_use_exact_in_memory_provider_adapters(monkeypatch, tmp_path):
    import api.tools_api as tools
    import api.authz as authz
    import skills.evolution.skill_genesis as genesis
    import skills.management.auto_skill as auto_skill
    import skills.bridge.tri_sage_collab as collab
    import skills.magi.council_approval as council
    import skills.memory.mem_bridge as memory
    import skills.law_firm.manage_clients as clients
    import skills.law_firm.manage_meetings as meetings
    import skills.bridge.legal_bridge as legal
    import skills.ops.red_phone as red_phone
    import scripts.code_skill_cycle as code_cycle
    import skills.management.code_autofix as code_autofix

    monkeypatch.setitem(tools.app.config, "MAGI_CSRF_TEST_MODE", True)
    monkeypatch.setattr(authz, "_check_api_key", lambda value: value == "offline-key")
    monkeypatch.setattr(authz, "_log_access", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools, "_check_external_api_key", lambda: (True, ""))
    monkeypatch.setattr(tools, "_resolve_external_api_key", lambda: "offline-key")
    monkeypatch.setattr(tools, "_check_tool_access", lambda *args, **kwargs: (True, None))
    monkeypatch.setattr(tools, "_start_tool_event", lambda *args, **kwargs: 1.0)
    monkeypatch.setattr(tools, "_finish_tool_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools, "_validate_fetch_url", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(tools, "_record_transcribe_metric", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools, "search_web", lambda query, count: [{"query": query, "count": count}])
    monkeypatch.setattr(
        tools,
        "research_topic",
        lambda topic, depth: {"topic": topic, "sources": [], "combined_content": f"depth={depth}"},
    )
    monkeypatch.setattr(tools, "fetch_url_content", lambda url: {"success": True, "url": url})
    monkeypatch.setattr(tools, "sync_skills_to_melchior", lambda *args, **kwargs: {"success": True})
    monkeypatch.setattr(tools, "generate_skill", lambda *args, **kwargs: {"success": True})
    monkeypatch.setattr(
        tools,
        "_run_with_timeout",
        lambda function, _timeout, *args, **kwargs: (True, function(*args)),
    )
    monkeypatch.setattr(auto_skill, "AutoSkill", _OfflineAutoSkill)
    monkeypatch.setattr(collab, "translate_text", lambda *args, **kwargs: {"success": True})
    monkeypatch.setattr(collab, "generate_music", lambda *args, **kwargs: {"success": True})
    monkeypatch.setattr(collab, "transcribe_audio", lambda *args, **kwargs: {"success": True, "text": "ok"})
    monkeypatch.setattr(council, "resolve_core_change", lambda *args, **kwargs: {"success": True})
    monkeypatch.setattr(memory, "remember", lambda *args, **kwargs: True)
    monkeypatch.setattr(memory, "recall", lambda *args, **kwargs: ["offline"])
    monkeypatch.setattr(clients, "add_client", lambda *args, **kwargs: {"success": True})
    monkeypatch.setattr(meetings, "book_meeting", lambda *args, **kwargs: {"success": True})
    monkeypatch.setattr(legal, "execute_skill", lambda *args, **kwargs: {"success": True})
    monkeypatch.setattr(red_phone, "alert_admin", lambda *args, **kwargs: {"success": True})
    monkeypatch.setattr(code_cycle, "run_cycle", lambda: {"success": True})
    monkeypatch.setattr(code_autofix, "autofix_codebase", lambda **kwargs: {"success": True})

    success = lambda *args, **kwargs: {"success": True}
    for name in (
        "auto_discover_and_suggest",
        "auto_install_skill",
        "acquire_skill",
        "list_skill_versions",
        "rollback_skill_version",
        "get_skill_release_state",
        "set_stable_skill_version",
        "start_canary_release",
        "stop_canary_release",
        "run_skill_ci",
        "add_iron_dome_pattern",
        "auto_harden_iron_dome_scope",
    ):
        monkeypatch.setattr(genesis, name, success, raising=False)
    monkeypatch.setattr(genesis, "run_skill_action", success)

    auth = {"X-API-Key": "offline-key"}
    client = tools.app.test_client()
    requests = [
        ("/search", {"query": "offline"}),
        ("/research", {"topic": "offline"}),
        ("/fetch", {"url": "https://example.test/offline"}),
        ("/melchior/skills/sync", {"skills_dir": str(tmp_path)}),
        ("/skills", {"name": "offline", "description": "d", "instructions": "i"}),
        ("/skills/discover", {"need": "offline"}),
        ("/skills/install", {"name": "offline"}),
        ("/skills/acquire", {"need": "offline"}),
        ("/skills/versions", {"skill": "offline"}),
        ("/skills/rollback", {"skill": "offline", "version_id": "v1"}),
        ("/skills/stable", {"skill": "offline", "version_id": "v1"}),
        ("/skills/canary/start", {"skill": "offline", "version_id": "v1"}),
        ("/skills/canary/stop", {"skill": "offline"}),
        ("/skills/ci", {"skill": "offline"}),
        ("/skills/teach", {"lesson": "offline"}),
        ("/skills/teach/file", {"file_path": str(tmp_path / "offline.txt")}),
        ("/skills/internalize", {"skill_name": "offline"}),
        ("/skills/internalize/codebase", {"source_dir": str(tmp_path)}),
        ("/skills/import/toolsai-auto-skill", {"local_path": str(tmp_path)}),
        ("/iron-dome/patterns", {"pattern": "offline"}),
        ("/iron-dome/auto-harden", {"incident": "offline"}),
        ("/code/autofix", {"target": str(tmp_path), "dry_run": True}),
        ("/code/skill-cycle", {}),
        ("/collab/translate", {"text": "offline"}),
        ("/collab/music", {"prompt": "offline"}),
        ("/collab/transcribe", {"audio_path": str(tmp_path / "offline.wav")}),
        ("/council/core/approve", {"approval_id": "offline"}),
        ("/council/core/reject", {"approval_id": "offline"}),
        ("/remember", {"content": "offline"}),
        ("/recall", {"query": "offline"}),
        ("/clients", {"code": "OFF", "name": "Offline"}),
        ("/meetings", {"title": "Offline", "start": "2026-07-16T00:00:00+08:00"}),
        ("/legal/offline", {"args": []}),
        ("/alert", {"message": "offline"}),
        ("/laf/smoke_login", {"mock_mode": True}),
        ("/osc/external/case_status", {"query": "offline"}),
    ]
    for path, body in requests:
        response = client.post(path, json=body, headers=auth)
        assert response.status_code == 200, (path, response.get_json())

    response = client.get("/skills/release?skill=offline", headers=auth)
    assert response.status_code == 200


def test_audit_restore_success_is_journaled_in_disposable_transaction(monkeypatch):
    import api.tools_api as tools
    import api.authz as authz
    import api.db_helper as db_helper

    monkeypatch.setitem(tools.app.config, "MAGI_CSRF_TEST_MODE", True)
    monkeypatch.setattr(authz, "_check_api_key", lambda value: value == "offline-key")
    monkeypatch.setattr(authz, "_log_access", lambda *args, **kwargs: None)

    class Cursor:
        statements = []

        def execute(self, sql, params=None):
            self.statements.append((" ".join(sql.split()), params))

        def fetchone(self):
            return {
                "target_db": "magi_brain",
                "table_name": "fixture_table",
                "record_id": 7,
                "old_value": {"id": 7, "status": "offline"},
            }

    class Connection:
        committed = False

        def commit(self):
            self.committed = True

    cursor = Cursor()
    connection = Connection()

    @contextmanager
    def get_cursor(**kwargs):
        assert kwargs["dictionary"] is True
        yield connection, cursor

    monkeypatch.setattr(tools, "_check_external_api_key", lambda: (True, ""))
    monkeypatch.setattr(db_helper, "get_cursor", get_cursor)
    response = tools.app.test_client().post(
        "/api/audit_log/restore/7",
        headers={"X-API-Key": "offline-key"},
    )

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert connection.committed is True
    assert [statement[0].split()[0] for statement in cursor.statements] == [
        "SELECT",
        "USE",
        "UPDATE",
        "USE",
        "INSERT",
    ]


def test_external_chat_and_vision_success_use_offline_provider_adapters(monkeypatch, tmp_path):
    import api.authz as authz
    import api.tools_api as tools
    import skills.bridge.grounded_ai as grounded_ai

    calls: list[tuple[str, object]] = []

    class OfflineOrchestrator:
        @staticmethod
        def _is_verified_admin_sender(*, user_id, platform):
            calls.append(("verified_admin", (user_id, platform)))
            return False

        @staticmethod
        def process_message(*, user_id, message, platform, role):
            calls.append(("chat", (user_id, message, platform, role)))
            return "offline reply"

    class OfflineInferenceGateway:
        @staticmethod
        def vision(**kwargs):
            calls.append(("vision", kwargs))
            return {
                "success": True,
                "analysis": "offline image description",
                "route": "offline_fixture",
                "model": "fixture-model",
                "degraded": False,
            }

    monkeypatch.setitem(tools.app.config, "MAGI_CSRF_TEST_MODE", True)
    monkeypatch.setattr(authz, "_check_api_key", lambda value: value == "offline-key")
    monkeypatch.setattr(authz, "_log_access", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools, "_check_external_api_key", lambda: (True, ""))
    monkeypatch.setattr(tools, "_get_osc_orchestrator", lambda: OfflineOrchestrator())
    monkeypatch.setattr(tools, "_record_external_chat_metric", lambda **kwargs: calls.append(("chat_metric", kwargs)))
    monkeypatch.setattr(tools, "_check_tool_access", lambda *args, **kwargs: (True, None))
    monkeypatch.setattr(tools, "_start_tool_event", lambda *args, **kwargs: 1.0)
    monkeypatch.setattr(tools, "_finish_tool_event", lambda *args, **kwargs: calls.append(("tool_event", kwargs)))
    monkeypatch.setattr(
        tools,
        "_run_with_timeout",
        lambda function, _timeout, *args, **kwargs: (True, function(*args, **kwargs)),
    )
    monkeypatch.setattr(tools, "_INFERENCE_GATEWAY", OfflineInferenceGateway())
    monkeypatch.setattr(tools, "_VISION_OCR_CONSENSUS_ENABLE", False)
    monkeypatch.setattr(tools, "_nemotron_parse_enabled", lambda: False)
    monkeypatch.setattr(grounded_ai, "_classify_query_tier", lambda _message: "SIMPLE")
    tools._EXTERNAL_CHAT_INFLIGHT_COUNT[0] = 0

    image_path = tmp_path / "fixture.png"
    image_path.write_bytes(b"offline-image")
    client = tools.app.test_client()

    chat_response = client.post(
        "/osc/external/chat",
        json={
            "message": "short offline question",
            "user_id": "fixture-user",
            "platform": "WEB",
            "async": False,
        },
        headers={"X-API-Key": "offline-key"},
    )
    vision_response = client.post(
        "/vision",
        json={"image_path": str(image_path), "prompt": "describe fixture", "task_type": "vision"},
        headers={"X-API-Key": "offline-key"},
    )

    assert chat_response.status_code == 200
    assert chat_response.get_json()["success"] is True
    assert chat_response.get_json()["reply"] == "offline reply"
    assert tools._EXTERNAL_CHAT_INFLIGHT_COUNT[0] == 0
    assert vision_response.status_code == 200
    assert vision_response.get_json() == {
        "degraded": False,
        "description": "offline image description",
        "error": "",
        "force_local": True,
        "image": str(image_path),
        "model": "fixture-model",
        "route": "offline_fixture",
        "sage": "vision_gateway",
        "success": True,
        "task_type": "vision",
    }
    assert [name for name, _payload in calls].count("chat") == 1
    assert [name for name, _payload in calls].count("vision") == 1
    assert [name for name, _payload in calls].count("tool_event") == 1
