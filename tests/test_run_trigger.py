"""Pipeline run trigger persistence and Turso/libSQL column alias handling."""

from __future__ import annotations

from pathlib import Path

from src.db.connection import get_connection, init_db
from src.db.repository import Repository, _normalize_run_row


def test_normalize_run_row_maps_libsql_trigger_alias() -> None:
    row = _normalize_run_row({"id": "x", "TRIGGER": "cron", "status": "success"})
    assert row["trigger"] == "cron"


def test_list_runs_exposes_trigger(tmp_path: Path) -> None:
    db_path = tmp_path / "trig.db"
    init_db(db_path)
    conn = get_connection(db_path)
    try:
        repo = Repository(conn)
        run_id = repo.start_run("ui")
        repo.finish_run(run_id, status="success")
        listed = repo.list_runs(limit=1)
        assert listed[0]["trigger"] == "ui"
    finally:
        conn.close()
