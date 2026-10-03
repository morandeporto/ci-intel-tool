"""Tests for fallback rescoring selection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.process.rescore import select_fallbacks_for_rescore


NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)


def _row(
    item_id: str,
    *,
    hours_ago: float,
    is_fallback: bool = True,
) -> dict:
    ingested = (NOW - timedelta(hours=hours_ago)).isoformat()
    return {
        "id": item_id,
        "ingested_at": ingested,
        "is_fallback": is_fallback,
        "title": item_id,
    }


def test_selects_newest_fallbacks_within_days() -> None:
    rows = [
        _row("old", hours_ago=24 * 4),  # outside 3 days
        _row("mid", hours_ago=24 * 2),
        _row("new", hours_ago=1),
        _row("ok", hours_ago=5, is_fallback=False),
    ]
    selected = select_fallbacks_for_rescore(
        rows, within_days=3, limit=10, now=NOW
    )
    assert [r["id"] for r in selected] == ["new", "mid"]


def test_respects_limit_newest_first() -> None:
    rows = [_row(f"i{i}", hours_ago=i) for i in range(5)]
    selected = select_fallbacks_for_rescore(
        rows, within_days=3, limit=2, now=NOW
    )
    assert [r["id"] for r in selected] == ["i0", "i1"]


def test_skips_missing_ingested_at() -> None:
    rows = [
        {"id": "bad", "ingested_at": None, "is_fallback": True},
        _row("good", hours_ago=2),
    ]
    selected = select_fallbacks_for_rescore(
        rows, within_days=3, limit=10, now=NOW
    )
    assert [r["id"] for r in selected] == ["good"]


def test_rejects_invalid_window_or_limit() -> None:
    with pytest.raises(ValueError):
        select_fallbacks_for_rescore([], within_days=0, limit=1)
    with pytest.raises(ValueError):
        select_fallbacks_for_rescore([], within_days=1, limit=0)
