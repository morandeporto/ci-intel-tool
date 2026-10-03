"""Idempotent schema migration tests (local SQLite)."""

from __future__ import annotations

from pathlib import Path

from src.db.connection import get_connection, init_db
from src.db.migrate import migrate_schema, _table_columns, _table_exists


LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    trigger TEXT NOT NULL,
    items_fetched INTEGER NOT NULL DEFAULT 0,
    items_new INTEGER NOT NULL DEFAULT 0,
    items_scored INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS news_items (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    source_id TEXT NOT NULL,
    competitor TEXT NOT NULL,
    published_at TEXT,
    ingested_at TEXT NOT NULL,
    summary TEXT,
    category TEXT,
    raw_excerpt TEXT,
    content_hash TEXT NOT NULL,
    relevance_score REAL,
    run_id TEXT
);
"""


def test_migrate_adds_columns_and_source_run_stats(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    conn = get_connection(db)
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            """
            INSERT INTO news_items (
                id, title, url, source_id, competitor, published_at, ingested_at,
                content_hash
            ) VALUES ('1', 'Keep me', 'https://ex.com/1', 's', 'jfrog',
                      '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00', 'h')
            """
        )
        conn.commit()
        changes = migrate_schema(conn)
        assert "news_items.status" in changes
        assert "source_run_stats" in changes
        assert "llm_usage" in changes
        cols = _table_columns(conn, "news_items")
        assert "status" in cols
        assert "filter_reason" in cols
        assert "item_type" in cols
        assert "jfrog_implication" in cols
        assert "is_fallback" in cols
        assert "scored_by_model" in cols
        assert "news_items.scored_by_model" in changes
        assert "rubric_version" in cols
        assert "news_items.rubric_version" in changes
        assert _table_exists(conn, "source_run_stats")
        assert _table_exists(conn, "llm_usage")
        # Existing row preserved.
        row = conn.execute("SELECT title, status FROM news_items WHERE id='1'").fetchone()
        assert row[0] == "Keep me"
        assert row[1] == "classified"
        # Idempotent second pass.
        assert migrate_schema(conn) == []
    finally:
        conn.close()


def test_init_db_applies_migration(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    init_db(db)
    conn = get_connection(db)
    try:
        cols = _table_columns(conn, "news_items")
        assert "status" in cols
        assert "scored_by_model" in cols
        assert "rubric_version" in cols
        assert _table_exists(conn, "source_run_stats")
        assert _table_exists(conn, "llm_usage")
    finally:
        conn.close()
