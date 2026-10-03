"""Freshness window filter for ingested feed entries.

Only items published within the configured window (default 48h) are kept.
Future-dated items are dropped. Missing dates are logged and skipped — never crash.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from src.ingest.normalize import NormalizedEntry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FreshnessResult:
    """Entries inside the window plus counts for telemetry / dry-run reports."""

    kept: list[NormalizedEntry]
    missing_date: int = 0
    future_dated: int = 0
    too_old: int = 0


def parse_published_at(value: str | None) -> datetime | None:
    """Parse an ISO-8601 published_at string to aware UTC, or None if unusable."""
    if not value or not str(value).strip():
        return None
    text = str(value).strip()
    # Support trailing Z from feedparser / normalize.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def filter_by_freshness(
    entries: Sequence[NormalizedEntry],
    *,
    window_hours: int,
    now: datetime | None = None,
    source_id_for_log: str | None = None,
) -> FreshnessResult:
    """Keep entries with published_at in (now - window_hours, now].

    - Future published_at → dropped (not kept).
    - Missing / unparseable published_at → logged, dropped, counted.
    - Older than the window → dropped.
    """
    if window_hours <= 0:
        raise ValueError(f"window_hours must be positive, got {window_hours}")

    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    cutoff = now_utc - timedelta(hours=window_hours)
    kept: list[NormalizedEntry] = []
    missing_date = 0
    future_dated = 0
    too_old = 0

    for entry in entries:
        published = parse_published_at(entry.published_at)
        if published is None:
            missing_date += 1
            src = source_id_for_log or entry.source_id
            logger.warning(
                "Skipping item with missing/unparseable published_at "
                "(source=%s url=%s title=%r)",
                src,
                entry.url,
                entry.title[:120],
            )
            continue
        if published > now_utc:
            future_dated += 1
            continue
        if published < cutoff:
            too_old += 1
            continue
        kept.append(entry)

    return FreshnessResult(
        kept=kept,
        missing_date=missing_date,
        future_dated=future_dated,
        too_old=too_old,
    )
