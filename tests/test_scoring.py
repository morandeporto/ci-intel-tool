"""Unit tests for weighted relevance scoring."""

from __future__ import annotations

import pytest

from src.db.models import DimensionScores
from src.process.scoring import (
    ScoringError,
    recalculate_scores,
    validate_dimensions,
    validate_weights,
    weighted_score,
)

EQUAL_WEIGHTS = {
    "jfrog_relevance": 0.30,
    "competitor_signal": 0.25,
    "strategic_impact": 0.25,
    "freshness": 0.10,
    "market_visibility": 0.10,
}

ALL_THREES = {
    "jfrog_relevance": 3,
    "competitor_signal": 3,
    "strategic_impact": 3,
    "freshness": 3,
    "market_visibility": 3,
}


def test_weighted_score_basic_formula() -> None:
    dims = {
        "jfrog_relevance": 5,
        "competitor_signal": 4,
        "strategic_impact": 3,
        "freshness": 2,
        "market_visibility": 1,
    }
    expected = 5 * 0.30 + 4 * 0.25 + 3 * 0.25 + 2 * 0.10 + 1 * 0.10
    assert weighted_score(dims, EQUAL_WEIGHTS) == pytest.approx(expected)


def test_weighted_score_accepts_dimension_scores_dataclass() -> None:
    scores = DimensionScores(
        jfrog_relevance=3,
        competitor_signal=3,
        strategic_impact=3,
        freshness=3,
        market_visibility=3,
        model_id="test-model",
        scored_at="2026-01-01T00:00:00+00:00",
    )
    assert weighted_score(scores, EQUAL_WEIGHTS) == pytest.approx(3.0)


def test_uniform_dimensions_equals_that_value() -> None:
    assert weighted_score(ALL_THREES, EQUAL_WEIGHTS) == pytest.approx(3.0)


def test_rejects_dimension_out_of_range() -> None:
    bad = dict(ALL_THREES)
    bad["freshness"] = 6
    with pytest.raises(ScoringError, match="out of range"):
        weighted_score(bad, EQUAL_WEIGHTS)


def test_rejects_dimension_below_one() -> None:
    bad = dict(ALL_THREES)
    bad["jfrog_relevance"] = 0
    with pytest.raises(ScoringError, match="out of range"):
        validate_dimensions(bad)


def test_rejects_missing_dimension() -> None:
    incomplete = dict(ALL_THREES)
    del incomplete["market_visibility"]
    with pytest.raises(ScoringError, match="Missing dimension"):
        validate_dimensions(incomplete)


def test_rejects_weights_not_summing_to_one() -> None:
    bad_weights = dict(EQUAL_WEIGHTS)
    bad_weights["jfrog_relevance"] = 0.50
    with pytest.raises(ScoringError, match="sum to 1.0"):
        validate_weights(bad_weights)


def test_weights_within_tolerance_accepted() -> None:
    # 0.005 drift is within the ±0.01 tolerance used by config loading.
    slightly_off = {
        "jfrog_relevance": 0.301,
        "competitor_signal": 0.25,
        "strategic_impact": 0.25,
        "freshness": 0.10,
        "market_visibility": 0.099,
    }
    assert sum(slightly_off.values()) == pytest.approx(1.0, abs=0.01)
    assert validate_weights(slightly_off)["jfrog_relevance"] == 0.301


def test_recalculate_scores_returns_id_score_pairs() -> None:
    items = [
        ("item-a", ALL_THREES),
        (
            "item-b",
            {
                "jfrog_relevance": 5,
                "competitor_signal": 5,
                "strategic_impact": 5,
                "freshness": 5,
                "market_visibility": 5,
            },
        ),
    ]
    results = recalculate_scores(items, EQUAL_WEIGHTS)
    assert results == [
        ("item-a", pytest.approx(3.0)),
        ("item-b", pytest.approx(5.0)),
    ]


def test_recalculate_scores_empty_input() -> None:
    assert recalculate_scores([], EQUAL_WEIGHTS) == []
