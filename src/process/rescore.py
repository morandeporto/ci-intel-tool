"""Select stored fallback items for a Gemini rescoring pass."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Sequence


def parse_ingested_at(value: str | None) -> datetime | None:
    if not value or not str(value).strip():
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def select_fallbacks_for_rescore(
    rows: Sequence[dict[str, Any]],
    *,
    within_days: int,
    limit: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return fallback rows to rescore: newest first, within the lookback window.

    Only rows with ``is_fallback`` truthy are considered. Missing/unparseable
    ``ingested_at`` values are skipped (not crashed on).
    """
    if within_days < 1:
        raise ValueError("within_days must be >= 1")
    if limit < 1:
        raise ValueError("limit must be >= 1")

    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)
    cutoff = now_utc - timedelta(days=within_days)

    eligible: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        if not row.get("is_fallback"):
            continue
        ingested = parse_ingested_at(row.get("ingested_at"))
        if ingested is None:
            continue
        if ingested < cutoff:
            continue
        eligible.append((ingested, row))

    eligible.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _dt, row in eligible[:limit]]


def select_items_for_rescore(
    rows: Sequence[dict[str, Any]],
    *,
    within_days: int,
    limit: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return pending_scoring / scoring / is_fallback rows: newest first, capped.

    Used by end-of-run heal and the nightly retry job (shared selection).
    """
    if within_days < 1:
        raise ValueError("within_days must be >= 1")
    if limit < 1:
        raise ValueError("limit must be >= 1")

    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)
    cutoff = now_utc - timedelta(days=within_days)

    eligible: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        status = str(row.get("status") or "")
        is_pending = status in ("pending_scoring", "scoring")
        is_fallback = bool(row.get("is_fallback"))
        if not is_pending and not is_fallback:
            continue
        ingested = parse_ingested_at(row.get("ingested_at"))
        if ingested is None:
            continue
        if ingested < cutoff:
            continue
        eligible.append((ingested, row))

    eligible.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _dt, row in eligible[:limit]]
