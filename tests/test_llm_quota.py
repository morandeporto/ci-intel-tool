"""Quota detection, soft budgets, and scoring claims (no live Gemini calls)."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from src.db.connection import get_connection, init_db
from src.db.models import NewsItem
from src.db.repository import Repository
from src.process.llm_quota import (
    DailyQuotaError,
    extract_retry_hint,
    is_daily_quota_error,
    quota_day_key,
    raise_if_daily_quota,
    warn_if_budgets_exceed_limits,
)


def test_is_daily_quota_detects_per_day() -> None:
    msg = (
        "429 Resource exhausted: Quota exceeded for metric "
        "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
        "limit: 20, model: gemini-3.8-flash QuotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier"
    )
    assert is_daily_quota_error(msg) is True
    assert is_daily_quota_error("503 Service Unavailable") is False
    # Transient 429 without PerDay is not a daily-quota hard signal by itself.
    assert is_daily_quota_error("429 Too Many Requests") is False


def test_raise_if_daily_quota_includes_retry_hint() -> None:
    exc = RuntimeError(
        "Quota exceeded PerDay Please retry in 3h42m12s for model gemini-x"
    )
    with pytest.raises(DailyQuotaError) as caught:
        raise_if_daily_quota(exc, model_id="gemini-x")
    assert caught.value.retry_hint is not None
    assert "3h" in caught.value.retry_hint or "3h42m12s" in caught.value.retry_hint


def test_extract_retry_hint() -> None:
    assert extract_retry_hint("Please retry in 1h2m3s.") is not None


def test_quota_day_key_utc() -> None:
    fixed = datetime(2026, 10, 3, 23, 30, tzinfo=timezone.utc)
    assert quota_day_key(tz_name="UTC", now=fixed) == "2026-10-03"


def test_warn_if_budgets_exceed_limits() -> None:
    cfg = {
        "pipeline_model": "gemini-3.8-flash",
        "model_id": "gemini-3.8-flash",
        "batch_size": 5,
        "max_items_per_run": 20,
        "rescore_fallback_limit": 40,
        "model_daily_limits": {"gemini-3.8-flash": 2},
    }
    warnings = warn_if_budgets_exceed_limits(cfg)
    assert warnings


def test_llm_usage_increment(tmp_path) -> None:
    db = tmp_path / "u.db"
    init_db(db)
    conn = get_connection(db)
    repo = Repository(conn)
    day = "2026-10-03"
    assert repo.get_llm_usage_calls(date_utc=day, model="m1") == 0
    repo.increment_llm_usage(date_utc=day, purpose="pipeline", model="m1", calls=3)
    repo.increment_llm_usage(date_utc=day, purpose="ask", model="m1", calls=1)
    assert repo.get_llm_usage_calls(date_utc=day, model="m1") == 4
    assert repo.get_llm_usage_calls(date_utc=day, model="m1", purpose="ask") == 1
    conn.close()


def _pending_item(item_id: str) -> NewsItem:
    return NewsItem(
        id=item_id,
        title=f"T {item_id}",
        url=f"https://example.com/{item_id}",
        source_id="snyk_blog",
        competitor="snyk",
        published_at="2026-10-03T00:00:00+00:00",
        ingested_at="2026-10-03T12:00:00+00:00",
        summary=None,
        category=None,
        raw_excerpt="x",
        content_hash=f"h{item_id}",
        relevance_score=None,
        run_id=None,
        status="pending_scoring",
        filter_reason="daily_quota",
        is_fallback=False,
    )


def test_claim_item_for_scoring_atomic_race(tmp_path) -> None:
    """Two workers racing for the same pending items - each id claimed once."""
    db = tmp_path / "race.db"
    init_db(db)
    conn = get_connection(db)
    repo = Repository(conn)
    ids = [f"n{i}" for i in range(6)]
    for i in ids:
        repo.upsert_news_item(_pending_item(i))

    won_a: list[str] = []
    won_b: list[str] = []
    barrier = threading.Barrier(2)

    def worker(bucket: list[str]) -> None:
        local = get_connection(db)
        local_repo = Repository(local)
        barrier.wait()
        for item_id in ids:
            if local_repo.claim_item_for_scoring(item_id):
                bucket.append(item_id)
        local.close()

    t1 = threading.Thread(target=worker, args=(won_a,))
    t2 = threading.Thread(target=worker, args=(won_b,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert sorted(won_a + won_b) == sorted(ids)
    assert set(won_a).isdisjoint(set(won_b))
    # All rows should now be scoring.
    rows = repo.list_pending_or_fallback_items()
    statuses = {r["id"]: r["status"] for r in rows}
    assert all(statuses[i] == "scoring" for i in ids)
    conn.close()


def test_release_stale_scoring_claims(tmp_path) -> None:
    db = tmp_path / "stale.db"
    init_db(db)
    conn = get_connection(db)
    repo = Repository(conn)
    repo.upsert_news_item(_pending_item("stale1"))
    assert repo.claim_item_for_scoring("stale1")
    # Force an old claim stamp.
    repo.conn.execute(
        """
        UPDATE news_items
        SET filter_reason = 'scoring_claimed_at:2020-01-01T00:00:00+00:00'
        WHERE id = 'stale1'
        """
    )
    repo.conn.commit()
    released = repo.release_stale_scoring_claims(older_than_minutes=30)
    assert released == 1
    row = repo.conn.execute(
        "SELECT status FROM news_items WHERE id = 'stale1'"
    ).fetchone()
    assert row[0] == "pending_scoring"
    conn.close()
