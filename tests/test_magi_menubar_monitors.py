from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

from gui.magi_menubar import (
    BUSINESS_LIVE_TIMEOUT_SEC,
    BUSINESS_MODULE_CHECKS,
    FACTORY_CHECKS,
    MAGI_HOME_URL,
    MONITOR_THREADS,
    OMLX_ENGINES,
    SERVICES,
    MAGIMenuBar,
    _business_module_status_failure,
    _business_module_status_from_payload,
    _business_module_status_live,
    _business_readiness_detail,
    _business_readiness_live,
    _check_omlx,
    _cron_details_from_state,
    _cron_failure_detail,
    _cron_stale_threshold_hours,
    _credential_detail,
    _cron_summary,
    _format_live_events,
    _health_state_from_payload,
    _live_log_events,
    _magi_process_memory_icon,
    _monitor_display_state,
    _omlx_text_status,
    _overall_state,
    _service_alive,
    _service_liveness,
    _system_memory_icon,
    _status_detail_text,
    _task_module_row_geometry,
)


def test_manual_business_live_check_has_full_probe_timeout():
    assert BUSINESS_LIVE_TIMEOUT_SEC >= 1200


def test_service_alive_accepts_display_name_aliases():
    assert _service_alive({"主伺服器": True}, "主伺服器", "Server") is True
    assert _service_alive({"Server": True}, "主伺服器", "Server") is True
    assert _service_alive({"主伺服器": False}, "主伺服器", "Server") is False


def test_menubar_text_status_marks_day_e4b_primary():
    status = _omlx_text_status("gemma-4-e4b-it-4bit", "day", "e4b", "day")
    assert status["icon"] == "🟢"
    assert status["degraded"] is False
    assert "日間4B" in status["label"]


def test_menubar_text_status_marks_night_e4b_degraded_fallback():
    status = _omlx_text_status("gemma-4-e4b-it-4bit", "night", "26b", "night-e4b-degraded")
    assert status["icon"] == "🟡"
    assert status["degraded"] is True
    assert "夜間降級E4B" in status["label"]
    assert "預期26B" in status["label"]


