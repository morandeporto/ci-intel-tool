"""Tests for pipeline run status including degraded fallback ratio."""

from __future__ import annotations

from src.pipeline.run_daily import _resolve_status


def test_degraded_when_fallback_ratio_over_30_percent() -> None:
    status = _resolve_status(
        items_fetched=10,
        items_new=10,
        items_scored=10,
        items_failed=0,
        items_fallback=4,
        source_errors=0,
        attempted=10,
    )
    assert status == "degraded"


def test_partial_when_fallback_ratio_at_or_below_30_percent() -> None:
    status = _resolve_status(
        items_fetched=10,
        items_new=10,
        items_scored=10,
        items_failed=0,
        items_fallback=3,
        source_errors=0,
        attempted=10,
    )
    assert status == "partial"


def test_failed_when_all_fallbacks() -> None:
    status = _resolve_status(
        items_fetched=5,
        items_new=5,
        items_scored=5,
        items_failed=0,
        items_fallback=5,
        source_errors=0,
        attempted=5,
    )
    assert status == "failed"


def test_success_when_no_fallbacks() -> None:
    status = _resolve_status(
        items_fetched=5,
        items_new=5,
        items_scored=5,
        items_failed=0,
        items_fallback=0,
        source_errors=0,
        attempted=5,
    )
    assert status == "success"
