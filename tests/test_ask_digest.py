"""Unit tests for light RAG retrieval (no LLM calls)."""

from __future__ import annotations

from src.db.connection import get_connection, init_db
from src.db.repository import Repository
from src.services.ask_digest import retrieve_relevant_items
from src.db.models import DimensionScores, NewsItem


def test_retrieve_ranks_keyword_overlap(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    conn = get_connection(db)
    repo = Repository(conn)

    repo.upsert_news_item(
        NewsItem(
            id="1",
            title="Snyk launches new SCA feature",
            url="https://example.com/snyk",
            source_id="snyk_blog",
            competitor="snyk",
            published_at="2026-10-01",
            ingested_at="2026-10-01T00:00:00+00:00",
            summary="Snyk improved open source scanning",
            category="product_release",
            raw_excerpt="Snyk SCA",
            content_hash="a" * 64,
            relevance_score=4.0,
            run_id=None,
        )
    )
    repo.upsert_news_item(
        NewsItem(
            id="2",
            title="Unrelated cooking recipes",
            url="https://example.com/food",
            source_id="x",
            competitor="industry",
            published_at="2026-10-01",
            ingested_at="2026-10-01T00:00:00+00:00",
            summary="pasta",
            category="other",
            raw_excerpt="pasta",
            content_hash="b" * 64,
            relevance_score=1.0,
            run_id=None,
        )
    )

    hits = retrieve_relevant_items(repo, "What did Snyk announce about SCA?", top_k=5)
    assert hits
    assert hits[0].id == "1"
    conn.close()
