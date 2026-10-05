"""Tests for pipeline run status including degraded fallback ratio."""

from __future__ import annotations

from src.pipeline.run_daily import PipelineResult, _resolve_status, exit_code_for_live_run


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


def test_degraded_when_all_fallbacks_stored_for_rescore() -> None:
    status = _resolve_status(
        items_fetched=5,
        items_new=5,
        items_scored=5,
        items_failed=0,
        items_fallback=5,
        source_errors=0,
        attempted=5,
    )
    assert status == "degraded"


def test_all_fallbacks_exit_zero_with_gha_warning(monkeypatch, capsys) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    result = PipelineResult(
        status="degraded",
        run_id="r1",
        message="Model call failed for every selected item (2/2 fallback)",
    )
    assert exit_code_for_live_run(result) == 0
    assert "::warning::Run degraded:" in capsys.readouterr().out


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


def test_live_exit_code_failed_is_one() -> None:
    result = PipelineResult(status="failed", run_id=None, message="hard fail")
    assert exit_code_for_live_run(result) == 1


def test_live_exit_code_degraded_is_zero_without_gha_warning(
    monkeypatch, capsys
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    result = PipelineResult(
        status="degraded",
        run_id="r1",
        message="daily quota: 3 item(s) left pending_scoring",
    )
    assert exit_code_for_live_run(result) == 0
    assert "::warning::" not in capsys.readouterr().out


def test_live_exit_code_degraded_emits_gha_warning(monkeypatch, capsys) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    result = PipelineResult(
        status="degraded",
        run_id="r1",
        message="high fallback ratio",
    )
    assert exit_code_for_live_run(result) == 0
    out = capsys.readouterr().out
    assert "::warning::Run degraded: high fallback ratio" in out


def test_live_exit_code_success_and_partial_are_zero() -> None:
    assert exit_code_for_live_run(PipelineResult(status="success", run_id=None)) == 0
    assert exit_code_for_live_run(PipelineResult(status="partial", run_id=None)) == 0
