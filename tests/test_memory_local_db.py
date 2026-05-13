from skills.memory import local_db


def test_save_local_skips_existing_content_hash(monkeypatch):
    class Cursor:
        def __init__(self):
            self.lastrowid = 0
            self.inserted = 0
            self.updated = 0
            self._row = None

        def execute(self, sql, params=None):
            normalized = " ".join(str(sql).split()).lower()
            if normalized.startswith("select id from documents where md5(content)"):
                self._row = (42,)
                return
            if normalized.startswith("update documents set synced = 1"):
                self.updated += 1
                return
            if normalized.startswith("insert into documents"):
                self.inserted += 1
                self.lastrowid = 99
                return
            raise AssertionError(f"unexpected sql: {sql}")

        def fetchone(self):
            return self._row

        def close(self):
            return None

    class Conn:
        def __init__(self, cursor):
            self.cursor_obj = cursor
            self.commits = 0

        def cursor(self):
            return self.cursor_obj

        def commit(self):
            self.commits += 1

        def is_connected(self):
            return True

        def close(self):
            return None

    cur = Cursor()
    conn = Conn(cur)
    monkeypatch.setattr(local_db, "_get_connection", lambda: conn)

    doc_id = local_db.save_local("同一段記憶", source="judicial_api:test", is_synced=True)

    assert doc_id == 42
    assert cur.inserted == 0
    assert cur.updated == 1
    assert conn.commits == 1


def test_save_vector_local_skips_existing_doc_id(monkeypatch):
    class Cursor:
        def __init__(self):
            self.inserted = 0
            self._row = None

        def execute(self, sql, params=None):
            normalized = " ".join(str(sql).split()).lower()
            if normalized.startswith("select 1 from vectors where doc_id"):
                self._row = (1,)
                return
            if normalized.startswith("insert into vectors"):
                self.inserted += 1
                return
            raise AssertionError(f"unexpected sql: {sql}")

        def fetchone(self):
            return self._row

        def close(self):
            return None

    class Conn:
        def __init__(self, cursor):
            self.cursor_obj = cursor
            self.commits = 0

        def cursor(self):
            return self.cursor_obj

        def commit(self):
            self.commits += 1

        def is_connected(self):
            return True

        def close(self):
            return None

    cur = Cursor()
    conn = Conn(cur)
    monkeypatch.setattr(local_db, "_get_connection", lambda: conn)

    assert local_db.save_vector_local(42, [0.1, 0.2]) is True
    assert cur.inserted == 0
    assert conn.commits == 0
