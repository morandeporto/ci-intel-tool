"""Idempotent schema migrations for local SQLite and Turso (libSQL).

CREATE TABLE IF NOT EXISTS in schema.sql covers new databases. This module
adds columns/tables to existing DBs without dropping data.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)


def _table_columns(conn: Any, table: str) -> set[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    rows = cur.fetchall()
    names: set[str] = set()
    for row in rows:
        if isinstance(row, sqlite3.Row):
            names.add(str(row["name"]))
        elif isinstance(row, dict):
            names.add(str(row.get("name") or row.get("Name")))
        else:
            # PRAGMA table_info: cid, name, type, notnull, dflt_value, pk
            names.add(str(row[1]))
    return names


def _table_exists(conn: Any, table: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    )
    return cur.fetchone() is not None


def _add_column_if_missing(
    conn: Any,
    table: str,
    column: str,
    ddl_type_and_default: str,
) -> bool:
    """ADD COLUMN when absent. Returns True if a column was added."""
    cols = _table_columns(conn, table)
    if column in cols:
        return False
    sql = f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type_and_default}"
    conn.execute(sql)
    logger.info("Migrated %s: added column %s", table, column)
    return True


def migrate_schema(conn: Any) -> list[str]:
    """Apply pending migrations. Safe to call on every init_db / open.

    Returns a list of human-readable change descriptions (empty if up to date).
    """
    changes: list[str] = []

    if not _table_exists(conn, "news_items"):
        # Fresh schema.sql apply will create tables; nothing to migrate yet.
        return changes

    if _add_column_if_missing(
        conn, "news_items", "status", "TEXT NOT NULL DEFAULT 'classified'"
    ):
        changes.append("news_items.status")
    if _add_column_if_missing(conn, "news_items", "filter_reason", "TEXT"):
        changes.append("news_items.filter_reason")
    if _add_column_if_missing(conn, "news_items", "item_type", "TEXT"):
        changes.append("news_items.item_type")
    if _add_column_if_missing(conn, "news_items", "jfrog_implication", "TEXT"):
        changes.append("news_items.jfrog_implication")
    if _add_column_if_missing(
        conn, "news_items", "is_fallback", "INTEGER NOT NULL DEFAULT 0"
    ):
        changes.append("news_items.is_fallback")
    if _add_column_if_missing(conn, "news_items", "scored_by_model", "TEXT"):
        changes.append("news_items.scored_by_model")

    if _table_exists(conn, "pipeline_runs"):
        if _add_column_if_missing(
            conn,
            "pipeline_runs",
            "items_classified_ok",
            "INTEGER NOT NULL DEFAULT 0",
        ):
            changes.append("pipeline_runs.items_classified_ok")
        if _add_column_if_missing(
            conn, "pipeline_runs", "items_fallback", "INTEGER NOT NULL DEFAULT 0"
        ):
            changes.append("pipeline_runs.items_fallback")
        if _add_column_if_missing(
            conn, "pipeline_runs", "retries_used", "INTEGER NOT NULL DEFAULT 0"
        ):
            changes.append("pipeline_runs.retries_used")

    if not _table_exists(conn, "source_run_stats"):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS source_run_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                http_status TEXT,
                fetched INTEGER NOT NULL DEFAULT 0,
                in_window INTEGER NOT NULL DEFAULT 0,
                new INTEGER NOT NULL DEFAULT 0,
                passed_gate INTEGER NOT NULL DEFAULT 0,
                selected INTEGER NOT NULL DEFAULT 0,
                classified INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                duration_ms INTEGER,
                FOREIGN KEY (run_id) REFERENCES pipeline_runs(id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_source_run_stats_run
            ON source_run_stats(run_id)
            """
        )
        changes.append("source_run_stats")
        logger.info("Migrated: created source_run_stats table")

    if not _table_exists(conn, "llm_usage"):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_usage (
                date_utc TEXT NOT NULL,
                purpose TEXT NOT NULL,
                model TEXT NOT NULL,
                calls INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (date_utc, purpose, model)
            )
            """
        )
        changes.append("llm_usage")
        logger.info("Migrated: created llm_usage table")

    try:
        conn.commit()
    except Exception:
        pass
    return changes
