import assistant_tasks


class _Result:
    def __init__(self, rows=(), rowcount=0):
        self._rows = list(rows)
        self.rowcount = rowcount

    def fetchall(self):
        return self._rows


class _FakeDB:
    is_pg = True

    def __init__(self):
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=()):
        self.queries.append((query, params))
        if "information_schema.columns" in query:
            return _Result([
                {"column_name": "request_id"},
                {"column_name": "surface"},
                {"column_name": "user_id"},
                {"column_name": "kind"},
                {"column_name": "payload"},
                {"column_name": "payload_hash"},
                {"column_name": "state"},
                {"column_name": "attempts"},
                {"column_name": "result"},
                {"column_name": "error_code"},
                {"column_name": "error_detail"},
                {"column_name": "cancel_requested"},
                {"column_name": "created_at"},
                {"column_name": "updated_at"},
                {"column_name": "started_at"},
                {"column_name": "finished_at"},
                {"column_name": "run_id"},
                {"column_name": "heartbeat_at"},
            ])
        return _Result(rowcount=1)


def test_ensure_tasks_table_uses_postgres_metadata_not_pragma(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(assistant_tasks, "TASKS_TABLE_READY", False)
    monkeypatch.setattr(assistant_tasks, "_get_db", lambda: fake)

    assistant_tasks.ensure_tasks_table()

    sql = "\n".join(query for query, _ in fake.queries)
    assert "information_schema.columns" in sql
    assert "PRAGMA table_info" not in sql
    assert "ALTER TABLE assistant_requests ADD COLUMN run_id TEXT" not in sql
    assert "ALTER TABLE assistant_requests ADD COLUMN heartbeat_at REAL" not in sql
