from __future__ import annotations


def test_laf_branch_profile_resolves_seed_aliases():
    from api.laf_branch_profiles import resolve_laf_branch_profile

    profile = resolve_laf_branch_profile("臺東")

    assert profile is not None
    assert profile.branch_label == "台東分會"
    assert profile.phone == "089-361363"
    assert profile.default_lawyer_name == "喬政翔律師"


def test_laf_law_firm_profile_is_prefilled():
    from api.laf_branch_profiles import get_law_firm_profile

    profile = get_law_firm_profile()

    assert profile.lawyer_name == "喬政翔律師"
    assert profile.address_line == "970花蓮縣花蓮市明禮路18之6號1樓"
    assert profile.phone == "03-835-7186"
    assert profile.fax == "03-835-7135"
    assert profile.mobile == "0937-753-800"


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
    from api.laf_branch_profiles import seed_laf_branch_profiles_to_db

    conn = _Conn()
    seed_laf_branch_profiles_to_db(conn)
    joined_sql = "\n".join(sql for sql, _ in conn.cursor_obj.statements)

    assert "CREATE TABLE IF NOT EXISTS laf_branch_profiles" in joined_sql
    assert "CREATE TABLE IF NOT EXISTS laf_law_firm_profiles" in joined_sql
    assert "VALUES ('default'" in joined_sql
    assert any(params and "花蓮分會" in params for _, params in conn.cursor_obj.statements)
