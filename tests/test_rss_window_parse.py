"""Ensure huge feeds only normalize entries inside the freshness window."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from src.ingest.rss_fetcher import _entry_in_window


NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)


def test_entry_in_window_keeps_recent() -> None:
    published = NOW - timedelta(hours=10)
    raw = {"published_parsed": published.timetuple()}
    assert _entry_in_window(raw, window_hours=48, now=NOW) is True


def test_entry_in_window_drops_old_archive_items() -> None:
    published = NOW - timedelta(days=30)
    raw = {"published_parsed": published.timetuple()}
    assert _entry_in_window(raw, window_hours=48, now=NOW) is False


def test_entry_in_window_drops_future_and_missing() -> None:
    future = NOW + timedelta(hours=2)
    assert (
        _entry_in_window(
            {"published_parsed": future.timetuple()},
            window_hours=48,
            now=NOW,
        )
        is False
    )
    assert _entry_in_window({}, window_hours=48, now=NOW) is False
