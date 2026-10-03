"""Unit tests for digest news-date filtering (Israel calendar day)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from src.services.digest import (
    filter_digest_by_news_dates,
    israel_today,
    item_news_date,
)

ISRAEL = ZoneInfo("Asia/Jerusalem")


def test_item_news_date_prefers_ingested_at() -> None:
    """Filter day = when the item entered our DB, not original publish day."""
    item = {
        "published_at": "2026-03-10T12:00:00+00:00",
        "ingested_at": "2026-03-15T08:00:00+00:00",
    }
    assert item_news_date(item) == date(2026, 3, 15)


def test_item_news_date_falls_back_to_published_at() -> None:
    item = {"published_at": "2026-01-10T10:00:00Z", "ingested_at": None}
    assert item_news_date(item) == date(2026, 1, 10)


def test_filter_digest_by_single_day_default_today() -> None:
    today = israel_today()
    yesterday = date.fromordinal(today.toordinal() - 1)
    items = [
        {
            "id": "a",
            # Old article, ingested today → should match "today"
            "published_at": "2020-01-01T12:00:00Z",
            "ingested_at": datetime(
                today.year, today.month, today.day, 9, 0, tzinfo=ISRAEL
            )
            .astimezone(timezone.utc)
            .isoformat(),
        },
        {
            "id": "b",
            "published_at": datetime(
                yesterday.year, yesterday.month, yesterday.day, 9, 0, tzinfo=ISRAEL
            )
            .astimezone(timezone.utc)
            .isoformat(),
            "ingested_at": datetime(
                yesterday.year, yesterday.month, yesterday.day, 10, 0, tzinfo=ISRAEL
            )
            .astimezone(timezone.utc)
            .isoformat(),
        },
    ]
    filtered = filter_digest_by_news_dates(items, [today])
    assert [i["id"] for i in filtered] == ["a"]


def test_filter_empty_selection_returns_empty() -> None:
    items = [{"id": "a", "ingested_at": "2026-01-01T12:00:00Z"}]
    assert filter_digest_by_news_dates(items, []) == []


def test_item_news_date_uses_israel_day_boundary() -> None:
    """UTC timestamps convert to Asia/Jerusalem before the calendar day is taken.

    In January Israel is UTC+2: 21:30 UTC is still Jan 10 locally, but 22:30 UTC
    is already Jan 11 00:30 Israel.
    """
    before = {
        "ingested_at": "2026-01-10T21:30:00+00:00",
        "published_at": "2020-01-01T00:00:00+00:00",
    }
    after = {
        "ingested_at": "2026-01-10T22:30:00+00:00",
        "published_at": "2020-01-01T00:00:00+00:00",
    }
    assert item_news_date(before) == date(2026, 1, 10)
    assert item_news_date(after) == date(2026, 1, 11)
