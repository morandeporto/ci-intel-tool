"""Local programming errors must fail the run; model/API errors keep degrading.

Background: for two days a TypeError raised inside the usage guard (before any
Gemini call) was treated like a model outage, so every item became a mid-score
fallback, the run stayed "degraded", and the workflow stayed green.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.db.connection import get_connection, init_db
from src.db.repository import Repository
from src.ingest.normalize import NormalizedEntry
from src.pipeline.run_daily import (
    exit_code_for_live_run,
    run_daily,
    run_rescore_only,
)
from src.process.llm_classify import (
    ClassifyError,
    LlmUsageGuard,
    PipelineInternalError,
    classify_entries_batch_with_fallback,
    classify_entry_with_fallback,
)
from tests.test_pipeline_integration import (
    FIXTURE_SOURCES_HAPPY,
    NOW,
    _happy_feeds,
    _install_pipeline_fixtures,
)

BUG_MESSAGE = "tuple indices must be integers or slices, not NoneType"


def _entry(suffix: str = "1") -> NormalizedEntry:
    return NormalizedEntry(
        title=f"Item {suffix}",
        url=f"https://example.com/item-{suffix}",
        published_at="2026-10-03T10:00:00+00:00",
        raw_excerpt="Excerpt.",
        source_id="snyk_blog",
        competitor="snyk",
        content_hash=f"hash-{suffix}",
    )


def _unit_cfg() -> dict[str, Any]:
    return {
        "pipeline_model": "m",
        "model_id": "m",
        "max_excerpt_chars": 200,
        "request_timeout_seconds": 1,
        "rate_limit_sleep_seconds": 0,
        "llm_max_attempts": 2,
        "llm_retry_base_seconds": 0,
        "llm_retry_max_seconds": 0,
        "quota_day_timezone": "UTC",
    }


def _guard_that_raises_locally(tmp_path: Path) -> tuple[LlmUsageGuard, Any]:
    """Real guard over a real repo whose block lookup reproduces the Oct-6 bug."""
    db = tmp_path / "guard.db"
    init_db(db)
    conn = get_connection(db)
    repo = Repository(conn)

    def _broken(model: str):  # type: ignore[no-untyped-def]
        raise TypeError(BUG_MESSAGE)

    repo.get_model_blocked_until = _broken  # type: ignore[method-assign]
    return LlmUsageGuard(repo, _unit_cfg(), purpose="pipeline", model_id="m"), conn


# ---------------------------------------------------------------------------
# Unit level: the classify wrappers
# ---------------------------------------------------------------------------
def test_batch_wrapper_raises_internal_error_without_calling_gemini(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.process.llm_classify.wait_llm_interval", lambda: None)
    guard, conn = _guard_that_raises_locally(tmp_path)
    try:
        with patch("src.process.llm_classify._gemini_generate") as gemini:
            with pytest.raises(PipelineInternalError) as caught:
                classify_entries_batch_with_fallback(
                    [_entry("a"), _entry("b")],
                    model_config=_unit_cfg(),
                    api_key="fake-key",
                    usage_guard=guard,
                    model_id_override="m",
                )
        assert gemini.call_count == 0
        assert isinstance(caught.value.__cause__, TypeError)
        assert "TypeError" in str(caught.value) and BUG_MESSAGE in str(caught.value)
    finally:
        conn.close()


def test_single_wrapper_raises_internal_error_without_calling_gemini(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.process.llm_classify.wait_llm_interval", lambda: None)
    guard, conn = _guard_that_raises_locally(tmp_path)
    try:
        with patch("src.process.llm_classify._gemini_generate") as gemini:
            with pytest.raises(PipelineInternalError):
                classify_entry_with_fallback(
                    _entry(),
                    model_config=_unit_cfg(),
                    api_key="fake-key",
                    usage_guard=guard,
                    model_id_override="m",
                )
        assert gemini.call_count == 0
    finally:
        conn.close()


def test_api_error_still_falls_back_in_wrappers(monkeypatch: pytest.MonkeyPatch) -> None:
    """503 / timeout from the provider keeps the mid-score fallback behavior."""
    monkeypatch.setattr("src.process.llm_classify.wait_llm_interval", lambda: None)
    monkeypatch.setattr("src.process.llm_classify.time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr("src.process.retry.time.sleep", lambda *_a, **_k: None)
    with patch(
        "src.process.llm_classify._gemini_generate",
        side_effect=ClassifyError("Gemini API call failed (m): 503 Service Unavailable"),
    ) as gemini:
        outcomes = classify_entries_batch_with_fallback(
            [_entry("a")], model_config=_unit_cfg(), api_key="fake-key"
        )
    assert gemini.call_count >= 1
    (_entry_out, result, used_fallback, err, retries) = outcomes[0]
    assert used_fallback is True
    assert result.category == "other"
    assert err and "503" in err
    assert retries >= 1  # transient 503 was retried (llm_max_attempts=2)


# ---------------------------------------------------------------------------
# Run level: pipeline_runs status + exit code
# ---------------------------------------------------------------------------
def _inject_local_bug(monkeypatch: pytest.MonkeyPatch) -> None:
    def _broken(self: Repository, model: str):  # type: ignore[no-untyped-def]
        raise TypeError(BUG_MESSAGE)

    monkeypatch.setattr(Repository, "get_model_blocked_until", _broken)


def test_run_daily_local_error_marks_run_failed_and_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "local_error.db"
    init_db(db_path)
    calls = _install_pipeline_fixtures(
        monkeypatch, sources=FIXTURE_SOURCES_HAPPY, feeds=_happy_feeds()
    )
    _inject_local_bug(monkeypatch)

    result = run_daily(trigger="cron", db_path=db_path, skip_auto_rescore=True)

    assert result.status == "failed"
    assert exit_code_for_live_run(result) == 1
    assert result.message.startswith("Internal error (not a model outage)")
    assert "TypeError" in result.message
    assert calls["n"] == 0, "no Gemini call should happen for a local bug"

    conn = get_connection(db_path)
    try:
        run = conn.execute(
            "SELECT status, error_message FROM pipeline_runs WHERE id = ?",
            (result.run_id,),
        ).fetchone()
        assert run is not None
        assert run[0] == "failed"
        assert "Internal error" in str(run[1])
        fallbacks = conn.execute(
            "SELECT COUNT(*) FROM news_items WHERE is_fallback = 1"
        ).fetchone()[0]
        assert fallbacks == 0, "a local bug must not be persisted as fallbacks"
    finally:
        conn.close()


def test_run_daily_api_error_stays_degraded_with_fallbacks_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "api_error.db"
    init_db(db_path)

    def outage(**kwargs: Any) -> str:
        del kwargs
        raise ClassifyError("Gemini API call failed (gemini-3.8-flash): 503 Service Unavailable")

    _install_pipeline_fixtures(
        monkeypatch, sources=FIXTURE_SOURCES_HAPPY, feeds=_happy_feeds(), gemini=outage
    )

    result = run_daily(trigger="cron", db_path=db_path, skip_auto_rescore=True)

    assert result.status == "degraded"
    assert exit_code_for_live_run(result) == 0
    assert result.items_fallback > 0
    assert result.items_classified_ok == 0
    assert "Model call failed for every selected item" in result.message
    assert "Internal error" not in result.message

    conn = get_connection(db_path)
    try:
        run = conn.execute(
            "SELECT status FROM pipeline_runs WHERE id = ?", (result.run_id,)
        ).fetchone()
        assert run[0] == "degraded"
        fallbacks = conn.execute(
            "SELECT COUNT(*) FROM news_items WHERE is_fallback = 1"
        ).fetchone()[0]
        assert fallbacks == result.items_fallback
    finally:
        conn.close()


def test_run_rescore_only_local_error_marks_run_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Retry workflow path must also go red on a local bug."""
    db_path = tmp_path / "rescore_local_error.db"
    init_db(db_path)
    calls = _install_pipeline_fixtures(
        monkeypatch, sources=FIXTURE_SOURCES_HAPPY, feeds=_happy_feeds()
    )
    # Seed one stored fallback row inside the rescore lookback window.
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO news_items (id, title, url, source_id, competitor, published_at,
                                    ingested_at, content_hash, status, is_fallback)
            VALUES ('fb-1', 'Stored fallback', 'https://example.com/fb-1', 'snyk_blog',
                    'snyk', ?, ?, 'hash-fb-1', 'classified', 1)
            """,
            (NOW.isoformat(), NOW.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    _inject_local_bug(monkeypatch)

    result = run_rescore_only(db_path=db_path, trigger="retry")

    assert result.status == "failed"
    assert result.message.startswith("Internal error (not a model outage)")
    assert calls["n"] == 0

    conn = get_connection(db_path)
    try:
        run = conn.execute(
            "SELECT status, error_message FROM pipeline_runs WHERE id = ?",
            (result.run_id,),
        ).fetchone()
        assert run[0] == "failed"
        assert "Internal error" in str(run[1])
    finally:
        conn.close()
