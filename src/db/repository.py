"""CRUD repository for news, scores, feedback, and pipeline runs.

Works with both stdlib sqlite3 (Row) and Turso/libsql (tuple rows).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import uuid4

from src.db.models import DIMENSION_NAMES, DimensionScores, NewsItem

NEWS_WITH_SCORES_SQL = """
    SELECT n.id, n.title, n.url, n.source_id, n.competitor, n.published_at,
           n.ingested_at, n.summary, n.category, n.raw_excerpt, n.content_hash,
           n.relevance_score, n.run_id,
           d.jfrog_relevance, d.competitor_signal, d.strategic_impact,
           d.freshness, d.market_visibility, d.model_id, d.scored_at
    FROM news_items n
    LEFT JOIN dimension_scores d ON d.news_item_id = n.id
    ORDER BY (n.relevance_score IS NULL), n.relevance_score DESC, n.ingested_at DESC
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _rows_as_dicts(cursor: Any) -> list[dict[str, Any]]:
    """Normalize sqlite3.Row / mapping / tuple rows using cursor.description."""
    raw = cursor.fetchall()
    if not raw:
        return []
    first = raw[0]
    if isinstance(first, sqlite3.Row):
        return [dict(r) for r in raw]
    if isinstance(first, dict):
        return [dict(r) for r in raw]
    description = getattr(cursor, "description", None) or []
    cols = [d[0] for d in description]
    if not cols:
        raise TypeError("Cannot map tuple rows without cursor.description")
    return [dict(zip(cols, r)) for r in raw]


