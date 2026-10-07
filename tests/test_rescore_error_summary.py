"""Rescore runs write the same failure-cause summary to error_message as the daily run."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from src.db.connection import get_connection, init_db
from src.pipeline.run_daily import run_rescore_only
from src.process.llm_classify import ClassifyError
from tests.test_rescore_quota_release import (
    _install_rescore_mocks,
    _payload,
    _seed_fallbacks,
)


class ServiceUnavailable(Exception):
    """Stand-in for the provider SDK exception wrapped by _gemini_generate."""


def _stored_error_message(db_path: Path, run_id: str) -> str | None:
    conn = get_connection(db_path)
    try:
        return conn.execute(
            "SELECT error_message FROM pipeline_runs WHERE id = ?", (run_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_rescore_error_message_has_cause_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "summary.db"
    init_db(db_path)
    conn = get_connection(db_path)
    _seed_fallbacks(conn, 3)
    conn.close()

    def outage(**kwargs: Any) -> str:
        del kwargs
        try:
            raise ServiceUnavailable("503 Service Unavailable")
        except ServiceUnavailable as exc:
            raise ClassifyError("Gemini API call failed: 503 Service Unavailable") from exc

    _install_rescore_mocks(monkeypatch, gemini=outage, batch_size=5)

    result = run_rescore_only(db_path=db_path, trigger="retry")

    assert result.items_rescored_fallback == 3
    stored = _stored_error_message(db_path, result.run_id) or ""
    assert stored.startswith("Rescored pending/fallbacks: attempted=3 ok=0 still_fallback=3")
    assert "; last=ServiceUnavailable:503; counts: ServiceUnavailable:503×3" in stored


def test_rescore_success_has_no_error_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "ok.db"
    init_db(db_path)
    conn = get_connection(db_path)
    _seed_fallbacks(conn, 2)
    conn.close()

    def ok(*, prompt: str, response_schema: Any = None, **kwargs: Any) -> str:
        del kwargs
        found = re.findall(r"=== ITEM id=([^\s=]+) ===", prompt)
        return json.dumps({"items": [_payload(i) for i in found]})

    _install_rescore_mocks(monkeypatch, gemini=ok, batch_size=5)

    result = run_rescore_only(db_path=db_path, trigger="retry")

    assert result.status == "success"
    assert _stored_error_message(db_path, result.run_id) is None
