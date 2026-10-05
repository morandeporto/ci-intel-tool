"""Pipeline run trigger persistence and Turso/libSQL column alias handling.

``ui`` remains a known trigger label so historical Run Now rows still display;
nothing in the UI creates new ``ui`` runs anymore.
"""

from __future__ import annotations

from pathlib import Path

from src.db.connection import get_connection, init_db
from src.db.repository import Repository, _normalize_run_row
from src.pipeline.run_daily import RUN_TRIGGER_CHOICES


def test_normalize_run_row_maps_libsql_trigger_alias() -> None:
    row = _normalize_run_row({"id": "x", "TRIGGER": "cron", "status": "success"})
    assert row["trigger"] == "cron"


def test_ui_trigger_kept_for_historical_rows() -> None:
    assert "ui" in RUN_TRIGGER_CHOICES


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
