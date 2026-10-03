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
           n.relevance_score, n.run_id, n.status, n.filter_reason,
           n.item_type, n.jfrog_implication, n.is_fallback, n.scored_by_model,
           d.jfrog_relevance, d.competitor_signal, d.strategic_impact,
           d.freshness, d.market_visibility, d.model_id, d.scored_at
    FROM news_items n
    LEFT JOIN dimension_scores d ON d.news_item_id = n.id
    WHERE COALESCE(n.status, 'classified') != 'filtered'
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
        items_classified_ok: int = 0,
        items_fallback: int = 0,
        retries_used: int = 0,
        error_message: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE pipeline_runs
            SET finished_at = ?, status = ?, items_fetched = ?, items_new = ?,
                items_scored = ?, items_classified_ok = ?, items_fallback = ?,
                retries_used = ?, error_message = ?
            WHERE id = ?
            """,
            (
                _utc_now(),
                status,
                items_fetched,
                items_new,
                items_scored,
                items_classified_ok,
                items_fallback,
                retries_used,
                error_message,
                run_id,
            ),
        )
        self.conn.commit()

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            """
            SELECT id, started_at, finished_at, status, trigger,
                   items_fetched, items_new, items_scored,
                   items_classified_ok, items_fallback, retries_used, error_message
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
                   items_fetched, items_new, items_scored,
                   items_classified_ok, items_fallback, retries_used, error_message
            FROM pipeline_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        )
        return _row_as_dict(cur, cur.fetchone())

    def save_source_run_stats(self, rows: list[dict[str, Any]]) -> None:
        """Persist per-source telemetry for one pipeline run."""
        if not rows:
            return
        self.conn.executemany(
            """
            INSERT INTO source_run_stats (
                run_id, source_id, http_status, fetched, in_window, new,
                passed_gate, selected, classified, error, warning, duration_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r["run_id"],
                    r["source_id"],
                    r.get("http_status"),
                    int(r.get("fetched") or 0),
                    int(r.get("in_window") or 0),
                    int(r.get("new") or 0),
                    int(r.get("passed_gate") or 0),
                    int(r.get("selected") or 0),
                    int(r.get("classified") or 0),
                    r.get("error"),
                    r.get("warning"),
                    r.get("duration_ms"),
                )
                for r in rows
            ],
        )
        self.conn.commit()

    def count_source_warnings_by_run(self, run_ids: list[str]) -> dict[str, int]:
        """Return run_id -> count of source_run_stats rows with a warning."""
        if not run_ids:
            return {}
        placeholders = ",".join("?" for _ in run_ids)
        cur = self.conn.execute(
            f"""
            SELECT run_id, COUNT(*) AS c
            FROM source_run_stats
            WHERE run_id IN ({placeholders})
              AND warning IS NOT NULL
              AND TRIM(warning) != ''
            GROUP BY run_id
            """,
            tuple(run_ids),
        )
        out: dict[str, int] = {}
        for row in _rows_as_dicts(cur):
            out[str(row["run_id"])] = int(row["c"] or 0)
        return out

    def list_source_run_stats(self, run_id: str) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            """
            SELECT run_id, source_id, http_status, fetched, in_window, new,
                   passed_gate, selected, classified, error, warning, duration_ms
            FROM source_run_stats
            WHERE run_id = ?
            ORDER BY source_id
            """,
            (run_id,),
        )
        return _rows_as_dicts(cur)

    # --- news + scores -------------------------------------------------
    def existing_urls(self) -> set[str]:
        cur = self.conn.execute("SELECT url FROM news_items")
        rows = _rows_as_dicts(cur)
        return {str(r["url"]) for r in rows}

    def existing_content_hashes(self) -> set[str]:
        cur = self.conn.execute("SELECT content_hash FROM news_items")
        rows = _rows_as_dicts(cur)
        return {str(r["content_hash"]) for r in rows}

    def urls_missing_dimension_scores(self) -> set[str]:
        """URLs stored without dimension_scores (failed or partial ingest).

        Skips gate-filtered and fallback rows so they are not retried forever.
        """
        cur = self.conn.execute(
            """
            SELECT n.url FROM news_items n
            LEFT JOIN dimension_scores d ON d.news_item_id = n.id
            WHERE d.news_item_id IS NULL
              AND COALESCE(n.status, 'classified') != 'filtered'
              AND COALESCE(n.is_fallback, 0) = 0
            """
        )
        return {str(r["url"]) for r in _rows_as_dicts(cur)}

    def upsert_news_item(self, item: NewsItem) -> str:
        """Insert or update by URL. Returns the stable news_items.id (needed for scores)."""
        row = self.conn.execute(
            "SELECT id FROM news_items WHERE url = ?",
            (item.url,),
        ).fetchone()
        if row is not None:
            news_id = str(_scalar(row, "id", 0))
            self.conn.execute(
                """
                UPDATE news_items SET
                    title = ?, source_id = ?, competitor = ?, published_at = ?,
                    ingested_at = ?, summary = ?, category = ?, raw_excerpt = ?,
                    content_hash = ?, relevance_score = ?, run_id = ?,
                    status = ?, filter_reason = ?, item_type = ?, jfrog_implication = ?,
                    is_fallback = ?, scored_by_model = ?
                WHERE url = ?
                """,
                (
                    item.title,
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
                    item.status,
                    item.filter_reason,
                    item.item_type,
                    item.jfrog_implication,
                    1 if item.is_fallback else 0,
                    item.scored_by_model,
                    item.url,
                ),
            )
        else:
            news_id = item.id
            self.conn.execute(
                """
                INSERT INTO news_items (
                    id, title, url, source_id, competitor, published_at, ingested_at,
                    summary, category, raw_excerpt, content_hash, relevance_score, run_id,
                    status, filter_reason, item_type, jfrog_implication, is_fallback,
                    scored_by_model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    item.status,
                    item.filter_reason,
                    item.item_type,
                    item.jfrog_implication,
                    1 if item.is_fallback else 0,
                    item.scored_by_model,
                ),
            )
        self.conn.commit()
        return news_id

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
                   n.relevance_score, n.run_id, n.status, n.filter_reason,
                   n.item_type, n.jfrog_implication, n.is_fallback,
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

    def list_fallback_items(self) -> list[dict[str, Any]]:
        """All fallback news rows (selection/window applied in process.rescore)."""
        cur = self.conn.execute(
            """
            SELECT id, title, url, source_id, competitor, published_at, ingested_at,
                   summary, category, raw_excerpt, content_hash, relevance_score,
                   run_id, status, filter_reason, item_type, jfrog_implication,
                   is_fallback, scored_by_model
            FROM news_items
            WHERE COALESCE(is_fallback, 0) = 1
              AND COALESCE(status, 'classified') != 'filtered'
            ORDER BY ingested_at DESC
            """
        )
        return _rows_as_dicts(cur)

    def apply_classification_to_item(
        self,
        news_item_id: str,
        *,
        summary: str,
        category: str,
        item_type: str | None,
        jfrog_implication: str | None,
        relevance_score: float | None,
        is_fallback: bool,
        scores: DimensionScores | None,
        scored_by_model: str | None = None,
    ) -> None:
        """Update a stored news row after (re)classification."""
        self.conn.execute(
            """
            UPDATE news_items SET
                summary = ?, category = ?, item_type = ?, jfrog_implication = ?,
                relevance_score = ?, is_fallback = ?, status = 'classified',
                filter_reason = NULL, scored_by_model = ?
            WHERE id = ?
            """,
            (
                summary,
                category,
                item_type,
                jfrog_implication,
                relevance_score,
                1 if is_fallback else 0,
                scored_by_model,
                news_item_id,
            ),
        )
        if scores is not None and not is_fallback:
            self.save_dimension_scores(news_item_id, scores)
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

    def upsert_feedback(
        self,
        news_item_id: str,
        original_score: float,
        vote: str,
        rationale: str | None = None,
    ) -> int:
        """Replace the shared feedback for a news item (exactly one row)."""
        if vote not in ("up", "down"):
            raise ValueError("vote must be 'up' or 'down'")
        item_id = str(news_item_id)
        # Drop every prior vote for this item so 👍 after 👎 updates, never duplicates.
        self.conn.execute("DELETE FROM feedback WHERE news_item_id = ?", (item_id,))
        cur = self.conn.execute(
            """
            INSERT INTO feedback (news_item_id, original_score, vote, rationale, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (item_id, original_score, vote, rationale, _utc_now()),
        )
        self.conn.commit()
        last_id = getattr(cur, "lastrowid", None)
        return int(last_id) if last_id is not None else 0

    def get_latest_feedback(self, news_item_id: str) -> dict[str, Any] | None:
        rows = self.list_feedback(news_item_id)
        return rows[0] if rows else None

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

    # --- llm_usage (soft per-model daily counters) ----------------------
    def get_llm_usage_calls(
        self,
        *,
        date_utc: str,
        model: str,
        purpose: str | None = None,
    ) -> int:
        """Sum soft call counters for a model on a calendar day."""
        if purpose is None:
            cur = self.conn.execute(
                """
                SELECT COALESCE(SUM(calls), 0) AS c FROM llm_usage
                WHERE date_utc = ? AND model = ?
                """,
                (date_utc, model),
            )
        else:
            cur = self.conn.execute(
                """
                SELECT COALESCE(SUM(calls), 0) AS c FROM llm_usage
                WHERE date_utc = ? AND model = ? AND purpose = ?
                """,
                (date_utc, model, purpose),
            )
        row = cur.fetchone()
        return int(_scalar(row, "c", 0) or 0)

    def increment_llm_usage(
        self,
        *,
        date_utc: str,
        purpose: str,
        model: str,
        calls: int = 1,
    ) -> int:
        """Atomically bump the soft usage counter, returns new total for that row."""
        if calls < 1:
            return self.get_llm_usage_calls(
                date_utc=date_utc, model=model, purpose=purpose
            )
        self.conn.execute(
            """
            INSERT INTO llm_usage (date_utc, purpose, model, calls)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date_utc, purpose, model) DO UPDATE SET
                calls = calls + excluded.calls
            """,
            (date_utc, purpose, model, calls),
        )
        self.conn.commit()
        return self.get_llm_usage_calls(
            date_utc=date_utc, model=model, purpose=purpose
        )

    def list_pending_or_fallback_items(self) -> list[dict[str, Any]]:
        """Rows awaiting (re)scoring: pending_scoring or is_fallback."""
        cur = self.conn.execute(
            """
            SELECT id, title, url, source_id, competitor, published_at, ingested_at,
                   summary, category, raw_excerpt, content_hash, relevance_score,
                   run_id, status, filter_reason, item_type, jfrog_implication,
                   is_fallback, scored_by_model
            FROM news_items
            WHERE COALESCE(status, 'classified') != 'filtered'
              AND (
                    COALESCE(status, '') IN ('pending_scoring', 'scoring')
                 OR COALESCE(is_fallback, 0) = 1
              )
            ORDER BY ingested_at DESC
            """
        )
        return _rows_as_dicts(cur)

    def release_stale_scoring_claims(
        self,
        *,
        older_than_minutes: int = 30,
    ) -> int:
        """Return items stuck in status=scoring back to pending_scoring."""
        # ingested_at / a side column isn't ideal, use filter_reason stamp when claimed.
        # Claim stores filter_reason = 'scoring_claimed_at:<iso>'.
        cur = self.conn.execute(
            """
            SELECT id, filter_reason FROM news_items
            WHERE status = 'scoring'
            """
        )
        rows = _rows_as_dicts(cur)
        now = datetime.now(timezone.utc)
        released = 0
        for row in rows:
            reason = str(row.get("filter_reason") or "")
            claimed_at = None
            if reason.startswith("scoring_claimed_at:"):
                raw = reason.split(":", 1)[1]
                try:
                    claimed_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    claimed_at = None
            if claimed_at is None:
                # Unknown claim time - release to avoid permanent stuck state.
                stale = True
            else:
                if claimed_at.tzinfo is None:
                    claimed_at = claimed_at.replace(tzinfo=timezone.utc)
                age_min = (now - claimed_at.astimezone(timezone.utc)).total_seconds() / 60.0
                stale = age_min > float(older_than_minutes)
            if not stale:
                continue
            self.conn.execute(
                """
                UPDATE news_items
                SET status = 'pending_scoring', filter_reason = 'stale_scoring_claim'
                WHERE id = ? AND status = 'scoring'
                """,
                (row["id"],),
            )
            released += 1
        if released:
            self.conn.commit()
        return released

    def claim_item_for_scoring(self, news_item_id: str) -> bool:
        """Atomically claim a pending_scoring (or reclaimable) row for scoring.

        Returns True only when this caller won the claim (rows affected == 1).
        """
        stamp = f"scoring_claimed_at:{_utc_now()}"
        cur = self.conn.execute(
            """
            UPDATE news_items
            SET status = 'scoring', filter_reason = ?
            WHERE id = ?
              AND status = 'pending_scoring'
            """,
            (stamp, news_item_id),
        )
        self.conn.commit()
        affected = getattr(cur, "rowcount", None)
        if affected is None:
            # libsql may not expose rowcount - verify status.
            row = self.conn.execute(
                "SELECT status, filter_reason FROM news_items WHERE id = ?",
                (news_item_id,),
            ).fetchone()
            if row is None:
                return False
            status = str(_scalar(row, "status", 0) or "")
            reason = str(_scalar(row, "filter_reason", 1) or "")
            return status == "scoring" and reason == stamp
        return int(affected) == 1

    def claim_fallback_item_for_scoring(self, news_item_id: str) -> bool:
        """Claim an is_fallback row that is not already being scored."""
        stamp = f"scoring_claimed_at:{_utc_now()}"
        cur = self.conn.execute(
            """
            UPDATE news_items
            SET status = 'scoring', filter_reason = ?
            WHERE id = ?
              AND COALESCE(is_fallback, 0) = 1
              AND COALESCE(status, 'classified') NOT IN ('filtered', 'scoring')
            """,
            (stamp, news_item_id),
        )
        self.conn.commit()
        affected = getattr(cur, "rowcount", None)
        if affected is None:
            row = self.conn.execute(
                "SELECT status, filter_reason FROM news_items WHERE id = ?",
                (news_item_id,),
            ).fetchone()
            if row is None:
                return False
            status = str(_scalar(row, "status", 0) or "")
            reason = str(_scalar(row, "filter_reason", 1) or "")
            return status == "scoring" and reason == stamp
        return int(affected) == 1


__all__ = ["Repository", "DIMENSION_NAMES"]
