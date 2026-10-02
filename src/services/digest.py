"""Daily digest service: list and re-rank news without touching Streamlit."""

from __future__ import annotations

from typing import Any, Literal

from src.config_loader import load_weights
from src.db.models import DIMENSION_NAMES
from src.db.repository import Repository
from src.process.scoring import recalculate_scores, weighted_score

DigestSort = Literal["date", "relevance"]


def _dimensions_from_row(row: dict[str, Any]) -> dict[str, int] | None:
    """Extract dimension scores from a joined news row, or None if unscored."""
    if row.get("jfrog_relevance") is None:
        return None
    return {name: int(row[name]) for name in DIMENSION_NAMES}


def _item_created_at(row: dict[str, Any]) -> str:
    """Prefer ingest time (DB creation); fall back to source publish time."""
    return str(row.get("ingested_at") or row.get("published_at") or "")


def list_digest(
    repo: Repository,
    *,
    weight_overrides: dict[str, float] | None = None,
    limit: int | None = None,
    persist_scores: bool = False,
    sort_by: DigestSort = "date",
) -> list[dict[str, Any]]:
    """Return digest items sorted by creation date (default) or relevance.

    When ``weight_overrides`` is provided, scores are recalculated in memory from
    stored dimension scores so the LLM is never re-queried. Set
    ``persist_scores=True`` to write recalculated totals back to SQLite.
    """
    weights = weight_overrides if weight_overrides is not None else load_weights()
    rows = repo.list_news_with_scores(limit=None)

    scored_pairs: list[tuple[str, dict[str, int]]] = []
    for row in rows:
        dims = _dimensions_from_row(row)
        if dims is not None:
            scored_pairs.append((row["id"], dims))

    if scored_pairs:
        recalculated = dict(recalculate_scores(scored_pairs, weights))
        for row in rows:
            if row["id"] in recalculated:
                row["relevance_score"] = recalculated[row["id"]]
                row["dimensions"] = _dimensions_from_row(row)
            else:
                row["dimensions"] = None
        if persist_scores:
            repo.update_relevance_scores(recalculated.items())
    else:
        for row in rows:
            row["dimensions"] = _dimensions_from_row(row)

    if sort_by == "relevance":
        rows.sort(
            key=lambda r: (
                r["relevance_score"] is None,
                -(r["relevance_score"] or 0.0),
                _item_created_at(r),
            )
        )
    else:
        rows.sort(key=_item_created_at, reverse=True)

    if limit is not None:
        rows = rows[:limit]
    return rows


def digest_kpis(repo: Repository, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Aggregate KPI numbers for the Daily Digest header."""
    items = items if items is not None else list_digest(repo)
    scores = [float(i["relevance_score"]) for i in items if i.get("relevance_score") is not None]
    latest_run = repo.get_latest_run()
    return {
        "item_count": len(items),
        "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
        "high_score_count": sum(1 for s in scores if s >= 4.0),
        "feedback_count": len(repo.list_feedback()),
        "latest_run_status": latest_run["status"] if latest_run else None,
        "latest_run_at": latest_run["started_at"] if latest_run else None,
    }


def score_item_with_weights(
    item: dict[str, Any],
    weights: dict[str, float],
) -> float | None:
    """Compute a single item's weighted score from stored dimensions."""
    dims = item.get("dimensions") or _dimensions_from_row(item)
    if dims is None:
        return None
    return weighted_score(dims, weights)
