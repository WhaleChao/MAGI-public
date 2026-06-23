from __future__ import annotations

import sys
from pathlib import Path


def _load_laf_profiles_module():
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))
    sys.modules.pop("api.laf_branch_profiles", None)
    sys.modules.pop("api", None)

    import api.laf_branch_profiles as module

    return module


def test_laf_branch_profile_resolves_seed_aliases(monkeypatch):
    module = _load_laf_profiles_module()

    monkeypatch.setenv("MAGI_LAF_BRANCH_PROFILE_DB", "0")
    profile = module.resolve_laf_branch_profile("臺東")

    assert profile is not None
    assert profile.branch_label == "台東分會"
    assert profile.phone == ""
    assert profile.default_lawyer_name == "受任律師"


def test_laf_law_firm_profile_is_prefilled(monkeypatch):
    module = _load_laf_profiles_module()

    monkeypatch.setenv("MAGI_LAF_BRANCH_PROFILE_DB", "0")
    profile = module.get_law_firm_profile()

    assert profile.lawyer_name == "受任律師"
    assert profile.address_line == "範例事務所地址"
    assert profile.phone == "事務所電話"
    assert profile.fax == ""
    assert profile.mobile == ""


class _Cursor:
    def __init__(self):
        self.statements: list[tuple[str, tuple | None]] = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def close(self):
        pass


class _Conn:
    def __init__(self):
        self.cursor_obj = _Cursor()

    def cursor(self, *_, **__):
        return self.cursor_obj


def test_laf_branch_profile_seed_to_db_creates_branch_and_law_firm_rows():
    module = _load_laf_profiles_module()

    conn = _Conn()
    module.seed_laf_branch_profiles_to_db(conn)
    joined_sql = "\n".join(sql for sql, _ in conn.cursor_obj.statements)

    assert "CREATE TABLE IF NOT EXISTS laf_branch_profiles" in joined_sql
    assert "CREATE TABLE IF NOT EXISTS laf_law_firm_profiles" in joined_sql
    assert "VALUES ('default'" in joined_sql
    assert any(params and "花蓮分會" in params for _, params in conn.cursor_obj.statements)
