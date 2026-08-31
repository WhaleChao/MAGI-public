from __future__ import annotations

import importlib.util
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYNC = ROOT / "scripts" / "sync_exam_tutor_trends.py"
SIDECAR = ROOT / "scripts" / "ops" / "cookie_video_hotfix_sidecar.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config() -> dict:
    return {
        "refresh_policy": {
            "source_monitor_interval_hours": 4,
            "minimum_refresh_interval_hours": 12,
            "maximum_snapshot_age_hours": 24,
        },
        "sources": [
            {"source_id": "official_a", "name": "官方甲", "tier": 1},
            {"source_id": "official_b", "name": "官方乙", "tier": 1},
        ],
    }


def test_monitor_checks_every_configured_source_and_keeps_failed_refresh_visible() -> None:
    module = _load(SYNC, "exam_trend_sync_monitor")
    now = datetime(2026, 8, 24, 8, tzinfo=timezone.utc)
    snapshot = {
        "generated_at": (now - timedelta(hours=30)).isoformat(),
        "source_registry": [{"source_id": "official_a", "content_sha256": "a" * 64}],
    }
    status = module.build_source_monitor_status(
        config=_config(),
        snapshot=snapshot,
        sources=[
            {"source_id": "official_a", "fetch_state": "ok", "content_sha256": "b" * 64, "fetched_at": now.isoformat()},
            {"source_id": "official_b", "fetch_state": "failed", "fetched_at": now.isoformat()},
        ],
        checked_at=now.isoformat(),
        now=now,
    )
    assert status["schema"] == "magi.exam-tutor-source-monitor/v1"
    assert status["state"] == "degraded"
    assert status["configured_source_count"] == 2
    assert status["successful_source_count"] == 1
    assert status["failed_source_count"] == 1
    assert status["changed_source_count"] == 1
    assert status["refresh_required"] is True
    assert status["refresh_due"] is True
    assert status["pii_included"] is False
    assert {row["source_id"] for row in status["sources"]} == {"official_a", "official_b"}


def test_source_fingerprint_ignores_view_counter_but_keeps_legal_dates() -> None:
    module = _load(SYNC, "exam_trend_stable_source")
    first = module._stable_source_text("115年憲判字第6號 判決日期 115年08月14日 瀏覽人次：156,049,689")
    second = module._stable_source_text("115年憲判字第6號 判決日期 115年08月14日 瀏覽人次 156,050,001")
    changed = module._stable_source_text("115年憲判字第6號 判決日期 115年08月15日 瀏覽人次 156,050,001")
    assert first == second
    assert first != changed
    moj_first = module._stable_source_text(
        "判決日期 115年08月14日 附件.pdf (下載次數：381次) 更新日期: 115/08/24 累計瀏覽統計"
    )
    moj_second = module._stable_source_text(
        "判決日期 115年08月14日 附件.pdf（下載次數：999次）更新日期: 115/08/25 累計瀏覽統計"
    )
    assert moj_first == moj_second
    assert "115年08月14日" in moj_first
    moj_click_first = module._stable_source_text(
        "發布日期：115/06/02 最後更新日期：115/06/02 點閱次數：53947 民法第1223條"
    )
    moj_click_second = module._stable_source_text(
        "發布日期：115/06/02 最後更新日期：115/06/02 點閱次數：53981 民法第1223條"
    )
    assert moj_click_first == moj_click_second
    assert "民法第1223條" in moj_click_first


def test_monitor_throttles_heavy_rebuild_but_not_four_hour_source_check() -> None:
    module = _load(SYNC, "exam_trend_sync_throttle")
    now = datetime(2026, 8, 24, 8, tzinfo=timezone.utc)
    status = module.build_source_monitor_status(
        config=_config(),
        snapshot={
            "generated_at": (now - timedelta(hours=2)).isoformat(),
            "source_registry": [{"source_id": "official_a", "content_sha256": "a" * 64}],
        },
        sources=[
            {"source_id": "official_a", "fetch_state": "ok", "content_sha256": "b" * 64, "fetched_at": now.isoformat()},
            {"source_id": "official_b", "fetch_state": "ok", "content_sha256": "c" * 64, "fetched_at": now.isoformat()},
        ],
        checked_at=now.isoformat(),
        now=now,
    )
    assert status["source_monitor_interval_hours"] == 4
    assert status["refresh_required"] is True
    assert status["refresh_due"] is False


