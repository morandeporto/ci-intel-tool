"""Ordered fallback_models chain: switch order, blocked models, no loops, Ask kept out."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from src.db.connection import get_connection, init_db
from src.db.repository import Repository
from src.pipeline.run_daily import (
    ModelFallbackChain,
    rescore_fallback_items,
    run_daily,
    run_rescore_only,
)
from src.process.llm_quota import (
    DailyQuotaError,
    next_fallback_model,
    pipeline_fallback_chain,
    resolve_pipeline_model,
)
from tests.test_pipeline_integration import (
    FIXTURE_SOURCES_HAPPY,
    _happy_feeds,
    _install_pipeline_fixtures,
)
from tests.test_rescore_quota_release import (
    NOW,
    _install_rescore_mocks,
    _payload,
    _seed_fallbacks,
)

PRIMARY = "gemini-3.8-flash"
FIRST = "gemini-3.5-flash-lite"
SECOND = "gemini-2.5-flash-lite"
ASK = "gemini-3.1-flash-lite"


def _ok_response(prompt: str, response_schema: Any) -> str:
    name = getattr(response_schema, "__name__", "") if response_schema else ""
    if name == "BatchedClassificationResponse":
        found = re.findall(r"=== ITEM id=([^\s=]+) ===", prompt)
        return json.dumps({"items": [_payload(i) for i in found]})
    return json.dumps(_payload())


def _gemini_quota_for(quota_models: set[str], calls: list[str]):
    """Mock that raises PerDay for every model in ``quota_models``."""

    def gemini(*, prompt: str, model_id: str, response_schema: Any = None, **kwargs: Any) -> str:
        del kwargs
        calls.append(model_id)
        if model_id in quota_models:
            raise DailyQuotaError("429 Quota exceeded PerDay", model_id=model_id)
        return _ok_response(prompt, response_schema)

    return gemini


def _run_row(db_path: Path, run_id: str) -> tuple[str, str]:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT status, error_message FROM pipeline_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return str(row[0]), str(row[1] or "")
    finally:
        conn.close()


def _statuses(db_path: Path) -> list[tuple[str, str | None]]:
    conn = get_connection(db_path)
    try:
        return [
            (str(r[0]), r[1])
            for r in conn.execute("SELECT status, scored_by_model FROM news_items")
        ]
    finally:
        conn.close()


def _seeded_db(tmp_path: Path, n: int, *, blocked: tuple[str, ...] = ()) -> Path:
    db_path = tmp_path / "chain.db"
    init_db(db_path)
    conn = get_connection(db_path)
    _seed_fallbacks(conn, n)
    repo = Repository(conn)
    for model_id in blocked:
        repo.set_model_blocked_until(model_id, NOW + timedelta(hours=6), retry_hint="6h")
    conn.close()
    return db_path


# --- config helpers ---------------------------------------------------------


def test_chain_keeps_order_and_drops_primary_duplicates_and_ask() -> None:
    cfg = {
        "pipeline_model": PRIMARY,
        "ask_model": ASK,
        "fallback_models": [FIRST, PRIMARY, ASK, FIRST, " ", SECOND],
    }
    assert pipeline_fallback_chain(cfg) == [FIRST, SECOND]


def test_chain_accepts_single_string_and_missing_key() -> None:
    assert pipeline_fallback_chain({"pipeline_model": PRIMARY, "fallback_models": FIRST}) == [
        FIRST
    ]
    assert pipeline_fallback_chain({"pipeline_model": PRIMARY}) == []


def test_next_fallback_skips_tried_and_blocked() -> None:
    chain = [FIRST, SECOND]
    assert next_fallback_model(chain, {PRIMARY}, lambda m: False) == FIRST
    assert next_fallback_model(chain, {PRIMARY}, lambda m: m == FIRST) == SECOND
    assert next_fallback_model(chain, {PRIMARY, FIRST}, lambda m: False) == SECOND
    assert next_fallback_model(chain, {PRIMARY}, lambda m: True) is None


def test_use_fallback_starts_on_first_entry() -> None:
    cfg = {"pipeline_model": PRIMARY, "fallback_models": [FIRST, SECOND]}
    assert resolve_pipeline_model(cfg, use_fallback=True) == FIRST
    with pytest.raises(ValueError, match="fallback_models"):
        resolve_pipeline_model({"pipeline_model": PRIMARY}, use_fallback=True)


def test_shipped_config_chain_excludes_ask_model() -> None:
    from src.config_loader import load_model_config

    cfg = load_model_config()
    chain = pipeline_fallback_chain(cfg)
    assert chain == [FIRST, SECOND]
    assert cfg["ask_model"] not in chain
    for model_id in chain:
        assert model_id in cfg["model_daily_limits"]
        assert model_id in cfg["model_min_interval_seconds"]


# --- rescore ----------------------------------------------------------------


def test_rescore_walks_chain_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _seeded_db(tmp_path, 4)  # batch_size=2 → 2 batches
    calls: list[str] = []
    _install_rescore_mocks(
        monkeypatch,
        gemini=_gemini_quota_for({PRIMARY, FIRST}, calls),
        batch_size=2,
        fallback_models=[FIRST, SECOND],
    )

    result = run_rescore_only(db_path=db_path, trigger="retry")

    assert result.status == "success"
    assert result.items_rescored_ok == 4
    assert calls == [PRIMARY, FIRST, SECOND, SECOND]
    assert all(status == "classified" and model == SECOND for status, model in _statuses(db_path))
    _, msg = _run_row(db_path, result.run_id)
    assert (
        f"switched to {FIRST} after PerDay on {PRIMARY}; "
        f"switched to {SECOND} after PerDay on {FIRST}"
    ) in msg


def test_rescore_skips_a_blocked_middle_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _seeded_db(tmp_path, 2, blocked=(FIRST,))
    calls: list[str] = []
    _install_rescore_mocks(
        monkeypatch,
        gemini=_gemini_quota_for({PRIMARY}, calls),
        batch_size=2,
        fallback_models=[FIRST, SECOND],
    )

    result = run_rescore_only(db_path=db_path, trigger="retry")

    assert calls == [PRIMARY, SECOND], "blocked model must not be called"
    _, msg = _run_row(db_path, result.run_id)
    assert f"switched to {SECOND} after PerDay on {PRIMARY}" in msg
    assert f"switched to {FIRST}" not in msg


def test_rescore_all_models_exhausted_stops_without_looping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _seeded_db(tmp_path, 4)
    calls: list[str] = []
    _install_rescore_mocks(
        monkeypatch,
        gemini=_gemini_quota_for({PRIMARY, FIRST, SECOND}, calls),
        batch_size=2,
        fallback_models=[FIRST, SECOND],
    )

    result = run_rescore_only(db_path=db_path, trigger="retry")

    assert result.status == "degraded"
    assert calls == [PRIMARY, FIRST, SECOND], "each model at most once, no loop back"
    assert all(status == "pending_scoring" for status, _ in _statuses(db_path))
    _, msg = _run_row(db_path, result.run_id)
    assert "daily quota" in msg


def test_rescore_all_fallbacks_blocked_never_calls_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _seeded_db(tmp_path, 2, blocked=(FIRST, SECOND))
    calls: list[str] = []
    _install_rescore_mocks(
        monkeypatch,
        gemini=_gemini_quota_for({PRIMARY}, calls),
        batch_size=2,
        fallback_models=[FIRST, SECOND],
    )

    result = run_rescore_only(db_path=db_path, trigger="retry")

    assert result.status == "degraded"
    assert calls == [PRIMARY]
    assert "switched to" not in _run_row(db_path, result.run_id)[1]


def test_use_fallback_model_starts_on_first_and_can_move_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _seeded_db(tmp_path, 2)
    calls: list[str] = []
    _install_rescore_mocks(
        monkeypatch,
        gemini=_gemini_quota_for({FIRST}, calls),
        batch_size=2,
        fallback_models=[FIRST, SECOND],
    )

    result = run_rescore_only(db_path=db_path, use_fallback_model=True, trigger="retry")

    assert result.status == "success"
    assert calls == [FIRST, SECOND], "never falls back to the primary"


def test_ask_model_in_chain_is_never_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _seeded_db(tmp_path, 2)
    calls: list[str] = []
    cfg = _install_rescore_mocks(
        monkeypatch,
        gemini=_gemini_quota_for({PRIMARY}, calls),
        batch_size=2,
        fallback_models=[ASK],
    )
    assert cfg["ask_model"] == ASK

    result = run_rescore_only(db_path=db_path, trigger="retry")

    assert result.status == "degraded"
    assert ASK not in calls


def test_shared_chain_does_not_reuse_models_tried_earlier_in_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daily ingest passes its chain to the auto-rescore: models it already used stay used."""
    db_path = _seeded_db(tmp_path, 2)
    calls: list[str] = []
    cfg = _install_rescore_mocks(
        monkeypatch,
        gemini=_gemini_quota_for({FIRST, SECOND}, calls),
        batch_size=2,
        fallback_models=[FIRST, SECOND],
    )
    chain = ModelFallbackChain(chain=[FIRST, SECOND], tried={PRIMARY, FIRST, SECOND})
    conn = get_connection(db_path)
    try:
        stats = rescore_fallback_items(
            Repository(conn),
            model_cfg=cfg,
            weights={"jfrog_relevance": 1.0},
            source_meta={},
            model_id_override=SECOND,
            model_chain=chain,
        )
    finally:
        conn.close()

    assert calls == [SECOND]
    assert stats.quota_stopped is True
    assert stats.model_switch_notes == []


