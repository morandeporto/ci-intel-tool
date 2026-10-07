"""Turso/libsql returns plain tuple rows, not sqlite3.Row.

Regression coverage for the 2026-10-06 outage: a column-name read on a tuple
row raised TypeError inside the usage guard before any Gemini call, so every
item silently became a fallback. These tests drive the real repository and
classify path over a tuple-row connection with only the network call mocked.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.db.connection import init_db
from src.db.repository import Repository, _scalar
from src.ingest.normalize import NormalizedEntry
from src.process.llm_classify import LlmUsageGuard, classify_entry

VALID_PAYLOAD = {
    "summary": "Fixture summary.",
    "category": "security",
    "item_type": "industry",
    "jfrog_implication": "Fixture implication.",
    "jfrog_relevance": 4,
    "competitor_signal": 3,
    "strategic_impact": 3,
    "freshness": 4,
    "market_visibility": 3,
}


def _tuple_row_connection(db_path: Path) -> sqlite3.Connection:
    """Same schema as production, but rows come back as tuples like libsql."""
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = None
    return conn


def test_scalar_index_none_falls_back_to_first_column_on_tuple() -> None:
    assert _scalar(("2026-10-06T00:01:59+00:00",), "blocked_until", None) == (
        "2026-10-06T00:01:59+00:00"
    )
    assert _scalar(("a", "b"), "second", 1) == "b"
    assert _scalar(None, "x", None) is None


def test_get_model_blocked_until_reads_tuple_rows(tmp_path: Path) -> None:
    conn = _tuple_row_connection(tmp_path / "turso_like.db")
    try:
        repo = Repository(conn)
        until = datetime(2026, 10, 6, 0, 1, 59, tzinfo=timezone.utc)
        repo.set_model_blocked_until("gemini-3.8-flash", until, retry_hint="5h40m37")
        # Sanity: this connection really yields tuples.
        raw = conn.execute("SELECT blocked_until FROM llm_model_blocks").fetchone()
        assert isinstance(raw, tuple)
        assert repo.get_model_blocked_until("gemini-3.8-flash") == until
    finally:
        conn.close()


def test_classify_entry_reaches_gemini_through_guard_with_expired_tuple_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real prompt build + usage guard + retry path; only the Gemini call is mocked."""
    monkeypatch.setattr("src.process.llm_classify.wait_llm_interval", lambda: None)
    conn = _tuple_row_connection(tmp_path / "turso_like.db")
    try:
        repo = Repository(conn)
        model_id = "gemini-3.8-flash"
        expired = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0)
        repo.set_model_blocked_until(model_id, expired, retry_hint="expired")
        cfg = {
            "pipeline_model": model_id,
            "model_id": model_id,
            "max_excerpt_chars": 400,
            "request_timeout_seconds": 5,
            "rate_limit_sleep_seconds": 0,
            "llm_max_attempts": 3,
            "llm_retry_base_seconds": 0.0,
            "llm_retry_max_seconds": 0.0,
            "model_daily_limits": {model_id: 100},
            "quota_day_timezone": "UTC",
        }
        guard = LlmUsageGuard(repo, cfg, purpose="pipeline", model_id=model_id)
        entry = NormalizedEntry(
            title="Dependency installation security measure already defeated on npm",
            url="https://example.com/npm-measure-defeated",
            published_at="2026-10-06T15:00:00+00:00",
            raw_excerpt="New malware bypasses the install script measure.",
            source_id="reversinglabs_blog",
            competitor="industry",
            content_hash="h-npm",
        )

        with patch(
            "src.process.llm_classify._gemini_generate",
            return_value=json.dumps(VALID_PAYLOAD),
        ) as gemini:
            result, retries = classify_entry(
                entry,
                model_config=cfg,
                api_key="fake-key",
                usage_guard=guard,
                model_id_override=model_id,
            )

        assert gemini.call_count == 1
        assert gemini.call_args.kwargs["model_id"] == model_id
        assert "UNTRUSTED_CONTENT" in gemini.call_args.kwargs["prompt"]
        assert result.category == "security"
        assert retries == 0
        # Expired block is cleared and the call is counted, both over tuple rows.
        assert repo.get_model_blocked_until(model_id) is None
        assert repo.get_llm_usage_calls(date_utc=guard.day_key(), model=model_id) == 1
    finally:
        conn.close()