def test_scheduled_sync_loads_only_hash_bound_nvidia_settings(monkeypatch, tmp_path: Path) -> None:
    module = _load(SYNC, "exam_trend_bound_env")
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        "NVIDIA_NIM_ENABLE=1\nNVIDIA_NIM_API_KEY=secret-fixture\nUNRELATED_VALUE=blocked\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    monkeypatch.setenv("MAGI_ENV_FILE", str(env_file))
    monkeypatch.setenv("MAGI_ENV_FILE_SHA256", hashlib.sha256(env_file.read_bytes()).hexdigest())
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "existing-wins")
    monkeypatch.delenv("NVIDIA_NIM_ENABLE", raising=False)
    monkeypatch.delenv("UNRELATED_VALUE", raising=False)
    module.load_bound_runtime_env()
    assert module.os.environ["NVIDIA_NIM_ENABLE"] == "1"
    assert module.os.environ["NVIDIA_NIM_API_KEY"] == "existing-wins"
    assert "UNRELATED_VALUE" not in module.os.environ

    monkeypatch.setenv("MAGI_ENV_FILE_SHA256", "f" * 64)
    try:
        module.load_bound_runtime_env()
    except RuntimeError as exc:
        assert str(exc) == "bound runtime env SHA mismatch"
    else:
        raise AssertionError("mismatched env file was accepted")


def test_live_feed_exposes_safe_freshness_without_local_paths(monkeypatch, tmp_path: Path) -> None:
    snapshot = tmp_path / "trend.json"
    monitor = tmp_path / "monitor.json"
    snapshot.write_text(json.dumps({
        "schema_version": 2,
        "ui_title": "趨勢分析",
        "generated_at": "2026-08-24T08:00:00+00:00",
        "source_registry": [{
            "source_id": "official_a", "name": "官方甲", "tier": 1,
            "source_type": "official_news", "detail_level": "index",
            "url": "https://example.invalid/a", "content_sha256": "a" * 64,
        }],
        "items": [],
    }, ensure_ascii=False), encoding="utf-8")
    monitor.write_text(json.dumps({
        "schema": "magi.exam-tutor-source-monitor/v1",
        "state": "current",
        "checked_at": "2026-08-24T08:05:00+00:00",
        "source_monitor_interval_hours": 4,
        "maximum_snapshot_age_hours": 24,
        "configured_source_count": 1,
        "successful_source_count": 1,
        "failed_source_count": 0,
        "changed_source_count": 0,
        "refresh_required": False,
        "pii_included": False,
        "sources": [{
            "source_id": "official_a", "name": "官方甲", "fetch_state": "ok",
            "changed_since_published": False, "checked_at": "2026-08-24T08:05:00+00:00",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("MAGI_EXAM_TUTOR_TREND_PATH", str(snapshot))
    monkeypatch.setenv("MAGI_EXAM_TUTOR_TREND_MONITOR_PATH", str(monitor))
    app = _load(SIDECAR, "exam_trend_sidecar").create_app()
    response = app.test_client().get("/api/exam-tutor/trends")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["refresh"]["state"] == "current"
    assert payload["refresh"]["configured_source_count"] == 1
    assert payload["refresh"]["sources"] == [{
        "source_id": "official_a", "name": "官方甲", "fetch_state": "ok",
        "changed_since_published": False, "checked_at": "2026-08-24T08:05:00+00:00",
    }]
    assert str(tmp_path) not in response.get_data(as_text=True)


def test_config_and_page_make_automatic_all_source_monitoring_explicit() -> None:
    config = json.loads((ROOT / "config" / "exam_tutor_trend_sources.json").read_text(encoding="utf-8"))
    policy = config["refresh_policy"]
    assert policy["source_monitor_interval_hours"] == 4
    assert policy["maximum_snapshot_age_hours"] == 24
    assert policy["refresh_on_material_source_change"] is True
    assert policy["fetch_transport"] == "macos_system_curl_verified_tls"
    authorizations = config["background_authorizations"]
    assert authorizations["analysis"]["task_type"] == "exam_tutor_trend_analysis"
    assert authorizations["statutory_audit"]["task_type"] == "exam_tutor_trend_statutory_audit"
    assert authorizations["analysis"]["source_class"] == "public_source"
    assert authorizations["analysis"]["model"] == policy["nvidia_model"]
    ids = {row["source_id"] for row in config["sources"]}
    assert "constitutional_115_6_fulltext" in ids
    assert len(ids) == len(config["sources"]) >= 20
    baseline = json.loads((ROOT / "static" / "exam_tutor" / "trend_analysis.json").read_text(encoding="utf-8"))
    judgment = next(item for item in baseline["items"] if item["uid"] == "trend-115-constitutional-judgment-6")
    assert judgment["event_date"] == "2026-08-14"
    assert judgment["analysis_state"] == "source_audited"
    assert judgment["source_ids"] == ["constitutional_115_6_fulltext"]
    assert "再行移送" not in json.dumps(judgment, ensure_ascii=False)
    page = (ROOT / "templates" / "exam_tutor.html").read_text(encoding="utf-8")
    assert 'id="trend-source-health"' in page
    assert "個設定來源每 ${interval} 小時自動巡檢" in page
    assert "不需自行回憶是否更新" in page
