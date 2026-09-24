import os
import sqlite3

import pytest

from edupagetasks.models import MapRow, SyncState
from edupagetasks.state import StateStore


def make_row(
    *,
    timelineid: int = 1001,
    task_id: int | None = 7,
    state: str = SyncState.CREATED,
    fingerprint: str = "fp1",
    updated: str = "2026-01-01T00:00:00Z",
) -> MapRow:
    return MapRow(
        userid="u1",
        timelineid=timelineid,
        vikunja_task_id=task_id,
        vikunja_project_id=12,
        content_fingerprint=fingerprint,
        done_state=False,
        school_year=2026,
        last_seen_due_date="2026-02-01",
        last_seen_at="2026-01-01T00:00:00Z",
        closed_at=None,
        pending_ops="[]",
        retry_count=0,
        sync_state=state,
        anchor_label_id=3,
        updated_at=updated,
    )


def test_row_round_trip(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), project_default=12)
    store.upsert_row(make_row())
    assert store.get_row("u1", 1001) == make_row()
    store.close()


def test_upsert_updates_instead_of_duplicating(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), project_default=12)
    store.upsert_row(make_row())
    store.upsert_row(
        make_row(fingerprint="fp2", task_id=8, updated="2026-01-02T00:00:00Z")
    )
    assert store.get_row("u1", 1001) == make_row(
        fingerprint="fp2", task_id=8, updated="2026-01-02T00:00:00Z"
    )
    assert len(store.rows()) == 1
    store.close()


def test_delete_row(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), project_default=12)
    store.upsert_row(make_row())
    store.delete_row("u1", 1001)
    assert store.get_row("u1", 1001) is None
    assert store.rows() == []
    assert store.rows_by_state(SyncState.CREATED) == []
    store.close()


def test_rows_by_state(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), project_default=12)
    store.upsert_row(make_row(state=SyncState.CREATED))
    store.upsert_row(make_row(timelineid=2002, state=SyncState.DEFERRED))
    assert [r.timelineid for r in store.rows_by_state("created")] == [1001]
    assert [r.timelineid for r in store.rows_by_state(SyncState.DEFERRED)] == [2002]
    assert len(store.rows()) == 2
    store.close()


def test_meta_round_trip_and_counters(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), project_default=12)
    assert store.get_meta("last_sync_ts") is None
    store.set_meta("last_sync_ts", "2026-01-01T00:00:00Z")
    assert store.get_meta("last_sync_ts") == "2026-01-01T00:00:00Z"
    assert store.get_meta_int("missing", 42) == 42
    store.set_meta("attempts", "5")
    assert store.get_meta_int("attempts", 0) == 5
    assert store.meta_counters_tap("create_attempts") == 1
    assert store.meta_counters_tap("create_attempts") == 2
    assert store.meta_counters_tap("create_attempts") == 3
    assert store.get_meta_int("create_attempts", 0) == 3
    store.close()


def test_reopen_persists(tmp_path):
    path = str(tmp_path / "state.db")
    store = StateStore(path, project_default=12)
    store.upsert_row(make_row())
    store.set_meta("k", "v")
    store.close()
    reopened = StateStore(path, project_default=12)
    assert reopened.get_row("u1", 1001) == make_row()
    assert reopened.get_meta("k") == "v"
    reopened.close()


def test_wal_mode_active(tmp_path):
    path = str(tmp_path / "state.db")
    store = StateStore(path, project_default=12)
    store.close()
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_db_file_chmod(tmp_path):
    path = str(tmp_path / "state.db")
    StateStore(path, project_default=12)
    assert (os.stat(path).st_mode & 0o777) == 0o600


def test_wal_and_shm_sidecars_chmod(tmp_path):
    path = str(tmp_path / "state.db")
    store = StateStore(path, project_default=12)
    store.upsert_row(make_row())
    for name in (path, f"{path}-wal", f"{path}-shm"):
        assert os.path.exists(name)
        assert (os.stat(name).st_mode & 0o777) == 0o600
    store.close()
    for name in (path, f"{path}-wal", f"{path}-shm"):
        if os.path.exists(name):
            assert (os.stat(name).st_mode & 0o777) == 0o600

    reopened = StateStore(path, project_default=12)
    reopened.upsert_row(make_row())
    for name in (path, f"{path}-wal", f"{path}-shm"):
        assert os.path.exists(name)
        assert (os.stat(name).st_mode & 0o777) == 0o600
    reopened.close()


def test_in_transaction_commit(tmp_path):
    path = str(tmp_path / "state.db")
    store = StateStore(path, project_default=12)
    with store.in_transaction() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES ('a', '1')")
        other = sqlite3.connect(path)
        assert other.execute("SELECT value FROM meta WHERE key='a'").fetchone() is None
        other.close()
    assert store.get_meta("a") == "1"
    store.close()


def test_in_transaction_rollback(tmp_path):
    path = str(tmp_path / "state.db")
    store = StateStore(path, project_default=12)
    with pytest.raises(RuntimeError), store.in_transaction() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES ('b', '1')")
        raise RuntimeError("boom")
    assert store.get_meta("b") is None
    store.close()


def test_in_transaction_rollback_failure_preserves_original(tmp_path, monkeypatch):
    import edupagetasks.state as state_module

    class FlakyConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if isinstance(sql, str) and sql.strip().upper() == "ROLLBACK":
                raise sqlite3.OperationalError("rollback exploded")
            return super().execute(sql, *args, **kwargs)

    original_connect = state_module.sqlite3.connect

    def flaky_connect(path, **kwargs):
        return original_connect(path, factory=FlakyConnection, **kwargs)

    monkeypatch.setattr(state_module.sqlite3, "connect", flaky_connect)
    path = str(tmp_path / "state.db")
    store = StateStore(path, project_default=12)
    with pytest.raises(RuntimeError, match="boom"):
        with store.in_transaction():
            store.connection.execute("INSERT INTO meta (key, value) VALUES ('c', '1')")
            raise RuntimeError("boom")
    store.close()
    reopened = StateStore(path, project_default=12)
    assert reopened.get_meta("c") is None
    reopened.close()


def test_in_transaction_nested_raises(tmp_path):
    path = str(tmp_path / "state.db")
    store = StateStore(path, project_default=12)
    with store.in_transaction():
        with pytest.raises(RuntimeError, match="nested"):
            with store.in_transaction():
                pass
    store.close()
