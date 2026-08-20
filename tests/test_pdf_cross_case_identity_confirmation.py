from __future__ import annotations

from pathlib import Path

from api.blueprints import osc_pdf


def _judgment(tmp_path: Path, case_number: str, payload: bytes) -> Path:
    root = tmp_path / f"{case_number}-測試當事人-一審-測試" / "10_判決書或終局裁定及處分"
    root.mkdir(parents=True)
    path = root / "台北地院民事判決_測試當事人_115.8.11.pdf"
    path.write_bytes(payload)
    return path


def test_exact_cross_case_copy_is_verified_by_sha256(monkeypatch, tmp_path):
    current = _judgment(tmp_path, "2026-0002", b"same immutable judgment")
    existing = _judgment(tmp_path, "2026-0001", b"same immutable judgment")

    def fake_exec(sql, params=(), fetch="none"):
        assert fetch == "all"
        return ([{
            "id": 91,
            "case_number": "2026-0001",
            "client_name": "測試當事人",
            "todo_type": "上訴",
            "source_file": str(existing),
            "case_type": "民事",
            "case_reason": "損害賠償",
            "court_name": "臺灣臺北地方法院",
            "court_case_number": "115年度訴字第1號",
        }], None)

    monkeypatch.setattr(osc_pdf, "_osc_exec", fake_exec)
    conflicts = osc_pdf._verified_cross_case_document_conflicts(
        current,
        case_number="2026-0002",
        client_name="測試當事人",
    )

    assert [row["case_number"] for row in conflicts] == ["2026-0001"]
    assert conflicts[0]["sha256"] == osc_pdf._document_sha256(current)


def test_same_filename_with_different_bytes_is_not_a_conflict(monkeypatch, tmp_path):
    current = _judgment(tmp_path, "2026-0002", b"document two")
    existing = _judgment(tmp_path, "2026-0001", b"document one")
    monkeypatch.setattr(
        osc_pdf,
        "_osc_exec",
        lambda *_args, **_kwargs: ([{
            "id": 92,
            "case_number": "2026-0001",
            "client_name": "測試當事人",
            "todo_type": "上訴",
            "source_file": str(existing),
        }], None),
    )

    assert osc_pdf._verified_cross_case_document_conflicts(
        current,
        case_number="2026-0002",
        client_name="測試當事人",
    ) == []


def test_scan_fails_closed_into_identity_confirmation(monkeypatch, tmp_path):
    current = _judgment(tmp_path, "2026-0002", b"same immutable judgment")
    monkeypatch.setattr(
        osc_pdf,
        "_load_headless_todo_helpers",
        lambda: (
            lambda *_args, **_kwargs: [{
                "type": "上訴",
                "date": "2026-08-31",
                "time": "",
                "description": "20日內上訴",
            }],
            lambda: {},
        ),
    )
    monkeypatch.setattr(
        osc_pdf,
        "_verified_cross_case_document_conflicts",
        lambda *_args, **_kwargs: [{"case_number": "2026-0001"}],
    )

    result = osc_pdf._scan_pdf_for_calendar(
        current,
        case_number="2026-0002",
        client_name="測試當事人",
        scan_text=False,
    )

    assert result["todos"] == []
    assert result["events"] == []
    assert result["identity_confirmation"]["status"] == "needs_human"
    assert result["identity_confirmation"]["existing_case_numbers"] == ["2026-0001"]
    assert result["identity_confirmation"]["blocked_todo_types"] == ["上訴"]


def test_manual_confirmation_has_no_calendar_date_and_is_idempotent(monkeypatch, tmp_path):
    current = _judgment(tmp_path, "2026-0002", b"same immutable judgment")
    calls = []

    def fake_exec(sql, params=(), fetch="none"):
        calls.append((sql, params, fetch))
        if fetch == "one":
            return (None, None)
        return ({"lastrowid": 1234}, None)

    monkeypatch.setattr(osc_pdf, "_osc_exec", fake_exec)
    result = osc_pdf._insert_document_identity_confirmation(
        path=current,
        case_number="2026-0002",
        client_name="測試當事人",
        identity_confirmation={"existing_case_numbers": ["2026-0001"]},
    )

    assert result == {"inserted": 1, "skipped": 0, "id": 1234, "status": "pending"}
    insert_sql, insert_params, _ = calls[-1]
    assert "todo_date, todo_time" in insert_sql
    assert "VALUES (%s,%s,%s,NULL,NULL" in insert_sql
    assert insert_params[2] == "文件歸檔確認"
    assert "確認前不會同步至 Google 日曆" in insert_params[3]

    calls.clear()
    monkeypatch.setattr(
        osc_pdf,
        "_osc_exec",
        lambda *_args, **_kwargs: ({"id": 1234, "status": "pending"}, None),
    )
    duplicate = osc_pdf._insert_document_identity_confirmation(
        path=current,
        case_number="2026-0002",
        client_name="測試當事人",
        identity_confirmation={"existing_case_numbers": ["2026-0001"]},
    )
    assert duplicate == {"inserted": 0, "skipped": 1, "id": 1234, "status": "pending"}
