"""Daily digest service: list and re-rank news without touching Streamlit."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from src.config_loader import load_weights
from src.db.models import DIMENSION_NAMES
from src.db.repository import Repository
from src.process.scoring import recalculate_scores, weighted_score

DigestSort = Literal["date", "relevance"]
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


def _dimensions_from_row(row: dict[str, Any]) -> dict[str, int] | None:
    """Extract dimension scores from a joined news row, or None if unscored."""
    if row.get("jfrog_relevance") is None:
        return None
    return {name: int(row[name]) for name in DIMENSION_NAMES}


def _item_created_at(row: dict[str, Any]) -> str:
    """Prefer ingest time (DB creation); fall back to source publish time."""
    return str(row.get("ingested_at") or row.get("published_at") or "")


def israel_today() -> date:
    """Calendar 'today' in Asia/Jerusalem (matches card timestamps)."""
    return datetime.now(ISRAEL_TZ).date()


def item_news_date(row: dict[str, Any]) -> date | None:
    """Digest calendar day in Israel TZ: ingest time (when it entered our DB).

    Falls back to published_at only if ingested_at is missing — matches the
    Daily Digest "Creation date" sort, so "today" shows what was ingested today.
    """
    raw = row.get("ingested_at") or row.get("published_at")
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ISRAEL_TZ).date()
    except ValueError:
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                return None
        return None


def filter_digest_by_news_dates(
    items: list[dict[str, Any]],
    selected_dates: list[date] | set[date] | None,
) -> list[dict[str, Any]]:
    """Keep items whose news date is in ``selected_dates``. Empty selection → []."""
    if not selected_dates:
        return []
    wanted = {d if isinstance(d, date) else date.fromisoformat(str(d)) for d in selected_dates}
    return [item for item in items if item_news_date(item) in wanted]


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
        row["is_fallback"] = bool(row.get("is_fallback"))
        dims = _dimensions_from_row(row)
        # Fallback rows are stored without ranking totals / usable scores.
        if dims is not None and not row["is_fallback"]:
            scored_pairs.append((row["id"], dims))

    if scored_pairs:
        recalculated = dict(recalculate_scores(scored_pairs, weights))
        for row in rows:
            if row["is_fallback"]:
                row["relevance_score"] = None
                row["dimensions"] = None
            elif row["id"] in recalculated:
                row["relevance_score"] = recalculated[row["id"]]
                row["dimensions"] = _dimensions_from_row(row)
            else:
                row["dimensions"] = None
        if persist_scores:
            repo.update_relevance_scores(recalculated.items())
    else:
        for row in rows:
            if row["is_fallback"]:
                row["relevance_score"] = None
                row["dimensions"] = None
            else:
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
    # Exclude fallback / unscored rows from average and high-score KPIs.
    scored_items = [
        i
        for i in items
        if i.get("relevance_score") is not None and not i.get("is_fallback")
    ]
    scores = [float(i["relevance_score"]) for i in scored_items]
    latest_run = repo.get_latest_run()
    return {
        "item_count": len(items),
        "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
        "high_score_count": sum(1 for s in scores if s >= 4.0),
        "feedback_count": len(repo.list_feedback()),
        "latest_run_status": latest_run["status"] if latest_run else None,
        "latest_run_at": latest_run["started_at"] if latest_run else None,
        "fallback_count": sum(1 for i in items if i.get("is_fallback")),
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
