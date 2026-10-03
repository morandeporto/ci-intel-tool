"""Tests for the configured freshness window filter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.ingest.normalize import NormalizedEntry
from src.process.freshness import filter_by_freshness, parse_published_at


def _entry(
    *,
    title: str = "Sample",
    url: str = "https://example.com/a",
    published_at: str | None,
    source_id: str = "src",
) -> NormalizedEntry:
    return NormalizedEntry(
        title=title,
        url=url,
        published_at=published_at,
        raw_excerpt="body",
        source_id=source_id,
        competitor="jfrog",
        content_hash="hash-" + url,
    )


NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)


def test_keeps_items_inside_48h_window() -> None:
    inside = _entry(published_at=(NOW - timedelta(hours=47)).isoformat())
    result = filter_by_freshness([inside], window_hours=48, now=NOW)
    assert result.kept == [inside]
    assert result.too_old == 0


def test_drops_items_older_than_window() -> None:
    old = _entry(published_at=(NOW - timedelta(hours=49)).isoformat())
    result = filter_by_freshness([old], window_hours=48, now=NOW)
    assert result.kept == []
    assert result.too_old == 1


def test_ignores_future_published_dates() -> None:
    future = _entry(published_at=(NOW + timedelta(hours=1)).isoformat())
    result = filter_by_freshness([future], window_hours=48, now=NOW)
    assert result.kept == []
    assert result.future_dated == 1


def test_missing_date_logged_and_skipped_no_crash(caplog: pytest.LogCaptureFixture) -> None:
    missing = _entry(published_at=None, url="https://example.com/missing")
    bad = _entry(published_at="not-a-date", url="https://example.com/bad")
    with caplog.at_level("WARNING"):
        result = filter_by_freshness([missing, bad], window_hours=48, now=NOW)
    assert result.kept == []
    assert result.missing_date == 2
    assert "missing/unparseable published_at" in caplog.text


def test_date_only_midnight_inside_48h_morning_run() -> None:
    # Date-only stamps at midnight look "old" vs a 24h window on a morning run.
    published = datetime(2026, 10, 2, 0, 0, 0, tzinfo=timezone.utc)
    morning = datetime(2026, 10, 3, 8, 0, 0, tzinfo=timezone.utc)
    entry = _entry(published_at=published.isoformat())
    assert filter_by_freshness([entry], window_hours=24, now=morning).kept == []
    assert filter_by_freshness([entry], window_hours=48, now=morning).kept == [entry]


def test_parse_published_at_accepts_z_suffix() -> None:
    dt = parse_published_at("2026-10-03T10:00:00Z")
    assert dt is not None
    assert dt.tzinfo is not None
