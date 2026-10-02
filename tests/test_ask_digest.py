"""Unit tests for light RAG retrieval + Ask prompt grounding (no LLM calls)."""

from __future__ import annotations

import pytest

from src.db.connection import get_connection, init_db
from src.db.models import NewsItem
from src.db.repository import Repository
from src.services.ask_digest import (
    MAX_FOLLOW_UPS,
    MAX_USER_TURNS,
    ChatTurn,
    RetrievedItem,
    ask_digest,
    build_ask_prompt,
    format_comparison_context,
    retrieve_relevant_items,
)
from src.process.llm_classify import ClassifyError


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


def test_format_comparison_includes_sourced_claims():
    text = format_comparison_context()
    assert "Capability:" in text
    assert "source=" in text or "Unknown" in text
    assert "jfrog" in text.lower() or "JFrog" in text


def test_build_ask_prompt_includes_matrix_and_news_and_history():
    items = [
        RetrievedItem(
            id="1",
            title="Snyk news",
            url="https://example.com/1",
            summary="SCA update",
            competitor="snyk",
            relevance_score=4.0,
            score=2.0,
        )
    ]
    prompt = build_ask_prompt(
        "How does that compare to Artifactory?",
        items,
        comparison_text="Capability: Artifact management\n  - JFrog: Artifactory | source=https://jfrog.com",
        history=[
            ChatTurn(role="user", content="What did Snyk announce?"),
            ChatTurn(role="assistant", content="Snyk announced SCA updates [1]."),
        ],
    )
    assert "PRODUCT_COMPARISON" in prompt
    assert "RETRIEVED_NEWS" in prompt
    assert "Snyk news" in prompt
    assert "Artifactory" in prompt
    assert "PRIOR CONVERSATION" in prompt
    assert "What did Snyk announce?" in prompt


def test_ask_digest_rejects_oversized_thread(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    conn = get_connection(db)
    repo = Repository(conn)
    history = []
    for i in range(MAX_USER_TURNS):
        history.append(ChatTurn(role="user", content=f"q{i}"))
        history.append(ChatTurn(role="assistant", content=f"a{i}"))
    assert MAX_FOLLOW_UPS == 2
    with pytest.raises(ClassifyError, match="follow-up"):
        ask_digest(repo, "one more?", history=history)
    conn.close()
