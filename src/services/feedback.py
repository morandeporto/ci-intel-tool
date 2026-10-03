"""User feedback capture for future weight-learning loops."""

from __future__ import annotations

from typing import Literal

from src.db.repository import Repository

Vote = Literal["up", "down"]


def record_feedback(
    repo: Repository,
    *,
    news_item_id: str,
    original_score: float,
    vote: Vote,
    rationale: str | None = None,
) -> int:
    """Persist a 👍/👎 vote and optional rationale. Returns feedback row id.

    One shared feedback row per news item (upsert) so all reviewers see the
    same saved signal. The automated weight-adjustment engine is Future Work.
    """
    if vote not in ("up", "down"):
        raise ValueError("vote must be 'up' or 'down'")
    item = repo.get_news_by_id(news_item_id)
    if item is None:
        raise ValueError(f"Unknown news item: {news_item_id}")

    clean_rationale = (rationale or "").strip() or None
    # Soft length guard - avoid storing large paste dumps as "PII-adjacent" noise.
    if clean_rationale and len(clean_rationale) > 2000:
        clean_rationale = clean_rationale[:2000]
    # Thumbs-up never needs a rationale.
    if vote == "up":
        clean_rationale = None

    return repo.upsert_feedback(
        news_item_id=news_item_id,
        original_score=float(original_score),
        vote=vote,
        rationale=clean_rationale,
    )


def get_latest_feedback(repo: Repository, news_item_id: str) -> dict | None:
    """Return the current shared feedback for a news item, if any."""
    return repo.get_latest_feedback(news_item_id)


def list_item_feedback(repo: Repository, news_item_id: str) -> list[dict]:
    """Return feedback rows for one news item (newest first)."""
    return [dict(r) for r in repo.list_feedback(news_item_id)]
