"""Retry helpers for transient LLM / network failures.

Retries 503, 429, and timeouts with exponential backoff + jitter.
Does not retry other 4xx client errors.
"""

from __future__ import annotations

import random
import re
import time
from collections import Counter
from typing import Callable, TypeVar

T = TypeVar("T")

# Match common provider error strings (Gemini, httpx, urllib).
_TRANSIENT_STATUS = re.compile(r"\b(429|503)\b")
_HTTP_STATUS = re.compile(r"\b(429|500|502|503|504)\b")
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
# Strip secrets if a provider ever echoes them into exception text.
_SECRET_RE = re.compile(
    r"(AIza[0-9A-Za-z_-]{20,}|Bearer\s+\S+|api[_-]?key\s*[=:]\s*\S+)",
    re.IGNORECASE,
)
_CAUSE_PREFIX_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*):(429|500|502|503|504|timeout|other)\b"
)


def is_transient_error(exc: BaseException) -> bool:
    """True for overload / rate-limit / timeout style failures."""
    # Daily PerDay quota must never be retried (see llm_quota.DailyQuotaError).
    if type(exc).__name__ == "DailyQuotaError":
        return False
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
    if type(exc).__name__ == "DailyQuotaError":
        return True
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


def sanitize_error_text(text: str, *, max_len: int = 240) -> str:
    """Strip likely secrets and truncate; never log request bodies or API keys."""
    cleaned = _SECRET_RE.sub("[redacted]", str(text or ""))
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > max_len:
        return cleaned[: max_len - 3] + "..."
    return cleaned


def _status_from_attrs(exc: BaseException) -> str | None:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    try:
        code = int(status) if status is not None else None
    except (TypeError, ValueError):
        code = None
    if code in (429, 500, 502, 503, 504):
        return str(code)
    return None


def http_status_label(exc_or_msg: BaseException | str) -> str:
    """Compact HTTP/status label: 503, 429, timeout, or other."""
    if isinstance(exc_or_msg, BaseException):
        from_attr = _status_from_attrs(exc_or_msg)
        if from_attr:
            return from_attr
        # Walk causes for nested provider exceptions.
        cur: BaseException | None = exc_or_msg
        seen: set[int] = set()
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            from_attr = _status_from_attrs(cur)
            if from_attr:
                return from_attr
            cur = cur.__cause__ or cur.__context__
        msg = str(exc_or_msg)
    else:
        msg = str(exc_or_msg)
    match = _HTTP_STATUS.search(msg)
    if match:
        return match.group(1)
    lower = msg.lower()
    if any(marker in lower for marker in _TIMEOUT_MARKERS):
        return "timeout"
    return "other"


def root_exception_type(exc: BaseException) -> str:
    """Innermost exception type name (prefer provider error over ClassifyError wrap)."""
    cur: BaseException = exc
    seen: set[int] = set()
    while True:
        seen.add(id(cur))
        nxt = cur.__cause__ or cur.__context__
        if nxt is None or id(nxt) in seen:
            break
        cur = nxt
    return type(cur).__name__


def format_model_failure_cause(exc: BaseException) -> str:
    """Safe ``ExceptionType:status`` label for logs / pipeline_runs.error_message."""
    return f"{root_exception_type(exc)}:{http_status_label(exc)}"


def summarize_model_failure_causes(
    error_lines: list[str],
    *,
    last_cause: str | None = None,
) -> str:
    """Aggregate per-item failure causes into a short, secret-free summary.

    Expects lines that begin with ``ExceptionType:status`` (optionally after a URL
    prefix ``url: Type:status ...``). Falls back to parsing HTTP status from text.
    """
    counts: Counter[str] = Counter()
    for raw in error_lines:
        line = str(raw or "").strip()
        if not line:
            continue
        # Strip optional "https://...: " prefix before the cause token.
        candidate = line
        if "://" in line:
            # url: Cause:status rest  OR  url: Gemini API call failed...
            parts = line.split(": ", 1)
            if len(parts) == 2 and "://" in parts[0]:
                candidate = parts[1]
        match = _CAUSE_PREFIX_RE.match(candidate)
        if match:
            cause = f"{match.group(1)}:{match.group(2)}"
        else:
            cause = f"Unknown:{http_status_label(candidate)}"
        counts[cause] += 1
    if not counts and not last_cause:
        return ""
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    buckets = ", ".join(f"{cause}×{n}" for cause, n in ranked)
    last = last_cause or (ranked[0][0] if ranked else "")
    if buckets and last:
        return f"last={last}; counts: {buckets}"
    if last:
        return f"last={last}"
    return f"counts: {buckets}"


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
        except Exception as exc:  # noqa: BLE001 - classified by helper predicates
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