def test_check_omlx_returns_actual_model_instead_of_legacy_main_model_filter(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"data": [{"id": "gemma-4-12B-it-4bit"}]}).encode("utf-8")

    monkeypatch.setenv("MAGI_MAIN_MODEL", "gemma-4-e4b-it-4bit")
    monkeypatch.setattr("gui.magi_menubar.urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse())

    assert _check_omlx(8080) == "gemma-4-12B-it-4bit"


def test_menubar_memory_icons_match_operational_health_thresholds():
    assert _system_memory_icon(69.9) == "🟢"
    assert _system_memory_icon(85.0) == "🟡"
    assert _system_memory_icon(92.0) == "🔴"

    assert _magi_process_memory_icon(2368) == "🟢"
    assert _magi_process_memory_icon(4096) == "🟡"
    assert _magi_process_memory_icon(8192) == "🔴"


def test_collect_status_populates_monitors_when_cron_jobs_have_no_last_run(tmp_path, monkeypatch):
    from gui import magi_menubar as mod

    static = tmp_path / "static"
    static.mkdir()
    now = datetime.now().replace(microsecond=0).isoformat()
    (static / "laf_gmail_monitor_state.json").write_text(
        json.dumps({"status": "ok", "updated_at": now, "interval_sec": 300}, ensure_ascii=False),
        encoding="utf-8",
    )
    (static / "file_review_email_monitor_state.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "updated_at": now,
                "source": "laf_gmail_monitor_cycle",
                "running": True,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (static / "file_review_auto_state.json").write_text(
        json.dumps(
            {
                "updated_at": now,
                "interval_sec": 3600,
                "result": {"ok": True, "degraded": False},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "cron_jobs.json").write_text(
        json.dumps([{"id": "job_without_last_run", "enabled": True, "cron": "*/2 * * * *", "desc": "No last run"}]),
        encoding="utf-8",
    )

    monkeypatch.setattr(mod, "MAGI_ROOT", str(tmp_path))
    monkeypatch.setattr(mod, "_pgrep_any", lambda _patterns: "123")
    monkeypatch.setattr(mod, "_pgrep", lambda _pattern: "456")
    monkeypatch.setattr(mod, "_http_liveness", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(mod, "_check_omlx", lambda _port: "")
    monkeypatch.setattr(mod, "_active_omlx_profile", lambda: "day")
    monkeypatch.setattr(mod, "_tcp", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(mod, "_get_system_memory", lambda: (64.0, 32.0, 50.0))
    monkeypatch.setattr(mod, "_get_module_memory", lambda: [])
    monkeypatch.setattr(mod, "_count_zombies", lambda: (0, ""))

    fake_app = SimpleNamespace(_status_cache={}, _cache_lock=threading.Lock())
    MAGIMenuBar._collect_status(fake_app)

    monitors = fake_app._status_cache["monitors"]
    assert monitors["法扶信箱監控"]["state"] == "alive"
    assert monitors["法扶附件重試"]["state"] == "waiting"
    assert monitors["法扶附件重試"]["detail"] == "等待啟用"
    assert monitors["閱卷信箱監控"]["state"] == "alive"
    assert monitors["閱卷入口掃描"]["state"] == "alive"
    assert "_error" not in monitors


def test_collect_status_uses_laf_retry_heartbeat(tmp_path, monkeypatch):
    from gui import magi_menubar as mod

    static = tmp_path / "static"
    static.mkdir()
    now = datetime.now().replace(microsecond=0).isoformat()
    (static / "laf_portal_retry_state.json").write_text(
        json.dumps(
            {
                "ok": True,
                "status": "idle",
                "enabled": True,
                "updated_at": now,
                "interval_sec": 3600,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "cron_jobs.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(mod, "MAGI_ROOT", str(tmp_path))
    monkeypatch.setattr(mod, "_pgrep_any", lambda _patterns: "123")
    monkeypatch.setattr(mod, "_pgrep", lambda _pattern: "456")
    monkeypatch.setattr(mod, "_http_liveness", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(mod, "_check_omlx", lambda _port: "")
    monkeypatch.setattr(mod, "_active_omlx_profile", lambda: "day")
    monkeypatch.setattr(mod, "_tcp", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(mod, "_get_system_memory", lambda: (64.0, 32.0, 50.0))
    monkeypatch.setattr(mod, "_get_module_memory", lambda: [])
    monkeypatch.setattr(mod, "_count_zombies", lambda: (0, ""))

    fake_app = SimpleNamespace(_status_cache={}, _cache_lock=threading.Lock())
    MAGIMenuBar._collect_status(fake_app)

    retry = fake_app._status_cache["monitors"]["法扶附件重試"]
    assert retry["state"] == "alive"
    assert retry["detail"] == "等待下一輪"


def test_collect_status_shows_laf_retry_running(tmp_path, monkeypatch):
    from gui import magi_menubar as mod

    static = tmp_path / "static"
    static.mkdir()
    now = datetime.now().replace(microsecond=0).isoformat()
    (static / "laf_portal_retry_state.json").write_text(
        json.dumps({"ok": True, "status": "running", "updated_at": now, "interval_sec": 3600}),
        encoding="utf-8",
    )
    (tmp_path / "cron_jobs.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(mod, "MAGI_ROOT", str(tmp_path))
    monkeypatch.setattr(mod, "_pgrep_any", lambda _patterns: "123")
    monkeypatch.setattr(mod, "_pgrep", lambda _pattern: "456")
    monkeypatch.setattr(mod, "_http_liveness", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(mod, "_check_omlx", lambda _port: "")
    monkeypatch.setattr(mod, "_active_omlx_profile", lambda: "day")
    monkeypatch.setattr(mod, "_tcp", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(mod, "_get_system_memory", lambda: (64.0, 32.0, 50.0))
    monkeypatch.setattr(mod, "_get_module_memory", lambda: [])
    monkeypatch.setattr(mod, "_count_zombies", lambda: (0, ""))

    fake_app = SimpleNamespace(_status_cache={}, _cache_lock=threading.Lock())
    MAGIMenuBar._collect_status(fake_app)

    retry = fake_app._status_cache["monitors"]["法扶附件重試"]
    assert retry["state"] == "alive"
    assert retry["detail"] == "執行中"


def test_dashboard_waiting_monitor_is_not_attention():
    state, value = _monitor_display_state(
        {"state": "waiting", "detail": "等待啟用"},
        lambda value, _limit: value,
    )

    assert state == "waiting"
    assert value == "等待啟用"


def test_menubar_cron_details_use_runtime_cron_state_over_legacy_last_run(tmp_path, monkeypatch):
    from gui import magi_menubar as mod

    (tmp_path / "static").mkdir()
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    recent_success = (datetime.now() - timedelta(minutes=8)).replace(microsecond=0).isoformat()
    (tmp_path / "cron_jobs.json").write_text(
        json.dumps(
            [
                {
                    "id": "job_live_state",
                    "enabled": True,
                    "cron": "*/2 * * * *",
                    "desc": "Live state job",
                    "last_run": None,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (runtime / "cron_state.json").write_text(
        json.dumps(
            {
                "job_live_state": {
                    "last_success_at": recent_success,
                    "last_success": True,
                    "returncode": 0,
                    "timed_out": False,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(mod, "MAGI_ROOT", str(tmp_path))
    monkeypatch.delenv("MAGI_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(mod, "_pgrep_any", lambda _patterns: "123")
    monkeypatch.setattr(mod, "_pgrep", lambda _pattern: "456")
    monkeypatch.setattr(mod, "_http_liveness", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(mod, "_check_omlx", lambda _port: "")
    monkeypatch.setattr(mod, "_active_omlx_profile", lambda: "day")
    monkeypatch.setattr(mod, "_tcp", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(mod, "_get_system_memory", lambda: (64.0, 32.0, 50.0))
    monkeypatch.setattr(mod, "_get_module_memory", lambda: [])
    monkeypatch.setattr(mod, "_count_zombies", lambda: (0, ""))

    fake_app = SimpleNamespace(_status_cache={}, _cache_lock=threading.Lock())
    MAGIMenuBar._collect_status(fake_app)

    detail = fake_app._status_cache["cron_details"][0]
    assert detail["relative"] != "從未"
    assert detail["status"] == "ok"
    assert detail["stale"] is False


def test_menubar_cron_stale_threshold_matches_function_health_style():
    assert _cron_stale_threshold_hours("0 9 * * *") == 66.0
    assert _cron_stale_threshold_hours("*/2 * * * *") == 6.083


def test_business_module_status_uses_unified_operational_terms():
    check_names = {
        check
        for group in list(BUSINESS_MODULE_CHECKS.values()) + list(FACTORY_CHECKS.values())
        for check in group
    }
    check_names.add("token_health_refresh")
    payload = {
        "ok": True,
        "results": [{"name": name, "ok": True} for name in sorted(check_names)],
    }

    status = _business_module_status_from_payload(payload)

    assert {item["label"] for item in status["modules"].values()} == {"運作正常"}
    assert {item["label"] for item in status["factory"].values()} == {"檢查通過"}
    assert status["credential"]["label"] == "運作正常"


def test_live_events_do_not_synthesize_realtime_records_without_a_parseable_log():
    events = _format_live_events({"live_events": []})

    assert events == [{"time": "--:--", "source": "即時紀錄", "state": "waiting", "label": "無可解析紀錄"}]


def test_live_events_prefer_structured_server_log_records():
    events = _live_log_events(
        "\n".join(
            [
                json.dumps({"ts": "2026-07-10T08:00:00", "level": "INFO", "logger": "werkzeug", "msg": '127.0.0.1 - - [10/Jul/2026] "GET /health HTTP/1.1" 200 -'}),
                json.dumps({"ts": "2026-07-10T08:00:05", "level": "ERROR", "logger": "discord_bot", "msg": "cron dispatch failed"}),
            ]
        )
    )

    assert events[0] == {"time": "08:00", "source": "主伺服器", "state": "ok", "label": "/health　200"}
    assert events[1]["source"] == "通訊機器人"
    assert events[1]["state"] == "attention"


def test_live_log_http_statuses_use_warning_for_4xx_and_error_for_5xx():
    events = _live_log_events(
        "\n".join(
            [
                json.dumps({"ts": "2026-07-10T08:00:00", "level": "INFO", "logger": "werkzeug", "msg": '127.0.0.1 - - [10/Jul/2026] "GET /missing HTTP/1.1" 404 -'}),
                json.dumps({"ts": "2026-07-10T08:00:01", "level": "INFO", "logger": "werkzeug", "msg": '127.0.0.1 - - [10/Jul/2026] "GET /health HTTP/1.1" 503 -'}),
            ]
        )
    )

    assert [event["state"] for event in events] == ["waiting", "attention"]


def _healthy_overall_cache():
    check_names = {
        check
        for group in list(BUSINESS_MODULE_CHECKS.values()) + list(FACTORY_CHECKS.values())
        for check in group
    }
    check_names.add("token_health_refresh")
    return {
        "services": {name: True for name, _ in SERVICES},
        "db": {"local": True},
        "zombies": (0, ""),
        "business_live": _business_module_status_from_payload(
            {"ok": True, "results": [{"name": name, "ok": True} for name in check_names]}
        ),
        "cron_summary": {"state": "ok"},
        "health": {"guardian": {"state": "ok"}, "function_health": {"state": "ok"}},
        "nas": {"lan": True, "mounted": True, "shares": {"homes": {"mounted": True}}},
        "engines": {name: "online" for name, _ in OMLX_ENGINES},
        "omlx_profile": {"expected_profile": "day", "text_status": {"label": "primary", "mismatch": False, "degraded": False}},
        "monitors": {name: {"state": "alive"} for name, _ in MONITOR_THREADS},
        "business_readiness": {
            "items": {"案件回報": {"state": "ok", "label": "無待處理"}}
        },
    }


def test_overall_state_never_stays_green_when_a_reported_component_is_red():
    cases = [
        ("factory", lambda cache: cache["business_live"]["factory"]["系統狀態"].update(state="attention")),
        ("credential", lambda cache: cache["business_live"]["credential"].update(state="attention")),
        ("cron", lambda cache: cache["cron_summary"].update(state="attention")),
        ("guardian", lambda cache: cache["health"]["guardian"].update(state="attention")),
        ("function_health", lambda cache: cache["health"]["function_health"].update(state="attention")),
        ("nas", lambda cache: cache["nas"]["shares"]["homes"].update(mounted=False)),
        ("model", lambda cache: cache["omlx_profile"]["text_status"].update(mismatch=True)),
        ("monitor", lambda cache: cache["monitors"]["法扶附件重試"].update(state="down")),
    ]

    for _name, make_red in cases:
        cache = _healthy_overall_cache()
        make_red(cache)
        assert _overall_state(cache) == "attention"


def test_overall_state_includes_business_readiness_blockers():
    cache = _healthy_overall_cache()
    cache["business_readiness"] = {
        "items": {"法扶附件": {"state": "attention", "label": "1份欠檔"}}
    }

    assert _overall_state(cache) == "attention"


def test_business_readiness_marks_stale_snapshot_attention(tmp_path, monkeypatch):
    from gui import magi_menubar as mod

    static = tmp_path / "static"
    static.mkdir()
    (static / "business_readiness_latest.json").write_text(
        json.dumps(
            {
                "generated_at": (datetime.now() - timedelta(hours=1)).replace(microsecond=0).isoformat(),
                "summary": {"attention": 0, "waiting": 0, "ok": 5},
                "items": {"案件回報": {"state": "ok", "label": "無待處理"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "MAGI_ROOT", str(tmp_path))

    status = _business_readiness_live()

    assert status["state"] == "attention"
    assert status["items"]["案件回報"]["label"] == "快照逾時"


def test_night_profile_only_requires_its_expected_models_for_overall_state():
    cache = _healthy_overall_cache()
    cache["omlx_profile"]["expected_profile"] = "night"
    cache["engines"] = {"文字推理": "night-primary", "向量嵌入": "embed"}

    assert _overall_state(cache) == "ok"


def test_cron_failures_and_stale_jobs_are_sorted_before_healthy_jobs_and_summarized():
    now = datetime(2026, 7, 11, 12, 0, 0)
    jobs = [
        {"id": f"ok-{index}", "enabled": True, "cron": "*/2 * * * *", "desc": f"OK {index}"}
        for index in range(16)
    ] + [
        {"id": "failed-last", "enabled": True, "cron": "*/2 * * * *", "desc": "Failed last"},
        {"id": "stale-last", "enabled": True, "cron": "*/2 * * * *", "desc": "Stale last"},
    ]
    cron_state = {
        **{f"ok-{index}": {"last_success_at": (now - timedelta(minutes=2)).isoformat(), "returncode": 0} for index in range(16)},
        "failed-last": {"last_result_at": now.isoformat(), "returncode": 1},
        "stale-last": {"last_success_at": (now - timedelta(hours=8)).isoformat(), "returncode": 0},
    }

    details = _cron_details_from_state(jobs, cron_state, now=now)
    summary = _cron_summary(len(jobs), True, details)

    assert [detail["id"] for detail in details[:2]] == ["failed-last", "stale-last"]
    assert [detail["id"] for detail in details[:15]].count("failed-last") == 1
    assert summary["state"] == "attention"
    assert summary["failed"] == 1
    assert summary["stale"] == 1


def test_cron_summary_treats_blocked_distill_candidate_as_working_guard():
    now = datetime(2026, 7, 11, 12, 0, 0)
    jobs = [
        {
            "id": "job_distill_train_gemma",
            "enabled": True,
            "cron": "0 11 * * 0",
            "command": "python scripts/nightly_distill_gemma.py",
            "desc": "validation-gated distill",
        }
    ]
    cron_state = {
        "job_distill_train_gemma": {
            "last_result_at": now.isoformat(),
            "returncode": 1,
            "last_success": False,
            "last_error": "Validation gate failed: channel_marker_leak; blocked from deploy",
        }
    }

    details = _cron_details_from_state(jobs, cron_state, now=now)
    summary = _cron_summary(len(jobs), True, details)

    assert details[0]["status"] == "ok"
    assert details[0]["safe_rejection"] is True
    assert summary["state"] == "ok"


def test_live_check_failure_marks_every_module_and_factory_check_red_without_old_report():
    status = _business_module_status_failure("exit=2", returncode=2)

    assert status["ok"] is False
    assert status["returncode"] == 2
    assert {item["state"] for item in status["modules"].values()} == {"attention"}
    assert {item["state"] for item in status["factory"].values()} == {"attention"}


def test_live_check_failure_is_recorded_in_cache_without_reusing_prior_green_status():
    app = SimpleNamespace(_cache_lock=threading.Lock(), _status_cache={"business_live": _healthy_overall_cache()["business_live"]})

    cache = MAGIMenuBar._record_live_check_failure(app, "exit=2", returncode=2, report_path="/tmp/report.json")

    assert cache["live_check_failure"]["returncode"] == 2
    assert cache["live_check_failure"]["report_path"] == "/tmp/report.json"
    assert {item["state"] for item in cache["business_live"]["modules"].values()} == {"attention"}


def test_health_payload_requires_explicit_green_evidence():
    assert _health_state_from_payload("guardian", {"ok": True, "requires_human": []})["state"] == "ok"
    assert _health_state_from_payload("guardian", {"ok": True, "requires_human": [{"id": "x"}]})["state"] == "attention"
    assert _health_state_from_payload("function_health", {"ok": True}, age_sec=79 * 3600)["state"] == "waiting"


def test_health_and_cron_failure_details_are_copyable_plain_text():
    health = _health_state_from_payload(
        "guardian",
        {"ok": False, "unresolved_issue_ids": ["doctor:cron_state_failures"]},
    )
    assert "doctor:cron_state_failures" in health["detail"]

    cron = _cron_failure_detail(
        [
            {"id": "ok", "desc": "正常排程", "status": "ok", "relative": "1 分鐘前"},
            {"id": "bad", "desc": "失敗排程", "status": "failed", "relative": "5 分鐘前"},
        ]
    )
    assert cron == "失敗排程：執行失敗（5 分鐘前）"

    text = _status_detail_text({"title": "定時排程", "state": "attention", "value": "1項失敗", "detail": cron})
    assert "狀態：需要處理" in text
    assert "原因：\n失敗排程" in text


def test_business_readiness_details_use_plain_taiwanese_wording():
    report = _business_readiness_detail(
        "案件回報",
        {
            "state": "waiting",
            "pending": 1,
            "review_pending": 1,
            "pending_items": [{"case_number": "2026-0001", "client_name": "王小明", "status": "已結案，待報結"}],
            "review_items": [{"case_number": "2026-0002", "client_name": "林小華", "original_due_date": "2026-07-01", "original_type": "陳報", "summary": "補正資料"}],
        },
    )
    assert "待完成結案回報（1 件）：" in report
    assert "1. 2026-0001｜王小明｜已結案，待報結" in report
    assert "待人工確認的逾期事項（1 項）：" in report
    assert "2026-0002｜林小華｜原期限 2026-07-01｜陳報" in report
    assert "補正資料" in report
    assert "pending" not in report
    assert "{" not in report

    laf = _business_readiness_detail(
        "法扶附件",
        {
            "state": "waiting",
            "missing": 0,
            "pending_retry": 1,
            "manual_review": 0,
            "retry_items": [
                {
                    "case_number": "2026-0060",
                    "laf_case_number": "1150529-W-002",
                    "client_name": "林文俊",
                    "case_type": "民事",
                    "case_reason": "返還借款",
                    "reason": "法扶網站目前尚未列出可下載附件",
                    "tries": 16,
                    "last_try_at": "2026-07-12 10:45:46",
                }
            ],
        },
    )
    assert "附件重試清單（1 件）：" in laf
    assert "2026-0060｜1150529-W-002｜林文俊" in laf
    assert "狀況：法扶網站目前尚未列出可下載附件" in laf
    assert "已重試 16 次；最後檢查 2026-07-12 10:45:46" in laf
    assert "每小時自動重試" in laf

    waiting_text = _status_detail_text(
        {"title": "案件回報", "state": "waiting", "value": "1案回報／1項確認", "detail": report}
    )
    assert "說明：" in waiting_text
    assert "原因：" not in waiting_text


def test_credential_detail_does_not_expose_internal_fields():
    assert _credential_detail({"state": "ok", "checks": ["internal_token_name"]}) == (
        "MAGI 使用中的登入憑證與授權均通過最近一次檢查。"
    )


def test_service_liveness_requires_http_evidence_for_server_and_tools(monkeypatch):
    monkeypatch.setattr("gui.magi_menubar._http_liveness", lambda *_args, **_kwargs: False)

    assert _service_liveness(True, "主伺服器") == (False, "HTTP 無回應")
    assert _service_liveness(True, "工具介面") == (False, "HTTP 無回應")
    assert _service_liveness(True, "守護程式") == (True, "運作正常")


def test_task_module_rows_fit_the_three_row_section():
    third_y, third_height = _task_module_row_geometry(2)

    assert third_y + third_height <= 144


def test_collect_status_single_flight_skips_a_second_collection():
    app = SimpleNamespace(_collection_lock=threading.Lock())
    assert app._collection_lock.acquire(blocking=False)

    assert MAGIMenuBar._collect_status(app) is False


def test_stale_business_module_report_is_not_shown_as_operational(tmp_path, monkeypatch):
    from gui import magi_menubar as mod

    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    checks = {name for group in list(BUSINESS_MODULE_CHECKS.values()) + list(FACTORY_CHECKS.values()) for name in group}
    checks.add("token_health_refresh")
    report = runtime / "business_module_live_check_latest.json"
    report.write_text(json.dumps({"ok": True, "results": [{"name": name, "ok": True} for name in checks]}), encoding="utf-8")
    old = datetime.now().timestamp() - 67 * 3600
    import os

    os.utime(report, (old, old))
    (tmp_path / "cron_jobs.json").write_text(
        json.dumps([{"id": "job_business_module_live_check", "cron": "10 3 * * *", "enabled": True}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "MAGI_ROOT", str(tmp_path))
    monkeypatch.delenv("MAGI_RUNTIME_DIR", raising=False)

    status = _business_module_status_live()

    assert status["stale"] is True
    assert {info["label"] for info in status["modules"].values()} == {"檢查逾時"}


def test_dashboard_home_action_opens_magi_home(monkeypatch):
    from gui import magi_menubar as mod

    opened = []

    class FakeApp:
        def __init__(self):
            self.notices = []

        def _set_dashboard_notice(self, text):
            self.notices.append(text)

    monkeypatch.setattr(mod.subprocess, "Popen", lambda cmd, **_kwargs: opened.append(cmd))

    fake = FakeApp()
    MAGIMenuBar._handle_dashboard_action(fake, "open_hub")

    assert fake.notices == ["正在開啟首頁"]
    assert opened == [["open", MAGI_HOME_URL]]
    assert "8088" not in MAGI_HOME_URL
