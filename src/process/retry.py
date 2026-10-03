"""Retry helpers for transient LLM / network failures.

Retries 503, 429, and timeouts with exponential backoff + jitter.
Does not retry other 4xx client errors.
"""

from __future__ import annotations

import random
import re
import time
from typing import Callable, TypeVar

T = TypeVar("T")

# Match common provider error strings (Gemini, httpx, urllib).
_TRANSIENT_STATUS = re.compile(r"\b(429|503)\b")
_TIMEOUT_MARKERS = (
    "timeout",
    "timed out",
    "deadline exceeded",
    "temporarily unavailable",
    "service unavailable",
    "resource exhausted",
    "rate limit",
    "too many requests",
)


def is_transient_error(exc: BaseException) -> bool:
    """True for overload / rate-limit / timeout style failures."""
    msg = str(exc).lower()
    if _TRANSIENT_STATUS.search(msg):
        # Explicitly allow 429/503 even when other digits appear in the message.
        if "429" in msg or "503" in msg:
            return True
    if any(marker in msg for marker in _TIMEOUT_MARKERS):
        return True
    # httpx / requests style attributes when present.
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    try:
        if int(status) in (429, 503):
            return True
    except (TypeError, ValueError):
        pass
    # Nested response.status_code (common on HTTPError wrappers).
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            if int(getattr(response, "status_code", 0)) in (429, 503):
                return True
        except (TypeError, ValueError):
            pass
    return False


def is_non_retryable_client_error(exc: BaseException) -> bool:
    """True for 4xx other than 429 (bad request, auth, not found, …)."""
    msg = str(exc)
    # Prefer structured status when available.
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    try:
        code = int(status) if status is not None else None
    except (TypeError, ValueError):
        code = None
    if code is not None:
        return 400 <= code < 500 and code != 429
    # String fallback: 40x / 41x / 42x except 429.
    match = re.search(r"\b(4\d\d)\b", msg)
    if not match:
        return False
    code = int(match.group(1))
    return 400 <= code < 500 and code != 429


def backoff_seconds(
    attempt_index: int,
    *,
    base_seconds: float = 1.0,
    max_seconds: float = 30.0,
    jitter_ratio: float = 0.25,
    rng: random.Random | None = None,
) -> float:
    """Exponential backoff for attempt_index 0..n-1 before the next try.

    attempt_index 0 → ~base, 1 → ~2*base, 2 → ~4*base, capped, with jitter.
    """
    if attempt_index < 0:
        raise ValueError("attempt_index must be >= 0")
    raw = min(max_seconds, float(base_seconds) * (2**attempt_index))
    rng = rng or random.Random()
    jitter = raw * jitter_ratio * rng.uniform(-1.0, 1.0)
    return max(0.0, raw + jitter)


def call_with_retries(
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_seconds: float = 1.0,
    max_seconds: float = 30.0,
    jitter_ratio: float = 0.25,
    sleep_fn: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    is_transient: Callable[[BaseException], bool] = is_transient_error,
) -> tuple[T, int]:
    """Call ``fn`` up to ``max_attempts`` times on transient errors.

    Returns ``(value, retries_used)`` where retries_used is attempts after the first.
    Non-transient errors (including 4xx other than 429) are raised immediately.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    rng = rng or random.Random()
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return fn(), attempt
        except Exception as exc:  # noqa: BLE001 — classified by helper predicates
            last_exc = exc
            if is_non_retryable_client_error(exc):
                raise
            if not is_transient(exc):
                raise
            if attempt >= max_attempts - 1:
                # WHY: callers that catch and fall back still need retries_used.
                setattr(exc, "retries_used", attempt)
                break
            delay = backoff_seconds(
                attempt,
                base_seconds=base_seconds,
                max_seconds=max_seconds,
                jitter_ratio=jitter_ratio,
                rng=rng,
            )
            sleep_fn(delay)
    assert last_exc is not None
    setattr(last_exc, "retries_used", max_attempts - 1)
    raise last_exc
