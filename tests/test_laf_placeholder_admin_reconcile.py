import os


class _FakeLAF:
    def login(self):
        return True

    def export_case_list_excel(self):
        return "/tmp/fake_laf_case_list.xlsx"

    def close(self):
        pass


class _FakeDB:
    def __init__(self, row):
        self.row = dict(row)
        self.writes = []

    def fetch_all(self, sql, params=None, as_dict=False):
        if "WHERE `legal_aid_number` = %s AND `id` <> %s" in sql:
            return []
        return [dict(self.row)]

    def execute_write(self, sql, params=None):
        self.writes.append((sql, params))
        if "`case_type` = %s" in sql and params:
            self.row["client_name"] = params[0]
            self.row["case_type"] = params[1]
            self.row["case_reason"] = params[2]
            self.row["case_stage"] = params[3]
        if "UPDATE `cases` SET `folder_path` = %s" in sql and params:
            self.row["folder_path"] = params[0]


def test_placeholder_reconcile_moves_labor_insurance_case_to_admin(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators import laf_nightly_audit as mod

    base = tmp_path / "cases"
    old_folder = base / "法扶案件" / "民事" / "2026-0045-李秀英-一審-勞工保險爭議"
    old_folder.mkdir(parents=True)
    row = {
        "id": "case-id",
        "case_number": "2026-0045",
        "client_name": "李秀英",
        "case_type": "民事",
        "case_reason": "待確認",
        "case_stage": "一審",
        "case_category": "法律扶助案件",
        "folder_path": str(old_folder),
        "legal_aid_number": "1150421-W-004",
    }
    db = _FakeDB(row)

    monkeypatch.setenv("LAF_EXPERIMENT_BASE_DIR", str(base))
    monkeypatch.setattr(mod, "_make_laf_web_automation", lambda log_prefix="": _FakeLAF())
    monkeypatch.setattr(
        mod,
        "_parse_case_list_excel",
        lambda _path: [
            {
                "applyno": "1150421-W-004",
                "name": "李秀英",
                "reason": "勞工保險爭議",
                "procedure": "一審",
            }
        ],
    )

    result = mod.reconcile_placeholder_cases(
        db,
        force=True,
        only_laf_no="1150421-W-004",
        notifier=None,
    )

    new_folder = base / "法扶案件" / "行政" / "2026-0045-李秀英-一審-勞工保險爭議"
    assert result["reconciled"][0]["new_case_reason"] == "勞工保險爭議"
    assert any(params and params[1] == "行政" for _sql, params in db.writes)
    assert not os.path.exists(old_folder)
    assert os.path.isdir(new_folder)
    assert db.row["folder_path"] == str(new_folder)
