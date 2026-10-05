"""One feedback query per digest rerun must match the old per-card lookups."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.db.connection import get_connection, init_db
from src.db.repository import Repository
from src.services.digest import digest_kpis
from src.services.feedback import latest_feedback_by_item

ITEM_IDS = ("item-a", "item-b", "item-c", "item-no-feedback")


@pytest.fixture()
def repo(tmp_path: Path) -> Repository:
    db_path = tmp_path / "fb.db"
    init_db(db_path)
    conn = get_connection(db_path)
    for item_id in ITEM_IDS:
        conn.execute(
            """
            INSERT INTO news_items (id, title, url, source_id, competitor, ingested_at, content_hash)
            VALUES (?, ?, ?, 'src', 'GitHub', '2026-10-05T10:00:00+00:00', ?)
            """,
            (item_id, item_id, f"https://example.com/{item_id}", f"hash-{item_id}"),
        )
    conn.commit()
    repo = Repository(conn)
    repo.upsert_feedback("item-a", 3.0, "up")
    repo.upsert_feedback("item-b", 2.0, "down", "Not relevant because test")
    repo.upsert_feedback("item-c", 4.0, "up")
    # Legacy duplicate (pre-upsert era): an older row for item-c must not win.
    conn.execute(
        """
        INSERT INTO feedback (news_item_id, original_score, vote, rationale, created_at)
        VALUES ('item-c', 1.0, 'down', 'old', '2020-01-01T00:00:00+00:00')
        """
    )
    conn.commit()
    return repo


def test_latest_feedback_map_matches_per_item_lookup(repo: Repository) -> None:
    latest = latest_feedback_by_item(repo.list_feedback())
    for item_id in ITEM_IDS:
        assert latest.get(item_id) == repo.get_latest_feedback(item_id)
    assert latest["item-c"]["vote"] == "up"
    assert "item-no-feedback" not in latest


def test_digest_kpis_reusing_feedback_rows_matches_default(repo: Repository) -> None:
    items = [{"relevance_score": 4.2}, {"relevance_score": 2.0}, {"relevance_score": None}]
    default = digest_kpis(repo, items)
    reused = digest_kpis(
        repo, items, feedback_rows=repo.list_feedback(), include_latest_run=False
    )
    for key in ("item_count", "avg_score", "high_score_count", "feedback_count", "fallback_count"):
        assert reused[key] == default[key]
    assert reused["feedback_count"] == 4
    assert reused["latest_run_status"] is None
    assert reused["latest_run_at"] is None
