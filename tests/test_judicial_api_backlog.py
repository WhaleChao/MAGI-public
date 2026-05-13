from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from api.domains.judicial_api_backlog import build_backlog_interpretation, format_backlog_notice
from scripts.ops.check_judicial_api_pipeline import build_report, scheduled_day_process_capacity


def test_backlog_interpretation_explains_stale_backlog():
    report = build_backlog_interpretation(
        backlog_before=69199,
        backlog_remaining=68999,
        handled=200,
        db_upserts=200,
        archive_upserts=200,
        vector_ingested=58,
        summarized=80,
        oldest_age_hours=822.88,
        newest_age_hours=1.5,
        raw_total=80000,
        skipped_low_value=3,
        skipped_missing_text=2,
        max_docs=200,
        runs_per_day=5,
    )

    assert report["status"] == "STALE"
    assert "見解庫的新鮮度已落後" in report["headline"]
    assert report["runs_left_at_current_rate"] == 345
    text = format_backlog_notice("⚠️ 司法院 API 晨間整理", report)
    assert "69,199" in text
    assert "68,999" in text
    assert "品質閘門" in text
    assert "約 345 輪" in text
    assert "約 69 天" in text


def test_backlog_interpretation_clear_state_is_readable():
    report = build_backlog_interpretation(
        backlog_before=10,
        backlog_remaining=0,
        handled=10,
        db_upserts=10,
        archive_upserts=10,
        summarized=5,
    )

    assert report["status"] == "CLEAR"
    assert "已清空" in report["headline"]
    assert any("10 → 0" in line for line in report["lines"])


def test_scheduled_day_process_capacity_reads_cron_payloads(tmp_path):
    cron = tmp_path / "cron_jobs.json"
    cron.write_text(
        """
[
  {"enabled": true, "command": "python action.py --task 'official_api_day_process {\\"max_docs\\":300}'"},
  {"enabled": true, "command": "python action.py --task 'official_api_day_process {\\"max_docs\\":2000}'"},
  {"enabled": false, "command": "python action.py --task 'official_api_day_process {\\"max_docs\\":9999}'"}
]
""",
        encoding="utf-8",
    )

    cap = scheduled_day_process_capacity(cron)
    assert cap["runs_per_day"] == 2
    assert cap["daily_max_docs"] == 2300
    assert cap["avg_batch"] == 1150


def test_missing_pull_state_does_not_mask_active_backlog(monkeypatch, tmp_path):
    cache_root = tmp_path / "judicial_api"
    raw_root = cache_root / "raw"
    normalized_root = cache_root / "normalized"
    raw_root.mkdir(parents=True)
    normalized_root.mkdir(parents=True)
    (raw_root / "case_a.json").write_text('{"payload":{"JID":"A"}}', encoding="utf-8")
    (raw_root / "case_b.json").write_text('{"payload":{"JID":"B"}}', encoding="utf-8")
    (normalized_root / "case_a.json").write_text("{}", encoding="utf-8")
    process_state = cache_root / "process_state.json"
    process_state.write_text(
        """
{
  "updated_at": "2999-01-01T00:00:00",
  "processed": {},
  "last_run": {
    "handled": 1,
    "backlog_before": 2,
    "db_upserts": 1,
    "archive_upserts": 1,
    "vector_ingested": 0,
    "summarized": 1,
    "errors": 0,
    "max_docs": 1
  }
}
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("JUDICIAL_API_CACHE_ROOT", str(cache_root))
    monkeypatch.setenv("JUDICIAL_API_RAW_ROOT", str(raw_root))
    monkeypatch.setenv("JUDICIAL_API_NORMALIZED_ROOT", str(normalized_root))
    monkeypatch.setenv("JUDICIAL_API_PROCESS_STATE_PATH", str(process_state))
    monkeypatch.setenv("JUDICIAL_API_PULL_STATE_PATH", str(cache_root / "missing_pull_state.json"))
    monkeypatch.setenv("MAGI_JUDICIAL_API_USER", "user")
    monkeypatch.setenv("MAGI_JUDICIAL_API_PASS", "pass")

    report = build_report()

    assert report["status"] == "BACKLOG_CATCHING_UP"
    assert report["exit_code"] == 10
    assert any("raw/process/normalized" in item for item in report["reasons"])


def test_extractive_judgment_summary_is_marked_and_source_bound():
    action_path = Path("skills/judgment-collector/action.py")
    spec = importlib.util.spec_from_file_location("judgment_action_for_test", action_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    full_text = (
        "臺灣測試地方法院民事判決\n"
        "主文\n"
        "被告應給付原告新臺幣十萬元。\n"
        "事實及理由\n"
        "按民法第184條規定，因故意或過失不法侵害他人權利者，負損害賠償責任。\n"
        "經查，被告駕車未注意車前狀況，撞擊原告車輛，應負侵權行為損害賠償責任。\n"
        "中華民國一一五年五月十日\n"
    )
    summary = mod._extractive_judgment_summary(full_text, "侵權行為損害賠償")
    normalized_source = re.sub(r"\s+", "", full_text)

    assert "## 摘要類型" in summary
    assert "抽取式快篩" in summary
    assert "## 主文摘錄" in summary
    assert "## 理由摘錄" in summary
    assert "裁判要旨" not in summary
    for snippet in ["被告應給付原告新臺幣十萬元", "經查，被告駕車未注意車前狀況"]:
        assert re.sub(r"\s+", "", snippet) in normalized_source
        assert snippet in summary
    assert "民法第184條" in summary
