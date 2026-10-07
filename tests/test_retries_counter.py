"""retries_used counts retries once per API call, not once per item in a batch."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from src.db.connection import get_connection, init_db
from src.pipeline.run_daily import run_daily, run_rescore_only
from src.process.llm_classify import ClassifyError
from tests.test_pipeline_integration import (
    FIXTURE_SOURCES_HAPPY,
    _happy_feeds,
    _install_pipeline_fixtures,
)
from tests.test_rescore_quota_release import (
    _install_rescore_mocks,
    _payload,
    _seed_fallbacks,
)


def _flaky_gemini(transient_failures: int, calls: dict[str, int]):
    """First ``transient_failures`` calls raise a retryable 503, then every call succeeds."""

    def gemini(*, prompt: str, response_schema: Any = None, **kwargs: Any) -> str:
        del kwargs
        calls["n"] += 1
        if calls["n"] <= transient_failures:
            raise ClassifyError("Gemini API call failed: 503 Service Unavailable")
        name = getattr(response_schema, "__name__", "") if response_schema else ""
        if name == "BatchedClassificationResponse":
            found = re.findall(r"=== ITEM id=([^\s=]+) ===", prompt)
            return json.dumps({"items": [_payload(i) for i in found]})
        return json.dumps(_payload())

    return gemini


def test_rescore_counts_batch_retries_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "rescore_retries.db"
    init_db(db_path)
    conn = get_connection(db_path)
    _seed_fallbacks(conn, 5)
    conn.close()

    calls = {"n": 0}
    cfg = _install_rescore_mocks(
        monkeypatch, gemini=_flaky_gemini(2, calls), batch_size=5
    )
    cfg["llm_max_attempts"] = 3

    result = run_rescore_only(db_path=db_path, trigger="retry")

    assert result.items_rescored_ok == 5
    assert calls["n"] == 3  # one batch call: 2 transient failures + 1 success
    assert result.retries_used == 2  # previously 2 x 5 items = 10


def test_daily_ingest_counts_batch_retries_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "daily_retries.db"
    init_db(db_path)
    calls = {"n": 0}
    _install_pipeline_fixtures(
        monkeypatch,
        sources=FIXTURE_SOURCES_HAPPY,
        feeds=_happy_feeds(),
        model_overrides={"batch_size": 2, "llm_max_attempts": 3},
        gemini=_flaky_gemini(1, calls),
    )

    result = run_daily(trigger="manual", db_path=db_path, skip_auto_rescore=True)

    assert result.items_classified_ok >= 2, "first batch must hold at least 2 items"
    assert result.items_fallback == 0
    assert result.retries_used == 1  # previously 1 x items in the first batch

    conn = get_connection(db_path)
    try:
        stored = conn.execute(
            "SELECT retries_used FROM pipeline_runs WHERE id = ?", (result.run_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert stored == 1
