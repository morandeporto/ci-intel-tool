"""On rescore DailyQuotaError, release remaining claims immediately (not after 30 min)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from src.db.connection import get_connection, init_db
from src.pipeline.run_daily import run_rescore_only
from src.process.llm_quota import DailyQuotaError


NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


def _payload(item_id: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "summary": "Ok summary.",
        "category": "security",
        "item_type": "industry",
        "jfrog_implication": "Implication.",
        "jfrog_relevance": 4,
        "competitor_signal": 3,
        "strategic_impact": 3,
        "freshness": 4,
        "market_visibility": 3,
    }
    if item_id is not None:
        body["id"] = item_id
    return body


def _seed_fallbacks(conn: Any, n: int) -> list[str]:
    ids: list[str] = []
    for i in range(n):
        news_id = str(uuid4())
        ids.append(news_id)
        conn.execute(
            """
            INSERT INTO news_items (
                id, title, url, source_id, competitor, published_at, ingested_at,
                summary, category, raw_excerpt, content_hash, status, is_fallback,
                scored_by_model
            ) VALUES (?, ?, ?, 'snyk_blog', 'snyk', ?, ?,
                      'placeholder', 'other', 'excerpt', ?, 'classified', 1,
                      'gemini-3.8-flash')
            """,
            (
                news_id,
                f"Fallback item {i}",
                f"https://example.com/fb-{i}",
                NOW.isoformat(),
                NOW.isoformat(),
                f"hash-fb-{i}",
            ),
        )
    conn.commit()
    return ids


def _install_rescore_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gemini: Any,
    batch_size: int = 2,
    fallback_model: str = "",
) -> dict[str, Any]:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)

    from src.config_loader import load_model_config

    cfg = dict(load_model_config())
    cfg.update(
        {
            "rate_limit_sleep_seconds": 0,
            "llm_min_interval_seconds": 0,
            "model_min_interval_seconds": {
                "gemini-3.8-flash": 0,
                "gemini-3.5-flash-lite": 0,
            },
            "llm_max_attempts": 1,
            "llm_retry_base_seconds": 0,
            "llm_retry_max_seconds": 0,
            "batch_size": batch_size,
            "rescore_fallback_days": 3,
            "rescore_fallback_limit": 40,
            "scoring_claim_timeout_minutes": 30,
            "fallback_model": fallback_model,
            "model_daily_limits": {
                "gemini-3.8-flash": 1000,
                "gemini-3.5-flash-lite": 1000,
            },
        }
    )
    monkeypatch.setattr("src.pipeline.run_daily.load_model_config", lambda: dict(cfg))
    monkeypatch.setattr("src.process.llm_classify.load_model_config", lambda: dict(cfg))
    monkeypatch.setattr(
        "src.pipeline.run_daily.load_weights",
        lambda: {
            "jfrog_relevance": 0.3,
            "competitor_signal": 0.25,
            "strategic_impact": 0.2,
            "freshness": 0.15,
            "market_visibility": 0.1,
        },
    )
    monkeypatch.setattr(
        "src.pipeline.run_daily._source_meta_by_id",
        lambda: {"snyk_blog": {"kind": "official_competitor"}},
    )

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            if tz is None:
                return NOW.replace(tzinfo=None)
            return NOW.astimezone(tz)

    monkeypatch.setattr("src.process.rescore.datetime", _FrozenDateTime)
    monkeypatch.setattr("src.pipeline.run_daily.datetime", _FrozenDateTime)
    monkeypatch.setattr("src.process.llm_classify.time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr("src.process.retry.time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr("src.process.llm_rate_limit.wait_llm_interval", lambda: None)
    monkeypatch.setattr("src.process.llm_classify._gemini_generate", gemini)
    return cfg


def test_rescore_quota_releases_remaining_claims_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First batch finishes; second hits PerDay → remaining claims → pending_scoring now."""
    db_path = tmp_path / "release.db"
    init_db(db_path)
    conn = get_connection(db_path)
    ids = _seed_fallbacks(conn, 5)  # batch_size=2 → batches of 2,2,1
    conn.close()

    calls = {"n": 0}

    def gemini(*, prompt: str, response_schema: Any = None, **kwargs: Any) -> str:
        del kwargs
        calls["n"] += 1
        # First successful batch generate; every later call is hard PerDay.
        if calls["n"] == 1:
            name = getattr(response_schema, "__name__", "") if response_schema else ""
            if name == "BatchedClassificationResponse":
                found = re.findall(r"=== ITEM id=([^\s=]+) ===", prompt)
                return json.dumps({"items": [_payload(i) for i in found]})
            return json.dumps(_payload())
        raise DailyQuotaError(
            "429 Quota exceeded PerDay",
            model_id="gemini-3.8-flash",
        )

    cfg = _install_rescore_mocks(
        monkeypatch, gemini=gemini, batch_size=2, fallback_model=""
    )

    result = run_rescore_only(db_path=db_path, trigger="retry")
    assert result.status == "degraded"
    assert result.items_rescored_ok == 2

    conn = get_connection(db_path)
    try:
        rows = {
            r[0]: r
            for r in conn.execute(
                "SELECT id, status, is_fallback, filter_reason FROM news_items"
            ).fetchall()
        }
        # Assert by status counts: finished earlier batch stays classified;
        # remaining claimed rows become pending_scoring immediately.
        classified = [i for i, r in rows.items() if r[1] == "classified" and r[2] == 0]
        pending = [i for i, r in rows.items() if r[1] == "pending_scoring"]
        scoring = [i for i, r in rows.items() if r[1] == "scoring"]
        assert len(classified) == 2, classified
        assert len(pending) == 3, pending
        assert len(scoring) == 0, "remaining claims must not stay in status=scoring"
        for pid in pending:
            assert rows[pid][2] == 0  # is_fallback cleared
            assert rows[pid][3] == "daily_quota"
        # Finished rows stay classified (not rewritten to pending).
        for cid in classified:
            assert rows[cid][3] in (None, "")
    finally:
        conn.close()