def _row_as_dict(cursor: Any, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if isinstance(row, dict):
        return dict(row)
    description = getattr(cursor, "description", None) or []
    cols = [d[0] for d in description]
    if not cols:
        raise TypeError("Cannot map tuple row without cursor.description")
    return dict(zip(cols, row))


def _scalar(row: Any, key: str = "c", index: int = 0) -> Any:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return row[key]
    if isinstance(row, dict):
        return row.get(key, row.get(list(row.keys())[index]))
    return row[index]


class Repository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    # --- pipeline runs -------------------------------------------------
    def start_run(self, trigger: str) -> str:
        run_id = str(uuid4())
        self.conn.execute(
            """
            INSERT INTO pipeline_runs (id, started_at, status, trigger)
            VALUES (?, ?, 'running', ?)
            """,
            (run_id, _utc_now(), trigger),
        )
        self.conn.commit()
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        items_fetched: int = 0,
        items_new: int = 0,
        items_scored: int = 0,
        error_message: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE pipeline_runs
            SET finished_at = ?, status = ?, items_fetched = ?, items_new = ?,
                items_scored = ?, error_message = ?
            WHERE id = ?
            """,
            (
                _utc_now(),
                status,
                items_fetched,
                items_new,
                items_scored,
                error_message,
                run_id,
            ),
        )
        self.conn.commit()

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            """
            SELECT id, started_at, finished_at, status, trigger,
                   items_fetched, items_new, items_scored, error_message
            FROM pipeline_runs
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return _rows_as_dicts(cur)

    def get_latest_run(self) -> dict[str, Any] | None:
        cur = self.conn.execute(
            """
            SELECT id, started_at, finished_at, status, trigger,
                   items_fetched, items_new, items_scored, error_message
            FROM pipeline_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        )
        return _row_as_dict(cur, cur.fetchone())

    # --- news + scores -------------------------------------------------
    def existing_urls(self) -> set[str]:
        cur = self.conn.execute("SELECT url FROM news_items")
        rows = _rows_as_dicts(cur)
        return {str(r["url"]) for r in rows}

    def existing_content_hashes(self) -> set[str]:
        cur = self.conn.execute("SELECT content_hash FROM news_items")
        rows = _rows_as_dicts(cur)
        return {str(r["content_hash"]) for r in rows}

    def upsert_news_item(self, item: NewsItem) -> None:
        self.conn.execute(
            """
            INSERT INTO news_items (
                id, title, url, source_id, competitor, published_at, ingested_at,
                summary, category, raw_excerpt, content_hash, relevance_score, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                title = excluded.title,
                summary = excluded.summary,
                category = excluded.category,
                raw_excerpt = excluded.raw_excerpt,
                relevance_score = excluded.relevance_score,
                run_id = excluded.run_id
            """,
            (
                item.id,
                item.title,
                item.url,
                item.source_id,
                item.competitor,
                item.published_at,
                item.ingested_at,
                item.summary,
                item.category,
                item.raw_excerpt,
                item.content_hash,
                item.relevance_score,
                item.run_id,
            ),
        )
        self.conn.commit()

    def save_dimension_scores(self, news_item_id: str, scores: DimensionScores) -> None:
        self.conn.execute(
            """
            INSERT INTO dimension_scores (
                news_item_id, jfrog_relevance, competitor_signal, strategic_impact,
                freshness, market_visibility, model_id, scored_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(news_item_id) DO UPDATE SET
                jfrog_relevance = excluded.jfrog_relevance,
                competitor_signal = excluded.competitor_signal,
                strategic_impact = excluded.strategic_impact,
                freshness = excluded.freshness,
                market_visibility = excluded.market_visibility,
                model_id = excluded.model_id,
                scored_at = excluded.scored_at
            """,
            (
                news_item_id,
                scores.jfrog_relevance,
                scores.competitor_signal,
                scores.strategic_impact,
                scores.freshness,
                scores.market_visibility,
                scores.model_id,
                scores.scored_at,
            ),
        )
        self.conn.commit()

    def list_news_with_scores(self, limit: int | None = None) -> list[dict[str, Any]]:
        if limit is not None:
            cur = self.conn.execute(NEWS_WITH_SCORES_SQL + " LIMIT ?", (limit,))
        else:
            cur = self.conn.execute(NEWS_WITH_SCORES_SQL)
        return _rows_as_dicts(cur)

    def get_news_by_id(self, news_item_id: str) -> dict[str, Any] | None:
        cur = self.conn.execute(
            """
            SELECT n.id, n.title, n.url, n.source_id, n.competitor, n.published_at,
                   n.ingested_at, n.summary, n.category, n.raw_excerpt, n.content_hash,
                   n.relevance_score, n.run_id,
                   d.jfrog_relevance, d.competitor_signal, d.strategic_impact,
                   d.freshness, d.market_visibility, d.model_id, d.scored_at
            FROM news_items n
            LEFT JOIN dimension_scores d ON d.news_item_id = n.id
            WHERE n.id = ?
            """,
            (news_item_id,),
        )
        return _row_as_dict(cur, cur.fetchone())

    def update_relevance_scores(self, updates: Iterable[tuple[str, float]]) -> None:
        self.conn.executemany(
            "UPDATE news_items SET relevance_score = ? WHERE id = ?",
            [(score, item_id) for item_id, score in updates],
        )
        self.conn.commit()

    def news_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM news_items").fetchone()
        val = _scalar(row, "c", 0)
        return int(val or 0)

    def average_relevance(self) -> float | None:
        row = self.conn.execute(
            "SELECT AVG(relevance_score) AS avg_score FROM news_items WHERE relevance_score IS NOT NULL"
        ).fetchone()
        val = _scalar(row, "avg_score", 0)
        return float(val) if val is not None else None

    # --- feedback ------------------------------------------------------
    def add_feedback(
        self,
        news_item_id: str,
        original_score: float,
        vote: str,
        rationale: str | None = None,
    ) -> int:
        if vote not in ("up", "down"):
            raise ValueError("vote must be 'up' or 'down'")
        cur = self.conn.execute(
            """
            INSERT INTO feedback (news_item_id, original_score, vote, rationale, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (news_item_id, original_score, vote, rationale, _utc_now()),
        )
        self.conn.commit()
        last_id = getattr(cur, "lastrowid", None)
        return int(last_id) if last_id is not None else 0

    def list_feedback(self, news_item_id: str | None = None) -> list[dict[str, Any]]:
        if news_item_id:
            cur = self.conn.execute(
                """
                SELECT id, news_item_id, original_score, vote, rationale, created_at
                FROM feedback WHERE news_item_id = ? ORDER BY created_at DESC
                """,
                (news_item_id,),
            )
        else:
            cur = self.conn.execute(
                """
                SELECT id, news_item_id, original_score, vote, rationale, created_at
                FROM feedback ORDER BY created_at DESC
                """
            )
        return _rows_as_dicts(cur)

    # --- app settings --------------------------------------------------
    def get_setting(self, key: str) -> str | None:
        row = self.get_setting_row(key)
        return None if row is None else str(row["value"])

    def get_setting_row(self, key: str) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT key, value, updated_at FROM app_settings WHERE key = ?",
            (key,),
        )
        return _row_as_dict(cur, cur.fetchone())

    def set_setting(self, key: str, value: str, updated_at: str | None = None) -> None:
        ts = updated_at or _utc_now()
        self.conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, ts),
        )
        self.conn.commit()


__all__ = ["Repository", "DIMENSION_NAMES"]