# --- daily ingest -----------------------------------------------------------


def test_daily_ingest_walks_chain_and_records_switches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "daily_chain.db"
    init_db(db_path)
    calls: list[str] = []
    _install_pipeline_fixtures(
        monkeypatch,
        sources=FIXTURE_SOURCES_HAPPY,
        feeds=_happy_feeds(),
        model_overrides={"batch_size": 2, "fallback_models": [FIRST, SECOND]},
        gemini=_gemini_quota_for({PRIMARY, FIRST}, calls),
    )

    result = run_daily(trigger="manual", db_path=db_path, skip_auto_rescore=True)

    assert result.status == "success"
    assert result.items_classified_ok == 4
    assert calls == [PRIMARY, FIRST, SECOND, SECOND]
    status, msg = _run_row(db_path, result.run_id)
    assert status == "success"
    assert (
        f"switched to {FIRST} after PerDay on {PRIMARY}; "
        f"switched to {SECOND} after PerDay on {FIRST}"
    ) in msg


def test_daily_ingest_all_models_exhausted_leaves_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "daily_exhausted.db"
    init_db(db_path)
    calls: list[str] = []
    _install_pipeline_fixtures(
        monkeypatch,
        sources=FIXTURE_SOURCES_HAPPY,
        feeds=_happy_feeds(),
        model_overrides={"batch_size": 2, "fallback_models": [FIRST, SECOND]},
        gemini=_gemini_quota_for({PRIMARY, FIRST, SECOND}, calls),
    )

    result = run_daily(trigger="manual", db_path=db_path)

    assert result.status == "degraded"
    assert calls == [PRIMARY, FIRST, SECOND], "no loop and no auto-rescore after quota"
    statuses = {status for status, _ in _statuses(db_path)}
    assert "classified" not in statuses
    assert "pending_scoring" in statuses
    _, msg = _run_row(db_path, result.run_id)
    assert "daily quota" in msg
    assert f"switched to {SECOND} after PerDay on {FIRST}" in msg
