"""Unit tests for transient LLM retry / backoff helpers."""

from __future__ import annotations

import random

import pytest

from src.process.retry import (
    backoff_seconds,
    call_with_retries,
    is_non_retryable_client_error,
    is_transient_error,
)


def test_is_transient_detects_503_429_and_timeout() -> None:
    assert is_transient_error(RuntimeError("Gemini API call failed: 503 This model is overloaded"))
    assert is_transient_error(RuntimeError("HTTP 429 Too Many Requests"))
    assert is_transient_error(TimeoutError("Request timed out after 60s"))
    assert is_transient_error(RuntimeError("deadline exceeded"))


def test_is_transient_false_for_validation_errors() -> None:
    assert is_transient_error(ValueError("invalid JSON")) is False
    assert is_transient_error(RuntimeError("permission denied")) is False


def test_non_retryable_4xx_except_429() -> None:
    assert is_non_retryable_client_error(RuntimeError("HTTP 400 Bad Request")) is True
    assert is_non_retryable_client_error(RuntimeError("HTTP 401 Unauthorized")) is True
    assert is_non_retryable_client_error(RuntimeError("HTTP 404 Not Found")) is True
    assert is_non_retryable_client_error(RuntimeError("HTTP 429 Too Many Requests")) is False
    assert is_non_retryable_client_error(RuntimeError("HTTP 503 overload")) is False


def test_backoff_grows_exponentially_with_jitter_bounds() -> None:
    rng = random.Random(0)
    d0 = backoff_seconds(0, base_seconds=1.0, jitter_ratio=0.0, rng=rng)
    d1 = backoff_seconds(1, base_seconds=1.0, jitter_ratio=0.0, rng=rng)
    d2 = backoff_seconds(2, base_seconds=1.0, jitter_ratio=0.0, rng=rng)
    assert d0 == 1.0
    assert d1 == 2.0
    assert d2 == 4.0
    capped = backoff_seconds(10, base_seconds=1.0, max_seconds=5.0, jitter_ratio=0.0, rng=rng)
    assert capped == 5.0


def test_call_with_retries_succeeds_after_transient_failures() -> None:
    sleeps: list[float] = []
    state = {"n": 0}

    def flaky() -> str:
        state["n"] += 1
        if state["n"] < 3:
            raise RuntimeError("503 overloaded")
        return "ok"

    value, retries = call_with_retries(
        flaky,
        max_attempts=3,
        base_seconds=0.01,
        jitter_ratio=0.0,
        sleep_fn=sleeps.append,
        rng=random.Random(1),
    )
    assert value == "ok"
    assert retries == 2
    assert len(sleeps) == 2


def test_call_with_retries_does_not_retry_400() -> None:
    calls = {"n": 0}

    def bad_request() -> str:
        calls["n"] += 1
        raise RuntimeError("HTTP 400 invalid argument")

    with pytest.raises(RuntimeError, match="400"):
        call_with_retries(bad_request, max_attempts=3, sleep_fn=lambda _d: None)
    assert calls["n"] == 1


def test_call_with_retries_exhausts_on_persistent_503() -> None:
    calls = {"n": 0}

    def always_503() -> str:
        calls["n"] += 1
        raise RuntimeError("503 overloaded")

    with pytest.raises(RuntimeError, match="503") as ei:
        call_with_retries(
            always_503,
            max_attempts=3,
            base_seconds=0.001,
            jitter_ratio=0.0,
            sleep_fn=lambda _d: None,
        )
    assert calls["n"] == 3
    assert getattr(ei.value, "retries_used") == 2


def test_format_model_failure_cause_includes_type_and_status() -> None:
    from src.process.retry import format_model_failure_cause, root_exception_type

    root = RuntimeError("HTTP 503 overloaded")
    wrapped = Exception(f"Gemini API call failed: {root}")
    wrapped.__cause__ = root
    assert format_model_failure_cause(wrapped) == "RuntimeError:503"
    assert root_exception_type(wrapped) == "RuntimeError"
    assert format_model_failure_cause(TimeoutError("deadline exceeded")) == (
        "TimeoutError:timeout"
    )


def test_summarize_model_failure_causes_counts_by_status() -> None:
    from src.process.retry import (
        sanitize_error_text,
        summarize_model_failure_causes,
    )

    summary = summarize_model_failure_causes(
        [
            "https://ex.com/a: ServiceUnavailable:503: overloaded",
            "https://ex.com/b: ServiceUnavailable:503: overloaded",
            "https://ex.com/c: TimeoutError:timeout: deadline exceeded",
        ]
    )
    assert "last=ServiceUnavailable:503" in summary
    assert "ServiceUnavailable:503×2" in summary
    assert "TimeoutError:timeout×1" in summary
    assert "api_key" not in sanitize_error_text("api_key=AIzaSyDummyKeyValue1234567890abcd")
    assert "[redacted]" in sanitize_error_text(
        "failed api_key=AIzaSyDummyKeyValue1234567890abcd"
    )
