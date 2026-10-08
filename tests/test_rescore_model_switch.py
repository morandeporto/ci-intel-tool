"""Rescore switches to the first fallback_models entry on a daily quota, like the daily ingest."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from src.db.connection import get_connection, init_db
from src.db.repository import Repository
from src.pipeline.run_daily import run_rescore_only
from src.process.llm_quota import DailyQuotaError
from tests.test_rescore_quota_release import (
    NOW,
    _install_rescore_mocks,
    _payload,
    _seed_fallbacks,
)

PRIMARY = "gemini-3.8-flash"
FALLBACK = "gemini-3.5-flash-lite"


def _ok_response(prompt: str, response_schema: Any) -> str:
    name = getattr(response_schema, "__name__", "") if response_schema else ""
    if name == "BatchedClassificationResponse":
        found = re.findall(r"=== ITEM id=([^\s=]+) ===", prompt)
        return json.dumps({"items": [_payload(i) for i in found]})
    return json.dumps(_payload())


def _gemini_by_model(quota_models: set[str], calls: list[str], *, ok_primary_calls: int = 0):
    """Mock that raises PerDay for models in ``quota_models`` (after N primary successes)."""

    def gemini(*, prompt: str, model_id: str, response_schema: Any = None, **kwargs: Any) -> str:
        del kwargs
        calls.append(model_id)
        if model_id == PRIMARY and calls.count(PRIMARY) <= ok_primary_calls:
            return _ok_response(prompt, response_schema)
        if model_id in quota_models:
            raise DailyQuotaError("429 Quota exceeded PerDay", model_id=model_id)
        return _ok_response(prompt, response_schema)

    return gemini


def _rows(db_path: Path) -> list[tuple[Any, ...]]:
    conn = get_connection(db_path)
    try:
        return conn.execute(
            "SELECT id, status, is_fallback, scored_by_model FROM news_items"
        ).fetchall()
    finally:
        conn.close()


def _run_error_message(db_path: Path, run_id: str) -> str:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT status, error_message FROM pipeline_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return f"{row[0]}|{row[1] or ''}"
    finally:
        conn.close()


def test_rescore_switches_once_and_stores_fallback_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Batch 1 on primary, batch 2 hits PerDay → same batch + rest run on fallback."""
    db_path = tmp_path / "switch.db"
    init_db(db_path)
    conn = get_connection(db_path)
    _seed_fallbacks(conn, 5)  # batch_size=2 → 2,2,1
    conn.close()

    calls: list[str] = []
    _install_rescore_mocks(
        monkeypatch,
        gemini=_gemini_by_model({PRIMARY}, calls, ok_primary_calls=1),
        batch_size=2,
        fallback_models=[FALLBACK],
    )

    result = run_rescore_only(db_path=db_path, trigger="retry")

    assert result.status == "success"
    assert result.items_rescored_ok == 5
    # primary ok, primary PerDay, then fallback for batch 2 (retried) and batch 3.
    assert calls == [PRIMARY, PRIMARY, FALLBACK, FALLBACK]
    rows = _rows(db_path)
    assert all(r[1] == "classified" and r[2] == 0 for r in rows)
    by_model = sorted(r[3] for r in rows)
    assert by_model == [FALLBACK] * 3 + [PRIMARY] * 2
    status_and_msg = _run_error_message(db_path, result.run_id)
    assert status_and_msg.startswith("success|")
    assert f"switched to {FALLBACK} after PerDay on {PRIMARY}" in status_and_msg


def test_rescore_switches_at_most_once_when_fallback_also_hits_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "once.db"
    init_db(db_path)
    conn = get_connection(db_path)
    _seed_fallbacks(conn, 4)
    conn.close()

    calls: list[str] = []
    _install_rescore_mocks(
        monkeypatch,
        gemini=_gemini_by_model({PRIMARY, FALLBACK}, calls),
        batch_size=2,
        fallback_models=[FALLBACK],
    )

    result = run_rescore_only(db_path=db_path, trigger="retry")

    assert result.status == "degraded"
    assert result.items_rescored_ok == 0
    assert calls == [PRIMARY, FALLBACK], "one switch, no loop back to primary"
    rows = _rows(db_path)
    assert all(r[1] == "pending_scoring" for r in rows)
    msg = _run_error_message(db_path, result.run_id)
    assert f"switched to {FALLBACK} after PerDay on {PRIMARY}" in msg
    assert "daily quota" in msg


def test_rescore_does_not_switch_when_fallback_is_already_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "blocked.db"
    init_db(db_path)
    conn = get_connection(db_path)
    _seed_fallbacks(conn, 2)
    Repository(conn).set_model_blocked_until(
        FALLBACK, NOW + timedelta(hours=6), retry_hint="6h"
    )
    conn.close()

    calls: list[str] = []
    _install_rescore_mocks(
        monkeypatch,
        gemini=_gemini_by_model({PRIMARY}, calls),
        batch_size=2,
        fallback_models=[FALLBACK],
    )

    result = run_rescore_only(db_path=db_path, trigger="retry")

    assert result.status == "degraded"
    assert calls == [PRIMARY], "blocked fallback must not be called"
    assert all(r[1] == "pending_scoring" for r in _rows(db_path))
    assert "switched to" not in _run_error_message(db_path, result.run_id)


def test_rescore_with_use_fallback_model_never_switches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--use-fallback-model already runs on the fallback; quota there just stops."""
    db_path = tmp_path / "already_fb.db"
    init_db(db_path)
    conn = get_connection(db_path)
    _seed_fallbacks(conn, 2)
    conn.close()

    calls: list[str] = []
    _install_rescore_mocks(
        monkeypatch,
        gemini=_gemini_by_model({FALLBACK}, calls),
        batch_size=2,
        fallback_models=[FALLBACK],
    )

    result = run_rescore_only(db_path=db_path, use_fallback_model=True, trigger="retry")

    assert result.status == "degraded"
    assert calls == [FALLBACK]
    assert "switched to" not in _run_error_message(db_path, result.run_id)
