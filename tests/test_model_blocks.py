"""Hard PerDay blocked_until: parse, persist, and pre-call gating (no live Gemini)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from src.db.connection import get_connection, init_db
from src.db.repository import Repository
from src.ingest.normalize import NormalizedEntry
from src.process.llm_classify import LlmUsageGuard, classify_entries_batch
from src.process.llm_quota import (
    BLOCK_SAFETY_MARGIN_SECONDS,
    DailyQuotaError,
    compute_blocked_until,
    format_reset_times,
    friendly_quota_message,
    parse_retry_delay,
    raise_if_daily_quota,
)


def test_parse_retry_delay_composite() -> None:
    delay = parse_retry_delay("18h55m33s")
    assert delay == timedelta(hours=18, minutes=55, seconds=33)
    assert parse_retry_delay("3h42m12s") == timedelta(hours=3, minutes=42, seconds=12)
    assert parse_retry_delay("90s") == timedelta(seconds=90)
    assert parse_retry_delay("not-a-duration") is None


def test_compute_blocked_until_uses_hint_plus_margin() -> None:
    now = datetime(2026, 10, 4, 5, 10, 8, tzinfo=timezone.utc)
    until = compute_blocked_until("18h55m33s", now=now, safety_margin_seconds=120)
    expected = now + timedelta(hours=18, minutes=55, seconds=33 + 120)
    assert until == expected.replace(microsecond=0)


def test_format_reset_times_utc_and_israel() -> None:
    until = datetime(2026, 10, 5, 0, 7, 41, tzinfo=timezone.utc)
    utc_label, israel_label = format_reset_times(until)
    assert "2026-10-05" in utc_label
    assert "00:07:41" in utc_label
    # Asia/Jerusalem is UTC+3 in October (IDT).
    assert "03:07:41" in israel_label
    assert until.astimezone(ZoneInfo("Asia/Jerusalem")).hour == 3


def test_raise_if_daily_quota_sets_blocked_until() -> None:
    exc = RuntimeError(
        "429 Quota exceeded PerDay Please retry in 18h55m33s for model gemini-x"
    )
    with pytest.raises(DailyQuotaError) as caught:
        raise_if_daily_quota(exc, model_id="gemini-x")
    assert caught.value.blocked_until is not None
    assert caught.value.retry_hint is not None
    assert "18h" in caught.value.retry_hint


def test_repo_model_block_roundtrip(tmp_path) -> None:
    db = tmp_path / "blocks.db"
    init_db(db)
    conn = get_connection(db)
    repo = Repository(conn)
    until = datetime(2026, 10, 5, 0, 7, 41, tzinfo=timezone.utc)
    repo.set_model_blocked_until("gemini-3.8-flash", until, retry_hint="18h55m33s")
    got = repo.get_model_blocked_until("gemini-3.8-flash")
    assert got == until
    # Upsert overwrites.
    later = until + timedelta(hours=1)
    repo.set_model_blocked_until("gemini-3.8-flash", later, retry_hint="1h")
    assert repo.get_model_blocked_until("gemini-3.8-flash") == later
    repo.clear_model_block("gemini-3.8-flash")
    assert repo.get_model_blocked_until("gemini-3.8-flash") is None
    conn.close()


def test_guard_check_hard_block_raises_without_api(tmp_path) -> None:
    db = tmp_path / "guard.db"
    init_db(db)
    conn = get_connection(db)
    repo = Repository(conn)
    until = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(microsecond=0)
    repo.set_model_blocked_until("m-blocked", until, retry_hint="2h")
    cfg = {"model_daily_limits": {"m-blocked": 100}, "quota_day_timezone": "UTC"}
    guard = LlmUsageGuard(repo, cfg, purpose="pipeline", model_id="m-blocked")
    with pytest.raises(DailyQuotaError) as caught:
        guard.check_before_call()
    assert caught.value.soft_budget is False
    assert caught.value.blocked_until == until
    conn.close()


def test_guard_record_hard_quota_persists_block(tmp_path) -> None:
    db = tmp_path / "rec.db"
    init_db(db)
    conn = get_connection(db)
    repo = Repository(conn)
    cfg = {"quota_day_timezone": "UTC"}
    guard = LlmUsageGuard(repo, cfg, purpose="compare", model_id="m1")
    now = datetime(2026, 10, 4, 5, 0, 0, tzinfo=timezone.utc)
    exc = DailyQuotaError(
        "PerDay",
        model_id="m1",
        retry_hint="1h0m0s",
        soft_budget=False,
    )
    stored = guard.record_hard_quota_block(exc, now=now)
    expected = compute_blocked_until("1h0m0s", now=now)
    assert stored == expected
    assert repo.get_model_blocked_until("m1") == expected
    conn.close()


def test_classify_batch_skips_api_when_blocked(tmp_path) -> None:
    db = tmp_path / "skip.db"
    init_db(db)
    conn = get_connection(db)
    repo = Repository(conn)
    until = datetime.now(timezone.utc) + timedelta(hours=6)
    repo.set_model_blocked_until("gemini-blocked", until)
    cfg = {
        "pipeline_model": "gemini-blocked",
        "model_id": "gemini-blocked",
        "batch_size": 5,
        "max_excerpt_chars": 100,
        "request_timeout_seconds": 5,
        "rate_limit_sleep_seconds": 0,
        "llm_max_attempts": 1,
        "llm_retry_base_seconds": 0.01,
        "llm_retry_max_seconds": 0.01,
        "quota_day_timezone": "UTC",
    }
    guard = LlmUsageGuard(repo, cfg, purpose="pipeline", model_id="gemini-blocked")
    entry = NormalizedEntry(
        title="T",
        url="https://example.com/t",
        published_at="2026-10-04T00:00:00+00:00",
        raw_excerpt="x",
        source_id="s",
        competitor="snyk",
        content_hash="h1",
    )
    with patch(
        "src.process.llm_classify._gemini_generate",
        side_effect=AssertionError("API must not be called while blocked"),
    ):
        with pytest.raises(DailyQuotaError):
            classify_entries_batch(
                [entry],
                model_config=cfg,
                usage_guard=guard,
                model_id_override="gemini-blocked",
                api_key="fake-key",
            )
    conn.close()


def test_friendly_quota_message_includes_israel_time() -> None:
    until = datetime(2026, 10, 5, 0, 7, 41, tzinfo=timezone.utc)
    msg = friendly_quota_message(blocked_until=until, model_id="gemini-x")
    assert "gemini-x" in msg
    assert "03:07:41" in msg or "2026-10-05" in msg
    assert "Israel" in msg or "IDT" in msg or "UTC" in msg


def test_safety_margin_constant_positive() -> None:
    assert BLOCK_SAFETY_MARGIN_SECONDS >= 60
