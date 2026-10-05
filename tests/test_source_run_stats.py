"""Per-source pipeline telemetry: one row per source per run."""

from __future__ import annotations

from pathlib import Path

from src.db.connection import get_connection, init_db
from src.db.repository import Repository
from src.db import migrate as migrate_mod
from src.pipeline.run_daily import _build_source_run_stat_rows
from src.ingest.rss_fetcher import SourceFetchStat


def test_build_source_run_stat_rows_no_duplicate_when_in_fetch_stats() -> None:
    from src.ingest.normalize import NormalizedEntry
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    entry = NormalizedEntry(
        source_id="thenewstack",
        competitor="industry",
        title="t",
        url="https://example.com/a",
        published_at=now,
        raw_excerpt="x",
        content_hash="h",
    )
    rows = _build_source_run_stat_rows(
        run_id="r1",
        fetch_stats=[
            SourceFetchStat(
                source_id="thenewstack",
                http_status="200",
                fetched=2,
            )
        ],
        new_entries=[entry],
        in_window=[entry],
        filtered=[],
        selected=[],
        classified_by_source={},
    )
    assert len(rows) == 1
    assert rows[0]["http_status"] == "200"


def test_save_source_run_stats_upserts_one_row_per_source(tmp_path: Path) -> None:
    db_path = tmp_path / "stats.db"
    init_db(db_path)
    conn = get_connection(db_path)
    migrate_mod.migrate_schema(conn)
    try:
        repo = Repository(conn)
        run_id = repo.start_run("cli")
        base = {
            "run_id": run_id,
            "source_id": "devops_com",
            "http_status": "200",
            "fetched": 1,
            "in_window": 1,
            "new": 1,
            "passed_gate": 1,
            "selected": 0,
            "classified": 0,
            "error": None,
            "warning": None,
            "duration_ms": 10,
        }
        repo.save_source_run_stats([base])
        repo.save_source_run_stats(
            [{**base, "http_status": None, "fetched": 0, "selected": 1, "classified": 1}]
        )
        cur = conn.execute(
            "SELECT COUNT(*) FROM source_run_stats WHERE run_id = ? AND source_id = ?",
            (run_id, "devops_com"),
        )
        assert int(cur.fetchone()[0]) == 1
        row = conn.execute(
            "SELECT selected, classified, http_status FROM source_run_stats WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row[0] == 1
        assert row[1] == 1
    finally:
        conn.close()
