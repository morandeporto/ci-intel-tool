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

    The automated weight-adjustment engine is Future Work; this only stores
    the signal so a learning loop can consume it later (lead-scoring style).
    """
    if vote not in ("up", "down"):
        raise ValueError("vote must be 'up' or 'down'")
    item = repo.get_news_by_id(news_item_id)
    if item is None:
        raise ValueError(f"Unknown news item: {news_item_id}")

    clean_rationale = (rationale or "").strip() or None
    # Soft length guard — avoid storing large paste dumps as "PII-adjacent" noise.
    if clean_rationale and len(clean_rationale) > 2000:
        clean_rationale = clean_rationale[:2000]

    return repo.add_feedback(
        news_item_id=news_item_id,
        original_score=float(original_score),
        vote=vote,
        rationale=clean_rationale,
    )


def list_item_feedback(repo: Repository, news_item_id: str) -> list[dict]:
    """Return feedback rows for one news item (newest first)."""
    return [dict(r) for r in repo.list_feedback(news_item_id)]
